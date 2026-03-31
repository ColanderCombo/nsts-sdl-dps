#!/usr/bin/env python3
"""Tests for fcmcmp — FCM image comparison tool.

Quick manual check (run from repo root to see formatted output):

  fcmcmp test/fcmcmp/test_simple_do.sym.json test/fcmcmp/test_simple_do.fcm test/fcmcmp/test_simple_do.fcm
  fcmcmp test/fcmcmp/test_simple_do.sym.json test/fcmcmp/test_simple_do.fcm test/fcmcmp/test_simple_do.unrelocated.fcm
"""

import subprocess
import sys
from pathlib import Path

TESTDIR = Path(__file__).parent
SYM_JSON = TESTDIR / "test_simple_do.sym.json"
FCM = TESTDIR / "test_simple_do.fcm"
FCM_UNRELOCATED = TESTDIR / "test_simple_do.unrelocated.fcm"


def run_fcmcmp(*args):
    result = subprocess.run(
        [sys.executable, "-m", "lnk101.fcmcmp", *[str(a) for a in args]],
        capture_output=True, text=True,
    )
    return result


def test_identical_images_pass():
    r = run_fcmcmp(SYM_JSON, FCM, FCM)
    assert r.returncode == 0
    assert "PASS" in r.stdout
    assert "16 sections match" in r.stdout


def test_unrelocated_images_fail():
    r = run_fcmcmp(SYM_JSON, FCM, FCM_UNRELOCATED)
    assert r.returncode == 1
    assert "FAIL:" in r.stdout
    assert "12/16 section(s) differ" in r.stdout


def test_output_uses_hex_halfwords():
    r = run_fcmcmp(SYM_JSON, FCM, FCM_UNRELOCATED)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Note") or line.startswith(("FAIL:", "PASS")):
            continue
        assert "0x" not in line, f"Found '0x' in: {line}"
        assert "bytes" not in line, f"Found 'bytes' in: {line}"
        assert "halfwords" in line or "vs" in line or "..." in line, \
            f"Missing 'halfwords' or 'vs' in: {line}"


def test_addresses_are_5_hex_digits():
    """Addresses after '@' should be 5 uppercase hex digits."""
    r = run_fcmcmp(SYM_JSON, FCM, FCM)
    import re
    for line in r.stdout.splitlines():
        for m in re.finditer(r'@ (\S+)', line):
            addr = m.group(1)
            assert re.fullmatch(r'[0-9A-F]{5}', addr), \
                f"Bad address format '{addr}' in: {line}"


def test_at_signs_aligned():
    """All '@' should be in the same column."""
    r = run_fcmcmp(SYM_JSON, FCM, FCM)
    cols = set()
    for line in r.stdout.splitlines():
        idx = line.find('@')
        if idx >= 0:
            cols.add(idx)
    assert len(cols) == 1, f"'@' in multiple columns: {cols}"


def test_ok_sections_in_unrelocated():
    """Some sections have no relocations and should still match."""
    r = run_fcmcmp(SYM_JSON, FCM, FCM_UNRELOCATED)
    ok_lines = [l for l in r.stdout.splitlines() if l.strip().startswith("OK:")]
    assert len(ok_lines) == 4  # #DTESTSI, #LCASPV, #LIOINIT, CASV


def test_diff_halfwords_shown():
    """Differing sections should list individual halfword diffs."""
    r = run_fcmcmp(SYM_JSON, FCM, FCM_UNRELOCATED)
    vs_lines = [l for l in r.stdout.splitlines() if " vs " in l]
    assert len(vs_lines) > 0


def test_max_hw_diffs():
    """--max-hw-diffs limits per-section output."""
    r = run_fcmcmp("--max-hw-diffs", "2", SYM_JSON, FCM, FCM_UNRELOCATED)
    assert "... and" in r.stdout


def test_module_filter():
    r = run_fcmcmp("--module", "IOINIT", SYM_JSON, FCM, FCM_UNRELOCATED)
    section_lines = [l.strip() for l in r.stdout.splitlines()
                     if l.strip().startswith(("OK:", "FAIL:", "SKIP:"))
                     and "@" in l]
    assert len(section_lines) > 0
    for l in section_lines:
        assert "IOINIT" in l or "IOCODE" in l or "IOBUF" in l, f"Unexpected: {l}"


def test_no_args_shows_help():
    r = run_fcmcmp()
    combined = r.stdout + r.stderr
    assert "Usage" in combined


def test_missing_file_no_traceback():
    r = run_fcmcmp("nonexistent.json", "a.fcm", "b.fcm")
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "does not exist" in r.stderr


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
