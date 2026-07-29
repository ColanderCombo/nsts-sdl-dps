#!/usr/bin/env python3
"""cdqanngen -- regenerate the FMPT-generated blocks of SSSRC/CDQANNUN.

CDQ_ANNUN is the SM/PL RECONFIGURABLE annunciation compool (DMTERR fault
message formatting).  For every OI/mission the SM/PL Preprocessor (SMPP)
annunciation pass re-emitted the ``F GEN`` / ``F END`` blocks of the
CDQANNUN source member from that mission's fault message definitions.

NOTE ON THE NAME "FMPT": FMPT is the *flight data structure* -- the Fault
Message Parameter Table (one entry = an FMP) -- attested verbatim in
``INCL80/STRPDT`` and ``INCL80/STRPCT`` ("INDEX INTO THE FAULT MESSAGE
PARAMETER TABLE") and in the symbols ``CSAS_PDTA_FMPT_INDEX`` /
``STM_FMPT_INDEX`` / ``FMPT_CLASS5``.  There is NO ground tool named FMPT;
earlier revisions of this file glossed it "Fault Message Preparation Tool",
which is unattested anywhere in the delivered tree or the doc archive.  The
generator is one pass of SMPP.  ``fmpt`` survives below only as the
subcommand name (it builds the FMPT).  Blocks emitted:

  1. CDQV_INDEX_ARRAY(4) INTEGER   -- [#mask hw, #FMPTs, 1-based hw index of
                                       majors, 1-based hw index of minors]
  2. CDQV_PL_MASK ARRAY(n) BIT(16) -- FMPT enable mask, 16 FMPTs per hw
  3. CDQK_PL_FMPS  (n)  2 hw each  -- DENSE RIGID: RESERVE(1) CODE(5)
                                       MAJOR(10) MINOR(8) IND(4) CLASS(4);
                                       MAJOR/MINOR are 1-based indices into
                                       the majors/minors tables
  4. CDQK_PL_MAJORS (n) 8 hw each  -- 7 FCWs + msg-id INTEGER
  5. CDQK_PL_MINORS (n) 3 hw each  -- 2 FCWs + msg-id INTEGER
  6. REPLACE S73_PL_... statements -- compile-time FMPT index macros

FCW (DEU format control word) encoding, derived empirically from the
member's own text comments vs its HEX literals (verified over all 150+59
message texts of the OI34.06-era snapshot with zero conflicts):

    FCW<15:14> = attribute (0b11 on every message word; 0b00 only in pads)
    FCW<13:7>  = first character,  7-bit ASCII
    FCW<6:0>   = second character, 7-bit ASCII

Each table ends with 5 all-zero PAD (spare) entries, rendered
``5#(...)`` by the generator.

Subcommands
-----------
  decode     DASS .ASC listing -> definitions JSON  (flight truth)
  extract    CDQANNUN source member -> definitions JSON
  generate   definitions JSON + template member -> regenerated member
  diff       two definitions JSONs -> structural comparison
  fmpt       FMPT input catalogs + template member -> regenerated member
  mkcatalog  definitions JSON -> FMPT input catalog (original card format)
  validate   FMPT input catalogs vs definitions JSON -> coverage report

FMPT input card formats (pinned from the delivered TOOLIN catalogs
GPCFMSGS / SMDNCSET / SMORBSET / SMPLSET / GPCFDATA; columns 1-based):

  G  major-text card   cols 1-3  message number (decimal, = major msg id)
                       cols 5-18 the 14-char major text, verbatim
                       col 70 'G'
  H  minor-text card   cols 1-2  minor code (2 hex digits, = minor msg id)
                       cols 4-7  the 4-char minor text, verbatim
                       col 70 'H'
  C1 FDA limit card    cols 1-9 MSID (cols 11+ variant tag), col 70 'C',
     (annunciation)    col 72 '1'; annunciation ref cols 25-29 =
                       <mmm major><NN minor hex>, col 31 class,
                       col 34 explicit IND (delivered decks: '0' on every
                       class-2 card, absent on class 3)

Derivation rules (validated against the delivered OI34.06-era decks and
the flight OI34.07 DASS decode -- see cmd_validate):

  * FMPT #1 is the fixed record (100 ILLEGAL ENTRY, minor 00, class 5).
  * Each G card of the GPCF catalog (GPCFMSGS) expands to a class-2 and a
    class-3 FMPT (minor 77 GPCF), in G-card order, plus a compile-time
    REPLACE macro per FMPT named <14-char text, blanks -> '_'>_<class>.
  * Every further FMPT comes from a C1-card annunciation ref, first
    occurrence wins (global dedupe on (major, minor, class)); RESERVE=1,
    CODE=0 on every real record.
  * The majors/minors tables are built in order of first FMPS reference;
    texts are looked up in the G/H catalogs by message number/code.
  * CDQV_PL_MASK covers ceil(#FMPT/16)*16 bits; a real FMPT's bit is set
    unless its class is 5 (only #1); pad bits are 0.
  * Each table gets 5 all-zero PAD entries; the index array follows.

The FMPS record ORDER (hence table order) beyond the GPCF block is the SM
preprocessor's internal parameter-database order, which is NOT fully
recoverable from the delivered decks (deck-order scan reproduces the
34.06 content 273/273 but only ~LCS 204/273 of the order); the mkcatalog
output therefore carries the annunciation cards in explicit FMPS order.

The OI34.07 FMPT inputs are absent from the delivered PDTIN/PSFIN/TOOLIN;
the reconstructed catalog is DASS-derived (mkcatalog marks each card
corroborated 'CO' / DASS-only 'DA' in cols 79-80).
"""

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# FCW codec
# --------------------------------------------------------------------------

