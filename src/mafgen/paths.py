"""One table of every file mafgen reads, and how to point it elsewhere.

Nothing here opens anything -- it records where each input is expected and
whether it is there, so --show-paths and the missing-input error can print
the same table.

Inputs resolve in two tiers.  Tier 1 is everything locatable from the
command line alone; tier 2 is the per-phase material, whose names come from
the composed manifest and so is only known once the image loads.  An error
raised before that point shows tier 1 only, which is the tier at fault.
"""
from dataclasses import dataclass, field
from pathlib import Path

# What an absent input costs.  Only REQUIRED aborts: the rest of the
# pipeline degrades on purpose (occupancy falls back to csect ranges, csect
# attribution to the config name, an asm csect to a hex dump).
REQUIRED = "required"
AFFECTS = "changes output"
OPTIONAL = "optional"

_DERIVED = "(derived)"


@dataclass
class Input:
    """One file or directory the tool reads."""
    option: str          # CLI option that overrides it, or _DERIVED
    what: str            # one-line purpose
    path: Path           # absolute
    need: str            # REQUIRED / AFFECTS / OPTIONAL
    pattern: str = ""    # set for a globbed set rather than a single path
    _count: int = field(default=-1, repr=False)

    @property
    def found(self) -> bool:
        if self.pattern:
            return self.count > 0
        return self.path.exists()

    @property
    def count(self) -> int:
        """Members matching a globbed set (-1 when not a set)."""
        if not self.pattern:
            return -1
        if self._count < 0:
            self._count = (len(list(self.path.glob(self.pattern)))
                           if self.path.is_dir() else 0)
        return self._count

    @property
    def status(self) -> str:
        if self.pattern:
            return f"{self.count} found" if self.count else "none found"
        return "found" if self.path.exists() else "MISSING"

    @property
    def display(self) -> str:
        return str(self.path / self.pattern) if self.pattern else str(self.path)


def _abs(p) -> Path:
    return Path(p).expanduser().resolve()


def tier1(config=None, mmu="build/mmu", fcm=None, deck=None, default_deck=None,
          base=None, degrade=None, score=None, strip=None, cache_dir=None):
    """Everything locatable before the image is read."""
    mmu_p = _abs(mmu)
    # Required on the phase load modules, not merely on the directory:
    # FcmImage falls back to ./build/mmu when the root it is handed has
    # none, which would quietly build the listing from a different tree
    # than the one named here.
    out = [Input("--mmu", "con80build/mmu2fcm output root", mmu_p, REQUIRED,
                 pattern="PHASE??.lib")]
    if strip:
        # --strip reads a reference listing and generates nothing
        out.append(Input("--strip", "reference listing to normalize",
                         _abs(strip), REQUIRED))
        return out

    if fcm:
        fcm_p = _abs(fcm)
    elif config:
        fcm_p = mmu_p / config / f"{config}.fcm"
    else:
        # no config named yet: show the shape rather than a 'None' path
        fcm_p = mmu_p / "<config>" / "<config>.fcm"
    out.append(Input("--fcm", "composed memory image", fcm_p, REQUIRED))
    out.append(Input(_DERIVED, "composed manifest (csects, symbols, relocs)",
                     fcm_p.with_name(fcm_p.stem + ".sym.json"), AFFECTS))
    out.append(Input("--deck", "csect AS TYPE classification (BCE/MSC/PATCH)",
                     _abs(deck) if deck else _abs(default_deck), AFFECTS))
    if base:
        out.append(Input("--base", "unpatched image for the '*' marker",
                         _abs(base), REQUIRED))
    if degrade:
        out.append(Input("--degrade", "symbols to leave unresolved",
                         _abs(degrade), REQUIRED))
    if score:
        out.append(Input("--score", "reference listing to score against",
                         _abs(score), REQUIRED))
    if cache_dir:
        out.append(Input(_DERIVED, "SDF name cache (written)",
                         _abs(cache_dir), OPTIONAL))
        out.append(Input(_DERIVED, "regenerated-SDF overlay",
                         _abs(cache_dir) / "sdfregen", OPTIONAL))
    return out


def tier2(mmu, phases=()):
    """Per-phase material, known once the manifest has named the phases.
    Each row is a set rather than one file, so the status is a count --
    the phases are folded into the glob to keep the table short."""
    mmu_p = _abs(mmu)
    return [
        Input(_DERIVED, "per-phase symbol tables (csect attribution)",
              mmu_p, AFFECTS, pattern="PHASE??/PHASE??.sym.json"),
        Input(_DERIVED, "SDF libraries (HAL names, data walk)",
              mmu_p, AFFECTS, pattern="PHASE??/gen/SDFLIB/##*.sdf"),
        Input(_DERIVED, "assembler sidecars (asm statement replay)",
              mmu_p, AFFECTS, pattern="PHASE??/obj/*.asmg.json"),
        Input(_DERIVED, "runtime-library sidecars (HAL library members)",
              mmu_p.parent / "lib" / "runtime", AFFECTS,
              pattern="*/*.asmg.json"),
    ]


def render(inputs, title="mafgen inputs"):
    """The table, as a list of lines.  Paths print relative to a common
    root when they share one, so the interesting part stays on screen; the
    root is named above the table."""
    shown = [i.display for i in inputs]
    root = ""
    if len(shown) > 1:
        common = Path(shown[0]).parent
        while common != common.parent and not all(
                s == str(common) or s.startswith(str(common) + "/")
                for s in shown):
            common = common.parent
        if common != common.parent:
            root = str(common)
            shown = [s[len(root) + 1:] or "." for s in shown]
    cols = [("option", [i.option if i.option != _DERIVED else ""
                        for i in inputs]),
            ("status", [i.status for i in inputs]),
            ("input", [i.what for i in inputs]),
            ("path", shown)]
    width = [max(len(h), *(len(v) for v in vals)) if vals else len(h)
             for h, vals in cols]
    out = [title]
    if root:
        out += ["", f"paths below are under {root}/"]
    out += ["", "  ".join(h.ljust(w) for (h, _v), w in zip(cols, width)),
            "  ".join("-" * w for w in width)]
    for r in zip(*[v for _h, v in cols]):
        out.append("  ".join(c.ljust(w) for c, w in zip(r, width)).rstrip())
    return out


def missing(inputs, need=REQUIRED):
    return [i for i in inputs if i.need == need and not i.found]
