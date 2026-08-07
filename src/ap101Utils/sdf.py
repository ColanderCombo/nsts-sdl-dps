#!/usr/bin/env python3
#
# Query interface over HAL/S-FC PASS3 Simulation Data Files (SDF) — the
# compiler's per-compilation-unit symbol database (ICD USA001556).
#
# An SDF library is a directory of `##<member>.sdf` files as written by
# HALSFC-PASS3 (the XCOM-I runtime's --sdfo directory; `halsc --sdf=DIR`).
# Access goes through the vendored SDFPKG port (ap101Utils/sdfpkg.py) —
# the same machinery HALSTAT and the compiler's MONITOR(22) use — with its
# raw page-stream and EBCDIC name support.
#
# Typical use:
#
#     from ap101Utils.sdf import SdfLibrary
#
#     lib = SdfLibrary("build/OI340600/gen/SDFLIB")
#     sym = lib.lookup("CGC_COMMON", "CGCB_BYPASS")   # -> SdfSymbol
#     sym.kind, sym.width, sym.hal_type()             # 'BIT', 3, 'BIT(3)'
#
#     unit = lib.unit("CVN_MM_UTILITY")               # -> SdfUnit
#     unit.symbol("CDHV_RW_BUFR").template            # 'CDHV_RW_BUFR'
#     unit.structure("CDHV_RW_BUFR")                  # [SdfSymbol leaves]
#     unit.replaces()                                 # {alias: body text}
#     unit.char_initials()                            # {char var: INITIAL}
#
# Or from the command line:
#
#     python3 -m ap101Utils.sdf LIBDIR COMPOOL [SYMBOL]
#
import codecs
import os
import struct
import sys
from dataclasses import dataclass, field
from typing import Optional

# sdfpkg is vendored alongside this module (src/ap101Utils/sdfpkg.py).  It used
# to be read out of the virtualagc submodule, but upstream deleted that port in
# 85a46336b and its replacements are not file-based readers, so the copy lives
# here now; see the provenance note at the top of sdfpkg.py.  SDFPKG_DIR still
# overrides, for anyone pointing at a different checkout of the port.
_SDFPKG_DIR = os.environ.get("SDFPKG_DIR") or os.environ.get("DFG_SDFPKG")
if _SDFPKG_DIR and _SDFPKG_DIR not in sys.path:
    sys.path.insert(0, _SDFPKG_DIR)
    import sdfpkg  # noqa: E402
else:
    from . import sdfpkg  # noqa: E402

# True SDFs (unlike sdfpkg's synthetic flat files) keep names in EBCDIC.
sdfpkg.set_name_encoding("cp037")

_EBCDIC = "cp037"

# Symbol classes as stored in the SDF Symbol Data Cell (ICD p.64).
CLASS_VARIABLE = 1
CLASS_LABEL = 2
CLASS_FUNCTION = 3
CLASS_TEMPLATE = 4       # structure-template members (and headers)

# Symbol type codes (SDC field 7) for CLASS_VARIABLE/CLASS_TEMPLATE.
_VARIABLE_KINDS = {
    1:  ("BIT", "SINGLE"),
    2:  ("CHARACTER", None),
    3:  ("MATRIX", "SINGLE"),
    4:  ("VECTOR", "SINGLE"),
    5:  ("SCALAR", "SINGLE"),
    6:  ("INTEGER", "SINGLE"),
    9:  ("BIT", "DOUBLE"),      # 32-bit BIT
    11: ("MATRIX", "DOUBLE"),
    12: ("VECTOR", "DOUBLE"),
    13: ("SCALAR", "DOUBLE"),
    14: ("INTEGER", "DOUBLE"),
    16: ("STRUCTURE", None),
    17: ("EVENT", None),
}
# Type codes for CLASS_LABEL.
_LABEL_KINDS = {
    1: "PROGRAM", 2: "PROCEDURE", 3: "FUNCTION", 4: "COMPOOL", 5: "TASK",
    6: "UPDATE", 7: "STATEMENT", 8: "EQUATE", 9: "REPLACE",
}
TYPE_STRUCTURE = 16
TYPE_COMPOOL_LABEL = 4
TYPE_REPLACE_LABEL = 9

