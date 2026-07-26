// Quick smoke test of the new exception-aware API.  Not a real test
// suite — refgen + the Node-side runner are.  Use to catch silly
// bugs in the new code before plumbing.
//
// Build:  cc -std=c11 -Wall -Wextra smoke.c ibmFloat.c -o smoke && ./smoke
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "ibmFloat.h"

static int failures = 0;

static const char *
exc_name(ibm_fp_exc_t e) {
    switch (e) {
    case IBM_FP_OK:               return "OK";
    case IBM_FP_EXP_OVERFLOW:     return "EXP_OVERFLOW";
    case IBM_FP_EXP_UNDERFLOW:    return "EXP_UNDERFLOW";
    case IBM_FP_SIGNIFICANCE:     return "SIGNIFICANCE";
    case IBM_FP_DIVIDE:           return "DIVIDE";
    case IBM_FP_CONVERT_OVERFLOW: return "CONVERT_OVERFLOW";
    }
    return "?";
}

#define CHECK_DP(op, a, b, want_exc, want_r)                                \
    do {                                                                    \
        uint64_t r = 0;                                                     \
        ibm_fp_exc_t e = ibm_dp_##op##_exc(a, b, &r);                       \
        if (e != want_exc || r != want_r) {                                 \
            printf("FAIL ibm_dp_" #op "_exc(%016" PRIX64 ", %016" PRIX64 ")\n" \
                   "  want exc=%s result=%016" PRIX64 "\n"                  \
                   "  got  exc=%s result=%016" PRIX64 "\n",                 \
                   (uint64_t)a, (uint64_t)b,                                \
                   exc_name(want_exc), (uint64_t)want_r,                    \
                   exc_name(e), (uint64_t)r);                               \
            failures++;                                                     \
        }                                                                   \
    } while (0)

#define CHECK_SP(op, a, b, want_exc, want_r)                                \
    do {                                                                    \
        uint32_t r = 0;                                                     \
        ibm_fp_exc_t e = ibm_sp_##op##_exc(a, b, &r);                       \
        if (e != want_exc || r != want_r) {                                 \
            printf("FAIL ibm_sp_" #op "_exc(%08" PRIX32 ", %08" PRIX32 ")\n" \
                   "  want exc=%s result=%08" PRIX32 "\n"                   \
                   "  got  exc=%s result=%08" PRIX32 "\n",                  \
                   (uint32_t)a, (uint32_t)b,                                \
                   exc_name(want_exc), (uint32_t)want_r,                    \
                   exc_name(e), (uint32_t)r);                               \
            failures++;                                                     \
        }                                                                   \
    } while (0)

#define CHECK_CC(label, got, want) do { \
    if ((got) != (want)) { \
        printf("FAIL cc %s: want %d got %d\n", label, (int)(want), (int)(got)); \
        failures++; \
    } \
} while (0)

