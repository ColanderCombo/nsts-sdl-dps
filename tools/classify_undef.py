#!/usr/bin/env python3
"""Classify PHASE02's undefined symbols: where does each one actually live?

Definition sources indexed:
  1. Built objects (build/mmu/*/obj, build/phase02.out/obj, runtime libs):
     ESD SD/LD entries -> module.
  2. ASM source scan (SSSRC, APPLSRC, MLIB80): 'X CSECT/START', 'ENTRY a,b,c'
     labels (catches members we have not assembled).
  3. HAL source scan: 'EQUATE EXTERNAL <sym>' exports.
For each defining member, find which CON80 phase segment(s) reach it (INSERT
of any of the member's csects in OFTMP@N layout programs).
"""
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "src")
from ap101Utils import concard, objModule

ROOT = Path("code/OI340600")
S = Path(__file__).parent

# ---- 1. collect the undefined list from a fresh PHASE02 link ----
r = subprocess.run(
    ["build/venv/bin/python", "-m", "lnk101",
     *map(str, sorted(Path("build/mmu/PHASE02/obj").glob("*.obj"))),
     "-L", "build/lib/runtime/RUN", "-L", "build/lib/runtime/ZCON",
     "--concard", str(ROOT / "CON80"), "--concard-root", "OFTMP@2",
     "--map-lib", "1=build/mmu/PHASE01.lib",
     "-o", "/dev/null", "--no-repro", "--allow-undefined"],
    capture_output=True, text=True, env={"PYTHONUTF8": "1", "PATH": "/usr/bin:/bin"})
undefs = sorted(set(re.findall(r"Undefined (?:symbol|COMPOOL)[^:]*: ([A-Z0-9#@$]+),",
                               r.stdout + r.stderr)))
print(f"{len(undefs)} unique undefined symbols\n")

# ---- 2. index built objects ----
objdef = {}      # symbol -> module stem
for pat in ("build/mmu/*/obj/*.obj", "build/phase02.out/obj/*.obj",
            "build/lib/runtime/*/*.obj"):
    for p in Path(".").glob(pat):
        try:
            mods = objModule.ObjectFile(p).modules
        except Exception:
            continue
        for m in mods:
            for e in m.esdEntries:
                if e.typeName in ("SD", "LD"):
                    objdef.setdefault(e.name.strip(), p.stem)

# ---- 3. scan ASM sources for CSECT/ENTRY definitions ----
srcdef = {}      # symbol -> "dir/member"
label_re = re.compile(r"^([A-Z0-9#@$]{1,8})\s+(CSECT|START)\b")
entry_re = re.compile(r"^\S*\s+ENTRY\s+([A-Z0-9#@$, ]+)")
for d in ("SSSRC", "APPLSRC", "MLIB80"):
    dd = ROOT / d
    if not dd.is_dir():
        continue
    for f in dd.iterdir():
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            body = line[:71]
            if body[:1] in ("*", "."):
                continue
            m = label_re.match(body)
            if m:
                srcdef.setdefault(m.group(1), f"{d}/{f.name}")
            m = entry_re.match(body)
            if m:
                for sym in m.group(1).replace(",", " ").split():
                    srcdef.setdefault(sym, f"{d}/{f.name}")
        # HAL EQUATE EXTERNAL exports
        for m in re.finditer(r"EQUATE\s+EXTERNAL\s+([A-Z0-9_#@$]+)", text):
            srcdef.setdefault(m.group(1)[:8], f"{d}/{f.name}")

# ---- 4. which phase segments reach which INSERTed csects ----
deck = concard.ConcardDeck(ROOT / "CON80")
segs = concard.phase_segments(deck, "OFTMP")
phase_of_insert = defaultdict(set)   # csect name -> {phase ids}
for nums, _ in segs:
    if not nums:
        continue
    pid = nums[0]
    try:
        prog = concard.layout_program(deck, f"OFTMP@{pid}")
    except Exception:
        continue
    for op in prog:
        if op.verb == "INSERT":
            phase_of_insert[op.operand].add(pid)

def phases_for_member(stem):
    """Phases whose layout INSERTs any csect of the member (by obj ESD)."""
    hits = set()
    for pat in ("build/mmu/*/obj", "build/phase02.out/obj"):
        for p in Path(".").glob(f"{pat}/{stem}.obj"):
            for m in objModule.ObjectFile(p).modules:
                for e in m.esdEntries:
                    if e.typeName == "SD":
                        hits |= phase_of_insert.get(e.name.strip(), set())
    return hits

# ---- classify ----
buckets = defaultdict(list)
for sym in undefs:
    if sym in objdef:
        stem = objdef[sym]
        ph = phases_for_member(stem)
        tag = f"defined in {stem}.obj" + (f" (phases {sorted(ph)})" if ph
                                          else " (module in NO phase's INSERTs)")
        buckets[tag].append(sym)
    elif sym in srcdef:
        # direct member or via INSERT in some phase
        member = srcdef[sym]
        stem = member.split("/")[1]
        ph = phase_of_insert.get(sym, set()) | phase_of_insert.get(stem, set())
        buckets[f"source-only: {member}"
                + (f" (phases {sorted(ph)})" if ph else "")].append(sym)
    else:
        ph = phase_of_insert.get(sym, set())
        if ph:
            buckets[f"INSERTed as csect by phases {sorted(ph)} "
                    f"(no source found)"].append(sym)
        else:
            buckets["NOWHERE FOUND"].append(sym)

for tag in sorted(buckets, key=lambda t: -len(buckets[t])):
    syms = buckets[tag]
    print(f"[{len(syms):3d}] {tag}")
    print("      " + " ".join(syms[:10]) + (" ..." if len(syms) > 10 else ""))
