#!/usr/bin/env python3
#
# IBM System/360 hexadecimal floating point (AP-101 native float format).
# 
# Layout, as 8 groups of 8 bits each (single precision = the first 4 groups):
# 
#     SEEEEEEE FFFFFFFF FFFFFFFF ... FFFFFFFF
# 
# S is the sign, E the exponent, F the fraction.  The exponent is a power of
# 16 biased by 64 (spanning 16**-64 through 16**63); the fraction is an
# unsigned binary fraction whose leftmost bit is 1/2, the next 1/4, and so
# on.  Zero is encoded as all zeroes.  E.g. 0x42640000 0x00000000:
# 
#     S = 0 (positive)
#     Exponent = 16**(0x42-0x40) = 16**2 = 2**8
#     Fraction = 0.0110 0100 ...  ->  1100100 binary  =  100.0
# 
# TWO encoders live here because the flight-era tools genuinely differed:
# 
#   * toFloatIBM — the assembler's conversion (DC E'/D' constants, =E/=D
#     literals): ROUNDS HALF UP, clamps the exponent at underflow, and
#     returns the 0xFF000000 marker on overflow.
#   * ibm_double — the DFG's conversion (scaled KVT limit tables): the
#     fraction is TRUNCATED, never rounded.  Oracle-validated:
#     359.99 -> 4316 7FD7 0A3D 70A3, where rounding would end ...70A4.
# 
# Each encoder pins its Decimal precision in a LOCAL context — the
# assembler's historical getcontext().prec = 20 is contained here, so
# importing this module cannot perturb other Decimal math in the process
# (the DFG's validated results depend on the default precision, 28).
# 
# toFloatIBM/fromFloatIBM are by Ronald Burkey (from ASM101S), declared
# by their author to be in the U.S. Public Domain.
#
from decimal import Decimal, localcontext, ROUND_HALF_UP

_TWO_TO_56 = Decimal(2 ** 56)
_TWO_TO_52 = Decimal(2 ** 52)
_SIXTEEN = Decimal(16)


def hround(x):
    """Round a Decimal half-integers-UPWARD (unlike Python's banker's
    rounding); None if x is not a number."""
    try:
        return int(x.to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:
        return None


def toFloatIBM(x, scale=1):
    """A Python number (or its string representation — preferred, since a
    value already coerced to float may have lost digits) as an IBM
    double-precision float: the pair (msw, lsw) of 32-bit words, or
    (0xFF000000, 0) on overflow.  Rounds half up (the assembler's rule)."""
    with localcontext() as ctx:
        ctx.prec = 20
        d = Decimal(x) * Decimal(scale)
        if d == 0:
            return 0x00000000, 0x00000000
        if d < 0:                                # sign off, kept as a flag
            s, d = 1, -d
        else:
            s = 0
        # Scale so the 56-bit fraction lands in the integer range
        # [2**52, 2**56), tracking the excess-64 base-16 exponent.
        d *= _TWO_TO_56
        e = 64
        while d < _TWO_TO_52:
            e -= 1
            d *= _SIXTEEN
        while d >= _TWO_TO_56:
            e += 1
            d /= _SIXTEEN
        if e < 0:                                # clamp at underflow
            e = 0
        if e > 127:
            return 0xFF000000, 0x00000000
        f = hround(d)
        msw = (s << 31) | (e << 24) | (f >> 32)
        return msw, f & 0xFFFFFFFF


def fromFloatIBM(msw, lsw):
    """The Python float of an IBM double-precision float's two 32-bit
    words (inverse of toFloatIBM)."""
    s = (msw >> 31) & 1
    e = ((msw >> 24) & 0x7F) - 64
    f = ((msw & 0x00FFFFFF) << 32) | (lsw & 0xFFFFFFFF)
    x = f * (16 ** e) / (2 ** 56)
    return -x if s else x


def ibm_double(v):
    """A decimal value as an IBM double-precision float, as the 4 halfwords
    the DFG stores in its scaled limit tables.  The fraction is TRUNCATED
    (the DFG's rule — see the module docstring), and the result is
    normalized if the fraction landed at 2**56 exactly."""
    with localcontext() as ctx:
        ctx.prec = 28
        d = Decimal(str(v))
        if d == 0:
            return [0, 0, 0, 0]
        neg, f, e16 = d < 0, abs(d), 0
        while f >= 1:
            f /= 16; e16 += 1
        while f < Decimal(1) / 16:
            f *= 16; e16 -= 1
        e = 64 + e16
        frac = int((f * _TWO_TO_56).to_integral_value(rounding="ROUND_DOWN"))
        if frac >= (1 << 56):                    # carried into the exponent
            frac >>= 4; e += 1
        w = ((1 if neg else 0) << 63) | (e << 56) | frac
        return [(w >> 48) & 0xFFFF, (w >> 32) & 0xFFFF,
                (w >> 16) & 0xFFFF, w & 0xFFFF]
