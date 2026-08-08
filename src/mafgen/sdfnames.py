"""SdfNameMap -- per-stem SDF facts (HAL block names/kinds, data symbols,
statements, xref) collected from the phases' SDFLIB dirs, JSON-cached."""
import json
from pathlib import Path

_CACHE_VERSION = 18


class SdfNameMap:
    """Per-stem SDF facts from the phases' SDFLIB dirs: the unit's HAL
    block name and kind, its data symbols (name, hw offset into the
    unit's #D/#P data csect), the #D literal-area offset, and -- when the
    SDF was compiled with ADDRS -- the executable statements for ST#
    lines.

    Later-loaded phases win, mirroring mmu2fcm overlay semantics.  Cached
    as JSON because opening ~900 SDF members takes a few seconds.
    """

    def __init__(self, mmu_root: Path, phases, cache: Path | None = None,
                 refresh=False, overlay: Path | None = None):
        self.map: dict[str, dict] = {}
        if cache and cache.exists() and not refresh:
            d = json.load(open(cache))
            if d.get("_version") == _CACHE_VERSION:
                self.map = d["units"]
                return
        from ap101Utils.sdf import SdfUnit, CLASS_LABEL, CLASS_VARIABLE
        for phase in phases:                        # load order
            libdir = mmu_root / phase / "gen" / "SDFLIB"
            # an ADDRS-bearing regenerated SDF set (mafgen regen)
            # covers every unit of the phase, not just the members the
            # build retained in SDFLIB -- prefer it when present
            if overlay and (overlay / phase / "sdf").is_dir():
                libdir = overlay / phase / "sdf"
            if not libdir.is_dir():
                continue
            for f in sorted(libdir.glob("##*.sdf")):
                stem = f.stem[2:]
                try:
                    u = SdfUnit(str(f))
                except Exception:
                    continue
                if not u.name:
                    continue
                label = u._by_name.get(u.name)
                kind = getattr(label, "kind", None) or "PROCEDURE"
                # initial=1 marks statically-placed (#D/#P-resident) data;
                # initial=0 variables are stack-frame temporaries whose
                # 'address' is a stack offset -- excluding them keeps loop
                # temps like I/J from shadowing the real #D symbols
                # Per-BLOCK symbol attribution -- MAFGEN's resolution
                # model: a unit sees its own #D data plus each INCLUDEd
                # compool's symbols in that compool's #P address space;
                # anything else prints as '?  #Pxxxx+off'.  Class-4
                # blocks are compool includes (csect = the #P); class-2
                # blocks are this unit's code blocks whose initial data
                # lives in the unit's #D and whose initial=0 temps are
                # R0(stack)-relative, scoped to the block's own csect.
                # Arrayed non-structure symbols display from address-1:
                # HAL arrays are 1-based, so the compiler's biased base
                # sits one element low (DM2B_APP_LOG bare at addr-1,
                # +1 at addr, ...).
                scopes: dict[str, list] = {}
                stak: dict[str, list] = {}
                # per-symbol citing statements (SDC XREFDATA): the
                # named-vs-'?' selector -- a scope hit only resolves by
                # name when the enclosing statement cites the symbol
                xref: dict[str, list] = {}
                # internal-block csects: [kind, block name] for headers
                # (DASS: 'PROCEDURE MM_WRITE_LB | DMP_MM_MSG_PROC')
                blocks: dict[str, list] = {}
                # Data-walk material for the MEMORY MAP data-csect
                # rendering, grouped per BLOCK: the unit's #D holds, after
                # the adcon pool, one segment per own block (main + A<n>
                # internals, ascending blk_id) -- a 2-halfword LOCAL BLOCK
                # DATA word, then that block's statics (INITIAL'd or
                # CONSTANT; plain initial=0 variables live on the stack).
                # Own blocks are the ones whose csect carries this stem
                # ($0/#C/A<n>/B<n> + stem); other units' blocks in the
                # symbol table are external references.  A compool unit
                # contributes a single bannerless group for its #P.
                data: dict[str, list] = {}
                own_d = "#D" + stem
                own_p = "#P" + stem
                blk_no = 1
                while True:
                    try:
                        b = u._ctx.find_block_by_number(blk_no)
                    except Exception:
                        break
                    tgt = b.csect_name if b.blk_class == 4 else own_d
                    own_pool = b.blk_class == 4 and b.csect_name == own_p
                    own_block = b.blk_class != 4 \
                        and b.csect_name[2:] == stem
                    grp = None
                    if own_pool or own_block:
                        key = own_p if own_pool else own_d
                        grp = [getattr(b, "blk_id", 0) or 0,
                               b.blk_name or "", []]
                        data.setdefault(key, []).append(grp)
                    if own_block and b.blk_name:
                        lbl = u._by_name.get(b.blk_name)
                        blocks[b.csect_name] = [
                            getattr(lbl, "kind", None) or "PROCEDURE",
                            b.blk_name]
                    for i in range(b.fsymb_no, b.lsymb_no + 1):
                        s = u._by_number.get(i)
                        # NAME variables declared over labels are classed
                        # with the referent but hold a stored pointer
                        # (DASS: DM5V_N_PROG 'NAME BIT STRING LABEL')
                        if s is None or s.address is None or not (
                                s.sclass == CLASS_VARIABLE
                                or (s.sclass == CLASS_LABEL
                                    and s.name_var)):
                            continue
                        bias = 1 if s.dims and s.kind != "STRUCTURE" else 0
                        # NAME variables are statically-homed pointer
                        # descriptors even without INITIAL (DASS
                        # #DDNXBMS: DNX_NM walks, value 0000) -- but a
                        # NAME formal parameter lives in the argument
                        # list, not the #D (DASS #DDGOGSE: no
                        # DGO_BUFFER_ADDR row at the colliding +000C).
                        # Stack residence is explicit in the SDC flags
                        # (parm/AUTOMATIC/TEMPORARY): a variable with
                        # none of them is #D-allocated even without
                        # INITIAL (DASS #DDMAMAC: DMA_MAX/DMA_OFFSET at
                        # +8/+9 value C6C6 -- allocated, never stored --
                        # and 'DMA_MAX' as the #CDMAMAC STH comments)
                        static = s.initial or s.constant \
                            or (s.name_var and not s.parm) \
                            or not (s.parm or s.auto or s.temporary)
                        if s.xrefs:
                            xref.setdefault(s.name, []).extend(s.xrefs)
                        if static:
                            scopes.setdefault(tgt, []).append(
                                (s.address - bias, s.name))
                        elif b.blk_class != 4:
                            stak.setdefault(b.csect_name, []).append(
                                (s.address - bias, s.name))
                        if grp is not None and (own_pool or static):
                            is_lbl = s.sclass == CLASS_LABEL
                            grp[2].append(
                                [s.address, s.name,
                                 None if is_lbl else s.kind, s.precision,
                                 s.width or 0, list(s.dims),
                                 (1 if s.name_var else 0)
                                 | (2 if s.constant else 0)
                                 | (8 if s.remote else 0)
                                 | (16 if is_lbl else 0),
                                 s.template])
                    blk_no += 1
                for lst in scopes.values():
                    lst.sort()
                for lst in stak.values():
                    lst.sort()
                for groups in data.values():
                    groups.sort(key=lambda g: g[0])
                    for g in groups:
                        g[2].sort()
                # structure templates: leaf fields in declaration order
                # (minor-structure group nodes descended; NAME-typed
                # structure members are 1-halfword pointer leaves)
                tmpl: dict[str, list] = {}
                for s in u._by_number.values():
                    if s.sclass == 4 and s.stype == 16 and s.is_template:
                        leaves: list = []
                        self._walk_leaves(u, s._eldest, leaves)
                        # the SDC brother chain is declaration order, but
                        # the compiler's alignment sort places members by
                        # address -- the DASS walks copies in ADDRESS
                        # order (CZ1V_D_MACT: ITEM_S@0 declared last,
                        # printed first)
                        leaves.sort(key=lambda l: l[0] or 0)
                        tmpl[s.name] = leaves
                # header attribute facts: DATA_REMOTE compile option
                # (root flag word bit 0x0004, DUMPSDF #DFLAG) and the
                # unit's REMOTE-keyword includes ('##STEM' members)
                try:
                    rinc = sorted({m[2:] for m, rem, _t in u.includes()
                                   if rem and m.startswith("##")})
                except Exception:
                    rinc = []
                ent = {"name": u.name, "kind": kind, "scopes": scopes,
                       "stak": stak, "litarea": u.literal_area(),
                       "ilit": u.initial_literal_area(),
                       "data": data, "tmpl": tmpl,
                       "xref": {n: sorted(set(v))
                                for n, v in xref.items()},
                       "blocks": blocks,
                       "dr": int(bool(u.root_flags() & 0x0004)),
                       "rinc": rinc,
                       "stmts": []}
                if u.root_flags() & 0x4000:         # ADDR_FLAG (ADDRS opt)
                    try:
                        ent["stmts"] = [
                            [n, cs, a1, cat, typ, srn, inc]
                            for n, cs, a1, _a2, cat, typ, srn, inc
                            in u.read_statements()]
                    except Exception:
                        pass
                self.map[stem] = ent
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"_version": _CACHE_VERSION, "units": self.map},
                      open(cache, "w"))

    @staticmethod
    def _walk_leaves(u, idx, out):
        """Declaration-order leaf fields of a structure template node:
        minor structures are descended, everything else (including
        NAME-typed structure pointers) is a leaf."""
        while idx:
            s = u._by_number.get(idx)
            if s is None:
                return
            if s.stype == 16 and not s.name_var and s._eldest:
                SdfNameMap._walk_leaves(u, s._eldest, out)
            else:
                out.append([s.address, s.name, s.kind, s.precision,
                            s.width or 0, list(s.dims),
                            (1 if s.name_var else 0)
                            | (8 if s.remote else 0), None])
            idx = s._brother

    def get(self, stem):
        e = self.map.get(stem)
        return (e["name"], e["kind"]) if e else None

    def scopes(self, stem):
        e = self.map.get(stem)
        return e.get("scopes", {}) if e else {}

    def litarea(self, stem):
        e = self.map.get(stem)
        return e.get("litarea") if e else None

    def ilit(self, stem):
        """(start, end) inclusive hw extent of the #D's leading adcon/
        literal pool, or None (see SdfUnit.initial_literal_area)."""
        e = self.map.get(stem)
        v = e.get("ilit") if e else None
        return tuple(v) if v else None

    def stak(self, stem):
        e = self.map.get(stem)
        return e.get("stak", {}) if e else {}

    def data(self, stem):
        e = self.map.get(stem)
        return e.get("data", {}) if e else {}

    def tmpl(self, stem):
        e = self.map.get(stem)
        return e.get("tmpl", {}) if e else {}

    def stmts(self, stem):
        e = self.map.get(stem)
        return e.get("stmts", []) if e else []

    def xref(self, stem):
        e = self.map.get(stem)
        return e.get("xref", {}) if e else {}

    def blocks(self, stem):
        e = self.map.get(stem)
        return e.get("blocks", {}) if e else {}

    def data_remote(self, stem):
        """True when the unit compiled with the DATA REMOTE option."""
        e = self.map.get(stem)
        return bool(e.get("dr")) if e else False

    def remote_included(self):
        """Stems of compools some unit INCLUDEd with the REMOTE keyword."""
        out = set()
        for e in self.map.values():
            out.update(e.get("rinc", ()))
        return out
