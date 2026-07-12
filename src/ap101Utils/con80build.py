#!/usr/bin/env python3
#
# Build a PFS flight load from a CON80 deck:
# 
#   1. resolve   -- read the CON80 deck, loads the worklist and worklist 
#                   and map each object-module name to its source file.
#   2. classify  -- decide HAL/S vs assembly per source.
#   3. assemble  -- run asm101 on the assembly sources
#   4. hal       -- run halsc on the HAL sources in template-dependency order
#                   (TODO: ordering/cardtypes ported from PASS.REL32V0/compilePASS).
#   5. display   -- uses dfg to generate HAL/S COMPOOLS from display decks.
#   6. link      -- run lnk101 with --concard emitting <out>/<root>.fcm 
# 
# Object output goes under <out>/obj
# Generated intermediates go under <out>/gen
#
# --emit-cmake FILE freezes the resolved worklist into a standalone CMake
#
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

from . import cards, concard, halorder


def _run(cmd, *, verbose: int = 0, env=None, cwd=None, label: str | None = None):
    if verbose:
        loc = f"   # cwd={cwd}" if cwd else ""
        tag = f"[{label}] " if label else ""
        print(f"+ {tag}" + " ".join(shlex.quote(str(c)) for c in cmd) + loc,
              flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    if verbose:
        out = (r.stdout or "") + (r.stderr or "")
        if out.strip():
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
            sys.stdout.flush()
    return r


SOURCE_DIRS = ("SSSRC", "APPLSRC")
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


# Some heuristics to decide whether a specific file is HAL/ASM/DFG code.
# Honestly we should probably be hardcoding a list somewhere, but the
# source file patterns are clean enough that this works semi-automagically:
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
    """Return 'HAL', 'ASM', or 'DISPLAY' for a source file"""
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
        if re.match(r"^(HEADER|INCLUDE|PAD|XC|YC|CHAR)\s*=", line):
            return "DISPLAY"
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

    by_type: dict[str, list[Path]] = {"ASM": [], "HAL": [], "DISPLAY": []}
    patches: dict[Path, str] = {}
    seen: set[Path] = set()
    runtime: list[str] = []
    unresolved: list[str] = []
    for m in modules:
        path = src.resolve(m) or provided.get(m)
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


def _assemble(items, objdir: Path, mlib: Path, python: str,
              tolerable: int = 4, verbose: int = 0,
              tag: str = "asm101") -> tuple[int, list[str]]:
    objdir.mkdir(parents=True, exist_ok=True)
    ok = 0
    failures: list[str] = []
    env = {**os.environ, "PYTHONUTF8": "1"}
    n = len(items)
    for i, (srcpath, obj) in enumerate(items, 1):
        cmd = [python, "-m", "asm101",
               f"--object={obj}", f"--library={mlib}",
               f"--tolerable={tolerable}", str(srcpath)]
        r = _run(cmd, verbose=verbose, env=env,
                 label=f"{tag} {i}/{n} {srcpath.name}")
        if r.returncode == 0 and obj.exists():
            ok += 1
        else:
            failures.append(srcpath.name)
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            print(f"  [{tag} FAIL {i}/{n}] {srcpath.name}: {tail[0]}",
                  file=sys.stderr)
    return ok, failures


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


def _hal_mirror(dirs, haltree: Path) -> None:
    """Build a .hal-extensioned symlink mirror of the (extensionless) HAL
    source dirs, so PASS tools that rglob('*.hal') (preprocessHALSFC) work.
    Each directory is mirrored under its basename."""
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
            if not link.exists():
                link.symlink_to(f.resolve())


def hal(sources, objdir: Path, gendir: Path, halsc: str, python: str,
        pass_rel32: Path, srcdirs, incl80: Path | None,
        verbose: int = 0) -> tuple[int, list[str]]:
    """Compile the HAL sources with halsc in template-dependency order, sharing
    one TEMPLIB so each unit sees the templates of the units before it.  Each
    source is first run through preprocessHALSFC (which expands transitive
    D INCLUDE TEMPLATE directives, avoiding DI11 errors) into gen/pp.
    `srcdirs` are the source search directories;
    `incl80` is the HAL-include directory.
    Returns (ok_count, failures)."""
    objdir.mkdir(parents=True, exist_ok=True)
    gendir.mkdir(parents=True, exist_ok=True)
    templib = (gendir / "TEMPLIB").resolve()

    # Worklist-completeness: the worklist units `D INCLUDE TEMPLATE` compools and
    # procedures that live in *other* overlays and so aren't in this link's
    # worklist, but whose templates must still be built.  
    # The transitive objects are SEGREGATED into gen/tmpl_obj so the concard-driven link
    # doesn't use it.
    worklist = list(sources)
    worklist_set = set(worklist)
    tree = halorder.tree_units(srcdirs)
    extra, _seed_progs = halorder.template_closure(worklist, tree)
    sources = worklist + extra
    tmplobj = (gendir / "tmpl_obj").resolve()
    tmplobj.mkdir(parents=True, exist_ok=True)
    print(f"  template closure: +{len(extra)} producers "
          f"(real-compiled, objects segregated to tmpl_obj)")

    src_root = Path(__file__).resolve().parent.parent       # .../src
    env = {**os.environ, "PYTHONUTF8": "1",
           "PYTHONPATH": os.pathsep.join([str(pass_rel32), str(src_root),
                                          os.environ.get("PYTHONPATH", "")])}

    # .hal mirror + preprocess each source into gen/pp/<stem>.hal.
    haltree = (gendir / "haltree").resolve()
    _hal_mirror([*srcdirs] + ([incl80] if incl80 else []), haltree)
    ppdir = (gendir / "pp").resolve()
    ppdir.mkdir(parents=True, exist_ok=True)
    pp_of: dict = {}
    for srcpath in sources:
        rel = f"{srcpath.parent.name}/{srcpath.name}.hal"
        out = ppdir / (srcpath.stem + ".hal")
        r = _run([python, str(pass_rel32 / "preprocessHALSFC"),
                  f"--in={rel}", f"--out={out}"],
                 verbose=verbose, cwd=str(haltree), env=env,
                 label=f"preprocessHALSFC {srcpath.name}")
        pp_of[srcpath] = out if (r.returncode == 0 and out.exists()) else srcpath

    # Initialise (clear) the template-library PDS via the PASS toolchain script.
    if templib.exists():
        import shutil
        shutil.rmtree(templib)
    _run([python, str(pass_rel32 / "prepareTEMPLIB"), "--clear"],
         verbose=verbose, cwd=str(gendir), env=env, label="prepareTEMPLIB")

    # Build the include dir: the macros referenced by non-template
    # D INCLUDE name directives are NOT inlined by preprocessHALSFC; the 
    # compiler resolves them at compile time from the passed in INCLIB
    # prepareINCLIB converts each INCL80 member to *EBCDIC* PDS records 
    #
    inclib = (gendir / "INCLIB").resolve()
    incl_mirror = haltree / incl80.name if incl80 else None
    if incl_mirror is not None and incl_mirror.is_dir():
        ienv = {**env, "PYTHONPATH": os.pathsep.join(
            [str(src_root / "XCOM-I"), env.get("PYTHONPATH", "")])}
        _run([python, str(pass_rel32 / "prepareINCLIB"),
              "--clear", f"--include={incl_mirror}"],
             verbose=verbose, cwd=str(gendir), env=ienv, label="prepareINCLIB")

    seedsdir = (gendir / "seeds").resolve()
    seedsdir.mkdir(parents=True, exist_ok=True)

    def compile_stub(progname, dn):
        """Compile a minimal empty-body PROGRAM so the compiler emits its
        (body-independent) program template into TEMPLIB."""
        stub = seedsdir / f"{dn}.hal"
        stub.write_text(f" {progname}: PROGRAM;\n CLOSE {progname};\n")
        _run([halsc, f"--parm={halorder.get_parms('_seed')}",
              f"--templib={templib}", f"--inclib={inclib}",
              f"--workdir={gendir / ('_seed_' + dn + '.work')}",
              "-o", str(gendir / ('_seed_' + dn + '.obj')), str(stub)],
             verbose=verbose, env=env, label=f"halsc seed {dn}")
        return (templib / f"@@{dn}").exists()

    # Seed PROGRAM templates to break compool/program template cycles.
    # The compiler inhibits template generation on ANY error, so a unit that 
    # D INCLUDE TEMPLATEs with an include loop fail. 
    #   (e.g. CDM_UI_COMPOOL <-> ASM_AUX_DPS)
    # The program template is just the program's declaration line, so we
    # fake it here to break the loop:
    src_index = {}
    for sd in srcdirs:
        sd = Path(sd)
        if sd.is_dir():
            for q in sd.iterdir():
                if q.is_file():
                    full, kind = halorder.unit_header(q)
                    if full:
                        src_index.setdefault(halorder.descore(full), (full, kind))
    referenced: set = set()
    for p in sources:
        _, deps = halorder.parse_hal(pp_of.get(p, p))
        referenced |= set(deps)
    built = {f.replace("@@", "") for f in os.listdir(templib)} if templib.exists() else set()
    nseed = 0
    for dn in sorted(referenced - built):
        info = src_index.get(dn)
        if info and info[1] == "PROGRAM" and compile_stub(info[0], dn):
            nseed += 1
    print(f"  seeded {nseed} program templates (cycle-break)")

    def _run_halsc(srcpath, obj, parm):
        workdir = gendir / (srcpath.stem + ".work")
        cmd = [halsc, f"--parm={parm}",
               f"--templib={templib}", f"--inclib={inclib}",
               f"--workdir={workdir}",
               "-o", str(obj), str(pp_of.get(srcpath, srcpath))]
        r = _run(cmd, verbose=verbose, env=env, label=f"halsc {srcpath.stem}")
        return r, workdir

    def compile_one(srcpath, obj=None):
        dest = objdir if srcpath in worklist_set else tmplobj
        obj = obj or dest / (srcpath.stem + ".obj")
        parm = halorder.get_parms(srcpath.stem)
        r, workdir = _run_halsc(srcpath, obj, parm)
        out = r.stdout + r.stderr
        if r.returncode == 0 and obj.exists():
            return True, ""
        # ZO3: the OPT pass over-fires "loop invariant pulled from IF-THEN"
        # virtualagc's compilePASS retries modules that fail with ,X1
        # to supress the optimization.  This seems to match the baselines:
        #
        optrpt = workdir / "opt.rpt"
        if optrpt.exists() and "ZO3" in optrpt.read_text(errors="replace"):
            r, workdir = _run_halsc(srcpath, obj, parm + ",X1")
            out = r.stdout + r.stderr
            if r.returncode == 0 and obj.exists():
                return True, ""
        if "BI002" in out:
            reason = "BI002 space-management compiler bug"
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
    pending = halorder.topo_order(sources)
    built: set = set()
    reasons: dict[str, str] = {}
    while pending:
        progressed = []
        still = []
        for srcpath in pending:
            good, why = compile_one(srcpath)
            if good:
                built.add(srcpath); progressed.append(srcpath)
                reasons.pop(srcpath.name, None)
            else:
                reasons[srcpath.name] = why; still.append(srcpath)
        if not progressed:
            break
        pending = still

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
            pass_rel32: Path, srcdirs, incl80: Path | None,
            deck_root: Path | None, verbose: int = 0) -> tuple[int, list[str]]:
    """dfg (deck -> HAL/S compool source),
    display compools are included TEXTUALLY, so their own dependency includes 
    must be inserted first), then halsc into objdir/<name>.obj (csect #P<name>).

    Runs AFTER hal(): the displays' INCLUDEd compool templates must already be
    in gen/TEMPLIB.  Compiles against a SCRATCH copy of that TEMPLIB — halsc's
    TEMPLATE parm APPENDS each compiled unit's own template, and display
    templates (@@CD0001 ...) must never pollute the shared library.
    Returns (ok_count, failures)."""
    import shutil
    objdir.mkdir(parents=True, exist_ok=True)
    gendir.mkdir(parents=True, exist_ok=True)
    templib = (gendir / "TEMPLIB").resolve()
    inclib = (gendir / "INCLIB").resolve()
    dispdir = (gendir / "displays").resolve()
    dispdir.mkdir(parents=True, exist_ok=True)
    scratch = (gendir / "disp_TEMPLIB").resolve()
    if scratch.exists():
        shutil.rmtree(scratch)
    if templib.is_dir():
        shutil.copytree(templib, scratch)
    else:
        print(f"  [display] no {templib} — run --hal first", file=sys.stderr)
        return 0, [p.stem for p in sources]
    haltree = (gendir / "haltree").resolve()
    _hal_mirror([*srcdirs] + ([incl80] if incl80 else []), haltree)

    src_root = Path(__file__).resolve().parent.parent       # .../src
    env = {**os.environ, "PYTHONUTF8": "1",
           "PYTHONPATH": os.pathsep.join([str(pass_rel32), str(src_root),
                                          os.environ.get("PYTHONPATH", "")])}
    ok, fails = 0, []
    for srcpath in sources:
        name = srcpath.stem
        halout = dispdir / f"{name}.hal"
        cmd = [python, "-m", "dfg", name, "--templib", str(templib),
               "-o", str(halout)]
        if deck_root is not None:
            cmd += ["--deck-root", str(deck_root)]
        r = _run(cmd, verbose=verbose, env=env, label=f"dfg {name}")
        if r.returncode != 0 or not halout.exists():
            tail = ((r.stderr or "").strip().splitlines()[-1:] or ["?"])[0]
            print(f"  [dfg FAIL] {name}: {tail[:70]}", file=sys.stderr)
            fails.append(name)
            continue
        pp = dispdir / f"{name}.pp.hal"
        r = _run([python, str(pass_rel32 / "preprocessHALSFC"),
                  f"--in={halout}", f"--out={pp}"],
                 verbose=verbose, cwd=str(haltree), env=env,
                 label=f"preprocessHALSFC {name}")
        src = pp if (r.returncode == 0 and pp.exists()) else halout
        obj = objdir / f"{name}.obj"
        r = _run([halsc, f"--parm={halorder.get_parms(name)}",
                  f"--templib={scratch}", f"--inclib={inclib}",
                  f"--workdir={dispdir / (name + '.work')}",
                  "-o", str(obj), str(src)],
                 verbose=verbose, env=env, label=f"halsc {name}")
        if r.returncode == 0 and obj.exists():
            ok += 1
            shutil.rmtree(dispdir / (name + ".work"), ignore_errors=True)
        else:
            out = (r.stdout or "") + (r.stderr or "")
            tail = (out.strip().splitlines()[-1:] or ["?"])[0]
            print(f"  [halsc FAIL] {name}: {tail[:70]}", file=sys.stderr)
            fails.append(name)
    return ok, fails