# SDC flag bytes (flag1..flag4 = ICD field 8, MSB first)
_F1_COMPOOL = 0x80
_F1_INPUT_PARM = 0x40      # INCSDF SDF_INPUT_PARM_FLAG "40000000"
_F1_ASSIGN_PARM = 0x20     # INCSDF SDF_ASSIGN_PARM_FLAG "20000000"
_F1_TEMPORARY = 0x10       # INCSDF SDF_TEMPORARY_FLAG "10000000"
_F1_AUTO = 0x08            # INCSDF SDF_AUTO_FLAG "08000000"
_F1_NAME = 0x04
_F1_TEMPLATE = 0x02
_F2_DENSE = 0x40
_F2_CONSTANT = 0x20
_F2_REMOTE = 0x01
_F3_INITIAL = 0x40
_F3_RIGID = 0x20


def _be16(mv, off):
    return (mv[off] << 8) | mv[off + 1]


def _be32(mv, off):
    return (mv[off] << 24) | (mv[off + 1] << 16) | (mv[off + 2] << 8) | mv[off + 3]


@dataclass
class SdfSymbol:
    """One symbol of a compilation unit, decoded from its Symbol Data Cell.

    `kind` is the friendly type: BIT/CHARACTER/INTEGER/SCALAR/VECTOR/MATRIX/
    EVENT/STRUCTURE for data symbols, or COMPOOL/REPLACE/EQUATE/... for
    labels.  HAL/S BOOLEAN is BIT with width 1 (the SDF has no separate
    BOOLEAN code)."""
    name: str
    unit: str                      # owning compilation unit
    number: int                    # 1-based symbol index table number
    sclass: int                    # raw SDC symbol class
    stype: int                     # raw SDC symbol type
    kind: Optional[str] = None
    precision: Optional[str] = None    # SINGLE/DOUBLE (None if untyped)
    width: Optional[int] = None        # BIT(n)/CHARACTER(n) n
    dims: tuple = ()                   # declared ARRAY/copiness dims
    address: Optional[int] = None      # halfword offset within the csect
    template: Optional[str] = None     # structure template name (instances)
    is_template: bool = False          # a structure-template header
    compool: bool = False              # declared in (included from) a compool
    name_var: bool = False             # a NAME (pointer) variable
    parm: bool = False                 # a formal parameter (INPUT/ASSIGN)
    auto: bool = False                 # AUTOMATIC (stack-frame) storage
    temporary: bool = False            # TEMPORARY (stack-frame) storage
    constant: bool = False
    initial: bool = False
    remote: bool = False
    rigid: bool = False
    dense: bool = False
    xrefs: tuple = ()              # citing statement numbers (XREFDATA)
    # internal link fields (symbol index numbers; 0 = none)
    _eldest: int = field(default=0, repr=False)
    _brother: int = field(default=0, repr=False)
    _replace_ptr: int = field(default=0, repr=False)

    @property
    def copies(self):
        """Total elements from the declared dims (1 = not arrayed)."""
        n = 1
        for d in self.dims:
            if d:
                n *= d
        return n

    def hal_type(self):
        """The HAL/S type spec string, e.g. 'BIT(3)', 'INTEGER DOUBLE',
        'QUATERNION-STRUCTURE'."""
        if self.kind == "STRUCTURE" and self.template:
            return "%s-STRUCTURE" % self.template
        if self.kind in ("BIT", "CHARACTER"):
            return "%s(%d)" % (self.kind, self.width or 1)
        if self.kind in ("INTEGER", "SCALAR", "VECTOR", "MATRIX") \
                and self.precision == "DOUBLE":
            return "%s DOUBLE" % self.kind
        return self.kind or "SCALAR"


