/*
 * License:     The author (Donald Schmidt) declares that this program
 *              is in the Public Domain (U.S. law) and may be used or
 *              modified for any purpose whatever without licensing.
 * Filename:    tools/floatIBM/ibmFloat.c
 * Purpose:     IBM hex floating-point arithmetic and conversions —
 *              AP-101S reference implementation for grading the
 *              simulator's ext/sim/gpc/floatIBM.coffee.  See the header
 *              for the exception-aware API contract.
 *
 *              Forked from ext/virtualagc/XCOM-I/ibmFloat.c.  The
 *              legacy ibm_dp_{add,sub,mul,div,addsub} entry points are
 *              preserved verbatim for the string converters that
 *              follow them; the new ibm_*_exc / ibm_sp_* / ibm_cvf*
 *              entry points implement the full AP-101S §8 semantics.
 *
 *              Based on the floating-point logic in the Hyperion/
 *              Hercules IBM 390 & z/Series emulator:
 *                https://github.com/hercules-390/hyperion/blob/master/float.c
 *
 * Reference:   http://www.ibibio.org/apollo/Shuttle.html
 *              IBM-85-C67-001 (AP-101S Principles of Operation), §8
 */

#include <assert.h>
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include "ibmFloat.h"

#define twoTo56 (1LL << IBM_DP_MANT_BITS)
#define twoTo52 (1LL << 52)

static uint64_t ibm_dp_from_uint(uint64_t v);

// Convert a C double to IBM double-precision float, returning the (msw, lsw)
// pair.  On overflow returns (IBM_DP_OVERFLOW_MSW, 0). 
// NOTE: IBM float has 56 bit mantissa, IEEE754 double is 53 bits,
//       so this loses precision.
void __attribute__ ((no_instrument_function))
ibm_dp_from_double(uint32_t *msw, uint32_t *lsw, double d) {
    assert(msw != NULL && lsw != NULL);
    int s; // The sign: 1 if negative, 0 if non-negative.
    int e; // Exponent.
    uint64_t f;
    if (d == 0) {
        *msw = 0x00000000;
        *lsw = 0x00000000;
        return;
    }
    // Make x positive but preserve the sign as a bit flag.
    if (d < 0) {
        s = 1;
        d = -d;
    } else
        s = 0;
    // Shift left by 24 bits.
    d *= twoTo56;
    // Find the exponent (biased by IBM_DP_EXP_BIAS) as a power of 16:
    e = IBM_DP_EXP_BIAS;
    while (d < twoTo52) {
        e -= 1;
        d *= 16;
    }
    while (d >= twoTo56) {
        e += 1;
        d /= 16;
    }
    if (e < 0)
        e = 0;
    if (e > IBM_DP_EXP_MAX) {
        *msw = IBM_DP_OVERFLOW_MSW;
        *lsw = 0x00000000;
        return;
    }
    f = llround(d);
    *msw = (s << 31) | (e << 24) | ((f >> 32) & 0xffffffff);
    *lsw = f & 0xffffffff;
}


double __attribute__ ((no_instrument_function))
ibm_dp_to_double(uint32_t msw, uint32_t lsw) {
    int s; // sign
    int e; // exponent
    long long int f;
    double x;
    s = (msw >> 31) & 1;
    e = ((msw >> 24) & IBM_DP_EXP_MAX) - IBM_DP_EXP_BIAS;
    f = ((msw & 0x00ffffffLL) << 32) | (lsw & 0xffffffff);
    x = f * pow(16, e) / twoTo56;
    if (s != 0)
        x = -x;
    return x;
}


// Convert a small unsigned integer to IBM DP packed format.  
static uint64_t
ibm_dp_from_uint(uint64_t v) {
    if (v == 0) return 0;
    int exp = IBM_DP_EXP_BIAS + IBM_DP_MANT_HEXDIGITS;
    while (v >= ((uint64_t)1 << IBM_DP_MANT_BITS)) { v >>= 4; exp += 1; }
    IBM_DP_RENORMALIZE_56(v, exp);
    if (exp < 0) return 0;
    if (exp > IBM_DP_EXP_MAX) return IBM_DP_OVERFLOW_PACKED;
    return IBM_DP_PACK(0, exp, v);
}


// ibm_dp_from_string():
// convert a HAL/S decimal-literal string to IBM DP hex.  Faithful
// reproduction of MONITOR.ASM/XXXTOD.bal — the routine the HAL/S-FC
// compiler invokes via MONITOR(10) at PASS1.PROCS/SCAN.xpl:690 to
// parse every numeric literal into the literal table.
//
// Final scaling follows XXXTOD.bal:1530-1700:
//   TIMES10A  while |dec_exp| >= 23: MD/DD F0,=D'1E23'
//   TIMES10B  build F4 = 10^(0..22 remainder) via repeated squaring
//   TIMES10C  MDR/DDR F0,F4
//
// =D'1E23' is IBM Assembler-H's evaluation of the literal `1E23`.
// True 10^23 × 16^14 / 16^20 = 5,960,464,477,539,062.5 — exactly
// halfway between two DP mantissas — and Assembler-H rounds half-up,
// so the constant lives at 0x54152D02C7E14AF7
//
static const uint64_t IBM_DP_1E23 = 0x54152D02C7E14AF7ULL;

void __attribute__ ((no_instrument_function))
ibm_dp_from_string(const char *s, uint32_t *msw, uint32_t *lsw) {
    assert(s != NULL && msw != NULL && lsw != NULL);
    uint8_t sign = 0;
    if (*s == '+') s++;
    else if (*s == '-') { sign = 1; s++; }

    uint64_t F0 = 0;
    uint64_t TEN = ibm_dp_from_uint(10);
    int frac_digits = 0, seen_dot = 0, ignore = 0;
    while (*s) {
        if (*s >= '0' && *s <= '9') {
            if (!ignore) {
                F0 = ibm_dp_mul(F0, TEN);
                if (*s != '0') {
                    F0 = ibm_dp_addsub(F0, ibm_dp_from_uint((uint64_t)(*s - '0')), 0, 1);
                }
                if (seen_dot) frac_digits++;
                // XXXTOD precision guard (MONITOR.ASM/XXXTOD.bal lines
                // 26-32): once F0 reaches the SP equivalent of ~7.2e15
                // (=X'4E19999A') and we're past the decimal point,
                // ignore all remaining digits.  Without this, every
                // additional digit costs another truncating *=10
                // multiply, accumulating LSB error past the 56-bit
                // mantissa's natural ~17-decimal-digit precision.  Note
                // pre-decimal digits never trigger ignore — they affect
                // magnitude and must always be processed.
                if (seen_dot
                    && ((uint32_t)(F0 >> 32)) >= 0x4E19999AU) {
                    ignore = 1;
                }
            }
            s++;
        } else if (*s == '.' && !seen_dot) {
            seen_dot = 1;
            s++;
        } else {
            break;
        }
    }

    int dec_exp = 0;
    if (*s == 'E' || *s == 'e') {
        s++;
        int es = 1;
        if (*s == '+') s++;
        else if (*s == '-') { es = -1; s++; }
        int v = 0;
        while (*s >= '0' && *s <= '9') { v = v * 10 + (*s - '0'); s++; }
        dec_exp = es * v;
    }
    dec_exp -= frac_digits;

    if (IBM_DP_MANT(F0) == 0) { *msw = 0; *lsw = 0; return; }
    if (sign) F0 |= IBM_DP_SIGN_BIT;

    if (dec_exp != 0) {
        int absexp = dec_exp < 0 ? -dec_exp : dec_exp;
        // TIMES10A: chunk by 23 using the Assembler-H 1E23 constant.
        while (absexp >= 23) {
            F0 = (dec_exp > 0)
                 ? ibm_dp_mul(F0, IBM_DP_1E23)
                 : ibm_dp_div(F0, IBM_DP_1E23);
            absexp -= 23;
        }
        // TIMES10B / TIMES10C: build F4 = 10^(0..22) via squaring,
        // then MDR/DDR.
        if (absexp > 0) {
            uint64_t F4 = ibm_dp_from_uint(1);
            uint64_t F2 = TEN;
            while (absexp > 0) {
                if (absexp & 1) F4 = ibm_dp_mul(F4, F2);
                absexp >>= 1;
                if (absexp) F2 = ibm_dp_mul(F2, F2);
            }
            F0 = (dec_exp > 0) ? ibm_dp_mul(F0, F4) : ibm_dp_div(F0, F4);
        }
    }

    if (F0 == IBM_DP_OVERFLOW_PACKED) { *msw = IBM_DP_OVERFLOW_MSW; *lsw = 0; return; }
    if (IBM_DP_MANT(F0) == 0) { *msw = 0; *lsw = 0; return; }
    *msw = (uint32_t)(F0 >> 32);
    *lsw = (uint32_t)F0;
}



