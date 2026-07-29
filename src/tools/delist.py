"""Recover source card decks from IBM mainframe compiler/assembler listings.

Converts a PDS listing tree (e.g. pfs/code/OI301700.listing, one file per
member, HAL/S-FC or assembler listing format) into card-image source decks
in the shape of pfs/code/OI340600: 80-column cards, content in columns
1-72, sequence number in 73-78, revision code in 79-80.

Two listing dialects are recognized per member:

* HAL/S-FC formatted source listing::

      cSSSSSSnnnnnfT|<--------- 101 columns of content --------->|RR|BLOCK

  c = ASA carriage control, S = card sequence number, n = statement
  number, f = '+' when the line came from an INCLUDE expansion (dropped),
  T = line type (C comment, D directive, M main, E exponent, S subscript),
  RR = revision code.  M/E/S groups are the output writer's 2-D rendering
  of a statement and are de-formatted back to 1-D source (subscripts to
  $(...), exponents to **(...), type-annotation overmarks dropped).

* Assembler (AP-101 XPL assembler / OS/360-style) listing: the original
  80-column card is embedded verbatim at a fixed column; lines whose
  statement number carries a '+' (macro/COPY expansions) are dropped.

The HAL/S output writer re-tokenizes the source, so recovered decks have
normalized spacing and merged trailing comments; card boundaries and
continuation-card sequence fields of multi-card statements are not
recoverable from the listing.  Where a reference tree (--splice) contains
a member whose statement matches by code-word stream and seq/rev, the
reference cards are adopted verbatim, recovering the original bytes.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional

EOL = b"\r\n"

# ----------------------------------------------------------------- parsing

MEMBER_BANNER = re.compile(rb"^.\*+MEMBER=(\S+)\s*\*+")


@dataclass
class HalLine:
    """One card line of a HAL/S formatted source listing."""
    cc: bytes         # ASA carriage control (col 1)
    seq: bytes        # card sequence field (cols 2-7); b'' when blank
    stmt: bytes       # statement number field (cols 8-12); b'' when blank
    included: bool    # '+' flag (col 13): INCLUDE-expansion line
    typ: bytes        # line type: C D M E S (col 14)
    content: bytes    # 101 columns after the '|' (cols 16-116)
    rev: bytes        # revision code (cols 118-119)
    lineno: int


class Account:
    """Strict accounting: every input line must land in a known bucket."""

    def __init__(self):
        self.buckets = {}
        self.oddities = []

    def add(self, bucket, line=None, lineno=None):
        self.buckets[bucket] = self.buckets.get(bucket, 0) + 1
        if bucket == "unknown":
            self.oddities.append((lineno, line))


_UNDERLINE = re.compile(rb"^\+[ _]*$")
_PAGEJUNK = re.compile(rb"^[+1 0-]\s*$")
_ERRSUM = re.compile(rb"^ \*\*\* SUMMARY OF ERRORS \*\*\*")


def parse_hal_member(data, acct):
    """Yield HalLine for each card line; junk goes to the account."""
    in_errsum = False
    for lineno, raw in enumerate(data.split(EOL), 1):
        line = raw.rstrip()
        if MEMBER_BANNER.match(line):
            acct.add("banner")
            continue
        if not line or _PAGEJUNK.match(line):
            acct.add("blank")
            continue
        if _UNDERLINE.match(line):
            acct.add("underline")
            if line.strip(b"+ "):
                yield HalLine(b"+", b"", b"", False, b"_",
                              raw.ljust(120)[15:116], b"", lineno)
            continue
        if _ERRSUM.match(line):
            in_errsum = True
        if in_errsum:
            acct.add("error-summary")
            continue
        if len(line) > 14 and line[14:15] == b"|":
            l = raw.ljust(120)
            cc = l[0:1]
            seq = l[1:7].strip()
            stmt = l[7:12].strip()
            included = l[12:13] == b"+"
            typ = l[13:14]
            content = l[15:116]
            bar = l[116:117]
            rev = l[117:119].strip()
            if typ not in b"CDMES" or bar not in (b"|", b" ", b""):
                acct.add("unknown", line, lineno)
                continue
            acct.add("card")
            yield HalLine(cc, seq, stmt, included, typ, content, rev, lineno)
        else:
            acct.add("unknown", line, lineno)


def detect_kind(data, member):
    """'hal', 'asm', or 'empty' for one listing member."""
    if not data.strip():
        return "empty"
    nhal = 0
    for raw in data.split(EOL)[:400]:
        if len(raw) > 14 and raw[14:15] == b"|" and raw[13:14] in b"CDMES":
            nhal += 1
    if nhal >= 2:
        return "hal"
    if AsmMember(data, Account(), member).calibrate() is not None:
        return "asm"
    return "unknown"


# ------------------------------------------------------------ asm handler

_SEQREV = re.compile(rb"\d{6}[A-Z][A-Z0-9 ]?$")


class AsmMember:
    """Assembler listing -> source cards.

    The 80-column source card is embedded at a fixed column, preceded by
    the statement number and a '+' flag for generated (macro expansion)
    lines.  The card column is calibrated from lines whose card region
    ends with a seq+rev field.
    """

    # statement number, flag, then the card
    _LINE = re.compile(rb"^(.{0,60}?)(\d+)([+ ])(.{1,80})$", re.DOTALL)

    _COPY = re.compile(rb"(START|END) OF COPY MEMBER (\S+)")

    def __init__(self, data, acct, member):
        self.data = data
        self.acct = acct
        self.member = member.encode()
        self.cards = []
        self.stmts = []           # statement number per card
        self.gens = []            # '+' (generated) lines since previous card
        self.copies = {}          # copied-member name -> [cards]

    def calibrate(self):
        """Find the card start column: mode of positions where a line has
        stmt+' '+card and card[72:80] looks like seq+rev."""
        votes = {}
        for raw in self.data.split(EOL):
            line = raw.rstrip()
            if len(line) < 40:
                continue
            # member name may trail the card; strip it
            body = line
            if body.endswith(self.member):
                body = body[: -len(self.member)].rstrip()
            m = self._LINE.match(body)
            if not m:
                continue
            card = m.group(4)
            if len(card) >= 79 and _SEQREV.search(card[:80].rstrip()):
                col = m.start(4)
                votes[col] = votes.get(col, 0) + 1
        if not votes:
            return None
        return max(votes, key=votes.get)

    def run(self):
        col = self.calibrate()
        if col is None:
            self.acct.add("asm-nocal")
            return []
        flagcol = col - 1
        stmt_re = re.compile(rb"^[ 0-9A-F]*? *(\d+)$")
        in_copy = None
        gen = 0
        for lineno, raw in enumerate(self.data.split(EOL), 1):
            line = raw.rstrip()
            if MEMBER_BANNER.match(line):
                self.acct.add("banner")
                continue
            if not line or _PAGEJUNK.match(line):
                self.acct.add("blank")
                continue
            m = self._COPY.search(line)
            if m and b"'" not in line[:col]:
                in_copy = m.group(2) if m.group(1) == b"START" else None
                self.acct.add("asm-copymark")
                continue
            body = line
            if body.endswith(self.member) and len(body) > col + 80:
                body = body[: -len(self.member)].rstrip()
            # head: ASA cc, then LOC/object-code (hex+spaces), then the
            # right-justified statement number; page titles etc. fail this
            head, flag = body[1:flagcol], body[flagcol:col]
            card = body[col:]
            if flag == b"+":
                self.acct.add("asm-generated")
                gen += 1
                continue
            m = stmt_re.match(head)
            if not m or not card.strip():
                self.acct.add("asm-junk", line, lineno)
                continue
            if len(card) > 80:
                self.acct.add("unknown", line, lineno)
                continue
            if in_copy:
                self.acct.add("asm-copied")
                self.copies.setdefault(in_copy.decode(), []).append(card.ljust(80))
            else:
                self.acct.add("card")
                self.cards.append(card.ljust(80))
                self.stmts.append(int(m.group(1)))
                self.gens.append(gen)
                gen = 0
        return self.cards


# ------------------------------------------------------- HAL/S statements

WIDTH = 101  # columns of listing content between the '|' bars


@dataclass
class MRow:
    m: bytes                 # M-line content, WIDTH cols
    e: Optional[bytes]       # E line above, WIDTH cols
    s: list = field(default_factory=list)   # S lines below
    seq: bytes = b""
    rev: bytes = b""
    verbatim: bool = False   # fully underlined (%macro text, wraps mid-token)


@dataclass
class Statement:
    stmt: bytes
    rows: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


_LEVEL = re.compile(rb"^ (\d{1,2})  +(?=\S)")
_SIMPLE_EXP = re.compile(rb"^[A-Z0-9_.$#@]+$")


def _exp(etext):
    """1-D form of an exponent harvested from an E-line gap."""
    if _SIMPLE_EXP.match(etext):
        return b"**" + etext
    if etext.startswith(b"(") and etext.endswith(b")"):
        return b"**" + etext
    return b"**(" + etext + b")"


class HalDeformatter:
    """One statement's E/M/S rows -> 1-D HAL/S source text + comment.

    The output writer prints one script level 2-D (E line above for
    exponents, S line below for subscripts -- deeper levels appear
    inline in $/**-notation already); annotation overmarks (. - * + ,)
    sit over non-blank code columns and are dropped.  Trailing /*...*/
    comments of the statement's cards are merged into a band that wraps
    onto following S rows re-prefixed with '/*' (which overwrites two
    characters of comment text -- unrecoverable from the listing alone).
    """

    def __init__(self, st: Statement):
        self.st = st
        self.code = []       # code text fragments
        self.comments = []   # closed comment texts (without delimiters)
        self.cur = None      # open comment text, or None
        self.rowend_close = False   # last '*/' sat at the physical row end
        self.in_string = False
        self.level = None    # DO-level marker digits, once seen

    def warn(self, msg):
        self.st.warnings.append(msg)

    def run(self):
        for ri, row in enumerate(self.st.rows):
            self.row(ri, row)
        if self.cur is not None:
            if not self.rowend_close:
                self.warn("unterminated comment")
            self.comments.append(self.cur)
            self.cur = None
        code = _collapse_blanks(b"".join(self.code))
        comment = b" ".join(b"/*" + c + b"*/" for c in self.comments)
        if self.in_string:
            self.warn("unterminated string")
        return code.strip(), comment

    # -- comment band ----------------------------------------------------

    def band_close(self, at_rowend):
        if at_rowend:
            self.rowend_close = True     # may be a wrap; reopened by '/*'
        else:
            self.comments.append(self.cur)
            self.cur = None

    def band_reopen_or_new(self):
        """A '/*' when a row-end '*/' is pending: glue (wrap artifact)."""
        if self.cur is not None and self.rowend_close:
            self.rowend_close = False    # wrapped: 'U*/' + '/*SED' = 'USED'
        elif self.cur is None:
            self.cur = b""
        self.rowend_close = False

    def band_flush_pending(self):
        """A row starts fresh while a row-end '*/' was pending: the
        comment really did end at that row's edge."""
        if self.cur is not None and self.rowend_close:
            self.comments.append(self.cur)
            self.cur = None
            self.rowend_close = False

    # -- script (subscript/exponent) harvesting --------------------------

    def script_text(self, bands, level, a, b):
        """1-D text of script row `level` within gap columns [a, b)."""
        band = bands[level].ljust(WIDTH)[a:b] if level < len(bands) else b""
        if not band.strip():
            return b""
        out = []
        i = 0
        n = len(band)
        while i < n:
            ch = band[i:i+1]
            if ch != b" ":
                out.append(ch)
                i += 1
                continue
            j = i
            while j < n and band[j:j+1] == b" ":
                j += 1
            deeper = self.script_text(bands, level + 1, a + i, a + j)
            if deeper:
                out.append(b"$(" + deeper + b")")
            if j < n and out:
                out.append(b" ")
            i = j
        return b"".join(out).strip()

    # -- one M row --------------------------------------------------------

    def row(self, ri, row):
        m = row.m.ljust(WIDTH)
        e = (row.e or b"").ljust(WIDTH)
        srows = row.s
        start = 1              # content[0] is an inserted blank
        # DO-level marker at content[1]: printed on the first code row
        # (a label-only row has none) and repeated on continuation rows
        lvl = _LEVEL.match(m)
        if lvl:
            if self.level in (None, lvl.group(1)):
                self.level = lvl.group(1)
                start = lvl.end()
        i = start
        if self.cur is not None and self.rowend_close:
            # pending row-end close: only a '/*' re-prefix continues it
            nb = m.strip()
            if not nb.startswith(b"/*"):
                self.band_flush_pending()
        if not self.in_string and self.cur is None:
            while i < WIDTH and m[i:i+1] == b" " and e[i:i+1] == b" " \
                    and not any(s[i:i+1].strip() for s in
                                (x.ljust(WIDTH) for x in srows[:1])):
                i += 1
        frag = []
        while i < WIDTH:
            ch = m[i:i+1]
            if self.cur is not None and not self.rowend_close:
                if m[i:i+2] == b"*/":
                    self.band_close(at_rowend=(i == WIDTH - 2))
                    i += 2
                    continue
                self.cur += ch
                i += 1
                continue
            if m[i:i+2] == b"/*":
                self.band_reopen_or_new()
                i += 2
                continue
            if self.in_string:
                frag.append(ch)
                if ch == b"'":
                    self.in_string = False
                i += 1
                continue
            if ch == b"'":
                self.in_string = True
                frag.append(ch)
                i += 1
                continue
            if ch in b"[]{}":     # writer's REMOTE/NAME reference marks
                i += 1
                continue
            if ch != b" ":
                frag.append(ch)
                i += 1
                continue
            # blank gap: harvest subscript/exponent text
            j = i
            while j < WIDTH and m[j:j+1] == b" ":
                j += 1
            stext = b""
            if srows and srows[0].ljust(WIDTH)[i:j].strip():
                seg = srows[0].ljust(WIDTH)[i:j]
                if b"/*" in seg:
                    self.warn(f"comment band inside code gap: {seg.strip()[:30]!r}")
                else:
                    stext = self.script_text(srows, 0, i, j)
            etext = e[i:j].strip()
            if stext:
                frag.append(b"$(" + stext + b")")
            if etext:
                frag.append(_exp(etext))
            if j < WIDTH or self.in_string:
                frag.append(b" ")
            i = j
        text = b"".join(frag)
        # row seam: tokens separated by a space, except verbatim macro
        # rows (wrap mid-token) and '.'-qualified name wraps
        if text.startswith(b".") and self.code and self.code[-1].endswith(b" "):
            self.code[-1] = self.code[-1][:-1]
        if not self.in_string and text and not text.endswith((b" ", b".")) \
                and not row.verbatim:
            text += b" "
        self.code.append(text)
        if self.cur is not None and not self.rowend_close:
            self.cur += b" "     # row seam inside an unwrapped comment
        # S rows: the comment band continues on them, re-prefixed '/*'
        for sr in srows:
            idx = sr.find(b"/*")
            if idx < 0:
                continue
            if self.cur is None and not self.comments:
                self.warn(f"comment band without open comment: "
                          f"{sr[idx:idx+30]!r}")
                continue
            self.band_reopen_or_new()
            seg = sr.ljust(WIDTH)[idx + 2:]
            k = seg.find(b"*/")
            if k >= 0:
                self.cur += seg[:k]
                self.band_close(at_rowend=(idx + 2 + k == WIDTH - 2))
            else:
                self.cur += seg.rstrip() + b" "


def _collapse_blanks(text):
    """Collapse blank runs to one space, except inside '...' strings."""
    out = []
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i:i+1]
        if in_str:
            out.append(ch)
            if ch == b"'":
                in_str = False
            i += 1
            continue
        if ch == b"'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == b" ":
            j = i
            while j < n and text[j:j+1] == b" ":
                j += 1
            out.append(b" ")
            i = j
            continue
        out.append(ch)
        i += 1
    return b"".join(out)


