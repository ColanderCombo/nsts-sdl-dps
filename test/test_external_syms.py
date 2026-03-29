#!/usr/bin/env python3
# 
# Test --external-syms: single-module relocation matches full link.
# 
# For a given program that was fully linked (with all libraries), this test:
#   1. Builds an external-syms JSON from the full link's .sym.json
#   2. Builds a reduced .lnk config with only the main module's section addresses
#   3. Runs lnk101 on just the main .obj with --external-syms + --load-config
#   4. Compares section bytes between the full FCM and single-module FCM
# 
# Usage:
#     python test_external_syms.py <program_name> <build_dir> <venv_python>
# 

import json
import os
import subprocess
import sys
import tempfile
import yaml
from pathlib import Path


def build_external_syms(sym_json_path, main_module_name):
    with open(sym_json_path) as f:
        data = json.load(f)

    skip_modules = {main_module_name, f"{main_module_name}#2"}

    ext_syms = {}
    for section in data["sections"]:
        if section["module"] in skip_modules:
            continue
        name = section["name"]
        hw_addr = section["address"]
        hw_size = section["size"]
        ext_syms[name] = {
            "start": hw_addr,
            "end": hw_addr + hw_size - 1,
        }

    # Add LD entry symbols (labels inside library sections)
    for sym in data.get("symbols", []):
        if sym.get("type") != "entry":
            continue
        if sym.get("module") in skip_modules:
            continue
        name = sym["name"]
        if name not in ext_syms:
            hw_addr = sym["address"]
            ext_syms[name] = {
                "start": hw_addr,
                "end": hw_addr,
            }

    return ext_syms


def build_reduced_lnk(lnk_path, main_module_name):
    with open(lnk_path) as f:
        config = yaml.safe_load(f)

    reduced = {
        "version": config["version"],
        "imageBase": config["imageBase"],
    }

    keep_modules = {main_module_name, f"{main_module_name}#2"}
    reduced["sections"] = [
        s for s in config.get("sections", [])
        if s.get("module") in keep_modules
    ]

    return reduced


def extract_sections(fcm_path, lnk_path, main_module_name):
    with open(lnk_path) as f:
        config = yaml.safe_load(f)

    image_base = int(config["imageBase"], 16)

    with open(fcm_path, "rb") as f:
        image = f.read()

    keep_modules = {main_module_name, f"{main_module_name}#2"}
    sections = {}
    for s in config.get("sections", []):
        if s.get("module") not in keep_modules:
            continue
        addr = int(s["address"], 16)
        length = s["length"]
        offset = addr - image_base
        sections[s["name"]] = image[offset : offset + length]

    return sections


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <program_name> <build_dir> <venv_python>",
              file=sys.stderr)
        sys.exit(2)

    program = sys.argv[1]
    build_dir = Path(sys.argv[2])
    venv_python = sys.argv[3]

    full_fcm = build_dir / "fcm" / f"{program}.fcm"
    full_lnk = build_dir / "fcm" / f"{program}.lnk"
    full_sym = build_dir / "fcm" / f"{program}.sym.json"

    for p in (full_fcm, full_lnk, full_sym):
        if not p.exists():
            print(f"SKIP: {p} not found (build incomplete?)", file=sys.stderr)
            sys.exit(0)

    main_obj = None
    for subdir in ("bench", "progHal"):
        candidate = build_dir / subdir / f"{program}.obj"
        if candidate.exists():
            main_obj = candidate
            break
    if main_obj is None:
        print(f"SKIP: {program}.obj not found", file=sys.stderr)
        sys.exit(0)

    main_module = main_obj.stem

    with tempfile.TemporaryDirectory(prefix="extsym_test_") as tmpdir:
        tmpdir = Path(tmpdir)

        ext_syms = build_external_syms(full_sym, main_module)
        ext_syms_path = tmpdir / "external_syms.json"
        with open(ext_syms_path, "w") as f:
            json.dump(ext_syms, f)

        reduced_lnk = build_reduced_lnk(full_lnk, main_module)
        reduced_lnk_path = tmpdir / "reduced.lnk"
        with open(reduced_lnk_path, "w") as f:
            yaml.dump(reduced_lnk, f)

        single_fcm = tmpdir / f"{program}.fcm"
        cmd = [
            venv_python, "-m", "lnk101",
            str(main_obj),
            "-o", str(single_fcm),
            "--external-syms", str(ext_syms_path),
            "--load-config", str(reduced_lnk_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"FAIL: single-module link failed:\n{result.stderr}",
                  file=sys.stderr)
            sys.exit(1)

        full_sections = extract_sections(full_fcm, full_lnk, main_module)
        single_sections = extract_sections(
            single_fcm, reduced_lnk_path, main_module)

        failures = 0
        for name in sorted(full_sections):
            if name not in single_sections:
                print(f"FAIL: section {name} missing from single-module link")
                failures += 1
                continue

            full_data = full_sections[name]
            single_data = single_sections[name]

            if full_data != single_data:
                for i, (a, b) in enumerate(zip(full_data, single_data)):
                    if a != b:
                        print(f"FAIL: {name} differs at byte offset {i}: "
                              f"full=0x{a:02X} single=0x{b:02X}")
                        break
                failures += 1
            else:
                print(f"  OK: {name} ({len(full_data)} bytes)")

        if failures:
            print(f"\n{failures} section(s) differ", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"\nPASS: all {len(full_sections)} sections match")


if __name__ == "__main__":
    main()