// IBM hex dp add/subtract
// based on hyperion's add_lf:
// https://github.com/hercules-390/hyperion/blob/bec74e3a3dc26acb251eb820b3aeafcee0576b88/float.c#L1307
//
uint64_t
ibm_dp_addsub(uint64_t a_packed, uint64_t b_packed,
              int subtract_b, int normalize) {
    uint8_t  a_sign = IBM_DP_SIGN(a_packed);
    int      a_exp  = IBM_DP_EXP(a_packed);
    uint64_t a_mant = IBM_DP_MANT(a_packed);
    uint8_t  b_sign = IBM_DP_SIGN(b_packed);
    int      b_exp  = IBM_DP_EXP(b_packed);
    uint64_t b_mant = IBM_DP_MANT(b_packed);
    if (subtract_b) b_sign ^= 1;

    int a_iszero = IBM_DP_IS_TRUE_ZERO(a_packed);
    int b_iszero = IBM_DP_IS_TRUE_ZERO(b_packed);

    if (!b_iszero && !a_iszero) {
        // Both not zero — align with guard digit, then signed add.
        if (a_exp == b_exp) {
            // Equal exponents: just introduce guard digit on both.
            a_mant <<= 4;
            b_mant <<= 4;
        } else if (a_exp < b_exp) {
            int shift = b_exp - a_exp - 1;  // minus guard digit
            a_exp = b_exp;
            if (shift > 0) {
                if (shift >= IBM_DP_MANT_HEXDIGITS
                    || ((a_mant >>= (shift * 4)) == 0)) {
                    // a effectively shifted to zero — copy b (no guard).
                    a_sign = b_sign;
                    a_mant = b_mant;
                    if (a_mant == 0 || !normalize) goto pack;
                    goto normalize_56bit;
                }
            }
            b_mant <<= 4;  // guard digit on b
        } else {
            int shift = a_exp - b_exp - 1;  // minus guard digit
            if (shift > 0) {
                if (shift >= IBM_DP_MANT_HEXDIGITS
                 || ((b_mant >>= (shift * 4)) == 0)) {
                    // b effectively shifted to zero — result is a (no guard).
                    if (a_mant == 0 || !normalize) goto pack;
                    goto normalize_56bit;
                }
            }
            a_mant <<= 4;  // guard digit on a
        }

        // Compute with guard digit (60-bit mantissas).
        uint64_t r_mant;
        uint8_t  r_sign;
        if (a_sign == b_sign) {
            r_sign = a_sign;
            r_mant = a_mant + b_mant;
        } else if (a_mant == b_mant) {
            // True cancellation — result is +0.
            a_sign = 0; a_exp = 0; a_mant = 0;
            goto pack;
        } else if (a_mant > b_mant) {
            r_sign = a_sign;
            r_mant = a_mant - b_mant;
        } else {
            r_sign = b_sign;
            r_mant = b_mant - a_mant;
        }

        // Post-add: handle overflow / drop guard / renormalize.
        if (r_mant & IBM_DP_OVERFLOW_60) {
            // Overflow into bit 60+: shift right 1 hex (cancels guard
            // and absorbs overflow), bump expo.
            r_mant >>= 8;
            a_exp += 1;
        } else if (!normalize) {
            // UNNORMAL: just drop guard.
            r_mant >>= 4;
        } else if (r_mant & IBM_DP_TOP_HEX_60) {
            // NORMAL, top guard-hex set: drop guard, already normalized.
            r_mant >>= 4;
        } else {
            // NORMAL, leading zero hex in 60-bit form: re-interpret as
            // 56-bit (decrement expo to compensate for the implicit
            // <<4 from the format change), then normalize.  High bits
            // beyond bit 55 will be truncated by the pack-time mask.
            a_exp -= 1;
            if (r_mant) IBM_DP_RENORMALIZE_56(r_mant, a_exp);
            else        { r_sign = 0; a_exp = 0; };
        }
        a_sign = r_sign;
        a_mant = r_mant;
        goto pack;
    }

    if (b_iszero && a_iszero) {
        // Both true zero.
        a_sign = 0; a_exp = 0; a_mant = 0;
        goto pack;
    }
    if (a_iszero) {
        // a true zero, b not — copy b.
        a_sign = b_sign;
        a_exp  = b_exp;
        a_mant = b_mant;
    }
    // (else: a not zero, b true zero — keep a as-is.)
    // For NORMAL we must always route through normalize_56bit so that
    // an `a` with mant=0 but exp!=0 (e.g., FIXER-format zero left by a
    // prior AW) canonicalizes to true zero — matches Hyperion's
    // significance_lf path in add_lf (float.c:1442-1452).
    if (normalize) goto normalize_56bit;
    goto pack;

normalize_56bit:
    if (a_mant == 0) { a_sign = 0; a_exp = 0; goto pack; }
    IBM_DP_RENORMALIZE_56(a_mant, a_exp);

pack:
    if (a_exp < 0) a_exp = 0;
    if (a_exp > IBM_DP_EXP_MAX) a_exp = IBM_DP_EXP_MAX;
    return IBM_DP_PACK(a_sign, a_exp, a_mant);
}

uint64_t ibm_dp_add(uint64_t a, uint64_t b) {
    return ibm_dp_addsub(a, b, /*subtract_b=*/0, 1);
}


uint64_t ibm_dp_sub(uint64_t a, uint64_t b) {
    return ibm_dp_addsub(a, b, /*subtract_b=*/1, 1);
}