# ----------------------------------------------------- HAL member -> deck


def _wrap_points(text, limit):
    """Split text into <=limit chunks, breaking at blanks outside strings."""
    chunks = []
    in_str = False
    while len(text) > limit:
        brk = -1
        st = in_str
        for i in range(limit + 1):
            ch = text[i:i+1]
            if st:
                if ch == b"'":
                    st = False
            elif ch == b"'":
                st = True
            elif ch == b" " and i <= limit:
                brk = i
        if brk <= 0:
            brk = limit          # hard break (long string)
            # recompute string state at the hard break
            st = in_str
            for i in range(brk):
                if text[i:i+1] == b"'":
                    st = not st
        else:
            st = in_str
            for i in range(brk):
                if text[i:i+1] == b"'":
                    st = not st
        chunks.append(text[:brk].rstrip() if not st else text[:brk])
        text = text[brk:] if st else text[brk:].lstrip()
        in_str = st
    if text.strip() or not chunks:
        chunks.append(text.rstrip())
    return chunks


def statement_cards(code, comment, seq, rev):
    """Wrap a de-formatted statement into 80-column cards."""
    text = code
    if comment:
        text = (code + b" " + comment).strip() if code else comment
    cards = []
    for k, chunk in enumerate(_wrap_points(text, 71)):
        s = seq if k == 0 else b""
        r = rev if k == 0 else b""
        cards.append(b" " + chunk.ljust(71) + s.ljust(6) + r.ljust(2))
    return cards


