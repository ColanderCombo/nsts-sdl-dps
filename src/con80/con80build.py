#!/usr/bin/env python3
#
# Build a PFS flight load from a CON80 deck:
# 
#   1. resolve   -- read the CON80 deck, load the worklist, and map each
#                   object-module name to its source file.
#   2. classify  -- decide HAL/S vs assembly per source.
#   3. assemble  -- run asm101 on the assembly sources
#   4. hal       -- run halsc on the HAL sources in template-dependency order
#   5. display   -- uses dfg to generate HAL/S COMPOOLS from display decks.
#   6. link      -- run lnk101 with --concard emitting <out>/<target>.fcm
# 
# Object output goes under <out>/obj
# Generated intermediates go under <out>/gen
#
# --emit-cmake FILE freezes the resolved worklist into a standalone CMake
#
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

from ap101Utils import cards, concard, halorder

from . import compilecache


def _short(tok: str) -> str:
    """Relativize path(-valued) tokens under the CWD so verbose command
    echoes stay readable (`--object=/abs/.../x.obj` -> `--object=gen/x.obj`)."""
    cwd = os.getcwd() + os.sep
    opt, sep, val = tok.partition("=")
    if sep and val.startswith(cwd):
        return opt + "=" + val[len(cwd):]
    if tok.startswith(cwd):
        return tok[len(cwd):]
    return tok


# Build profiling.  Setting CON80BUILD_TIMING=<file> makes every toolchain
# subprocess and every in-process step append a record to <file>
_TIMING = os.environ.get("CON80BUILD_TIMING") or None