// IBM hex DP multiply 
// based on Hyperion's mul_lf:
// https://github.com/hercules-390/hyperion/blob/bec74e3a3dc26acb251eb820b3aeafcee0576b88/float.c#L2082
//
uint64_t
ibm_dp_mul(uint64_t a, uint64_t b) {
    uint64_t a_mant = IBM_DP_MANT(a);
    uint64_t b_mant = IBM_DP_MANT(b);
    if (a_mant == 0 || b_mant == 0) return 0;

    int     a_exp  = IBM_DP_EXP(a);
    int     b_exp  = IBM_DP_EXP(b);
    uint8_t r_sign = IBM_DP_SIGN(a) ^ IBM_DP_SIGN(b);

    IBM_DP_RENORMALIZE_56(a_mant, a_exp);
    IBM_DP_RENORMALIZE_56(b_mant, b_exp);

    // 56x56 → 112-bit product via four 32x32 partial multiplications.
    // We compute the upper 96 bits (low 32 bits of product are always 0
    // for normalized 56-bit operands, since the smallest nonzero hex
    // digit lives at bit 0 and the lowest possible product bit is 0).
    uint64_t a_lo = a_mant & 0xFFFFFFFFULL, a_hi = a_mant >> 32;
    uint64_t b_lo = b_mant & 0xFFFFFFFFULL, b_hi = b_mant >> 32;
    uint64_t wk = (a_lo * b_lo) >> 32;
    wk += a_lo * b_hi;
    wk += a_hi * b_lo;
    uint32_t v  = (uint32_t)(wk & 0xFFFFFFFFULL);
    uint64_t hi = (wk >> 32) + (a_hi * b_hi);

    int      r_exp;
    uint64_t r_mant;
    if (hi & 0x0000F00000000000ULL) {
        // Top hex of result already at bit 44-47 of `hi` → shift left 8
        // to pack 56 bits into low part of U64.
        r_mant = (hi << 8) | (v >> 24);
        r_exp  = a_exp + b_exp - IBM_DP_EXP_BIAS;
    } else {
        // Top hex is one digit lower → shift left 12 and account via expo.
        r_mant = (hi << 12) | (v >> 20);
        r_exp  = a_exp + b_exp - (IBM_DP_EXP_BIAS + 1);
    }

    if (r_exp < 0) return 0;
    if (r_exp > IBM_DP_EXP_MAX) return IBM_DP_OVERFLOW_PACKED;  // overflow sentinel
    return IBM_DP_PACK(r_sign, r_exp, r_mant);
}


// IBM hex DP divide
// based on Hyperion's div_lf:
// https://github.com/hercules-390/hyperion/blob/bec74e3a3dc26acb251eb820b3aeafcee0576b88/float.c#L2298
//
uint64_t
ibm_dp_div(uint64_t a, uint64_t b) {
    uint64_t a_mant = IBM_DP_MANT(a);
    uint64_t b_mant = IBM_DP_MANT(b);
    assert(b_mant != 0 && "ibm_dp_div: divisor mantissa is zero");
    if (a_mant == 0) return 0;

    int     a_exp  = IBM_DP_EXP(a);
    int     b_exp  = IBM_DP_EXP(b);
    uint8_t r_sign = IBM_DP_SIGN(a) ^ IBM_DP_SIGN(b);

    IBM_DP_RENORMALIZE_56(a_mant, a_exp);
    IBM_DP_RENORMALIZE_56(b_mant, b_exp);

    int r_exp;
    if (a_mant < b_mant) {
        r_exp = a_exp - b_exp + IBM_DP_EXP_BIAS;
    } else {
        r_exp = a_exp - b_exp + (IBM_DP_EXP_BIAS + 1);
        b_mant <<= 4;  // shift divisor up so first quotient digit fits
    }

    // Long division: produce 14 hex digits (= 56 bits) of quotient.
    uint64_t wk2 = a_mant / b_mant;
    uint64_t wk  = (a_mant % b_mant) << 4;
    int i = IBM_DP_MANT_HEXDIGITS - 1;
    while (i--) {
        wk2 = (wk2 << 4) | (wk / b_mant);
        wk  = (wk % b_mant) << 4;
    }
    uint64_t r_mant = (wk2 << 4) | (wk / b_mant);

    if (r_exp < 0) return 0;
    if (r_exp > IBM_DP_EXP_MAX) return IBM_DP_OVERFLOW_PACKED;
    return IBM_DP_PACK(r_sign, r_exp, r_mant);
}

// data from TENSTBL.bal, used by XXDTOC.
//
// Each entry is the IBM Assemblerevaluation of the literal `=D'10^N'`
//  NOTE: `=D` literals use a round-up rule, not the truncate rule
//        used elsewhere in this code.
//
static const uint64_t TENSTBL[17] = {
    /* 10^0  */  0x4110000000000000ULL,
    /* 10^1  */  0x41A0000000000000ULL,
    /* 10^2  */  0x4264000000000000ULL,
    /* 10^3  */  0x433E800000000000ULL,
    /* 10^4  */  0x4427100000000000ULL,
    /* 10^5  */  0x45186A0000000000ULL,
    /* 10^6  */  0x45F4240000000000ULL,
    /* 10^7  */  0x4698968000000000ULL,
    /* 10^8  */  0x475F5E1000000000ULL,
    /* 10^9  */  0x483B9ACA00000000ULL,
    /* 10^10 */  0x492540BE40000000ULL,
    /* 10^20 */  0x5156BC75E2D63100ULL,
    /* 10^30 */  0x59C9F2C9CD04674FULL,
    /* 10^40 */  0x621D6329F1C35CA5ULL,
    /* 10^50 */  0x6A446C3B15F99267ULL,
    /* 10^60 */  0x729F4F2726179A22ULL,
    /* 10^70 */  0x7B172EBAD6DDC73DULL,
};

// ibm_dp_to_string — direct IBM hex DP -> decimal string
//
// Based on MONITOR.ASM/XXDTOC.bal
//
//   1. Iteratively scale |value| by powers of 10 until
//      the characteristic field equals 0x4E (= 78), at which point
//      the 56-bit mantissa is the leading 16-17 decimal digits of the
//      original value as an integer.  Each iteration's log10 step is
//      computed the same way the assembly did:
//          (|char-78| * 19728 + 8192) >> 14
//      ≈ |char-78| * log10(16), rounded.
//   2. Format the mantissa as a 17-digit string.  XXDTOC's WINT
//      assembly is just a packed-decimal representation of this
//      integer; the leading digit may be a real digit (17-digit
//      mantissa) or an implicit leading '0' (16-digit mantissa).
//      Take sig_digits chars starting from the first non-zero, and
//      pad on the right with '0' if pad_to_digits > sig_digits.
//   3. The decimal exponent for the displayed "D.DDD...DD" is
//      E10 + 16 - (leading zeros skipped), matching XXDTOC's
//      `AH R1,H15` (and conditional `AH R1,ONE` for 17-digit case).
//
void
ibm_dp_to_string(uint32_t msw, uint32_t lsw, int sig_digits,
                 int pad_to_digits, char *out, size_t out_len)
{
    assert(out != NULL && out_len > 0);
    assert(sig_digits > 0 && sig_digits <= 30);
    assert(pad_to_digits >= sig_digits && pad_to_digits <= 30);

    if (((msw & 0x7FFFFFFFu) | lsw) == 0) {
        snprintf(out, out_len, "0.0");
        return;
    }

    int sign = (msw >> 31) & 1u;
    uint64_t value = ((uint64_t)(msw & 0x7FFFFFFFu) << 32) | lsw; // unsigned

    int E10 = 0;

    for (int iter = 0; iter < 8; iter++) {
        int char_field = (int)((value >> 56) & IBM_DP_EXP_MAX);
        int diff = char_field - 78;
        if (diff == 0) break;

        int abs_diff = diff < 0 ? -diff : diff;
        int log10_diff = ((abs_diff * 19728) + 8192) >> 14;
        if (log10_diff > 78) log10_diff = 78;
        if (log10_diff <= 0) break;

        int ones = log10_diff % 10;
        int tens = log10_diff / 10;

        if (ones > 0) {
            uint64_t f = TENSTBL[ones];
            value = (diff > 0) ? ibm_dp_div(value, f)
                               : ibm_dp_mul(value, f);
        }
        if (tens > 0) {
            uint64_t f = TENSTBL[9 + tens];
            value = (diff > 0) ? ibm_dp_div(value, f)
                               : ibm_dp_mul(value, f);
        }

        if (diff > 0) E10 += log10_diff;
        else          E10 -= log10_diff;
    }

    uint64_t mantissa = value & IBM_DP_MANT_MASK;

    // Render mantissa as 17 chars (leading zero if 16-digit)
    char m[20];
    snprintf(m, sizeof(m), "%017llu", (unsigned long long)mantissa);

    // Skip leading zeros to find the first significant digit.
    int start = 0;
    while (start < 16 && m[start] == '0') start++;

    int E10_display = E10 + (16 - start);

    char display[40];
    int avail = 17 - start;
    int take  = sig_digits < avail ? sig_digits : avail;
    int pos = 0;
    for (int i = 0; i < take; i++) display[pos++] = m[start + i];
    for (int i = take; i < pad_to_digits; i++) display[pos++] = '0';
    display[pos] = '\0';

    int abs_E10 = E10_display < 0 ? -E10_display : E10_display;
    snprintf(out, out_len, "%s%c.%sE%c%02d", sign ? "-" : "",
                                             display[0],
                                             display + 1,
                                             E10_display < 0 ? '-' : '+',
                                             abs_E10);
}