def worklist_objects(objdir: Path, by_type: dict, patches: dict) -> list[Path]:
    """The object files this worklist produces: <stem>.obj for each
    ASM/HAL/DISPLAY source and PCHnnTXT.obj for each patch, filtered to
    those that exist."""
    want = [objdir / (p.stem + ".obj") for p in by_type["ASM"]]
    want += [objdir / (p.stem + ".obj") for p in by_type["HAL"]]
    want += [objdir / (p.stem + ".obj") for p in by_type["DISPLAY"]]
    want += [objdir / (member + ".obj") for member in patches.values()]
    return [o for o in want if o.exists()]


def link(objdir: Path, fcm: Path, deck_dir: Path, concard_root: str,
         linklibs, python: str, json_symbols: Path | None = None,
         allow_undefined: bool = True, verbose: int = 0,
         objs: list[Path] | None = None) -> bool:
    """Link objects into `fcm`, placing csects from the CON80 deck (lnk101
    --concard).  `objs` is the explicit object list to link; when None, every
    *.obj in `objdir` is linked."""
    objs = sorted(objdir.glob("*.obj")) if objs is None else sorted(objs)
    if not objs:
        print(f"  [lnk101] no objects to link in {objdir} "
              f"(run --assemble / --hal first)", file=sys.stderr)
        return False
    cmd = [python, "-m", "lnk101", *(str(o) for o in objs)]
    for lib in linklibs:
        cmd += ["-L", str(lib)]
    cmd += ["--concard", str(deck_dir), "--concard-root", concard_root,
            "-o", str(fcm)]
    if json_symbols is not None:
        cmd += ["--json-symbols", str(json_symbols)]
    if allow_undefined:
        cmd.append("--allow-undefined")
    env = {**os.environ, "PYTHONUTF8": "1"}
    r = _run(cmd, verbose=verbose, env=env, label=f"lnk101 -> {fcm.name}")
    ok = r.returncode == 0 and fcm.exists()
    if not ok:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-1:] or [""]
        print(f"  [lnk101 FAIL] {tail[0]}", file=sys.stderr)
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
    build: Annotated[str, typer.Option("--build",
        help="build dir; outputs under <build>/OI340600/{obj,gen} "
             "unless --out overrides")],
    oi_dir: Annotated[Optional[str], typer.Option("--oi",
        help="conventionally laid out source tree (CON80/, SSSRC/, APPLSRC/, "
             "MLIB80/, INCL80/); shorthand for --con80/--src/--mlib/--incl")] = None,
    con80: Annotated[Optional[str], typer.Option("--con80",
        help="CON80 deck directory [default: <oi>/CON80]")] = None,
    src: Annotated[Optional[list[str]], typer.Option("--src", metavar="DIR",
        help="source search directory (repeatable, searched in order) "
             "[default: <oi>/SSSRC, <oi>/APPLSRC]")] = None,
    mlib: Annotated[Optional[str], typer.Option("--mlib",
        help="asm101 macro/COPY library [default: <oi>/MLIB80]")] = None,
    incl: Annotated[Optional[str], typer.Option("--incl",
        help="HAL inclusion library (INCL80) [default: <oi>/INCL80]")] = None,
    deck_root: Annotated[Optional[str], typer.Option("--deck-root",
        help="root the dfg display generator resolves deck sources under "
             "[default: --oi]")] = None,
    out: Annotated[Optional[str], typer.Option("--out",
        help="output root (obj/, gen/, <root>.fcm beneath) "
             "[default: <build>/OI340600]")] = None,
    emit_cmake: Annotated[Optional[str], typer.Option("--emit-cmake",
        metavar="FILE",
        help="write a standalone CMake recipe that rebuilds this worklist "
             "without reading CON80 or running con80build")] = None,
    root: Annotated[str, typer.Option("--root",
        help="top CON80 card")] = "SSW",
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
        help="link the built objects into <out>/<root>.fcm "
             "with CON80 (lnk101 --concard)")] = False,
    concard_root: Annotated[str, typer.Option("--concard-root",
        help="top CON80 card laid out by the linker (--root selects the "
             "config within it)")] = "OFTMP",
    linklib: Annotated[Optional[list[str]], typer.Option("--linklib", metavar="DIR",
        help="runtime object-library dir for the link (-L; repeatable; "
             "default: <build>/lib/runtime/{RUN,ZCON})")] = None,
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
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True,
        help="echo each toolchain command (asm101/halsc/...) and its "
             "output as it runs")] = 0,
) -> None:
    pass_rel32 = Path(pass_rel32_dir)
    runlibs = ([Path(d) for d in runlib] if runlib
               else [pass_rel32 / "RUNASM", pass_rel32 / "ZCONASM"])

    # Tree locations: everything defaults from the conventional --oi layout
    # but any piece can be pointed elsewhere explicitly.
    oi = Path(oi_dir) if oi_dir else None
    con80_dir = Path(con80) if con80 else (oi / "CON80" if oi else None)
    srcdirs = ([Path(d) for d in src] if src
               else [oi / d for d in SOURCE_DIRS] if oi else None)
    mlib_dir = Path(mlib) if mlib else (oi / "MLIB80" if oi else None)
    incl_dir = Path(incl) if incl else (oi / "INCL80" if oi else None)
    deck_root_dir = Path(deck_root) if deck_root else oi
    if con80_dir is None or srcdirs is None:
        raise typer.BadParameter(
            "give --oi, or explicit --con80 and --src directories")
    out_root = Path(out) if out else Path(build) / "OI340600"
    objdir = out_root / "obj"
    gendir = out_root / "gen"

    srcidx = SourceIndex(srcdirs)
    by_type, runtime, unresolved, patches = resolve_worklist(
        con80_dir, root, srcidx, runtime_names(runlibs))

    print(f"[{root}] sources: {len(by_type['ASM'])} ASM, {len(by_type['HAL'])} HAL, "
          f"{len(by_type['DISPLAY'])} display, {len(patches)} patch; "
          f"{len(runtime)} runtime/library, {len(unresolved)} unresolved")
    if unresolved:
        print("  unresolved:", " ".join(sorted(unresolved)[:30]),
              "..." if len(unresolved) > 30 else "")

    linklibs = ([Path(d) for d in linklib] if linklib
                else [Path(build) / "lib" / "runtime" / "RUN",
                      Path(build) / "lib" / "runtime" / "ZCON"])

    if emit_cmake:
        if (by_type["ASM"] or patches) and mlib_dir is None:
            raise typer.BadParameter("--emit-cmake needs --mlib (or --oi): "
                                     "the worklist has assembly sources")
        from . import con80cmake
        con80cmake.emit(
            Path(emit_cmake), root=root, by_type=by_type, patches=patches,
            srcdirs=srcdirs, con80_dir=con80_dir, mlib=mlib_dir,
            incl80=incl_dir, deck_root=deck_root_dir, out_root=out_root,
            python=python, halsc=os.path.abspath(halsc),
            pass_rel32=pass_rel32.resolve(), tolerable=tolerable,
            linklibs=linklibs, concard_root=concard_root,
            allow_undefined=not no_undefined)
        print(f"wrote {emit_cmake}")

    if plan or (emit_cmake and not (run_assemble or run_hal
                                    or run_display or run_link)):
        return

    if run_assemble:
        if mlib_dir is None:
            raise typer.BadParameter("--assemble needs --mlib (or --oi)")
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
        ok, fails = hal(by_type["HAL"], objdir, gendir,
                        os.path.abspath(halsc), python,
                        pass_rel32.resolve(), srcdirs, incl_dir, verbose)
        print(f"compiled {ok}/{len(by_type['HAL'])} HAL -> {objdir}"
              + (f"; {len(fails)} failed" if fails else ""))
        if fails:
            print("  failed:", " ".join(sorted(fails)))

    if run_display:
        ok, fails = display(by_type["DISPLAY"], objdir, gendir,
                            os.path.abspath(halsc), python,
                            pass_rel32.resolve(), srcdirs, incl_dir,
                            deck_root_dir, verbose)
        print(f"built {ok}/{len(by_type['DISPLAY'])} displays -> {objdir}"
              + (f"; {len(fails)} failed" if fails else ""))
        if fails:
            print("  failed:", " ".join(sorted(fails)))

    if run_link:
        fcm = out_root / f"{root}.fcm"
        symjson = fcm.with_suffix(".sym.json")
        # Scope the link to this root's modules so a partial build (e.g. one
        # phase) doesn't pull other phases' csects out of the shared obj dir.
        objs = worklist_objects(objdir, by_type, patches)
        good = link(objdir, fcm, con80_dir, concard_root, linklibs,
                    python, json_symbols=symjson,
                    allow_undefined=not no_undefined, verbose=verbose,
                    objs=objs)
        print(f"linked {len(objs)} objects -> {fcm}" if good
              else "link FAILED (see above)")


if __name__ == "__main__":
    app()
