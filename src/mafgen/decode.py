"""AP-101S disassembler core for the mafgen replacement tool.

Decodes halfword pairs against the descriptor strings in
src/asm101/instr_defs.json (the same definitions the assembler encodes from,
extracted from ext/sim/gpc/cpu_instr.coffee) and renders MAFGEN-style
operand text.

Addressing semantics follow gpc's decodef/g_EA:
  * SRS short form: 6-bit D2 field; halfword ops use D2 as-is, fullword and
    doubleword ops use D2<<1 (fullword-granular displacement).
  * SRS escapes (non-shift): D2==0x3C -> extended long form (hw2 = 16-bit
    displacement; B2==11 means no base), D2==0x3D -> indexed long form
    (hw2 = X(3) IA(1) I(1) D(11)).  IAL additionally escapes on 0x3E/0x3F.
  * RS "/X" forms (D2 field fixed 1111xa in hw1): a==0 extended, a==1 indexed.
  * "/I" forms carry an immediate-data halfword.
  * BCF/BCB/BCTB are IC-relative: target = addr + len +/- D2.

MAFGEN rendering conventions (from the DASS listings):
  * numbers 0..9 print decimal, >=10 print X'%04X'
  * shift counts and branch condition masks always print decimal
  * indexed forms print (Rx,Rb) and suffix the mnemonic with '@' when IA=1
    (indirect) and '#' when I=1 (auto-modify), e.g. SCAL@#
"""
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from asm101.instrdefs import CPU_DEFS               # noqa: E402
from asm101.instrset import Descriptor              # noqa: E402

# mnemonics whose D2 field is a shift count (no SRS escapes, always decimal)
SHIFT_MNEMONICS = frozenset(
    name for name, d in CPU_DEFS.items()
    if any("Count" in f for f in d.get("fmt", ())))

# IC-relative SRS branches: EA = addr + len (+/-) D2
BRANCH_FWD = frozenset({"BCF"})
BRANCH_BACK = frozenset({"BCB", "BCTB"})
# Instructions whose EA is a branch target: these get the instruction-sector
# 19-bit expansion.  Data references print the 16-bit EA raw -- DASS
# evidence: every high-bit data EA (LH/ST/LA/...) prints unexpanded
# (e.g. $0DMPMMM LH X'ACC2' -> 00ACC2), every high-bit BC target expands.
BRANCH_EA = frozenset({"BAL", "BC", "BCT", "BIX", "BVC", "SCAL"})

# MAFGEN's display vocabulary where it differs from the assembler's
# mnemonic: the RR-form divide always prints TDR (every 4E-RR site in
# the SSW DASS; the RUNASM sources write DR)
_DISPLAY = {"DR": "TDR"}

# Floating-point instructions print F-registers (DASS: LER F0,F0;
# LE F1 X-disp; CVFX R5,F0; CVFL F0,R5).  Register numbers are the raw
# field values -- only the prefix letter changes.
FP_MNEMONICS = frozenset({
    "AE", "AED", "AEDR", "AER", "CE", "CED", "CEDR", "CER", "DE", "DED",
    "DEDR", "DER", "LE", "LECR", "LED", "LER", "LFLI", "LFLR", "ME",
    "MED", "MEDR", "MER", "SE", "SED", "SEDR", "SER", "STE", "STED",
    "CVFL", "CVFX"})


def _regpfx(name):
    """(first, second) register-operand prefix letters."""
    if name in ("CVFX", "LFXR"):
        # LFXR loads a fixed register FROM a float register (DASS:
        # 'LFXR R3,F1')
        return "R", "F"
    if name in ("CVFL", "LFLR"):
        # LFLR loads a float register FROM a fixed register (DASS:
        # 'LFLR F1,R3')
        return "F", "R"
    if name in FP_MNEMONICS:
        return "F", "F"
    return "R", "R"

_AW_SHIFT = {"HALFWORD": 0, "FULLWORD": 1, "DBLEWORD": 1, None: 0}


