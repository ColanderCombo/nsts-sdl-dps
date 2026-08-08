"""CLI for the mafgen replacement tool.

Generate a DASS-style listing from a composed config build:
    mafgen G2 -o build/mafgen/G2.ASC

Normalize a real MAFGEN listing for diffing:
    mafgen --strip DASS_G2.ASC -o g2.ref

Generate + compare in one shot (the fidelity metric):
    mafgen G2 --score DASS_G2.ASC
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

import typer

REPO = Path(__file__).resolve().parents[2]
for p in (REPO / "src",):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ap101Utils.fcmImage import FcmImage             # noqa: E402
from . import paths                                  # noqa: E402
from .csects import deck_types                       # noqa: E402
from .listing import Listing                         # noqa: E402
from .sdfnames import SdfNameMap                     # noqa: E402
from .strip import strip_dass                        # noqa: E402

# MAFGEN's control deck (DEFINE CSECTS ... AS TYPE MSC/BCE/PATCH), the
# only source of the IOP-program and non-PCH patch classifications
_DEFAULT_DECK = REPO.parent / "pfs" / "PFS" / "OI340600" / "TOOLIN" / "MAFTWO"
_CACHE_DIR = REPO / "build" / "mafgen"


def build_listing(config, mmu, fcm=None, sections="index,extent,dump",
                  refresh_sdf=False, deck=None, base=None, degrade=None):
    mmu = Path(mmu)
    fcm = Path(fcm) if fcm else mmu / config / f"{config}.fcm"
    img = FcmImage.load(fcm, mmu_dir=mmu)
    for w in img.warnings:
        print(f"[warn] {w}", file=sys.stderr)
    cache = _CACHE_DIR / f"sdfnames-{img.config}.json"
    overlay = _CACHE_DIR / "sdfregen"
    sdf = SdfNameMap(mmu, img.phases, cache=cache, refresh=refresh_sdf,
                     overlay=overlay if overlay.is_dir() else None)
    deck_path = Path(deck) if deck else _DEFAULT_DECK
    deck_map = deck_types(deck_path) if deck_path.is_file() else None
    stars = None
    if base:
        # star halfwords the patch chain changed: the DASS '*' marks
        # memory differing from the load module -- ours marks the
        # patched image differing from the unpatched base build
        base_img = FcmImage.load(Path(base), mmu_dir=mmu)
        stars = {a for a, v in img.words.items()
                 if v != base_img.words.get(a)}
        print(f"[base] {len(stars)} patched halfwords starred",
              file=sys.stderr)
    degraded = None
    if degrade:
        d = json.load(open(degrade))
        degraded = set(d["symbols"] if isinstance(d, dict) else d)
        print(f"[degrade] {len(degraded)} symbols left unresolved",
              file=sys.stderr)
    lst = Listing(img, sdf, mmu_root=mmu, deck=deck_map, stars=stars,
                  degrade=degraded)
    want = sections.split(",")
    out = []
    if "index" in want:
        out += lst.index_lines()
    if "extent" in want:
        out += lst.extent_lines()
    if "dump" in want:
        out += lst.dump_lines()
    return out


_CLASSES = ("index", "header", "code", "data", "other")
_ADDR6 = re.compile(r"^[0-9A-F]{6}")
_IDXPAIR = re.compile(r"\S{1,8}\s+[0-9A-F]{6}(?:\s{3}|$)")


def _line_class(ln):
    body = ln[1:]
    if _ADDR6.match(body):
        if "****" in body[:36]:
            return "header"
        if body[24:25] == "+" or body[23:24] == "+":
            # dump body line: code when the mnemonic field is populated
            return "code" if body[64:71].strip() else "data"
        return "other"
    return "index" if len(_IDXPAIR.findall(body)) >= 2 else "other"


def score(ours, ref):
    """Exact-line multiset intersection, reported per line class."""
    ours_n, ref_n, hit_n = Counter(), Counter(), Counter()
    ref_pool = Counter(ref)
    for ln in ours:
        ours_n[_line_class(ln)] += 1
    for ln in ref:
        ref_n[_line_class(ln)] += 1
    for ln in ours:
        if ref_pool[ln] > 0:
            ref_pool[ln] -= 1
            hit_n[_line_class(ln)] += 1
    print(f"{'class':8s} {'ours':>8s} {'dass':>8s} {'match':>8s}  pct")
    for cl in _CLASSES:
        o, r, h = ours_n[cl], ref_n[cl], hit_n[cl]
        pct = 100.0 * h / r if r else 0.0
        print(f"{cl:8s} {o:8d} {r:8d} {h:8d}  {pct:5.1f}%")
    tot_o, tot_r, tot_h = sum(ours_n.values()), sum(ref_n.values()), \
        sum(hit_n.values())
    pct = 100.0 * tot_h / tot_r if tot_r else 0.0
    print(f"{'total':8s} {tot_o:8d} {tot_r:8d} {tot_h:8d}  {pct:5.1f}%")


def _require(found):
    """Abort with the input table when something we cannot run without is
    absent; note the degraded ones on one line rather than a banner."""
    gone = paths.missing(found)
    if gone:
        # the table no longer says which rows are fatal, so name them
        what = ", ".join(i.option if i.option != "(derived)" else i.what
                         for i in gone)
        print("\n".join(paths.render(
            found, f"mafgen cannot run without: {what}")), file=sys.stderr)
        print("\nSet the option in the first column to point at your copy.",
              file=sys.stderr)
        raise typer.Exit(2)
    soft = paths.missing(found, paths.AFFECTS)
    if soft:
        names = ", ".join(i.option if i.option != "(derived)" else i.what
                          for i in soft)
        print(f"[warn] absent, so the listing will differ: {names} "
              f"(run --show-paths for details)", file=sys.stderr)


app = typer.Typer(
    help=__doc__,
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.command(help=__doc__)
def generate(
    ctx: typer.Context,
    config: Annotated[Optional[str], typer.Argument(
        help="config name, e.g. G2")] = None,
    *,
    mmu: Annotated[str, typer.Option("--mmu",
        help="con80build/mmu2fcm output root")] = "build/mmu",
    fcm: Annotated[Optional[str], typer.Option("--fcm",
        help="explicit composed .fcm path")] = None,
    sections: Annotated[str, typer.Option("--sections",
        help="listing sections to emit, comma-separated")]
        = "index,extent,dump",
    strip: Annotated[Optional[str], typer.Option("--strip", metavar="ASC",
        help="normalize a real DASS .ASC instead of generating")] = None,
    score_against: Annotated[Optional[str], typer.Option("--score",
        metavar="ASC",
        help="generate, then report line-match rate vs this DASS")] = None,
    refresh_sdf: Annotated[bool, typer.Option("--refresh-sdf",
        help="rebuild the SDF block-name cache")] = False,
    deck: Annotated[Optional[str], typer.Option("--deck", metavar="MAFTWO",
        help="MAFGEN control deck for csect AS TYPE classification "
             "(default: the source tree's TOOLIN copy)")] = None,
    base: Annotated[Optional[str], typer.Option("--base", metavar="FCM",
        help="unpatched reference image: halfwords that differ print the "
             "'*' patch marker")] = None,
    degrade: Annotated[Optional[str], typer.Option("--degrade",
        metavar="JSON",
        help='JSON list (or {"symbols": [...]}) of symbol names to leave '
             "unresolved (' ?  csect+off' form) -- diff parity with "
             "flight-SDF xref gaps")] = None,
    output: Annotated[Optional[str], typer.Option("-o", "--output",
        help="output file (default stdout)")] = None,
    show_paths: Annotated[bool, typer.Option("--show-paths",
        help="print every input file, where it is looked for and whether "
             "it was found, then exit")] = False,
):
    found = paths.tier1(config=config, mmu=mmu, fcm=fcm, deck=deck,
                        default_deck=_DEFAULT_DECK, base=base,
                        degrade=degrade, score=score_against, strip=strip,
                        cache_dir=_CACHE_DIR)

    if show_paths:
        # tier 2 needs the manifest's phase list, so extend the table only
        # when the image actually loads
        if not paths.missing(found):
            try:
                img = FcmImage.load(
                    Path(fcm) if fcm else Path(mmu) / str(config)
                    / f"{config}.fcm", mmu_dir=Path(mmu))
                found += paths.tier2(mmu, img.phases)
            except OSError:
                pass
        print("\n".join(paths.render(found)))
        return

    if strip:
        lines = strip_dass(strip)
    else:
        if not config and not fcm:
            print(ctx.get_help())
            raise typer.Exit(2)
        _require(found)
        lines = build_listing(config, mmu, fcm=fcm, sections=sections,
                              refresh_sdf=refresh_sdf, deck=deck,
                              base=base, degrade=degrade)
        if score_against:
            score(lines, strip_dass(score_against))
            if not output:
                return

    text = "\n".join(lines) + "\n"
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text)
        print(f"{len(lines)} lines -> {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def main():
    app()


if __name__ == "__main__":
    main()
