#!/usr/bin/env python3

import json
import re
import sys
from pathlib import Path


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


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} LOG_FILE [OUTPUT_JSON]", file=sys.stderr)
        sys.exit(1)

    log_path = Path(sys.argv[1])
    out_path = (
        Path(sys.argv[2]) if len(sys.argv) > 2
        else log_path.with_suffix('.diffs.json')
    )

    text = log_path.read_text()
    diffs = extract_diffs(text)

    out_path.write_text(json.dumps({"diffs": diffs}, indent=2) + "\n")
    print(f"Extracted {len(diffs)} diffs to {out_path}")


if __name__ == "__main__":
    main()