class SdfUnit:
    """One SDF member: a compilation unit and its symbols."""

    def __init__(self, path):
        self._ctx = sdfpkg.SdfContext.open(path)
        self._ctx.select(os.path.basename(path))    # raw stream: any name
        self.path = path
        self.name = None                            # unit's HAL name
        self.parse_errors = 0       # symbols whose SDC could not be decoded
        self._by_number = {}
        self._by_name = {}
        blk_no = 1
        while True:
            try:
                b = self._ctx.find_block_by_number(blk_no)
            except sdfpkg.SdfError:
                break
            for i in range(b.fsymb_no, b.lsymb_no + 1):
                try:
                    s = self._read_symbol(i)
                except (sdfpkg.SdfError, struct.error, IndexError):
                    self.parse_errors += 1      # rare malformed SDC (a cell
                    continue                    # cut short at a page edge)
                self._by_number[i] = s
                self._by_name.setdefault(s.name, s)
            blk_no += 1
        # The unit's own name comes from the directory root's CUBTC pointer
        # (the compilation unit's block cell): the first COMPOOL label in the
        # symbol table may be an INCLUDEd compool's, not this unit's.
        # BLKTCELL: ...post_dcl(2) stak_list(2) at 40, bname_len(1) at 44,
        # blk_name (EBCDIC, bname_len bytes) at 45.
        try:
            root = self._ctx.locate_root()
            cubtc_ptr = _be32(root, 44)
            if cubtc_ptr:
                cell = self._ctx.locate(cubtc_ptr)
                ln = cell[44]
                self.name = bytes(cell[45:45 + ln]).decode("cp037") or None
        except (sdfpkg.SdfError, IndexError):
            pass
        if self.name is None:
            for s in self._by_number.values():
                if s.sclass == CLASS_LABEL and s.stype == TYPE_COMPOOL_LABEL:
                    self.name = s.name
                    break
        for s in self._by_number.values():
            s.unit = self.name
            if s.stype == TYPE_STRUCTURE and not s.is_template \
                    and s.sclass == CLASS_VARIABLE:
                tmpl = self._by_number.get(s.width or 0)
                s.template = tmpl.name if tmpl else None
                s.width = None

    def _read_symbol(self, symb_no):
        r = self._ctx.find_symbol_by_number(symb_no)
        _, sdc_ptr = self._ctx.find_symbol_node_by_number(symb_no)
        mv = self._ctx.locate(sdc_ptr)
        kind = precision = None
        if r.sym_class in (CLASS_VARIABLE, CLASS_TEMPLATE, CLASS_FUNCTION):
            kind, precision = _VARIABLE_KINDS.get(r.sym_type, (None, None))
        elif r.sym_class == CLASS_LABEL:
            kind = _LABEL_KINDS.get(r.sym_type)
        # SDC field 12 ((rows<<8)|columns) is the BIT/CHAR width — or, for a
        # STRUCTURE instance, the template's symbol number (resolved by the
        # caller once every symbol is loaded).  For MATRIX/VECTOR it keeps
        # the (rows<<8)|columns shape; for a DENSE-packed BIT terminal the
        # high byte is the field's right-shift count (0xFF encodes 0).
        f12 = (r.rows << 8) | r.columns
        width = f12 if kind in ("BIT", "CHARACTER", "STRUCTURE",
                                "MATRIX", "VECTOR") else None
        ndims = r.array_dims[0]
        dims = tuple(d for d in r.array_dims[1:1 + ndims])
        # raw-SDC extras (cells never straddle a page, so mv covers the SDC);
        # 0xFFFF in a member link marks end-of-chain.
        struct_of = mv[5]
        eldest = _be16(mv, struct_of + 2) if struct_of else 0
        brother = _be16(mv, struct_of + 4) if struct_of else 0
        # ICD field 13 is a Replace Text Parameter Cell pointer only for a
        # REPLACE label (it is a constant-value cell or lock data otherwise).
        replace_ptr = 0
        if r.sym_class == CLASS_LABEL and r.sym_type == TYPE_REPLACE_LABEL:
            replace_ptr = _be32(mv, 20)
        address = None
        if r.sym_class not in (CLASS_LABEL, CLASS_FUNCTION) \
                or (r.flag1 & _F1_NAME):
            # a NAME variable declared over a label/function type is
            # classed with its referent but owns a stored pointer cell:
            # field 10 is its data offset (DASS #DDM5NEW+0019
            # DM5V_N_PROG, a NAME..LABEL at halfword 25)
            address = (mv[13] << 16) | (mv[14] << 8) | mv[15]   # field 10
        xrefs = self._read_xrefs(mv, mv[3]) if mv[3] else ()
        return SdfSymbol(
            # The alphabetically-first index entry's name carries a leading
            # EBCDIC blank (it sorts lowest, anchoring the binary search).
            name=r.symb_name.strip(),
            unit=None, number=symb_no,
            sclass=r.sym_class, stype=r.sym_type,
            kind=kind, precision=precision, width=width, dims=dims,
            address=address,
            is_template=bool(r.flag1 & _F1_TEMPLATE),
            compool=bool(r.flag1 & _F1_COMPOOL),
            name_var=bool(r.flag1 & _F1_NAME),
            parm=bool(r.flag1 & (_F1_INPUT_PARM | _F1_ASSIGN_PARM)),
            auto=bool(r.flag1 & _F1_AUTO),
            temporary=bool(r.flag1 & _F1_TEMPORARY),
            constant=bool(r.flag2 & _F2_CONSTANT),
            initial=bool(r.flag3 & _F3_INITIAL),
            remote=bool(r.flag2 & _F2_REMOTE),
            rigid=bool(r.flag3 & _F3_RIGID),
            dense=bool(r.flag2 & _F2_DENSE),
            _eldest=0 if eldest == 0xFFFF else eldest,
            _brother=0 if brother == 0xFFFF else brother,
            _replace_ptr=replace_ptr,
            xrefs=xrefs,
        )

    def _read_xrefs(self, mv, xoff):
        """XREFDATA walk (DUMPSDF PRINT_XREF_DATA): at SDC byte ``xoff``,
        halfword 0 = entry count, then halfword entries of
        (usage flags << 13) | statement number.  A 0xFFFF entry chains to
        an extension cell: the next fullword-aligned word is its vptr and
        entries continue at that cell's offset 0."""
        out = []
        k = xoff // 2
        cnt = _be16(mv, 2 * k)
        k += 1
        j = 0
        while j < cnt and len(out) < 8192:
            item = _be16(mv, 2 * k)
            if item != 0xFFFF:
                out.append(item & 0x1FFF)
                j += 1
                k += 1
            else:
                fw = (k + 2) // 2
                mv = self._ctx._locate_vptr(_be32(mv, 4 * fw))
                k = 0
        return tuple(out)

    # -- queries ---------------------------------------------------------------
    def root_flags(self):
        """The directory root cell's flag halfword (SRN_FLAG=0x8000,
        ADDR_FLAG=0x4000, ...; ICD PDF p.34)."""
        return _be16(self._ctx.locate_root(), 0)

    def literal_area(self):
        """Halfword offset of the literal area in the unit's #D csect,
        or None (the SDF stores 65535 when there is none)."""
        v = _be16(self._ctx.locate_root(), 42)
        return None if v == 0xFFFF else v

    def initial_literal_area(self):
        """Inclusive (start, end) hw extent of the #D's LEADING adcon/
        literal pool -- DUMPSDF's 'START/END OF INITIAL LITERAL AREA
        (#D)', root halfwords 84/85.  None when absent (end 0/65535):
        MAFGEN dumps the '??? ADCONS,LITERALS,ETC. ???' region only to
        this end; the slack up to the first LOCAL BLOCK DATA word
        (remote-descriptor cons, fill) is skipped entirely."""
        root = self._ctx.locate_root()
        s, e = _be16(root, 168), _be16(root, 170)
        if e == 0xFFFF:
            return None
        return (s, e)       # (0, 0) = a real 1-hw pool (DASS #DDUMULK)

    def includes(self):
        """The unit's INCLUDE list: [(member name, remote, template)].

        DUMPSDF's 'I N C L U D E   D A T A' section: directory-root
        fullword 16 heads a chain of include cells -- next pointer at
        +0, INCLIB member name at +4 (8 EBCDIC chars), flag byte at
        +16 with 0x02 = TEMPLATE, 0x01 = REMOTE (the include carried
        the REMOTE keyword)."""
        ptr = _be32(self._ctx.locate_root(), 64)
        out = []
        while ptr > 0 and len(out) < 512:
            mv = self._ctx.locate(ptr)
            name = bytes(mv[4:12]).decode("cp037").strip()
            fl = mv[16]
            out.append((name, bool(fl & 0x01), bool(fl & 0x02)))
            ptr = _be32(mv, 0)
        return out

    def read_statements(self):
        """Executable statements: [(stmt_no, csect_name, addr1, addr2,
        category, stype, srn, incl_cnt)].

        Statement data cell (ICD PDF p.103): block index (2), statement
        category (1: bit0 SRN present, bits3-4 subtype, bits5-7 context),
        statement type (1), label count (1), LHS halfword count (1), then
        the label and LHS arrays; when the root's ADDR_FLAG is set two
        3-byte csect-relative code addresses follow, then the original SRN.
        Declaration statements (negated data pointer) are skipped."""
        ctx = self._ctx
        root = ctx.locate_root()
        flags = _be16(root, 0)
        has_addr = bool(flags & 0x4000)
        first, last = _be16(root, 52), _be16(root, 54)
        csect_of = {}
        out = []
        for n in range(first, last + 1):
            try:
                r = ctx.find_stmt_by_number(n)
            except sdfpkg.SdfError:
                continue                    # null pointer / out of range
            if not r.is_executable:
                continue
            # re-locate the data cell for the fields past sdfpkg's parse
            sn_mv = ctx._locate_vptr(ctx._ndx2ptr(8, n))
            if ctx._cur_fcb.flags & sdfpkg.FLAG_HAS_SRNS:
                sdc_ptr = _be32(sn_mv, 8)
            else:
                sdc_ptr = _be32(sn_mv, 0)
            mv = ctx.locate(sdc_ptr & 0x7FFFFFFF)
            addr1 = addr2 = None
            if has_addr:
                p = 6 + 2 * r.num_labls + 2 * r.num_lhs
                addr1 = (mv[p] << 16) | (mv[p + 1] << 8) | mv[p + 2]
                addr2 = (mv[p + 3] << 16) | (mv[p + 4] << 8) | mv[p + 5]
            blk = r.blk_no
            if blk not in csect_of:
                try:
                    csect_of[blk] = ctx.find_block_by_number(blk).csect_name
                except sdfpkg.SdfError:
                    csect_of[blk] = None
            srn = r.srn.decode("cp037", errors="replace").strip() \
                if isinstance(r.srn, bytes) else str(r.srn)
            out.append((n, csect_of[blk], addr1, addr2,
                        r.stmt_type >> 8, r.stmt_type & 0xFF, srn,
                        r.incl_cnt))
        return out

    def symbols(self):
        """Every symbol, in symbol-index order."""
        return [self._by_number[i] for i in sorted(self._by_number)]

    def symbol(self, name):
        """The symbol called `name`, or None."""
        return self._by_name.get(name.strip().upper())

    def variables(self):
        """The unit's declared variables (class 1) in index order."""
        return [s for s in self.symbols() if s.sclass == CLASS_VARIABLE]

    def structure(self, template_name, flatten=True):
        """The template's member symbols.  With flatten=True (default),
        minor-structure group nodes are descended and only LEAF fields are
        returned, in declaration order."""
        head = self.symbol(template_name)
        if head is None or head.stype != TYPE_STRUCTURE:
            return None
        out = []
        self._walk(head._eldest, out, flatten)
        return out

    def _walk(self, idx, out, flatten):
        while idx:
            s = self._by_number.get(idx)
            if s is None:
                return
            if flatten and s.stype == TYPE_STRUCTURE:
                self._walk(s._eldest, out, flatten)
            else:
                out.append(s)
            idx = s._brother

    def structures(self):
        """{template name: [leaf member SdfSymbol]} for every structure
        template defined in this unit."""
        out = {}
        for s in self.symbols():
            if s.sclass == CLASS_TEMPLATE and s.stype == TYPE_STRUCTURE \
                    and s.is_template:
                leaves = []
                self._walk(s._eldest, leaves, True)
                out[s.name] = leaves
        return out

    def replaces(self):
        """{alias: body-text} from the Replace Text cells (ICD §2.2.2.2.4.4):
        parameter cell = [macro-cell ptr:4][#args:2]...; macro cells =
        [next:4][len:2][text], text blank-compressed as (0xEE, count-1)."""
        out = {}
        for s in self.symbols():
            if not s._replace_ptr:
                continue
            mv = self._ctx.locate(s._replace_ptr)
            macro = _be32(mv, 0)
            raw = bytearray()
            while macro:
                mv = self._ctx.locate(macro)
                nxt = _be32(mv, 0)
                ln = _be16(mv, 4)
                raw += bytes(mv[6:6 + ln])
                macro = nxt
            text = bytearray()
            i = 0
            while i < len(raw):
                if raw[i] == 0xEE and i + 1 < len(raw):
                    text += b"\x40" * (raw[i + 1] + 1)
                    i += 2
                else:
                    text.append(raw[i])
                    i += 1
            out[s.name] = codecs.decode(bytes(text), _EBCDIC).strip()
        return out

    def char_initials(self):
        """{CHARACTER var: INITIAL string} via SDFPKG mode 18: the view lands
        on the variable's slot in the Initialization Table — a CHARACTER(n)
        datum is a descriptor halfword (max<<8 | current-length) followed by
        packed EBCDIC text."""
        out = {}
        for s in self.variables():
            if s.kind != "CHARACTER" or not s.initial:
                continue
            try:
                mv = self._ctx.find_init_data(s.number)
            except sdfpkg.SdfError:
                continue
            cur = min(mv[1], max(0, len(mv) - 2))
            out[s.name] = codecs.decode(bytes(mv[2:2 + cur]), _EBCDIC)
        return out

    def initial_data(self, name, nbytes):
        """Raw Initialization Table bytes for symbol `name` (its in-memory
        initial value image), or None."""
        s = self.symbol(name)
        if s is None:
            return None
        try:
            mv = self._ctx.find_init_data(s.number)
        except sdfpkg.SdfError:
            return None
        return bytes(mv[:nbytes])

    def close(self):
        self._ctx.close()


