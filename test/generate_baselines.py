#!/usr/bin/env python3
"""Generate baseline output files for HAL/S test programs.

Runs each .fcm through gpc-batch and saves output as .expected.out6
baselines for regression testing.
"""

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

# Project layout (relative to this script)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FCM_DIR      = PROJECT_ROOT / "build" / "fcm"
TEST_DIR     = PROJECT_ROOT / "test"
BASELINE_DIR = TEST_DIR / "baselines"
GPC_BATCH    = PROJECT_ROOT / "ext" / "sim" / "dist" / "gpc-batch.js"

app = typer.Typer(add_completion=False)


def run_program(fcm: Path, outfile: Path, infile5: Path | None,
                max_steps: int) -> tuple[int, str]:
    """Run gpc-batch on an FCM, return (exit_code, stderr_text)."""
    args = [
        "node", str(GPC_BATCH), str(fcm),
        "--no-trace", "--max-steps", str(max_steps),
        f"--outfile6={outfile}",
    ]
    if infile5 and infile5.exists():
        args.append(f"--infile5={infile5}")

    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stderr


def stop_reason(stderr: str) -> str:
    """Pull the stop-reason line from gpc-batch stderr."""
    for line in stderr.splitlines():
        if "STOPPED" in line or "FATAL" in line:
            return line.strip()
    return ""


@app.command()
def generate(
    only: Annotated[Optional[list[str]], typer.Argument(help="Only these program names")] = None,
    max_steps: Annotated[int, typer.Option(help="Max simulation steps per program")] = 100_000,
    update: Annotated[bool, typer.Option("--update", help="Overwrite existing baselines")] = False,
):
    if not GPC_BATCH.exists():
        typer.echo(f"gpc-batch not built: {GPC_BATCH}\n"
                   "  Run: cd ext/sim && npm run batch:build", err=True)
        raise typer.Exit(1)

    BASELINE_DIR.mkdir(exist_ok=True)

    fcm_files = sorted(FCM_DIR.glob("*.fcm"))
    if not fcm_files:
        typer.echo(f"No .fcm files in {FCM_DIR}", err=True)
        raise typer.Exit(1)

    if only:
        names = set(only)
        fcm_files = [f for f in fcm_files if f.stem in names]

    print(f"Generating baselines for {len(fcm_files)} programs")
    print(f"  FCM dir:    {FCM_DIR}")
    print(f"  Baselines:  {BASELINE_DIR}")
    print(f"  Max steps:  {max_steps}")
    print()

    ok = ok_empty = fail_input = fail_other = 0

    for fcm in fcm_files:
        name = fcm.stem
        baseline = BASELINE_DIR / f"{name}.expected.out6"
        status_file = BASELINE_DIR / f"{name}.status"

        if baseline.exists() and not update:
            print(f"  {'SKIP':20s} {name} (baseline exists)")
            continue

        infile5 = TEST_DIR / f"{name}.in5"
        if not infile5.exists():
            infile5 = None

        tmp_out = BASELINE_DIR / f"{name}.tmp.out6"

        try:
            rc, stderr = run_program(fcm, tmp_out, infile5, max_steps)
        except subprocess.TimeoutExpired:
            rc, stderr = -1, "TIMEOUT after 120s"

        out_lines = sum(1 for _ in tmp_out.open()) if tmp_out.exists() else 0

        status_file.write_text(
            f"exit={rc}\nlines={out_lines}\n"
            f"reason={stop_reason(stderr)}\n"
            f"input={'yes' if infile5 else 'no'}\n"
        )

        if rc == 0:
            if tmp_out.exists():
                tmp_out.rename(baseline)
            else:
                baseline.write_text("")
            if out_lines > 0:
                ok += 1; tag = "OK"
            else:
                ok_empty += 1; tag = "OK (no output)"
        elif "input" in stderr.lower() or "infile5" in stderr.lower():
            fail_input += 1; tag = "NEED INPUT"
            tmp_out.unlink(missing_ok=True)
        else:
            fail_other += 1; tag = f"FAIL (rc={rc})"
            tmp_out.unlink(missing_ok=True)

        input_note = " [has .in5]" if infile5 else ""
        print(f"  {tag:20s} {name}{input_note}  ({out_lines} lines)")

    print(f"\n=== Summary ===")
    print(f"  With output:    {ok}")
    print(f"  No output:      {ok_empty}")
    print(f"  Need input:     {fail_input}")
    print(f"  Other failures: {fail_other}")


if __name__ == "__main__":
    app()