def _tlog(**rec) -> None:
    if not _TIMING:
        return
    rec.setdefault("pid", os.getpid())
    line = (json.dumps(rec) + "\n").encode()
    fd = os.open(_TIMING, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _timed(kind: str, name: str = "", **extra):
    """Time an in-process build step into the timing log."""
    if not _TIMING:
        yield
        return
    t0 = time.time()
    try:
        yield
    finally:
        _tlog(kind=kind, name=name, t0=t0, dt=time.time() - t0, **extra)


def _run(cmd, *, verbose: int = 0, env=None, cwd=None, label: str | None = None):
    if verbose:
        loc = f"   # cwd={cwd}" if cwd else ""
        tag = f"[{label}] " if label else ""
        print(f"+ {tag}" + " ".join(shlex.quote(_short(str(c))) for c in cmd)
              + loc, flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    if _TIMING:
        _tlog(kind="proc", tool=Path(str(cmd[0])).name, label=label or "",
              t0=t0, dt=time.time() - t0, rc=r.returncode)
    if verbose:
        out = (r.stdout or "") + (r.stderr or "")
        if out.strip():
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
            sys.stdout.flush()
    return r


_JOBS = 1
_PHASE_JOBS = 1

_CACHE_ROOT: str | None = None  # Root of the cross-phase compile cache (--cache DIR)
_CACHE_VERIFY = False           # whether every hit is re-verified against a fresh compile.


def _pmap(fn, items, jobs: int | None = None) -> list:
    """`[fn(i) for i in items]`, up to `jobs` at a time, results in input
    order.  The workers are threads because the work is subprocesses."""
    items = list(items)
    n = jobs or _JOBS
    if n <= 1 or len(items) <= 1:
        return [fn(i) for i in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(n, len(items))) as ex:
        return list(ex.map(fn, items))


# Directory of the installed tool wrappers (asm101/lnk101/dfg/... shell
# shims beside the halsc wrapper) — set by main() from --halsc.  When a
# wrapper exists the subprocesses run through it, keeping verbose command
# echoes short; otherwise fall back to `<venv python> -m <module>`.
_BINDIR: Path | None = None


def _tool(module: str, python: str) -> list[str]:
    """Command prefix for a venv tool: its wrapper, else `python -m module`."""
    if _BINDIR is not None:
        w = _BINDIR / module
        if w.is_file() and os.access(w, os.X_OK):
            return [str(w)]
    return [python, "-m", module]


# lnk101 -L default (also validated up front by _require_dirs).
_DEFAULT_LINKLIBS = (Path("build") / "lib" / "runtime" / "RUN",
                     Path("build") / "lib" / "runtime" / "ZCON")


def _require_dirs(entries) -> None:
    """Fail when required directories are absent, BEFORE any toolchain
    call: report every missing one so a single fix-up round suffices."""
    missing = [(opt, Path(p)) for opt, p in entries
               if p is not None and not Path(p).is_dir()]
    if not missing:
        return
    for opt, p in missing:
        where = "" if p.is_absolute() else f" (cwd-relative: {p.resolve()})"
        print(f"con80build: {opt} directory not found: {p}{where}",
              file=sys.stderr)
    n = len(missing)
    print(f"con80build: {n} required director{'y' if n == 1 else 'ies'} "
          "missing; nothing was run", file=sys.stderr)
    raise typer.Exit(2)


SOURCE_DIRS = ("SSSRC", "APPLSRC")
INCL_DIR = "INCL80"        # overlays like SOURCE_DIRS, see _as_dirs
CON80_DIR = "CON80"        # overlays too, see _con80_overlay
CON80_MIRROR = "con80"     # where the merged deck is materialized under --out
_CODED_PREFIX = "#$@"
# Names that are runtime/library/autocall targets, not buildable flight source.
_RUNTIME_RE = re.compile(r"^(#?[QZ]|#?L|#?0|\$0|\$Y|\$X|#Y|#T)")

# Patch-area decks: a source PCHnnSRC (SSSRC) is assembled to an object
# whose CON80 object-library member name is PCHnnTXT -- the deck pulls it in
# with INCLUDE SYSLIBL1(PCHnnTXT) and then INSERTs the csects it defines.
_PATCH_SRC_RE = re.compile(r"^PCH\d+SRC$")
_PATCH_MEMBER_RE = re.compile(r"^PCH\d+TXT$")


def patch_member(src_name: str) -> str:
    """PCHnnSRC source filename -> PCHnnTXT object-library member name."""
    return src_name[:-3] + "TXT"


# An IS-macro invocation in a patch deck: <label> IS <len>,<prefix>,<fill>.
# The macro emits a CSECT named <prefix><label>; the prefix carries over
# from the previous IS line when omitted (it is a GBLC in the macro).
_IS_LINE = re.compile(r"^(\S+)\s+IS\s+([^,\s]+)(?:,([^,\s]*))?")


def patch_csects(path: Path) -> list[str]:
    """CSECT names a patch source (PCHnnSRC) defines, in file order.

    Replays the IS macro's <prefix><label> naming -- the prefix
    (2nd operand) defaults to the previous line's when omitted -- without a full
    macro expansion (every PCH deck is purely IS invocations).  Used both to
    account for the deck's INSERTed csects in the worklist and to map each to
    its providing source """
    names: list[str] = []
    prefix = ""
    for ln in path.read_text(errors="replace").splitlines():
        t = cards.code(ln)
        if not t.strip() or t[0] == "*":
            continue
        m = _IS_LINE.match(t)
        if not m:
            continue
        label, _len, pfx = m.group(1), m.group(2), m.group(3)
        if pfx:
            prefix = pfx
        names.append(prefix + label)
    return names


class SourceIndex:
    def __init__(self, dirs):
        self.dirs = [Path(d) for d in dirs]
        self.by_name: dict[str, Path] = {}
        self.by_stem6: dict[str, Path] = {}
        for sub in self.dirs:
            if not sub.is_dir():
                continue
            for p in sorted(sub.iterdir()):
                if not p.is_file():
                    continue
                self.by_name.setdefault(p.name, p)
                self.by_stem6.setdefault(p.name[:6], p)
                # Some source filenames embed a '#' that is NOT part of the
                # 6-char stem encoded in the csect name (e.g. the file
                # DCI#STK backs csect #DDCISTK -> stem DCISTK).
                # Index a '#'-stripped stem too so those resolve; setdefault
                # keeps an exact-named file's claim on the key.
                stripped = p.name.replace("#", "")[:6]
                if stripped != p.name[:6]:
                    self.by_stem6.setdefault(stripped, p)

    def resolve(self, module: str) -> Path | None:
        """Map an object-module/csect name to its source file, or None."""
        # A patch object-library member (PCHnnTXT) is the assembled PCHnnSRC.
        if _PATCH_MEMBER_RE.match(module):
            return self.by_name.get(module[:-3] + "SRC")
        if module and module[0] in _CODED_PREFIX:
            stem = module[2:8]                      # drop 2-char type code
            return self.by_stem6.get(stem[:6])
        # plain assembly csect: name is (a prefix of) the source-file name
        if module in self.by_name:
            return self.by_name[module]
        return self.by_stem6.get(module[:6])

    def csect_defs(self) -> dict[str, Path]:
        """csect name -> the source file that DEFINES it (via CSECT/START/COM).

        The filename-stem heuristic in resolve() conflates split-source modules
        whose data/entry csects live in separate files that share a stem (e.g.
        #DDCICYC is defined in DCI#DATA, #EDCICYC in DCI#PDE, yet both stem-
        resolve to the code file DCICYC).  Scanning the actual CSECT definitions
        is authoritative; used as the primary resolver so an INSERTed csect maps
        to the file that really assembles it.  ENTRY/DEF lines are deliberately
        excluded -- they name symbols within a csect (and can appear in operand
        comments), not csect definitions.  Cached; first-defining file wins."""
        if getattr(self, "_csect_defs", None) is None:
            defs: dict[str, Path] = {}
            for sub in self.dirs:
                if not sub.is_dir():
                    continue
                for p in sorted(sub.iterdir()):
                    if not p.is_file():
                        continue
                    try:
                        text = p.read_text(errors="replace")
                    except OSError:
                        continue
                    for ln in text.splitlines():
                        m = _CSECT_DEF.match(cards.code(ln))
                        if m:
                            defs.setdefault(m.group(1), p)
            self._csect_defs = defs
        return self._csect_defs


# Heuristics to decide whether a file is HAL / ASM / DISPLAY / AMT code:
#
# A HAL compilation-unit header -- name : COMPOOL|PROGRAM|... -- but the
# colon and the keyword may sit on different cards, so it is matched against a
# join of a few consecutive code lines (cf. PASS.REL32V0/compilePASS).
_HAL_HDR = re.compile(r"[A-Z][A-Z0-9_]*\s*:\s*"
                      r"(COMPOOL|PROGRAM|PROCEDURE|FUNCTION|TASK)\b")
# A HAL compiler directive card (D INCLUDE TEMPLATE ..., D DEFINE ...).
_HAL_DIR = re.compile(r"^D\s+(INCLUDE|DEFINE|VERSION|CLOSE|REPLACE|ORDER)\b")
# An AP-101S assembly header: an optional-label defining op, OR the FCOS
# module-defining macros (LABEL PROGRAM TITLE=… / LABEL TASK …) which
# REQUIRE a column-1 label -- HAL's PROGRAM; is indented, so it won't match.
_ASM_HDR = re.compile(r"^(?:[A-Z@#$][A-Z0-9@#$]*)?\s+"
                      r"(TITLE|CSECT|START|MACRO|DSECT|COM)\b"
                      r"|^[A-Z@#$][A-Z0-9@#$]*\s+(PROGRAM|TASK)\b")


def classify(path: Path) -> str:
    """Return 'HAL', 'ASM', 'DISPLAY', or 'AMT' for a source file"""
    code = []
    with open(path, errors="replace") as f:
        for raw in f:
            line = cards.code(raw).rstrip("\n")
            s = line.strip()
            if not s or line[0] == "*":          # blank / assembly comment
                continue
            if line[0] == "C":                   # HAL comment card
                code.append(("C", line)); continue
            code.append(("?", line))
    seen_c = False
    for i, (kind, line) in enumerate(code):
        if kind == "C":
            seen_c = True
            continue
        if _HAL_DIR.match(line):
            return "HAL"
        # HAL colon-form header may span cards -> test a short join first, so
        # name: PROGRAM is never mistaken for the asm PROGRAM macro.
        joined = " ".join(l for _, l in code[i:i + 3])
        if _HAL_HDR.search(joined):
            return "HAL"
        if _ASM_HDR.match(line):
            return "ASM"
        # DFG display-deck statement cards begin in column 1 or 2 (CG1011's
        # ` HEADER=1011G,` has a leading blank); the indent cap keeps an asm
        # operand field (col 10+) like `...,INCLUDE=X` from matching.
        if re.match(r"^\s{0,4}(HEADER|INCLUDE|PAD|XC|YC|CHAR)\s*=", line):
            return "DISPLAY"
        # DFG AMT-mode deck (CDAPnn): PMF=/AMTG=/AMTP=/AMTS= command cards.
        # dfg generates the CDA_Pnn_AMT moding-table compool from these.
        # (The hand-built CDAP02 never reaches here: its D INCLUDE cards
        # classify it HAL above.)
        if re.match(r"^\s{0,4}(PMF|AMT[GPS])\s*=\s*\(", line):
            return "AMT"
    return "HAL" if seen_c else "ASM"


def runtime_names(runlibs) -> set[str]:
    """Collect the csect/object-module names supplied by the runtime libraries.

    `runlibs` are directories of runtime source (RUNASM/ZCONASM, *.asm) or
    built objects (*.obj); the provided name is each file's stem.  These are
    the HAL/S runtime + ZCON members (SQRT, EXP, CPAS, VV0DN, #QSQRT, ...) that
    are built separately (cmake BuildRuntime) and pulled in by lnk101 -L at
    link, so the worklist should treat them as runtime rather than unresolved.
    """
    names: set[str] = set()
    for d in runlibs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file():
                names.add(p.stem)
    return names


# A csect/section definition (ASM NAME CSECT|START|COM) and the names a
# module makes available (ENTRY/DEF -- NOT EXTRN, which only references).
_CSECT_DEF = re.compile(r"^([A-Z@#$][A-Z0-9@#$]{0,7})\s+(?:CSECT|START|COM)\b")
_DEF_OP = re.compile(r"^\s+(?:ENTRY|DEF)\s+(.+)")
_NAME_OK = re.compile(r"^[A-Z@#$][A-Z0-9@#$]{0,7}$")


def included_csects(graph, src: SourceIndex) -> dict:
    """Map every csect/ENTRY name DEFINED in an INCLUDE <lib>(...) member
    file to that file.  The CON80 deck names the providing files via INCLUDE
    (e.g. INCLUDE SYSLIBL1(BILDNEW5,MENU12,MMULDTBL)) and then INSERTs
    csects those files contain (INSERT GPCIPL -> CSECT GPCIPL in BILDNEW5).
    The csect name often differs from the filename, so scan the named files to
    recover the mapping."""
    provided: dict[str, Path] = {}
    for mem in graph.library_members():
        f = src.resolve(mem)
        if f is None or not f.is_file():
            continue
        # Patch decks define their csects through the IS macro, so the generic
        # CSECT/ENTRY scan finds nothing -- derive the names from the IS lines.
        if _PATCH_SRC_RE.match(f.name):
            for nm in patch_csects(f):
                provided.setdefault(nm, f)
            continue
        for ln in f.read_text(errors="replace").splitlines():
            t = cards.code(ln)
            m = _CSECT_DEF.match(t)
            if m:
                provided.setdefault(m.group(1), f)
                continue
            d = _DEF_OP.match(t)
            if d:
                for nm in re.split(r"[,\s]+", d.group(1).strip()):
                    if _NAME_OK.match(nm):
                        provided.setdefault(nm, f)
    return provided


def patch_index(src: SourceIndex) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for sub in src.dirs:
        if not sub.is_dir():
            continue
        for p in sorted(sub.iterdir()):
            if p.is_file() and _PATCH_SRC_RE.match(p.name):
                for nm in patch_csects(p):
                    idx.setdefault(nm, p)
    return idx


def resolve_worklist(deck_dir: Path, root: str, src: SourceIndex,
                     runlib_names: set[str] | None = None):
    """Return (by_type, runtime, unresolved, patches):
      by_type:    {'ASM': [Path...], 'HAL': [Path...]} de-duplicated source files
      runtime:    [names] resolved to runtime/library (built elsewhere)
      unresolved: [names] with no source and not obviously runtime
      patches:    {PCHnnSRC Path -> PCHnnTXT member} patch-area decks to assemble

    A name not found in the main source tree is classed runtime if it is in
    `runlib_names` (a runtime-library member) or matches the runtime/autocall
    name pattern; otherwise it is unresolved.  Patch-deck members (the
    PCHnnTXT object and every csect its PCHnnSRC defines) are routed to
    `patches` so they assemble to PCHnnTXT.obj rather than the generic path.
    """
    runlib_names = runlib_names or set()
    deck = concard.ConcardDeck(str(deck_dir))
    graph = concard.build_graph(deck, root)
    modules = graph.object_modules()

    provided = included_csects(graph, src)
    pidx = patch_index(src)

    by_type: dict[str, list[Path]] = {"ASM": [], "HAL": [], "DISPLAY": [],
                                      "AMT": []}
    patches: dict[Path, str] = {}
    seen: set[Path] = set()
    runtime: list[str] = []
    unresolved: list[str] = []
    csect_defs = src.csect_defs()
    for m in modules:
        # A definitive CSECT definition in the source tree wins over the
        # filename-stem heuristic (which conflates split-source csects like
        # #DDCICYC -> DCI#DATA with the code file DCICYC).
        path = csect_defs.get(m) or src.resolve(m) or provided.get(m)
        if path is not None and _PATCH_SRC_RE.match(path.name):
            patches.setdefault(path, patch_member(path.name))
            continue
        if path is None:
            pf = pidx.get(m)
            if pf is not None:
                patches.setdefault(pf, patch_member(pf.name))
                continue
            # A bare runtime name (SQRT, CPAS, ...) or a CSECT name whose stem is
            # a runtime member (#QSQRT -> SQRT)
            stem = m[2:] if (m and m[0] in _CODED_PREFIX) else m
            is_rt = (m in runlib_names or stem in runlib_names
                     or bool(_RUNTIME_RE.match(m)))
            (runtime if is_rt else unresolved).append(m)
            continue
        if path in seen:
            continue
        seen.add(path)
        by_type[classify(path)].append(path)
    return by_type, runtime, unresolved, patches


def library_stub_syms(deck_dir: Path, root: str) -> set[str]:
    """Symbols named by autocall-suppressing LIBRARY cards anywhere in the
    target's CONCARDS expansion.  The LE ran WITH automatic library call
    unless the deck says NOCALLER; these cards name the symbols autocall
    must leave unresolved.  Two forms:
      * `LIBRARY *(a,b,...)` -- restricted no-call (config stubs);
      * `LIBRARY (a,b,...)`  -- blank ddname: member-level suppression of
        modules supplied by a LATER job of the same config."""
    deck = concard.ConcardDeck(str(deck_dir))
    out: set[str] = set()
    for _, d in concard.expand_directives(deck, root):
        if d.verb == "LIBRARY" and d.operand.startswith(("*(", "(")):
            body = d.operand.lstrip("*")[1:].rstrip(")")
            out.update(s.strip() for s in body.split(",") if s.strip())
    return out


def stack_seed_modules(deck_dir: Path, root: str) -> list[str]:
    """$0<prog> operands of STACK cards in the expansion -- the SDL linkedit
    creates the @0 stack from the module's call tree, so a STACK card forces
    the module into the link even without an INSERT."""
    deck = concard.ConcardDeck(str(deck_dir))
    return [d.operand for _, d in concard.expand_directives(deck, root)
            if d.verb == "STACK" and d.operand and d.operand.startswith("$0")]


_CHANGE_OPERAND = re.compile(r"([A-Z0-9#@$]+)\(([A-Z0-9#@$]+)\)")


def concard_changes(deck_dir: Path, root: str) -> dict[str, dict[str, str]]:
    """`CHANGE old(new)` renames of the expansion, keyed by INCLUDE member.

    The CESD rename chain a CHANGE builds applies to the one module supplied
    by the next module-reading card -- in the CON80 decks always the
    following `INCLUDE ddname(member)` (LE PLM p.28/35; several CHANGEs may
    stack before one INCLUDE), and it covers every ESD entry type: SD, LD and
    ER.  Same rule lnk101's Linker.applyConcardChanges applies to the loaded
    modules; autocall has to see the same CESD, because the LE resolved its
    automatic library call against the renamed entries.  Reading the ERs raw
    makes autocall demand the pre-CHANGE name, which pulls both the old and
    the new csect into the link and leaves the renamed target's own compool
    unresolved.

    Merged per member: one member reached by several CHANGE groups sees the
    union, which is what the LE's per-INCLUDE chains add up to."""
    deck = concard.ConcardDeck(str(deck_dir))
    out: dict[str, dict[str, str]] = {}
    pending: dict[str, str] = {}
    for _, d in concard.expand_directives(deck, root):
        if d.verb == "CHANGE":
            m = _CHANGE_OPERAND.match((d.operand or "").strip())
            if m:
                pending[m.group(1)] = m.group(2)
        elif d.verb == "INCLUDE" and pending:
            operand = (d.operand or "").strip()
            m = re.match(r"[A-Z0-9]+\(([^)]*)\)", operand)
            member = (m.group(1) if m else operand).strip()
            if member:
                out.setdefault(member, {}).update(pending)
            pending = {}
    return out


def autocall_scan(objs, external_defs: set[str],
                  stub_syms: set[str], runlib_names: set[str],
                  changes: dict[str, dict[str, str]] | None = None):
    """One automatic-library-call wave: scan the LINK SET's objects' ESDs,
    return (ordered unresolved-ER names, defined names).  Generated-csect
    name classes (@stacks, #Z zcon stubs, #T checksums, patch/fill areas)
    never resolve from source and are skipped.

    `changes` is concard_changes()'s {member: {old: new}}: an object module
    that supplies one of those INCLUDE members is scanned through the rename,
    exactly as the LE saw it."""
    from ap101Utils import objModule
    from ap101Utils.objModule import EsdType
    changes = changes or {}
    defined = set(external_defs)
    order: list[str] = []
    for p in sorted(objs):
        try:
            f = objModule.read(p)
        except Exception:
            continue
        for m in f.modules:
            rename: dict[str, str] = {}
            if changes:
                for e in m.esdEntries:
                    if e.type in (EsdType.SD, EsdType.LD):
                        r = changes.get((e.name or "").strip())
                        if r:
                            rename.update(r)
            for e in m.esdEntries:
                n = (e.name or "").strip()
                if not n:
                    continue
                n = rename.get(n, n)
                if e.type in (EsdType.SD, EsdType.LD):
                    defined.add(n)
                elif e.type in (EsdType.ER, EsdType.WX) and n not in order:
                    order.append(n)
    need = []
    for n in order:
        if n in defined or n in stub_syms:
            continue
        if n.startswith("#Z"):
            # a ZCON stub (linker-synthesized) targets the module's own
            # #C<stem> compool-procedure csect: autocall hunts the TARGET
            tgt = "#C" + n[2:]
            if tgt not in defined and tgt not in stub_syms \
                    and tgt not in need:
                need.append(tgt)
            continue
        if n.startswith(("@", "#T", "#X", "#Y", "$X", "$Y", "$Z")):
            continue
        stem = n[2:] if n[0] in _CODED_PREFIX else n
        if n in runlib_names or stem in runlib_names or _RUNTIME_RE.match(n):
            continue
        need.append(n)
    return need, defined


# --halt-on-error: stop the build at the first failing member:
_HALT_ON_ERROR = False


def _halt(msg: str) -> None:
    if _HALT_ON_ERROR:
        print(f"  [halt] {msg}", file=sys.stderr)
        sys.exit(1)


def _assemble(items, objdir: Path, mlib: Path, python: str,
              tolerable: int = 4, verbose: int = 0,
              tag: str = "asm101") -> tuple[int, list[str]]:
    objdir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1"}
    n = len(items)

    def one(job):
        i, (srcpath, obj) = job
        cmd = [*_tool("asm101", python),
               f"--object={obj}", f"--library={mlib}",
               f"--tolerable={tolerable}", str(srcpath)]
        r = _run(cmd, verbose=verbose, env=env,
                 label=f"{tag} {i}/{n} {srcpath.name}")
        if r.returncode == 0 and obj.exists():
            return None
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"  [{tag} FAIL {i}/{n}] {srcpath.name}: {tail[0]}",
              file=sys.stderr)
        return srcpath.name

    results = _pmap(one, list(enumerate(items, 1)))
    failures = [r for r in results if r is not None]
    for name in failures:
        _halt(f"{tag} {name} failed")
    return n - len(failures), failures


def assemble(sources, objdir: Path, mlib: Path, python: str,
             tolerable: int = 4, verbose: int = 0) -> tuple[int, list[str]]:
    """Assemble each source with asm101 into objdir/<stem>.obj.  Returns
    (ok_count, failures)."""
    items = [(s, objdir / (s.stem + ".obj")) for s in sources]
    return _assemble(items, objdir, mlib, python, tolerable, verbose)


def assemble_patches(patches: dict, objdir: Path, mlib: Path, python: str,
                     tolerable: int = 4, verbose: int = 0) -> tuple[int, list[str]]:
    """Assemble each patch source (PCHnnSRC) into objdir/PCHnnTXT.obj --
    the object-library member name the CON80 deck INCLUDEs.  `patches` is the
    {source Path -> member name} map from :func:`resolve_worklist`.  Returns
    (ok_count, failures)."""
    items = [(s, objdir / (member + ".obj"))
             for s, member in sorted(patches.items())]
    return _assemble(items, objdir, mlib, python, tolerable, verbose,
                     tag="asm101 patch")


def _disp_overlays(gen_tree: Path | None, phase: int | None) -> list[Path]:
    """display overlay directories, most specific first."""
    if gen_tree is None:
        return []
    dirs = []
    if phase is not None:
        d = gen_tree / f"phase{phase:02d}" / "displays"
        if d.is_dir():
            dirs.append(d)
    d = gen_tree / "displays"
    if d.is_dir():
        dirs.append(d)
    return dirs


def _overlay_hal(overlays, stem: str) -> Path | None:
    """The first overlay holding `<stem>.hal`, or None."""
    for d in overlays or ():
        pre = d / f"{stem}.hal"
        if pre.is_file():
            return pre
    return None


def _deck_template_seeds(path: Path, overlays=()) -> list[str]:
    """Descored template names a DFG deck (display or AMT) requires.  
    Display decks: INCLUDE= compools.  
    AMT decks: the derived DFB/DMMD compools + every OPGM/SPGM 
               control-segment program the generated compool 
               D INCLUDE TEMPLATEs.
    """
    extra = []
    pre = _overlay_hal(overlays, path.stem)
    if pre is not None:
        extra = halorder.parse_hal(pre)[1]
    from dfg import amt
    if amt.is_amt_deck(path):
        try:
            deck = [halorder.descore(n)
                    for n in amt.template_names(amt.parse(path))]
        except amt.AmtError:
            deck = []
    else:
        deck = halorder.display_deps(path)
    seen: set = set()
    return [d for d in [*deck, *extra] if not (d in seen or seen.add(d))]


def _native_comment_chars(stem: str) -> str:
    """Column-1 chars that are native card types remapped to comment by
    the deck's per-file CARDTYPE entry — the remaps REL32V0 cannot honour
    itself (empty string for most decks)."""
    cmap = halorder._cardtype_map(stem)
    if not cmap:
        return ""
    return "".join(sorted(j for j, k in cmap.items()
                          if j in "EMSCD" and k == "C" and j != "C"))


def comment_native_cards(text: str, chars: str) -> str | None:
    """Comment every card whose column-1 char is in `chars` (col-1 -> C,
    columns preserved).  None when nothing matched."""
    lines = text.splitlines(keepends=True)
    changed = False
    for i, ln in enumerate(lines):
        if ln[:1] and ln[0] in chars:
            lines[i] = "C" + ln[1:]
            changed = True
    return "".join(lines) if changed else None


def _mirror_rewrite(path: Path) -> str | None:
    """The materialized mirror text for a deck that any build-layer
    source rewrite applies to, or None to mirror the pristine file.
    Currently only native-CARDTYPE comment remaps."""
    chars = _native_comment_chars(path.stem)
    if not chars:
        return None
    return comment_native_cards(path.read_bytes().decode("latin-1"), chars)


def _as_dirs(dirs) -> list:
    """`None`, one path, or a search list -> a list of paths, in order."""
    if dirs is None:
        return []
    if isinstance(dirs, (str, Path)):
        return [Path(dirs)]
    return [Path(d) for d in dirs]


def _hal_mirror(dirs, haltree) -> None:
    """Build a .hal-extensioned mirror of the (extensionless) HAL source
    dirs.  Each directory is mirrored under its basename.

    Most entries are symlinks to the pristine source.  Decks needing a
    build-layer source rewrite (see _mirror_rewrite: native-CARDTYPE
    comment remaps) are materialized as transformed copies instead, so
    every consumer of the mirror sees the rewritten view — the pristine
    tree is never modified."""
    haltree = Path(haltree)
    # Distinct source dirs can share a basename (the gen/ overlay's SSSRC
    # shadows the delivered SSSRC) and so share a mirror subdir: the FIRST
    # dir claiming a name this run wins, and a link left by an earlier run
    # pointing elsewhere is re-pointed, never trusted.
    claimed: set = set()
    for srcdir in dirs:
        srcdir = Path(srcdir)
        if not srcdir.is_dir():
            continue
        dst = haltree / srcdir.name
        dst.mkdir(parents=True, exist_ok=True)
        for f in srcdir.iterdir():
            if not f.is_file():
                continue
            link = dst / (f.name + ".hal")
            if link in claimed:
                continue
            claimed.add(link)
            new = _mirror_rewrite(f)
            if new is not None:
                data = new.encode("latin-1")
                if link.is_symlink() or (
                        link.exists() and link.read_bytes() != data):
                    link.unlink()
                if not link.exists():
                    link.write_bytes(data)
            else:
                if link.is_symlink():
                    if link.resolve() != f.resolve():   # stale (or dangling)
                        link.unlink()
                elif link.exists():                     # stale materialized copy
                    link.unlink()
                if not link.is_symlink() and not link.exists():
                    link.symlink_to(f.resolve())


def _con80_mirror(dirs, dst) -> Path:
    """Generate a single directory containing the resolved members 
       including gen/ overlays"""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    claimed: set = set()
    for deckdir in dirs:
        deckdir = Path(deckdir)
        if not deckdir.is_dir():
            continue
        for f in sorted(deckdir.iterdir()):
            if f.name in claimed or not f.is_file():
                continue
            claimed.add(f.name)
            link = dst / f.name
            if link.is_symlink():
                if link.resolve() == f.resolve():
                    continue
                link.unlink()
            elif link.exists():
                link.unlink()
            link.symlink_to(f.resolve())
    for stale in dst.iterdir():
        if stale.name not in claimed:
            stale.unlink()
    return dst


def _gen_tree(gen, tree) -> Path | None:
    """The generated-source overlay tree: explicit --gen, else the source
    archive's gen/ sibling (<...>/gen/<OI> next to <...>/code/<OI>, found
    by resolving the --root symlink)."""
    if gen:
        return Path(gen)
    if tree is None:
        return None
    tree = Path(tree)
    return tree.resolve().parent.parent / "gen" / tree.name


def _con80_overlay(gen_tree, con80_dir: Path, out: str) -> Path:
    """`con80_dir` with the gen tree's CON80/ overlaid, or `con80_dir`
    unchanged when there is nothing to overlay."""
    if gen_tree is None or con80_dir is None:
        return con80_dir
    gen_con80 = Path(gen_tree) / CON80_DIR
    if not gen_con80.is_dir():
        return con80_dir
    mirror = Path(out) / CON80_MIRROR
    if con80_dir.resolve() == mirror.resolve():
        return con80_dir                    # already the merged deck
    _con80_mirror([gen_con80, con80_dir], mirror)
    print(f"[gen] CON80 overlay: {gen_con80} -> {mirror}")
    return mirror


def hal(sources, objdir: Path, gendir: Path, halsc: str, python: str,
        pass_rel32: Path, srcdirs, incl80,
        verbose: int = 0, displays=(), owned=None,
        gen_tree: Path | None = None,
        phase: int | None = None) -> tuple[int, list[str]]:
    """Compile the HAL sources with halsc in template-dependency order, sharing
    one TEMPLIB so each unit sees the templates of the units before it.  Units
    compile straight from the .hal mirror (copied into gen/pp per unit);
    includes are satisfied by the SDF include function.
    Each unit's PASS3 SDF member is collected into gen/SDFLIB (the
    simulation-data twin of TEMPLIB; dfg --sdflib reads it).
    `srcdirs` are the source search directories;
    `incl80` is the HAL-include directory.
    Returns (ok_count, failures)."""
    objdir.mkdir(parents=True, exist_ok=True)
    gendir.mkdir(parents=True, exist_ok=True)
    templib = (gendir / "TEMPLIB").resolve()
    sdflib = (gendir / "SDFLIB").resolve()

    # Worklist-completeness: the worklist units `D INCLUDE TEMPLATE` compools and
    # procedures that live in *other* overlays and so aren't in this link's
    # worklist, but whose templates must still be built.
    # The transitive objects are SEGREGATED into gen/tmpl_obj so the concard-driven link
    # doesn't use it.
    # The phase's display + AMT decks seed the closure too: their INCLUDE=
    # compools (displays) / derived DFB+OPGM+SPGM templates (AMT) must land
    # in gen/SDFLIB for dfg/halsc even when no worklist HAL unit references
    # them (e.g. CG0543's CGG_C03).
    incremental = owned is not None
    owned = set(owned or ())
    worklist = list(sources)
    worklist_set = set(worklist)        # this call's units, for pass/fail
    # ...but an objdir unit is anything objdir already holds or will hold: a
    # unit an earlier call placed there may reappear in this call's closure and
    # must not be re-routed to tmpl_obj
    objdir_units = worklist_set | owned
    with _timed("step", "tree_units"):
        tree = halorder.tree_units(srcdirs)
    disp_overlay = _disp_overlays(gen_tree, phase)
    disp_seeds = [d for p in displays
                  for d in _deck_template_seeds(p, disp_overlay)]
    with _timed("step", "template_closure"):
        extra, seed_progs = halorder.template_closure(worklist, tree, disp_seeds)
    tmplobj = (gendir / "tmpl_obj").resolve()
    tmplobj.mkdir(parents=True, exist_ok=True)
    skipped = [p for p in extra if p in owned]
    extra = [p for p in extra if p not in owned]
    # An incremental call keeps TEMPLIB, so a closure producer an earlier
    # wave already compiled still has both its object and its template: its
    # source has not changed and the library has only grown.  Reuse it
    # rather than re-running the whole closure per wave.
    reused: list = []
    if incremental:
        # A tmpl_obj object left by an EARLIER RUN predates this run's fresh
        # TEMPLIB/SDFLIB: reusing it without its published template+SDF
        # members leaves holes (halsc misses the template, mafgen degrades
        # the unit to NONHAL).  Reuse only when both members are present;
        # otherwise recompile (the compile cache makes that a fetch).
        def _real_sdf(p):
            m = sdflib / f"##{p.stem[:6]:<6}.sdf"
            # a 3,360-byte member is compile_stub's declaration-only seed
            # stub, not a real PASS3 SDF
            return m.exists() and m.stat().st_size > 3360
        reused = [p for p in extra
                  if (tmplobj / (p.stem + ".obj")).exists()
                  and (templib / f"@@{p.stem[:6]}").exists()
                  and _real_sdf(p)]
        extra = [p for p in extra if p not in set(reused)]
    sources = worklist + extra
    print(f"  template closure: +{len(extra)} producers "
          f"(real-compiled, objects segregated to tmpl_obj)"
          + (f"; {len(skipped)} already built by the main flow" if skipped
             else "")
          + (f"; {len(reused)} reused from an earlier wave" if reused
             else ""))

    src_root = Path(__file__).resolve().parent.parent       # .../src
    env = {**os.environ, "PYTHONUTF8": "1",
           "PYTHONPATH": os.pathsep.join([str(pass_rel32), str(src_root),
                                          os.environ.get("PYTHONPATH", "")])}

    # .hal mirror; units compile straight from the mirror (materialized
    # rewrites included).  The gen/pp dir keeps a copy per unit so
    # reports/tools that expect gen/pp/<stem>.hal keep working.
    haltree = (gendir / "haltree").resolve()
    with _timed("step", "hal_mirror"):
        _hal_mirror([*srcdirs, *_as_dirs(incl80)], haltree)
    ppdir = (gendir / "pp").resolve()
    ppdir.mkdir(parents=True, exist_ok=True)
    pp_of: dict = {}
    for srcpath in sources:
        mirror = haltree / srcpath.parent.name / (srcpath.name + ".hal")
        out = ppdir / (srcpath.stem + ".hal")
        if mirror.exists():
            out.unlink(missing_ok=True)
            out.write_bytes(mirror.read_bytes())
            pp_of[srcpath] = out
        else:
            pp_of[srcpath] = srcpath

    # Initialise (clear) the template-library PDS via the PASS toolchain script,
    # and the PASS3 SDF library collected alongside it.  A fresh TEMPLIB
    # invalidates every HAL object, so drop them too: a stale object left by
    # an earlier invocation would otherwise survive this run's compile failure
    # and be linked.  An incremental call skips both: its units ADD to the
    # libraries the main flow built, and `sources` excludes everything already
    # compiled, so the pre-clean can only reach this call's own objects.
    import shutil
    if not incremental:
        if templib.exists():
            shutil.rmtree(templib)
        if sdflib.exists():
            shutil.rmtree(sdflib)
    for p in sources:
        for d in (objdir, tmplobj):
            (d / (p.stem + ".obj")).unlink(missing_ok=True)
    if not incremental:
        _run([python, str(pass_rel32 / "prepareTEMPLIB"), "--clear"],
             verbose=verbose, cwd=str(gendir), env=env, label="prepareTEMPLIB")

    # Build the include dir: macros referenced by non-template D INCLUDE
    # name directives are resolved at compile time from INCLIB.
    # prepareINCLIB converts each INCL80 member to *EBCDIC* PDS records.
    #
    # An incremental call reuses it: INCLIB derives only from INCL80, which
    # cannot change mid-build, and rebuilding it per autocall wave is pure cost.
    inclib = (gendir / "INCLIB").resolve()
    incl_dirs = _as_dirs(incl80)
    incl_mirror = haltree / incl_dirs[0].name if incl_dirs else None
    if incl_mirror is not None and incl_mirror.is_dir() and not incremental:
        ienv = {**env, "PYTHONPATH": os.pathsep.join(
            [str(src_root / "XCOM-I"), env.get("PYTHONPATH", "")])}
        _run([python, str(pass_rel32 / "prepareINCLIB"),
              "--clear", f"--include={incl_mirror}"],
             verbose=verbose, cwd=str(gendir), env=ienv, label="prepareINCLIB")

    seedsdir = (gendir / "seeds").resolve()
    seedsdir.mkdir(parents=True, exist_ok=True)

    def compile_stub(progname, dn):
        """Compile a minimal empty-body PROGRAM so the compiler emits its
        (body-independent) program template and SDF — the SDF member lets
        dependents' `D INCLUDE TEMPLATE <program>` resolve via the SDF path
        instead of textual fallback."""
        stub = seedsdir / f"{dn}.hal"
        stub.write_text(f" {progname}: PROGRAM;\n CLOSE {progname};\n")
        _run([halsc, f"--parm={halorder.get_parms('_seed')}",
              f"--templib={templib}", f"--inclib-ebcdic={inclib}",
              f"--sdf={sdflib}",
              f"--workdir={gendir / ('_seed_' + dn + '.work')}",
              "-o", str(gendir / ('_seed_' + dn + '.obj')), str(stub)],
             verbose=verbose, env=env, label=f"halsc seed {dn}")
        return (sdflib / f"##{dn:<6}.sdf").exists() \
            or (templib / f"@@{dn}").exists()

    # Seed PROGRAM templates to break compool/program template cycles.
    # The compiler inhibits template generation on ANY error, so a unit that 
    # D INCLUDE TEMPLATEs with an include loop fail. 
    #   (e.g. CDM_UI_COMPOOL <-> ASM_AUX_DPS)
    # The program template is just the program's declaration line, so we
    # fake it here to break the loop:
    _t_srcindex = time.time()
    # `tree` is the same whole-source-tree index already built above.
    src_index = {dn: (full, kind) for dn, (_p, full, kind) in tree.items()}
    _tlog(kind="step", name="seed_src_index", t0=_t_srcindex,
          dt=time.time() - _t_srcindex) if _TIMING else None
    referenced: set = set()
    _t_deps = time.time()
    for p in sources:
        _, deps = halorder.parse_hal(pp_of.get(p, p))
        referenced |= set(deps)
    _tlog(kind="step", name="seed_parse_deps", t0=_t_deps,
          dt=time.time() - _t_deps) if _TIMING else None
    # PROGRAM units the closure pruned to seeds (body-independent), incl.
    # the display/AMT decks' control-segment programs (an AMT compool's
    # NAME(<opgm>) initializers need their program templates even when no
    # worklist unit references them).
    referenced |= seed_progs
    built = ({f[2:8].strip() for f in os.listdir(sdflib)
              if f.startswith("##")} if sdflib.exists() else set())
    # Stubs are independent one-line compiles into distinct members.
    seeds = [(src_index[dn][0], dn) for dn in sorted(referenced - built)
             if src_index.get(dn) and src_index[dn][1] == "PROGRAM"]
    nseed = sum(_pmap(lambda s: bool(compile_stub(*s)), seeds))
    print(f"  seeded {nseed} program templates (cycle-break)")

    # --inclib-ebcdic (not --inclib) everywhere below: gen/INCLIB is
    # prepareINCLIB's own output, i.e. EBCDIC PDS records by construction, so
    # halsc can skip its ASCII-detection pass over the whole library.
    # Each compile emits its own template and SDF member into PRIVATE
    # directories, which are then merged into the shared libraries.  Same
    # result as stowing straight into them, but it makes one compile's output
    # identifiable -- which is what the cache stores -- and leaves the shared
    # libraries with a single writer.
    outdir = (gendir / "unit_out").resolve()

    # The cross-phase compile cache.  Keyed on everything the compiler reads
    # (see compilecache); disabled with --no-cache.
    unit_name = {p: n for n, (p, _f, _k) in tree.items()}
    cache = None
    if _CACHE_ROOT:
        binaries = [Path(halsc)]
        hbin = Path(halsc).resolve().parent.parent / "halsfc"
        if hbin.is_dir():
            binaries += sorted(hbin.glob("HALSFC-*"))

        def _mirror_bytes(p: Path) -> bytes:
            m = haltree / p.parent.name / (p.name + ".hal")
            return (m if m.exists() else p).read_bytes()

        cache = compilecache.CompileCache(
            Path(_CACHE_ROOT), tree, _mirror_bytes,
            extra=compilecache.dir_content_fingerprint(inclib) + b"|"
            + compilecache.fingerprint(binaries))

    def _verify_hit(srcpath, obj, parm, key):
        """--cache-verify: force a compile and compare against cached file"""
        tout, sout = _private_out(srcpath.stem + ".verify")
        fresh = outdir / (srcpath.stem + ".verify.obj")
        r, _wd = _run_halsc(srcpath, fresh, parm, tout, sout)
        if r.returncode != 0 or not fresh.exists():
            print(f"  [cache-verify] {srcpath.name}: recompile FAILED but a "
                  f"cache entry was used ({key[:12]})", file=sys.stderr)
            return
        if compilecache.stamp_normalize(fresh.read_bytes()) != \
                compilecache.stamp_normalize(obj.read_bytes()):
            print(f"  [cache-verify] MISMATCH {srcpath.name} ({key[:12]})",
                  file=sys.stderr)
            _halt(f"cache-verify mismatch on {srcpath.name}")

    def _private_out(stem):
        t, s = outdir / stem / "tmpl", outdir / stem / "sdf"
        shutil.rmtree(outdir / stem, ignore_errors=True)
        t.mkdir(parents=True, exist_ok=True)
        s.mkdir(parents=True, exist_ok=True)
        return t, s

    def _publish(t, s):
        for src, dest in ((t, templib), (s, sdflib)):
            dest.mkdir(parents=True, exist_ok=True)
            for m in src.iterdir():
                if m.is_file():
                    shutil.copyfile(m, dest / m.name)

    def _run_halsc(srcpath, obj, parm, tout, sout):
        workdir = gendir / (srcpath.stem + ".work")
        cmd = [halsc, f"--parm={parm}",
               f"--templib={templib}", f"--templib-out={tout}",
               f"--inclib-ebcdic={inclib}",
               f"--sdf={sout}", f"--sdfi={sdflib}", f"--workdir={workdir}",
               "-o", str(obj), str(pp_of.get(srcpath, srcpath))]
        r = _run(cmd, verbose=verbose, env=env, label=f"halsc {srcpath.stem}")
        return r, workdir

    def compile_one(srcpath, obj=None):
        dest = objdir if srcpath in objdir_units else tmplobj
        obj = obj or dest / (srcpath.stem + ".obj")
        parm = halorder.get_parms(srcpath.stem)
        ckey = (cache.key(srcpath, unit_name.get(srcpath), parm)
                if cache is not None else None)
        if ckey:
            # A unit that trips an undowngraded ZO3 is only ever produced with
            # ,X1 (below), so its result is cached under that parm: look there
            # too, rather than repeating the compile that has to fail first.
            for k in (ckey, cache.key(srcpath, unit_name.get(srcpath),
                                      parm + ",X1")):
                if k and cache.fetch(k, obj, templib, sdflib):
                    if _CACHE_VERIFY:
                        _verify_hit(srcpath, obj, parm, k)
                    return True, ""
            cache.missed()
        tout, sout = _private_out(srcpath.stem)
        r, workdir = _run_halsc(srcpath, obj, parm, tout, sout)
        out = r.stdout + r.stderr
        if r.returncode == 0 and obj.exists():
            _publish(tout, sout)
            if ckey:
                cache.store(ckey, obj, tout, sout)
            return True, ""
        # ZO3 ("loop invariant pulled from IF-THEN", OPT pass): units that
        # trip it carry `D DOWNGRADE ZO3` in the source -- the optimizer
        # ran for flight, so suppressing optimization with ,X1 would
        # diverge from flight codegen.  Retry with ,X1 only when a ZO3
        # was NOT downgraded (a real severity-1 the deck didn't expect);
        # a downgraded ZO3 alongside a failure means the failure lies
        # elsewhere -- report it, don't mask it.
        optrpt = workdir / "opt.rpt"
        opttxt = optrpt.read_text(errors="replace") if optrpt.exists() else ""
        if "ZO3" in opttxt and "DOWNGRADED" not in opttxt:
            tout, sout = _private_out(srcpath.stem)
            r, workdir = _run_halsc(srcpath, obj, parm + ",X1", tout, sout)
            out = r.stdout + r.stderr
            if r.returncode == 0 and obj.exists():
                _publish(tout, sout)
                x1key = (cache.key(srcpath, unit_name.get(srcpath),
                                   parm + ",X1") if cache is not None else None)
                if x1key:
                    cache.store(x1key, obj, tout, sout)
                return True, ""
        if "BI002" in out:
            reason = "BI002 (historically an XCOM-I ADDR/freeBase port bug)"
        elif "NOT IN INCLUDE LIBRARY" in out:
            import re as _re
            miss = sorted(set(_re.findall(r"([A-Z0-9#]+) +NOT IN INCLUDE LIBRARY", out)))
            reason = "missing template/include: " + ",".join(miss[:6])
        elif "PM2" in out and "DUPLICATE DEFINITION OF STRUCTURE TEMPLATE" in out:
            import re as _re
            m = _re.search(r"DUPLICATE DEFINITION OF STRUCTURE TEMPLATE (\w+)", out)
            dup = m.group(1) if m else "?"
            reason = f"PM2 duplicate structure template {dup} (canonical build skips PM2)"
        elif "ABANDONED" in out:
            reason = "compilation abandoned (deps/cycle)"
        else:
            reason = (out.strip().splitlines()[-1:] or [""])[0][:60]
        return False, reason

    # Multi-pass: TEMPLIB grows each pass, so units blocked on a not-yet-built
    # producer (incl. cycle members that bootstrap via D DOWNGRADE) get retried
    # until a pass makes no new progress.
    #
    with _timed("step", "topo_levels"):
        levels = halorder.topo_levels(sources)
    built: set = set()
    reasons: dict[str, str] = {}
    while True:
        progressed = False
        still: list = []
        for level in levels:
            todo = [p for p in level if p not in built]
            if not todo:
                continue
            for srcpath, (good, why) in zip(todo, _pmap(compile_one, todo)):
                if good:
                    built.add(srcpath); progressed = True
                    reasons.pop(srcpath.name, None)
                else:
                    reasons[srcpath.name] = why; still.append(srcpath)
        if not still or not progressed:
            break

    if cache is not None and (cache.hits or cache.misses):
        print(f"  compile cache: {cache.hits} hit, {cache.misses} miss, "
              f"{cache.stores} stored"
              + (" [--cache-verify: every hit recompiled and checked]"
                 if _CACHE_VERIFY else ""))

    work_fail = sorted(p.name for p in worklist_set if p not in built)
    work_fail_set = set(work_fail)
    extra_fail = sorted(n for n in reasons if n not in work_fail_set)
    for name in work_fail:
        print(f"  [halsc FAIL] {name}: {reasons.get(name, '?')}", file=sys.stderr)
    if extra_fail:
        print(f"  ({len(extra_fail)} closure-only producers also failed: "
              f"{' '.join(extra_fail[:12])}{' ...' if len(extra_fail) > 12 else ''})",
              file=sys.stderr)
    ok = len(worklist_set) - len(work_fail)
    return ok, work_fail


def display(sources, objdir: Path, gendir: Path, halsc: str, python: str,
            pass_rel32: Path, srcdirs, incl80,
            deck_root: Path | None, verbose: int = 0,
            tag: str = "dfg", gen_tree: Path | None = None,
            phase: int | None = None) -> tuple[int, list[str]]:
    """dfg (deck -> HAL/S compool source; types from gen/SDFLIB), then halsc
    into objdir/<name>.obj (csect #P<name>).

    Also builds the AMT decks (CDAPnn -> CDA_Pnn_AMT moding-table compool,
    csect #PCDAPnn): dfg auto-detects the deck form, and the same
    generate -> preprocess -> compile flow applies (`tag` labels the echo).

    Runs AFTER hal(): the displays' INCLUDEd compools must already have SDF
    members in gen/SDFLIB.  Each display's own PASS3 SDF member is
    collected there too (--sdf), which is where SDF consumers such as
    src/mafgen's #PCD0nnn data-csect walk read them from.  The compile
    still gets a SCRATCH copy of TEMPLIB — halsc's TEMPLATE parm APPENDS
    each compiled unit's own template, and display templates (@@CD0001
    ...) must never reach the shared library; it also remains the textual
    fallback for any include without an SDF.
    Returns (ok_count, failures)."""
    import shutil
    objdir.mkdir(parents=True, exist_ok=True)
    gendir.mkdir(parents=True, exist_ok=True)
    templib = (gendir / "TEMPLIB").resolve()
    inclib = (gendir / "INCLIB").resolve()
    dispdir = (gendir / "displays").resolve()
    dispdir.mkdir(parents=True, exist_ok=True)
    if not templib.is_dir():
        print(f"  [display] no {templib} — run --hal first", file=sys.stderr)
        return 0, [p.stem for p in sources]
    haltree = (gendir / "haltree").resolve()
    with _timed("step", "hal_mirror"):
        _hal_mirror([*srcdirs, *_as_dirs(incl80)], haltree)

    src_root = Path(__file__).resolve().parent.parent       # .../src
    env = {**os.environ, "PYTHONUTF8": "1",
           "PYTHONPATH": os.pathsep.join([str(pass_rel32), str(src_root),
                                          os.environ.get("PYTHONPATH", "")])}

    overlay = _disp_overlays(gen_tree, phase)

    def build_one(srcpath) -> str | None:
        """Generate + compile one deck; returns its name on failure."""
        name = srcpath.stem
        halout = dispdir / f"{name}.hal"
        pre = _overlay_hal(overlay, name)
        if pre is not None:
            shutil.copyfile(pre, halout)
            print(f"  [{tag}] {name}: using the recovered compool "
                  f"{pre.parent.parent.name}/{pre.parent.name}/{pre.name} "
                  f"(deck not generated)")
        else:
            cmd = [*_tool("dfg", python), name,
                   "--sdflib", str((gendir / "SDFLIB").resolve()),
                   "-o", str(halout)]
            if deck_root is not None:
                cmd += ["--deck-root", str(deck_root)]
            r = _run(cmd, verbose=verbose, env=env, label=f"{tag} {name}")
            if r.returncode != 0 or not halout.exists():
                tail = ((r.stderr or "").strip().splitlines()[-1:]
                        or ["?"])[0]
                print(f"  [dfg FAIL] {name}: {tail[:70]}", file=sys.stderr)
                return name
        # dfg output compiles as-is (includes resolved via SDF).
        scratch = (gendir / f"disp_TEMPLIB.{name}").resolve()
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.copytree(templib, scratch)
        obj = objdir / f"{name}.obj"
        r = _run([halsc, f"--parm={halorder.get_parms(name)}",
                  f"--templib={scratch}", f"--inclib-ebcdic={inclib}",
                  f"--sdf={(gendir / 'SDFLIB').resolve()}",
                  f"--workdir={dispdir / (name + '.work')}",
                  "-o", str(obj), str(halout)],
                 verbose=verbose, env=env, label=f"halsc {name}")
        shutil.rmtree(scratch, ignore_errors=True)
        if r.returncode == 0 and obj.exists():
            shutil.rmtree(dispdir / (name + ".work"), ignore_errors=True)
            return None
        out = (r.stdout or "") + (r.stderr or "")
        tail = (out.strip().splitlines()[-1:] or ["?"])[0]
        print(f"  [halsc FAIL] {name}: {tail[:70]}", file=sys.stderr)
        return name

    fails = [f for f in _pmap(build_one, sources) if f is not None]
    for name in fails:
        _halt(f"display {name} failed")
    return len(sources) - len(fails), fails


def runtime_csect_index(linklibs) -> dict[str, Path]:
    """Map every csect a runtime-library object provides -> that object.

    Built from each object's `.asmg.json` sidecar (its non-DSECT SD sections;
    e.g. SQRT.obj -> {SQRT, #LSQRT}).  A CON80 INSERT names a *csect*, which is
    often not the object's filename (INSERT #0ITOE -> ITOE.obj, INSERT #LSQRT ->
    SQRT.obj), so we index by csect.  First object wins on a duplicate name."""
    idx: dict[str, Path] = {}
    for d in linklibs:
        d = Path(d)
        if not d.is_dir():
            continue
        for ag in sorted(d.glob("*.asmg.json")):
            obj = ag.parent / (ag.name[:-len(".asmg.json")] + ".obj")
            if not obj.exists():
                continue
            try:
                meta = json.loads(ag.read_text())
            except (OSError, ValueError):
                continue
            for s in meta.get("sections", []):
                if s.get("dsect"):
                    continue
                name = s.get("name")
                if name:
                    idx.setdefault(name, obj)
    return idx


def inserted_runtime_objects(deck_dir: Path, root: str, linklibs,
                             already: set[Path]) -> tuple[list[Path], list[str]]:
    """Runtime-library objects that supply CON80-INSERTed csects.

    The deck's linkage editor runs with NCAL (no automatic library call), so a
    resident-library csect placed by an INSERT (SQRT, ACOS, #0ITOE, ...) is NOT
    pulled by reference -- it must be loaded explicitly, exactly as the deck
    directs.  Returns (objects-to-add, still-unresolved-INSERT-names-in-libs).
    Objects already in `already` (by path) are skipped."""
    deck = concard.ConcardDeck(str(deck_dir))
    inserts = [op.operand for op in concard.layout_program(deck, root)
               if op.verb == "INSERT" and op.operand]
    idx = runtime_csect_index(linklibs)
    objs: list[Path] = []
    seen = set(already)
    for name in inserts:
        obj = idx.get(name)
        if obj is not None and obj not in seen:
            seen.add(obj)
            objs.append(obj)
    return sorted(objs), []


def worklist_objects(objdir: Path, by_type: dict, patches: dict) -> list[Path]:
    """The object files this worklist produces: <stem>.obj for each
    ASM/HAL/DISPLAY/AMT source and PCHnnTXT.obj for each patch, filtered to
    those that exist.
    """
    want = [objdir / (p.stem + ".obj") for p in by_type["ASM"]]
    want += [objdir / (p.stem + ".obj") for p in by_type["HAL"]]
    want += [objdir / (p.stem + ".obj") for p in by_type["DISPLAY"]]
    want += [objdir / (p.stem + ".obj") for p in by_type.get("AMT", [])]
    want += [objdir / (member + ".obj") for member in patches.values()]
    have = [o for o in want if o.exists()]
    if len(have) != len(want):
        missing = sorted({o.stem for o in want} - {o.stem for o in have})
        print(f"  [link] {len(missing)} worklist member(s) produced no object "
              f"-- their csects will be ABSENT from the image: "
              + " ".join(missing), file=sys.stderr)
    return have


def link(objdir: Path, fcm: Path, deck_dir: Path, concard_root: str,
         linklibs, python: str, json_symbols: Path | None = None,
         allow_undefined: bool = True, nocall: bool = False, verbose: int = 0,
         objs: list[Path] | None = None,
         map_libs: list[str] | None = None,
         lib: Path | None = None,
         warn_unresolved: bool = False,
         autocall_json: Path | None = None,
         link_order: Path | None = None) -> bool:
    """Link objects into `fcm`, placing csects from the CON80 deck (lnk101
    --concard).  `objs` is the explicit object list to link; when None, every
    *.obj in `objdir` is linked."""
    objs = sorted(objdir.glob("*.obj")) if objs is None else sorted(objs)
    if not objs:
        print(f"  [lnk101] no objects to link in {objdir} "
              f"(run --assemble / --hal first)", file=sys.stderr)
        return False
    cmd = [*_tool("lnk101", python), *(str(o) for o in objs)]
    for libdir in linklibs:
        cmd += ["-L", str(libdir)]
    cmd += ["--concard", str(deck_dir), "--concard-root", concard_root,
            "-o", str(fcm),
            "--lib", str(lib if lib is not None else fcm.with_suffix(".lib"))]
    for spec in (map_libs or []):
        cmd += ["--map-lib", spec]
    if warn_unresolved:
        cmd.append("--Wunresolved-phases")
    if json_symbols is not None:
        cmd += ["--json-symbols", str(json_symbols)]
    if allow_undefined:
        cmd.append("--allow-undefined")
    if nocall:
        cmd.append("--nocall")
    if autocall_json is not None and Path(autocall_json).exists():
        cmd += ["--autocall", str(autocall_json)]
    if link_order is not None:
        cmd += ["--link-order", str(link_order)]
    env = {**os.environ, "PYTHONUTF8": "1"}
    r = _run(cmd, verbose=verbose, env=env, label=f"lnk101 -> {fcm.name}")
    ok = r.returncode == 0 and fcm.exists()
    if ok and not verbose:
        for line in (r.stderr or "").splitlines():
            if " layout: " in line:
                print(f"  [{fcm.stem}] {line.strip()}", flush=True)
    if not ok:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-15:] or [""]
        for line in tail:
            print(f"  [lnk101 FAIL] {line}", file=sys.stderr)
    return ok


