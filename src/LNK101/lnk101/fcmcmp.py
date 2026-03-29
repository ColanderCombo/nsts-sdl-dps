#!/usr/bin/env python3
#
# Compare two FCM images section-by-section.
#

import json
import sys
from pathlib import Path

# data created by the linker, not from real .obj files
_SYNTHETIC_MODULES = {'<external-syms>', '<ext-syms>', '<generated-stacks>',
                      '<stacks>', '<defined-symbols>', '<defined>'}


def load_sections(sym_json_path, modules=None, include_all=False):
    with open(sym_json_path) as f:
        data = json.load(f)

    if modules is None and not include_all:
        # Auto-detect: include modules whose filename doesn't start with '<'
        modules = set()
        for m in data.get("modules", []):
            fn = m.get("filename", "")
            if not fn.startswith("<"):
                modules.add(m["name"])

    sections = []
    for s in data["sections"]:
        if modules and s["module"] not in modules:
            continue
        byte_addr = s["address"] * 2
        byte_len = s["size"] * 2
        sections.append((s["name"], byte_addr, byte_len))
    sections.sort(key=lambda t: t[1])
    return sections


def compare(sections, image_a, image_b):
    failures = 0
    checked = 0
    for name, offset, length in sections:
        if offset + length > len(image_a):
            print(f"  SKIP: {name} @ 0x{offset:06X} ({length} bytes) — "
                  f"beyond end of first image ({len(image_a)} bytes)")
            continue
        if offset + length > len(image_b):
            print(f"  SKIP: {name} @ 0x{offset:06X} ({length} bytes) — "
                  f"beyond end of second image ({len(image_b)} bytes)")
            continue

        a = image_a[offset : offset + length]
        b = image_b[offset : offset + length]
        checked += 1

        if a == b:
            print(f"  OK:   {name} @ 0x{offset:06X} ({length} bytes)")
        else:
            diffs = sum(1 for x, y in zip(a, b) if x != y)
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    print(f"  FAIL: {name} @ 0x{offset:06X} ({length} bytes) — "
                          f"{diffs} byte(s) differ, first at +{i}: "
                          f"0x{x:02X} vs 0x{y:02X}")
                    break
            failures += 1

    return checked, failures


def main():
    args = sys.argv[1:]
    modules = set()
    include_all = False

    while args and args[0].startswith("--"):
        if args[0] == "--module" and len(args) >= 2:
            modules.add(args[1])
            args = args[2:]
        elif args[0] == "--all":
            include_all = True
            args = args[1:]
        else:
            break

    if len(args) != 3:
        print(f"Usage: {Path(sys.argv[0]).name} [--module MOD ...] [--all] "
              f"<sym.json> <fcm_a> <fcm_b>", file=sys.stderr)
        sys.exit(2)

    sym_path, fcm_a_path, fcm_b_path = args

    sections = load_sections(sym_path,
                             modules=modules or None,
                             include_all=include_all)

    with open(fcm_a_path, "rb") as f:
        image_a = f.read()
    with open(fcm_b_path, "rb") as f:
        image_b = f.read()

    if len(image_a) != len(image_b):
        print(f"Note: images differ in size ({len(image_a)} vs {len(image_b)} bytes)")

    checked, failures = compare(sections, image_a, image_b)

    if failures:
        print(f"\nFAIL: {failures}/{checked} section(s) differ")
        sys.exit(1)
    else:
        print(f"\nPASS: all {checked} sections match")


if __name__ == "__main__":
    main()
