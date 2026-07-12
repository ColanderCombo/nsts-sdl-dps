#!/usr/bin/env python3

import json
import os
import re
from pathlib import Path
from typing import Annotated, Optional

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer


def extract_diffs(text):
    diffs = []
    current_section = None

    for line in text.splitlines():
        stripped = line.strip()

        # FAIL: #ZVXMMID @ 00140 (2 halfwords) -- 2 halfwords differ
        m = re.match(r'FAIL:\s+(\S+)\s+@', stripped)
        if m:
            current_section = m.group(1)
            continue

        if stripped.startswith('OK:') or stripped.startswith('SKIP:'):
            current_section = None
            continue

        # @ 00140 8000 vs 827C  ; commend
        m = re.match(
            r'@\s+([0-9A-Fa-f]{5})\s+([0-9A-Fa-f]{4})\s+vs\s+([0-9A-Fa-f]{4})',
            stripped,
        )
        if m and current_section:
            diffs.append({
                "section": current_section,
                "address": m.group(1).upper(),
                "hw_a": m.group(2).upper(),
                "hw_b": m.group(3).upper(),
            })

    return diffs


app = typer.Typer(
    help="Extract the per-halfword FAIL diffs from an fcmcmp log as JSON.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.command()
def main(
    log_file: Annotated[Path, typer.Argument(help="fcmcmp output log")],
    output_json: Annotated[Optional[Path], typer.Argument(
        help="output path (default: LOG_FILE with .diffs.json suffix)")] = None,
) -> None:
    out_path = output_json if output_json else log_file.with_suffix('.diffs.json')

    text = log_file.read_text()
    diffs = extract_diffs(text)

    out_path.write_text(json.dumps({"diffs": diffs}, indent=2) + "\n")
    print(f"Extracted {len(diffs)} diffs to {out_path}")


if __name__ == "__main__":
    app()