int
main(void) {
    // ---- DP: trivial OK cases ----
    // 1.0 + 1.0 = 2.0
    //   1.0 = 0x4110000000000000, 2.0 = 0x4120000000000000
    CHECK_DP(add, 0x4110000000000000ULL, 0x4110000000000000ULL,
             IBM_FP_OK, 0x4120000000000000ULL);
    // 1.0 - 1.0 = significance, *result = 0
    CHECK_DP(sub, 0x4110000000000000ULL, 0x4110000000000000ULL,
             IBM_FP_SIGNIFICANCE, 0);
    // 2.0 * 0.5 = 1.0
    //   0.5 = 0x4080000000000000
    CHECK_DP(mul, 0x4120000000000000ULL, 0x4080000000000000ULL,
             IBM_FP_OK, 0x4110000000000000ULL);
    // 1.0 / 2.0 = 0.5
    CHECK_DP(div, 0x4110000000000000ULL, 0x4120000000000000ULL,
             IBM_FP_OK, 0x4080000000000000ULL);
    // X / 0 -> FP_DIVIDE
    {
        uint64_t r = 0;
        ibm_fp_exc_t e = ibm_dp_div_exc(0x4110000000000000ULL, 0, &r);
        if (e != IBM_FP_DIVIDE || r != IBM_FP_DIVIDE_SENTINEL_DP) {
            printf("FAIL ibm_dp_div_exc by zero\n");
            failures++;
        }
    }
    // Mul overflow: 16^60 × 16^60 = 16^120, char would be 64+120=184>127.
    //   Operand 0x7C10000000000000: char 0x7C=124, value=0.1×16^60.
    {
        uint64_t r = 0;
        ibm_fp_exc_t e = ibm_dp_mul_exc(0x7C10000000000000ULL,
                                        0x7C10000000000000ULL, &r);
        if (e != IBM_FP_EXP_OVERFLOW) {
            printf("FAIL mul overflow: exc=%s result=%016llX (want EXP_OVERFLOW)\n",
                   exc_name(e), (unsigned long long)r);
            failures++;
        }
    }
    // Mul that does NOT overflow: 16^31 × 16^31 = 16^62. Verify expected
    // packed form (0.1×16^63 = 0x7F10...) for sanity.
    CHECK_DP(mul, 0x6010000000000000ULL, 0x6010000000000000ULL,
             IBM_FP_OK, 0x7F10000000000000ULL);
    // Underflow: 16^-32 * 16^-32 -> exp -64 -> underflow (true zero result)
    //   16^-32: char = 32, exp = -32. char = bias + (-32) = 64 - 32 = 32 = 0x20.
    CHECK_DP(mul, 0x2010000000000000ULL, 0x2010000000000000ULL,
             IBM_FP_EXP_UNDERFLOW, 0);

    // ---- SP smoke ----
    // 1.0 (SP) = 0x41100000
    CHECK_SP(add, 0x41100000U, 0x41100000U, IBM_FP_OK, 0x41200000U);
    CHECK_SP(sub, 0x41100000U, 0x41100000U, IBM_FP_SIGNIFICANCE, 0);
    CHECK_SP(mul, 0x41200000U, 0x40800000U, IBM_FP_OK, 0x41100000U);
    CHECK_SP(div, 0x41100000U, 0x41200000U, IBM_FP_OK, 0x40800000U);
    // SP /0
    {
        uint32_t r = 0;
        ibm_fp_exc_t e = ibm_sp_div_exc(0x41100000U, 0, &r);
        if (e != IBM_FP_DIVIDE || r != IBM_FP_DIVIDE_SENTINEL_SP) {
            printf("FAIL ibm_sp_div_exc by zero\n");
            failures++;
        }
    }
    // SP mul overflow: (0.1×16^60)² = 16^120, char would be 64+120>127
    {
        uint32_t r = 0;
        ibm_fp_exc_t e = ibm_sp_mul_exc(0x7C100000U, 0x7C100000U, &r);
        if (e != IBM_FP_EXP_OVERFLOW) {
            printf("FAIL sp mul overflow: exc=%s result=%08X\n",
                   exc_name(e), r);
            failures++;
        }
    }
    // SP mul underflow: (0.1×16^-32)²
    CHECK_SP(mul, 0x20100000U, 0x20100000U, IBM_FP_EXP_UNDERFLOW, 0);
    // SP add overflow: 0.F × 16^63 + 0.F × 16^63 -> carry to char 0x80.
    // The 28-bit guarded sum is 0x1E000000 (carry into bit 28); after
    // >>8 to drop guard+absorb-overflow we get 0x1E0000 in the SP frac
    // slot.  Char wraps from 0x80 to 0x00 via the &0x7F mask in pack.
    CHECK_SP(add, 0x7FF00000U, 0x7FF00000U, IBM_FP_EXP_OVERFLOW,
             0x001E0000U);

    // ---- Quasi-extended multiply (POO §8.17) ----
    // Normal case agrees with full mul: 0.5 × 0.5 = 0.25
    {
        uint64_t r_full = 0, r_qe = 0;
        ibm_fp_exc_t e_full = ibm_dp_mul_exc(0x4080000000000000ULL,
                                             0x4080000000000000ULL, &r_full);
        ibm_fp_exc_t e_qe   = ibm_dp_mul_qe_exc(0x4080000000000000ULL,
                                                0x4080000000000000ULL, &r_qe);
        if (e_full != IBM_FP_OK || e_qe != IBM_FP_OK || r_full != r_qe) {
            printf("FAIL mul vs mul_qe (0.5²): full=%016llX qe=%016llX\n",
                   (unsigned long long)r_full, (unsigned long long)r_qe);
            failures++;
        }
    }
    // POO §8.17 programming note: "exponent overflow will be caused by
    // rounding a floating point number like 7FFFFFFFFF000000."  Squaring
    // forces rounding-induced char bump from 0x7F to 0x80 → overflow.
    {
        uint64_t r = 0;
        ibm_fp_exc_t e = ibm_dp_mul_qe_exc(0x7FFFFFFFFF000000ULL,
                                           0x7FFFFFFFFF000000ULL, &r);
        if (e != IBM_FP_EXP_OVERFLOW) {
            printf("FAIL mul_qe rounding overflow: exc=%s result=%016llX\n",
                   exc_name(e), (unsigned long long)r);
            failures++;
        }
    }

    // ---- DP compare ----
    CHECK_CC("1.0 == 1.0", ibm_dp_cmp(0x4110000000000000ULL, 0x4110000000000000ULL), 0);
    CHECK_CC("2.0 >  1.0", ibm_dp_cmp(0x4120000000000000ULL, 0x4110000000000000ULL), 1);
    CHECK_CC("1.0 <  2.0", ibm_dp_cmp(0x4110000000000000ULL, 0x4120000000000000ULL), 3);
    // Different-sign zero compare equal
    CHECK_CC("+0 == -0", ibm_dp_cmp(0, 0x8000000000000000ULL), 0);

    // ---- SP compare ----
    CHECK_CC("SP 1.0 == 1.0", ibm_sp_cmp(0x41100000U, 0x41100000U), 0);
    CHECK_CC("SP 2.0 >  1.0", ibm_sp_cmp(0x41200000U, 0x41100000U), 1);
    CHECK_CC("SP 1.0 <  2.0", ibm_sp_cmp(0x41100000U, 0x41200000U), 3);

    // ---- POO §8.11 anomaly — all three example cases ----
    // ex.1: equal exponents, low-half fractional diff
    {
        uint64_t a = 0x423FFFFF00001234ULL;
        uint64_t b = 0x423FFFFF00801234ULL;
        CHECK_CC("anomaly ex1 correct",  ibm_dp_cmp(a, b), 3);
        CHECK_CC("anomaly ex1 faithful", ibm_dp_cmp_anomalous(a, b), 0);
    }
    // ex.2: exp diff 1, both negative (b has higher characteristic).
    //   a = BEFFFFFFFB076890, b = BF10000000307689.
    //   POO says correct CC = 01 (OP1 > OP2): -|a| > -|b| because
    //   |a| < |b| (when prealigned, a's mantissa shifted to b's exp is
    //   slightly less than b's), so a (less negative) > b.
    {
        uint64_t a = 0xBEFFFFFFFB076890ULL;
        uint64_t b = 0xBF10000000307689ULL;
        CHECK_CC("anomaly ex2 correct",  ibm_dp_cmp(a, b), 1);
        CHECK_CC("anomaly ex2 faithful", ibm_dp_cmp_anomalous(a, b), 0);
    }
    // ex.3: exp diff 1, a has higher exp
    //   a = 4010000000001234, b = 3FFFFFFFF8012340
    //   correct a > b -> CC=1
    {
        uint64_t a = 0x4010000000001234ULL;
        uint64_t b = 0x3FFFFFFFF8012340ULL;
        CHECK_CC("anomaly ex3 correct",  ibm_dp_cmp(a, b), 1);
        CHECK_CC("anomaly ex3 faithful", ibm_dp_cmp_anomalous(a, b), 0);
    }

    // ---- CVFL / CVFX round-trip on small values ----
    {
        int32_t v = 12345;
        uint32_t fp = ibm_cvfl(v);
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(fp, &back);
        if (e != IBM_FP_OK || back != v) {
            printf("FAIL cvfl/cvfx round-trip 12345: fp=%08X back=%d exc=%s\n",
                   fp, back, exc_name(e));
            failures++;
        }
    }
    {
        int32_t v = -67890;
        uint32_t fp = ibm_cvfl(v);
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(fp, &back);
        if (e != IBM_FP_OK || back != v) {
            printf("FAIL cvfl/cvfx round-trip -67890: fp=%08X back=%d exc=%s\n",
                   fp, back, exc_name(e));
            failures++;
        }
    }

    // CVFX bounds: 0x41100000 (=1.0 * 16) -> integer 16 = 0x00000010,
    // and POO §8.13 ANOMALY says 0x41100000 yields CC=00.  Here we just
    // check the value (CC is set in the CPU layer, not here).
    {
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(0x41100000U, &back);
        if (e != IBM_FP_OK || back != 0x00010000) {
            printf("FAIL cvfx 0x41100000: back=0x%08X exc=%s (want 0x00010000 OK)\n",
                   (uint32_t)back, exc_name(e));
            failures++;
        }
    }
    // CVFX positive overflow: 0x44800000 = 0.8×16^4 = 32768 → would scale
    // to 32768×2^16 = 0x80000000.  As signed int32, that's INT32_MIN —
    // doesn't fit a positive int.  Convert overflow.
    {
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(0x44800000U, &back);
        if (e != IBM_FP_CONVERT_OVERFLOW) {
            printf("FAIL cvfx pos-overflow: exc=%s back=0x%08X\n",
                   exc_name(e), (uint32_t)back);
            failures++;
        }
    }
    // CVFX negative-overflow boundary: 0xC4800000 = -0.8×16^4 = -32768
    // → -32768×2^16 = -0x80000000 = INT32_MIN.  This DOES fit (just barely).
    {
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(0xC4800000U, &back);
        if (e != IBM_FP_OK || back != INT32_MIN) {
            printf("FAIL cvfx neg INT32_MIN: exc=%s back=%d (want %d OK)\n",
                   exc_name(e), back, INT32_MIN);
            failures++;
        }
    }
    // CVFX char too high to even shift: 0x4F100000 (chr=0x4F, way past 0x44).
    {
        int32_t back = 0;
        ibm_fp_exc_t e = ibm_cvfx(0x4F100000U, &back);
        if (e != IBM_FP_CONVERT_OVERFLOW) {
            printf("FAIL cvfx chr-too-high: exc=%s back=0x%08X\n",
                   exc_name(e), (uint32_t)back);
            failures++;
        }
    }

    if (failures == 0) {
        printf("smoke PASS (%d failures)\n", failures);
        return 0;
    }
    printf("smoke FAIL (%d failures)\n", failures);
    return 1;
}
