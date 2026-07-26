"""
Apply the all.txt-derived literal values to tools/halfp/data/variable_values.json.

For each sidecar entry whose name has a clean single-value declaration in
testCases/vagc#1296/all.txt:
  - If the declaration is a numeric literal, replace the sidecar value
    with dp_from_string(literal).
  - If it's an expression (e.g., '1/CGIK_DEG_RAD'), evaluate it via
    halsexpr against the current sidecar.

The sidecar's documentation keys (those starting with _) are preserved.
A new `_alltxt_provenance` key records when the values were last
refreshed.

Run from repo root after reviewing the proposed diffs from diff_alltxt.py:
    PYTHONPATH=. python3 tools/halfp/apply_alltxt.py
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.halsexpr import (
    evaluate as eval_halexpr, EvalError,
)
from tools.halfp.halfp_runner import HalfpDriver

ALLTXT_PATH = REPO_ROOT / "testCases/vagc#1296/all.txt"
SIDECAR_PATH = REPO_ROOT / "tools/halfp/data/variable_values.json"
DRIVER_PATH = REPO_ROOT / "build/test/halfp_driver"

LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s+`([^`]*)`\s*$')
NUM_RE  = re.compile(r'^[+\-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+\-]?\d+)?$')
EXPR_OP_RE = re.compile(r'[+\-*/()A-Za-z_]')


class _D:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def _scan_referenced_names(dataset_path):
    """Collect every identifier referenced by a source_literal in
    literalSCALARS-exp2.txt.  Used to decide which all.txt entries to
    add to the sidecar (we don't want to bloat with thousands of
    irrelevant names)."""
    from tools.halfp.parse_literal_scalars import parse_file
    from tools.halfp.halsexpr import collect_vars
    refs = set()
    for v in parse_file(str(dataset_path)):
        if v.source_literal:
            try:
                refs.update(collect_vars(v.source_literal))
            except EvalError:
                pass
    return refs


def main():
    sidecar = json.loads(SIDECAR_PATH.read_text())
    sidecar_keys = {k for k in sidecar if not k.startswith("_")}

    # Parse all.txt clean lines.
    all_decls = {}
    for ln in ALLTXT_PATH.read_text().splitlines():
        m = LINE_RE.match(ln)
        if m:
            all_decls[m.group(1)] = m.group(2).strip()

    # Set of names referenced by any HAL/S source literal in the
    # dataset.  We'll add new sidecar entries only for those names —
    # adding all 13k names from all.txt would bloat the sidecar
    # uselessly.
    dataset = REPO_ROOT / "testCases/vagc#1296/literalSCALARS-exp2.txt"
    referenced = _scan_referenced_names(dataset)

    # Names to consider:
    # - existing sidecar entries that have an all.txt declaration (update)
    # - names referenced by the dataset that are in all.txt but missing
    #   from sidecar (add)
    overlap_existing = sidecar_keys & all_decls.keys()
    new_to_add = (referenced & all_decls.keys()) - sidecar_keys

    updated  = []   # (name, literal, old, new)
    added    = []   # (name, literal, new)
    skipped  = []   # (name, literal, reason)

    def _convert(literal, drv, wrap):
        if NUM_RE.match(literal):
            return drv.dp_from_string(literal).upper(), None
        if EXPR_OP_RE.search(literal):
            try:
                return eval_halexpr(wrap, sidecar, literal).upper(), None
            except EvalError as e:
                return None, f"eval failed: {e}"
        return None, "not numeric or expression"

    from tools.halfp.halsexpr import collect_vars

    with HalfpDriver(str(DRIVER_PATH)) as drv:
        wrap = _D(drv)
        # 1) Refresh existing sidecar entries against all.txt.
        for name in sorted(overlap_existing):
            literal = all_decls[name]
            old = sidecar[name].lower()
            new, err = _convert(literal, drv, wrap)
            if err is not None:
                skipped.append((name, literal, err))
                continue
            if new.lower() == old:
                continue
            updated.append((name, literal, old.upper(), new))
            sidecar[name] = new

        # 2) Fixed-point pass to add new entries.  Expressions in all.txt
        # often reference *other* all.txt names that aren't yet in the
        # sidecar; resolving them recursively (until no progress) closes
        # the chain.  We seed with names directly referenced by the
        # dataset and grow the work-set as we discover dependencies.
        todo = set(new_to_add)
        deferred = []         # (name, literal, last_error)
        last_size = -1
        iteration = 0
        while todo:
            iteration += 1
            new_todo = set()
            progress = False
            still_blocked = []
            for name in sorted(todo):
                if name in sidecar:
                    continue
                literal = all_decls[name]
                new, err = _convert(literal, drv, wrap)
                if err is not None:
                    # Track which dependencies are missing so next iter
                    # can pick them up.
                    try:
                        deps = collect_vars(literal)
                    except EvalError:
                        deps = set()
                    for d in deps:
                        if d not in sidecar and d in all_decls:
                            new_todo.add(d)
                    still_blocked.append((name, literal, err))
                    continue
                added.append((name, literal, new))
                sidecar[name] = new
                progress = True
            # Keep retrying anything that didn't resolve, plus newly
            # discovered dependencies.
            todo = {name for (name, _, _) in still_blocked} | new_todo
            todo -= set(sidecar.keys())   # don't reprocess
            if not progress:
                # Even if there are still unresolved entries, we can't
                # make progress without more info — break.
                deferred = still_blocked
                break
            if len(todo) == last_size:
                deferred = still_blocked
                break
            last_size = len(todo)

        for name, literal, err in deferred:
            skipped.append((name, literal, err))

    # Record provenance.
    sidecar["_alltxt_provenance"] = (
        f"Values for the names below were derived on {date.today().isoformat()} "
        f"from testCases/vagc#1296/all.txt by parsing the declared literal "
        f"through ibm_dp_from_string (or evaluating via halsexpr if it's an "
        f"expression like '1/CGIK_DEG_RAD').  See tools/halfp/apply_alltxt.py "
        f"and tools/halfp/diff_alltxt.py.  Re-run apply_alltxt.py if all.txt "
        f"changes."
    )

    SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2) + "\n")

    print(f"Updated: {len(updated)}, Added: {len(added)}, Skipped: {len(skipped)}")
    print()
    if updated:
        print(f"--- Updates (existing sidecar entries refreshed) ---")
        for name, literal, old, new in updated:
            delta = int(new, 16) - int(old, 16)
            print(f"  {name:<28}  {literal:<28}  {old} -> {new}  (Δ={delta:+d})")
    if added:
        print()
        print(f"--- Added ({len(added)} new entries referenced by the dataset) ---")
        for name, literal, new in added[:30]:
            print(f"  {name:<28}  {literal:<28}  -> {new}")
        if len(added) > 30:
            print(f"  ... and {len(added) - 30} more")
    if skipped:
        print()
        print(f"Skipped ({len(skipped)} — couldn't resolve in fixed-point pass):")
        for name, lit, why in skipped[:20]:
            print(f"  {name:<28}  {lit!r:<60}  {why}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")


if __name__ == "__main__":
    main()