@dataclass
class Inst:
    name: str            # base mnemonic, no @/# suffixes
    nhw: int             # instruction length in halfwords (1 or 2)
    mnemonic: str        # rendered mnemonic incl. @/# suffixes
    operands: str        # rendered operand text, MAFGEN style
    ea: int | None       # effective address when statically known
    imm: int | None      # immediate value (RI/SI), else None
    fields: dict
    stack_ref: bool = False   # operand is R0(stack-frame)-relative
    aw: int = 0               # operand width shift: 0=halfword, 1=fullword
    idx_info: tuple | None = None   # indexed long form: (X, IA, I, D2)


class _Entry:
    __slots__ = ("name", "desc", "meta", "first_mask", "first_val", "fixed")

    def __init__(self, name, desc, meta):
        self.name = name
        self.desc = desc
        self.meta = meta
        if desc.nbits == 16:
            self.first_mask, self.first_val = desc.mask, desc.val
        else:
            self.first_mask = desc.mask >> 16
            self.first_val = desc.val >> 16
        self.fixed = bin(self.first_mask).count("1")


def _num(v):
    return str(v) if 0 <= v <= 9 else f"X'{v:04X}'"


def _expand(ea, addr, branch):
    """AP-101S expanded addressing (POO fig 2-18): a 16-bit EA with the
    high bit set takes its upper 4 bits from the sector register.  MAFGEN
    applies the referencing instruction's own sector (addr >> 15) to
    branch targets only; data EAs print as the raw 16-bit value -- unless
    the base register is tracked to a full 19-bit address (a relocated
    address constant), in which case the full-address EA passes through
    (DASS: #CDCDDOW LH R2,0(R1) -> 038214)."""
    if ea is None:
        return None
    if ea > 0xFFFF:
        return ea
    ea &= 0xFFFF
    if branch and ea & 0x8000:
        return ((addr >> 15) << 15) | (ea & 0x7FFF)
    return ea


