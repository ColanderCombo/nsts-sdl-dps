/*
 * string_to_ibm.c — proposed fix for virtualagc issue #1296.
 *
 * Replacement for the literal-conversion path in
 *   ext/virtualagc/XCOM-I/runtimeC.c  (MONITOR10 / toFloatIBM)
 *
 * The bug: MONITOR10 currently does
 *     FR[0] = atof(s);              // IEEE-754 double — already lossy
 *     toFloatIBM(&msw, &lsw, FR[0]); // round-half-up
 * which disagrees with the original IBM HAL/S-FC compiler in the LSB
 * of the IBM DP mantissa.  The IBM compiler routine XXXTOD.bal builds
 * the value with S/360 hex floating-point (MD/AD/MDR/DDR), all of
 * which truncate the 14-hex-digit mantissa.
 *
 * Strategy: reimplement the conversion as XXXTOD does — with software-
 * emulated truncating IBM DP arithmetic, using __uint128_t for the
 * intermediate widths.
 *
 * Verified bit-exact match to IBM for the issue case (1.0E-8) and other
 * literals whose absolute decimal exponent is < 23.  For |exp| >= 23,
 * XXXTOD takes a shortcut path that multiplies/divides by the precomputed
 * BAL literal `=D'1E23'`; this code builds 10^N purely by squaring, which
 * may diverge from IBM in the last hex digit at very large magnitudes.
 * The lexer caps significant digits at 74 (SCAN.xpl:688) but in practice
 * HAL/S literals rarely exceed ~15 mantissa digits.
 *
 * The digit accumulator below mirrors XXXTOD's structure literally:
 *   F0 = ibm_add(ibm_mul(F0, 10), digit)
 * which truncates per-digit in IBM-DP precision (matching MD/AD on
 * S/360).  For mantissas of more than ~14 digits this loses LSBs the
 * same way real IBM hardware would.
 *
 * Build & test (standalone):
 *   cc test/float_literal/string_to_ibm.c -o /tmp/sti && /tmp/sti
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

typedef struct {
    uint8_t  sign;     /* 0 or 1 */
    int16_t  exp;      /* biased exponent; valid 0..127, in-flight may go negative */
    uint64_t mant;     /* mantissa in lower 56 bits; normalized in [2^52, 2^56); 0 means value 0 */
} ibmdp_t;

static const ibmdp_t IBM_ZERO = {0, 0, 0};

/* Renormalize {sign, exp, mant} where mant may be too large or too small.
 * Truncates on right-shift (matching S/360 hex FP). */
static ibmdp_t ibm_normalize(ibmdp_t r) {
    if (r.mant == 0) return IBM_ZERO;
    while (r.mant >= ((uint64_t)1 << 56)) { r.mant >>= 4; r.exp += 1; }
    while (r.mant <  ((uint64_t)1 << 52)) { r.mant <<= 4; r.exp -= 1; }
    return r;
}

/* Build IBM DP from a uint64_t value (treated as exact integer ≥ 0). */
static ibmdp_t ibm_from_u64(uint64_t v) {
    if (v == 0) return IBM_ZERO;
    /* value = m / 2^56 * 16^(exp - 64); m in [2^52, 2^56) implies exp - 64 = 14 - shift_count.
     * Initialize as if v occupies bits 0..63 of an integer "fraction * 16^14 / 2^56"... easier:
     * treat raw v as the mantissa, exp=78, and renormalize. (78 = 64 + 14: factor 16^14 cancels 2^56.) */
    ibmdp_t r = { 0, 78, v };
    /* If v has > 56 useful bits, the high ones are dropped on right-shift (truncation). */
    return ibm_normalize(r);
}

