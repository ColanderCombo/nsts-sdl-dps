#!/usr/bin/env python3
#
# Render an encoded display as HAL/S COMPOOL source.
# 
# The HAL output reproduces the DFG's own format: a boxed metadata comment header,
# the deck's directives echoed on `C` comment cards, `D INCLUDE TEMPLATE` cards for
# the referenced compools, then the encoded image as HAL declarations —
#   * the header + KVT + static prefix as one `DFT_<n>` BIT(16) array, and
#   * one declare per dynamic word: a `_VPD` literal, or a `_PADR` typed `NAME(var)`
#     pointer the compiler relocates.
# Every `--`/`-` annotation comes from the segment that produced the words, so the
# source doubles as documentation of the format.
# 
from .deck import encodable_directives, read_cards, resolve_deck
from .model import Padr
from .resolve import padr_types, alias_body, includes

_DFG_VERSION = "30.40"


def _comment(text):
    return "C " + text if text else "C"


def _comment_cards(text):
    """One or more `C` comment cards for `text`: embedded newlines (a directive
    value that spanned deck cards) collapse to spaces, and content wraps to <=72
    columns so nothing spills past the card into a code column."""
    text = " ".join(str(text).split()) 
    if not text:
        return ["C"]
    out = []
    while len(text) > 70:
        cut = text.rfind(" ", 0, 70)
        if cut <= 0:
            cut = 70
        out.append("C " + text[:cut])
        text = text[cut:].lstrip()
    out.append("C " + text)
    return out


def _code(text):
    """A main HAL code card: column-1 blank."""
    return " " + text


def _wrap(stmt):
    """Split one HAL statement onto <=72-column code cards (HAL/S continues a
    statement across cards until its `;`), breaking at spaces."""
    line = " " + stmt
    if len(line) <= 72:
        return [line]
    out, cur = [], line
    while len(cur) > 72:
        cut = cur.rfind(" ", 1, 72)
        if cut <= 0:
            break
        out.append(cur[:cut])
        cur = "    " + cur[cut + 1:]
    out.append(cur)
    return out


def _box(text):
    """One line of the boxed metadata header (`C ****  ...  ****`)."""
    inner = text.center(58)
    return _comment("****%s****" % inner)


def _dynamic_fcws(enc):
    """Runtime max FCWs the cyclic process builds: the sum of the rate-group
    counts + 10 per group (the DEU inter-group pad, DCI#FMT CASE 21's
    `CLOCBRAN += count + 10`) + a fixed 33-FCW overhead; 0 for a display with
    no rate groups."""
    rcs = enc.rate_counts
    return sum(rcs) + 10 * len(rcs) + 33 if rcs else 0


def _metadata(enc, padn):
    """The boxed statistics header the DFG writes atop each compool.  A
    critical format counts nothing as GPC-built (the whole array is the DEU
    draw), so its MAXIMUM FCW COUNT is the array size and the other counts
    are 0."""
    ddt_entries = enc.total - enc.ddt_idx
    static_fcws = 0 if enc.crtfmt else enc.ddt_idx - enc.stat_idx
    gpc_storage = (enc.total + 1) // 2
    dynamic_fcws = _dynamic_fcws(enc)
    max_fcws = enc.total if enc.crtfmt else static_fcws + dynamic_fcws
    rows = [
        ("COMPOOL NAME:", enc.hal),
        ("DFG VERSION:", _DFG_VERSION),
        ("", ""),
        ("DDT ENTRIES:", ddt_entries),
        ("PAD HALFWORDS:", padn),
        ("STATIC FCWS(BUILT BY DFG):", static_fcws),
        ("DYNAMIC FCWS(BUILT BY CYCLIC):", dynamic_fcws),
        ("MAXIMUM FCW COUNT:", max_fcws),
        ("GPC STORAGE(IN 32 BIT WORDS):", gpc_storage),
    ]
    lines = [_box("")]
    for label, val in rows:
        if not label:
            lines.append(_box(""))
        else:
            lines.append(_box("%-30s %s" % (label, val)))
    lines.append(_box(""))
    lines.append(_box("       DFG INPUT      "))
    return lines


def _header_word_comments(enc, i):
    """The fixed `--`/`-` annotations for header word `i` (0..5)."""
    return {
        0: ["-- Aliases in Display Page Table = %d" % enc.pages],
        1: ["- The DFT size is %d halfwords" % enc.total],
        2: ["- The displacement to the KVT is %d halfwords" % enc.kvt_idx],
        3: ["- The displacement to the background is %d" % enc.stat_idx],
        4: ["- The displacement to the DDT is %d halfwords" % enc.ddt_idx],
        5: ["- Bits 0-1 are the major function and 2-15 are",
            "- the display page numbers",
            "-- HEADER = %s" % enc.hdrval],
    }.get(i, [])


def _resolved(enc):
    """The fully-numeric image (PADR positions as 0 — the HAL output emits
    typed `NAME(var)` pointers for them instead)."""
    return enc.image()