class SdfLibrary:
    """A directory of PASS3 SDF members, resolved by compool/unit name.

    `path` may also be a search path -- an os.pathsep-separated string or a
    list of directories -- searched in order with the FIRST member of a given
    filename winning (so a per-phase SDFLIB can shadow a shared one)."""

    def __init__(self, path):
        if isinstance(path, (list, tuple)):
            self.paths = [str(p) for p in path]
        else:
            self.paths = str(path).split(os.pathsep)
        self.path = os.pathsep.join(self.paths)
        self._units = {}            # member filename -> SdfUnit (parsed)
        self._by_name = {}          # HAL unit name -> SdfUnit
        self._scanned = False

    def _member_path(self, fn):
        """Full path of member `fn` in the first directory holding it."""
        for p in self.paths:
            full = os.path.join(p, fn)
            if os.path.exists(full):
                return full
        return None

    def _load(self, fn):
        u = self._units.get(fn)
        if u is None:
            full = self._member_path(fn)
            if full is None:
                return None
            u = SdfUnit(full)
            self._units[fn] = u
            if u.name:
                self._by_name.setdefault(u.name.upper(), u)
        return u

    def unit(self, name):
        """The SdfUnit for a unit reference: its full HAL name
        (`CGC_COMMON`) or its compressed MEMBER name (`CGZFL3`).  PASS3
        names SDF members `##` + the unit name squeezed of underscores,
        truncated to 6; falls back to scanning every member's
        compilation-unit symbol for a full-name match."""
        want = name.strip().upper()
        if want in self._by_name:
            return self._by_name[want]
        sq = want.replace("_", "")
        fn = "##" + sq[:6] + ".sdf"
        if self._member_path(fn) is not None:
            u = self._load(fn)
            # accept a full-name match, or a member-name reference (the
            # squeezed unit name extends the squeezed reference)
            if u.name is None or u.name.upper() == want \
                    or u.name.upper().replace("_", "").startswith(sq):
                return u
        if self._scan():
            if want in self._by_name:
                return self._by_name[want]
        return None

    def _scan(self):
        """Parse every member of every directory once (first dir wins per
        filename via the _units cache).  True if a scan just happened."""
        if self._scanned:
            return False
        self._scanned = True
        for p in self.paths:
            if not os.path.isdir(p):
                continue
            for f in sorted(os.listdir(p)):
                if f.startswith("##") and f.endswith(".sdf"):
                    try:
                        self._load(f)
                    except Exception:
                        pass
        return True

    def units(self):
        """Every unit in the library (scans and parses all members)."""
        self._scan()
        return [self._units[f] for f in sorted(self._units)]

    def lookup(self, unit_name, symbol_name):
        """One-call query: the SdfSymbol for `symbol_name` in `unit_name`,
        or None.  A name that is a leaf field of a structure template is
        found through the template as well."""
        u = self.unit(unit_name)
        if u is None:
            return None
        s = u.symbol(symbol_name)
        if s is not None:
            return s
        for leaves in u.structures().values():
            for leaf in leaves:
                if leaf.name == symbol_name.strip().upper():
                    return leaf
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not (2 <= len(argv) <= 3):
        print("usage: python3 -m ap101Utils.sdf LIBDIR UNIT [SYMBOL]",
              file=sys.stderr)
        return 2
    lib = SdfLibrary(argv[0])
    unit = lib.unit(argv[1])
    if unit is None:
        print("no SDF member for unit %r in %s" % (argv[1], argv[0]),
              file=sys.stderr)
        return 1
    if len(argv) == 2:
        print("unit %s (%s)" % (unit.name, os.path.basename(unit.path)))
        for s in unit.symbols():
            extra = " ARRAY%r" % (s.dims,) if s.dims else ""
            print("  %-32s %s%s" % (s.name, s.hal_type(), extra))
        return 0
    sym = lib.lookup(argv[1], argv[2])
    if sym is None:
        print("symbol %r not found in %s" % (argv[2], unit.name),
              file=sys.stderr)
        return 1
    print("%s.%s: %s" % (sym.unit, sym.name, sym.hal_type()))
    for k in ("kind", "precision", "width", "dims", "copies", "address",
              "template", "constant", "initial", "remote", "rigid", "dense"):
        print("  %-10s %r" % (k, getattr(sym, k)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
