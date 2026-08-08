"""DASS-style listing emitters + real-DASS normalizer (strip mode).

Line geometry (0-based columns in the 133-char listing line; col 0 is the
ASA carriage-control character) measured from DASS_G2.ASC:

  addr        1-6    start address, 6 hex; '-' at 7; end address 8-13
  csect      16-23   owning csect, 8 wide; '+' 24; offset 25-28 (4 hex)
  header     25-28 '****'; 30-40 'SSSS(nnnnn)'; 43-85 category; 86 '|';
             88+ HAL block name
  hex        36+    4-hex halfword tokens, stride 6
  ea         48-53   resolved effective address, 6 hex
  mnemonic   65-71   7 wide incl. @/# suffixes
  operands   72+
  comment    95+    symbolic operand / immediate value
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (REPO / "src",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ap101Utils.fcmImage import FcmImage             # noqa: E402
from .asmrender import AsmRender                     # noqa: E402
from .common import (_ASM_CATS, _BANNER, COL_EA, COL_MARK,  # noqa: E402
                     COL_MNEM, addr_range, stmt_kind)
from .csects import Csect, classify                  # noqa: E402
from .decode import Ap101Disassembler                # noqa: E402
from .errsim import ErrorSim                         # noqa: E402
from .haldata import DataWalk                        # noqa: E402
from .halcode import CodeRender                      # noqa: E402
from .sdfnames import SdfNameMap                     # noqa: E402
from .strip import strip_dass                        # noqa: E402  (re-export)


class Listing(DataWalk, AsmRender, CodeRender):
    def __init__(self, img: FcmImage, sdf: SdfNameMap,
                 mmu_root: Path | None = None, deck: dict | None = None,
                 stars: set | None = None,
                 degrade: set | None = None):
        self.img = img
        self.sdf = sdf
        # patched halfwords (differ from the --base image): their hex
        # tokens print with '*' in the preceding column, the DASS
        # memory-vs-load-module marker
        self.stars = stars or set()
        # symbols to leave unresolved (the ' ?  csect+off' form) for
        # diff parity with flight-SDF xref gaps -- see --degrade
        self.degrade = frozenset(degrade or ())
        self.mmu_root = Path(mmu_root) if mmu_root else None
        self._asmg_cache: dict = {}         # (phase, module) -> per-csect info
        self._line_cache: dict = {}         # (phase, module) -> {name: line}
        self.dis = Ap101Disassembler()
        self.csects = []
        seen = set()
        for c in img.csects:
            if c.imported or c.name in seen:
                continue
            # DASS listing scope excludes the IPL bootstrap phase: its
            # csects never appear (memory it still owns reads as holes),
            # though its load blocks still yield checksum slots below.
            if c.phase == "PHASE10":
                continue
            seen.add(c.name)
            cat, kind, halname = classify(c.name, sdf, deck)
            # bare-named RUNASM runtime-library members (IMOD, ACOS,
            # SQRT...) are HAL LIBRARY CODE in the DASS but NONHAL by
            # name; the membership signal is the module's sidecar
            # resolving in the runtime library, not a phase obj dir
            # (the same fallback order _asmg_for uses)
            mod, ph = getattr(c, "module", None), getattr(c, "phase", None)
            if cat == "NONHAL" and self.mmu_root and mod and ph \
                    and not (self.mmu_root / ph / "obj"
                             / (mod + ".asmg.json")).exists() \
                    and (self.mmu_root.parent / "lib" / "runtime" / "RUN"
                         / (mod + ".asmg.json")).exists():
                cat = "LIBCODE"
            self.csects.append(
                Csect(c.name, c.start, c.size, cat, kind, halname,
                      getattr(c, "module", None), getattr(c, "phase", None),
                      reserved=getattr(c, "reserved", False)))
        # internal-block csects header their own block kind + name
        # (DASS: 'PROCEDURE MM_WRITE_LB'; parent unit stays after '|')
        for c in self.csects:
            if c.cat == "INTERNAL":
                b = sdf.blocks(c.name[2:]).get(c.name)
                if b:
                    c.kind, c.blockname = b[0], b[1]
        # load-block CHECKSUM slots (the DASS '--------' pseudo-csects):
        # 2-hw fullword-aligned gaps after each TEXT extent run, staged
        # C6C6 by the MMB unless the IPL stamped them (Post-IPL stars)
        for a in getattr(img, "checksum_slots", []):
            self.csects.append(Csect("--------", a, 2, "CHECKSUM"))
        # R1 (local data base) per code stem: address of the #D csect
        self.data_base = {c.name[2:]: c.start for c in self.csects
                          if c.name.startswith("#D")}
        self.cat_by_name = {c.name: c.cat for c in self.csects}
        self.esim = ErrorSim(img, self.csects)   # dump_lines resets it
        # compool stems some unit INCLUDEd with the REMOTE keyword: their
        # #P headers annotate 'INCLUDE REMOTE'
        self._remote_included = sdf.remote_included()
        # relocated halfwords: an immediate that was relocated is an
        # address constant -> the LHI encoding really is LA (MAFGEN
        # prints LA with the resolved EA there)
        self.reloc_sites = {r["address"]
                            for r in img.manifest.get("relocations", [])}
        # relocated hw -> full 19-bit target address: MAFGEN resolves the
        # address constant through the RLD, so an LA of a high-sector #D
        # shows the full EA (DASS: E9F3 8214 -> 038214 #DDCDDOW)
        self.reloc_target = {r["address"]: r["target"]
                             for r in img.manifest.get("relocations", [])
                             if r.get("target") is not None}
        # target csect names for reloc-resolved EAs: aliased csects share
        # a base address, and the RLD knows which one the ZCON meant
        # (DASS: SCAL through #ZFIOCGR -> '#CFIOCGR', not #CRIOCGR)
        self.reloc_name = {r["address"]: r["targetName"]
                           for r in img.manifest.get("relocations", [])
                           if r.get("targetName")}
        # relocated halfwords that are genuine Y-adcons (sane in-image
        # target): the code-region 'DC Y' rule fires only for these --
        # exotic reloc types (MSC/BCE vector fixups, garbage targets)
        # disassemble as code, exactly as flight rendered FCMIMSC+0
        self.ycon_sites = {r["address"]
                           for r in img.manifest.get("relocations", [])
                           if (r.get("target") is None
                               or r["target"] < 0x80000)}
        # unresolved sites are relocated halfwords too (=Y'' material)
        self.unresolved_sites = {
            r["imageOffsetHW"]
            for r in img.manifest.get("unresolvedRelocations", [])
            if r.get("imageOffsetHW") is not None}
        # Per-unit resolution scopes (MAFGEN's model): a unit's code sees
        # its own #D symbols plus each INCLUDEd compool's symbols in that
        # compool's #P space; anything else annotates as '?  #Pxxx+off'.
        import bisect
        self._bisect = bisect
        self.scopes: dict[str, dict[str, tuple[list, list]]] = {}
        self.stack_syms: dict[str, tuple[list, list]] = {}   # by code csect
        self.lit_area: dict[str, int] = {}
        self.init_pool: dict[str, int] = {}   # #D -> leading pool hw count
        self.blockdata: dict[str, int] = {}
        for stem in sdf.map:
            sc = {}
            for csect, lst in sdf.scopes(stem).items():
                sc[csect] = ([a for a, _ in lst], [n for _, n in lst])
            if sc:
                self.scopes[stem] = sc
            for csect, lst in sdf.stak(stem).items():
                self.stack_syms[csect] = ([a for a, _ in lst],
                                          [n for _, n in lst])
            own_d = "#D" + stem
            if own_d in sc and sc[own_d][0]:
                self.blockdata[own_d] = sc[own_d][0][0] - 2
            lit = sdf.litarea(stem)
            if lit is not None:
                self.lit_area[own_d] = lit
            il = sdf.ilit(stem)
            if il is not None:
                self.init_pool[own_d] = il[1] + 1
        # data-csect walk material: entries per #D/#P csect, templates per
        # stem (later-loaded phases win, same as the SdfNameMap itself)
        self.data_map: dict[str, list] = {}
        self.tmpl_map: dict[str, dict] = {}
        for stem in sdf.map:
            for cs, lst in sdf.data(stem).items():
                self.data_map[cs] = self._rebucket(lst)
            t = sdf.tmpl(stem)
            if t:
                self.tmpl_map[stem] = t
        # per-unit XREF citation sets: {stem: {symbol name: {stmt nums}}}
        # -- the named-vs-'?' selector (a scope hit resolves by name only
        # when the ref's enclosing statement cites the symbol)
        self.xref_by_stem: dict[str, dict[str, set]] = {}
        for stem in sdf.map:
            x = sdf.xref(stem)
            if x:
                self.xref_by_stem[stem] = {n: set(v) for n, v in x.items()}
        # multi-copy STRUCTURE symbols: MAFGEN flattens copies 1-based, so
        # a reference at hw offset k prints as NAME+(k+stride) (DASS:
        # CDWV_DWNLST_AREA hw+2, 3 copies of 128 -> '+130').  Map csect ->
        # {symbol_addr: (stride, total_extent)}.
        self.struct_stride: dict[str, dict[int, tuple[int, int]]] = {}
        # arrayed-STRUCTURE biased bases: scope lists keep structures
        # unbiased (the multi-copy flattening arithmetic depends on it),
        # but an indexed PEA landing on an arrayed structure's addr-1 is
        # the same 1-based biased hit as any arrayed symbol (DASS
        # $0DGMWRT: LH -> EA 003001, bare CDUV_TWO_STAGE_BUFF)
        self.struct_bias: dict[str, set] = {}
        for cs, groups in self.data_map.items():
            for _gid, _gn, ents in groups:
                for e in ents:
                    addr, _nm, kind, _pr, _w, dims, fl, tmplname = e
                    if kind != "STRUCTURE" and not fl & 1 \
                            and sum(1 for dd in dims if dd) >= 2:
                        # multi-dimension arrays flatten 1-based in every
                        # dimension: display offset = scope offset +
                        # trailing-dims row stride (DASS $0AIBGPC:
                        # ARRAY(19,3) INTEGER ref at +0 -> CDCV_PHASES+4
                        # = 1 + row 3), keyed at the biased base like
                        # the scope list
                        ehw = self._elem_hw(kind, _pr, _w)
                        row = full = ehw
                        for k2, dd in enumerate(dims):
                            if dd:
                                full *= dd
                                if k2 > 0:
                                    row *= dd
                        self.struct_stride.setdefault(cs, {})[addr - 1] = (
                            row, full + 1)
                        continue
                    if kind != "STRUCTURE" or fl & 1:
                        continue
                    if dims:
                        self.struct_bias.setdefault(cs, set()).add(
                            addr - 1)
                    copies = self._copies(dims)
                    leaves = self.tmpl_map.get(cs[2:], {}).get(
                        tmplname or "")
                    if copies > 1 and leaves:
                        stride, _used = self._tmpl_geom(leaves)
                        self.struct_stride.setdefault(cs, {})[addr] = (
                            stride, stride * copies)
        # per-symbol storage extents (halfwords), keyed like the scope
        # lists (arrays at their biased base): nearest-below resolution
        # only holds while the offset is inside the symbol -- a ref past
        # a symbol's extent prints the ' ?  csect+off' form (DASS:
        # 'TFCMID' exact but ' ?  #PFCMCOM+62' past TFCMBCEB1)
        self.sym_extent: dict[str, dict[int, int]] = {}
        for cs, groups in self.data_map.items():
            d = self.sym_extent.setdefault(cs, {})
            for _gid, _gn, ents in groups:
                for e in ents:
                    addr, _nm, kind, prec, wraw, dims, fl, tmplname = e
                    bias = 0
                    if fl & 1:
                        ext = 2 if fl & 8 else 1
                    elif kind == "STRUCTURE":
                        leaves = self.tmpl_map.get(cs[2:], {}).get(
                            tmplname or "")
                        copies = self._copies(dims)
                        if leaves:
                            stride, used = self._tmpl_geom(leaves)
                            ext = used if copies == 1 else stride * copies
                        else:
                            ext = 1
                    else:
                        ext = (self._elem_hw(kind, prec, wraw)
                               * self._copies(dims))
                        bias = 1 if dims else 0
                    d[addr - bias] = max(ext + bias, d.get(addr - bias, 0))
        # LBD base from the data groups (same derivation as _data_rows):
        # first stored static minus the 2-hw headers of the blocks laid
        # out ahead of it -- the scope-based first-addr-2 guess above is
        # wrong when a CONSTANT static sits at offset 0.  Every block's
        # LBD word gets the annotation (DASS: '#DDMPMMM+200 (LOCAL
        # BLOCK DATA)' for the A8 block), so keep the full offset set.
        self.lbd_offsets: dict[str, set] = {}
        for cs, groups in self.data_map.items():
            if not cs.startswith("#D"):
                continue
            first = True
            pend_empty = 0
            for i, (_gid, _gn, ents) in enumerate(groups):
                stored = [e for e in ents if not (e[6] & 2 and e[0] == 0)]
                if not stored:
                    # statics-less blocks still own a 2-hw LBD word,
                    # packed right before the next block's (DASS:
                    # MM_WRITE_LB's at +200 before MM_RPL_UPDATE's +202)
                    pend_empty += 1
                    continue
                if first:
                    self.blockdata[cs] = stored[0][0] - 2 * (i + 1)
                    first = False
                lbd = stored[0][0] - 2
                s = self.lbd_offsets.setdefault(cs, set())
                for k in range(pend_empty + 1):
                    s.add(lbd - 2 * k)
                pend_empty = 0
        # ST# statements, re-keyed by owning csect (a unit's statements
        # span its #C/$0 csect and its internal-block A<n> csects)
        self.stmts_by_csect: dict[str, list] = {}
        known = {c.name for c in self.csects}
        for stem in sdf.map:
            for n, cs, a1, cat, typ, srn, inc in sdf.stmts(stem):
                if cs in known and a1 is not None:
                    self.stmts_by_csect.setdefault(cs, []).append(
                        (a1, n, stmt_kind(cat, typ), srn, inc))
        for lst in self.stmts_by_csect.values():
            lst.sort()

    @staticmethod
    def _rebucket(groups):
        """MAFGEN's #D walk is ADDRESS-bucketed: a static renders in the
        block REGION its address falls in, not its declaring block's
        group.  The compiler occasionally allocates an inner block's
        static in an earlier region (DASS #DASLTMC: ASL_EVENT_TIMER,
        declared in ASL_TM_CASE_PROC, sits at +005C inside the root
        block's statics).  Move any entry whose address precedes an
        earlier group's content into the group whose region covers it."""
        prev_max = None                 # highest stored addr so far
        anchors = []                    # (min stored addr, group index)
        for gi, (_gid, _gn, ents) in enumerate(groups):
            if prev_max is not None:
                # stack-resident twins (flag 32) are tie-break material,
                # never walked -- they neither move nor anchor
                stored = [e for e in ents
                          if not (e[6] & 2 and e[0] == 0)
                          and not e[6] & 32]
                strays = [e for e in stored if e[0] < prev_max]
                # only isolated strays move: the group's real region
                # must continue past the prior content (a group lying
                # wholly below keeps the declaring-block layout)
                if strays and any(e[0] >= prev_max for e in stored):
                    for e in strays:
                        tgt = None
                        for lo, gj in anchors:
                            if lo <= e[0]:
                                tgt = gj
                        if tgt is not None:
                            ents.remove(e)
                            groups[tgt][2].append(e)
                            groups[tgt][2].sort(key=lambda x: x[0])
            stored = [e[0] for e in ents
                      if not (e[6] & 2 and e[0] == 0) and not e[6] & 32]
            if stored:
                anchors.append((min(stored), gi))
                prev_max = max(prev_max or 0, max(stored))
        return groups

    #
    # sections
    #
    def index_lines(self):
        """Alphabetical (EBCDIC collation) csect -> address index, 7/row.
        Checksum pseudo-csects are unnamed and stay out of the index."""
        ordered = sorted((c for c in self.csects if c.cat != "CHECKSUM"),
                         key=lambda c: c.name.encode("cp037"))
        out, row = [], []
        for c in ordered:
            row.append(f"{c.name:<8} {c.start:06X}")
            if len(row) == 7:
                out.append(" " + "   ".join(row))
                row = []
        if row:
            out.append(" " + "   ".join(row))
        return out

    def extent_lines(self):
        out = []
        for c in sorted(self.csects, key=lambda c: c.start):
            if c.reserved:
                out.append(" *** BEGIN RESERVED CSECT ***")
            out.append(self.header_line(c))
            if c.reserved:
                out.append(" *** END RESERVED CSECT ***")
        return out

    def header_line(self, c: Csect):
        # a single-location csect prints no end address (DASS: FCMTBLPG)
        rng = (f"{c.start:06X}-{c.end:06X}" if c.size > 1
               else f"{c.start:06X}       ")
        head = f" {rng}  {c.name:<8} **** {c.size:04X}({c.size:5d})  "
        if c.cat in _BANNER:
            return head + _BANNER[c.cat]
        if c.cat == "INTERNAL" and c.blockname:
            cat = f"{c.kind:<9} {c.blockname}"
        elif c.cat in ("PROGRAM", "COMSUB", "INTERNAL"):
            cat = f"{c.kind:<9} {c.halname}"
        else:
            cat = {"DATA": "DATA", "COMPOOL": "DATA", "ZCON": "ZCON",
                   "PDE": "PDE", "STACK": "STACK",
                   "EXCLUSIVE": "EXCLUSIVE"}[c.cat]
        # header attribute columns after the HAL name -- code/data csects
        # of a DATA REMOTE unit (name padded to 23), and #P compools some
        # unit INCLUDEd with the REMOTE keyword (name + 2 spaces); ZCON/
        # PDE/STACK headers never carry either (DASS: #ZDMZLOG plain)
        name = c.halname
        if c.cat in ("PROGRAM", "COMSUB", "INTERNAL", "DATA") \
                and self.sdf.data_remote(c.name[2:]):
            name = f"{c.halname:<23}DATA REMOTE   FC-32.0"
        elif c.cat == "COMPOOL" and c.name[2:] in self._remote_included:
            name = f"{c.halname}  INCLUDE REMOTE"
        return f"{head}{cat:<43}| {name}"

    def dump_lines(self):
        # flight MAFGEN's error simulation is one machine over the whole
        # dump: global error numbering, register file carried across
        # csects -- fresh per emission so repeated calls don't leak
        self.esim = ErrorSim(self.img, self.csects)
        out = []
        for c in sorted(self.csects, key=lambda c: c.start):
            if c.reserved:
                out.append(" *** BEGIN RESERVED CSECT ***")
            out.append(self.header_line(c))
            out.extend(self.body_lines(c))
            if c.reserved:
                out.append(" *** END RESERVED CSECT ***")
        return out

    #
    # body forms
    #
    def body_lines(self, c: Csect):
        # the flight error simulator resets its register file per csect;
        # HAL csects re-seed the R0 stack frame and the R1 #D-base pin
        self.esim.begin_csect(
            dbase=self.data_base.get(c.name[2:]),
            hal=c.cat in ("PROGRAM", "COMSUB", "INTERNAL"))
        if c.cat == "STACK":
            return []
        if c.cat == "CHECKSUM":
            # one hex row: the slot's two halfwords (C6C6 staging, or the
            # IPL-stamped value); occupancy never covers slots
            return self._hex_rows(c, width=2, any_hw=True)
        if c.cat in ("ZCON", "QCON", "PDE", "EXCLUSIVE"):
            # always load-backed text; the .lib occupancy map may not
            # cover the PSA pointer slots, so dump unconditionally
            return self._hex_rows(c, width=min(c.size, 16), any_hw=True)
        if c.cat == "LIBCODE":
            # runtime-library members replay their asm sidecar stream
            # (DASS renders them asm-style: DS 0H labels, in-csect
            # symbol comments); no sidecar degrades to disassembly
            rows = self._asm_rows(c)
            return rows if rows is not None else self._code_rows(c)
        if c.cat == "LIBDATA":
            # HAL LIBRARY DATA (#L) csects replay the module's DC
            # stream too (DASS #LDEXP: 'AERROR1 DC H  20', unnamed
            # 'DC XL4' rows with their DP float renderings); no
            # sidecar degrades to the hex dump
            rows = self._asm_rows(c)
            if rows is not None:
                return rows
        if c.cat in ("PROGRAM", "COMSUB", "INTERNAL"):
            return self._code_rows(c)
        if c.cat in ("DATA", "COMPOOL"):
            rows = self._data_rows(c)
            if rows is not None:
                return rows
        if c.cat == "PATCH":
            # patch areas dump as bare 16-wide hex rows -- never the
            # patch deck's DC statement lines (DASS OPSZFILL/#Y021001:
            # no 'DC 80XL1' row, just the fill halfwords)
            return self._dhex_rows(c, c.start, c.size)
        if c.cat in _ASM_CATS:
            rows = self._asm_rows(c)
            if rows is not None:
                return rows
        return self._hex_rows(c, width=16)

    def _hx(self, a, v):
        """A 4-hex value token with the patch star ('*' for a halfword
        differing from the --base image) in the preceding column."""
        return ("*" if a in self.stars else " ") + f"{v:04X}"

    def _row(self, c, a, values):
        body = f" {addr_range(a, len(values))}  {c.name:<8}+{a - c.start:04X}"
        body = body.ljust(COL_MARK) + " ".join(
            self._hx(a + i, v) for i, v in enumerate(values))
        return body.rstrip()

    def _inst_prefix(self, c, a, inst, hw1, hw2):
        """A disassembly line up to and including the operands: address
        range, csect+offset, hex halfwords, resolved EA, mnemonic.  The
        comment column is the caller's -- the two code renderers attach
        (and wrap) it differently."""
        n = inst.nhw
        line = f" {addr_range(a, n)}  {c.name:<8}+{a - c.start:04X}"
        line = line.ljust(COL_MARK) + self._hx(a, hw1)
        if n > 1:
            line += " " + self._hx(a + 1, hw2)
        line = line.ljust(COL_EA)
        line += f"{inst.ea:06X}" if inst.ea is not None else "      "
        line = line.ljust(COL_MNEM)
        return line + f"{inst.mnemonic:<6} {inst.operands}"

    def _hex_rows(self, c, width, any_hw=False):
        """Raw dump rows over load-backed halfwords, up to ``width`` per
        row, breaking at unoccupied gaps (RESERVE space stays silent).
        any_hw dumps the full extent regardless of the occupancy map."""
        out, run, run_a = [], [], None
        occ, words = self.img.occupied, self.img.words
        for a in range(c.start, c.start + c.size):
            if any_hw or a in occ:
                if not run:
                    run_a = a
                run.append(words.get(a, 0))
                if len(run) == width:
                    out.append(self._row(c, run_a, run))
                    run = []
            elif run:
                out.append(self._row(c, run_a, run))
                run = []
        if run:
            out.append(self._row(c, run_a, run))
        return out
