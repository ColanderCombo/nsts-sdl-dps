"""Normalize a real DASS .ASC listing for diffing (strip mode): drop page
furniture and the report sections mafgen does not replicate."""
from .common import w

_TITLE_MAP = w("MEMORY MAP")
_TITLES_SKIP = (w("MAP SUMMARY"), w("INSTRUCTION"), w("CSECT CROSS"),
                "%MACRO", w("SVC LOC"), w("PATCH SUMMARY"),
                w("RUN STATISTICS"))

def strip_dass(path):
    """Normalize a real MAFGEN DASS .ASC to bare memory-map body lines:
    keeps the csect index / extent list / dump sections, drops page
    furniture (headers, subtitles, overprint and blank lines) and all other
    report sections, so the result diffs against our generated listing."""
    out = []
    keep = False           # inside a MEMORY MAP page
    expect_subtitle = 0
    for raw in open(path, errors="replace"):
        line = raw.rstrip("\n").rstrip()
        cc, body = (line[0], line[1:]) if line else (" ", "")
        if cc == "1":                       # page header -> subtitle follows
            expect_subtitle = 1
            continue
        if expect_subtitle:
            expect_subtitle = 0
            if _TITLE_MAP in body or body.lstrip().startswith(
                    "CONFIGURATION"):
                keep = True
                continue
            if any(t in body for t in _TITLES_SKIP):
                keep = False
                continue
            # not a recognized subtitle: fall through as a body line of the
            # current page state
        if not keep:
            continue
        if _TITLE_MAP in body:              # repeated mid-page titles
            continue
        if cc == "+" or not body.strip():
            continue
        out.append(" " + body.rstrip())
    return out
