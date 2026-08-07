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
# SDL ("Software Development Laboratory") = phase-image mode: drops the START
# bootstrap stub and the program's @0 stack ER (stackless SDL programs receive
# their stack from FCOS at dispatch; the linkage editor creates the @0 CSECTs
# from the CON80 STACK cards).  STACKEND SYM data is identical either way.
_PARM_BASE = ("ADDRS,SREF,LIST,LISTING2,SRN,TEMPLATE,NOLFXI,REGOPT,SDL,"
              "LITSTRINGS=3000,CARDTYPE=")
# ADDRS stores per-statement code addresses in the SDF statement cells
# (root ADDR_FLAG); MAFGEN's ST# lines print them, and the DM2APP
# addresses regenerated from an ADDRS compile match the DASS exactly.
# Codegen-neutral: object decks compiled with and without it differ only
# in the SYM HALS/END compile-time stamp (PASS2 EMIT_ADDRS writes the
# addresses into the statement data file and nowhere else).
# Option provenance (2026-07-18 audit): NOLFXI is DASS-VERIFIED -- every
# LFXI in the flight images (~130/config) sits in an ASM csect
# (FPM*/FIO*/FCM*) or a sourceless Spacelab library member; zero in
# HAL-compiled csects.  REGOPT's leaf-reg gate is subsumed by SDL either
# way (PASS2 INITIALI CR11095).  SDL is OUR addition (not compilePASS),
# DASS-backed: flight images have no per-program START stubs; stacks are
# CON80 STACK-card @0 csects.  LITSTRINGS=3000 is capacity only.
#
# CARDTYPE= is a list of from->to pairs mapping nonstandard column-1
# card-type characters (change flags) onto the standard set (blank/M, C,
# D, E, S).  "TM" maps the 'T' change flag some OI340600 HAL decks carry
# on main statement cards (e.g. APPLSRC/GKFHOR; flight report compiles
# the same SRN as main) -- unmapped, PASS1 turns those cards into
# comments ("ILLEGAL CARD TYPE") and the unit abends.  The U/X/V/W pairs
# are inherited compilePASS guesses and DEAD in OI340600: no
# HAL-classified deck or INCL80 member carries those column-1 chars
# (they appear only in display/ASM decks, which never see this parm).
_MODERN = "UDXCVMWCTM"
_CARDTYPE = {
    # R (INCL80/STRPDT's unqualified NAME-structure declares) defaults to
    # COMMENT, per the OI30 flight listings: the SM compools that include
    # STRPDT (CSAPDT/CS2PDT/CS4PDT/CPGPCD/PGGPCF/PGPPLD/PGSCRU) compile
    # its R cards as comments -- their SDFs export only the 9 CSAS_PDT_*
    # structure templates, so a unit may import several without DQ7/PM1.
    # No other HAL deck or live INCL80 member carries col-1 R cards
    # (GKPMNV's are covered by the GK* per-file parms below, which its
    # header documents verbatim).
    "default":  "FCRC",
    # The 8 PROCEDURE decks whose flight listings compile STRPDT's R
    # cards as MAIN statements (block-local unqualified structures,
    # never exported -- legal inner-scope shadowing of the imported
    # templates; SAFACQ verified by its 18-statement include delta,
    # OI30 report stmts 229->247):
    "SAFACQ":   "FCRM",
    "SCKPNT":   "FCRM",
    "STCCYCL":  "FCRM",
    "SULUPLIN": "FCRM",
    "STMTAB":   "FCRM",
    "SPNINT":   "FCRM",
    "SPPPRECO": "FCRM",
    "SRESTO":   "FCRM",
    "CS2INI":   "ACFCRC",
    "CS4INI":   "ACFCRC",
    # Flight OI30 compiles CPTOSV/CPUSLS per-OPS (SM preprocessor
    # configs; the listing is the SM2O form, "ACBCFCRC").  We build the
    # both-OPS configuration instead (B->D activates the CS4_PDT include;
    # CS2/CS4 declare sets are disjoint) so one object serves the SM2 and
    # SM4 phase links -- a deliberate config choice, revisit for per-OPS
    # fidelity.
    "CPTOSV":   "ACBDFCRC",
    "CPUSLS":   "ACBDFCRC",
    "GKDASC":   "AMOCNCRMFDGCHC",
    "GKRORB":   "ACOMNCRCFCGDHC",
    "GKGMNV":   "ACOCNMRMFCGCHD",
    # CV5SLCOM historically carried "DC" (comment out its CPSSLD include) —
    # an SDL-era workaround; the flight listing compiles the include
    # ACTIVE with zero diagnostics, which works since the build stopped
    # injecting preprocessHALSFC's hoisted includes.  Entry gone.
}

_UNIT_HDR = re.compile(r"([A-Z][A-Z0-9_]*)\s*:\s*"
                       r"(?:COMPOOL|PROGRAM|PROCEDURE|FUNCTION|TASK)\b")
# Column 1 is the card-type character; the directive text starts in column 2,
# so `DINCLUDE TEMPLATE X` (no blank after the D) is as valid as
# `D INCLUDE TEMPLATE X` (cf. APPLSRC/GEHRTL et al. in OI340600).
_INCL_TMPL = re.compile(r"^D\s*INCLUDE\s+TEMPLATE\s+([A-Z0-9_]+)", re.I)


def descore(s: str) -> str:
    return s.strip().replace("_", "")[:6]