/* IBM DP multiply with truncation (matches MD on S/360). */
static ibmdp_t ibm_mul(ibmdp_t a, ibmdp_t b) {
    if (a.mant == 0 || b.mant == 0) return IBM_ZERO;
    __uint128_t p = (__uint128_t)a.mant * (__uint128_t)b.mant;
    /* p in [2^104, 2^112).  Want top 56 bits as new mantissa. */
    int e = a.exp + b.exp - 64;
    /* Normalize the 128-bit product first (avoids dropping a real digit when
     * the product's leading hex digit is zero). */
    while (p < ((__uint128_t)1 << 108)) { p <<= 4; e -= 1; }
    uint64_t m = (uint64_t)(p >> 56);    /* truncate */
    ibmdp_t r = { (uint8_t)(a.sign ^ b.sign), (int16_t)e, m };
    return ibm_normalize(r);
}

/* IBM DP divide with truncation (matches DD on S/360). */
static ibmdp_t ibm_div(ibmdp_t a, ibmdp_t b) {
    if (a.mant == 0) return IBM_ZERO;
    /* assume b.mant != 0 — caller guarantees */
    /* Compute 56-bit fractional quotient: q = (a.mant << 56) / b.mant.
     * a.mant ∈ [2^52, 2^56), b.mant ∈ [2^52, 2^56) → q ∈ [2^52, 2^60). */
    __uint128_t num = (__uint128_t)a.mant << 56;
    uint64_t q = (uint64_t)(num / b.mant);  /* truncating divide */
    int e = a.exp - b.exp + 64;
    ibmdp_t r = { (uint8_t)(a.sign ^ b.sign), (int16_t)e, q };
    return ibm_normalize(r);
}

/* IBM DP add (assumes both operands non-negative — sufficient for digit accumulation). */
static ibmdp_t ibm_add(ibmdp_t a, ibmdp_t b) {
    if (a.mant == 0) return b;
    if (b.mant == 0) return a;
    /* Align so a has the larger exponent. */
    if (a.exp < b.exp) { ibmdp_t t = a; a = b; b = t; }
    int diff = a.exp - b.exp;
    if (diff >= 14) return a;          /* b too small to matter */
    uint64_t bm = b.mant >> (4 * diff); /* truncating right-shift */
    ibmdp_t r = { a.sign, a.exp, a.mant + bm };
    return ibm_normalize(r);
}

/* Convert ibmdp_t to wire format (msw/lsw). */
static void ibm_pack(ibmdp_t a, uint32_t *msw, uint32_t *lsw) {
    if (a.mant == 0 || a.exp < 0) { *msw = 0; *lsw = 0; return; }
    if (a.exp > 127) { *msw = 0xFF000000u; *lsw = 0; return; }
    *msw = ((uint32_t)a.sign << 31)
         | ((uint32_t)(a.exp & 0x7F) << 24)
         | (uint32_t)((a.mant >> 32) & 0x00FFFFFFu);
    *lsw = (uint32_t)(a.mant & 0xFFFFFFFFu);
}

/* Parse a HAL/S decimal-literal string s into IBM DP hex (msw, lsw),
 * matching the truncating S/360 hex FP behavior of XXXTOD.bal. */