def _to_hal_cft(enc):
    """Render a critical format: one `CFT_<n>` BIT(16) array — the DEU-resident
    background body — with no DFT header, KVT, DDT or PADRs."""
    hal = enc.hal
    tag = "".join(c for c in hal if c.isdigit()) or hal
    img = enc.image()
    L = _metadata(enc, 0)
    deck_path = resolve_deck(enc.src)
    if deck_path:
        for card in read_cards(deck_path):
            L += _comment_cards(card)
    L.append(_comment("****   BEGIN COMPOOL   ****"))
    L.append(_code("%s: COMPOOL RIGID;" % hal))
    L.append(_code("DECLARE CFT_%s ARRAY(%d) BIT(16) INITIAL(" % (tag, enc.total)))
    off = emitted = 0
    for seg in enc.segments:
        for c in seg.comments or []:
            L += _comment_cards(c)
        toks = ["HEX'%04X'" % (img[off + j] & 0xFFFF)
                for j in range(len(seg.words))]
        off += len(seg.words)
        for j in range(0, len(toks), 6):
            chunk = toks[j:j + 6]
            emitted += len(chunk)
            end = ");" if emitted == enc.total else ","
            L.append(_code("  " + ", ".join(chunk) + end))
    L.append(_code("CLOSE %s;" % hal))
    return "\n".join(L) + "\n"


def to_hal(enc):
    """Render `enc` (an `encode.Encoded`) as HAL/S COMPOOL source text."""
    if enc.crtfmt:
        return _to_hal_cft(enc)
    hal = enc.hal
    ds = encodable_directives(enc.src)
    compools = includes(ds)
    img = _resolved(enc)
    tag = "".join(c for c in hal if c.isdigit()) or hal      # DFG names use the number
    fam = hal[1] if len(hal) > 1 else "S"                    # family letter: CS->S, CG->G
    padn = next((int((p[1:-1] if p.startswith("(") else p).split(",")[0])
                 for k, p in ds if k == "PAD" and p), 0)

    L = []
    L += _metadata(enc, padn)
    # Echo the deck's directive cards verbatim as comments.
    deck_path = resolve_deck(enc.src)
    if deck_path:
        for card in read_cards(deck_path):
            L += _comment_cards(card)
    # Referenced compools -> INCLUDE TEMPLATE directive cards.
    L.append(_comment(" **** REFERENCED COMPOOLS"))
    for cp in compools:
        L.append("D INCLUDE TEMPLATE %s" % cp)
    L.append(_comment("****   BEGIN COMPOOL   ****"))
    L.append(_code("%s: COMPOOL RIGID;" % hal))

    # --- DFT array: the header + KVT + static prefix (words[:ddt_idx]) ---
    L.append(_code("DECLARE DFT_%s ARRAY(%d) BIT(16) INITIAL(" % (tag, enc.ddt_idx)))
    prefix_words = []                         # (comment_lines_or_None, [values])
    off = 0
    for seg in enc.segments:
        if seg.section not in ("header", "kvt", "static"):
            continue
        vals = [img[off + j] for j in range(len(seg.words))]
        off += len(seg.words)
        if seg.section == "header" and seg.comments is None:
            for j, val in enumerate(vals):    # header words annotated individually
                prefix_words.append((_header_word_comments(enc, j), [val]))
        else:
            prefix_words.append((seg.comments, vals))
    # flatten to HEX tokens, closing the INITIAL list on the last value
    total_vals = sum(len(v) for _, v in prefix_words)
    emitted = 0
    for comments, vals in prefix_words:
        for c in comments or []:
            L += _comment_cards(c)
        toks = ["HEX'%04X'" % (v & 0xFFFF) for v in vals]
        for j in range(0, len(toks), 6):
            chunk = toks[j:j + 6]
            emitted += len(chunk)
            end = ");" if emitted == total_vals else ","
            L.append(_code("  " + ", ".join(chunk) + end))

    # --- dynamic declares: one per DDT/terminator/PAD word ---
    pt = padr_types(ds, enc.names)             # (name, type) in PADR order
    ptypes = {}                                # name -> type
    for name, ty in pt:
        ptypes.setdefault(name, ty)
    # A REPLACE-macro alias referent is emitted as its BODY (the declared
    # name); an `@n` suffix (a DMDUPD watch's bit offset) is generator-side
    # only and never part of the referent.
    ref_of = {n: (alias_body(n, compools) or n).split("@")[0] for n in ptypes}
    vpd_n = padr_n = 0
    off = enc.ddt_idx
    for seg in enc.segments:
        if seg.section not in ("ddt",):
            continue
        for c in (seg.comments or []):
            L += _comment_cards(c)
        for w in seg.words:
            val = img[off]
            if isinstance(w, Padr):
                padr_n += 1
                ty = ptypes.get(w.name) or "BIT(16)"      # fallback until template built
                L += _wrap("DECLARE %s%d_%s_PADR NAME %s INITIAL(NAME(%s));"
                           % (fam, padr_n, tag, ty, ref_of.get(w.name, w.name)))
            else:
                vpd_n += 1
                L.append(_code("DECLARE %s%d_%s_VPD BIT(16) INITIAL(HEX'%04X');"
                               % (fam, vpd_n, tag, val & 0xFFFF)))
            off += 1
    L.append(_code("CLOSE %s;" % hal))
    return "\n".join(L) + "\n"


def n_untyped(enc):
    """Count PADR pointers whose referent type could not be resolved (missing
    template) — they fall back to `NAME BIT(16)`."""
    ds = encodable_directives(enc.src)
    return sum(1 for _, ty in padr_types(ds, enc.names) if ty is None)
