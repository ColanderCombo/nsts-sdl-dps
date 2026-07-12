#!/usr/bin/env python3
"""
Proposed fix for the float-literal conversion bug (virtualagc #1296).

Strategy:
  1. Take the literal as a STRING (not a Python float / C double) so we
     don't pre-round it via IEEE-754.
  2. Use arbitrary-precision Decimal arithmetic to scale into IBM DP
     normalized range [2^52, 2^56).
  3. TRUNCATE (round-toward-zero) the mantissa to a 56-bit integer —
     matching the S/360 hex floating-point hardware that the original
     IBM HAL/S-FC compiler used inside XXXTOD.bal.

Verifies against the case from virtualagc issue #1295/#1296:
  1.0E-8 -> 0x3A2AF31D_C4611873 (was 0x3A2AF31D_C4611874).
"""
from decimal import Decimal, getcontext, ROUND_DOWN

getcontext().prec = 60

twoTo56 = Decimal(2 ** 56)
twoTo52 = Decimal(2 ** 52)


def stringToFloatIBM(s: str) -> tuple[int, int]:
    """Convert a decimal literal string to IBM double-precision hex float,
    matching the truncating behavior of S/360 hex FP used by the original
    HAL/S-FC compiler (see MONITOR.ASM/XXXTOD.bal)."""
    d = Decimal(s)
    if d == 0:
        return 0, 0
    sign = 0
    if d < 0:
        sign, d = 1, -d
    d *= twoTo56
    e = 64
    while d < twoTo52:
        e -= 1
        d *= 16
    while d >= twoTo56:
        e += 1
        d /= 16
    if e < 0:
        e = 0
    if e > 127:
        return 0xFF000000, 0x00000000
    f = int(d.to_integral_value(rounding=ROUND_DOWN))   # <-- truncate
    msw = ((sign << 31) | (e << 24) | (f >> 32)) & 0xFFFFFFFF
    lsw = f & 0xFFFFFFFF
    return msw, lsw


# Self-check: the issue-ticket case + a few discriminators.
EXPECTED = {
    "1.0E-8":              (0x3A2AF31D, 0xC4611873),  # from issue #1295
    "0.1":                 (0x40199999, 0x99999999),
    "1.0":                 (0x41100000, 0x00000000),
    "3.14159":             (0x413243F3, 0xE0370CDC),
    "1E-9":                (0x3944B82F, 0xA09B5A52),
    "9.81":                (0x419CF5C2, 0x8F5C28F5),
}

if __name__ == "__main__":
    fails = 0
    for lit, (em, el) in EXPECTED.items():
        gm, gl = stringToFloatIBM(lit)
        ok = (gm, gl) == (em, el)
        fails += 0 if ok else 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}]  {lit:<14}  -> {gm:08X}_{gl:08X}  (expected {em:08X}_{el:08X})")
    print()
    print("ALL PASS" if fails == 0 else f"{fails} FAILED")