// ibm_dp_to_hal_string — HAL/S MONITOR(12)-style formatting.
//
//     0.0:        Printed as " 0.0"   (notice the leading space)
//     Positive:   Printed as " d.ddd...E±ee"
//     Negative:   Printed as "-d.ddd...E±ee"
// SP gets 7 sig digits (6 fractional); DP gets 16 (15 fractional);
// precision==0 selects SP, anything else selects DP.
//
// Zero detection treats any all-zero-mantissa packed value as zero,
// which also folds the IBM overflow sentinel (0xFF000000_00000000)
// into "0.0" the same way the previous IEEE-roundtrip path did.
void
ibm_dp_to_hal_string(uint32_t msw, uint32_t lsw, int precision,
                     char *out, size_t out_len)
{
    if ((msw & 0x00FFFFFFu) == 0 && lsw == 0) {
        snprintf(out, out_len, " 0.0");
        return;
    }
    int sig = (precision == 0) ? 7 : 16;
    char body[64];
    ibm_dp_to_string(msw, lsw, sig, sig, body, sizeof(body));
    if (body[0] == '-')
        snprintf(out, out_len, "%s", body);
    else
        snprintf(out, out_len, " %s", body);
}


// =====================================================================
// AP-101S exception-aware DP arithmetic.
//
// Modeled on Hercules add_lf/mul_lf/div_lf/cmp_lf (~/sw/hyperion/float.c)
// and the existing ibm_dp_addsub above.  Differences from the legacy
// (non-_exc) entry points:
//   - Returns ibm_fp_exc_t per the contract in ibmFloat.h.
//   - Overflow is reported as IBM_FP_EXP_OVERFLOW with *result holding
//     the would-be-wrapped (exp & 0x7F) value for trace use.  Caller
//     must not write back per POO §8.8.
//   - Underflow is reported as IBM_FP_EXP_UNDERFLOW with *result = 0.
//     Caller decides whether to write based on PSW mask bit 22.
//   - True cancellation (or any post-op zero fraction in add/sub) is
//     reported as IBM_FP_SIGNIFICANCE with *result = 0.  Caller always
//     writes; PSW mask bit 23 only gates the interrupt.
//   - Divide by zero is reported as IBM_FP_DIVIDE with *result =
//     IBM_FP_DIVIDE_SENTINEL_DP (no assert).
// =====================================================================

