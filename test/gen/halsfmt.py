"""
halsfmt — HAL/S WRITE(6) output formatting for test baselines.

Reproduces the formatting produced by the HAL/S runtime (ETOC / halUCP)
when executing WRITE(6) statements:

  Scalars:   sign(1) + d.ddddddE±dd(12) = 13 chars; zero = " 0.0" + 9 spaces
  Integers:  right-justified in 6 chars
  Vectors:   MMWSNP row (newline + 1 space indent), then elements
  Matrices:  one row per line, same MMWSNP formatting
  Strings:   literal text, trailing space
  Each formatted element gets a trailing space.
"""

import sys
import numpy as np


def fmt_scalar(v):
    """Format a scalar in HAL/S SP format: 13 chars.
    sign(1) + d.ddddddE±dd(12) = 13; zero = ' 0.0' + 9 spaces."""
    if v == 0.0:
        return " 0.0" + " " * 9  # 13 chars
    sign = "-" if v < 0 else " "
    av = abs(float(v))
    exp = int(np.floor(np.log10(av)))
    mantissa = av / 10.0**exp
    # Guard: .6f rounding can push 9.999... to "10.000000"
    if mantissa >= 10.0 or float(f"{mantissa:.6f}") >= 10.0:
        mantissa /= 10.0
        exp += 1
    elif mantissa < 1.0:
        mantissa *= 10.0
        exp -= 1
    mstr = f"{mantissa:.6f}"
    exp_sign = "+" if exp >= 0 else "-"
    estr = f"E{exp_sign}{abs(exp):02d}"
    return sign + mstr + estr  # 13 chars


def fmt_integer(v):
    """Format an integer right-justified in 6 chars."""
    return f"{int(v):6d}"


def write6(*items):
    """
    IOINIT emits a newline (terminates previous line / starts new output).
    Every output item (numeric or string) gets a trailing space.
    Vectors/matrices use MMWSNP which emits SKIP(1)+COLUMN(1) per row.
    """
    out = "\n"  # IOINIT: terminates previous line
    for item in items:
        if isinstance(item, str):
            out += item + " "
        elif isinstance(item, np.ndarray):
            if item.ndim == 1:
                # Vector: SKIP(1) + COLUMN(1) then elements
                out += "\n "
                for x in item:
                    out += fmt_scalar(x) + " "
            elif item.ndim == 2:
                # Matrix: one row per line
                for row in item:
                    out += "\n "
                    for x in row:
                        out += fmt_scalar(x) + " "
        elif isinstance(item, (int, np.integer)):
            out += fmt_integer(item) + " "
        else:
            # scalar float
            out += fmt_scalar(float(item)) + " "
    return out


def save(output, argv=None):
    if argv is None:
        argv = sys.argv
    if len(argv) > 1:
        with open(argv[1], "w") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
