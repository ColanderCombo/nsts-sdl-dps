"""NONHAL (asm) and patch csect rendering from asmg debug sidecars."""
import json

from .common import (_CC_CMP, _CC_KEEP, _CC_TEST, COL_COMMENT,
                     COL_MARK, COL_MNEM, COL_NOTE, LINE_WIDTH,
                     addr_range, alias_for)


# assembler/compiler alignment fill: asm C9FB, HAL C6C6, CNOP D800/C000
_FILL_HW = frozenset((0xC9FB, 0xC6C6, 0xD800, 0xC000))


class AsmRender:
    #
    # NONHAL (asm) csects
    #
    # MAFGEN replayed the assembler's TEST SYM stream: labels print as
    # 'name DS 0F/0H' or 'name EQU *' statement lines, DC data prints
    # typed ('DC Y', 'DC Z', 'DC 19F', 'DC XL2' -- the L is halfwords),
    # and everything uncovered disassembles.  We rebuild the stream from
    # asm101's .asmg.json sidecars (v2 'data' statement records +
    # symbols); column geometry is the instruction line's (label at 56,
    # mnemonic 65, operand 72), rendered values end at 89 (ints) or keep
    # a sign slot at 89 (floats), C9FB fill prints as ALIGNMENT GAP rows.

    def _module_lines(self, loc):
        """{symbol name: (source line, kind, extent hw)} for the module
        defining ``loc``, or None (no asm sidecar) -- orders co-located
        symbol comments by definition and gates comment visibility."""
        if self.mmu_root is None or not loc.module or not loc.phase:
            return None
        key = (loc.phase, loc.module)
        if key not in self._line_cache:
            lines = None
            p = self.mmu_root / loc.phase / "obj" \
                / (loc.module + ".asmg.json")
            if not p.exists():
                for d in (self.mmu_root.parent / "lib" / "runtime" / "RUN",
                          self.mmu_root.parent / "lib" / "runtime"
                          / "ZCON"):
                    q = d / (loc.module + ".asmg.json")
                    if q.exists():
                        p = q
                        break
            if p.exists():
                try:
                    d = json.load(open(p))
                    ext = {}
                    for _s, a, b, _form, _lt, _n, label in d.get("data",
                                                                 []):
                        if label:
                            ext[label] = max(ext.get(label, 0),
                                             (b - a + 1) // 2)
                    lines = {s["name"]: (s.get("line") or 0, s["kind"],
                                         ext.get(s["name"], 0))
                             for s in d.get("symbols", [])
                             if not s["name"].startswith(".")}
                except (OSError, ValueError):
                    lines = None
            self._line_cache[key] = lines
        return self._line_cache[key]

    @staticmethod
    def _asm_visible(info, name, off):
        """MAFGEN's asm-caller comment dictionary: EQU-defined symbols
        never resolve, instruction labels resolve exactly, DC/DS labels
        resolve inside their statement extent (DASS: 'FP$EVAL +2' inside
        DS 8F, but FIOWCE+2 past a 0-hw marker and every TFCM*/CZ*
        HAL-twin symbol print nothing)."""
        if info is None:
            return False
        ent = info.get(name)
        if ent is None:
            return False
        _line, kind, ext = ent
        if kind == "equ":
            return False
        if off == 0:
            return True
        return kind == "data" and off < ext

    def _asmg_for(self, c):
        """Per-csect asmg info {'data': [records], 'labels': {hw: [names]},
        'equs': {hw: [names]}} or None (no sidecar).  'labels' collects
        every location symbol (branch-comment material, several may share
        an address); DC/DS line labels come from the records themselves."""
        if self.mmu_root is None or not c.module or not c.phase:
            return None
        key = (c.phase, c.module)
        if key not in self._asmg_cache:
            info = None
            p = self.mmu_root / c.phase / "obj" / (c.module + ".asmg.json")
            if not p.exists():
                # runtime-library modules (RUNASM/ZCONASM) live beside the
                # linked libraries, not in a phase obj dir
                for d in (self.mmu_root.parent / "lib" / "runtime" / "RUN",
                          self.mmu_root.parent / "lib" / "runtime" / "ZCON"):
                    q = d / (c.module + ".asmg.json")
                    if q.exists():
                        p = q
                        break
            if p.exists():
                try:
                    d = json.load(open(p))
                except (OSError, ValueError):
                    d = None
                if d and d.get("version", 0) >= 2:
                    info = {}

                    def blank():
                        return {"data": [], "labels": {},
                                "equs": {}, "lits": {}}
                    for s, a, b, form, letter, count, label \
                            in d.get("data", []):
                        info.setdefault(s, blank())["data"].append(
                            [a, b, form, letter, count, label])
                    # literal-pool slots [sect, startB, endB, T, L]: a
                    # pool reference comments with the source type
                    # (=X'00007FFF', not width-guessed =F)
                    for s, a, b, t, l in d.get("literals", []):
                        if a % 2 == 0:
                            info.setdefault(s, blank())[
                                "lits"][a // 2] = (t, l)
                        # the pool slots are DATA in the replayed stream:
                        # MAFGEN prints them as typed DC rows (DASS
                        # #0ETOH: 'DC XL2  ^'), and the preceding C9FB
                        # pad becomes an ALIGNMENT GAP instead of being
                        # disassembled as SVC/code
                        info.setdefault(s, blank())["data"].append(
                            [a, b, "DC", t, 1, ""])
                    for sym in d.get("symbols", []):
                        sect, off = sym.get("section"), sym.get("offset")
                        if sect is None or off is None \
                                or sym["name"].startswith("."):
                            # macro-internal dot-labels never print --
                            # no EQU * rows, no branch-comment targets
                            # (DASS: zero dot-label references; CPASP
                            # BNZ prints bare, not '.PASS   +12')
                            continue
                        e = info.setdefault(sect, blank())
                        if sym.get("kind") == "equ":
                            # EQU-defined symbols never print -- neither
                            # as dump labels nor in branch comments
                            # (DASS $0DCICYC: 'DCI#CON' not
                            # 'START,DCI#CON'; BCTB nearest-label picks
                            # '#@LB29  +21' past the equ TDCIEP1)
                            continue
                        elif sym.get("kind") == "code":
                            # a label ON an instruction (CPU or IOP)
                            # prints as 'name EQU *' (DASS: FCMINCVT,
                            # FIOBYRR5)
                            e["equs"].setdefault(off, []).append(
                                (sym.get("line") or 0, sym["name"]))
                        e["labels"].setdefault(off, []).append(
                            (sym.get("line") or 0, sym["name"]))
                    for e in info.values():
                        # branch comments list co-located labels in
                        # source-line (definition) order (DASS FIOERRLC
                        # '#@LB24,#@LB23,#@LB19' = lines 15370/15518/
                        # 15666; FIOPDISP '#@LB3,#@LB1' = 3620/3768)
                        e["labels"] = {
                            off: [n for _l, n in sorted(v)]
                            for off, v in e["labels"].items()}
            self._asmg_cache[key] = info
        info = self._asmg_cache[key]
        return info.get(c.name) if info else None

    def _dc_line(self, c, off, nhw, label, mnem, opnd, hexs=None,
                 rint=None, rflt=None):
        a = c.start + off
        rng = addr_range(a, nhw)
        line = f" {rng}  {c.name:<8}+{off:04X}"
        if hexs:
            line = line.ljust(COL_MARK) + " ".join(
                self._hx(c.start + off + j, v) for j, v in enumerate(hexs))
        if label:
            line = line.ljust(56) + label
        line = line.ljust(COL_MNEM) + mnem
        line = line.ljust(72) + opnd
        if rint is not None:
            line = line.ljust(COL_NOTE) + rint
        elif rflt is not None:
            # '-' and '^' occupy the sign slot (col 89); digits start at 90
            line = line.ljust(89 if rflt.startswith(("-", "^")) else 90) + rflt
        return line.rstrip()

    # element-row/rendered-value mapping per DC letter
    _DC_ELEM = {"H": ("INTEGER", None), "F": ("INTEGER", "DOUBLE"),
                "E": ("SCALAR", None), "D": ("SCALAR", "DOUBLE")}

    def _dc_float(self, hexs):
        """MAFGEN's DC float rendering: '^' when normalization would
        underflow the IBM characteristic -- more leading-zero fraction
        digits than the exponent can absorb (DASS: 0000FFFF prints '^'
        but 00FF00FF prints 8.6025647E-78) -- else the truncating
        scientific form."""
        iv = 0
        for hw in hexs:
            iv = (iv << 16) | hw
        bits = 16 * len(hexs)
        frac = iv & ((1 << (bits - 8)) - 1)
        if frac:
            char = (iv >> (bits - 8)) & 0x7F
            lead = (bits - 8) // 4 - len(f"{frac:X}")
            if char < lead:
                return "^"
        return self._scalar_text(hexs)

    def _asm_datum(self, c, out, rec):
        startB, endB, form, letter, count, label = rec
        off = startB // 2
        hw_end = (endB + 1) // 2
        nhw = max(1, hw_end - off)
        if count == 0:                          # 'DS 0F' / 'DS 0H' marker
            if label:                           # unnamed markers don't print
                out.append(self._dc_line(c, off, 1, label, "DS",
                                         "0" + letter))
            return
        elem_b = max(1, (endB - startB) // count)
        elem_hw = max(1, (elem_b + 1) // 2)
        if letter == "A":
            # DC A and DC Z assemble to the same fullword ZCON format on
            # the AP-101S; MAFGEN's vocabulary is uniformly Z (the SSW
            # DASS has 198 'DC Z' rows and not one 'DC A')
            letter = "Z"
        opnd = (str(count) if count != 1 else "") + letter
        if letter in ("X", "C", "B"):
            opnd += f"L{elem_hw}"
        words = self.img.words
        a0 = c.start + off
        wide = count > 1 or nhw > 3
        if not wide:
            hexs = [words.get(a0 + j, 0) for j in range(nhw)]
            rint = rflt = None
            iv = 0
            for hw in hexs:
                iv = (iv << 16) | hw
            if letter == "H" or (letter == "X" and nhw == 1):
                rint = str(iv - 0x10000 if iv & 0x8000 else iv)
            elif letter == "F" and nhw == 2:
                rint = str(iv - (1 << 32) if iv & 0x80000000 else iv)
            elif letter in ("E", "D") or (letter == "X" and nhw == 2):
                rflt = self._dc_float(hexs)
            out.append(self._dc_line(c, off, nhw, label, form, opnd,
                                     hexs=hexs, rint=rint, rflt=rflt))
            return
        out.append(self._dc_line(c, off, nhw, label, form, opnd))
        if self._nonzero(a0, nhw):
            rendered = None
            if letter in ("X", "D") and count == 1 and nhw == 4:
                # a single 8-byte X or D constant renders its DP float
                # value on the first hex row, not on a wrapped element
                # row (DASS #0ROUND SCALER1 DC XL4; $0DCICYC DCIROUND
                # DC D -> 5.0000000000000000E-01; sign slot at col 89)
                txt = self._dc_float([words.get(a0 + j, 0)
                                      for j in range(4)])
                rendered = txt if txt.startswith(("-", "^")) else " " + txt
            out.extend(self._dhex_rows(c, a0, nhw, rendered=rendered))
            kindprec = self._DC_ELEM.get(letter)
            if kindprec and rendered is None:
                out.extend(self._element_rows(c, a0, kindprec[0],
                                              kindprec[1], 0, count))

    def _asm_code(self, c, out, a, end, st, gap_to=None):
        """Disassemble [a, end); MAFGEN leaves based references
        unresolved in NONHAL csects (no R0/R3 convention) -- only
        direct, IC-relative and relocated-immediate references get an
        EA, plus refs through an LXA-tracked base: LXA Rx,<addr> loads
        the register with the dump fullword's address part, and only
        LXA/LXAR write MAFGEN's NONHAL register machine -- every other
        write is ignored, the value persists (DASS FPMSCHED: LXA
        R2,FCMPLDSE -> '1(R2)' EA 000001; the later 'LH R2,3(R0)' does
        not disturb it, '4(R2)'/'5(R2)' still resolve 000004/000005;
        FCMBMASK: three consecutive 'LA R0,1(R0)' each print 000001).
        Branch targets comment with all of the csect's labels at the
        target (comma-joined) through the CC-context aliases."""
        words = self.img.words
        labels = st["labels"]
        lbl_offs = sorted(labels)
        track = st.setdefault("track", {})   # persists across regions
        # flight MAFGEN never decoded deck-typed IOP-program csects, so
        # its error simulator never stepped or faulted inside them
        sim = c.cat not in ("BCE", "MSC")
        while a < end:
            hw1 = words.get(a, 0)
            hw2 = words.get(a + 1, 0) if a + 1 < end else 0
            # a 0000 halfword in the walk starts an alignment gap that
            # runs to the region's end (the next labeled/data statement)
            # regardless of the fill values (DASS FCMLINIT: one gap row
            # '0000 F300 0001 0800 C9FB' after FCMIBCE's single SLL;
            # FCMMGBOV: '0000 D800' before the halfword-odd FCMGRSST) --
            # but never past a relocated pool adcon (its 'DC Y' prints)
            if hw1 == 0 and gap_to is not None and gap_to > a:
                stop = gap_to
                for k in range(a + 1, gap_to):
                    if k in self.ycon_sites or k in self.unresolved_sites:
                        stop = k
                        break
                out.extend(self._gap_rows(c, a, stop - a))
                if stop == gap_to:
                    return True
                a = stop
                continue
            if a in self.ycon_sites or a in self.unresolved_sites:
                # a standalone relocated halfword in a code region is an
                # LTORG-pool adcon: 'DC     Y', no label, no value
                # (DASS $0DCICYC+015E..) -- genuine Y-adcon relocs only
                # (listing.ycon_sites); exotic vector fixups decode
                out.append(self._dc_line(c, a - c.start, 1, "", "DC",
                                         "Y", hexs=[hw1]))
                a += 1
                continue
            inst = self.dis.decode(a, hw1, hw2, track, conventions=False)
            if inst is None:
                # undecodable loaded halfword: '?' mnemonic + ILLEGAL OP
                # CODE (un-loaded holes and IOP csects stay bare rows)
                if sim and a in self.img.occupied:
                    out.append(self._row(c, a, [hw1]).ljust(COL_MNEM) + "?")
                    out.extend(self.esim.illegal(a))
                else:
                    out.append(self._row(c, a, [hw1]))
                a += 1
                continue
            if inst.name == "LHI" and a + 1 in self.reloc_sites:
                x = inst.fields.get("x")
                inst.name = inst.mnemonic = "LA"
                inst.operands = f"R{x},X'{hw2:04X}'"
                inst.ea, inst.imm = hw2, None
            if inst.name == "BAL" and inst.idx_info and inst.ea is None:
                # BAL@# through a pointer (PSA transfer vector / ZCON):
                # same fetch as SCAL@# -- the pointer sits at D2 (B2=11),
                # the fetched halfword expands in the caller's sector
                # (DASS: BAL@# R7,X'000C'(R7,R3) -> 0198A0 FCMTRACE)
                _ix, ia, _ii, disp = inst.idx_info
                if ia and inst.fields.get("b") == 3:
                    if disp in self.reloc_target:
                        inst.ea = self.reloc_target[disp]
                    else:
                        ptr = words.get(disp)
                        if ptr is not None:
                            inst.ea = (((a >> 15) << 15) | (ptr & 0x7FFF)
                                       if ptr & 0x8000 else ptr)
            n = inst.nhw
            line = self._inst_prefix(c, a, inst, hw1, hw2)
            comment = ""
            if inst.name in ("BC", "BCF", "BCB", "BCT", "BCTB", "BIX",
                             "BAL") and inst.ea is not None:
                names = labels.get(inst.ea - c.start)
                if not names and inst.name == "BAL":
                    # cross-module call: the callee's entry symbol
                    # (DASS: BAL R7,X'ADBA' -> FPMITUPD)
                    bloc = self.img.locate(inst.ea)
                    if bloc.symbol and bloc.symbol_offset == 0:
                        names = [bloc.symbol]
                if not names and c.start <= inst.ea < c.start + c.size:
                    # in-csect target past a label: nearest-below label
                    # + offset, name padded (DASS: BCTB -> '#@LB29  +11')
                    j = self._bisect.bisect_right(
                        lbl_offs, inst.ea - c.start) - 1
                    if j >= 0:
                        off = inst.ea - c.start - lbl_offs[j]
                        # among co-located candidates the INSTRUCTION
                        # label wins over DS-0 markers (DASS #0ETOH:
                        # 'CONVERT +6', not 'ETOH    +6')
                        eq = st.get("equs", {}).get(lbl_offs[j])
                        name = min(eq)[1] if eq else labels[lbl_offs[j]][0]
                        names = [f"{name:<8}+{off}"]
                tgt = ",".join(names) if names else ""
                if inst.name in ("BCT", "BCTB", "BIX", "BAL") \
                        or st["cc"] == "UNDEF":
                    comment = tgt
                else:
                    alias = alias_for(st["cc"], inst.fields.get("x"))
                    # an unresolvable target leaves the bare alias
                    # (DASS: FPMUPTOX 'NOP', CPASP 'BNZ')
                    if alias:
                        comment = f"{alias:<8}{tgt}".rstrip()
                    else:
                        comment = tgt
            elif inst.name in ("BCR", "BCRE") \
                    and inst.fields.get("x") == 7:
                # register-return alias, suppressed like every alias
                # when the CC context is UNDEF (DASS FCMSVC: BR after
                # BAL at +0038, bare after PC at +003F/+0064)
                comment = "" if st["cc"] == "UNDEF" else "BR"
            elif inst.name == "BC" and inst.idx_info \
                    and inst.idx_info[1]:
                # indirect branch (BC@/@#): target unknowable, the
                # alias prints alone (DASS: BC@# 7,... -> 'B')
                comment = alias_for(st["cc"], inst.fields.get("x"))
            else:
                comment = self._comment(inst, c)
            if comment:
                line = line.ljust(COL_COMMENT) + comment
                # long label joins wrap at the 132-column line width,
                # continuing at the comment column (DASS: 'BNP     #@LB38,
                # #@LB36,#@LB34,#@LB32,' / col-96 '#@LB30')
                while len(line) > LINE_WIDTH:
                    cut = line.rfind(",", COL_COMMENT, LINE_WIDTH) + 1
                    if cut <= COL_COMMENT:
                        break
                    out.append(line[:cut].rstrip())
                    line = " " * COL_COMMENT + line[cut:]
            out.append(line.rstrip())
            if sim:
                out.extend(self.esim.step(a, inst, hw1, hw2))
            if inst.name == "SVC" and inst.ea is not None \
                    and inst.ea in words:
                out.append(self._CONT_PAD
                           + f"***   SVC CODE: {words[inst.ea] & 0xFF}")
            if inst.name == "LXA":
                # dump fullword's address part (POO: bits 1-15 of the
                # first halfword); the FCOS dispatch pointers this loads
                # (FCMPLDSE/$ZDSECLR/FPMSVPTR) all hold 0
                x = inst.fields.get("x")
                v = words.get(inst.ea) if inst.ea is not None else None
                if x is not None:
                    if v is not None:
                        track[x] = v & 0x7FFF
                    else:
                        track.pop(x, None)
            elif inst.name == "LXAR":
                x, y = inst.fields.get("x"), inst.fields.get("y")
                if x is not None:
                    if y in track:
                        track[x] = track[y]
                    else:
                        track.pop(x, None)
            if inst.name in _CC_CMP:
                st["cc"] = "CMP"
            elif inst.name in _CC_TEST:
                st["cc"] = "TEST"
            elif inst.name == "PC":
                # a program call returns with an unknown CC: the alias
                # context drops back to UNDEF (DASS FCMSVC +003F)
                st["cc"] = "UNDEF"
            elif inst.name not in _CC_KEEP:
                st["cc"] = None
            a += n
        return False

    def _fill_between(self, c, fromB, toB):
        """True when byte range [fromB, toB) is empty or holds only
        alignment fill -- the IOP hex run continues across it."""
        if toB < fromB:
            return False
        words = self.img.words
        return all(words.get(c.start + b // 2) in _FILL_HW
                   for b in range(fromB, toB, 2))

    def _asm_region(self, c, out, off0, off1, st, data_next):
        """A region uncovered by data statements: code, except trailing
        C9FB alignment fill ahead of a data item (the assembler's csect
        gap fill), which prints as ALIGNMENT GAP rows.  Fill closing the
        CSECT is a gap too -- disassembling it ran a 2-halfword SVC off
        the end into the next csect (DASS CPASP+0011, FCMSMASK+0081,
        IREM+000F and 7 more: a bare 1-hw gap row, no instruction)."""
        words = self.img.words
        end = off1
        if data_next or off1 >= c.size:
            while end > off0 and words.get(c.start + end - 1) == 0xC9FB:
                end -= 1
        if end > off0:
            if self._asm_code(c, out, c.start + off0, c.start + end, st,
                              gap_to=c.start + off1):
                return          # a 0000-led gap ran through off1
        if end < off1:
            out.extend(self._gap_rows(c, c.start + end, off1 - end))

    def _asm_rows(self, c):
        """NONHAL csect rendering from the asmg statement stream, or None
        when the module has no v2 sidecar (degrades to the hex dump)."""
        info = self._asmg_for(c)
        if info is None:
            return None
        # IOP statement records coalesce to hex runs only in deck-typed
        # IOP-program csects; embedded IOP fragments in a CPU-class csect
        # disassemble as CPU code exactly as flight MAFGEN did -- it had
        # no statement types, only the deck (DASS FCMMGBOV: 'SRA R0,0' /
        # '?' rows over the BCE handler table)
        iop_ok = c.cat in ("BCE", "MSC")
        events = [(r[0] // 2, 1, r) for r in info["data"]
                  if r[0] // 2 < c.size and (iop_ok or r[2] != "IOP")]
        for off, pairs in info["equs"].items():
            if off < c.size:
                events.extend((off, 0, n)
                              for _l, n in sorted(pairs))
        # co-located ordering (DASS #0ETOH: 'ETOH DS 0H' / 'DTOH DS 0H'
        # before 'CONVERT EQU *'): labeled DS-0 markers print first,
        # then the EQU-star code labels, then real data statements
        def _order(e):
            off, kind, payload = e
            if kind == 1 and payload[4] == 0 and payload[5]:
                return (off, 0)                 # labeled DS 0F/0H marker
            return (off, 1 if kind == 0 else 2)
        events.sort(key=_order)
        out: list = []
        st = {"cc": "UNDEF", "labels": info["labels"],
              "equs": info["equs"]}
        cursor = 0
        i = 0
        while i < len(events):
            off, kind, payload = events[i]
            if off > cursor:
                self._asm_region(c, out, cursor, off, st, kind == 1)
                cursor = off
            if kind == 0:
                out.append(self._dc_line(c, off, 1, payload, "EQU", "*"))
                i += 1
                continue
            if payload[2] == "IOP":
                # consecutive IOP statements coalesce into label-bounded
                # hex row runs (MAFGEN never disassembled BCE programs)
                endB = payload[1]
                j = i + 1
                while j < len(events) and events[j][1] == 1 \
                        and events[j][2][2] == "IOP" \
                        and self._fill_between(c, endB, events[j][2][0]):
                    endB = events[j][2][1]
                    j += 1
                hw0, hwe = payload[0] // 2, (endB + 1) // 2
                # alignment pad inside or trailing the run is part of its
                # hex, not a gap of its own: MAFGEN dumped the whole IOP
                # region straight through to the next statement (DASS
                # FIOMCNTL+0014 = one 6-hw row ending 'C9FB'; +002A = one
                # 9-hw row running through an interior CNOP 'C000')
                nxt = events[j][0] if j < len(events) else c.size
                while hwe < nxt \
                        and self.img.words.get(c.start + hwe) in _FILL_HW:
                    hwe += 1
                out.extend(self._dhex_rows(c, c.start + hw0, hwe - hw0))
                cursor = max(cursor, hwe)
                i = j
                continue
            self._asm_datum(c, out, payload)
            cursor = max(cursor, (payload[1] + 1) // 2)
            i += 1
        if cursor < c.size:
            self._asm_region(c, out, cursor, c.size, st, False)
        return out