void stringToFloatIBM(const char *s, uint32_t *msw, uint32_t *lsw) {
    uint8_t sign = 0;
    if (*s == '+') s++;
    else if (*s == '-') { sign = 1; s++; }

    /* Mirror XXXTOD's digit loop in IBM-DP arithmetic so per-digit
     * truncation matches the original compiler. */
    ibmdp_t F0  = IBM_ZERO;
    ibmdp_t TEN = ibm_from_u64(10);
    int frac_digits = 0;
    bool seen_dot = false;

    while (*s) {
        if (*s >= '0' && *s <= '9') {
            F0 = ibm_mul(F0, TEN);
            if (*s != '0') F0 = ibm_add(F0, ibm_from_u64((uint64_t)(*s - '0')));
            if (seen_dot) frac_digits++;
            s++;
        } else if (*s == '.' && !seen_dot) {
            seen_dot = true;
            s++;
        } else {
            break;   /* hit E, H, B, or end */
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
    /* TODO: handle 'H' (hex) and 'B' (binary) suffixes if HAL ever uses
     * them in scalar literals; XXXTOD does, but our SCAN.xpl/MONITOR10
     * path may filter them out before we get here. */

    dec_exp -= frac_digits;

    if (F0.mant == 0) { *msw = 0; *lsw = 0; return; }
    F0.sign = sign;

    if (dec_exp != 0) {
        int absexp = dec_exp < 0 ? -dec_exp : dec_exp;
        /* F4 = 10^absexp via squaring (matching XXXTOD's TIMES10A/B/C). */
        ibmdp_t F4 = ibm_from_u64(1);
        ibmdp_t F2 = ibm_from_u64(10);
        while (absexp > 0) {
            if (absexp & 1) F4 = ibm_mul(F4, F2);
            absexp >>= 1;
            if (absexp) F2 = ibm_mul(F2, F2);
        }
        F0 = (dec_exp > 0) ? ibm_mul(F0, F4) : ibm_div(F0, F4);
    }

    ibm_pack(F0, msw, lsw);
}

/* ----------------------------------------------------------------------- */
#ifdef STANDALONE_TEST
struct test { const char *lit; uint32_t want_msw, want_lsw; };

static const struct test TESTS[] = {
    /* The issue ticket case */
    {"1.0E-8",                  0x3A2AF31D, 0xC4611873},
    /* More truncation discriminators */
    {"1E-9",                    0x3944B82F, 0xA09B5A52},
    {"1E-10",                   0x386DF37F, 0x675EF6EA},
    {"1.5E-7",                  0x3B2843EB, 0xE81B06EC},
    {"0.1",                     0x40199999, 0x99999999},
    {"0.3",                     0x404CCCCC, 0xCCCCCCCC},
    {"3.14159",                 0x413243F3, 0xE0370CDC},
    {"2.718281828",             0x412B7E15, 0x16092336},
    {"1.41421356",              0x4116A09E, 0x65DC27DE},
    {"9.81",                    0x419CF5C2, 0x8F5C28F5},
    /* Exact representations — must be unchanged */
    {"0.0",                     0x00000000, 0x00000000},
    {"1.0",                     0x41100000, 0x00000000},
    {"0.5",                     0x40800000, 0x00000000},
    {"0.25",                    0x40400000, 0x00000000},
    {"2.0",                     0x41200000, 0x00000000},
    {"-2.5",                    0xC1280000, 0x00000000},
    {"16.0",                    0x42100000, 0x00000000},
    {"256.0",                   0x43100000, 0x00000000},
    {"1E10",                    0x492540BE, 0x40000000},
    {"100000000",               0x475F5E10, 0x00000000},
    {"3E-8",                    0x3A80D959, 0x4D23495B},
    /* Values from the rburkey discrepancy report — these are MATCHING
     * (not differing) cases, so any fix must not break them. */
    {"6.022E23",                0x547F8553, 0xD15FA84A},
    {"1.602E-19",               0x312F485E, 0xA92C8FF6},
};

int main(void) {
    int n = sizeof(TESTS)/sizeof(TESTS[0]);
    int fails = 0;
    for (int i = 0; i < n; i++) {
        uint32_t msw, lsw;
        stringToFloatIBM(TESTS[i].lit, &msw, &lsw);
        bool ok = (msw == TESTS[i].want_msw) && (lsw == TESTS[i].want_lsw);
        if (!ok) fails++;
        printf("  [%s]  %-22s -> %08X_%08X   (want %08X_%08X)\n",
               ok ? "PASS" : "FAIL",
               TESTS[i].lit, msw, lsw,
               TESTS[i].want_msw, TESTS[i].want_lsw);
    }
    printf("\n%s — %d/%d passed\n", fails ? "FAIL" : "OK", n - fails, n);
    return fails;
}
#endif
