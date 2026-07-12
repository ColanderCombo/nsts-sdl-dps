#!/usr/bin/env python3
#
# HAL/S template dependency ordering.
# 
# Extracted from code/PASS.REL32V0/compilePASS
# Every compilation unit and every D INCLUDE TEMPLATE name is "descored" 
# with underscores removed, truncated to 6 chars
# 
from __future__ import annotations

import re

from . import cards
from pathlib import Path


# From compilePASS, shared halsfc params and CARDTYPES for specific files:
#
_PARM_BASE = ("SREF,LIST,LISTING2,SRN,TEMPLATE,NOLFXI,REGOPT,"
              "LITSTRINGS=3000,CARDTYPE=")
_MODERN = "UDXCVMWC"
_CARDTYPE = {
    "default":  "FCRM",
    "CS2INI":   "ACFCRM",
    "CS4INI":   "ACFCRM",
    "CPTOSV":   "ACBDFCRM",
    "CPUSLS":   "ACBDFCRM",
    "GKDASC":   "AMOCNCRMFDGCHC",
    "GKRORB":   "ACOMNCRCFCGDHC",
    "GKGMNV":   "ACOCNMRMFCGCHD",
    "CV5SLCOM": "DC",
}

_UNIT_HDR = re.compile(r"([A-Z][A-Z0-9_]*)\s*:\s*"
                       r"(?:COMPOOL|PROGRAM|PROCEDURE|FUNCTION|TASK)\b")
_INCL_TMPL = re.compile(r"^D\s+INCLUDE\s+TEMPLATE\s+([A-Z0-9_]+)", re.I)


def descore(s: str) -> str:
    return s.strip().replace("_", "")[:6]


def get_parms(stem: str) -> str:
    if stem.startswith("_"):
        stem = stem[1:]
    return _PARM_BASE + _CARDTYPE.get(stem, _CARDTYPE["default"]) + _MODERN


_UNIT_KIND = re.compile(r"([A-Z][A-Z0-9_]*)\s*:\s*"
                        r"(COMPOOL|PROGRAM|PROCEDURE|FUNCTION|TASK)\b")


def unit_header(path) -> tuple[str | None, str | None]:
    """Return (full unit name, kind) for a HAL file's outermost block"""
    from pathlib import Path
    lines = [cards.code(raw) for raw in open(Path(path), errors="replace")
             if raw[:1] != "C"]
    for i, line in enumerate(lines):
        if _INCL_TMPL.match(line.strip()):
            continue
        m = _UNIT_KIND.search(" ".join(lines[i:i + 3]))
        if m:
            return m.group(1), m.group(2)
    return None, None


def parse_hal(path: Path) -> tuple[str | None, list[str]]:
    """Return (descored unit name, [descored template deps]) for a HAL file.

    The unit header may span cards, so
    the name/keyword are matched against a 3-line join (cf. compilePASS).
    """
    lines = [cards.code(raw) for raw in open(path, errors="replace")
             if raw[:1] != "C"]
    deps: list[str] = []
    name: str | None = None
    for i, line in enumerate(lines):
        m = _INCL_TMPL.match(line.strip())
        if m:
            deps.append(descore(m.group(1)))
            continue
        if name is None:
            joined = " ".join(lines[i:i + 3])
            hm = _UNIT_HDR.search(joined)
            if hm:
                name = descore(hm.group(1))
    # de-dup deps preserving order
    seen: set[str] = set()
    deps = [d for d in deps if not (d in seen or seen.add(d))]
    return name, deps


def tree_units(dirs) -> dict:
    """Index every HAL unit with a parseable block header in the source dirs.

    Returns {descored name: (path, full_name, kind)} over all files in
    `dirs` (directory paths, searched in order), so a D INCLUDE TEMPLATE
    name can be resolved.
    """
    idx: dict = {}
    for sd in dirs:
        sd = Path(sd)
        if not sd.is_dir():
            continue
        for q in sorted(sd.iterdir()):
            if q.is_file():
                full, kind = unit_header(q)
                if full:
                    idx.setdefault(descore(full), (q, full, kind))
    return idx


def template_closure(worklist, tree) -> tuple[list, set]:
    """Transitive D INCLUDE TEMPLATE closure of `worklist` (paths) over the
    tree index
    """
    work_paths = set(worklist)
    work_names: set = set()
    for p in worklist:
        n, _ = parse_hal(p)
        if n:
            work_names.add(n)
    extra: list = []
    seed: set = set()
    queue = list(worklist)
    while queue:
        _, deps = parse_hal(queue.pop())
        for dep in deps:
            if dep in work_names:
                continue
            ent = tree.get(dep)
            if ent is None:                      # external (RUN/ZCON) -- ignore
                continue
            path, _full, kind = ent
            if kind == "PROGRAM":
                seed.add(dep)                    # prune: seeded, body-independent
                continue
            if path in work_paths:
                continue
            work_paths.add(path)
            work_names.add(dep)
            extra.append(path)
            queue.append(path)
    return extra, seed


def topo_order(paths):
    """Order HAL source files so each unit's template producers precede it.
    """
    parsed = {p: parse_hal(p) for p in paths}
    producer = {name: p for p, (name, _) in parsed.items() if name}
    # internal deps = deps that some file in the set produces
    internal = {p: {d for d in deps if d in producer and producer[d] is not p}
                for p, (_, deps) in parsed.items()}

    done: set = set()
    order: list = []
    remaining = list(paths)
    progress = True
    while remaining and progress:
        progress = False
        still: list = []
        for p in remaining:
            if internal[p] <= {parsed[q][0] for q in done}:
                order.append(p); done.add(p); progress = True
            else:
                still.append(p)
        remaining = still
    order.extend(remaining)          # cycles / unproduced deps: attempt anyway
    return order
