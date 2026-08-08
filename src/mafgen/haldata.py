"""HAL #D/#P data-csect rendering: the SDF symbol/structure walk, plus the
shared data-row primitives (_dhex_rows/_gap_rows/_banner_line) the asm
renderer also draws on."""
from .common import COL_NOTE, addr_range


class DataWalk:
    #
    # data-csect walk
    #
    # HAL #D/#P csects render as an SDF symbol/structure walk:
    # symbol name at col 32, its halfwords at 65 (stride 6),
    # rendered value at 88 (scalars keep a sign slot: digits at 89),
    # NAME tag at 100, ARRAY tag at 106, type text at 113.  Wide items
    # (arrays, CHARACTER, VECTOR/MATRIX, BIT>16) print a name line, then
    # 16-per-row hex at col 36 and per-type rendered element rows --
    # suppressed entirely when the extent is all zero.  Structure
    # instances walk their template's leaves per copy (+++ COPY n OF m
    # +++ / +++ END OF STRUCTURE +++ at col 19), and padding prints as
    # '*** ALIGNMENT GAP ***' rows with the fill halfwords.

    _TYPE_NAMES = {"BIT": "BIT STRING", "CHARACTER": "CHARACTER",
                   "STRUCTURE": "STRUCTURE", "EVENT": "EVENT"}

    @classmethod
    def _type_text(cls, kind, prec, role):
        # untyped symbols (e.g. NAMEs of program labels) print BIT STRING
        name = cls._TYPE_NAMES.get(kind) or (
            "BIT STRING" if kind is None else
            f"{'DP' if prec == 'DOUBLE' else 'SP'} {kind}")
        return f"{name:<10} {role}"

    @staticmethod
    def _bit_geom(prec, wraw):
        """(container halfwords, right-shift, field width) of a BIT item.
        The SDC packs (shift<<8)|width for DENSE fields; shift 0xFF
        encodes 0 (no 0 byte ever appears in the SDC, 0xFF does)."""
        sh, w = (wraw >> 8) & 0xFF, wraw & 0xFF
        if sh == 0xFF:
            sh = 0
        w = w or 16
        cont = 2 if (prec == "DOUBLE" or w > 16 or sh + w > 16) else 1
        return cont, sh, w

    def _elem_hw(self, kind, prec, wraw, name_var=False):
        """Halfwords of one element."""
        if name_var:
            return 1
        if kind == "INTEGER":
            return 2 if prec == "DOUBLE" else 1
        if kind == "SCALAR":
            return 4 if prec == "DOUBLE" else 2
        if kind == "BIT":
            return self._bit_geom(prec, wraw)[0]
        if kind == "CHARACTER":
            return 1 + ((wraw & 0xFF) + 1) // 2
        if kind in ("VECTOR", "MATRIX"):
            n = max(wraw >> 8, 1) * max(wraw & 0xFF, 1)
            return n * (4 if prec == "DOUBLE" else 2)
        return 1                                    # EVENT, NAME, unknown

    def _elem_align(self, kind, prec, wraw, name_var=False):
        if name_var:
            return 1
        if kind in ("SCALAR", "VECTOR", "MATRIX"):
            return 4 if prec == "DOUBLE" else 2
        if kind == "INTEGER" and prec == "DOUBLE":
            return 2
        if kind == "BIT" and self._bit_geom(prec, wraw)[0] == 2:
            return 2
        return 1

    @staticmethod
    def _copies(dims):
        n = 1
        for d in dims:
            if d:
                n *= d
        return n

    def _tmpl_geom(self, leaves):
        """(stride, used size) of one structure copy: leaf extents padded
        to the template's strictest member alignment.  A NAME leaf is a
        single 1-hw (2 if REMOTE) pointer descriptor -- its dims describe
        the REFERENT, never pointer copies (DASS #PCDMUIC CDMV_B_NAMES:
        6 copies of a NAME-ARRAY member = stride 1, not 2)."""
        size, align = 0, 1
        for addr, _n, kind, prec, wraw, dims, fl, _t in leaves:
            nv = bool(fl & 1)
            if nv:
                hw = 2 if fl & 8 else 1
            else:
                hw = self._elem_hw(kind, prec, wraw) * self._copies(dims)
            size = max(size, (addr or 0) + hw)
            align = max(align, self._elem_align(kind, prec, wraw, nv))
        stride = -(-size // align) * align
        return stride, size

    #
    # XXDTOC.bal replay (MAFGEN's float->decimal conversion)
    #
    # MAFGEN scales the DP value by TENSTBL powers of ten with the
    # AP-101 truncating hex-float multiply/divide until the
    # characteristic hits 0x4E, then reads the mantissa as the decimal
    # digits.  The chained truncation leaves tails Decimal-exact math
    # doesn't (DASS #0ROUND: 40FFFFFFFFFFFFFF -> 9.9999999999999980E-01,
    # exact would end ...98).  Port of tools/floatIBM/ibmFloat.c
    # ibm_dp_to_string.
    _MANT56 = (1 << 56) - 1
    _TENSTBL = (0x4110000000000000, 0x41A0000000000000, 0x4264000000000000,
                0x433E800000000000, 0x4427100000000000, 0x45186A0000000000,
                0x45F4240000000000, 0x4698968000000000, 0x475F5E1000000000,
                0x483B9ACA00000000, 0x492540BE40000000, 0x5156BC75E2D63100,
                0x59C9F2C9CD04674F, 0x621D6329F1C35CA5, 0x6A446C3B15F99267,
                0x729F4F2726179A22, 0x7B172EBAD6DDC73D)

    @staticmethod
    def _renorm56(m, e):
        while m & 0x00F0000000000000 == 0:
            m <<= 4
            e -= 1
        return m, e

    @classmethod
    def _dmul(cls, a, b):
        am, bm = a & cls._MANT56, b & cls._MANT56
        if not am or not bm:
            return 0
        sg = ((a ^ b) >> 63) & 1
        am, ae = cls._renorm56(am, (a >> 56) & 0x7F)
        bm, be = cls._renorm56(bm, (b >> 56) & 0x7F)
        p = am * bm
        if p >> 108:
            mant, ex = p >> 56, ae + be - 64
        else:
            mant, ex = (p >> 52) & cls._MANT56, ae + be - 65
        if ex < 0:
            return 0
        if ex > 0x7F:
            return 0xFF00000000000000
        return (sg << 63) | (ex << 56) | mant

    @classmethod
    def _ddiv(cls, a, b):
        am, bm = a & cls._MANT56, b & cls._MANT56
        if not am:
            return 0
        sg = ((a ^ b) >> 63) & 1
        am, ae = cls._renorm56(am, (a >> 56) & 0x7F)
        bm, be = cls._renorm56(bm, (b >> 56) & 0x7F)
        if am < bm:
            ex = ae - be + 64
        else:
            ex = ae - be + 65
            bm <<= 4
        mant = (am << 56) // bm
        if ex < 0:
            return 0
        if ex > 0x7F:
            return 0xFF00000000000000
        return (sg << 63) | (ex << 56) | mant

    @classmethod
    def _scalar_text(cls, hws):
        """IBM hex float -> MAFGEN's rendering: 0.0 for zero, otherwise
        d.<7|16 digits>E±ee via the XXDTOC scaling replay (mantissa
        digits fall out of the truncating hex-float arithmetic)."""
        v = 0
        for hw in hws:
            v = (v << 16) | hw
        if len(hws) == 2:               # SP extends to DP, low word 0
            v <<= 32
        if v & cls._MANT56 == 0:
            return "0.0"
        sign = v >> 63
        value = v & 0x7FFFFFFFFFFFFFFF
        e10 = 0
        for _ in range(8):
            diff = ((value >> 56) & 0x7F) - 78
            if diff == 0:
                break
            l10 = min((abs(diff) * 19728 + 8192) >> 14, 78)
            if l10 <= 0:
                break
            ones, tens = l10 % 10, l10 // 10
            if ones:
                f = cls._TENSTBL[ones]
                value = cls._ddiv(value, f) if diff > 0 \
                    else cls._dmul(value, f)
            if tens:
                f = cls._TENSTBL[9 + tens]
                value = cls._ddiv(value, f) if diff > 0 \
                    else cls._dmul(value, f)
            e10 += l10 if diff > 0 else -l10
        m = f"{value & cls._MANT56:017d}"
        start = 0
        while start < 16 and m[start] == "0":
            start += 1
        e10d = e10 + (16 - start)
        sig = 17 if len(hws) == 4 else 8
        take = min(sig, 17 - start)
        disp = m[start:start + take] + "0" * (sig - take)
        return f"{'-' if sign else ''}{disp[0]}.{disp[1:]}E{e10d:+03d}"

    @staticmethod
    def _int_text(hws):
        v = 0
        for hw in hws:
            v = (v << 16) | hw
        bits = 16 * len(hws)
        if v >> (bits - 1):
            v -= 1 << bits
        return str(v)

    @staticmethod
    def _bit_text(val, width):
        if width == 1:
            return "TRUE" if val else "FALSE"
        return "BIN'" + format(val, "0%db" % width) + "'"

    def _dsym_line(self, c, a, nhw, name, hws=None, rendered="",
                   nametag=False, arrtag=False, ttext=""):
        """One inline data-symbol line."""
        rng = addr_range(a, nhw)
        line = f" {rng}  {c.name:<8}+{a - c.start:04X}"
        line = line.ljust(32) + name
        if hws:
            line = line.ljust(64) + " ".join(
                self._hx(a + j, v) for j, v in enumerate(hws))
        if rendered:
            # scalars keep a sign slot at 88; everything else starts there
            if rendered[0].isdigit() and "E" in rendered or rendered == "0.0":
                line = line.ljust(COL_NOTE) + rendered
            else:
                line = line.ljust(88) + rendered
        if nametag:
            line = line.ljust(100) + "NAME"
        if arrtag:
            line = line.ljust(106) + "ARRAY"
        if ttext:
            line = line.ljust(113) + ttext
        return line.rstrip()

    def _dhex_rows(self, c, a0, n, note=None, rendered=None):
        """16-per-row hex at col 36; ``note`` places a text at col 89 on
        every row (ALIGNMENT GAP), ``rendered`` on the first row only
        (single BIT>16 items carry their BIN' there)."""
        out = []
        words = self.img.words
        for r0 in range(a0, a0 + n, 16):
            k = min(16, a0 + n - r0)
            line = self._row(c, r0, [words.get(a, 0)
                                     for a in range(r0, r0 + k)])
            txt = note if note else (rendered if r0 == a0 else None)
            if txt:
                line = line.ljust(COL_NOTE) + txt
            out.append(line.rstrip())
        return out

    def _banner_line(self, c, a0, n, text):
        rng = addr_range(a0, n)
        line = f" {rng}  {c.name:<8}+{a0 - c.start:04X}"
        return (line.ljust(32) + text).rstrip()

    def _gap_rows(self, c, a0, n):
        # deck-typed IOP-program csects (AS TYPE BCE/MSC) never carry the
        # annotation: flight MAFGEN dumped them as raw hex and ran none of
        # its alignment-gap analysis inside them.  Across the whole SSW
        # DASS not one BCE/MSC csect has a noted gap, and all three that
        # hold fill print it bare (FIOCBLKS +0093..+02C3, FIOHISAM+00CB,
        # FIOMUWP9+0609)
        note = (None if getattr(c, "cat", None) in ("BCE", "MSC")
                else "*** ALIGNMENT GAP ***")
        return self._dhex_rows(c, a0, n, note=note)

    def _nonzero(self, a0, n):
        words = self.img.words
        return any(words.get(a) for a in range(a0, a0 + n))

    def _element_rows(self, c, a0, kind, prec, wraw, count):
        """Rendered element rows under an array/vector/matrix hex dump:
        integers right-justified in 8-wide cells (10/row), scalars in
        16-wide (SP, 5/row) / 24-wide (DP, 3/row) cells with a sign
        column, BIN'/TRUE tokens left-justified at 36 two spaces apart
        (as many as fit in 132)."""
        words = self.img.words
        # vector/matrix rows walk the aggregate's scalar elements
        ehw = (self._elem_hw("SCALAR", prec, 0)
               if kind in ("VECTOR", "MATRIX")
               else self._elem_hw(kind, prec, wraw))
        vals = [[words.get(a0 + i * ehw + j, 0) for j in range(ehw)]
                for i in range(count)]
        out = []
        if kind == "INTEGER":
            texts = [self._int_text(v) for v in vals]
            for i in range(0, len(texts), 12):
                out.append((" " * 34 + "".join(f"{t:>8}"
                            for t in texts[i:i + 12])).rstrip())
        elif kind in ("SCALAR", "VECTOR", "MATRIX"):
            cw = 24 if prec == "DOUBLE" else 16
            per = (wraw & 0xFF if kind == "MATRIX" else
                   3 if prec == "DOUBLE" else 5) or 1
            texts = [self._scalar_text(v) for v in vals]
            for i in range(0, len(texts), per):
                line = ""
                for j, t in enumerate(texts[i:i + per]):
                    col = 37 + j * cw          # digits; sign slot at col-1
                    line = line.ljust(col - 1 if t.startswith("-") else col)
                    line += t
                out.append(line.rstrip())
        elif kind in ("BIT", "EVENT"):
            _cont, sh, w = self._bit_geom(prec, wraw)
            toks = []
            for v in vals:
                iv = 0
                for hw in v:
                    iv = (iv << 16) | hw
                if kind == "EVENT":
                    toks.append("TRUE" if iv else "FALSE")
                else:
                    toks.append(self._bit_text((iv >> sh) & ((1 << w) - 1),
                                               w))
            per = max(1, (132 - 36 + 2) // (len(toks[0]) + 2)) if toks else 1
            for i in range(0, len(toks), per):
                out.append(" " * 36 + "  ".join(toks[i:i + per]))
        return out

    def _emit_datum(self, c, out, e, role):
        """Emit one data symbol (top-level entry or structure leaf);
        returns its halfword extent."""
        addr, name, kind, prec, wraw, dims, fl, tmplname = e
        name_var = bool(fl & 1)
        if fl & 2:
            role = "CONSTANT"
        if fl & 16:
            # a NAME over a label prints the referent's role (DASS
            # DM5V_N_PROG: 'NAME  BIT STRING LABEL')
            role = "LABEL"
        a0 = c.start + addr
        words = self.img.words
        copies = self._copies(dims)
        arrtag = bool(dims) and kind != "STRUCTURE"
        ttext = self._type_text(kind, prec, role)
        if kind == "STRUCTURE" and not name_var:
            return self._emit_struct(c, out, e, role)
        if name_var:
            # a REMOTE NAME is a 2-halfword (sector,offset) pointer;
            # otherwise 1 halfword -- either way a single descriptor,
            # arrayed referents notwithstanding
            nhw = 2 if fl & 8 else 1
            out.append(self._dsym_line(c, a0, nhw, name,
                                       hws=[words.get(a0 + j, 0)
                                            for j in range(nhw)],
                                       nametag=True, arrtag=arrtag,
                                       ttext=ttext))
            return nhw
        ehw = self._elem_hw(kind, prec, wraw)
        ext = ehw * copies
        # a packed sub-field of a 32-bit container stays inline (both
        # container halfwords at 65); only a full-width BIT(17..32) item
        # uses the hex-row form with its BIN' at col 89
        wide = (copies > 1 or kind in ("CHARACTER", "VECTOR", "MATRIX")
                or (kind == "BIT" and (wraw & 0xFF or 16) > 16))
        if not wide:
            hws = [words.get(a0 + j, 0) for j in range(ehw)]
            iv = 0
            for hw in hws:
                iv = (iv << 16) | hw
            if kind == "INTEGER":
                rendered = self._int_text(hws)
            elif kind == "SCALAR":
                rendered = self._scalar_text(hws)
            elif kind == "EVENT":
                rendered = "TRUE" if iv else "FALSE"
            elif kind == "BIT":
                _cont, sh, w = self._bit_geom(prec, wraw)
                rendered = self._bit_text((iv >> sh) & ((1 << w) - 1), w)
            else:
                rendered = ""
            out.append(self._dsym_line(c, a0, ext, name, hws=hws,
                                       rendered=rendered, arrtag=arrtag,
                                       ttext=ttext))
            return ext
        # wide: name line, then (unless all-zero) hex + rendered rows
        out.append(self._dsym_line(c, a0, ext, name, arrtag=arrtag,
                                   ttext=ttext))
        if self._nonzero(a0, ext):
            if kind == "BIT" and copies == 1:       # single BIT(17..32)
                _cont, sh, w = self._bit_geom(prec, wraw)
                iv = (words.get(a0, 0) << 16) | words.get(a0 + 1, 0)
                out.extend(self._dhex_rows(
                    c, a0, ext, rendered=self._bit_text(
                        (iv >> sh) & ((1 << w) - 1), w)))
            else:
                out.extend(self._dhex_rows(c, a0, ext))
                if kind != "CHARACTER":
                    n = (max(wraw >> 8, 1) * max(wraw & 0xFF, 1) * copies
                         if kind in ("VECTOR", "MATRIX") else copies)
                    out.extend(self._element_rows(c, a0, kind, prec, wraw,
                                                  n))
        return ext

    def _emit_struct(self, c, out, e, role):
        addr, name, _kind, prec, wraw, dims, fl, tmplname = e
        leaves = self.tmpl_map.get(c.name[2:], {}).get(tmplname or "")
        copies = self._copies(dims)
        a0 = c.start + addr
        if not leaves:
            out.append(self._dsym_line(
                c, a0, 1, name, ttext=self._type_text("STRUCTURE", prec,
                                                      role)))
            return 1
        stride, used = self._tmpl_geom(leaves)
        # a single copy spans just its used size (trailing pad belongs to
        # the csect, DASS: DM9V_SAVE_TABLE); multi-copy instances keep the
        # stride padding inside every copy incl. the last (CZ1V_D_MACT)
        ext = used if copies == 1 else stride * copies
        out.append(self._dsym_line(c, a0, ext, name,
                                   ttext=self._type_text("STRUCTURE", prec,
                                                         role)))
        for k in range(copies):
            base = addr + k * stride
            cend = base + (used if copies == 1 else stride)
            if copies > 1:
                ca = c.start + base
                rng = (addr_range(ca, stride) if stride > 1
                       else f"{ca:06X}       ")
                out.append(f" {rng}     +++ COPY {k + 1} OF {copies} +++")
            cur = base
            for leaf in leaves:
                la = base + (leaf[0] or 0)
                if la > cur:
                    out.extend(self._gap_rows(c, c.start + cur, la - cur))
                    cur = la
                lext = self._emit_datum(
                    c, out, [la, leaf[1], leaf[2], leaf[3], leaf[4],
                             leaf[5], leaf[6], leaf[7]], "TERMINAL")
                cur = max(cur, la + lext)
            if k == copies - 1:
                out.append(" " * 19 + "+++ END OF STRUCTURE +++")
            if cur < cend:
                out.extend(self._gap_rows(c, c.start + cur, cend - cur))
        return ext

    def _walk_entries(self, c, out, entries, cursor):
        for e in entries:
            addr = e[0]
            if addr >= c.size:
                continue
            if addr > cursor:
                out.extend(self._gap_rows(c, c.start + cursor,
                                          addr - cursor))
                cursor = addr
            ext = self._emit_datum(c, out, e, "VARIABLE")
            cursor = max(cursor, addr + ext)
        return cursor

    def _data_rows(self, c):
        """The full #D/#P walk, or None when the stem has no data entries
        (degrades to the plain hex dump)."""
        groups = self.data_map.get(c.name)
        if not groups:
            return None
        # a CONSTANT at offset 0 has no storage (its value lives in the
        # adcon/literal pool); stored constants carry a real offset
        # (DASS: VMM_BLK_LNTH absent, AIBK_KYBD_MSG walked)
        groups = [[gid, gname,
                   [e for e in ents if not (e[6] & 2 and e[0] == 0)]]
                  for gid, gname, ents in groups]
        out: list = []
        cursor = 0
        lit = self.lit_area.get(c.name)
        if c.name.startswith("#D"):
            # a unit with no stored statics still walks: pool banner +
            # each block's LBD word (DASS #DDUMULK: '0015' pool, silent
            # slack, LBD at size-2)
            if not groups:
                return None
            # adcon pool runs to the first block's LOCAL BLOCK DATA word:
            # blocks lay out in blk_id order, 2 halfwords each ahead of
            # the first stored static
            bd = None
            for i, (_gid, _gn, ents) in enumerate(groups):
                if ents:
                    bd = ents[0][0] - 2 * (i + 1)
                    break
            # the dumped pool extent is the SDF's INITIAL LITERAL AREA
            # end (root hw 85), not the block-data start: the slack up
            # to the first LBD (remote-descriptor cons, fill) is
            # skipped entirely (DASS #DAIESIP +20-+25, #DDXRDMM
            # +12-+15 print nothing)
            pool = self.init_pool.get(c.name)
            pool = bd if pool is None else min(pool, bd or pool)
            if pool and pool > 0:
                out.append(self._banner_line(
                    c, c.start, pool, "??? ADCONS,LITERALS,ETC. ???"))
                out.extend(self._dhex_rows(c, c.start, pool))
                cursor = pool
            first = True
            for gi, (_gid, gname, ents) in enumerate(groups):
                nhw = 2
                if ents:
                    lbd = ents[0][0] - 2
                    if first and self.cat_by_name.get("#X" + c.name[2:]):
                        # an EXCLUSIVE unit's block data carries a 3-hw
                        # exclusive-control cell ahead of the LBD word:
                        # the banner spans 5 hw (DASS: all 8 #X-bearing
                        # SSW units and only those -- #DDNXBMS+000C
                        # '0000 8012 000F 0011 06EA')
                        lbd -= 3
                        nhw = 5
                elif bd is None and gi == len(groups) - 1:
                    # no block stores statics: the final LBD sits just
                    # ahead of the literal area (DASS #DDKFCCO: LBD at
                    # +4, litarea 6, adcon list follows to csect end;
                    # #DDUMULK's litarea == size so its +4-in-6-hw is
                    # the same rule).  Statics-less blocks between
                    # stored ones pack before the next block's LBD
                    # (the DMPMMM rule) -- those keep the packed path.
                    lbd = (lit - 2 if lit is not None
                           and 2 <= lit <= c.size else c.size - 2)
                else:
                    lbd = cursor + (cursor & 1)
                if lbd > cursor:
                    if first:
                        cursor = lbd        # pool slack: silent hole
                    else:
                        out.extend(self._gap_rows(c, c.start + cursor,
                                                  lbd - cursor))
                        cursor = lbd
                first = False
                if lbd + nhw > c.size:
                    break
                out.append(self._banner_line(
                    c, c.start + cursor, nhw,
                    f"--- LOCAL BLOCK DATA (BLOCK {gname}) ---"))
                out.extend(self._dhex_rows(c, c.start + cursor, nhw))
                cursor += nhw
                cursor = self._walk_entries(c, out, ents, cursor)
        else:
            for _gid, _gn, ents in groups:
                cursor = self._walk_entries(c, out, ents, cursor)
        if lit is not None and lit < c.size and lit >= cursor:
            # the trailing pool banner starts at the walk end, not the
            # SDF litarea: initial-value material between the last
            # static and the literal area belongs to the same region
            # (DASS #DDNXBMS: banner at +15, litarea at +31)
            out.append(self._banner_line(
                c, c.start + cursor, c.size - cursor,
                "??? ADCONS,LITERALS,ETC. ???"))
            out.extend(self._dhex_rows(c, c.start + cursor,
                                       c.size - cursor))
        elif cursor < c.size:
            # leftover past the walked entries is trailing pool material
            # even when the SDF litarea is unusable (DXCCCS: litarea 14
            # in a 13-hw csect; DASS still banners +0A)
            out.append(self._banner_line(
                c, c.start + cursor, c.size - cursor,
                "??? ADCONS,LITERALS,ETC. ???"))
            out.extend(self._dhex_rows(c, c.start + cursor,
                                       c.size - cursor))
        return out
