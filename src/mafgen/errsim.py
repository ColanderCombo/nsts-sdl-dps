"""Flight MAFGEN register-simulation diagnostics (*** ERROR #n ... ***).

Format facts:

  * One global machine per listing: error numbers are sequential #1..
    across all dump csects (both severities share the counter); the
    register file resets at every csect boundary.
  * Severity 1 BAD EFFECTIVE ADDRESS fires when a rendered code row
    prints an EA that fails the validity predicate:
      - data refs: the EA must fall inside some listed csect (the boot
        phase's memory reads as holes, like the listing scope itself);
      - branch targets (B/BC/BCT/BIX/BVC/BAL/SCAL): the csect must also
        be code-class (PROGRAM COMSUB INTERNAL LIBCODE NONHAL PATCH)
        and the halfword actually loaded -- C6C6 MMU staging inside an
        allocated patch area is not code (DASS: SCAL@# into #T023001).
  * '*** LAST BAD EFFECTIVE ADDRESS WAS AT ADDR x ***' cites the
    previous sev-1 fault at start+nhw-2: the flight simulator's IC runs
    one fullword ahead, so 2-hw instructions cite their own start and
    1-hw ones start-1.  Omitted on the first sev-1 fault.
  * Severity 0 ILLEGAL OP CODE: a code-region halfword that decodes to
    no instruction (the '?' mnemonic rows).  No FIX REGS, no stack
    line; its own chain line '*** LAST ILLEGAL OP CODE ERROR WAS AT
    ADDR x ***' cites the previous sev-0 fault's start address.
  * '   *** FIX REGS R0 xxxxxxxx ...' dumps the simulator's 8x32-bit
    register images at the fault; the flag column is '?' when the value
    is unknown or stack-dependent, blank otherwise.  Halfword ops write
    register bits 0-15 (the upper half) leaving the lower alone; IAL and
    IHL write the lower half; shifts are full 32-bit; unknown results
    display as zero (no compute-through); loads read the composed image.
    Values the IPL wrote after the load module was built cannot match --
    flight read post-IPL dump memory, which we do not have.

    HAL csects seed R0 = known-0 stack frame and R1 = the unit's #D
    base; BAL kills its link register; BAL/SCAL re-assert R1 = #D and
    R3 = the block's LBD (the first LA-R3 value) and preserve every
    other register; SVC changes nothing; PC (program call) and an
    indirect unconditional branch (BC@# 7) kill the whole file.  16-bit
    data EAs are absolute (no instruction-sector expansion); loads from
    un-mapped EAs yield unknown.

    Those semantics are HAL-only.  The NONHAL register file is the same
    LXA-only machine the EA renderer models: LXA/LXAR are its sole
    writers (the loaded pointer's address part), every other write is
    ignored, and the file starts all-unknown (DASS FCMPMOD #193: LXA
    R1,FCMPLDSE -> R1 00000000 known-blank while the XR R5,R5 / LA R0 /
    L R4 before the fault all left their targets 00000000?; FCMLINIT
    has no LXA and all four of its FIX lines are all-unknown despite
    LHI/LA/IAL runs in between).
  * ' BASE OR INDEX REGISTER CONTAINS STACK DEPENDENT VALUE' prints
    between the LAST BAD and FIX lines when the failing EA was computed
    through a register whose value came out of stack memory (an R0
    stack-frame load or a fetch inside a STACK csect).
"""
import bisect

# branch-class mnemonics: their (printed) EA must land on loaded code
_BRANCH = frozenset({"B", "BAL", "BC", "BCT", "BIX", "BVC", "SCAL"})
_CODE_CATS = frozenset({"PROGRAM", "COMSUB", "INTERNAL", "LIBCODE",
                        "NONHAL", "PATCH"})
# instructions that never write a fixed-point register (stores, memory
# bit ops, compares, branches sans side effect, system ops)
_NO_WRITE = frozenset({
    "ST", "STH", "STE", "STED", "STM", "TS", "TB", "SB", "ZH", "ZB",
    "SSM", "SHW", "OST", "NST", "ISPB", "IST", "AST", "SST",
    "C", "CH", "CB", "CR", "CHI", "CI", "CIS", "CIST",
    "SVC", "SRET", "LPS", "SPM", "ICR",
    "B", "BC", "BCF", "BCB", "BCR", "BCFR", "BCBR", "BCRE", "BVC",
    "BVCR", "NOP", "MVH", "MVS", "XCH"})