def fcw_decode(word: int) -> tuple[int, str]:
    """16-bit FCW -> (attr, 2 chars)."""
    attr = (word >> 14) & 0x3
    return attr, chr((word >> 7) & 0x7F) + chr(word & 0x7F)


def fcw_encode(two: str, attr: int = 0b11) -> int:
    assert len(two) == 2
    return (attr << 14) | ((ord(two[0]) & 0x7F) << 7) | (ord(two[1]) & 0x7F)


def fcws_text(fcws) -> str:
    return "".join(fcw_decode(int(h, 16))[1] for h in fcws)


# --------------------------------------------------------------------------
# decode: DASS listing -> definitions
# --------------------------------------------------------------------------

FMP_FIELDS = ("res", "code", "major", "minor", "ind", "cls")


def unpack_fmp(w1: int, w2: int) -> dict:
    full = (w1 << 16) | w2
    return dict(res=(full >> 31) & 1, code=(full >> 26) & 0x1F,
                major=(full >> 16) & 0x3FF, minor=(full >> 8) & 0xFF,
                ind=(full >> 4) & 0xF, cls=full & 0xF)


def decode_words(vals, provenance: str) -> dict:
    """Halfword list (whole #PCDQANN csect) -> definitions dict."""
    nmask, nfmp, imaj, imin = vals[0:4]
    assert imaj == nmask + 2 * nfmp + 1, "index array inconsistent"
    assert (imin - imaj) % 8 == 0, "majors region not 8 hw records"
    nmaj = (imin - imaj) // 8
    off = 4 + nmask
    masks = [f"{v:04X}" for v in vals[4:off]]
    fmps = [unpack_fmp(vals[off + 2 * i], vals[off + 2 * i + 1])
            for i in range(nfmp)]
    off += 2 * nfmp
    majors = []
    for i in range(nmaj):
        ws = vals[off + 8 * i: off + 8 * i + 8]
        majors.append(dict(fcws=[f"{w:04X}" for w in ws[:7]], id=ws[7],
                           text=fcws_text([f"{w:04X}" for w in ws[:7]])))
    off += 8 * nmaj
    assert (len(vals) - off) % 3 == 0, "minors region not 3 hw records"
    nmin = (len(vals) - off) // 3
    minors = []
    for i in range(nmin):
        ws = vals[off + 3 * i: off + 3 * i + 3]
        minors.append(dict(fcws=[f"{w:04X}" for w in ws[:2]], id=ws[2],
                           text=fcws_text([f"{w:04X}" for w in ws[:2]])))
    return dict(provenance=provenance, masks=masks, fmps=fmps,
                majors=majors, minors=minors)