app = typer.Typer(
    help=__doc__,
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.command()
def main(
    target: Annotated[Optional[str], typer.Argument(metavar="TARGET",
        help="top CON80 card to build (e.g. SSW); or use --phase N")] = None,
    *,
    phase: Annotated[Optional[int], typer.Option("--phase", metavar="N",
        help="build MMU phase N as defined by the master deck's PHASE "
             "directives (prologue + phase segment; outputs named PHASE0N "
             "under <out>/PHASE0N/, load module at <out>/PHASE0N.lib; MAP "
             "dependencies auto-resolved from <out>/PHASE0P.lib)")] = None,
    master: Annotated[str, typer.Option("--master",
        help="master deck member whose PHASE directives define the phases "
             "(used with --phase)")] = "OFTMP",
    out: Annotated[str, typer.Option("--out",
        help="output root (obj/, gen/, <target>.fcm beneath)")],
    lib_dir: Annotated[Optional[str], typer.Option("--lib-dir", metavar="DIR",
        help="where ALREADY-BUILT phase load modules are READ from: "
             "[default: --out]")] = None,
    root: Annotated[Optional[str], typer.Option("--root",
        help="conventionally laid out source tree (CON80/, SSSRC/, APPLSRC/, "
             "MLIB80/, INCL80/); shorthand for --con80/--src/--mlib/--incl")] = None,
    con80: Annotated[Optional[str], typer.Option("--con80",
        help="CON80 deck directory [default: <root>/CON80]")] = None,
    src: Annotated[Optional[list[str]], typer.Option("--src", metavar="DIR",
        help="source search directory (repeatable, searched in order) "
             "[default: <root>/SSSRC, <root>/APPLSRC]")] = None,
    gen: Annotated[Optional[str], typer.Option("--gen", metavar="DIR",
        help="generated-source overlay tree (SSSRC/, APPLSRC/ beneath); "
             "members here OVERRIDE same-named members under --src "
             "[default: <resolved root>/../../gen/<basename of --root>]")] = None,
    mlib: Annotated[Optional[str], typer.Option("--mlib",
        help="asm101 macro/COPY library [default: <root>/MLIB80]")] = None,
    incl: Annotated[Optional[str], typer.Option("--incl",
        help="HAL inclusion library (INCL80) [default: <root>/INCL80]")] = None,
    deck_root: Annotated[Optional[str], typer.Option("--deck-root",
        help="root the dfg display generator resolves deck sources under "
             "[default: --root]")] = None,
    emit_cmake: Annotated[Optional[str], typer.Option("--emit-cmake",
        metavar="FILE",
        help="write a standalone CMake recipe that rebuilds this worklist "
             "without reading CON80 or running con80build")] = None,
    python: Annotated[str, typer.Option("--python",
        help="python for asm101/lnk101 modules")] = sys.executable,
    tolerable: Annotated[int, typer.Option("--tolerable")] = 4,
    plan: Annotated[bool, typer.Option("--plan",
        help="resolve+classify only; no build")] = False,
    run_assemble: Annotated[bool, typer.Option("--assemble",
        help="assemble the ASM sources")] = False,
    run_hal: Annotated[bool, typer.Option("--hal",
        help="compile the HAL sources (template order)")] = False,
    run_display: Annotated[bool, typer.Option("--display",
        help="build the display decks (dfg generate -> preprocess -> "
             "halsc against a scratch TEMPLIB copy); run after --hal")] = False,
    run_link: Annotated[bool, typer.Option("--link",
        help="link the built objects into <out>/<target>.fcm "
             "with CON80 (lnk101 --concard)")] = False,
    concard_root: Annotated[Optional[str], typer.Option("--concard-root",
        help="top CON80 card laid out by the linker (default: TARGET, so "
             "layout and MAP cards are scoped to the phase being built)")] = None,
    map_lib: Annotated[Optional[list[str]], typer.Option("--map-lib",
        metavar="N=LIB",
        help="earlier-phase load module for the deck's MAP cards, as "
             "N=path/to/PHASE0N.lib (repeatable; forwarded to lnk101)")] = None,
    link_order: Annotated[Optional[str], typer.Option("--link-order",
        metavar="FILE",
        help="linkorder.json pins file with the link orderings "
             "(Z1 ZCON pool, autocall waves/streams); forwarded to lnk101 "
             "[default: <gen tree>/linkorder.json if it exists]")] = None,
    resolve_phases: Annotated[bool, typer.Option("--resolve-phases",
        help="cross-resolve every <out>/PHASE*.lib against each other, "
             "patching residual RLD sites in the .lib and .fcm files (run "
             "after all phases are linked); no TARGET/--phase needed")] = False,
    build_all: Annotated[bool, typer.Option("--build-all",
        help="build every phase of the master deck in OFTMP order "
             "(--phase 1..N), then cross-resolve the load modules; "
             "all of --assemble --hal --display --link are "
             "implied; no TARGET/--phase needed")] = False,
    keep_going: Annotated[bool, typer.Option("--keep-going",
        help="with --build-all: continue past a failed phase (resolution "
             "still runs over the load modules that built)")] = False,
    halt_on_error: Annotated[bool, typer.Option("--halt-on-error", "-e",
        help="stop at the first failing member")] = False,
    warn_unresolved_phases: Annotated[bool, typer.Option(
        "--Wunresolved-phases",
        help="list per-symbol unresolved warnings (forwarded to "
             "lnk101/phaseresolve; default: summary counts only)")] = False,
    linklib: Annotated[Optional[list[str]], typer.Option("--linklib", metavar="DIR",
        help="runtime object-library dir for the link (-L; repeatable; "
             "default: build/lib/runtime/{RUN,ZCON})")] = None,
    autocall: Annotated[bool, typer.Option("--autocall/--no-autocall",
        help="model the LE automatic library call for decks without a "
             "NOCALLER card: iteratively compile+pull modules resolving "
             "leftover external refs (LIBRARY *(...) stubs excluded)")] = True,
    prune_objs: Annotated[bool, typer.Option("--prune-objs",
        help="delete .obj files in this phase's objdir that neither the "
             "worklist nor autocall produced (stale leftovers pollute the "
             "autocall stack sizer's call graph); default: warn only")] = False,
    no_undefined: Annotated[bool, typer.Option("--no-undefined",
        help="fail the link on unresolved symbols (default: allow them, "
             "since HAL/runtime modules may be incomplete)")] = False,
    halsc: Annotated[str, typer.Option("--halsc",
        help="halsc wrapper path")] = "build/bin/halsc",
    pass_rel32_dir: Annotated[str, typer.Option("--pass-rel32",
        help="PASS.REL32V0 dir (prepareTEMPLIB/preprocess scripts)")] = "code/PASS.REL32V0",
    runlib: Annotated[Optional[list[str]], typer.Option("--runlib", metavar="DIR",
        help="runtime-library dir whose members (SQRT, EXP, ...) are "
             "supplied at link, not built here (repeatable; default: "
             "<pass-rel32>/RUNASM and <pass-rel32>/ZCONASM)")] = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", metavar="N",
        help="run up to N toolchain processes at once (assembly, each HAL "
             "dependency level, the display decks). 0 = one per CPU; "
             "1 = the strictly serial build")] = 0,
    phase_jobs: Annotated[int, typer.Option("--phase-jobs", "-J", metavar="P",
        help="with --build-all: compile up to P phases at once")] = 0,
    cache: Annotated[Optional[str], typer.Option("--cache-dir", metavar="DIR",
        help="cross-phase compile cache directory (default: "
             "<out>/.compilecache")] = None,
    shared_cache: Annotated[bool, typer.Option("--shared-cache",
        help="put the compile cache in a fixed per-user location under "
             "$TMPDIR instead of under --out")] = False,
    use_cache: Annotated[bool, typer.Option("--cache/--no-cache",
        help="cache halsfc output")] = True,
    cache_verify: Annotated[bool, typer.Option("--cache-verify",
        help="recompile on every cache hit and check the cached object"
        )] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True,
        help="echo each command and output as it runs")] = 0,
) -> None:
    pass_rel32 = Path(pass_rel32_dir)
    runlibs = ([Path(d) for d in runlib] if runlib
               else [pass_rel32 / "RUNASM", pass_rel32 / "ZCONASM"])

    # The tool wrappers (asm101/lnk101/dfg) live beside the halsc wrapper.
    global _BINDIR, _HALT_ON_ERROR, _JOBS, _PHASE_JOBS
    global _CACHE_ROOT, _CACHE_VERIFY
    _BINDIR = Path(os.path.abspath(halsc)).parent
    _HALT_ON_ERROR = halt_on_error
    _JOBS = jobs if jobs > 0 else (os.cpu_count() or 1)
    _PHASE_JOBS = max(1, phase_jobs if phase_jobs > 0 else _JOBS // 4)
    if cache and shared_cache:
        raise typer.BadParameter("--cache-dir and --shared-cache both name "
                                 "where the cache goes; give one")
    if use_cache:
        shared = os.path.join(
            os.environ.get("HALSC_CACHE")
            or os.path.join(os.environ.get("TMPDIR", "/tmp"),
                            f"halsc-cache-{os.getuid()}"), "units")
        _CACHE_ROOT = os.path.abspath(
            cache or (shared if shared_cache else None)
            or os.environ.get("CON80BUILD_CACHE")
            or os.path.join(out, ".compilecache"))
    _CACHE_VERIFY = cache_verify
    if halt_on_error and keep_going:
        raise typer.BadParameter("--halt-on-error and --keep-going conflict")

    if resolve_phases:
        libs = sorted(Path(out).glob("PHASE*.lib"))
        if not libs:
            raise typer.BadParameter(f"no PHASE*.lib in {out}")
        deckdir = Path(con80) if con80 \
            else (Path(root) / "CON80" if root else None)
        if deckdir is None:
            raise typer.BadParameter(
                "--resolve-phases needs --root or --con80")
        _require_dirs([("--con80", deckdir)])
        cmd = [python, "-m", "lnk101.phaseresolve",
               "--con80", str(deckdir), *map(str, libs)]
        if warn_unresolved_phases:
            cmd.append("--Wunresolved-phases")
        r = _run(cmd, verbose=verbose,
                 env={**os.environ, "PYTHONUTF8": "1"},
                 label="phaseresolve")
        print((r.stdout or "").rstrip())
        if r.returncode != 0:
            print((r.stderr or "").rstrip(), file=sys.stderr)
        raise typer.Exit(r.returncode)

    # --build-all: run the whole MMU software build -- every phase segment
    # of the master deck in OFTMP order (each as a `--phase N` sub-build,
    # inheriting the tree/step options), then one cross-phase resolution at
    # the end.  Deck order is dependency-consistent (MAP cards reference
    # earlier phase numbers), so each phase finds its dep libs in <out>.
    if build_all:
        if target is not None or phase is not None:
            raise typer.BadParameter("--build-all takes no TARGET/--phase")
        deckdir = Path(con80) if con80 \
            else (Path(root) / "CON80" if root else None)
        if deckdir is None:
            raise typer.BadParameter("--build-all needs --root or --con80")

        # Validate every directory the selected steps will need across ALL
        # phases before phase 1 starts (the per-phase sub-builds re-check,
        # but by then earlier phases have already run).
        do_all = not (run_assemble or run_hal or run_display or run_link)
        tree = Path(root) if root else None
        need = [("--con80", deckdir)]
        need += ([("--src", Path(d)) for d in src] if src
                 else [("--src", tree / d) for d in SOURCE_DIRS] if tree
                 else [])
        if gen is not None:
            need.append(("--gen", Path(gen)))
        if run_assemble or do_all:
            need.append(("--mlib", Path(mlib) if mlib
                         else tree / "MLIB80" if tree else None))
        if run_hal or run_display or do_all:
            need.append(("--incl", Path(incl) if incl
                         else tree / "INCL80" if tree else None))
            need.append(("--pass-rel32", pass_rel32))
        if run_display or do_all:
            need.append(("--deck-root",
                         Path(deck_root) if deck_root else tree))
        need += [("--runlib", d) for d in runlibs]
        if run_link or do_all:
            need += [("--linklib", Path(d)) for d in
                     (linklib or _DEFAULT_LINKLIBS)]
        _require_dirs(need)

        deckdir = _con80_overlay(_gen_tree(gen, tree), deckdir, out)

        ids = [nums[0] for nums, _ in concard.phase_segments(
            concard.ConcardDeck(deckdir), master) if nums]

        fwd = []
        for flag, val in (("--root", root), ("--con80", str(deckdir)),
                          ("--gen", gen),
                          ("--mlib", mlib), ("--incl", incl),
                          ("--deck-root", deck_root), ("--halsc", halsc),
                          ("--pass-rel32", pass_rel32_dir),
                          ("--master", master)):
            if val is not None:
                fwd += [flag, str(val)]
        for flag, vals in (("--src", src), ("--runlib", runlib),
                           ("--linklib", linklib)):
            for v in (vals or []):
                fwd += [flag, str(v)]
        fwd += ["--tolerable", str(tolerable), "--python", python]
        if _CACHE_ROOT:
            fwd += ["--cache-dir", _CACHE_ROOT]
        else:
            fwd.append("--no-cache")
        if _CACHE_VERIFY:
            fwd.append("--cache-verify")
        if no_undefined:
            fwd.append("--no-undefined")
        if warn_unresolved_phases:
            fwd.append("--Wunresolved-phases")
        if halt_on_error:
            fwd.append("--halt-on-error")
        if prune_objs:
            fwd.append("--prune-objs")
        fwd += ["-v"] * verbose
        steps = [f for f, on in (("--assemble", run_assemble),
                                 ("--hal", run_hal),
                                 ("--display", run_display),
                                 ("--link", run_link)) if on] \
            or ["--assemble", "--hal", "--display", "--link"]

        def run_phase(pid, phase_steps, jobs, tag=""):
            r = subprocess.run(
                [python, "-m", "con80.con80build", "--phase", str(pid),
                 "--out", out, *phase_steps, *fwd, "--jobs", str(jobs)],
                capture_output=_PHASE_JOBS > 1, text=True)
            if r.stdout:
                sys.stdout.write(f"----- phase {pid}{tag} -----\n" + r.stdout)
                sys.stdout.flush()
            if r.stderr:
                sys.stderr.write(r.stderr)
            return r.returncode

        failed = []
        if _PHASE_JOBS <= 1 or len(ids) == 1 or "--link" not in steps:
            work = [(pid, steps) for pid in ids]
            if _PHASE_JOBS > 1:
                rcs = _pmap(lambda w: run_phase(w[0], w[1], _JOBS), work,
                            jobs=_PHASE_JOBS)
            else:
                rcs = []
                for i, pid in enumerate(ids, 1):
                    print(f"===== phase {pid} ({i}/{len(ids)}) =====",
                          flush=True)
                    rcs.append(run_phase(pid, steps, _JOBS))
            for pid, rc in zip(ids, rcs):
                if rc != 0:
                    failed.append(pid)
                    print(f"===== phase {pid} FAILED =====", file=sys.stderr,
                          flush=True)
                    if not keep_going:
                        raise typer.Exit(1)
        else:
            # Phase-parallel: split each phase at the boundary the serial
            # build already has, between its display step and its link.  What
            # each phase does and the order it does it in are unchanged; only
            # the boundary between phases moves.  The boundary must respect:
            #   compile(N) needs the sym.json of N's MAP phases, so it needs
            #             those phases linked;
            #   link(N)   needs every earlier phase's load module, because a
            #             phase segment re-opens overlay regions earlier
            #             segments defined (see the link step's comment).
            # So the links stay a chain in deck order and the compiles -- the
            # bulk of the build -- fan out behind it.  MAP deps always name
            # earlier phases, so by the time the chain reaches N its compile
            # is dispatchable.
            deck0 = concard.ConcardDeck(str(deckdir))
            mapdeps = {n: sorted({int(op.operand.split(',')[0])
                                  for op in concard.layout_program(
                                      deck0, f"{master}@{n}")
                                  if op.verb == "MAP" and op.operand
                                  and op.operand.split(',')[0].strip().isdigit()})
                       for n in ids}
            csteps = [s for s in steps if s != "--link"]
            per = _JOBS
            print(f"===== {len(ids)} phases, {_PHASE_JOBS} at a time, "
                  f"up to {per} job(s) each =====", flush=True)
            from concurrent.futures import ThreadPoolExecutor
            futs: dict = {}
            linked: set = set()
            with ThreadPoolExecutor(max_workers=_PHASE_JOBS) as ex:
                def dispatch():
                    for n in ids:
                        if n not in futs and set(mapdeps[n]) <= linked:
                            futs[n] = ex.submit(run_phase, n, csteps, per,
                                                " compile")
                for i, pid in enumerate(ids, 1):
                    dispatch()
                    print(f"===== phase {pid} ({i}/{len(ids)}) =====",
                          flush=True)
                    rc = futs[pid].result()
                    if rc == 0:
                        rc = run_phase(pid, ["--link"], per, " link")
                    if rc != 0:
                        failed.append(pid)
                        print(f"===== phase {pid} FAILED =====",
                              file=sys.stderr, flush=True)
                        if not keep_going:
                            raise typer.Exit(1)
                    linked.add(pid)
        print(f"===== cross-phase resolution =====", flush=True)
        r = subprocess.run(
            [python, "-m", "con80.con80build", "--resolve-phases",
             "--out", out, "--con80", str(deckdir),
             *(["--Wunresolved-phases"] if warn_unresolved_phases else [])])
        if failed:
            print(f"--build-all: {len(failed)} phase(s) failed: "
                  f"{' '.join(f'{p}' for p in failed)}", file=sys.stderr)
        raise typer.Exit(1 if (failed or r.returncode) else 0)

    if phase is not None and target is not None:
        raise typer.BadParameter("give TARGET or --phase, not both")
    if phase is not None:
        target = f"{master}@{phase}"
        name = f"PHASE{phase:02d}"
    elif target is not None:
        name = target
    else:
        raise typer.BadParameter("give TARGET or --phase")

    # Layout root defaults to the build target so the linker's CON80 replay
    # (placement, MAP cards, NOCALLER) is scoped to the phase being built.
    concard_root = concard_root or target

    # Tree locations: everything defaults from the conventional --root layout
    # but any piece can be pointed elsewhere explicitly.
    tree = Path(root) if root else None
    con80_dir = Path(con80) if con80 else (tree / "CON80" if tree else None)
    srcdirs = ([Path(d) for d in src] if src
               else [tree / d for d in SOURCE_DIRS] if tree else None)
    # Generated-source overlay: tool/preprocessor-generated members live
    # in a separate gen tree and shadow the delivered source, which is
    # NEVER modified.  The default overlay is the source archive's gen/
    # sibling (<...>/gen/SRC next to <...>/code/<SRC)
    gen_tree = _gen_tree(gen, tree)
    if srcdirs is not None and gen_tree is not None and gen_tree.is_dir():
        gendirs = [gen_tree / d for d in SOURCE_DIRS
                   if (gen_tree / d).is_dir()]
        if gendirs:
            print(f"[gen] source overlay: "
                  f"{', '.join(str(d) for d in gendirs)}")
            srcdirs = gendirs + srcdirs
        # Per-phase source overlay: <gen>/phase<NN>/{SSSRC,APPLSRC}/, ahead
        # of the whole-build overlay.  A member is normally the same text in
        # every phase that links it, and sharing one object across phases is
        # what the compile cache is for -- but a member whose SMPP selection
        # region picks which OPS's parameter-data-table template to compile
        # against is genuinely two different compiles.  Compiled once, the
        # object keeps one OPS's table reference in every phase, and the other
        # OPS's phase then autocalls that whole table into its own image.
        #
        # The compile cache is keyed on source content, not path, so the two
        # variants get different keys and neither shadows the other.
        if phase is not None:
            phdirs = [gen_tree / f"phase{phase:02d}" / d for d in SOURCE_DIRS
                      if (gen_tree / f"phase{phase:02d}" / d).is_dir()]
            if phdirs:
                print(f"[gen] phase-{phase:02d} source overlay: "
                      f"{', '.join(str(d) for d in phdirs)}")
                srcdirs = phdirs + srcdirs
    # INCL80 overlays the same way, and has to: three of the members SMPP
    # regenerates are HAL includes (FLEXDATA, FLEXTBL), so without this they
    # could only be localized into the delivered tree.
    gen_incl = (gen_tree / INCL_DIR) if gen_tree is not None else None
    # Link-order pins: explicit --link-order, else the gen tree's
    # linkorder.json (the orderings ride the gen/ overlay, not the
    # source tree).
    link_order_path = (Path(link_order) if link_order
                       else gen_tree / "linkorder.json"
                       if gen_tree is not None
                       and (gen_tree / "linkorder.json").is_file()
                       else None)
    if link_order_path is not None:
        if not link_order_path.is_file():
            raise typer.BadParameter(
                f"--link-order file not found: {link_order_path}")
        print(f"[link-order] pins: {link_order_path}")
    mlib_dir = Path(mlib) if mlib else (tree / "MLIB80" if tree else None)
    incl_dir = Path(incl) if incl else (tree / INCL_DIR if tree else None)
    incl_dirs = [d for d in (gen_incl, incl_dir)
                 if d is not None and d.is_dir()]
    if gen_incl is not None and gen_incl.is_dir():
        print(f"[gen] include overlay: {gen_incl}")
    deck_root_dir = Path(deck_root) if deck_root else tree
    if con80_dir is None or srcdirs is None:
        raise typer.BadParameter(
            "give --root, or explicit --con80 and --src directories")
    libdir = Path(lib_dir) if lib_dir else Path(out)
    outdir = Path(out)                  # this phase's load module lands here
    out_root = Path(out) / name if phase is not None else Path(out)
    objdir = out_root / "obj"
    gendir = out_root / "gen"
    linklibs = ([Path(d) for d in linklib] if linklib
                else list(_DEFAULT_LINKLIBS))

    need = [("--con80", con80_dir)] + [("--src", d) for d in srcdirs]
    if gen is not None:
        need.append(("--gen", Path(gen)))
    if run_assemble:
        need.append(("--mlib", mlib_dir))
    if run_hal or run_display:
        need += [("--incl", incl_dir), ("--pass-rel32", pass_rel32)]
    if run_display:
        need.append(("--deck-root", deck_root_dir))
    need += [("--runlib", d) for d in runlibs]
    if run_link:
        need += [("--linklib", d) for d in linklibs]
    _require_dirs(need)

    con80_dir = _con80_overlay(gen_tree, con80_dir, out)

    if phase is not None:
        _deck = concard.ConcardDeck(str(con80_dir))
        if not _deck.has(target):
            _valid = [nums[0] for nums, _ in
                      concard.phase_segments(_deck, master) if nums]
            raise typer.BadParameter(
                f"phase {phase} is not defined by {master}'s PHASE "
                f"directives; valid phases: "
                f"{' '.join(str(n) for n in _valid)}")

    with _timed("step", "SourceIndex"):
        srcidx = SourceIndex(srcdirs)
    by_type, runtime, unresolved, patches = resolve_worklist(
        con80_dir, target, srcidx, runtime_names(runlibs))

    print(f"[{name}] sources: {len(by_type['ASM'])} ASM, {len(by_type['HAL'])} HAL, "
          f"{len(by_type['DISPLAY'])} display, {len(by_type['AMT'])} AMT, "
          f"{len(patches)} patch; "
          f"{len(runtime)} runtime/library, {len(unresolved)} unresolved")
    if unresolved:
        print("  unresolved:", " ".join(sorted(unresolved)[:30]),
              "..." if len(unresolved) > 30 else "")

    # NOCALLER in the *target's* deck (not the layout root, which is usually
    # the whole-SSW OFTMP) = the linkage editor's NCAL option: this load
    # module keeps unresolved externals without failing (e.g. PHASE01's dummy
    # SSL copy references FIOMUWB2, resolved only in the phase that actually
    # runs the SSL).
    nocall = concard.has_nocall(concard.ConcardDeck(str(con80_dir)), target)

    if emit_cmake:
        if (by_type["ASM"] or patches) and mlib_dir is None:
            raise typer.BadParameter("--emit-cmake needs --mlib (or --root): "
                                     "the worklist has assembly sources")
        from . import con80cmake
        con80cmake.emit(
            Path(emit_cmake), root=target, by_type=by_type, patches=patches,
            srcdirs=srcdirs, con80_dir=con80_dir, mlib=mlib_dir,
            incl80=incl_dirs, deck_root=deck_root_dir, out_root=out_root,
            python=python, halsc=os.path.abspath(halsc),
            pass_rel32=pass_rel32.resolve(), tolerable=tolerable,
            linklibs=linklibs, concard_root=concard_root,
            allow_undefined=not no_undefined, nocall=nocall)
        print(f"wrote {emit_cmake}")

    if plan or (emit_cmake and not (run_assemble or run_hal
                                    or run_display or run_link)):
        return

    if run_assemble:
        if mlib_dir is None:
            raise typer.BadParameter("--assemble needs --mlib (or --root)")
        with _timed("phase", "assemble"):
            ok, fails = assemble(by_type["ASM"], objdir, mlib_dir,
                                 python, tolerable, verbose)
        print(f"assembled {ok}/{len(by_type['ASM'])} ASM -> {objdir}"
              + (f"; {len(fails)} failed" if fails else ""))
        if fails:
            print("  failed:", " ".join(sorted(fails)))
        if patches:
            pok, pfails = assemble_patches(patches, objdir, mlib_dir,
                                           python, tolerable, verbose)
            print(f"assembled {pok}/{len(patches)} patch (PCHnnTXT.obj) -> {objdir}"
                  + (f"; {len(pfails)} failed" if pfails else ""))
            if pfails:
                print("  failed:", " ".join(sorted(pfails)))

    if run_hal:
        with _timed("phase", "hal"):
            ok, fails = hal(by_type["HAL"], objdir, gendir,
                            os.path.abspath(halsc), python,
                            pass_rel32.resolve(), srcdirs, incl_dirs, verbose,
                            displays=by_type["DISPLAY"] + by_type["AMT"],
                            gen_tree=gen_tree, phase=phase)
        print(f"compiled {ok}/{len(by_type['HAL'])} HAL -> {objdir}"
              + (f"; {len(fails)} failed" if fails else ""))
        if fails:
            print("  failed:", " ".join(sorted(fails)))
            _halt(f"HAL step: {len(fails)} unit(s) failed")

    if run_display:
        with _timed("phase", "display"):
            ok, fails = display(by_type["DISPLAY"], objdir, gendir,
                                os.path.abspath(halsc), python,
                                pass_rel32.resolve(), srcdirs, incl_dirs,
                                deck_root_dir, verbose, gen_tree=gen_tree,
                                phase=phase)
        print(f"built {ok}/{len(by_type['DISPLAY'])} displays -> {objdir}"
              + (f"; {len(fails)} failed" if fails else ""))
        if fails:
            print("  failed:", " ".join(sorted(fails)))
        if by_type["AMT"]:
            with _timed("phase", "display_amt"):
                aok, afails = display(by_type["AMT"], objdir, gendir,
                                      os.path.abspath(halsc), python,
                                      pass_rel32.resolve(), srcdirs, incl_dirs,
                                      deck_root_dir, verbose, tag="dfg amt")
            print(f"built {aok}/{len(by_type['AMT'])} AMT compools -> {objdir}"
                  + (f"; {len(afails)} failed" if afails else ""))
            if afails:
                print("  failed:", " ".join(sorted(afails)))

    # AUTOMATIC LIBRARY CALL (see library_stub_syms).  Decks without
    # NOCALLER ran the LE with autocall: unresolved externals pull modules
    # from the application library.  Model: scan compiled objects' ERs,
    # resolve the definer in the source tree, compile it, iterate.  STACK
    # $0<prog> cards seed the set (stack creation forces the module in).
    # Only jobs whose autocall placement is validated against a flight
    # load run the closure -- each config needs its own wave-order/anchor
    # derivation. 
    AUTOCALL_VALIDATED = {4, 5, 6, 7, 8, 12, 15, 16}
    autocalled: list[dict] = []
    # The autocall decision is the GATE's, never a leftover file's: track
    # it explicitly so the link step and the autocall.json lifecycle below
    # follow the same verdict.
    autocall_wanted = autocall and not nocall and phase in AUTOCALL_VALIDATED
    steps_running = (run_hal or run_assemble or run_link) and objdir.is_dir()
    autocall_live = autocall_wanted and steps_running
    autocall_path = out_root / "autocall.json"
    if autocall_live:
        stub_syms = library_stub_syms(con80_dir, target)
        with _timed("step", "csect_defs"):
            csect_defs = srcidx.csect_defs()
        # MAP-phase definitions: csects the co-resident base already defines.
        ext_defs: set[str] = set()
        _deck0 = concard.ConcardDeck(str(con80_dir))
        # `MAP p,<region...>` imports only the named regions' definitions.
        # A definition in an unmapped region is not resident for this MC:
        # the LE leaves such a reference unresolved and autocall re-links
        # the module into this phase.  A bare `MAP p` (no regions) imports
        # the whole phase.
        _map_regions: dict[int, list | None] = {}
        for op in concard.layout_program(_deck0, target):
            if op.verb != "MAP" or not op.operand:
                continue
            _parts = [t.strip() for t in op.operand.split(",")]
            if not _parts[0].isdigit():
                continue
            _pn = int(_parts[0])
            _regs = [t for t in _parts[1:] if t]
            if not _regs:
                _map_regions[_pn] = None          # whole phase
            elif _map_regions.get(_pn, []) is not None:
                _map_regions.setdefault(_pn, []).extend(_regs)
        _deps = sorted(_map_regions)
        for p_ in _deps:
            symp = libdir / f"PHASE{p_:02d}" / f"PHASE{p_:02d}.sym.json"
            if symp.exists():
                import json as _json
                _sym = _json.loads(symp.read_text())
                # imported markers (module 'X>') describe ANOTHER phase's
                # definition -- the defining phase's own (region-gated)
                # entries are the authority on residency
                entries = [e for e in (list(_sym.get("sections", []))
                                       + list(_sym.get("symbols", [])))
                           if not e.get("module", "").endswith(">")]
                _regs = _map_regions.get(p_)
                _libp = libdir / f"PHASE{p_:02d}.lib"
                if _regs is not None and _libp.exists():
                    from ap101Utils.libModule import (LibModule,
                                                      regionIntervals)
                    _ivals = [(s // 2, e // 2) for s, e in regionIntervals(
                        LibModule.read(_libp), _regs)]
                    if _ivals:
                        # Only REMOTE-class compools re-link per MC; every
                        # other unmapped base definition still resolves in
                        # place (the LE's one-job model), so only #P names
                        # outside the mapped intervals stop suppressing.
                        _n0 = len(entries)
                        entries = [e for e in entries
                                   if not e["name"].startswith("#P")
                                   or any(e["address"] < t
                                          and e["address"]
                                          + max(e.get("size", 1), 1) > s
                                          for s, t in _ivals)]
                        if _n0 != len(entries):
                            print(f"[{name}] autocall: MAP phase {p_} "
                                  f"region-gated -- {_n0 - len(entries)} "
                                  f"unmapped remote-compool definition(s) "
                                  f"won't suppress pulls")
                ext_defs.update(e["name"] for e in entries)
            else:
                # an empty exclusion set makes the closure pull modules the
                # co-resident base already defines (split flight-fixed
                # csects) -- refuse quietly-wrong, warn loudly
                print(f"[{name}] autocall: MAP phase {p_} sym.json missing "
                      f"({symp}); its csects WON'T suppress pulls -- "
                      f"build phase {p_} first", file=sys.stderr)
        seeds = stack_seed_modules(con80_dir, target)
        # The deck's LE CHANGE cards rewrite the CESD of the module each one
        # precedes; autocall must demand the renamed external, not the name
        # in the object's raw ESD (see concard_changes).
        changes = concard_changes(con80_dir, target)
        if changes:
            print(f"[{name}] autocall: {sum(len(v) for v in changes.values())}"
                  f" CHANGE rename(s) over {len(changes)} INCLUDE member(s)")
        attempted: set[Path] = set()   # newsrc gets ONE compile attempt
        nosource_seen: set[str] = set()
        converged = False
        for wave in range(6):
            _t_wave = time.time()
            with _timed("step", f"autocall_scan{wave}"):
                linkset = [p for p in worklist_objects(objdir, by_type, patches)
                           if p.exists()]
                need, defined = autocall_scan(linkset, ext_defs, stub_syms,
                                              runtime_names(runlibs),
                                              changes=changes)
            if wave == 0:
                need = [s for s in seeds if s not in defined] + need
            newsrc: list[tuple[str, Path]] = []
            relist: list[tuple[str, Path]] = []
            worklisted = {q for lst in by_type.values() for q in lst}
            relisted = set()
            for n in need:
                p = csect_defs.get(n) or srcidx.resolve(n)
                if p is None:
                    if n not in nosource_seen:
                        nosource_seen.add(n)
                    continue
                if p.stem in stub_syms:
                    # LIBRARY-stub cards name the library MEMBER; a demand
                    # for one of its inner symbols is suppressed the same
                    # way.
                    continue
                if (objdir / f"{p.stem}.obj").exists():
                    _sdfm = gendir / "SDFLIB" / f"##{p.stem[:6]:<6}.sdf"
                    sdf_ok = (classify(p) == "ASM"
                              or (_sdfm.exists()
                                  and _sdfm.stat().st_size > 3360))
                    if sdf_ok:
                        if p not in worklisted and p not in relisted:
                            relist.append((n, p))
                            relisted.add(p)
                        continue
                if p in worklisted:
                    # a deck member with no object = its MAIN-step compile
                    # failed; adopting it as "autocalled" would exempt it
                    # from later-phase deferral and hand it to the
                    # placement waves at the wrong position
                    print(f"[{name}] autocall: {p.name} is deck worklist "
                          f"with no object (compile failed?) -- NOT "
                          f"adopting as autocall", file=sys.stderr)
                    continue
                if p in attempted:
                    print(f"[{name}] autocall: {p.name} produced no object "
                          f"last wave -- giving up on {n}", file=sys.stderr)
                    continue
                if p not in [q for _, q in newsrc]:
                    newsrc.append((n, p))
                    attempted.add(p)
            byt = {"ASM": [], "HAL": [], "DISPLAY": [], "AMT": []}
            newpaths = {q for _, q in newsrc}
            for n, p in newsrc + relist:
                kind = classify(p)
                if p in newpaths:
                    byt[kind].append(p)       # needs compiling this wave
                if p not in by_type[kind]:
                    by_type[kind].append(p)   # joins the link's object list
                autocalled.append({"symbol": n, "source": str(p),
                                   "member": p.name, "wave": wave})
            if not newsrc and not relist:
                converged = True
                break
            print(f"[{name}] autocall wave {wave}: "
                  + " ".join(p.name for _, p in newsrc)
                  + ("" if not relist else
                     " (rejoining: " + " ".join(p.name for _, p in relist)
                     + ")"))
            if byt["AMT"]:
                # main flow compiles AMTs in a separate dfg pass; the
                # closure has no such branch -- an AMT pull would loop
                print(f"[{name}] autocall: AMT compool(s) pulled but the "
                      f"closure has no AMT compile path (unimplemented): "
                      + " ".join(p.name for p in byt['AMT']),
                      file=sys.stderr)
            if byt["ASM"] and mlib_dir is not None:
                assemble(byt["ASM"], objdir, mlib_dir, python, tolerable,
                         verbose)
            if byt["HAL"]:
                hal(byt["HAL"], objdir, gendir, os.path.abspath(halsc),
                    python, pass_rel32.resolve(), srcdirs, incl_dirs, verbose,
                    displays=byt["DISPLAY"] + byt["AMT"],
                    owned=set(by_type["HAL"]) - set(byt["HAL"]),
                    gen_tree=gen_tree, phase=phase)
            if byt["DISPLAY"]:
                display(byt["DISPLAY"], objdir, gendir,
                        os.path.abspath(halsc), python, pass_rel32.resolve(),
                        srcdirs, incl_dirs, deck_root_dir, verbose,
                        gen_tree=gen_tree, phase=phase)
            _tlog(kind="phase", name=f"autocall_wave{wave}", t0=_t_wave,
                  dt=time.time() - _t_wave) if _TIMING else None
        if not converged:
            print(f"[{name}] autocall: closure NOT converged after 6 "
                  f"waves -- link set may be incomplete", file=sys.stderr)
        if nosource_seen:
            print(f"[{name}] autocall: {len(nosource_seen)} symbol(s) with "
                  f"no source definer stay unresolved: "
                  + " ".join(sorted(nosource_seen)[:12])
                  + (" ..." if len(nosource_seen) > 12 else ""))

    if autocall_live and autocalled:
        import json as _json
        autocall_path.write_text(_json.dumps(autocalled, indent=1))
        print(f"[{name}] autocall: {len(autocalled)} module(s) pulled "
              f"-> {autocall_path}")
    elif steps_running and autocall_path.exists():
        autocall_path.unlink()
        print(f"[{name}] autocall: removed stale {autocall_path.name} "
              + ("(gate on, nothing pulled)" if autocall_live
                 else "(autocall gated off for this job)"))

    if steps_running and phase is not None and objdir.is_dir():
        expected = {(p.stem + ".obj")
                    for lst in by_type.values() for p in lst}
        expected |= {member + ".obj" for member in patches.values()}
        stale = sorted(f.name for f in objdir.glob("*.obj")
                       if f.name not in expected)
        if stale:
            print(f"[{name}] objdir hygiene: {len(stale)} stale .obj "
                  + ("PRUNED" if prune_objs else
                     "(not in worklist/autocall; --prune-objs deletes)")
                  + ": " + " ".join(stale[:10])
                  + (" ..." if len(stale) > 10 else ""), file=sys.stderr)
            if prune_objs:
                for fn in stale:
                    (objdir / fn).unlink()

    if run_link:
        fcm = out_root / f"{name}.fcm"
        symjson = fcm.with_suffix(".sym.json")

        # Resolve the deck's MAP dependencies: every `MAP p,...` card needs
        # phase p's load module.  Explicit --map-lib p=path wins; otherwise
        # a phase build expects the sibling <out>/PHASE0p.lib.
        map_libs = list(map_lib or [])
        given = {spec.partition('=')[0] for spec in map_libs}
        deps = sorted({
            int(op.operand.split(',')[0])
            for op in concard.layout_program(
                concard.ConcardDeck(str(con80_dir)), concard_root)
            if op.verb == "MAP" and op.operand.split(',')[0].isdigit()})
        missing = []
        for p in deps:
            if str(p) in given:
                continue
            dep = libdir / f"PHASE{p:02d}.lib"
            if dep.exists():
                map_libs.append(f"{p}={dep}")
            else:
                missing.append(p)
        if missing:
            for p in missing:
                print(f"  [link] MAP needs phase {p}: build it first "
                      f"(con80build --phase {p} --out {libdir} ... --link)",
                      file=sys.stderr)
            print("link FAILED (missing MAP phase libraries)",
                  file=sys.stderr)
            raise typer.Exit(1)

        # The linkage editor processes the master deck as one job, so a
        # phase segment sees the overlay-region table built up by every earlier
        # segment: PHASE13's origin-less `OVERLAY PHAS13D` re-opens a region the
        # phase-2 segment defined, with no MAP card at all (the deck never
        # needed one).  Model that state by also supplying every earlier
        # phase's load module: lnk101 imports region ORIGINS from every
        # supplied lib (lowest phase wins) but reserves intervals and imports
        # symbols only for explicit MAP cards, so this changes nothing except
        # origin-less OVERLAY re-opens -- the same re-open the LE would do.
        if phase is not None:
            order = [nums[0] for nums, _ in concard.phase_segments(
                concard.ConcardDeck(str(con80_dir)), master) if nums]
            if phase in order:
                have = given | {str(p) for p in deps}
                absent = []
                for p in order[:order.index(phase)]:
                    dep = libdir / f"PHASE{p:02d}.lib"
                    if str(p) in have:
                        continue
                    if dep.exists():
                        map_libs.append(f"{p}={dep}")
                    else:
                        absent.append(p)
                if absent:
                    print(f"  [link] {len(absent)} earlier phase load "
                          f"module(s) NOT FOUND in {libdir}: "
                          + " ".join(f"PHASE{p:02d}" for p in absent), file=sys.stderr)

        # Scope the link to this root's modules so a partial build (e.g. one
        # phase) doesn't pull other phases' csects out of the shared obj dir.
        objs = worklist_objects(objdir, by_type, patches)
        # A CON80 INSERT is an unconditional, positioned placement.  Under NCAL
        # the resident library is not autocalled, so INSERTed library csects
        # must be loaded explicitly from -L or they go missing (and whatever
        # does load lands in reference order, not the deck's INSERT order).
        extra_objs, _ = inserted_runtime_objects(
            con80_dir, concard_root, linklibs, set(objs))
        if extra_objs:
            print(f"  [link] +{len(extra_objs)} INSERTed runtime object(s) "
                  f"loaded explicitly (NCAL: no autocall)")
            objs = objs + extra_objs
        libout = (outdir / f"{name}.lib" if phase is not None
                  else fcm.with_suffix(".lib"))
        good = link(objdir, fcm, con80_dir, concard_root, linklibs,
                    python, json_symbols=symjson,
                    allow_undefined=not no_undefined, nocall=nocall,
                    verbose=verbose, objs=objs, map_libs=map_libs,
                    lib=libout, warn_unresolved=warn_unresolved_phases,
                    autocall_json=autocall_path if autocall_live else None,
                    link_order=link_order_path)
        print(f"linked {len(objs)} objects -> {fcm} (+ {libout.name})"
              if good else "link FAILED (see above)")
        if not good:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