class HalMember:
    def __init__(self, lines, acct, member):
        self.lines = lines
        self.acct = acct
        self.member = member
        self.cards = []
        self.warnings = []

    def flush(self, st, pending_c):
        if st is not None:
            code, comment = HalDeformatter(st).run()
            for w in st.warnings:
                self.warnings.append(f"stmt {st.stmt.decode()}: {w}")
            row0 = st.rows[0]
            self.cards.extend(statement_cards(code, comment, row0.seq, row0.rev))
        for hl in pending_c:
            card = hl.typ + hl.content[1:72].ljust(71) \
                + hl.seq.ljust(6) + hl.rev.ljust(2)
            # keep cards in sequence order: a C/D card printed after the
            # statement it belongs before (the writer prints statements
            # as a unit) is moved back to its sequence position; card
            # groups (statement + blank-seq continuations) stay together
            pos = len(self.cards)
            if hl.seq.isdigit():
                while pos > 0:
                    gstart = pos
                    while gstart > 0 and not self.cards[gstart - 1][72:78].strip():
                        gstart -= 1
                    if gstart == 0:
                        break
                    gseq = self.cards[gstart - 1][72:78]
                    if gseq.isdigit() and int(gseq) > int(hl.seq):
                        pos = gstart - 1
                    else:
                        break
            self.cards.insert(pos, card)
        pending_c.clear()

    def run(self):
        st = None
        pending_e = None
        pending_c = []
        last_stmt_row = False
        for hl in self.lines:
            if hl.included:
                self.acct.add("hal-included")
                continue
            if hl.typ not in (b"E", b"_"):
                last_stmt_row = hl.typ in (b"M", b"S")
            if hl.typ in (b"C", b"D"):
                if hl.content[72:].strip():
                    self.warnings.append(
                        f"line {hl.lineno}: {hl.typ.decode()}-card text past "
                        f"col 72: {hl.content[72:].strip()[:30]!r}")
                if st is None:
                    self.flush(None, [hl])
                else:
                    pending_c.append(hl)   # comment card amid a statement
                continue
            if hl.typ == b"E":
                if pending_e is not None:
                    self.warnings.append(f"line {hl.lineno}: stacked E rows")
                pending_e = hl.content
                continue
            if hl.typ == b"S":
                if st is None or not st.rows:
                    if hl.content.strip():
                        self.warnings.append(
                            f"line {hl.lineno}: orphan S row")
                else:
                    st.rows[-1].s.append(hl.content)
                continue
            if hl.typ == b"_":
                # overprint underline; a full-width one marks verbatim
                # macro text that wraps mid-token at the row seam
                if st is not None and st.rows and last_stmt_row:
                    mrow = st.rows[-1]
                    mend = len(mrow.m.rstrip())
                    uend = len(hl.content.rstrip())
                    if mend > 2 and uend >= mend:
                        mrow.verbatim = True
                continue
            # M row
            if st is not None and hl.stmt == st.stmt:
                st.rows.append(MRow(hl.content, pending_e, [], hl.seq, hl.rev))
            else:
                self.flush(st, pending_c)
                st = Statement(hl.stmt)
                st.rows.append(MRow(hl.content, pending_e, [], hl.seq, hl.rev))
            pending_e = None
        self.flush(st, pending_c)
        return self.cards