def cmd_decode(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.fidelity.dassoracle import DassOracle
    o = DassOracle.load(args.dass)
    cs = [c for c in o.csects if c.name == args.csect]
    if not cs:
        sys.exit(f"csect {args.csect} not found in {args.dass}")
    c = cs[0]
    vals = []
    for a in range(c.start, c.start + c.size):
        v = o.words.get(a)
        if v is None:
            sys.exit(f"missing halfword at {a:06X} in DASS dump")
        vals.append(v)
    defs = decode_words(
        vals, f"decoded from {Path(args.dass).name} csect {args.csect} "
              f"@{c.start:06X} size {c.size} hw (flight truth)")
    Path(args.output).write_text(json.dumps(defs, indent=1))
    print(f"{args.csect}: {len(defs['masks'])} masks, {len(defs['fmps'])} "
          f"FMPs, {len(defs['majors'])} majors, {len(defs['minors'])} minors"
          f" -> {args.output}")


# --------------------------------------------------------------------------
# extract: source member -> definitions
# --------------------------------------------------------------------------

def gen_blocks(body):
    """[(start,end)] line index ranges of F GEN..F END block interiors."""
    blocks, i = [], 0
    while i < len(body):
        if body[i].startswith("F GEN"):
            j = i + 1
            while not body[j].startswith("F END"):
                j += 1
            blocks.append((i + 1, j))
            i = j
        i += 1
    return blocks


def parse_member(text: str) -> dict:
    body = [ln[:72] for ln in text.splitlines()]
    blocks = gen_blocks(body)
    assert len(blocks) == 6, f"expected 6 F GEN blocks, got {len(blocks)}"
    blk = lambda k: body[blocks[k][0]:blocks[k][1]]

    idx = [int(x) for x in re.findall(r"\d+", " ".join(blk(0)))]

    masks = []
    for cnt, val in re.findall(r"(?:(\d+)#)?HEX'([0-9A-F]+)'",
                               " ".join(blk(1))):
        masks += [val] * (int(cnt) if cnt else 1)

    b = " ".join(l for l in blk(2) if not l.startswith("C")
                 and "DECLARE" not in l)
    vals = []
    for dec, pc, pn, pv in re.findall(
            r"DEC'(\d+)'|(\d+)#\((\d+)#DEC'(\d+)'\)", b):
        if dec != "":
            vals.append(int(dec))
        else:
            vals += [int(pv)] * (int(pc) * int(pn))
    assert len(vals) % 6 == 0
    fmps = [dict(zip(FMP_FIELDS, vals[i:i + 6]))
            for i in range(0, len(vals), 6)]

    def recs(lines, nfcw):
        b = " ".join(l for l in lines if not l.startswith("C")
                     and "DECLARE" not in l)
        toks = re.findall(
            r"HEX'([0-9A-F]{4})'|(\d+)#\((\d+)#HEX'0000',0\)"
            r"|(?<![#'A-F0-9])(\d+)(?![#0-9])", b)
        seq = []
        for h, pc, pn, n in toks:
            if h:
                seq.append(("h", h))
            elif pc:
                for _ in range(int(pc)):
                    seq += [("h", "0000")] * int(pn) + [("i", 0)]
            else:
                seq.append(("i", int(n)))
        out, i = [], 0
        while i < len(seq):
            fcws = [seq[i + k][1] for k in range(nfcw)]
            assert all(seq[i + k][0] == "h" for k in range(nfcw))
            assert seq[i + nfcw][0] == "i"
            out.append(dict(fcws=fcws, id=seq[i + nfcw][1],
                            text=fcws_text(fcws)))
            i += nfcw + 1
        return out

    majors = recs(blk(3), 7)
    minors = recs(blk(4), 2)
    reps = [(m.group(1), int(m.group(2))) for m in
            (re.match(r' REPLACE (\S+) BY "(\d+)";', l) for l in blk(5)) if m]

    # cross-check the index array against the parsed tables
    assert idx == [len(masks), len(fmps), len(masks) + 2 * len(fmps) + 1,
                   len(masks) + 2 * len(fmps) + 1 + 8 * len(majors)], idx
    return dict(masks=masks, fmps=fmps, majors=majors, minors=minors,
                replaces=reps)


def cmd_extract(args):
    defs = parse_member(Path(args.member).read_text())
    defs["provenance"] = f"extracted from source member {args.member}"
    Path(args.output).write_text(json.dumps(defs, indent=1))
    print(f"{args.member}: {len(defs['masks'])} masks, {len(defs['fmps'])} "
          f"FMPs, {len(defs['majors'])} majors, {len(defs['minors'])} minors"
          f" -> {args.output}")


# --------------------------------------------------------------------------
# generate: definitions + template member -> regenerated member
# --------------------------------------------------------------------------

def trailing_zero_pad(items, is_zero):
    """Split items into (real, pad_count) on the trailing all-zero run."""
    n = len(items)
    while n > 0 and is_zero(items[n - 1]):
        n -= 1
    return items[:n], len(items) - n


class Seq:
    """Card sequence numbers: 6-digit, step 2, 'CD' generated-line suffix."""

    def __init__(self, start: int):
        self.n = start

    def line(self, content: str) -> str:
        s = f"{content:<72.72}{self.n % 1000000:06d}CD"
        self.n += 2
        return s


def gen_index(defs, seq):
    m, f = len(defs["masks"]), len(defs["fmps"])
    imaj = m + 2 * f + 1
    imin = imaj + 8 * len(defs["majors"])
    return [seq.line(f"     {m},{f},{imaj},{imin}")]


def rle(values):
    out = []
    for v in values:
        if out and out[-1][1] == v:
            out[-1][0] += 1
        else:
            out.append([1, v])
    return out


def wrap_items(items, first_pre, cont_pre, tail):
    """Join comma-separated items into <=72 col lines."""
    lines, cur = [], first_pre
    for k, item in enumerate(items):
        piece = item + ("," if k < len(items) - 1 else tail)
        sep = "  " if cur.rstrip().endswith(",") else ""
        if cur != first_pre and cur != cont_pre and \
                len(cur) + len(sep) + len(piece) > 72:
            lines.append(cur)
            cur = cont_pre
            sep = ""
        cur += sep + piece
    lines.append(cur)
    return lines


def gen_mask(defs, seq):
    n = len(defs["masks"])
    lines = [seq.line(f"  DECLARE  CDQV_PL_MASK  ARRAY({n}) BIT(16) "
                      f"INITIAL(")]
    items = [f"HEX'{v}'" if c == 1 else f"{c}#HEX'{v}'"
             for c, v in rle(defs["masks"])]
    for l in wrap_items(items, "    ", "    ", " );"):
        lines.append(seq.line(l))
    return lines


def gen_fmps(defs, seq):
    real, pad = trailing_zero_pad(
        defs["fmps"], lambda f: all(v == 0 for v in f.values()))
    majors, minors = defs["majors"], defs["minors"]
    n = len(defs["fmps"])
    lines = [seq.line(f"  DECLARE CDQK_PL_FMPS  CDQK_PL_FMP-STRUCTURE({n})"
                      f"  INITIAL(")]
    for i, f in enumerate(real, 1):
        majid = majors[f["major"] - 1]["id"]
        minhex = f"{minors[f['minor'] - 1]['id']:02X}"
        lines.append(seq.line(f"C     # {i}  -  {majid}  {minhex}"
                              f"    {f['ind']}  {f['cls']}"))
        vals = ",".join(f"DEC'{f[k]}'" for k in FMP_FIELDS)
        pre = "             " if i == 1 else "    ,        "
        lines.append(seq.line(pre + vals))
    if pad:
        lines.append(seq.line("C  - - - -  PAD  - - - -"))
        lines.append(seq.line(f"       ,       {pad}#(6#DEC'0') );"))
    else:
        lines.append(seq.line("     );"))
    return lines


def gen_majors(defs, seq):
    real, pad = trailing_zero_pad(
        defs["majors"],
        lambda m: m["id"] == 0 and set(m["fcws"]) == {"0000"})
    n = len(defs["majors"])
    lines = [seq.line(f"  DECLARE CDQK_PL_MAJORS  CDQK_PL_MAJOR-STRUCTURE"
                      f"({n})  INITIAL(")]
    for i, m in enumerate(real, 1):
        lines.append(seq.line(f"C      # {i}  -  {m['id']}  "
                              f"'{fcws_text(m['fcws'])}'"))
        lines.append(seq.line("         " +
                              ",".join(f"HEX'{h}'" for h in m["fcws"][:6]) +
                              ","))
        lines.append(seq.line(f"          HEX'{m['fcws'][6]}',{m['id']},"))
    if pad:
        lines.append(seq.line("C  - - - -  PAD  - - - -"))
        lines.append(seq.line(f"               {pad}#(7#HEX'0000',0) );"))
    else:
        lines.append(seq.line("     );"))
    return lines


def gen_minors(defs, seq):
    real, pad = trailing_zero_pad(
        defs["minors"],
        lambda m: m["id"] == 0 and set(m["fcws"]) == {"0000"})
    n = len(defs["minors"])
    lines = [seq.line(f"  DECLARE CDQK_PL_MINORS  CDQK_PL_MINOR-STRUCTURE"
                      f"({n})  INITIAL(")]
    for i, m in enumerate(real, 1):
        lines.append(seq.line(f"C        # {i}  -  {m['id']:02X}  "
                              f"'{fcws_text(m['fcws'])}'"))
        lines.append(seq.line(f"           HEX'{m['fcws'][0]}',"
                              f"HEX'{m['fcws'][1]}',{m['id']},"))
    if pad:
        lines.append(seq.line("C  - - - -  PAD  - - - -"))
        lines.append(seq.line(f"               {pad}#(2#HEX'0000',0) );"))
    else:
        lines.append(seq.line("     );"))
    return lines


def gen_replaces(defs, seq, template_lines):
    reps = defs.get("replaces")
    if not reps:                       # keep the template's REPLACE block
        return list(template_lines)
    return [seq.line(f' REPLACE {name} BY "{val}";') for name, val in reps]


def generate_member(defs, member: str, output: str):
    raw = Path(member).read_bytes().decode()
    eol = "\r\n" if "\r\n" in raw else "\n"
    tmpl = raw.splitlines()
    body = [ln[:72] for ln in tmpl]
    blocks = gen_blocks(body)
    assert len(blocks) == 6

    def start_seq(k):
        # first generated line's sequence number in the template block
        first = tmpl[blocks[k][0]]
        return int(first[72:78]) if len(first) >= 78 and \
            first[72:78].isdigit() else 2

    gens = [
        gen_index(defs, Seq(start_seq(0))),
        gen_mask(defs, Seq(start_seq(1))),
        gen_fmps(defs, Seq(start_seq(2))),
        gen_majors(defs, Seq(start_seq(3))),
        gen_minors(defs, Seq(start_seq(4))),
        gen_replaces(defs, Seq(start_seq(5)),
                     tmpl[blocks[5][0]:blocks[5][1]]),
    ]
    out, pos = [], 0
    for (s, e), newlines in zip(blocks, gens):
        out += tmpl[pos:s]
        out += newlines
        pos = e
    out += tmpl[pos:]
    Path(output).write_text(eol.join(out) + eol)
    print(f"wrote {output}: {len(out)} lines "
          f"({len(defs['masks'])} masks, {len(defs['fmps'])} FMPs, "
          f"{len(defs['majors'])} majors, {len(defs['minors'])} minors)")


def cmd_generate(args):
    defs = json.loads(Path(args.defs).read_text())
    generate_member(defs, args.member, args.output)


# --------------------------------------------------------------------------
# FMPT: input catalogs (original card format) -> definitions -> member
# --------------------------------------------------------------------------

def parse_catalog(path) -> dict:
    """Parse an FMPT input catalog (card format documented in the module
    docstring) into G/H text cards and C1 annunciation refs."""
    g, h, ann = [], [], []
    for lno, ln in enumerate(Path(path).read_text().splitlines(), 1):
        if len(ln) < 70:
            continue
        t = ln[69]
        if t == "G" and ln[:3].isdigit() and ln[3] == " ":
            g.append(dict(id=int(ln[:3]), text=f"{ln[4:18]:<14}", line=lno))
        elif t == "H" and re.fullmatch(r"[0-9A-F]{2}", ln[:2]) \
                and ln[2] == " ":
            h.append(dict(id=int(ln[:2], 16), text=f"{ln[3:7]:<4}", line=lno))
        elif t == "C" and len(ln) > 71 and ln[71] == "1":
            m = re.fullmatch(r"(\d{3})([0-9A-F]{2})", ln[24:29])
            if m and len(ln) > 30 and ln[30] in "2345":
                ind = int(ln[33]) if len(ln) > 33 and ln[33].isdigit() else 0
                ann.append(dict(major=int(m.group(1)),
                                minor=int(m.group(2), 16),
                                cls=int(ln[30]), ind=ind, line=lno,
                                msid=ln[:13].strip()))
    return dict(path=str(path), name=Path(path).name, g=g, h=h, ann=ann)


def text_unions(cats):
    """(majtxt, mintxt) id->text unions over catalogs; conflict = error."""
    majtxt, mintxt = {}, {}
    for cat in cats:
        for kind, cards, table in (("G", cat["g"], majtxt),
                                   ("H", cat["h"], mintxt)):
            for c in cards:
                old = table.get(c["id"])
                if old is not None and old[0] != c["text"]:
                    sys.exit(f"{kind}-card conflict for id {c['id']}: "
                             f"{old[0]!r} ({old[1]}) vs {c['text']!r} "
                             f"({cat['name']}:{c['line']})")
                table.setdefault(c["id"], (c["text"],
                                           f"{cat['name']}:{c['line']}"))
    return majtxt, mintxt


BUILTIN_FMP = (100, 0x00, 5, 0)      # ILLEGAL ENTRY, blank minor, class 5


def fmpt_ann_list(gpcf_cat, cats):
    """Ordered annunciation list [(major, minorcode, cls, ind)] + REPLACE
    macro list, per the derivation rules."""
    ann = [BUILTIN_FMP]
    replaces = []
    for gc in gpcf_cat["g"]:
        for cls in (2, 3):
            ann.append((gc["id"], 0x77, cls, 0))
            replaces.append([gc["text"].replace(" ", "_") + f"_{cls}",
                             len(ann)])
    seen = {(a, b, c) for a, b, c, _ in ann}
    for cat in cats:
        for r in cat["ann"]:
            key = (r["major"], r["minor"], r["cls"])
            if key not in seen:
                seen.add(key)
                ann.append((r["major"], r["minor"], r["cls"], r["ind"]))
    return ann, replaces


def fmpt_defs(gpcf_cat, cats, provenance):
    """Full definitions dict from parsed catalogs."""
    majtxt, mintxt = text_unions([gpcf_cat] + cats)
    ann, replaces = fmpt_ann_list(gpcf_cat, cats)

    maj_order, min_order = [], []      # message ids in first-use order
    for a, b, _c, _i in ann:
        if a not in maj_order:
            maj_order.append(a)
        if b not in min_order:
            min_order.append(b)
    missing = [f"major {a}" for a in maj_order if a not in majtxt] + \
              [f"minor {b:02X}" for b in min_order if b not in mintxt]
    if missing:
        sys.exit("no catalog text for: " + ", ".join(missing))

    fmps = [dict(res=1, code=0, major=maj_order.index(a) + 1,
                 minor=min_order.index(b) + 1, ind=i, cls=c)
            for a, b, c, i in ann]
    fmps += [dict(res=0, code=0, major=0, minor=0, ind=0, cls=0)] * 5

    def rec(text, nfcw):
        fcws = [f"{fcw_encode(text[2*k:2*k+2]):04X}" for k in range(nfcw)]
        return fcws

    majors = [dict(fcws=rec(majtxt[a][0], 7), id=a, text=majtxt[a][0])
              for a in maj_order]
    majors += [dict(fcws=["0000"] * 7, id=0, text="\0" * 14)] * 5
    minors = [dict(fcws=rec(mintxt[b][0], 2), id=b, text=mintxt[b][0])
              for b in min_order]
    minors += [dict(fcws=["0000"] * 2, id=0, text="\0" * 4)] * 5

    nmaskhw = (len(fmps) + 15) // 16
    bits = 0
    for i, (_a, _b, c, _i2) in enumerate(ann):
        if c != 5:
            bits |= 1 << (nmaskhw * 16 - 1 - i)
    masks = [f"{(bits >> (16 * (nmaskhw - 1 - k))) & 0xFFFF:04X}"
             for k in range(nmaskhw)]
    return dict(provenance=provenance, masks=masks, fmps=fmps,
                majors=majors, minors=minors, replaces=replaces)


def cmd_fmpt(args):
    gpcf = parse_catalog(args.gpcf_catalog)
    cats = [parse_catalog(p) for p in args.catalog]
    defs = fmpt_defs(gpcf, cats,
                     "FMPT from catalogs: " +
                     ", ".join([gpcf["name"] + " (GPCF)"] +
                               [c["name"] for c in cats]))
    if args.defs_out:
        Path(args.defs_out).write_text(json.dumps(defs, indent=1))
    generate_member(defs, args.member, args.output)


# --------------------------------------------------------------------------
# mkcatalog: definitions -> FMPT input catalog (original card format)
# --------------------------------------------------------------------------

def real_entries(defs):
    """(fmps, majors, minors) without the 5 trailing PAD entries."""
    fmps = [f for f in defs["fmps"] if f["major"] != 0]
    majors = [m for m in defs["majors"]
              if m["id"] != 0 or set(m["fcws"]) != {"0000"}]
    minors = [m for m in defs["minors"]
              if m["id"] != 0 or set(m["fcws"]) != {"0000"}]
    return fmps, majors, minors


def cmd_mkcatalog(args):
    defs = json.loads(Path(args.defs).read_text())
    fmps, majors, minors = real_entries(defs)
    gpcf = parse_catalog(args.gpcf_catalog)
    gpcf_majids = {gc["id"] for gc in gpcf["g"]}
    gpcf_ann = {(a, b, c) for a, b, c, _ in fmpt_ann_list(gpcf, [])[0]}

    corr_g, corr_h, corr_ann = {}, {}, {}
    for p in args.corroborate or []:
        cat = parse_catalog(p)
        for c in cat["g"]:
            corr_g.setdefault(c["id"], c["text"])
        for c in cat["h"]:
            corr_h.setdefault(c["id"], c["text"])
        for r in cat["ann"]:
            corr_ann.setdefault((r["major"], r["minor"], r["cls"]), r["ind"])

    lines = []
    seq = [100]

    def card(body, ctype, mark="  "):
        s = f"{body:<68.68}{ctype:<4.4}{seq[0]:06d}{mark}"
        seq[0] += 100
        lines.append(s)

    def comment(text):
        card(f"*{'':3}{text}", "**")

    comment("*" * 60)
    comment("CDQANNUN FMPT INPUT CATALOG -- RECONSTRUCTED FROM DASS")
    comment(defs.get("provenance", "")[:60])
    comment("THE DELIVERED TOOLIN SET IS THE OI34.06-ERA BASELINE; THE")
    comment("MISSION CATALOGS BEHIND THE FLIGHT OI34.07 FMPT PRODUCT ARE")
    comment("NOT IN THE DELIVERED TREE.  THIS MEMBER RECONSTRUCTS THEM IN")
    comment("THE ORIGINAL CARD FORMAT FROM THE DASS #PCDQANN DECODE.")
    comment("COLS 79-80: CO = CARD ALSO PRESENT (IDENTICAL) IN A DELIVERED")
    comment("CATALOG (STALE-BUT-MATCHING); DA = DASS-ONLY (RECONSTRUCTED).")
    comment("GPCF MAJORS/MINOR 77/CLS 2+3 FMPTS COME FROM THE DELIVERED")
    comment("GPCFMSGS MEMBER AND ARE NOT REPEATED HERE.  C1 CARDS ARE IN")
    comment("EXPLICIT FMPS ORDER (SMPP-INTERNAL ORDER, NOT DECK-DERIVABLE).")
    comment("*" * 60)

    ng = nh = nc = 0
    for m in majors:
        if m["id"] in gpcf_majids:
            continue
        mark = "CO" if corr_g.get(m["id"]) == m["text"] else "DA"
        card(f"{m['id']:3d} {m['text']}", " G", mark)
        ng += 1
    for m in minors:
        if m["id"] == 0x77:
            continue
        mark = "CO" if corr_h.get(m["id"]) == m["text"] else "DA"
        card(f"{m['id']:02X} {m['text']}", " H", mark)
        nh += 1
    majids = [m["id"] for m in majors]
    minids = [m["id"] for m in minors]
    for f in fmps:
        key = (majids[f["major"] - 1], minids[f["minor"] - 1], f["cls"])
        if key in gpcf_ann or key == BUILTIN_FMP[:3]:
            continue
        mark = "CO" if corr_ann.get(key) == f["ind"] else "DA"
        ind = f"  {f['ind']}" if f["cls"] == 2 else ""
        body = f"{'':18}1     {key[0]:03d}{key[1]:02X} {f['cls']}{ind}"
        card(body, " C 1", mark)
        nc += 1
    Path(args.output).write_text("\n".join(lines) + "\n")
    print(f"wrote {args.output}: {ng} G cards, {nh} H cards, "
          f"{nc} C1 annunciation cards "
          f"(+{len(gpcf['g'])} GPCF majors via {gpcf['name']})")


# --------------------------------------------------------------------------
# validate: catalogs vs definitions -> coverage report
# --------------------------------------------------------------------------

def lcs_len(a, b):
    """Longest common subsequence length (patience/LIS on first positions)."""
    import bisect
    pos = {}
    for i, x in enumerate(a):
        pos.setdefault(x, i)
    seq = [pos[x] for x in b if x in pos]
    tails = []
    for v in seq:
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
        else:
            tails[j] = v
    return len(tails)


def cmd_validate(args):
    defs = json.loads(Path(args.defs).read_text())
    fmps, majors, minors = real_entries(defs)
    gpcf = parse_catalog(args.gpcf_catalog)
    cats = [parse_catalog(p) for p in args.catalog]
    majtxt, mintxt = text_unions([gpcf] + cats)
    majids = [m["id"] for m in majors]
    minids = [m["id"] for m in minors]

    print(f"truth: {args.defs} ({len(fmps)} FMPs, {len(majors)} majors, "
          f"{len(minors)} minors)")
    for name, table, txts in (("majors", majors, majtxt),
                              ("minors", minors, mintxt)):
        found = [m for m in table if m["id"] in txts]
        exact = [m for m in found if txts[m["id"]][0] == m["text"]]
        print(f"{name}: {len(table)} real; id in catalogs {len(found)}; "
              f"text-exact {len(exact)}")
        for m in found:
            if txts[m["id"]][0] != m["text"]:
                print(f"  text MISMATCH id {m['id']}: member {m['text']!r} "
                      f"catalog {txts[m['id']][0]!r} ({txts[m['id']][1]})")

    truth = [(majids[f["major"] - 1], minids[f["minor"] - 1], f["cls"],
              f["ind"]) for f in fmps]
    ann, replaces = fmpt_ann_list(gpcf, cats)
    annset = {(a, b, c): i for a, b, c, i in ann}
    cov = [t for t in truth if t[:3] in annset]
    indok = [t for t in cov if annset[t[:3]] == t[3]]
    print(f"FMPS: {len(truth)} real; triple covered {len(cov)}; "
          f"IND agrees {len(indok)}")
    unc = [t for t in truth if t[:3] not in annset]
    for t in unc[:args.max_uncovered]:
        print(f"  UNCOVERED {t}")
    if len(unc) > args.max_uncovered:
        print(f"  ... {len(unc) - args.max_uncovered} more uncovered")
    tt = [t[:3] for t in truth]
    pt = [a[:3] for a in ann]
    pos = sum(1 for x, y in zip(pt, tt) if x == y)
    print(f"order: deck-scan positional {pos}/{len(tt)}, "
          f"LCS {lcs_len(pt, tt)}/{len(tt)}")

    # rule self-checks against the truth defs
    print(f"mask rule: {'MATCH' if recomputed_masks_match(defs, truth) else 'MISMATCH'}")
    if defs.get("replaces"):
        print(f"REPLACE rule: "
              f"{'MATCH' if replaces == defs['replaces'] else 'MISMATCH'}"
              f" ({len(replaces)} vs {len(defs['replaces'])})")


def recomputed_masks_match(defs, truth):
    n = len(defs["fmps"])
    nmaskhw = (n + 15) // 16
    bits = 0
    for i, (_a, _b, c, _i2) in enumerate(truth):
        if c != 5:
            bits |= 1 << (nmaskhw * 16 - 1 - i)
    masks = [f"{(bits >> (16 * (nmaskhw - 1 - k))) & 0xFFFF:04X}"
             for k in range(nmaskhw)]
    return masks == defs["masks"]


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def cmd_diff(args):
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    for key in ("masks", "fmps", "majors", "minors"):
        av, bv = a[key], b[key]
        pre = 0
        for x, y in zip(av, bv):
            if key in ("majors", "minors"):
                same = x["fcws"] == y["fcws"] and x["id"] == y["id"]
            else:
                same = x == y
            if not same:
                break
            pre += 1
        print(f"{key}: {len(av)} vs {len(bv)}, common prefix {pre}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help="DASS .ASC -> definitions JSON")
    d.add_argument("--dass", required=True)
    d.add_argument("--csect", default="#PCDQANN")
    d.add_argument("-o", "--output", required=True)
    d.set_defaults(func=cmd_decode)

    e = sub.add_parser("extract", help="source member -> definitions JSON")
    e.add_argument("--member", required=True)
    e.add_argument("-o", "--output", required=True)
    e.set_defaults(func=cmd_extract)

    g = sub.add_parser("generate",
                       help="definitions + template -> regenerated member")
    g.add_argument("--defs", required=True)
    g.add_argument("--member", required=True,
                   help="template member (fixed source kept verbatim)")
    g.add_argument("-o", "--output", required=True)
    g.set_defaults(func=cmd_generate)

    f = sub.add_parser("diff", help="compare two definitions JSONs")
    f.add_argument("a")
    f.add_argument("b")
    f.set_defaults(func=cmd_diff)

    p = sub.add_parser("fmpt",
                       help="FMPT input catalogs + template -> member")
    p.add_argument("--gpcf-catalog", required=True,
                   help="GPCF message catalog (GPCFMSGS): its G cards "
                        "expand to the class-2/3 FMPT pairs + REPLACEs")
    p.add_argument("--catalog", action="append", default=[],
                   help="further catalog file (repeatable, scanned in "
                        "order: G/H texts + C1 annunciation cards)")
    p.add_argument("--member", required=True,
                   help="template member (fixed source kept verbatim)")
    p.add_argument("--defs-out", help="also write the intermediate "
                                      "definitions JSON here")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_fmpt)

    m = sub.add_parser("mkcatalog",
                       help="definitions JSON -> FMPT input catalog")
    m.add_argument("--defs", required=True)
    m.add_argument("--gpcf-catalog", required=True,
                   help="delivered GPCF catalog whose content is NOT "
                        "repeated in the output")
    m.add_argument("--corroborate", action="append",
                   help="delivered catalog to mark matching cards "
                        "CO instead of DA (repeatable)")
    m.add_argument("-o", "--output", required=True)
    m.set_defaults(func=cmd_mkcatalog)

    v = sub.add_parser("validate",
                       help="catalog coverage report vs definitions JSON")
    v.add_argument("--defs", required=True, help="truth definitions JSON")
    v.add_argument("--gpcf-catalog", required=True)
    v.add_argument("--catalog", action="append", default=[])
    v.add_argument("--max-uncovered", type=int, default=20)
    v.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
