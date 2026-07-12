#!/usr/bin/env python3
#
# Translate a display deck into the encoded `#P<name>` compool.
# 
# The image is: a 6-word header, optional page-alias pointers, the KVT, the static
# FCW list, the DDT, an alignment terminator, and PAD fill — built as annotated
# `Segment`s. 
#
import os
import re

from . import kvt as _kvt
from . import ddt as _ddt
from . import static as _static
from .deck import encodable_directives
from .fcw import Branch, FCW
from .model import Error, Segment, Padr, flatten
from .ops import immed
from .resolve import char_inits, w0_resolver


def _fmtnum(hdrval, name=None):
    """Format word: the numeric part | major-function bits from the trailing
    letter (C=command 0x0000, PL=0x4000, GNC=0x8000, SM=0xC000).  `name`
    overrides the number for the main display (its own name), else the alias
    header's digits."""
    src = name if name is not None else hdrval
    num = int(re.sub(r"\D", "", src) or 0)
    mf = {"G": 0x8000, "S": 0xC000, "P": 0x4000}.get(hdrval.strip()[-1:], 0)
    return num | mf


class Encoded:
    """Result of encoding one display: the annotated segments plus the layout
    metadata the emitters need."""

    def __init__(self, hal, segments, pages, kvt_idx, stat_idx, ddt_idx, total,
                 names, hdrval="", rate_counts=None, src=None, crtfmt=False):
        self.hal = hal                   # display name (deck basename)
        self.src = src or hal            # what locates the deck: a path or the name
        self.crtfmt = crtfmt             # critical-format (CRTFMT= deck) -> CFT array
        self.segments = segments
        self.pages = pages
        self.kvt_idx = kvt_idx
        self.stat_idx = stat_idx
        self.ddt_idx = ddt_idx
        self.total = total
        self.names = names               # PADR names in image order
        self.hdrval = hdrval             # HEADER= directive value (for comments)
        self.rate_counts = rate_counts or []   # resolved per-rate-group counts
        self.words = flatten(segments)   # flat list of int | FCW | Padr

    @property
    def padr_offsets(self):
        return [i for i, w in enumerate(self.words) if isinstance(w, Padr)]

    def image(self, padr_value=None):
        """The fully-numeric image.  A `Padr` becomes `padr_value(offset, name)`
        (default 0 — its value is irrelevant to the HAL output, which emits a
        `NAME(var)` pointer)."""
        out = []
        for i, w in enumerate(self.words):
            if isinstance(w, Padr):
                out.append(((padr_value(i, w.name) if padr_value else 0) or 0) & 0xFFFF)
            else:
                out.append(int(w or 0) & 0xFFFF)   # w: int or FCW
        return out


def _branch_lands_at_end(ddt_ops, ddt_len):
    """True when a TIGHTLY-GUARDED unconditional branch targets exactly the DDT
    length — the `if(!cond) goto end` idiom: either the BRANCH an `IF=ON` op
    carries after its TEST, or a deck `BR` immediately preceded by a TEST whose
    skip count is exactly 1 (covering just that BR).  When that falls past the
    last structure the DFG plants the null-IMMED [5001,0] — per DCICYC the
    stored END-OF-REFRESH FCW "at the end of the dynamic FCW's" of the least
    frequent rate group, annotated `NO FCW PAD - Two additional halfwords`.
    Neither conditional TEST-skips to the end nor CASE-style end branches
    (content-preceded or wide-TEST-preceded BRs, label BRs) take a pad.

    The caller must ALSO require the display's last rate group be RATE 3: a
    guarded end branch in a display ending on a RATE=2 group takes NO plant.
    Why the rate matters is unknown."""
    off = 0
    prev_kind, prev_skip1 = "", False
    for op in ddt_ops:
        guarded = (op.kind == "IF" and op.if_on) or \
            (op.kind == "BR" and prev_kind == "TEST" and prev_skip1)
        if guarded and op.skip_at is not None:
            skip = op.words[op.skip_at]           # filled by _resolve_flow
            if isinstance(skip, int) and skip < 0x800 \
                    and off + op.skip_at + 1 + skip == ddt_len:
                return True
        prev_skip1 = op.kind == "TEST" and op.target == 1
        prev_kind = op.kind
        off += len(op.words)
    return False