class Ap101Disassembler:
    """Stateless decoder; base-register context is passed per call."""

    def __init__(self):
        self.entries = []
        for name, d in CPU_DEFS.items():
            desc = d["d"]
            if name == "SPM":
                # flight MAFGEN decodes the whole C8-CB row as SPM
                # (DASS FCMMGBOV: 'CBEA' -> 'SPM R2'); the assembler's
                # encoding fixes the x bits, the listing's does not
                desc = "11001xxx11101yyy"
            self.entries.append(_Entry(name, Descriptor(name, desc), d))
        # most-constrained first so e.g. AR beats the A SRS pattern and
        # AST beats both
        self.entries.sort(key=lambda e: (-e.fixed, e.name))

    def _match(self, hw1):
        for e in self.entries:
            if (hw1 & e.first_mask) == e.first_val:
                return e
        return None

    def decode(self, addr, hw1, hw2, base_regs=None, conventions=True):
        """Decode one instruction at halfword address ``addr``.

        base_regs: optional {reg_number: value_or_None} giving statically
        known base-register contents (MAFGEN convention: R3=0/FCM, R1=the
        owning block's #D data csect).  conventions=False turns the
        R0/R3-hold-0 HAL convention OFF (MAFGEN leaves based references
        unresolved in NONHAL csects).  Returns None if hw1 matches no
        instruction.
        """
        e = self._match(hw1)
        if e is None:
            return None
        f = {ch: (hw1 >> shift) & (fmask >> shift)
             for ch, (shift, fmask, _b) in e.desc.fields.items()}
        name = _DISPLAY.get(e.name, e.name)
        aw = _AW_SHIFT.get(e.meta.get("addrWidth"), 0)
        bases = base_regs or {}
        nhw = 1
        imm = None
        ea = None
        suffix = ""
        x = f.get("x")
        y = f.get("y")
        b = f.get("b")
        d = f.get("d")

        if e.desc.immediate:                       # RI / SI
            nhw = 2
            imm = hw2
            if d is not None:                      # SI: D2(B2),Data
                ea = _expand(self._srs_ea(d, b, aw, bases, conventions), addr,
                             name in BRANCH_EA)
                ops = f"{_num(d << aw)}({self._rn(b)}),{_num(imm)}"
            else:                                  # RI: Rn,Data
                reg = y if y is not None else x
                ops = f"{_regpfx(name)[0]}{reg},{_num(imm)}"
        elif e.desc.flags == "X":                  # RS long form
            nhw = 2
            a = f.get("a", 0)
            ops, ea, suffix = self._long_form(name, addr, a, x, b, hw2,
                                              bases, conventions)
        elif name in SHIFT_MNEMONICS:
            if d is not None and d >= 0x38:
                # register shift: count in (R d&7); DASS prints 'R4,0(R5)'
                ops = f"R{x},0(R{d & 7})"
            else:
                ops = f"R{x},{d}"
        elif name in BRANCH_FWD or name in BRANCH_BACK:
            ea = addr + nhw + (d if name in BRANCH_FWD else -d)
            reg = f"R{x}" if name == "BCTB" else str(x)
            ops = f"{reg},{_num(d)}"
        elif d is not None and b is not None:      # SRS
            if d == 0x3C or (name == "IAL" and d == 0x3E):
                nhw = 2
                ops, ea, suffix = self._long_form(name, addr, 0, x, b, hw2,
                                                  bases, conventions)
            elif d == 0x3D or (name == "IAL" and d == 0x3F):
                nhw = 2
                ops, ea, suffix = self._long_form(name, addr, 1, x, b, hw2,
                                                  bases, conventions)
            else:
                ea = _expand(self._srs_ea(d, b, aw, bases, conventions), addr,
                             name in BRANCH_EA)
                dd = _num(d << aw)
                ops = (f"{_regpfx(name)[0]}{x},{dd}({self._rn(b)})"
                       if x is not None else f"{dd}({self._rn(b)})")
        elif y is not None:                        # RR
            p1, p2 = _regpfx(name)
            if name == "SPM":
                # one-operand form; the x bits are don't-care (DASS:
                # 'CBEA' -> 'SPM R2')
                ops = f"R{y}"
            elif x is not None:
                ops = f"{p1}{x},{p2}{y}"
            else:
                ops = f"{p2}{y}"
            if name in ("BCR", "BCFR", "BCBR", "BCRE", "BVCR", "SRET"):
                # M1-first RR forms: the mask/value prints bare
                # (DASS: 'SRET 7,R0', never R7)
                ops = f"{x},R{y}"
            elif name == "LFXI":
                # the immediate is Y2-2, printed decimal (DASS: BCE3 ->
                # 'LFXI R4,1'; matches gpc decodef 'd.y = d.y - 2')
                ops = f"R{x},{y - 2}"
            elif name == "LFLI":
                # Y2 is a raw immediate, no -2 bias (DASS: 88E0 ->
                # 'LFLI F0,0'; gpc builds 0x41000000 | y<<20)
                ops = f"F{x},{y}"
        else:
            ops = f"R{x}" if x is not None else ""

        if name == "CIST":          # MAFGEN's listing name for B5xx
            name = "CIS"
        if name == "AHI" and imm is not None and imm & 0x8000:
            # MAFGEN prints add-of-negative as a subtract (DASS: B0E1 FFFF
            #
            # > 'SHI R1,1', HAL and NONHAL csects alike)
            #
            name = "SHI"
            imm = 0x10000 - imm
            reg = y if y is not None else x
            ops = f"R{reg},{_num(imm)}"
        if name == "IAL" and not (d == 0x3F and b == 3):
            # stack-frame arithmetic, not an address -- except the
            # indexed escape's architectural no-base form (B2=3), which
            # resolves (DASS FCMG3STR: 'IAL R3,0(R4,R3)' EA 000000 ->
            # TPSASTRT,TPSARES1); extended-form b=3 is real R3
            ea = None
        # base R0 = the HAL/S stack-frame register: the numeric EA still
        # prints (frame assumed at 0) but it is not a PSA reference
        stack_ref = (b == 0 and d is not None
                     and not (name in BRANCH_FWD or name in BRANCH_BACK)
                     and name not in SHIFT_MNEMONICS)
        # recover the indexed-long-form fields for consumers (SCAL/ZCON
        # resolution); extended forms leave idx_info None
        idx_info = None
        if nhw == 2 and not e.desc.immediate and (
                (e.desc.flags == "X" and f.get("a", 0))
                or (d is not None and b is not None
                    and (d == 0x3D or (name == "IAL" and d == 0x3F)))):
            idx_info = (hw2 >> 13, (hw2 >> 12) & 1, (hw2 >> 11) & 1,
                        hw2 & 0x7FF)
        return Inst(name=name, nhw=nhw, mnemonic=name + suffix,
                    operands=ops, ea=ea, imm=imm, fields=f,
                    stack_ref=stack_ref, aw=aw, idx_info=idx_info)

    #
    # helpers
    #
    @staticmethod
    def _rn(b):
        return f"R{b}"

    @staticmethod
    def _srs_ea(d, b, aw, bases, conv=True):
        """SRS short form: 16-bit base register value + scaled displacement
        (the caller expands to 19 bits).  A tracked value in ``bases``
        wins; otherwise R0 and R3 hold 0 by MAFGEN's flight convention
        (HAL csects only -- conv=False leaves untracked bases unresolved)
        and other bases stay unresolved.  Base values above the 15-bit
        direct range are given in their 16-bit register form (high bit =
        sector-register select)."""
        base = bases.get(b)
        if base is None and conv:
            base = 0 if b in (0, 3) else None
        if base is None:
            return None
        if base > 0xFFFF:                  # full 19-bit tracked address
            return base + (d << aw)
        if base > 0x7FFF:                  # 19-bit address -> register form
            base = (base & 0x7FFF) | 0x8000
        return (base + (d << aw)) & 0xFFFF

    def _long_form(self, name, addr, a, x, b, hw2, bases, conv=True):
        """Extended (a=0) or indexed (a=1) long form; returns
        (operand_text, ea_or_None, mnemonic_suffix)."""
        if name in ("BC", "BVC", "ISPB"):          # M1-first: the mask
            r1 = f"{x},"                           # prints bare (DASS:
            # 'ISPB 0,X'001F'', never R0)
        else:
            r1 = f"{_regpfx(name)[0]}{x}," if x is not None else ""
        if a == 0:
            disp = hw2
            if b == 3:                             # architecturally no base
                return (f"{r1}{_num(disp)}",
                        _expand(disp, addr, name in BRANCH_EA), "")
            base = (0 if b == 0 and conv else bases.get(b))
            if base is not None and 0x7FFF < base <= 0xFFFF:
                base = (base & 0x7FFF) | 0x8000
            ea = None if base is None else _expand(base + disp, addr,
                                                   name in BRANCH_EA)
            return f"{r1}{_num(disp)}(R{b})", ea, ""
        idx = hw2 >> 13
        ia = (hw2 >> 12) & 1
        ii = (hw2 >> 11) & 1
        disp = hw2 & 0x7FF
        suffix = ("@" if ia else "") + ("#" if ii else "")
        if b == 3 and idx == 0 and not ia \
                and (name in BRANCH_EA or not conv):
            # indexed form, B2=3, no index: IC-relative, I selects the
            # direction (g_EA steps 3-4: EA = updated IC +/- D2) --
            # architecturally for branches, and for data references
            # too in NONHAL csects (DASS: 'L R6,X'03AE'' at 0185C2
            # resolves to 018972; 'BC 3,X'006B'' backward at 041E13 to
            # 041DAA); MAFGEN prints the raw displacement, no suffix
            ea = addr + 2 - disp if ii else addr + 2 + disp
            return f"{r1}{_num(disp)}", ea, ""
        ea = None
        if name not in BRANCH_EA and not ia:
            # data-indexed: MAFGEN's EA column shows the PEA (base + D2,
            # index unknown).  B2=11 is the architectural no-base
            # encoding -- it resolves in asm csects too, conventions
            # notwithstanding (DASS $0DCICYC: LH R4,0(R7,R3) -> 000000
            # TPSASTRT); only real base registers need tracking
            base = 0 if b == 3 else bases.get(b)
            if base is not None:
                if base > 0xFFFF:            # full 19-bit tracked address
                    ea = base + disp
                else:
                    if base > 0x7FFF:
                        base = (base & 0x7FFF) | 0x8000
                    ea = (base + disp) & 0xFFFF
        # a zero index register is omitted from the operand ('(R2)', not
        # '(R0,R2)'); an absent base (B2=3) prints when an index is
        # present (DASS: SCAL@# R0,X'01D4'(R1,R3)) and is omitted only
        # in the no-index forms
        if idx:
            paren = f"(R{idx},R{b})"
        elif b != 3:
            paren = f"(R{b})"
        else:
            paren = ""
        ops = f"{r1}{_num(disp)}{paren}"
        return ops, ea, suffix