# --------------------------------------------------------- verification

_TOKEN = re.compile(rb"'(?:[^']|'')*'|[A-Z0-9_.#@]+|.")


def code_words(cards):
    """Lexical token stream of a deck's code cards, comments stripped."""
    return code_words_prov(cards)[0]


def code_words_prov(cards):
    """(tokens, card-index-per-token) of a deck's code cards.

    D SPACE / D EJECT cards are listing-invisible and excluded, so their
    absence from a recovered deck does not break token alignment."""
    idx = [k for k, c in enumerate(cards) if c[:1] in (b" ", b"D")
           and not _INVISIBLE.match(c)]
    text = b"\n".join(cards[k][1:72] for k in idx)
    # strings first, then strip comments (string-aware scan)
    out = []
    pos = []
    i, n = 0, len(text)
    in_c = False
    while i < n:
        ch = text[i:i+1]
        if in_c:
            if text[i:i+2] == b"*/":
                in_c = False
                i += 2
                continue
            i += 1
            continue
        if text[i:i+2] == b"/*":
            in_c = True
            i += 2
            continue
        if ch == b"'":
            j = i + 1
            while j < n:
                if text[j:j+1] == b"'":
                    if text[j+1:j+2] == b"'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j+1])
            pos.append(i)
            i = j + 1
            continue
        if ch in b" \n":
            i += 1
            continue
        m = _TOKEN.match(text, i)
        out.append(m.group(0))
        pos.append(i)
        i = m.end()
    # canonicalize $ ( T ) -> $ T and * * ( T ) -> * * T
    toks = []
    prov = []
    k = 0
    while k < len(out):
        card = idx[pos[k] // 72]
        if out[k] in (b"$", b"*") and k + 3 < len(out) and out[k+1] == b"(" \
                and out[k+3] == b")" and (out[k] == b"$" or
                                          (toks and toks[-1] == b"*")):
            toks += [out[k], out[k+2]]
            prov += [card, idx[pos[k+2] // 72]]
            k += 4
            continue
        toks.append(out[k])
        prov.append(card)
        k += 1
    return toks, prov


# ------------------------------------------------------------ splice pass


def _card_bounds(prov, ncards):
    """first/last token index per card (cards with no tokens -> None)."""
    first = {}
    last = {}
    for t, c in enumerate(prov):
        first.setdefault(c, t)
        last[c] = t
    return first, last


_INVISIBLE = re.compile(rb"^D +(SPACE|EJECT)\b")


def _expand_macros(toks, prov, notes):
    """Expand `NAME` macro invocations using the deck's own REPLACE
    definitions (the listing shows the expansion, the source the name)."""
    defs = {}
    params = set()
    for k in range(len(toks) - 4):
        if toks[k] == b"REPLACE" and toks[k+2] == b"BY" and toks[k+3] == b'"':
            name = toks[k+1]
            e = k + 4
            body = []
            while e < len(toks) and toks[e] != b'"':
                body.append((toks[e], prov[e]))
                e += 1
            defs[name] = body
        if toks[k] == b"REPLACE" and toks[k+2] == b"(":
            params.add(toks[k+1])
    if not defs:
        return toks, prov
    for _ in range(3):
        out, op = [], []
        changed = False
        k = 0
        while k < len(toks):
            if toks[k] == b"`" and k + 2 < len(toks) and toks[k+2] == b"`" \
                    and toks[k+1] in defs and toks[k+1] not in params:
                for t, _c in defs[toks[k+1]]:
                    out.append(t)
                    op.append(prov[k])
                k += 3
                changed = True
                continue
            out.append(toks[k])
            op.append(prov[k])
            k += 1
        toks, prov = out, op
        if not changed:
            break
    return toks, prov


def splice(ours, ref, notes):
    """Adopt ref cards for regions whose code-token stream matches.

    Recovers original card boundaries, continuation seq/rev fields,
    comment spacing, version-letter cards, macro-invocation forms, and
    unlisted control cards (D SPACE / D EJECT) for code unchanged
    between the two releases.  Guarded per region by seq/rev agreement,
    with recursive bisection so a single changed card only blocks its
    own neighborhood; changed regions keep the de-formatted cards.
    """
    import difflib
    at, ap = code_words_prov(ours)
    bt, bp = code_words_prov(ref)
    bt, bp = _expand_macros(bt, bp, notes)
    if not at and not bt:
        # commentary-only member: adopt wholesale when card identities
        # (seq, rev) agree exactly in both directions
        oid = [(c[72:78], c[78:80]) for c in ours if c[72:78].strip()]
        rid = [(c[72:78], c[78:80]) for c in ref
               if c[72:78].strip() and not _INVISIBLE.match(c)]
        if oid == rid:
            return list(ref)
        notes.append("splice: comment-only member differs from ref")
        return ours
    afirst, alast = _card_bounds(ap, len(ours))
    bfirst, blast = _card_bounds(bp, len(ref))

    regions = []                 # (our-card-start, our-card-end, ref-card-range)

    def guard(ca1, ca2, cb1, cb2):
        oseq = {c[72:78]: c[78:80] for c in ours[ca1:ca2 + 1]
                if c[72:78].strip().isdigit()}
        for q, r in oseq.items():
            hit = [c for c in ref[cb1:cb2 + 1] if c[72:78] == q]
            if not hit or hit[0][78:80] != r:
                return q
        for cb in range(cb1, cb2 + 1):
            c = ref[cb]
            q = c[72:78]
            # a zero-token ref card whose seq we never saw is an
            # addition -- unless it is invisible in listings anyway
            if q.strip().isdigit() and q not in oseq \
                    and cb not in bfirst and not _INVISIBLE.match(c):
                return q
        return None

    def try_region(s, e, joff):
        """Equal token span [s,e) (ours) matching [s+joff, e+joff) (ref):
        trim to card boundaries, guard, split at the offending card on
        failure so one changed card only blocks itself."""
        while s < e:
            ca, cb = ap[s], bp[s + joff]
            if afirst[ca] == s and bfirst[cb] == s + joff:
                break
            s += 1
        while e > s:
            ca, cb = ap[e - 1], bp[e - 1 + joff]
            if alast[ca] == e - 1 and blast[cb] == e - 1 + joff:
                break
            e -= 1
        if e <= s:
            return
        ca1, ca2 = ap[s], ap[e - 1]
        cb1, cb2 = bp[s + joff], bp[e - 1 + joff]
        bad = guard(ca1, ca2, cb1, cb2)
        if bad is None:
            regions.append((ca1, ca2, cb1, cb2))
            return
        notes.append(f"splice: kept OI30 text at seq {bad.decode()}")
        # left part: tokens before the bad card; right: tokens after it
        badcards = [k for k in range(ca1, ca2 + 1)
                    if ours[k][72:78] == bad]
        if badcards:
            cbad = badcards[0]
            t1 = next((t for t in range(s, e) if ap[t] >= cbad), e)
            t2 = next((t for t in range(t1, e) if ap[t] > cbad), e)
        else:
            rbad = [k for k in range(cb1, cb2 + 1) if ref[k][72:78] == bad][0]
            t1 = next((t for t in range(s, e) if bp[t + joff] >= rbad), e)
            t2 = next((t for t in range(t1, e) if bp[t + joff] > rbad), e)
        if t1 <= s and t2 >= e:
            return
        try_region(s, t1, joff)
        try_region(t2, e, joff)

    sm = difflib.SequenceMatcher(a=at, b=bt, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            try_region(i1, i2, j1 - i1)

    def merge_gap(acards, rcards):
        """Interleave kept OI30 cards with listing-invisible ref cards
        by sequence number (blank-seq continuations follow their card)."""
        rq = [c for c in rcards if _INVISIBLE.match(c)]
        out = []
        ai = 0
        for c in rq:
            q = c[72:78]
            while ai < len(acards):
                aq = acards[ai][72:78]
                if aq.strip().isdigit() and int(aq) > int(q):
                    break
                out.append(acards[ai])
                ai += 1
            out.append(c)
        out.extend(acards[ai:])
        return out

    out = []
    acur = 0
    bcur = 0
    for ca1, ca2, cb1, cb2 in sorted(regions):
        if ca1 < acur:
            continue
        out.extend(merge_gap(ours[acur:ca1], ref[bcur:cb1]))
        out.extend(ref[cb1:cb2 + 1])
        acur = ca2 + 1
        bcur = cb2 + 1
    out.extend(merge_gap(ours[acur:], ref[bcur:]))
    return out


_LISTCTL = re.compile(rb"^[A-Z@#$0-9&]* +(TITLE|SPACE|EJECT|PRINT) ")


def asm_splice(ours, stmts, gens, ref, notes):
    """Restore cards an assembler listing does not echo, from a matching
    reference deck: TITLE/SPACE/EJECT/PRINT cards, and whole PRINT OFF
    regions.  A statement-number gap (minus macro-generated lines) at
    the insertion point proves the listing hid that many cards there;
    without a gap, reference-only cards are release additions and stay
    out."""
    import difflib
    sm = difflib.SequenceMatcher(a=ours, b=ref, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            if i1 < len(stmts):
                prev = stmts[i1 - 1] if i1 > 0 else 0
                gap = stmts[i1] - prev - 1 - gens[i1]
            else:
                gap = 0
            if gap >= (j2 - j1) or all(_LISTCTL.match(c) for c in ref[j1:j2]):
                out.extend(ref[j1:j2])
                if not all(_LISTCTL.match(c) for c in ref[j1:j2]):
                    notes.append(f"adopted {j2-j1} unlisted cards "
                                 f"(PRINT OFF region) at {ref[j1][72:78].decode(errors='replace')}")
            else:
                notes.append(f"ref-only cards not adopted: {ref[j1][:40]!r}")
        else:
            out.extend(ours[i1:i2])
    return out


# ------------------------------------------------------- DFG display decks

_DFG_MARK = re.compile(rb"\*+\s+DFG INPUT\s+\*+")
_DFG_TRAILER = re.compile(rb"\s*\*+\s*(REFERENCED COMPOOLS|BEGIN COMPOOL)")


def dfg_deck(lines):
    """Display-format members: the DFG-generated COMPOOL listing echoes
    the DFG's input deck in its comments; the source PDS member (as in
    the reference tree) is that input deck, so recover it instead of the
    generated compool body.  Returns None for non-DFG members."""
    start = None
    for k, hl in enumerate(lines):
        if hl.typ == b"C" and _DFG_MARK.search(hl.content):
            start = k + 1
            break
    if start is None:
        return None
    cards = []
    for hl in lines[start:]:
        if hl.typ == b"_":
            continue
        if hl.typ != b"C" or _DFG_TRAILER.match(hl.content[1:]):
            break
        cards.append(hl.content[1:73].ljust(72) + b" " * 8)
    while cards and not cards[-1].strip():
        cards.pop()
    return cards


def dfg_splice(ours, ref, notes):
    """Text-matched card adoption for display decks (the echo carries no
    seq/rev fields; matching reference cards supply them)."""
    import difflib
    a = [c[:72].rstrip() for c in ours]
    b = [c[:72].rstrip() for c in ref]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ref[j1:j2])
        else:
            out.extend(ours[i1:i2])
    return out


# ------------------------------------------------------------ entry point


def convert_member(path, member):
    with open(path, "rb") as f:
        data = f.read()
    acct = Account()
    kind = detect_kind(data, member)
    if kind == "empty":
        return kind, [], acct
    if kind == "asm":
        am = AsmMember(data, acct, member)
        am.run()
        return kind, am, acct
    if kind == "hal":
        lines = list(parse_hal_member(data, acct))
        deck = dfg_deck(lines)
        if deck is not None:
            return "dfg", (deck, []), acct
        hm = HalMember(lines, acct, member)
        cards = hm.run()
        return kind, (cards, hm.warnings), acct
    return kind, [], acct


def read_cards(path):
    with open(path, "rb") as f:
        return [l.rstrip(b"\r\n").ljust(80) for l in f.read().split(EOL) if l.strip()]


def write_cards(path, cards):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for c in cards:
            f.write(c)
            f.write(EOL)


def main():
    import argparse
    import collections
    ap = argparse.ArgumentParser(
        description="Recover source card decks from compiler/assembler "
                    "listings (see module docstring).")
    ap.add_argument("root", help="listing tree, e.g. OI301700.listing")
    ap.add_argument("-o", "--out", help="write recovered decks here")
    ap.add_argument("--ref", help="reference source tree (e.g. OI340600) "
                                  "for splicing and verification")
    ap.add_argument("--report", help="write per-member report file")
    args = ap.parse_args()

    kinds = collections.Counter()
    stats = collections.Counter()
    report = []
    all_copies = {}              # COPY members recovered from echoes
    for d in sorted(os.listdir(args.root)):
        dd = os.path.join(args.root, d)
        if not os.path.isdir(dd):
            continue
        for fn in sorted(os.listdir(dd)):
            p = os.path.join(dd, fn)
            if not os.path.isfile(p):
                continue
            kind, out, acct = convert_member(p, fn)
            kinds[kind] += 1
            notes = [f"unknown line {ln}: {l[:60]!r}"
                     for ln, l in acct.oddities]
            rp = os.path.join(args.ref, d, fn) if args.ref else None
            refcards = read_cards(rp) if rp and os.path.exists(rp) else None
            copies = {}
            if kind == "hal":
                cards, warnings = out
                notes += warnings
                stats["hal-warnings"] += len(warnings)
                if refcards is not None:
                    if code_words(cards) == code_words(refcards):
                        stats["hal-words-identical"] += 1
                    else:
                        stats["hal-words-differ"] += 1
                    cards = splice(cards, refcards, notes)
                    if cards == refcards:
                        stats["hal-final-identical-to-ref"] += 1
            elif kind == "dfg":
                cards, _ = out
                if refcards is not None:
                    cards = dfg_splice(cards, refcards, notes)
                    if cards == refcards:
                        stats["dfg-final-identical-to-ref"] += 1
                    else:
                        stats["dfg-differs"] += 1
            elif kind == "asm":
                am = out
                cards = am.cards
                copies = am.copies
                if refcards is not None:
                    if cards == refcards:
                        stats["asm-identical"] += 1
                    cards = asm_splice(cards, am.stmts, am.gens,
                                       refcards, notes)
                    if cards == refcards:
                        stats["asm-final-identical-to-ref"] += 1
            else:
                cards = []
            if refcards is None and kind != "empty":
                stats[f"{kind}-noref"] += 1
            for cname, ccards in copies.items():
                if cname in all_copies and all_copies[cname][0] != ccards:
                    notes.append(f"COPY member {cname} echo differs from "
                                 f"the one in {all_copies[cname][1]}")
                else:
                    all_copies.setdefault(cname, (ccards, fn))
            if args.out:
                write_cards(os.path.join(args.out, d, fn), cards)
            report.append((p, kind, len(cards), notes))
            if notes:
                stats["members-with-notes"] += 1
    # COPY members echoed inside assembler listings -> MLIB80
    for cname, (ccards, src) in sorted(all_copies.items()):
        notes = []
        rp = os.path.join(args.ref, "MLIB80", cname) if args.ref else None
        if rp and os.path.exists(rp):
            refcards = read_cards(rp)
            ccards = asm_splice(ccards, [], [], refcards, notes)
            if ccards == refcards:
                stats["mlib80-final-identical-to-ref"] += 1
            else:
                stats["mlib80-differs"] += 1
        else:
            stats["mlib80-noref"] += 1
        if args.out:
            write_cards(os.path.join(args.out, "MLIB80", cname), ccards)
        report.append((f"(COPY echo in {src})", "mlib80", len(ccards), notes))
    print(kinds)
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if args.report:
        with open(args.report, "w") as f:
            for p, kind, n, notes in report:
                f.write(f"{p} [{kind}] {n} cards\n")
                for note in notes:
                    f.write(f"    {note}\n")
        print(f"report: {args.report}")


if __name__ == "__main__":
    main()
