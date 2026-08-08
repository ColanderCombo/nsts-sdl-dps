"""HAL code-csect rendering: ST# statement lines, AP-101S disassembly rows,
branch-alias and comment-column synthesis."""
from .common import (_ASM_CATS, _CC_CMP, _CC_KEEP, _CC_TEST,
                     _NONHAL_CATS, COL_COMMENT, COL_MNEM, COL_NOTE,
                     alias_for)
from .decode import FP_MNEMONICS


class CodeRender:
    def _st_line(self, c, off, label, kind, srn, flag="", inc=0):
        line = f" {c.start + off:06X}         {c.name:<8}+{off:04X}"
        line = line.ljust(49) + flag
        line = line.ljust(56) + label
        line = line.ljust(COL_MNEM) + "EQU    *"
        if kind:
            line = line.ljust(COL_NOTE) + kind
            line = line.ljust(121) + srn
            if inc:
                # statements from an expanded (%COPY/REPLACE) source
                # line carry the SRN include-count, right-aligned wd 5
                line += f"{inc:5d}"
        return line.rstrip()

    # branches whose in-csect targets get (F )/( B) flags and, when no
    # statement starts there, a compiler #@LBn label line
    _FLAG_BRANCHES = ("BCF", "BCB", "BCTB", "BC", "BCT", "BIX", "BVC",
                      "BVCF")

    def _branch_targets(self, c):
        """offset -> direction flags for branch targets inside ``c``."""
        targets: dict[int, set] = {}
        words = self.img.words
        bases = {1: self.data_base.get(c.name[2:]), 2: None}
        a, end = c.start, c.start + c.size
        while a < end:
            hw2 = words.get(a + 1, 0) if a + 1 < end else 0
            inst = self.dis.decode(a, words.get(a, 0), hw2, bases)
            if inst is None:
                a += 1
                continue
            if inst.name in self._FLAG_BRANCHES and inst.ea is not None \
                    and c.start <= inst.ea < end:
                targets.setdefault(inst.ea - c.start, set()).add(
                    "F" if inst.ea > a else "B")
            a += inst.nhw
        return targets

    @staticmethod
    def _flag_text(dirs):
        return f"({'F' if 'F' in dirs else ' '}{'B' if 'B' in dirs else ' '})"

    def _flush_stmts(self, c, out, stlist, si, upto, targets, lbl_no):
        """Emit ST# lines for statements at offsets <= upto; flagged
        offsets flag their LAST statement line, and flagged offsets with
        no statement get a compiler #@LBn label."""
        while si < len(stlist) and stlist[si][0] <= upto:
            off = stlist[si][0]
            group = []
            while si < len(stlist) and stlist[si][0] == off:
                group.append(stlist[si])
                si += 1
            dirs = targets.pop(off, None)
            for j, (o, n, kind, srn, inc) in enumerate(group):
                flag = (self._flag_text(dirs)
                        if dirs and j == len(group) - 1 else "")
                out.append(self._st_line(c, o, f"ST#{n}", kind, srn, flag,
                                         inc))
        if upto in targets:                 # branch target, no statement
            dirs = targets.pop(upto)
            lbl_no += 1
            out.append(self._st_line(c, upto, f"#@LB{lbl_no}", "", "",
                                     self._flag_text(dirs)))
        return si, lbl_no

    def _lit_pool(self, ea):
        """True when ``ea`` lands in a #D csect's adcon or literal pool."""
        loc = self.img.locate(ea)
        if not loc.csect or loc.csect[:2] != "#D":
            return False
        bd = self.blockdata.get(loc.csect)
        lit = self.lit_area.get(loc.csect)
        if bd is None and lit is None:
            return False
        return (bd is not None and loc.offset < bd) or (
            lit is not None and loc.offset >= lit)

    _CONT_PAD = " " * COL_NOTE

    def _branch_comment(self, c, inst, cc, stmt_label, lbl_map):
        """Alias mnemonic + ST#/#@LB target label at the comment column."""
        if inst.ea is None:
            return ""
        off = inst.ea - c.start
        if not (0 <= off < c.size):
            return ""
        label = stmt_label.get(off) \
            or (f"#@LB{lbl_map[off]}" if off in lbl_map else "")
        if not label:
            return ""
        if inst.name in ("BCT", "BCTB", "BIX"):  # count/index: label only
            return label
        if cc == "UNDEF":
            # no CC-setting instruction yet (compiler prologue): MAFGEN
            # prints the bare target label without an alias
            return label
        alias = alias_for(cc, inst.fields.get("x"))
        return f"{alias:<8}{label}" if alias else ""

    def _code_rows(self, c):
        out = []
        words = self.img.words
        dbase = self.data_base.get(c.name[2:])
        stlist = self.stmts_by_csect.get(c.name, [])
        hal_code = c.cat in ("PROGRAM", "COMSUB", "INTERNAL")
        # flags + synthesized #@LB labels are a HAL flow annotation only:
        # asm/library csects carry real labels (SYM/asmg), never marks
        targets = self._branch_targets(c) if hal_code else {}
        target_offs = set(targets)
        # precompute the emission's #@LB numbering and per-offset last-ST#
        # so branch comments can name their targets
        stmt_offs = {s[0] for s in stlist}
        stmt_label = {}
        for off, n, _k, _s, _i in stlist:
            stmt_label[off] = f"ST#{n}"       # last one at the offset wins
        lbl_map = {}
        for off in sorted(target_offs - stmt_offs):
            lbl_map[off] = len(lbl_map) + 1
        track: dict[int, int] = {}            # value-tracked registers
        # regs whose value came from a data variable (memory snapshot)
        # rather than a literal/adcon constant: refs through them render
        # in the ' ?  csect+off' form (DASS: LH R2,X'326D' variable load
        #
        # > next ref ' ?  #PCDWDOW+384')
        #
        uncertain: set[int] = set()
        # regs whose tracked value passed through index arithmetic
        # (LH/SLL chains): MAFGEN evaluates those and uses them as
        # indexed-EA offsets even though the source was a variable
        # (DASS A2ASLTMC+022C: LH R7 from CAAV_MACT_I, SLL R7,5 ->
        # LH R6,X'001B'(R7,R2) resolves CZ1V_D_MACT+59)
        computed: set[int] = set()
        # regs holding a Y-con to a NONHAL csect: refs through them
        # comment the base's RLD target as name+addend (4-hex),
        # whatever the displacement (DASS: 101x 'FCMBMTPG+0000' at
        # many different EAs)
        base_note: dict[int, str] = {}
        cc = "UNDEF"          # 'UNDEF' | 'CMP' | 'TEST' | None=arith
        si = 0
        lbl_no = 0
        a, end = c.start, c.start + c.size
        while a < end:
            off = a - c.start
            si, lbl_no = self._flush_stmts(c, out, stlist, si, off,
                                           targets, lbl_no)
            cur_stmt = stlist[si - 1][1] if si else None
            cur_kind = stlist[si - 1][2] if si else None
            # tracked values deliberately survive control-flow joins:
            # MAFGEN's tracking is last-load and straight-line, not
            # flow-sensitive
            bases = {1: dbase, **track}
            hw1 = words.get(a, 0)
            hw2 = words.get(a + 1, 0) if a + 1 < end else 0
            inst = self.dis.decode(a, hw1, hw2, bases)
            if inst is None:
                # undecodable loaded halfword in a code region: '?'
                # mnemonic + the flight simulator's ILLEGAL OP CODE
                # diagnostic; un-loaded holes stay bare (flight never
                # walked them)
                if a in self.img.occupied:
                    out.append(self._row(c, a, [hw1]).ljust(COL_MNEM) + "?")
                    out.extend(self.esim.illegal(a))
                else:
                    out.append(self._row(c, a, [hw1]))
                a += 1
                continue
            la_reloc = False
            scal_name = None
            if inst.name == "LHI" and a + 1 in self.reloc_sites:
                # relocated immediate = address constant: same encoding
                # as LA extended/no-base, and that is how MAFGEN shows it.
                # The EA is the RLD target's full 19-bit address (operand
                # text keeps the raw stored halfword)
                x = inst.fields.get("x")
                inst.name = inst.mnemonic = "LA"
                inst.operands = f"R{x},X'{hw2:04X}'"
                inst.ea = self.reloc_target.get(a + 1, hw2)
                inst.imm = None
                la_reloc = True
            if inst.idx_info and inst.ea is None:
                # indirect (@/@#) refs through a ZCON or PSA transfer
                # vector: the pointer sits at the PEA (base+D2; B2=11
                # means D2 alone -- the index is applied after the
                # fetch), and the fetched halfword expands in the
                # referencing sector.  SCAL@#/BAL@# (DASS $0DMPMMM:
                # BAL@# R4,X'01EC'(R1,R3) -> 04704F CAS) and data refs
                # alike (DASS $0AIBGPC: LH@# R4,X'0052'(R5,R1) ->
                # 01CCF4 FCMMGPT+6 through the #D adcon's RLD)
                _ix, ia, _ii, disp = inst.idx_info
                b = inst.fields.get("b")
                base = 0 if b == 3 else bases.get(b)
                if ia and base is not None:
                    if base > 0xFFFF:
                        pa = base + disp
                    else:
                        if base > 0x7FFF:
                            base = (base & 0x7FFF) | 0x8000
                        pa = (base + disp) & 0xFFFF
                    if pa in self.reloc_target:
                        # the ZCON halfword is relocated: the RLD knows
                        # the callee's full address (DASS: 018030 #CFIOCGR
                        # from a caller in another sector)
                        inst.ea = self.reloc_target[pa]
                        if inst.name in ("SCAL", "BAL"):
                            scal_name = self.reloc_name.get(pa)
                    else:
                        ptr = words.get(pa)
                        if ptr is not None:
                            sec = words.get(pa + 1) if _ii else None
                            if ptr & 0x8000 and sec is not None \
                                    and sec < 16:
                                # '#' indirection through a full ZCON
                                # pair: the sector lives in the second
                                # halfword, not the referencing sector
                                # (DASS #CDXRDMM+003D: '8211 0005' at
                                # #DDXRDMM+0014 -> 028211)
                                inst.ea = (sec << 15) | (ptr & 0x7FFF)
                            else:
                                inst.ea = (((a >> 15) << 15)
                                           | (ptr & 0x7FFF)
                                           if ptr & 0x8000 else ptr)
                    if inst.ea is not None \
                            and inst.name not in ("SCAL", "BAL") \
                            and _ix in track \
                            and (_ix not in uncertain or _ix in computed):
                        # data @# refs: the tracked index adds to the
                        # fetched pointer (DASS $0AIBGPC: R5=6 ->
                        # 01CCF4 = ptr+6); for SCAL/BAL the (R1,R3)
                        # index is linkage convention, never added
                        inst.ea += track[_ix]
                        inst.idx_info = None
            elif inst.idx_info and inst.ea is not None \
                    and inst.idx_info[1] == 0 \
                    and inst.idx_info[0] in track \
                    and (inst.idx_info[0] not in uncertain
                         or inst.idx_info[0] in computed):
                # literal-tracked index register: the true EA is PEA +
                # index (DASS: ZH X'0085'(R7,R2) with R7 = =F'65541'
                # pool load -> 0030E4 CDWV_DWNLST_AREA+134); resolves
                # as a plain reference.  Variable-loaded (dump) values
                # over-fire -- literal-certain only -- except when the
                # value passed through index arithmetic (LH/SLL chain,
                # DASS A2ASLTMC+022C -> CZ1V_D_MACT+59): MAFGEN
                # evaluates those.
                inst.ea += track[inst.idx_info[0]]
                inst.idx_info = None
            elif inst.idx_info and inst.ea is not None \
                    and self._lit_pool(inst.ea):
                # indexed into a #D adcon/literal pool: 1-based element,
                # EA = PEA+1 with the LITERAL value there (DASS: LH
                # R6,X'0068'(R5,R1) -> 03827D LITERAL: =H'-14964')
                inst.ea += 1
                inst.idx_info = None
            elif inst.idx_info and inst.ea is not None:
                # data-indexed: the true EA is at least one 1-based array
                # element past the PEA.  When the PEA lands exactly on a
                # (biased) symbol base the reference is that symbol's
                # first element: EA = PEA+1, plain 'NAME+1' comment;
                # otherwise keep the PEA and show both straddling
                # candidates ('A ?  B ?', handled in _comment)
                loc = self.img.locate(inst.ea)
                scope = self.scopes.get(c.name[2:], {}).get(loc.csect) \
                    if loc.csect else None
                if scope:
                    addrs, _names = scope
                    j = self._bisect.bisect_right(addrs, loc.offset) - 1
                    # arrayed structures sit unbiased in the scope list;
                    # their addr-1 biased base counts as the same hit
                    # (DASS $0DGMWRT -> bare CDUV_TWO_STAGE_BUFF) -- but
                    # not for LA: indexed address arithmetic keeps the
                    # raw PEA (DASS #CDSPSPC: LA -> ' ?  #PCDULNK+2')
                    if (j >= 0 and addrs[j] == loc.offset) \
                            or (inst.name != "LA"
                                and loc.offset in self.struct_bias.get(
                                    loc.csect, ())):
                        inst.ea += 1
                        inst.idx_info = None    # renders as a plain ref
            n = inst.nhw
            line = self._inst_prefix(c, a, inst, hw1, hw2)
            if hal_code and inst.name in ("BC", "BCF", "BCB", "BCT",
                                          "BCTB", "BIX"):
                comment = self._branch_comment(c, inst, cc, stmt_label,
                                               lbl_map)
            elif hal_code and inst.name in ("BCR", "BCRE") \
                    and inst.fields.get("x") == 7:
                comment = "BR"          # unconditional register branch
            elif scal_name is not None:
                comment = scal_name
            elif inst.name == "BAL" and inst.ea is not None:
                # direct BAL: the callee's entry symbol or csect (DASS:
                # BAL R4,X'93EE' -> A7DMPMMM)
                bloc = self.img.locate(inst.ea)
                if bloc.symbol and bloc.symbol_offset == 0:
                    comment = bloc.symbol
                elif bloc.csect and bloc.offset == 0:
                    comment = bloc.csect
                else:
                    comment = ""
            elif inst.ea is not None and inst.idx_info is None \
                    and not inst.stack_ref \
                    and inst.fields.get("b") in base_note:
                # ref through a NONHAL-target base: the base's RLD
                # identity, displacement notwithstanding (DASS:
                # 'FCMBMTPG+0000' at ABB8/ABC4/ABDC/AC2C alike)
                comment = base_note[inst.fields.get("b")]
            else:
                # the XREF citation check inside _comment (via cur_stmt)
                # is the named-vs-'?' selector.  Marking every
                # variable-base ref '?' without consulting it over-fires:
                # plenty of variable-based refs do resolve by name.
                comment = self._comment(inst, c, cur_stmt, cur_kind)
            if comment:
                line = line.ljust(COL_COMMENT) + comment
            out.append(line.rstrip())
            # flight error simulation: EA validity + register semantics
            out.extend(self.esim.step(a, inst, hw1, hw2))
            #
            # continuation annotations
            #
            if inst.name == "SVC" and inst.ea is not None \
                    and inst.ea in words:
                out.append(self._CONT_PAD
                           + f"***   SVC CODE: {words[inst.ea] & 0xFF}")
            #
            # register value tracking + CC context
            #
            x = inst.fields.get("x")
            if inst.name in ("LH", "L") and inst.ea is not None \
                    and not inst.stack_ref and x is not None \
                    and self._lit_pool(inst.ea):
                val = words.get(inst.ea)
                if val is not None:
                    track[x] = val
                    uncertain.discard(x)
                    # the literal is a data pointer: locate raw (DASS:
                    # =H'-25486'/=X'9C72' -> =Y(#PFCMCOM+0000), sector 0).
                    # The continuation prints only for the compool base
                    # registers R2/R3 (DASS: 840 LH R2 + 126 LH R3, none
                    # for R5-R7 value loads)
                    loc = self.img.locate(val)
                    if x in (2, 3) and loc.csect \
                            and loc.csect[:2] == "#P":
                        out.append(
                            self._CONT_PAD + "***   COMPOOL BASEREG: "
                            f"=Y({loc.csect}+{loc.offset:04X})")
                    # a Y-con to a NONHAL csect: refs through this base
                    # comment the base's csect + a LITERAL '+0000' --
                    # the target sits at FCMBMTPG+970 yet DASS prints
                    # 'FCMBMTPG+0000' at every displacement
                    base_note.pop(x, None)
                    if inst.ea in self.reloc_target:
                        tl = self.img.locate(self.reloc_target[inst.ea])
                        if tl.csect and self.cat_by_name.get(
                                tl.csect) in _NONHAL_CATS:
                            base_note[x] = f"{tl.csect}+0000"
            elif inst.name in ("LH", "L") and inst.ea is not None \
                    and not inst.stack_ref and x is not None:
                # variable load: MAFGEN reads the dump, so the value
                # tracks -- flagged uncertain
                val = words.get(inst.ea)
                if val is not None:
                    track[x] = val
                    uncertain.add(x)
                else:
                    track.pop(x, None)
                    uncertain.discard(x)
                computed.discard(x)
                base_note.pop(x, None)
            elif la_reloc and x is not None:
                track[x] = inst.ea            # address-constant load
                uncertain.discard(x)
                computed.discard(x)
                base_note.pop(x, None)
            elif inst.name == "LA" and inst.ea is not None \
                    and not inst.stack_ref and x is not None:
                # address arithmetic: LA Rx,d(Rb) tracks the EA (DASS:
                # LA R2,X'0058'(R1) then NIST 0(R2) resolves to
                # CDIV_DATA_WRT+1 at 00A586)
                track[x] = inst.ea
                uncertain.discard(x)
                computed.discard(x)
                base_note.pop(x, None)
            elif inst.name == "LR" and x is not None:
                # register copy carries the tracked value + certainty
                # (DASS $0AIBGPC: LR R6,R7 then AHI R6,3 -> R6 = 4,
                # added to the LH@# fetched pointer)
                yy = inst.fields.get("y")
                if yy is not None and yy in track:
                    track[x] = track[yy]
                    (uncertain.add if yy in uncertain
                     else uncertain.discard)(x)
                    (computed.add if yy in computed
                     else computed.discard)(x)
                else:
                    track.pop(x, None)
                    uncertain.discard(x)
                    computed.discard(x)
                base_note.pop(x, None)
            elif inst.name in ("AHI", "SHI") and inst.imm is not None:
                # immediate arithmetic on a tracked value (the AHI-neg
                # display rename already made SHI's imm positive)
                r = inst.fields.get("y")
                r = x if r is None else r
                if r is not None:
                    if r in track:
                        track[r] += (inst.imm if inst.name == "AHI"
                                     else -inst.imm)
                        computed.add(r)
                    else:
                        uncertain.discard(r)
                        computed.discard(r)
                    base_note.pop(r, None)
            elif inst.name == "LHI" and inst.imm is not None:
                # literal immediate: tracks certain (DASS $0AIBGPC:
                # LHI R5,6 then LH@# ... -> fetched ptr + 6)
                r = inst.fields.get("y")
                r = x if r is None else r
                if r is not None:
                    if r == 1 and hal_code and dbase is not None \
                            and inst.imm != (dbase & 0xFFFF):
                        # an un-relocated #D prologue adcon (the
                        # E9F3-immediate reloc-gap class: our image
                        # holds 'LHI R1,X'3A96'' where flight holds
                        # the relocated 'LA R1,X'4CE4' #DDXRDMM') --
                        # the #D pin stays authoritative, matching
                        # flight's every (R1) ref in the block
                        track.pop(r, None)
                    else:
                        track[r] = inst.imm
                    uncertain.discard(r)
                    computed.discard(r)
                    base_note.pop(r, None)
            elif inst.name in ("SLL", "SLA", "SRL", "SRA") \
                    and x is not None and x in track:
                # index arithmetic on a tracked value (MAFGEN's LH/SLL
                # chains): immediate-count shifts transform the value;
                # register-count shifts (d >= 0x38) are unknowable
                d = inst.fields.get("d")
                if d is None or d >= 0x38:
                    track.pop(x, None)
                    uncertain.discard(x)
                    computed.discard(x)
                else:
                    track[x] = (track[x] << d if inst.name in ("SLL", "SLA")
                                else track[x] >> d)
                    computed.add(x)
            elif x is not None and inst.name not in FP_MNEMONICS \
                    and inst.name not in _CC_KEEP \
                    and inst.name not in _CC_CMP \
                    and inst.name not in _CC_TEST:
                track.pop(x, None)            # register overwritten
                uncertain.discard(x)
                computed.discard(x)
                base_note.pop(x, None)
            if inst.name in ("SCAL", "BAL", "BALR"):
                # call boundary: scratch registers clobber, but the
                # certain (adcon/LA-derived) compool/data bases R2/R3
                # survive -- callees save and restore them (DASS
                # A1ASLTMC: LA R3 at +0003 still resolves 6(R3) as
                # ASL_TEMP_T at +006D, past a SCAL and a BAL).
                # R1 must not be kept: a stale track[1] then shadows the
                # block's #D default.  Uncertain (memory-loaded) values
                # must not be kept either -- only the two certain bases.
                # SVC is no boundary at all, since the FCOS preserves
                # user registers (A2ASLTMC+0226).
                keep = {r: v for r, v in track.items()
                        if r in (2, 3) and r not in uncertain}
                track.clear()
                track.update(keep)
                uncertain.clear()
                computed.clear()
                base_note.clear()
            if inst.name in _CC_CMP:
                cc = "CMP"
            elif inst.name in _CC_TEST:
                cc = "TEST"
            elif inst.name not in _CC_KEEP:
                cc = None
            a += n
        return out

    @staticmethod
    def _sym_off(name, off):
        return name + (f"+{off}" if off else "")

    def _extent_ok(self, csect, scope, offset):
        """True when ``offset`` falls inside the storage extent of its
        nearest-below scope symbol (or extents are unknown)."""
        exts = self.sym_extent.get(csect)
        if not exts:
            return True
        addrs, _names = scope
        j = self._bisect.bisect_right(addrs, offset) - 1
        if j < 0:
            return True
        ext = exts.get(addrs[j])
        if ext is None:
            return True
        return offset - addrs[j] < ext

    def _scope_lookup(self, scope, offset, strides=None):
        """(text, found, name) nearest-below within one resolution scope.
        A hit inside a multi-copy STRUCTURE prints its offset
        stride-shifted (copies are 1-based: hw offset k ->
        NAME+(k+stride))."""
        addrs, names = scope
        j = self._bisect.bisect_right(addrs, offset) - 1
        if j < 0:
            return "", False, None
        off = offset - addrs[j]
        if strides:
            st = strides.get(addrs[j])
            if st and off < st[1]:
                off += st[0]
        return self._sym_off(names[j], off), True, names[j]

    def _comment(self, inst, c=None, stmt=None, stmt_kind=None):
        if inst.stack_ref:
            # R0-relative = HAL/S stack frame; the fixed prologue slots
            # that save the caller's bases are annotated, other frame
            # offsets resolve against the BLOCK's own stack temporaries.
            # Loads/stores print the name plainly; arithmetic operand
            # references print the doubled 'NAME ?  NAME ?' form.
            # NONHAL csects get none of it (DASS $0DCICYC: STH R1,5(R0)
            # prints bare -- no SAVE REG annotation)
            if c is not None and c.cat in _ASM_CATS:
                return ""
            f = inst.fields
            if inst.name == "STH" and (f.get("x"), f.get("d")) in (
                    (1, 5), (3, 9)):
                return f"*** SAVE REG {f['x']} ***"
            if c is not None and inst.ea is not None:
                stak = self.stack_syms.get(c.name)
                if stak:
                    text, ok, _n = self._scope_lookup(stak, inst.ea)
                    if ok:
                        if inst.name[0] in "LZ" or inst.name[:2] == "ST":
                            return text
                        return f"{text} ?  {text} ?"
            return ""
        if inst.name in ("BC", "BCF", "BCB", "BCT", "BCTB", "BAL", "BIX"):
            # branch targets are labeled by _branch_comment for HAL code;
            # asm csects keep their comments off until SYM-driven rendering
            return ""
        if inst.ea is not None:
            loc = self.img.locate(inst.ea)
            if loc.csect and c is not None:
                stem = c.name[2:]
                scope = self.scopes.get(stem, {}).get(loc.csect)
                own_d = loc.csect == "#D" + stem
                indexed = inst.idx_info is not None
                if own_d and not indexed:
                    txt = self._own_d_comment(inst, loc)
                    if txt is not None:
                        return txt
                if scope:
                    strides = self.struct_stride.get(loc.csect)
                    if not indexed:
                        # XREF citation resolution: among the symbols
                        # whose storage covers the offset (overlapping
                        # declarations walk back from nearest-below),
                        # the one the enclosing statement cites wins
                        # (DASS: 'CAAB_ITEM_FLAG+9' at ST#356 over the
                        # exact-hit uncited CAAV_LEAST_ITEM; uncited ->
                        # the '?' form, e.g. ' ?  #PCDULNK+116' at
                        # ST#124).  Without XREF data (or before the
                        # first ST#) the extent rule alone decides.
                        addrs, names = scope
                        exts = self.sym_extent.get(loc.csect, {})
                        xr = self.xref_by_stem.get(c.name[2:]) \
                            if stmt is not None else None
                        j = self._bisect.bisect_right(
                            addrs, loc.offset) - 1
                        pick = None
                        if j >= 0 and xr is not None:
                            for k in range(j, max(-1, j - 16), -1):
                                off = loc.offset - addrs[k]
                                ext = exts.get(addrs[k])
                                covers = (off == 0
                                          or (ext is not None and off < ext)
                                          or (ext is None and k == j))
                                if covers and stmt in xr.get(names[k], ()):
                                    pick = k
                                    break
                        elif j >= 0 and self._extent_ok(
                                loc.csect, scope, loc.offset):
                            pick = j
                        if pick is not None \
                                and names[pick] in self.degrade:
                            # --degrade: flight's SDF xref lacked the
                            # citation here; print the '?' form for
                            # diff parity
                            pick = None
                        if pick is not None:
                            off = loc.offset - addrs[pick]
                            if strides:
                                st = strides.get(addrs[pick])
                                if st and off < st[1]:
                                    off += st[0]
                            return self._sym_off(names[pick], off)
                    else:
                        # indexed: the true EA is >= PEA by at least one
                        # 1-based array element -- MAFGEN shows both
                        # straddling candidates, each marked uncertain
                        t1, ok1, _n1 = self._scope_lookup(
                            scope, loc.offset, strides)
                        t2, ok2, _n2 = self._scope_lookup(
                            scope, loc.offset + 1, strides)
                        if ok1 and ok2:
                            return f"{t1} ?  {t2} ?"
                if loc.csect[:2] in ("#D", "#P") \
                        and c.name[2:] in self.scopes:
                    # outside the referencing unit's resolution scope --
                    # a HAL-unit annotation only (asm csects print
                    # nothing); the form leads with a space (DASS col 97)
                    off = f"+{loc.offset}" if loc.offset else ""
                    return f" ?  {loc.csect}{off}"
            if loc.symbol:
                # NONHAL: manifest (SYM-card) symbols; several may share
                # the address -- exact hits list them comma-joined
                sect = self.img._sym_by_sect.get(loc.csect)
                if sect:
                    addrs, names = sect
                    j = self._bisect.bisect_right(addrs, inst.ea) - 1
                    if j >= 0 and addrs[j] == inst.ea:
                        k = j
                        while k > 0 and addrs[k - 1] == inst.ea:
                            k -= 1
                        if k < j:
                            # the csect's own name shares its base address
                            # with the first real symbol -- not listed.
                            # Co-located symbols list in source-line
                            # (definition) order of the defining module
                            # (DASS: 'TWCE0024,FIOCWE,TCWE0000' = lines
                            # 1993/2000/2001)
                            multi = [n for n in names[k:j + 1]
                                     if n != loc.csect]
                            info = self._module_lines(loc)
                            if c is not None and c.cat not in (
                                    "PROGRAM", "COMSUB", "INTERNAL"):
                                multi = [n for n in multi
                                         if self._asm_visible(info, n, 0)]
                            if multi:
                                if info:
                                    multi.sort(key=lambda n: info.get(
                                        n, (1 << 30,))[0])
                                return ",".join(multi)
                    if j >= 0 and addrs[j] == inst.ea \
                            and names[j] == loc.csect \
                            and c is not None and c.cat not in (
                                "PROGRAM", "COMSUB", "INTERNAL"):
                        # the manifest folds same-named locals across
                        # modules (each math routine defines its own
                        # AERROR1; one copy survives the symbol merge) --
                        # the defining module's sidecar still knows its
                        # label here (DASS #0ROUND: SVC X'9DEA' ->
                        # AERROR1 at #LROUND+0)
                        from types import SimpleNamespace
                        tinfo = self._asmg_for(SimpleNamespace(
                            module=loc.module, phase=loc.phase,
                            name=loc.csect))
                        if tinfo:
                            minfo = self._module_lines(loc)
                            vis = [n for n in tinfo["labels"].get(
                                       loc.offset, [])
                                   if self._asm_visible(minfo, n, 0)]
                            if vis:
                                return ",".join(vis)
                if loc.symbol == loc.csect and loc.symbol_offset == 0 \
                        and self.cat_by_name.get(
                            loc.csect) in _NONHAL_CATS:
                    # only the csect's own section symbol anchors the
                    # address: NONHAL prints the dump-label form
                    # (DASS: LDM X'0520' -> '$ZDSESET+0000'; LA sites
                    # too -- 'LA R1,X'4B48'' -> 'FCMG3DAT+0000' and 111
                    # more.  The two bare FCMG3INT LAs at +001F/+002D
                    # that once justified an LA exception are a flight
                    # patch artifact: FCMG3INT is patched in the flight
                    # image, and the patched halfwords lost their RLD
                    # identity)
                    return f"{loc.csect}+0000"
                off = loc.symbol_offset
                if c is not None and c.cat not in ("PROGRAM", "COMSUB",
                                                   "INTERNAL"):
                    # asm callers: visibility-gated (see _asm_visible);
                    # symbol+offset renders in the dump-label geometry,
                    # name padded to 8 (DASS: 'FP$EVAL +2', 'TCVTMSC +1';
                    # HAL callers print 'RTCIO+2' unpadded).  When
                    # co-located symbols tie, the one whose extent
                    # covers the offset wins (DASS: 'TPSARES1+1', not
                    # the 0-hw TPSASTRT sharing its address)
                    name = loc.symbol
                    info = self._module_lines(loc)
                    if not self._asm_visible(info, name, off) and sect:
                        addrs, names = sect
                        j = self._bisect.bisect_right(addrs, inst.ea) - 1
                        base = addrs[j] if j >= 0 else None
                        k = j
                        while k >= 0 and addrs[k] == base:
                            if self._asm_visible(info, names[k], off):
                                name = names[k]
                                break
                            k -= 1
                    if not self._asm_visible(info, name, off):
                        # an exact module-LOCAL label at the EA still
                        # wins (DASS LOG: LE -> ROUND past the entry
                        # symbol's extent)
                        vis = self._local_label(loc)
                        if vis:
                            return vis
                        # nothing visible covers the offset: a foreign
                        # NONHAL csect's dump-label identity, before the
                        # =Y'' marker and LA included -- displacement
                        # notwithstanding (DASS FPMSCHED 'LH R4,5(R2)'
                        # EA 000005 / 'LH R4,X'005D'' -> 'FCMPSA  +0000';
                        # FCMBUSCM+00BE 'LA R2,X'810C'' into FCMPROTD+10
                        # over the invisible LD FCMSTDF2 ->
                        # 'FCMPROTD+0000'; FCMDSCRM+0011 LA into
                        # FCMCBLKS' relocated hw -> 'FCMCBLKS+0000',
                        # not =Y'')
                        if loc.csect != c.name and self.cat_by_name.get(
                                loc.csect) in _NONHAL_CATS:
                            return f"{loc.csect:<8}+0000"
                        return self._asm_ycon(inst, c)
                    if off:
                        return f"{name:<8}+{off}"
                    return name
                return self._sym_off(loc.symbol, off)
            if loc.csect and loc.module and c is not None \
                    and c.cat not in ("PROGRAM", "COMSUB", "INTERNAL"):
                # no manifest symbol names the EA: a module-local DC/DS
                # label (no SYM card) may -- the defining module's
                # sidecar knows it, and it beats the pool-literal
                # rendering (DASS LOG: LE F3,X'0012' -> ROUND at +0042)
                vis = self._local_label(loc)
                if vis:
                    return vis
            if loc.csect and loc.offset == 0:
                # NONHAL csect base with no manifest symbol: MAFGEN
                # prints the dump-line label form, offset always shown
                # 4-hex (DASS: LDM X'0520' -> '$ZDSESET+0000'; LA
                # alike -- 'FCMPROTD+0000', 'FCMCBLKS+0000'); SDF-known
                # csects print bare (DASS: SCAL@# -> '#CFIOCGR')
                if self.cat_by_name.get(loc.csect) in _NONHAL_CATS:
                    return f"{loc.csect}+0000"
                return loc.csect
            # a foreign NONHAL csect's identity beats the bare =Y''
            # marker (DASS FCMDSCRM+0011: LA into FCMCBLKS' relocated
            # base hw -> 'FCMCBLKS+0000', not =Y''); own-csect pool
            # refs keep the ycon/literal renderings
            if c is not None and loc.csect != c.name \
                    and c.cat not in ("PROGRAM", "COMSUB", "INTERNAL") \
                    and self.cat_by_name.get(loc.csect) in _NONHAL_CATS:
                return f"{loc.csect:<8}+0000"
            return self._asm_ycon(inst, c)
        if inst.imm is not None and inst.name in ("CHI", "MHI", "AHI",
                                                  "SHI", "NHI", "XHI",
                                                  "OHI", "TRB", "ZRB"):
            v = inst.imm - 0x10000 if inst.imm & 0x8000 else inst.imm
            return str(v)
        return ""

    def _local_label(self, loc):
        """Visible module-local sidecar labels at exactly ``loc``, or
        None -- local DC/DS labels have no SYM cards, so the manifest
        can't name them (DASS LOG: ROUND at LOG+0042)."""
        if not (loc.csect and loc.module):
            return None
        from types import SimpleNamespace
        tinfo = self._asmg_for(SimpleNamespace(
            module=loc.module, phase=loc.phase, name=loc.csect))
        if not tinfo:
            return None
        minfo = self._module_lines(loc)
        vis = [n for n in tinfo["labels"].get(loc.offset, [])
               if self._asm_visible(minfo, n, 0)]
        return ",".join(vis) if vis else None

    def _asm_ycon(self, inst, c):
        """A data ref in an asm csect that no symbol names: a relocated
        halfword prints the bare address-constant marker =Y'' (DASS
        $0DCICYC/FIOERRLC/FPMREL -- flight MAFGEN read the load module,
        so the RLD name was gone); an in-csect (LTORG pool) reference
        prints the typed literal value -- =H/=F by operand width,
        =E/=D for floats with a sign slot (DASS: CH -> =H'1',
        ME -> =E' 1.1377778E+01')."""
        if c is None or c.cat not in _ASM_CATS:
            return ""
        if inst.ea in self.reloc_sites or inst.ea in self.unresolved_sites:
            return "=Y''"
        if inst.name == "SVC" \
                or not c.start <= inst.ea < c.start + c.size:
            return ""
        words = self.img.words
        # the assembler's literal record names the source type: an
        # =X'00007FFF' prints hex, not the width-guessed =F'32767'
        # (DASS #0ETOH); modules without the record keep the width rule
        info = self._asmg_for(c)
        lit = (info["lits"].get(inst.ea - c.start)
               if info and "lits" in info else None)
        if lit:
            t, nb = lit
            hws = [words.get(inst.ea + i, 0) for i in range((nb + 1) // 2)]
            if t in ("E", "D"):
                txt = self._scalar_text(hws)
                if not txt.startswith("-"):
                    txt = " " + txt
                return f"={t}'{txt}'"
            if t in ("H", "F"):
                return f"={t}'{self._int_text(hws)}'"
            if t == "X":
                bs = b"".join(hw.to_bytes(2, "big") for hw in hws)[:nb]
                return f"=X'{bs.hex().upper()}'"
        if inst.name in FP_MNEMONICS:
            return self._fp_lit(inst)
        n = 2 if inst.aw else 1
        return "={}'{}'".format("F" if inst.aw else "H", self._int_text(
            [words.get(inst.ea + i, 0) for i in range(n)]))

    def _fp_lit(self, inst):
        """=E'/=D' rendering of the FP literal at inst.ea: precision from
        the mnemonic's D suffix; sign slot kept; zero pads to the field
        width inside the quotes (DASS: \"=D' 0.0                   '\")."""
        words = self.img.words
        n, letter, wide = ((4, "D", 23) if inst.name.endswith("D")
                           else (2, "E", 14))
        txt = self._scalar_text([words.get(inst.ea + i, 0)
                                 for i in range(n)])
        if not txt.startswith("-"):
            txt = " " + txt
        return f"={letter}'{txt:<{wide}}'"

    def _own_d_comment(self, inst, loc):
        """Comments special to the unit's own #D: bare csect at offset 0,
        LITERAL values in the adcon/literal pools, the LOCAL BLOCK DATA
        base.  None = fall through to plain scope resolution."""
        bd = self.blockdata.get(loc.csect)
        if loc.offset == 0 and inst.name == "LA":
            # address-of the data block (the relocated base load); when
            # the LBD sits at offset 0 (no adcon pool) the annotation
            # stays (DASS: LA R1 -> '#DDMZLOG (LOCAL BLOCK DATA)')
            if bd == 0:
                return f"{loc.csect} (LOCAL BLOCK DATA)"
            return loc.csect
        lit = self.lit_area.get(loc.csect)
        if (bd is not None and loc.offset < bd) or (
                lit is not None and loc.offset >= lit):
            if inst.name == "LA":       # address-of a literal: no value
                return "LITERAL:"
            if inst.name in FP_MNEMONICS:
                # FP referent: the float form alone, no =X companion
                # (DASS: SED -> LITERAL: =D' 1.4999999999999997E-02')
                return f"LITERAL: {self._fp_lit(inst)}"
            w = self.img.words
            if inst.aw:
                v = (w.get(inst.ea, 0) << 16) | w.get(inst.ea + 1, 0)
                sv = v - (1 << 32) if v & 0x80000000 else v
                return f"LITERAL: =F'{sv}', =X'{v:08X}'"
            v = w.get(inst.ea, 0)
            sv = v - 0x10000 if v & 0x8000 else v
            return f"LITERAL: =H'{sv}', =X'{v:04X}'"
        if loc.offset in self.lbd_offsets.get(loc.csect, ()) \
                or (bd is not None and loc.offset == bd):
            if loc.offset == 0:
                return f"{loc.csect} (LOCAL BLOCK DATA)"
            return f"{loc.csect}+{loc.offset} (LOCAL BLOCK DATA)"
        return None