# ------------------------------------------------------------------- encode
def encode(hal):
    """Encode display `hal` (a bare name or a deck path) into an `Encoded`,
    deriving every word from the deck.  A word the deck does not determine
    raises `Error`."""
    src = hal
    hal = os.path.basename(hal.replace("\\", "/"))
    ds = encodable_directives(src)

    # A deck led by CRTFMT= is a critical format: a bare CFT FCW array (no
    # DFT header/KVT/DDT), packed into DEUCFLM for residency in DEU memory.
    first_hdr = next((k for k, _ in ds if k in ("HEADER", "CRTFMT")), None)
    if first_hdr == "CRTFMT":
        hdrval = next(v for k, v in ds if k == "CRTFMT" and v)
        segments = _static.build_static(ds, crtfmt=True)
        total = len(flatten(segments))
        return Encoded(hal, segments, 0, 0, 0, total, total, [],
                       hdrval.strip(), src=src, crtfmt=True)

    # --- header word 5 + page aliases -------------------------------------
    # Each HEADER=/CRTFMT= is a page alias; the count is the "Aliases in Display
    # Page Table".  Word 5 carries the main display's format number; the other
    # aliases become page pointers just after the header.
    headers = [v for k, v in ds if k in ("HEADER", "CRTFMT") and v]
    # Display Page Table: one suffix|fmtnum entry per HEADER whose suffix is
    # a real major function (C/PL/G/S), the primary included.  A V
    # (vehicle-utility) HEADER only names the display: bits 0-1 have no V
    # code, so it contributes NO entry.  Header word 0 counts the entries.
    dpt_hdrs = [h for h in headers if h.strip()[-1:] != "V"] or headers[:1]
    hdrval = dpt_hdrs[0] if dpt_hdrs else ""
    dpt = [_fmtnum(dpt_hdrs[0], name=hal)
           if headers and dpt_hdrs[0] == headers[0] else _fmtnum(dpt_hdrs[0])] \
        + [_fmtnum(h) for h in dpt_hdrs[1:]] if dpt_hdrs else []
    pages = max(1, len(dpt))
    fmtnum = dpt[0] if dpt else 0
    page_ptrs = dpt[1:]

    # --- KVT ---
    hdr3, body, srtab, irtab, echoes, nitems = _kvt.build_kvt(ds)

    # --- static (or external-background link) ---
    static_segs = _static.build_static(ds)
    if static_segs is None:
        # External background: no inline FCWs — [0x2000, Branch(DEULOC)].
        # DEULOC is the DEU memory address of this background's CFIT slot
        # (the resident-format table at 0x0100); displays with the same
        # DEULOC draw over the same resident background.  The 0x2000 lead
        # word's role is unknown (FCW op 0x2 is otherwise unused).
        deuloc = next((int(re.sub(r"\D", "", v) or 0)
                       for k, v in ds if k == "DEULOC" and v), 0)
        static_segs = [Segment("static", ["-- External background link (DEULOC %d)"
                                          % deuloc], [0x2000, Branch(deuloc)])]

    # --- DDT ---
    ddt_ops = _ddt.build_ddt(ds, w0_resolver(ds))
    # PADR referent names in emission order (each op names its own pointers).
    names = [n for op in ddt_ops for n in op.padr_names()]

    # --- layout arithmetic ---
    kvt_idx = 6 + len(page_ptrs)
    kvt_words = list(hdr3)
    kvt_body_base = kvt_idx + 3 + len(body)
    if srtab:                                    # header word1 -> scaled table
        kvt_words[1] = kvt_body_base
    if irtab:                                    # header word2 -> integer table
        kvt_words[2] = kvt_body_base + len(srtab)
    kvt_words += body + srtab + irtab
    stat_idx = kvt_idx + len(kvt_words)
    static_words = flatten(static_segs)
    ddt_idx = stat_idx + len(static_words)
    ddt_words = flatten(ddt_ops)

    # DDT terminator.  Two independent reasons the DFG appends a null IMMED:
    #   * ALIGNMENT: the compool is fullword-aligned, so the DDT end (before
    #     PAD) must be even; an odd end takes the 3-word IMMED(2) [5002,0,0].
    #   * BRANCH LANDING: a TEST/BR whose skip target is PAST the last DDT
    #     structure (a display ending inside a conditional) needs a real DDT
    #     entry to land on; the DFG emits the 2-word IMMED(1) [5001,0]
    #     (parity-preserving).
    # An odd end with a landing branch takes [5002,0,0], which serves both
    # roles.
    padv = next((v for k, v in ds if k == "PAD"), None)
    padn = int((padv[1:-1] if padv and padv.startswith("(") else padv).split(",")[0]) \
        if padv else 0
    ddt_end = ddt_idx + len(ddt_words)
    rates = [int(op.value) for op in ddt_ops if op.kind == "RATE"]
    landing = _branch_lands_at_end(ddt_ops, len(ddt_words)) \
        and bool(rates) and rates[-1] == 3
    term = immed(FCW.noop(), FCW.noop()) if ddt_end % 2 \
        else (immed(FCW.noop()) if landing else [])
    tail_ops = _ddt.terminator_and_pad(ds, term)
    total = ddt_end + len(term) + padn

    # rate_count (per rate group) is derived after the DDT terminator exists — its
    # IMMED draws count toward the last rate group.  A group that cannot be
    # derived raises Error (never a wrong value).
    _ddt.resolve_rate_count(ddt_ops + tail_ops, char_inits(ds), hal)
    rate_counts = [op.words[1] for op in ddt_ops if op.kind == "RATE"]

    # --- header + page-pointer segments ---
    header_seg = Segment("header", None, [pages, total, kvt_idx, stat_idx,
                                          ddt_idx, fmtnum])
    segments = [header_seg]
    if page_ptrs:
        segments.append(Segment("header", ["-- Display page pointers (aliases)"],
                                list(page_ptrs)))

    # KVT segments: 3 header words (annotated individually), one 2-word entry per
    # item, then the two limit tables.
    kseg = [
        Segment("kvt", ["- The number of item entries is %d" % nitems,
                        "- and is in bits 0 - 7.  The EXEC",
                        "- key flag is the 9th bit position, 1 is ON"],
                [kvt_words[0]]),
        Segment("kvt", ["- The offset to the Scalar Limits Table is",
                        "- %d" % kvt_words[1]], [kvt_words[1]]),
        Segment("kvt", ["- The offset to the Non Scalar Limits Table is",
                        "- %d" % kvt_words[2]], [kvt_words[2]]),
    ]
    bi = 3
    for e in echoes:                             # one 2-word entry per item
        kseg.append(Segment("kvt", [e], list(kvt_words[bi:bi + 2]))); bi += 2
    if srtab:
        kseg.append(Segment("kvt", ["-- Scalar Limits Table"],
                            list(kvt_words[bi:bi + len(srtab)]))); bi += len(srtab)
    if irtab:
        kseg.append(Segment("kvt", ["-- Non Scalar Limits Table"],
                            list(kvt_words[bi:bi + len(irtab)])))
    segments += kseg
    segments += static_segs
    # DDT ops (and the terminator/pad ops) render 1:1 as emission segments —
    # built only now, after the flow/rate fills, so no placeholder survives.
    segments += [Segment("ddt", op.comments, op.words)
                 for op in ddt_ops + tail_ops]

    return Encoded(hal, segments, pages, kvt_idx, stat_idx, ddt_idx, total,
                   names, hdrval.strip(), rate_counts=rate_counts, src=src)
