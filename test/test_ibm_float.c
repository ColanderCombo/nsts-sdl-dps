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

    /* XXXTOD-faithful chunk-by-23 + Assembler-H 1E23 = 0x54152D02C7E14AF7
     * (round-half-up of true 10^23, which lands exactly at the 0.5
     * mantissa boundary).  Reverting either piece — using a single
     * repeated-square build of 10^|exp|, or building 1E23 as the
     * truncating chained-multiply 0x54152D02C7E14AF6 — regresses these
     * cases by 3-7 ULPs each in the literalSCALARS-exp2 dataset.
     * Source: MONITOR.ASM/XXXTOD.bal:1530-1700 (TIMES10A loop). */
    expect_str("5.3101057542E-13",   0x369577582F69A8C8ULL);
    expect_str("-3.72836339327E-13", 0xB668F1B0886F02ECULL);
    expect_str("-2.30211679566E-15", 0xB4A5E28F320AB7EBULL);
    /* 1E23 itself: exposes the round-half-up of the constant directly. */
    expect_str("1E23",         0x54152D02C7E14AF7ULL);

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

    /* ibm_dp_to_string — direct IBM hex -> decimal, no IEEE roundtrip.
     * Spot checks against MAFGEN's compiled-image output (rburkey's
     * literalSCALARS.txt, virtualagc issue #1296).  Full 125-case
     * survey lives in test_literal_scalars; these are the gates. */
    {
        struct { uint32_t msw, lsw; int sig, pad; const char *want;
                 const char *desc; } cases[] = {
            /* True zero. */
            { 0,          0,          16, 17, "0.0",
              "to_string(+0)" },
            { 0x80000000u,0,          16, 17, "0.0",
              "to_string(-0)" },
            /* SP: 8 sig digs, no pad.  Truncating gives MAFGEN's value. */
            { 0x41100000u,0,           8,  8, "1.0000000E+00",
              "to_string SP 1.0" },
            { 0x40B33333u,0,           8,  8, "6.9999998E-01",
              "to_string SP 0x40B33333 (CGCK_DBFE_INNER, ~0.7)" },
            { 0xBEC49BA5u,0,           8,  8, "-2.9999997E-03",
              "to_string SP 0xBEC49BA5 (A3_TOL, ~-0.003)" },
            /* DP: 17 sig digs.  XXDTOC's port produces the actual 17th
             * digit from the scaled mantissa (or an implicit '0' when
             * the mantissa happens to be only 16 digits). */
            { 0x3E2235B4u,0xEDB2F661u,17, 17, "5.2199999999999990E-04",
              "to_string DP AIBK_PT522 (issue #1296: "
              "0.000522 -> 3E2235B4_EDB2F661 -> matches MAFGEN)" },
            { 0x3F1273D5u,0xBAB21815u,17, 17, "4.5049999999999990E-03",
              "to_string DP AIBK_DELTA (0.004505)" },
            { 0x40555555u,0x55555555u,17, 17, "3.3333333333333332E-01",
              "to_string DP ATHIRD (= 1/3 truncated, 17-digit mantissa "
              "matches MAFGEN's non-zero 17th digit)" },
            /* 7e75 with characteristic 0x7F.  Until 2026-05, our TENSTBL
             * entries for 10^30..10^70 were chained-multiply truncations
             * (1-4 ULPs below true) and the renderer produced …14E+75.
             * After repairing TENSTBL to the IBM Assembler-H round-half-up
             * values that XXDTOC actually sees, the scaling produces
             * MAFGEN's …07E+75 bit-for-bit. */
            { 0x7FF79DC0u,0xE8C518FFu,17, 17, "7.0000000000000007E+75",
              "to_string DP near max characteristic (=0x7F, "
              "exercises TENSTBL[10^70])" },
        };
        for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
            char buf[40];
            ibm_dp_to_string(cases[i].msw, cases[i].lsw,
                             cases[i].sig, cases[i].pad,
                             buf, sizeof(buf));
            checks++;
            if (strcmp(buf, cases[i].want) != 0) {
                printf("FAIL %s\n  expected \"%s\"\n  actual   \"%s\"\n",
                       cases[i].desc, cases[i].want, buf);
                failures++;
            }
        }
    }

    /* ----------------------------------------------------------------
     * Compile-time semantics of HAL/S-FC scalar literals.
     *
     * These three blocks test the *rules* the test_literal_scalars_v2
     * harness depends on, independently of the vagc#1296 dataset.
     * Source citations point to the PASS1/PASS2 .xpl files we verified
     * the rules against.  See memory/halfc_compile_time_arithmetic.md.
     * ---------------------------------------------------------------- */

    /* Rule 1: SP-from-literal is "convert as DP, then take the high 32".
     * That's exactly what IBM /360 STE ("70") does at codegen time —
     * see PASS2.PROCS/GENERATE.xpl:3166.  Any future "round before
     * truncate" patch must fail this test. */
    {
        uint32_t msw, lsw;
        ibm_dp_from_string(".017453293", &msw, &lsw);
        checks++;
        if (msw != 0x3F477D1Au) {
            printf("FAIL STE truncation of .017453293\n"
                   "  expected high-32 0x3F477D1A\n"
                   "  actual   high-32 0x%08X\n", msw);
            failures++;
        }
        /* Low 32 must NOT be zero — i.e., the DP parse really does
         * carry sub-SP precision that STE then drops. */
        checks++;
        if (lsw == 0) {
            printf("FAIL .017453293 expected non-zero LSW (DP-precise),"
                   " got 0x%08X\n", lsw);
            failures++;
        }
    }

    /* Rule 2: `SCALAR CONSTANT(literal)` keeps the DP-precise value for
     * compile-time arithmetic; only the final assignment-to-SP truncates.
     * The classic counter-example: 260 × CGMK_DEG_RAD where the SP
     * COMPOOL stores 0x3F477D1A but the in-unit literal table holds
     * 0x3F477D1AAA47B416.
     *
     *   260 × 0x3F477D1AAA47B416  -> STE -> 0x41489B0F  (matches binary)
     *   260 × 0x3F477D1A00000000  -> STE -> 0x41489B0E  (off by 1)
     *
     * Source path: PASS1.PROCS/PREPLITE.xpl:75-77 stores 8 bytes, then
     * SAVELITE.xpl:57-66 puts both halves into LIT2/LIT3, and
     * ARITHLIT.xpl:82-91 reloads them as DP for the multiply. */
    {
        const uint64_t two_sixty   = 0x4310400000000000ULL;  /* 260 in DP */
        const uint64_t deg_rad_dp  = 0x3F477D1AAA47B416ULL;  /* dp_from_string */
        const uint64_t deg_rad_sp  = 0x3F477D1A00000000ULL;  /* SP-zero-extended */
        uint64_t prod_dp = ibm_dp_mul(two_sixty, deg_rad_dp);
        uint64_t prod_sp = ibm_dp_mul(two_sixty, deg_rad_sp);
        uint32_t high_dp = (uint32_t)(prod_dp >> 32);
        uint32_t high_sp = (uint32_t)(prod_sp >> 32);
        checks++;
        if (high_dp != 0x41489B0Fu) {
            printf("FAIL DP-precise 260 * CGMK_DEG_RAD then STE\n"
                   "  expected 0x41489B0F  actual 0x%08X\n", high_dp);
            failures++;
        }
        checks++;
        if (high_sp != 0x41489B0Eu) {
            printf("FAIL SP-zero-extended 260 * CGMK_DEG_RAD then STE\n"
                   "  expected 0x41489B0E  actual 0x%08X\n", high_sp);
            failures++;
        }
        /* And the pair MUST disagree in their LSB — that's the whole
         * point of preserving DP-precise constants. */
        checks++;
        if (high_dp == high_sp) {
            printf("FAIL DP-precise and SP-zero-extended folds matched —"
                   " ibm_dp_mul lost the low-half precision somewhere\n");
            failures++;
        }
    }

    /* Rule 3: DP→INT in HALMATIN.xpl:ROUND_SCALAR uses the rounder
     * constant ADDR_ROUNDER (DP 0x407FFFFFFFFFFFFF, ≈ 0.5 - 1 ULP) per
     * PASS1.PROCS/INITIALI.xpl:506-508. Algorithm: |x| + ROUNDER, then
     * the AW/STD integer-extract trick.  Net effect: round half toward
     * zero (because the rounder is just under 0.5).
     *
     * We don't emulate the full AW+STD here — instead we verify the
     * rounding boundaries the rule produces, by checking |x| + ROUNDER
     * against the integer thresholds.  The test will fail if anyone
     * later switches to "round half away from zero" or to truncation. */
    {
        const uint64_t ROUNDER  = 0x407FFFFFFFFFFFFFULL;
        const uint64_t ONE_DP   = 0x4110000000000000ULL;  /* 1.0 */
        const uint64_t TWO_DP   = 0x4120000000000000ULL;  /* 2.0 */
        const uint64_t THREE_DP = 0x4130000000000000ULL;  /* 3.0 */
        struct {
            const char *literal;
            uint64_t    threshold;
            int         expect_below;  /* 1: |x|+ROUNDER < threshold */
            const char *desc;
        } cases[] = {
            { "2.5",      THREE_DP, 1, "round(2.5) -> 2 (half-to-zero)" },
            { "2.5000001",THREE_DP, 0, "round(2.5000001) -> 3" },
            { "2.6",      THREE_DP, 0, "round(2.6) -> 3" },
            { "0.5",      ONE_DP,   1, "round(0.5) -> 0 (half-to-zero)" },
            { "0.6",      ONE_DP,   0, "round(0.6) -> 1" },
            { "1.5",      TWO_DP,   1, "round(1.5) -> 1 (half-to-zero)" },
        };
        for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
            uint32_t msw, lsw;
            ibm_dp_from_string(cases[i].literal, &msw, &lsw);
            uint64_t x_packed = ((uint64_t)msw << 32) | lsw;
            uint64_t absx = x_packed & 0x7FFFFFFFFFFFFFFFULL;
            uint64_t plus_round = ibm_dp_add(absx, ROUNDER);
            /* For positive normalized DP values, packed bit-comparison
             * agrees with numeric comparison (exponent occupies the
             * high bits and bigger exp = bigger magnitude).  Both
             * operands here are positive. */
            int below = (plus_round < cases[i].threshold);
            checks++;
            if (below != cases[i].expect_below) {
                printf("FAIL %s\n"
                       "  |x|+ROUNDER = 0x%016" PRIX64 "\n"
                       "  threshold   = 0x%016" PRIX64 "\n"
                       "  expected %s, got %s\n",
                       cases[i].desc, plus_round, cases[i].threshold,
                       cases[i].expect_below ? "below" : "at-or-above",
                       below ? "below" : "at-or-above");
                failures++;
            }
        }
    }

    printf("%d checks, %d failure(s)\n", checks, failures);
    return failures ? 1 : 0;
}