# float-register instructions leave the fixed file alone (CVFX/LFXR,
# the float->fixed movers, are handled explicitly)
_FLOAT = frozenset({
    "AE", "AED", "AEDR", "AER", "CE", "CED", "CEDR", "CER", "DE", "DED",
    "DEDR", "DER", "LE", "LECR", "LED", "LER", "LFLI", "LFLR", "ME",
    "MED", "MEDR", "MER", "SE", "SED", "SEDR", "SER", "STE", "STED",
    "CVFL"})


class ErrorSim:
    def __init__(self, img, csects):
        self.words = img.words
        # CHECKSUM pseudo-csects are tape furniture, not map entries --
        # flight faults EAs that land on them (DASS: STH X'AC8A')
        ivals = sorted((c.start, c.start + c.size - 1, c.cat)
                       for c in csects if c.cat != "CHECKSUM")
        self._ivals = ivals
        self._starts = [s for s, _e, _c in ivals]
        self._stack_ivals = [(s, e) for s, e, cat in ivals
                             if cat == "STACK"]
        self._stack_starts = [s for s, _e in self._stack_ivals]
        self.err_no = 0
        self.last_bad = None            # previous sev-1 site, IC-2 form
        self.last_ill = None            # previous sev-0 site, start addr
        # register file: [value32, known, stack-dependent] x 8
        self.regs = [[0, False, False] for _ in range(8)]
        self._hal = False
        self._dbase = None              # HAL: the unit's #D base (R1)
        self._lbd = None                # HAL: first LA-R3 value (LBD)

    def begin_csect(self, dbase=None, hal=False):
        """Flight resets its register file at every csect boundary (DASS
        B0DMPMMM: R7 unknown at +000B, R0 re-zeroed for the block's own
        IAL frame setup).  HAL csects seed R0 = the known-0 stack frame
        and R1 = the unit's #D base."""
        self.regs = [[0, False, False] for _ in range(8)]
        self._hal = hal
        self._dbase = dbase
        self._lbd = None
        if hal:
            self.regs[0] = [0, True, False]
            if dbase is not None:
                self.regs[1] = [(dbase & 0xFFFF) << 16, True, False]

    #
    # lookup
    #
    def _cat_at(self, hw):
        i = bisect.bisect_right(self._starts, hw) - 1
        best = None
        while i >= 0 and hw - self._ivals[i][0] < 0x20000:
            s, e, cat = self._ivals[i]
            if s <= hw <= e and (best is None or e - s < best[1] - best[0]):
                best = (s, e, cat)
            i -= 1
        return best[2] if best else None

    def _in_stack(self, hw):
        i = bisect.bisect_right(self._stack_starts, hw) - 1
        return i >= 0 and self._stack_ivals[i][0] <= hw \
            <= self._stack_ivals[i][1]

    def _bad_ea(self, name, ea):
        cat = self._cat_at(ea)
        if cat is None:
            return True
        if name in _BRANCH:
            return cat not in _CODE_CATS or self.words.get(ea) == 0xC6C6
        return False

    #
    # diagnostics
    #
    def illegal(self, addr):
        """A code-region halfword that decodes to nothing ('?' row)."""
        self.err_no += 1
        out = [f" *** ERROR #{self.err_no} SEVERITY 0 - "
               "ILLEGAL OP CODE ***"]
        if self.last_ill is not None:
            out.append(" *** LAST ILLEGAL OP CODE ERROR WAS AT ADDR "
                       f"{self.last_ill:06X} ***")
        self.last_ill = addr
        return out

    def _fix_line(self):
        parts = []
        for i, (v, known, dep) in enumerate(self.regs):
            flag = "?" if (not known or dep) else " "
            parts.append(f"R{i} {v & 0xFFFFFFFF:08X}{flag}")
        return ("   *** FIX REGS " + "  ".join(parts)).rstrip()

    def step(self, addr, inst, hw1, hw2):
        """Check the rendered instruction's printed EA, then execute its
        register semantics.  Returns the diagnostic lines to append."""
        out = []
        ea = inst.ea
        # tracker-garbage EAs (negative / past the 19-bit space) are
        # rendering artifacts, never simulator faults
        if ea is not None and 0 <= ea < 0x80000 \
                and self._bad_ea(inst.name, ea):
            self.err_no += 1
            out.append(f" *** ERROR #{self.err_no} SEVERITY 1 - "
                       "BAD EFFECTIVE ADDRESS ***")
            if self.last_bad is not None:
                out.append(" *** LAST BAD EFFECTIVE ADDRESS WAS AT ADDR "
                           f"{self.last_bad:06X} ***")
            self.last_bad = addr + inst.nhw - 2
            f = inst.fields
            b = f.get("b")
            if b == 3 and (inst.idx_info is not None
                           or (inst.nhw == 2 and inst.imm is None
                               and f.get("d") is None)):
                b = None               # fullword-form b=3 means NO base
            dep = any(self.regs[r][2]
                      for r in (b, (inst.idx_info or (0,))[0])
                      if r)
            if dep:
                out.append(" BASE OR INDEX REGISTER CONTAINS STACK "
                           "DEPENDENT VALUE")
            out.append(self._fix_line())
        self._exec(addr, inst, hw1, hw2)
        return out

    #
    # simulation
    #
    def _hi(self, r):
        return (self.regs[r][0] >> 16) & 0xFFFF

    def _mem16(self, addr, a):
        """Halfword fetch: 16-bit data EAs are absolute (sectors 0-1) --
        no instruction-sector expansion (DASS: LH X'81A8' from sector-3
        FCMMGBOV reads FCMCBLKS+008E at absolute 0081A8).  Frame cells
        read the image too -- MAFGEN did not forward its own stores
        (DASS A1DXRDMM #16..#19: the FIX R7/R6/R4 values are dump[12]=1
        chains, not the STH-stored CDMV chain values)."""
        if a is None:
            return None
        return self.words.get(a)

    def _set_hi(self, x, v, known, dep):
        if not known:
            self.regs[x][:] = [0, False, dep]
            return
        r = self.regs[x]
        r[0] = ((v & 0xFFFF) << 16) | (r[0] & 0xFFFF)
        r[1], r[2] = known, dep

    def _set_lo(self, x, v, known, dep):
        if not known:
            self.regs[x][:] = [0, False, dep]
            return
        r = self.regs[x]
        r[0] = (r[0] & 0xFFFF0000) | (v & 0xFFFF)
        r[1], r[2] = known, dep

    def _set32(self, x, v, known, dep):
        if not known:
            v = 0
        self.regs[x][:] = [v & 0xFFFFFFFF, known, dep]

    def _unknown(self, x):
        # unknown registers display as 00000000?; only stack-dependent
        # values keep their computed image (DASS: R7 00060000?)
        self.regs[x][:] = [0, False, False]

    def _sim_ea(self, addr, inst, hw2):
        """(ea, known, dep) as the flight simulator resolves it: real
        register values everywhere, no R0/R3 rendering conventions.
        dep marks stack-frame (R0-based) and stack-csect fetch paths."""
        f = inst.fields
        b, d = f.get("b"), f.get("d")
        if inst.idx_info is not None:
            ix, ia, _ii, disp = inst.idx_info
            base, bk, bdep = (0, True, False) if b == 3 else (
                self._hi(b), self.regs[b][1], self.regs[b][2])
            if not bk:
                return None, False, False
            if ia:
                pa = (base + disp) & 0xFFFF
                ptr = self._mem16(addr, pa)
                if ptr is None:
                    return None, False, bdep
                dep = bdep or self._in_stack(pa)
                ea = ptr
            else:
                ea, dep = (base + disp) & 0xFFFF, bdep
            if ix:
                if not self.regs[ix][1]:
                    return None, False, dep
                ea = (ea + self._hi(ix)) & 0xFFFF
                dep = dep or self.regs[ix][2]
            return ea, True, dep or (b == 0)
        if inst.nhw == 2 and inst.imm is None and d is None:
            # extended long form: 16-bit displacement in hw2
            if b == 3:
                return hw2, True, False
            if not self.regs[b][1]:
                return None, False, False
            return (self._hi(b) + hw2) & 0xFFFF, True, \
                self.regs[b][2] or b == 0
        if d is None or b is None:
            return None, False, False
        # SRS short form (the simulator uses real register values; the
        # printed column's R0/R3-hold-0 convention is rendering only)
        if not self.regs[b][1]:
            return None, False, False
        return (self._hi(b) + (d << inst.aw)) & 0xFFFF, True, \
            self.regs[b][2] or b == 0

    def _exec(self, addr, inst, hw1, hw2):
        if not self._hal:
            self._exec_lxa(inst)
            return
        name = inst.name
        f = inst.fields
        x, y = f.get("x"), f.get("y")
        if name == "BC" and f.get("x") == 7 and inst.idx_info \
                and inst.idx_info[1]:
            for i in range(8):
                self.regs[i][:] = [0, False, False]
            return
        if name == "PC":
            # program call: control leaves the unit, every register
            # image is dead on return (DASS FCMLINIT: all-unknown FIX
            # at #1 right after the LHI/PC dispatch loop)
            for i in range(8):
                self._unknown(i)
            return
        if name in _FLOAT or name in _NO_WRITE:
            return
        if name == "LHI":
            if x == 1 and self._dbase is not None \
                    and inst.imm != (self._dbase & 0xFFFF):
                # un-relocated #D prologue adcon (reloc-gap image
                # class): flight executed the relocated 'LA R1,#D',
                # so its R1 image stays the #D base (DASS #CDXRDMM
                # #14 FIX: R1 4CE40000)
                return
            self._set_hi(x, inst.imm, True, False)
            return
        if name == "LFXI":
            self._set_hi(x, (y - 2) & 0xFFFF, True, False)
            return
        if name == "LA":
            if x == 3 and self._hal and self._lbd is None \
                    and inst.ea is not None:
                self._lbd = inst.ea & 0xFFFF
            if inst.ea is not None:
                self._set_hi(x, inst.ea, True, False)
            else:
                ea, known, dep = self._sim_ea(addr, inst, hw2)
                if known:
                    self._set_hi(x, ea, True, dep)
                else:
                    self._unknown(x)
            return
        if name == "LR":
            self.regs[x][:] = list(self.regs[y])
            return
        if name in ("BAL", "SCAL"):
            # the BAL link register comes back dead (DASS $0DMPMMM+0044:
            # BAL@# R4 -> the next FIX shows R4 00000000?); both calls
            # re-assert the HAL base conventions (R1 = #D, R3 = LBD)
            # and preserve every other register (DASS #26 -> #27)
            if name == "BAL":
                self._unknown(x)
            if self._hal:
                if self._dbase is not None:
                    self.regs[1] = [(self._dbase & 0xFFFF) << 16,
                                    True, False]
                if self._lbd is not None:
                    self.regs[3] = [(self._lbd & 0xFFFF) << 16,
                                    True, False]
            return
        if name in ("BCT", "BCTB"):
            r = self.regs[x]
            self._set_hi(x, self._hi(x) - 1, r[1], r[2])
            return
        if name == "BIX":
            r = self.regs[x]
            self._set_hi(x, self._hi(x) + 1, r[1], r[2])
            return
        if name == "XUL":
            rx, ry = self.regs[x], self.regs[y]
            known = rx[1] and ry[1]
            dep = rx[2] or ry[2]
            xv = ((rx[0] >> 16) ^ ry[0]) & 0xFFFF
            if x == y:
                self._set32(x, (xv << 16) | xv, known, dep)
            else:
                self._set32(x, (xv << 16) | (rx[0] & 0xFFFF), known, dep)
                self._set32(y, (ry[0] & 0xFFFF0000) | xv, known, dep)
            return
        if name in ("SLL", "SRL", "SRA", "SLA", "SLDL", "SRDL",
                    "SLDA", "SRDA"):
            d = f.get("d", 0)
            if d >= 0x38:               # count from a register
                cr = d & 7
                if not self.regs[cr][1]:
                    self._unknown(x)
                    if name in ("SLDL", "SRDL", "SLDA", "SRDA"):
                        self._unknown(x ^ 1)
                    return
                cnt = self.regs[cr][0] & 0x3F
            else:
                cnt = d
            r = self.regs[x]
            if name in ("SLDL", "SRDL", "SLDA", "SRDA"):
                r2 = self.regs[x ^ 1]
                known, dep = r[1] and r2[1], r[2] or r2[2]
                v = ((r[0] & 0xFFFFFFFF) << 32) | (r2[0] & 0xFFFFFFFF)
                if name == "SLDL" or name == "SLDA":
                    v = (v << cnt) & ((1 << 64) - 1)
                elif name == "SRDA" and v & (1 << 63):
                    v = ((v - (1 << 64)) >> cnt) & ((1 << 64) - 1)
                else:
                    v >>= cnt
                self._set32(x, v >> 32, known, dep)
                self._set32(x ^ 1, v, known, dep)
                return
            v = r[0] & 0xFFFFFFFF
            if name == "SLL" or name == "SLA":
                v = (v << cnt) & 0xFFFFFFFF
            elif name == "SRL":
                v >>= cnt
            else:                       # SRA: arithmetic
                sv = v - (1 << 32) if v & 0x80000000 else v
                v = (sv >> cnt) & 0xFFFFFFFF
            self._set32(x, v, r[1], r[2])
            return
        if name in ("AR", "SR", "NR", "OR", "XR"):
            rx, ry = self.regs[x], self.regs[y]
            known, dep = rx[1] and ry[1], rx[2] or ry[2]
            a, bv = rx[0] & 0xFFFFFFFF, ry[0] & 0xFFFFFFFF
            v = {"AR": a + bv, "SR": a - bv, "NR": a & bv,
                 "OR": a | bv, "XR": a ^ bv}[name]
            self._set32(x, v, known, dep)
            return
        if name in ("AHI", "SHI", "NHI", "OHI", "XHI"):
            r = self.regs[x if x is not None else y]
            reg = x if x is not None else y
            h = self._hi(reg)
            imm = inst.imm or 0
            v = {"AHI": h + imm, "SHI": h - imm, "NHI": h & imm,
                 "OHI": h | imm, "XHI": h ^ imm}[name]
            self._set_hi(reg, v, r[1], r[2])
            return
        if name == "IAL":
            imm = hw2 if inst.nhw == 2 else (f.get("d", 0) << inst.aw)
            r = self.regs[x]
            self._set_lo(x, (r[0] & 0xFFFF) + imm, r[1], r[2])
            return
        if name in ("D", "DR", "TDR"):
            self._unknown(x)
            self._unknown(x ^ 1)
            return
        #
        # memory-sourced ops
        #
        ea, known, dep = self._sim_ea(addr, inst, hw2)
        if known and ea is not None and self._in_stack(ea):
            dep = True
        if name in ("LH", "L", "IHL", "AH", "SH", "A", "S", "N", "O",
                    "X", "MIH"):
            # a load from un-mapped memory yields nothing (DASS FCMG3STR:
            # 'L R0,X'FFFE'' faults and the next FIX shows R0 00000000?)
            if not known or ea is None or not (0 <= ea < 0x80000) \
                    or self._bad_ea(name, ea):
                self._unknown(x)
                return
            m = self._mem16(addr, ea)
            if m is None:
                self._unknown(x)
                return
            if name == "LH":
                self._set_hi(x, m, True, dep)
            elif name == "IHL":
                self._set_lo(x, m, True, dep)
            elif name in ("AH", "SH"):
                r = self.regs[x]
                h = self._hi(x)
                v = h + m if name == "AH" else h - m
                self._set_hi(x, v, r[1], r[2] or dep)
            elif name == "MIH":
                h = self._hi(x)
                h = h - 0x10000 if h & 0x8000 else h
                mm = m - 0x10000 if m & 0x8000 else m
                r = self.regs[x]
                self._set32(x, ((h * mm) & 0xFFFF) << 16,
                            r[1], r[2] or dep)
            else:
                m2 = self._mem16(addr, ea + 1)
                if m2 is None:
                    self._unknown(x)
                    return
                mv = (m << 16) | m2
                r = self.regs[x]
                if name == "L":
                    self._set32(x, mv, True, dep)
                else:
                    a2 = r[0] & 0xFFFFFFFF
                    v = {"A": a2 + mv, "S": a2 - mv, "N": a2 & mv,
                         "O": a2 | mv, "X": a2 ^ mv}[name]
                    self._set32(x, v, r[1], r[2] or dep)
            return
        # anything else that names a target register: value unknowable
        if x is not None:
            self._unknown(x)

    def _exec_lxa(self, inst):
        """NONHAL register file: LXA/LXAR are the SOLE writers (the
        loaded pointer's address part, as a base-register image),
        every other write is ignored and the value persists."""
        name = inst.name
        if name == "LXA":
            x = inst.fields.get("x")
            if x is None:
                return
            v = self.words.get(inst.ea) if inst.ea is not None else None
            if v is None:
                self.regs[x][:] = [0, False, False]
            else:
                self.regs[x][:] = [(v & 0x7FFF) << 16, True, False]
        elif name == "LXAR":
            x, y = inst.fields.get("x"), inst.fields.get("y")
            if x is not None and y is not None:
                self.regs[x][:] = list(self.regs[y])
