/*
 * unit test for ext/virtualagc/XCOM-I/ibmFloat.c.
 *
 *
 * Specific regressions checked:
 *   - virtualagc issue #1296:  "1.0E-8" must convert to 0x3A2AF31D_C4611873.
 *   - FIXER + true zero (NORMAL) must become "true" zero
 *   - AW with FIXER must align mantissa to exp 0x4E and leave the result
 *     unnormalized (the integer-extraction trick used by PREP_LITERAL).
 */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "ibmFloat.h"

static int failures = 0;
static int checks   = 0;

static void
expect_pkd(uint64_t actual, uint64_t expected, const char *what) {
    checks++;
    if (actual != expected) {
        printf("FAIL %s\n  expected 0x%016" PRIX64 "\n  actual   0x%016" PRIX64 "\n",
               what, expected, actual);
        failures++;
    }
}

static void
expect_str(const char *s, uint64_t expected) {
    uint32_t msw, lsw;
    ibm_dp_from_string(s, &msw, &lsw);
    uint64_t got = ((uint64_t)msw << 32) | lsw;
    char buf[80];
    snprintf(buf, sizeof(buf), "ibm_dp_from_string(\"%s\")", s);
    expect_pkd(got, expected, buf);
}

int
main(void) {
    /* ibm_dp_from_string */
    /* virtualagc issue #1296: truncating S/360 conversion, NOT atof+llround. */
    expect_str("1.0E-8",       0x3A2AF31DC4611873ULL);
    expect_str("1.0E-9",       0x3944B82FA09B5A52ULL);
    expect_str("1.0E-10",      0x386DF37F675EF6EAULL);
    expect_str("1.5E-7",       0x3B2843EBE81B06ECULL);

    /* Exact representations (no truncation). */
    expect_str("0",            0x0000000000000000ULL);
    expect_str("0.0",          0x0000000000000000ULL);
    expect_str("1.0",          0x4110000000000000ULL);
    expect_str("-1.0",         0xC110000000000000ULL);
    expect_str("0.5",          0x4080000000000000ULL);  /* 8/16        */
    expect_str("0.25",         0x4040000000000000ULL);  /* 4/16        */
    expect_str("0.0625",       0x4010000000000000ULL);  /* 1/16        */
    expect_str("16.0",         0x4210000000000000ULL);  /* 1/16 * 16^2 */
    expect_str("100",          0x4264000000000000ULL);  /* 0x64/16^6*16^2 */
    expect_str("256.0",        0x4310000000000000ULL);  /* 1/16 * 16^3 */

    /* Sign handling. */
    expect_str("+1.0",         0x4110000000000000ULL);
    expect_str("-0.5",         0xC080000000000000ULL);

    /* No-decimal-point and pure-fraction forms. */
    expect_str("3",            0x4130000000000000ULL);
    expect_str(".5",           0x4080000000000000ULL);

    /* Sub-1.0 fractional (PREP_LITERAL drives AW with FIXER for these).
     * S/360 hex DP truncates the mantissa rather than rounding, so the
     * last hex digit of these is one below the IEEE-rounded value. */
    expect_str("0.1",          0x4019999999999999ULL);  /* 0.0999... */
    expect_str("0.015",        0x3F3D70A3D70A3D70ULL);  /* 0.0149... */

    /* ibm_dp_addsub / ibm_dp_add / ibm_dp_sub */
    expect_pkd(ibm_dp_add(0x4E00000000000000ULL, 0),
               0x0000000000000000ULL,
               "ibm_dp_add(FIXER-zero, 0) canonicalizes to true zero");

    /* Cancellation: a - a = +0 (true zero). */
    expect_pkd(ibm_dp_sub(0x4110000000000000ULL, 0x4110000000000000ULL),
               0x0000000000000000ULL,
               "ibm_dp_sub(1.0, 1.0) = +0");
    expect_pkd(ibm_dp_sub(0xC130000000000000ULL, 0xC130000000000000ULL),
               0x0000000000000000ULL,
               "ibm_dp_sub(-3.0, -3.0) = +0");

    /* Same-exp add. */
    expect_pkd(ibm_dp_add(0x4110000000000000ULL, 0x4110000000000000ULL),
               0x4120000000000000ULL,
               "ibm_dp_add(1.0, 1.0) = 2.0");

    /* Same-exp sub. */
    expect_pkd(ibm_dp_sub(0x4118000000000000ULL, 0x4110000000000000ULL),
               0x4080000000000000ULL,
               "ibm_dp_sub(1.5, 1.0) = 0.5");

    /* Different-exp add (alignment + guard digit). */
    expect_pkd(ibm_dp_add(0x4110000000000000ULL, 0x4080000000000000ULL),
               0x4118000000000000ULL,
               "ibm_dp_add(1.0, 0.5) = 1.5");

    /* Sign flip on cancellation when |b| > |a|. */
    expect_pkd(ibm_dp_sub(0x4110000000000000ULL, 0x4120000000000000ULL),
               0xC110000000000000ULL,
               "ibm_dp_sub(1.0, 2.0) = -1.0");

    /* Adding a true zero leaves the value normalized. */
    expect_pkd(ibm_dp_add(0x4118000000000000ULL, 0),
               0x4118000000000000ULL,
               "ibm_dp_add(1.5, 0) = 1.5");
    expect_pkd(ibm_dp_add(0, 0x4118000000000000ULL),
               0x4118000000000000ULL,
               "ibm_dp_add(0, 1.5) = 1.5");

    /* ibm_dp_addsub - UNNORMAL AW  */
    /* AW with FIXER on 1.5: aligns 0x18000000_00000000 down 13 hex digits
     * (mantissa shift right 52 bits), giving lsw = 1 at exp 0x4E. */
    expect_pkd(ibm_dp_addsub(0x4118000000000000ULL,
                             0x4E00000000000000ULL, 0, 0),
               0x4E00000000000001ULL,
               "AW(1.5, FIXER) = 0x4E000000_00000001");

    /* AW with FIXER on 0.5: shift past the right edge → 0 at exp 0x4E. */
    expect_pkd(ibm_dp_addsub(0x4080000000000000ULL,
                             0x4E00000000000000ULL, 0, 0),
               0x4E00000000000000ULL,
               "AW(0.5, FIXER) = 0x4E000000_00000000");

    /* AW with FIXER on -1.5: result preserves sign (signed unnormalized add). */
    expect_pkd(ibm_dp_addsub(0xC118000000000000ULL,
                             0x4E00000000000000ULL, 0, 0),
               0xCE00000000000001ULL,
               "AW(-1.5, FIXER) = 0xCE000000_00000001");

    /* AW with FIXER on integer 100 (= 0x42_64...): mantissa shifts
     * 12 hex digits to put the integer 100 = 0x64 in the lsw. */
    expect_pkd(ibm_dp_addsub(0x4264000000000000ULL,
                             0x4E00000000000000ULL, 0, 0),
               0x4E00000000000064ULL,
               "AW(100, FIXER) = 0x4E000000_00000064");

    /* ibm_dp_from_double / ibm_dp_to_double round-trip */
    {
        struct { double d; uint64_t bits; const char *name; } cases[] = {
            { 1.0,    0x4110000000000000ULL, "1.0" },
            { -1.0,   0xC110000000000000ULL, "-1.0" },
            { 0.5,    0x4080000000000000ULL, "0.5" },
            { 100.0,  0x4264000000000000ULL, "100.0" },
            { 0.0,    0x0000000000000000ULL, "0.0" },
        };
        for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
            uint32_t m, l;
            ibm_dp_from_double(&m, &l, cases[i].d);
            uint64_t got = ((uint64_t)m << 32) | l;
            char buf[80];
            snprintf(buf, sizeof(buf), "ibm_dp_from_double(%s)", cases[i].name);
            expect_pkd(got, cases[i].bits, buf);
            /* Round-trip back. */
            double back = ibm_dp_to_double(m, l);
            checks++;
            if (back != cases[i].d) {
                printf("FAIL ibm_dp_to_double round-trip %s: got %g, expected %g\n",
                       cases[i].name, back, cases[i].d);
                failures++;
            }
        }
    }

    /* IBM_DP_PACK / IBM_DP_SIGN / IBM_DP_EXP / IBM_DP_MANT macros */
    {
        uint64_t v = IBM_DP_PACK(1, 0x42, 0x64ULL << 48);  /* -100.0 */
        expect_pkd(v, 0xC264000000000000ULL, "IBM_DP_PACK(-100.0)");
        checks++;
        if (IBM_DP_SIGN(v) != 1)        { printf("FAIL IBM_DP_SIGN\n");  failures++; }
        checks++;
        if (IBM_DP_EXP(v) != 0x42)      { printf("FAIL IBM_DP_EXP\n");   failures++; }
        checks++;
        if (IBM_DP_MANT(v) != 0x64ULL << 48) { printf("FAIL IBM_DP_MANT\n");  failures++; }
        checks++;
        if (IBM_DP_IS_TRUE_ZERO(v))     { printf("FAIL IS_TRUE_ZERO(-100)\n"); failures++; }
        checks++;
        if (!IBM_DP_IS_TRUE_ZERO(0))    { printf("FAIL IS_TRUE_ZERO(0)\n"); failures++; }
        checks++;
        if (!IBM_DP_IS_TRUE_ZERO(IBM_DP_SIGN_BIT)) {
            printf("FAIL IS_TRUE_ZERO(-0)\n"); failures++;
        }
        checks++;
        if (IBM_DP_IS_TRUE_ZERO(0x4E00000000000000ULL)) {
            printf("FAIL IS_TRUE_ZERO(FIXER)\n"); failures++;
        }
    }

    printf("%d checks, %d failure(s)\n", checks, failures);
    return failures ? 1 : 0;
}