def get_parms(stem: str) -> str:
    if stem.startswith("_"):
        stem = stem[1:]
    return _PARM_BASE + _CARDTYPE.get(stem, _CARDTYPE["default"]) + _MODERN


_UNIT_KIND = re.compile(r"([A-Z][A-Z0-9_]*)\s*:\s*"
                        r"(COMPOOL|PROGRAM|PROCEDURE|FUNCTION|TASK)\b")


def _cardtype_map(stem: str) -> dict[str, str] | None:
    """Column-1 change-flag -> standard card-type map for a variant deck.

    Only files with a per-file _CARDTYPE entry get a map (their column-1
    chars are variant change flags whose meaning the CARDTYPE parm pairs
    assign, cf. PASS1 INITIALI.xpl); everything else returns None and
    parses with the standard card types.  The map includes the shared
    _MODERN pairs, mirroring what get_parms hands the compiler.
    """
    if stem.startswith("_"):
        stem = stem[1:]
    ct = _CARDTYPE.get(stem)
    if ct is None:
        return None
    ct += _MODERN
    return dict(zip(ct[0::2], ct[1::2]))


def _code_lines(path) -> list[str]:
    """The file's live code lines, column-1 card type resolved.

    Comment cards drop out.  On a variant deck (per-file CARDTYPE entry)
    each change-flagged line is rewritten with its *mapped* type in
    column 1, so e.g. GKRORB's inactive `A GKD_...: PROCEDURE` header
    (A->C) disappears while the active `O GKR_...: PROCEDURE` (O->M)
    and a B-flagged `B INCLUDE TEMPLATE ...` (B->D, CPTOSV) read as the
    code/directive cards the compiler sees.
    """
    path = Path(path)
    cmap = _cardtype_map(path.stem)
    lines: list[str] = []
    for raw in open(path, errors="replace"):
        c = raw[:1]
        t = cmap.get(c, c) if cmap else c
        if t == "C":
            continue
        line = cards.code(raw)
        if cmap and c in cmap:
            line = (t if t in "DES" else " ") + line[1:]
        lines.append(line)
    return lines


def unit_header(path) -> tuple[str | None, str | None]:
    """Return (full unit name, kind) for a HAL file's outermost block"""
    lines = _code_lines(path)
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
    lines = _code_lines(path)
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


# A DFG display-deck INCLUDE=<compool> statement card (columns 1-2; the
# name may be punched full (CGZ_FLD_MC_16_3) or already descored (CGSGNC)).
_DISP_INCL = re.compile(r"^\s{0,4}INCLUDE\s*=\s*([A-Z][A-Z0-9_]*)")


def display_deps(path) -> list[str]:
    """Descored template names a DFG display deck requires (INCLUDE= cards).

    The generated display compool `D INCLUDE TEMPLATE`s these, so their
    producers must be in the template closure even when no worklist HAL
    unit references them (dfg also needs their SDF members to type the
    deck -- without them it falls back to NAME BIT(16) declarations that
    can never compile)."""
    deps: list[str] = []
    seen: set[str] = set()
    for raw in open(Path(path), errors="replace"):
        m = _DISP_INCL.match(cards.code(raw))
        if m:
            d = descore(m.group(1))
            if d not in seen:
                seen.add(d)
                deps.append(d)
    return deps


def template_closure(worklist, tree, seed_names=()) -> tuple[list, set]:
    """Transitive D INCLUDE TEMPLATE closure of `worklist` (paths) over the
    tree index.  `seed_names` are additional descored template names to
    close over (e.g. display-deck INCLUDE= requirements) whose producers
    are closure-only even if no worklist unit references them.
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

    def add_dep(dep):
        """Admit one descored dependency name; return its path if it is a
        new closure-only producer whose own deps must be walked."""
        if dep in work_names:
            return None
        ent = tree.get(dep)
        if ent is None:                          # external (RUN/ZCON) -- ignore
            return None
        path, _full, kind = ent
        if kind == "PROGRAM":
            seed.add(dep)                        # prune: seeded, body-independent
            return None
        if path in work_paths:
            return None
        work_paths.add(path)
        work_names.add(dep)
        extra.append(path)
        return path

    for dep in seed_names:
        p = add_dep(dep)
        if p is not None:
            queue.append(p)
    while queue:
        _, deps = parse_hal(queue.pop())
        for dep in deps:
            p = add_dep(dep)
            if p is not None:
                queue.append(p)
    return extra, seed


def topo_order(paths):
    """Order HAL source files so each unit's template producers precede it.

    Edges into PROGRAM producers are ignored: a program template is
    body-independent and stub-seeded before any real compile (see the
    seeding step in con80build/con80cmake), so a program's template never
    gates its dependents.  Without this the OI340600 display cluster's
    program<->procedure reference cycle (a 29-unit SCC) defeats the sort
    and those units fall back to source order, needing one retry round
    per dependency level; with program edges dropped the remaining graph
    is a DAG and a single pass suffices.
    """
    parsed = {p: parse_hal(p) for p in paths}
    producer = {name: p for p, (name, _) in parsed.items() if name}
    seeded = {name for name, p in producer.items()
              if unit_header(p)[1] == "PROGRAM"}
    # internal deps = deps that some file in the set produces
    internal = {p: {d for d in deps if d in producer and producer[d] is not p
                    and d not in seeded}
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