ibm_fp_exc_t
ibm_dp_addsub_exc(uint64_t a_packed, uint64_t b_packed,
                  int subtract_b, uint64_t *result) {
    assert(result != NULL);
    uint8_t  a_sign = IBM_DP_SIGN(a_packed);
    int      a_exp  = IBM_DP_EXP(a_packed);
    uint64_t a_mant = IBM_DP_MANT(a_packed);
    uint8_t  b_sign = IBM_DP_SIGN(b_packed);
    int      b_exp  = IBM_DP_EXP(b_packed);
    uint64_t b_mant = IBM_DP_MANT(b_packed);
    if (subtract_b) b_sign ^= 1;

    int a_iszero = IBM_DP_IS_TRUE_ZERO(a_packed);
    int b_iszero = IBM_DP_IS_TRUE_ZERO(b_packed);

    if (!b_iszero && !a_iszero) {
        // Both not zero — align with guard digit, then signed add.
        if (a_exp == b_exp) {
            a_mant <<= 4;
            b_mant <<= 4;
        } else if (a_exp < b_exp) {
            int shift = b_exp - a_exp - 1;  // minus guard digit
            a_exp = b_exp;
            if (shift > 0) {
                if (shift >= IBM_DP_MANT_HEXDIGITS
                    || ((a_mant >>= (shift * 4)) == 0)) {
                    // a effectively shifted to zero — result is just b.
                    a_sign = b_sign;
                    a_mant = b_mant;
                    goto normalize_56bit;
                }
            }
            b_mant <<= 4;
        } else {
            int shift = a_exp - b_exp - 1;
            if (shift > 0) {
                if (shift >= IBM_DP_MANT_HEXDIGITS
                 || ((b_mant >>= (shift * 4)) == 0)) {
                    // b effectively shifted to zero — result is just a.
                    goto normalize_56bit;
                }
            }
            a_mant <<= 4;
        }

        // Compute with guard digit (60-bit mantissas).
        uint64_t r_mant;
        uint8_t  r_sign;
        if (a_sign == b_sign) {
            r_sign = a_sign;
            r_mant = a_mant + b_mant;
        } else if (a_mant == b_mant) {
            // True cancellation — significance.
            *result = 0;
            return IBM_FP_SIGNIFICANCE;
        } else if (a_mant > b_mant) {
            r_sign = a_sign;
            r_mant = a_mant - b_mant;
        } else {
            r_sign = b_sign;
            r_mant = b_mant - a_mant;
        }

        // Post-add: handle overflow / drop guard / renormalize.
        if (r_mant & IBM_DP_OVERFLOW_60) {
            // Overflow into bit 60+: shift right 1 hex (cancels guard
            // and absorbs overflow), bump expo.
            r_mant >>= 8;
            a_exp += 1;
        } else if (r_mant & IBM_DP_TOP_HEX_60) {
            // Top guard-hex set: drop guard, already normalized.
            r_mant >>= 4;
        } else {
            // Leading zero hex in 60-bit form: re-interpret as 56-bit
            // (decrement expo to compensate for the implicit <<4 from
            // the format change), then normalize.  Truncation by the
            // pack-time mask drops bits beyond bit 55.
            a_exp -= 1;
            if (r_mant) {
                IBM_DP_RENORMALIZE_56(r_mant, a_exp);
            } else {
                // Significance — both operands' magnitudes cancelled.
                *result = 0;
                return IBM_FP_SIGNIFICANCE;
            }
        }
        a_sign = r_sign;
        a_mant = r_mant;
        goto pack;
    }

    if (b_iszero && a_iszero) {
        // Both true zero — POO §8.9: significance interrupt fires here
        // too (intermediate sum is zero).
        *result = 0;
        return IBM_FP_SIGNIFICANCE;
    }
    if (a_iszero) {
        a_sign = b_sign;
        a_exp  = b_exp;
        a_mant = b_mant;
    }
    // (else: a not zero, b true zero — keep a as-is.)
    goto normalize_56bit;

normalize_56bit:
    if (a_mant == 0) {
        // Magnitude-only zero (e.g., FIXER pattern: mant=0, exp!=0).
        *result = 0;
        return IBM_FP_SIGNIFICANCE;
    }
    IBM_DP_RENORMALIZE_56(a_mant, a_exp);

pack:
    if (a_exp > IBM_DP_EXP_MAX) {
        // Exponent overflow — caller MUST NOT write back, but we still
        // pack a wrapped value so refgen has something deterministic to
        // compare against.
        *result = IBM_DP_PACK(a_sign, a_exp & IBM_DP_EXP_MAX, a_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (a_exp < 0) {
        // Exponent underflow with non-zero fraction.  POO §8.8: result
        // is true zero (the masked-off behavior).  Caller chooses based
        // on PSW bit 22.
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_DP_PACK(a_sign, a_exp, a_mant);
    return IBM_FP_OK;
}

ibm_fp_exc_t ibm_dp_add_exc(uint64_t a, uint64_t b, uint64_t *result) {
    return ibm_dp_addsub_exc(a, b, 0, result);
}

ibm_fp_exc_t ibm_dp_sub_exc(uint64_t a, uint64_t b, uint64_t *result) {
    return ibm_dp_addsub_exc(a, b, 1, result);
}

ibm_fp_exc_t
ibm_dp_mul_exc(uint64_t a, uint64_t b, uint64_t *result) {
    assert(result != NULL);
    uint64_t a_mant = IBM_DP_MANT(a);
    uint64_t b_mant = IBM_DP_MANT(b);
    if (a_mant == 0 || b_mant == 0) {
        // POO §8.17: zero operand forces true zero result, no exception.
        *result = 0;
        return IBM_FP_OK;
    }

    int     a_exp  = IBM_DP_EXP(a);
    int     b_exp  = IBM_DP_EXP(b);
    uint8_t r_sign = IBM_DP_SIGN(a) ^ IBM_DP_SIGN(b);

    IBM_DP_RENORMALIZE_56(a_mant, a_exp);
    IBM_DP_RENORMALIZE_56(b_mant, b_exp);

    uint64_t a_lo = a_mant & 0xFFFFFFFFULL, a_hi = a_mant >> 32;
    uint64_t b_lo = b_mant & 0xFFFFFFFFULL, b_hi = b_mant >> 32;
    uint64_t wk = (a_lo * b_lo) >> 32;
    wk += a_lo * b_hi;
    wk += a_hi * b_lo;
    uint32_t v  = (uint32_t)(wk & 0xFFFFFFFFULL);
    uint64_t hi = (wk >> 32) + (a_hi * b_hi);

    int      r_exp;
    uint64_t r_mant;
    if (hi & 0x0000F00000000000ULL) {
        r_mant = (hi << 8) | (v >> 24);
        r_exp  = a_exp + b_exp - IBM_DP_EXP_BIAS;
    } else {
        r_mant = (hi << 12) | (v >> 20);
        r_exp  = a_exp + b_exp - (IBM_DP_EXP_BIAS + 1);
    }

    if (r_exp > IBM_DP_EXP_MAX) {
        *result = IBM_DP_PACK(r_sign, r_exp & IBM_DP_EXP_MAX, r_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (r_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_DP_PACK(r_sign, r_exp, r_mant);
    return IBM_FP_OK;
}

ibm_fp_exc_t
ibm_dp_div_exc(uint64_t a, uint64_t b, uint64_t *result) {
    assert(result != NULL);
    uint64_t a_mant = IBM_DP_MANT(a);
    uint64_t b_mant = IBM_DP_MANT(b);
    if (b_mant == 0) {
        // FP divide exception — division suppressed per POO §8.8.
        *result = IBM_FP_DIVIDE_SENTINEL_DP;
        return IBM_FP_DIVIDE;
    }
    if (a_mant == 0) {
        // POO §8.15: dividend zero -> true zero result, no exception.
        *result = 0;
        return IBM_FP_OK;
    }

    int     a_exp  = IBM_DP_EXP(a);
    int     b_exp  = IBM_DP_EXP(b);
    uint8_t r_sign = IBM_DP_SIGN(a) ^ IBM_DP_SIGN(b);

    IBM_DP_RENORMALIZE_56(a_mant, a_exp);
    IBM_DP_RENORMALIZE_56(b_mant, b_exp);

    int r_exp;
    if (a_mant < b_mant) {
        r_exp = a_exp - b_exp + IBM_DP_EXP_BIAS;
    } else {
        r_exp = a_exp - b_exp + (IBM_DP_EXP_BIAS + 1);
        b_mant <<= 4;
    }

    uint64_t wk2 = a_mant / b_mant;
    uint64_t wk  = (a_mant % b_mant) << 4;
    int i = IBM_DP_MANT_HEXDIGITS - 1;
    while (i--) {
        wk2 = (wk2 << 4) | (wk / b_mant);
        wk  = (wk % b_mant) << 4;
    }
    uint64_t r_mant = (wk2 << 4) | (wk / b_mant);

    if (r_exp > IBM_DP_EXP_MAX) {
        *result = IBM_DP_PACK(r_sign, r_exp & IBM_DP_EXP_MAX, r_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (r_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_DP_PACK(r_sign, r_exp, r_mant);
    return IBM_FP_OK;
}

// AP-101S §8.17 quasi-extended multiply.  Each operand mantissa is
// pre-truncated to 31 fraction bits and rounded into bit 31 from bit 32
// before the multiply.  Bits 32..55 of each operand are discarded.
//
// Rounding rule: "Rounding means adding the 32nd bit to the 31st bit
// gating all possible carries."  POO §8.17 programming note further
// warns that rounding can cause exponent overflow (a 7FFFFFFFFF000000
// operand rounds to 8000000000000000 → expo bumped, then overflow).
static uint64_t
ibm_dp_round_to_31bit(uint64_t mant, int *exp) {
    // mant is the 56-bit fraction from a packed value (bits 0..55).
    // Top 4 hex digits = bits 32..55; we keep the top 31 frac bits
    // (bits 25..55 ... with sign-bit position implicit).  Equivalent to
    // truncating the bottom 25 bits and rounding with bit 24.
    //
    // Concretely: we want a 31-bit number in bits 25..55, rounded from
    // bit 24.  Add 1 << 24 then mask off bits 0..24.
    uint64_t rounded = mant + (1ULL << 24);
    if (rounded & (1ULL << 56)) {
        // Round caused carry out of the mantissa — shift right one hex
        // digit and bump the exponent.  This is the §8.17 case where
        // rounding triggers exponent overflow (caller checks).
        rounded >>= 4;
        *exp += 1;
    }
    return rounded & ~((1ULL << 25) - 1);  // clear bottom 25 bits
}

ibm_fp_exc_t
ibm_dp_mul_qe_exc(uint64_t a, uint64_t b, uint64_t *result) {
    assert(result != NULL);
    uint64_t a_mant = IBM_DP_MANT(a);
    uint64_t b_mant = IBM_DP_MANT(b);
    if (a_mant == 0 || b_mant == 0) {
        *result = 0;
        return IBM_FP_OK;
    }

    int     a_exp  = IBM_DP_EXP(a);
    int     b_exp  = IBM_DP_EXP(b);
    uint8_t r_sign = IBM_DP_SIGN(a) ^ IBM_DP_SIGN(b);

    IBM_DP_RENORMALIZE_56(a_mant, a_exp);
    IBM_DP_RENORMALIZE_56(b_mant, b_exp);

    a_mant = ibm_dp_round_to_31bit(a_mant, &a_exp);
    b_mant = ibm_dp_round_to_31bit(b_mant, &b_exp);

    // Pre-multiply rounding may have driven either exponent above max.
    if (a_exp > IBM_DP_EXP_MAX || b_exp > IBM_DP_EXP_MAX) {
        *result = IBM_DP_PACK(r_sign, IBM_DP_EXP_MAX, 0);
        return IBM_FP_EXP_OVERFLOW;
    }

    // After rounding, both mantissas are 31-bit values living in bits
    // 25..55 of the original mantissa slot.  Shift down by 25 so each
    // fits in the low 31 bits of a uint64; their product fits in 62
    // bits.  Algebra: a_frac = a31 / 2^31; product_value = prod / 2^62;
    // packed mantissa target = product_value * 2^56 = prod >> 6.
    uint64_t a31 = a_mant >> 25;
    uint64_t b31 = b_mant >> 25;
    uint64_t prod = a31 * b31;        // up to 62 bits

    uint64_t target = prod >> 6;
    uint64_t r_mant;
    int r_exp;
    if (target == 0) {
        // Can only happen if prod < 2^6 — both operands tiny — but the
        // smallest normalized 31-bit operand is 0x08000000 (top hex 1
        // at bit 27 after the >>25), so prod >= 2^54 and target >= 2^48.
        // We assert here because this branch is unreachable for valid
        // normalized inputs.
        assert(0 && "ibm_dp_mul_qe_exc: zero product from normalized inputs");
        *result = 0;
        return IBM_FP_OK;
    }
    if (target & IBM_DP_TOP_HEX_56) {
        // Top hex (bits 52..55) of target is non-zero — normalized.
        r_mant = target & IBM_DP_MANT_MASK;
        r_exp  = a_exp + b_exp - IBM_DP_EXP_BIAS;
    } else {
        // target's top hex is one digit below — shift up 4 (= prod >> 2)
        // and decrement exp by 1.  At most one hex of normalization is
        // needed: smallest product 2^54 maps to target 2^48 (top hex at
        // bits 48..51), shifted up to bits 52..55.
        r_mant = (prod >> 2) & IBM_DP_MANT_MASK;
        r_exp  = a_exp + b_exp - (IBM_DP_EXP_BIAS + 1);
    }

    if (r_exp > IBM_DP_EXP_MAX) {
        *result = IBM_DP_PACK(r_sign, r_exp & IBM_DP_EXP_MAX, r_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (r_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_DP_PACK(r_sign, r_exp, r_mant);
    return IBM_FP_OK;
}


// =====================================================================
// DP compare — returns CC: 0=equal, 1=a>b, 3=a<b.
//
// Per POO §8.11, compare cannot raise exponent overflow / underflow /
// significance.  The "anomalous" variant reproduces the documented
// hardware bug: false equality when fractions differ by x'00 0000 0080
// 0000' after prealignment.
//
// Implementation is modeled on Hercules cmp_lf — prealign with guard
// digit, signed-fract subtract, examine the result fraction.
// =====================================================================

static int
dp_compare_internal(uint64_t a_packed, uint64_t b_packed, int anomalous) {
    uint8_t  a_sign = IBM_DP_SIGN(a_packed);
    int      a_exp  = IBM_DP_EXP(a_packed);
    uint64_t a_mant = IBM_DP_MANT(a_packed);
    uint8_t  b_sign = IBM_DP_SIGN(b_packed);
    int      b_exp  = IBM_DP_EXP(b_packed);
    uint64_t b_mant = IBM_DP_MANT(b_packed);

    int a_iszero = IBM_DP_IS_TRUE_ZERO(a_packed);
    int b_iszero = IBM_DP_IS_TRUE_ZERO(b_packed);

    // Either operand zero: short-circuit (POO programming note: zero
    // fractions compare equal regardless of sign or characteristic).
    if (a_iszero && b_iszero) return 0;
    if (a_iszero) return b_sign ? 1 : 3;   // a=0, b<0 -> a>b ; a=0, b>0 -> a<b
    if (b_iszero) return a_sign ? 3 : 1;

    // Prealign with guard digit (Hercules cmp_lf style).
    if (a_exp == b_exp) {
        a_mant <<= 4;
        b_mant <<= 4;
    } else if (a_exp < b_exp) {
        int shift = b_exp - a_exp - 1;
        if (shift > 0) {
            if (shift >= IBM_DP_MANT_HEXDIGITS
                || ((a_mant >>= (shift * 4)) == 0)) {
                // a effectively zero after alignment; result follows b.
                return b_sign ? 1 : 3;
            }
        }
        b_mant <<= 4;
    } else {
        int shift = a_exp - b_exp - 1;
        if (shift > 0) {
            if (shift >= IBM_DP_MANT_HEXDIGITS
                || ((b_mant >>= (shift * 4)) == 0)) {
                return a_sign ? 3 : 1;
            }
        }
        a_mant <<= 4;
    }

    // Signed-fraction subtract a - b for comparison.
    uint64_t r_mant;
    uint8_t  r_sign;
    if (a_sign != b_sign) {
        // Different signs — magnitudes add; sign of result follows a.
        r_mant = a_mant + b_mant;
        r_sign = a_sign;
    } else if (a_mant >= b_mant) {
        r_mant = a_mant - b_mant;
        r_sign = a_sign;
    } else {
        r_mant = b_mant - a_mant;
        r_sign = a_sign ^ 1;
    }

    if (anomalous) {
        // POO §8.11 anomaly: false equality when the post-prealign
        // 60-bit-form difference equals exactly 0x8000000.  Derived
        // from all three POO examples — each yields r_mant=0x8000000
        // after the guard-digit prealignment:
        //   ex.1 (equal exps):  a=423F..001234, b=423F..801234
        //                       both <<4, b-a = 0x8000000
        //   ex.2 (exp diff 1):  a=BEFF..076890, b=BF10..076890
        //                       b<<4, |b-a| = 0x8000000
        //   ex.3 (exp diff 1):  a=4010..001234, b=3FFF..012340
        //                       a<<4, a-b = 0x8000000
        if (r_mant == 0x8000000ULL) return 0;
    }

    if (r_mant == 0) return 0;
    return r_sign ? 3 : 1;
}

int ibm_dp_cmp(uint64_t a, uint64_t b) {
    return dp_compare_internal(a, b, 0);
}

int ibm_dp_cmp_anomalous(uint64_t a, uint64_t b) {
    return dp_compare_internal(a, b, 1);
}


// =====================================================================
// SP (24-bit fraction) arithmetic.
//
// Modeled on Hercules add_sf/mul_sf/div_sf/cmp_sf at 24-bit fraction
// width.  AP-101S short FP is fully 24-bit throughout; not "DP and
// truncate".
// =====================================================================

#define SP_TRUE_ZERO(p) (((p) & IBM_SP_MAGNITUDE_MASK) == 0)

ibm_fp_exc_t
ibm_sp_addsub_exc(uint32_t a_packed, uint32_t b_packed,
                  int subtract_b, uint32_t *result) {
    assert(result != NULL);
    uint8_t  a_sign = IBM_SP_SIGN(a_packed);
    int      a_exp  = IBM_SP_EXP(a_packed);
    uint32_t a_mant = IBM_SP_MANT(a_packed);
    uint8_t  b_sign = IBM_SP_SIGN(b_packed);
    int      b_exp  = IBM_SP_EXP(b_packed);
    uint32_t b_mant = IBM_SP_MANT(b_packed);
    if (subtract_b) b_sign ^= 1;

    int a_iszero = SP_TRUE_ZERO(a_packed);
    int b_iszero = SP_TRUE_ZERO(b_packed);

    if (!b_iszero && !a_iszero) {
        if (a_exp == b_exp) {
            a_mant <<= 4;
            b_mant <<= 4;
        } else if (a_exp < b_exp) {
            int shift = b_exp - a_exp - 1;
            a_exp = b_exp;
            if (shift > 0) {
                if (shift >= IBM_SP_MANT_HEXDIGITS
                    || ((a_mant >>= (shift * 4)) == 0)) {
                    a_sign = b_sign;
                    a_mant = b_mant;
                    goto sp_normalize_24bit;
                }
            }
            b_mant <<= 4;
        } else {
            int shift = a_exp - b_exp - 1;
            if (shift > 0) {
                if (shift >= IBM_SP_MANT_HEXDIGITS
                 || ((b_mant >>= (shift * 4)) == 0)) {
                    goto sp_normalize_24bit;
                }
            }
            a_mant <<= 4;
        }

        uint32_t r_mant;
        uint8_t  r_sign;
        if (a_sign == b_sign) {
            r_sign = a_sign;
            r_mant = a_mant + b_mant;
        } else if (a_mant == b_mant) {
            *result = 0;
            return IBM_FP_SIGNIFICANCE;
        } else if (a_mant > b_mant) {
            r_sign = a_sign;
            r_mant = a_mant - b_mant;
        } else {
            r_sign = b_sign;
            r_mant = b_mant - a_mant;
        }

        if (r_mant & IBM_SP_OVERFLOW_28) {
            r_mant >>= 8;
            a_exp += 1;
        } else if (r_mant & IBM_SP_TOP_HEX_28) {
            r_mant >>= 4;
        } else {
            a_exp -= 1;
            if (r_mant) {
                IBM_SP_RENORMALIZE_24(r_mant, a_exp);
            } else {
                *result = 0;
                return IBM_FP_SIGNIFICANCE;
            }
        }
        a_sign = r_sign;
        a_mant = r_mant;
        goto sp_pack;
    }

    if (b_iszero && a_iszero) {
        *result = 0;
        return IBM_FP_SIGNIFICANCE;
    }
    if (a_iszero) {
        a_sign = b_sign;
        a_exp  = b_exp;
        a_mant = b_mant;
    }
    goto sp_normalize_24bit;

sp_normalize_24bit:
    if (a_mant == 0) {
        *result = 0;
        return IBM_FP_SIGNIFICANCE;
    }
    IBM_SP_RENORMALIZE_24(a_mant, a_exp);

sp_pack:
    if (a_exp > IBM_DP_EXP_MAX) {
        *result = IBM_SP_PACK(a_sign, a_exp & IBM_DP_EXP_MAX, a_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (a_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_SP_PACK(a_sign, a_exp, a_mant);
    return IBM_FP_OK;
}

ibm_fp_exc_t ibm_sp_add_exc(uint32_t a, uint32_t b, uint32_t *result) {
    return ibm_sp_addsub_exc(a, b, 0, result);
}

ibm_fp_exc_t ibm_sp_sub_exc(uint32_t a, uint32_t b, uint32_t *result) {
    return ibm_sp_addsub_exc(a, b, 1, result);
}

ibm_fp_exc_t
ibm_sp_mul_exc(uint32_t a, uint32_t b, uint32_t *result) {
    assert(result != NULL);
    uint32_t a_mant = IBM_SP_MANT(a);
    uint32_t b_mant = IBM_SP_MANT(b);
    if (a_mant == 0 || b_mant == 0) {
        *result = 0;
        return IBM_FP_OK;
    }

    int     a_exp  = IBM_SP_EXP(a);
    int     b_exp  = IBM_SP_EXP(b);
    uint8_t r_sign = IBM_SP_SIGN(a) ^ IBM_SP_SIGN(b);

    IBM_SP_RENORMALIZE_24(a_mant, a_exp);
    IBM_SP_RENORMALIZE_24(b_mant, b_exp);

    // 24x24 -> 48-bit product, fits in uint64.
    uint64_t prod = (uint64_t)a_mant * (uint64_t)b_mant;

    int      r_exp;
    uint32_t r_mant;
    // Top hex of normalized 24-bit mantissas is at bit 20..23, so the
    // 48-bit product's top hex sits at bit 44..47 (already-normalized)
    // or bit 40..43 (one hex below — needs shift+expo adjust).
    if (prod & 0x0000F00000000000ULL) {
        r_mant = (uint32_t)((prod >> 24) & IBM_SP_MANT_MASK);
        r_exp  = a_exp + b_exp - IBM_DP_EXP_BIAS;
    } else {
        r_mant = (uint32_t)((prod >> 20) & IBM_SP_MANT_MASK);
        r_exp  = a_exp + b_exp - (IBM_DP_EXP_BIAS + 1);
    }

    if (r_exp > IBM_DP_EXP_MAX) {
        *result = IBM_SP_PACK(r_sign, r_exp & IBM_DP_EXP_MAX, r_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (r_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_SP_PACK(r_sign, r_exp, r_mant);
    return IBM_FP_OK;
}

ibm_fp_exc_t
ibm_sp_div_exc(uint32_t a, uint32_t b, uint32_t *result) {
    assert(result != NULL);
    uint32_t a_mant = IBM_SP_MANT(a);
    uint32_t b_mant = IBM_SP_MANT(b);
    if (b_mant == 0) {
        *result = IBM_FP_DIVIDE_SENTINEL_SP;
        return IBM_FP_DIVIDE;
    }
    if (a_mant == 0) {
        *result = 0;
        return IBM_FP_OK;
    }

    int     a_exp  = IBM_SP_EXP(a);
    int     b_exp  = IBM_SP_EXP(b);
    uint8_t r_sign = IBM_SP_SIGN(a) ^ IBM_SP_SIGN(b);

    IBM_SP_RENORMALIZE_24(a_mant, a_exp);
    IBM_SP_RENORMALIZE_24(b_mant, b_exp);

    // Position the dividend so the quotient lands in the 24-bit slot.
    // Hercules div_sf shifts dividend by 24 (or 20 if a_mant >= b_mant).
    uint64_t wk;
    int      r_exp;
    if (a_mant < b_mant) {
        wk = (uint64_t)a_mant << 24;
        r_exp = a_exp - b_exp + IBM_DP_EXP_BIAS;
    } else {
        wk = (uint64_t)a_mant << 20;
        r_exp = a_exp - b_exp + (IBM_DP_EXP_BIAS + 1);
    }
    uint32_t r_mant = (uint32_t)(wk / b_mant);

    if (r_exp > IBM_DP_EXP_MAX) {
        *result = IBM_SP_PACK(r_sign, r_exp & IBM_DP_EXP_MAX, r_mant);
        return IBM_FP_EXP_OVERFLOW;
    }
    if (r_exp < 0) {
        *result = 0;
        return IBM_FP_EXP_UNDERFLOW;
    }
    *result = IBM_SP_PACK(r_sign, r_exp, r_mant);
    return IBM_FP_OK;
}

// SP compare — same shape as DP.  Anomaly: false equality when post-
// prealign |a-b| in the 28-bit (post-guard) form equals 0x80000U.
// Per POO §8.11 this is the SP equivalent of x'80 0000' in the DP
// fraction (the third hex from the top of the 6-digit short fraction).
static int
sp_compare_internal(uint32_t a_packed, uint32_t b_packed, int anomalous) {
    uint8_t  a_sign = IBM_SP_SIGN(a_packed);
    int      a_exp  = IBM_SP_EXP(a_packed);
    uint32_t a_mant = IBM_SP_MANT(a_packed);
    uint8_t  b_sign = IBM_SP_SIGN(b_packed);
    int      b_exp  = IBM_SP_EXP(b_packed);
    uint32_t b_mant = IBM_SP_MANT(b_packed);

    int a_iszero = SP_TRUE_ZERO(a_packed);
    int b_iszero = SP_TRUE_ZERO(b_packed);
    if (a_iszero && b_iszero) return 0;
    if (a_iszero) return b_sign ? 1 : 3;
    if (b_iszero) return a_sign ? 3 : 1;

    if (a_exp == b_exp) {
        a_mant <<= 4;
        b_mant <<= 4;
    } else if (a_exp < b_exp) {
        int shift = b_exp - a_exp - 1;
        if (shift > 0) {
            if (shift >= IBM_SP_MANT_HEXDIGITS
                || ((a_mant >>= (shift * 4)) == 0)) {
                return b_sign ? 1 : 3;
            }
        }
        b_mant <<= 4;
    } else {
        int shift = a_exp - b_exp - 1;
        if (shift > 0) {
            if (shift >= IBM_SP_MANT_HEXDIGITS
                || ((b_mant >>= (shift * 4)) == 0)) {
                return a_sign ? 3 : 1;
            }
        }
        a_mant <<= 4;
    }

    uint32_t r_mant;
    uint8_t  r_sign;
    if (a_sign != b_sign) {
        r_mant = a_mant + b_mant;
        r_sign = a_sign;
    } else if (a_mant >= b_mant) {
        r_mant = a_mant - b_mant;
        r_sign = a_sign;
    } else {
        r_mant = b_mant - a_mant;
        r_sign = a_sign ^ 1;
    }

    if (anomalous && r_mant == 0x00800000U) return 0;

    if (r_mant == 0) return 0;
    return r_sign ? 3 : 1;
}

int ibm_sp_cmp(uint32_t a, uint32_t b) {
    return sp_compare_internal(a, b, 0);
}

int ibm_sp_cmp_anomalous(uint32_t a, uint32_t b) {
    return sp_compare_internal(a, b, 1);
}


// =====================================================================
// CVFX / CVFL (POO §8.13 / §8.14)
// =====================================================================

// Short FP -> two's-complement int32 with binary point between bits
// 15 and 16 (POO bit numbering: bit 0 = MSB).  In native uint32_t terms
// that means: result = round_toward_zero(fp_value * 2^16).  Convert
// overflow when |fp_value| × 2^16 doesn't fit a signed 32-bit int.
//
// POO §8.13 ANOMALY: "A floating-point value of 41100000 is converted
// to a fixed-point 00010000 but gives a condition code of 00."  The
// CC=00 oddity is the CPU layer's job; this function just produces
// 0x00010000 from 0x41100000 with IBM_FP_OK.
ibm_fp_exc_t
ibm_cvfx(uint32_t fp, int32_t *fixed) {
    assert(fixed != NULL);
    uint8_t  sign = IBM_SP_SIGN(fp);
    int      chr  = IBM_SP_EXP(fp);  // biased characteristic 0..127
    uint32_t mant = IBM_SP_MANT(fp);

    if (mant == 0) {
        *fixed = 0;
        return IBM_FP_OK;
    }
    IBM_SP_RENORMALIZE_24(mant, chr);

    // Position the 24-bit fraction in a 64-bit work value so its top
    // hex sits at bits 60..63.  Then the value-times-2^16 we want lives
    // at bits N..N+31 where N = (16 + 8 - 4*chr_unbiased) ... ugh.
    //
    // Simpler algebra: fp_value = (mant / 2^24) * 16^(chr - 64)
    //                           = mant * 16^(chr - 64) / 2^24
    //                           = mant * 2^(4*(chr-64)-24)
    // Want fp_value * 2^16 = mant * 2^(4*chr - 256 - 24 + 16)
    //                      = mant * 2^(4*chr - 264).
    // For chr = 0x44 = 68: shift = 4*68 - 264 = 272 - 264 = 8.
    //   So mant<<8 is the 32-bit two's-complement integer at chr=0x44.
    //   That matches POO's "place into bits 8..39 of intermediate".
    // For chr = 0x41 = 65: shift = 260 - 264 = -4 (right shift 4).
    //   mant >> 4 is the result.  For mant=0x100000 (=1.0×16):
    //   mant>>4 = 0x10000 — matches the §8.13 anomaly case.
    int shift = 4 * chr - 264;

    // Detect convert overflow before shifting.  Magnitude of result
    // (before sign) = |mant| << shift (positive shift) or |mant| >>
    // (-shift).  Overflow condition: magnitude doesn't fit in 31 bits
    // (positive) or 31 bits + 1 sign bit (allow exactly 0x80000000 if
    // sign bit set).
    if (shift > 32) {
        *fixed = 0;
        return IBM_FP_CONVERT_OVERFLOW;
    }

    uint64_t mag64;
    if (shift >= 0) {
        mag64 = (uint64_t)mant << shift;
    } else {
        // Right shift can yield zero — that's fine, zero is in range.
        int rs = -shift;
        mag64 = (rs >= 32) ? 0 : ((uint64_t)mant >> rs);
    }

    // Range check.  Positive: must fit in [0, 2^31 - 1].  Negative:
    // must fit in [0, 2^31] (latter encodes INT32_MIN).
    if (sign) {
        if (mag64 > 0x80000000ULL) {
            *fixed = -(int32_t)(mag64 & 0x7FFFFFFFU);
            return IBM_FP_CONVERT_OVERFLOW;
        }
        *fixed = (mag64 == 0) ? 0 : -(int32_t)mag64;
    } else {
        if (mag64 > 0x7FFFFFFFULL) {
            *fixed = (int32_t)(mag64 & 0x7FFFFFFFU);
            return IBM_FP_CONVERT_OVERFLOW;
        }
        *fixed = (int32_t)mag64;
    }
    return IBM_FP_OK;
}

// int32 (binary point between bits 15/16) -> short FP.
// Inverse of CVFX: result = (fixed / 2^16) packed as IBM SP.
// POO §8.14: cannot raise.  Per the section: place magnitude into bits
// 8..39 of a 40-bit intermediate with characteristic 0x44, normalize,
// truncate to 24-bit fraction.
uint32_t
ibm_cvfl(int32_t fixed) {
    if (fixed == 0) return 0;

    uint8_t  sign;
    uint32_t mag;
    if (fixed < 0) {
        sign = 1;
        mag = (uint32_t)(-(int64_t)fixed);  // safe for INT32_MIN
    } else {
        sign = 0;
        mag = (uint32_t)fixed;
    }

    // Build the 40-bit intermediate as a 64-bit work value.  Char =
    // 0x44 implicitly.  Mantissa = mag in the top 32 of the 40-bit
    // mantissa slot — i.e., mag<<8 when viewed as a 40-bit number.
    // We'll normalize by shifting the 40-bit form left until its top
    // hex (bits 36..39 of the 40-bit form, = bits 28..31 of the
    // 32-bit-shifted view) is non-zero, decrementing char each step.
    // Then the SP fraction is the top 24 bits of the 40-bit form
    // (bits 16..39 of 40-bit form, = bits 8..31 of mag<<8).
    //
    // Working in a single uint64_t with the value left-aligned to
    // bit 39 of the low 40 bits:
    uint64_t inter = ((uint64_t)mag) << 8;   // bits 8..39 = mag
    int chr = IBM_DP_EXP_BIAS + 4;           // 0x44

    // Normalize: shift left in 4-bit hex steps until bits 36..39 of
    // the 40-bit form (= bits 36..39 of inter, since inter is 40 bits
    // total) are non-zero.
    while (inter != 0 && (inter & 0x000000F000000000ULL) == 0) {
        inter <<= 4;
        chr   -= 1;
    }

    // SP fraction = bits 16..39 of the 40-bit form, = top 24 bits.
    uint32_t mant = (uint32_t)((inter >> 16) & IBM_SP_MANT_MASK);

    // Per POO §8.14, no overflow path possible (max int32 magnitude
    // 2^31 fits comfortably under char 0x44 with up to 4 hex digits of
    // headroom).  Pack and return.
    if (chr < 0) chr = 0;
    if (chr > IBM_DP_EXP_MAX) chr = IBM_DP_EXP_MAX;
    return IBM_SP_PACK(sign, chr, mant);
}
