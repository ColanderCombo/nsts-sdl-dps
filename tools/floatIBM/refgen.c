// refgen.c — vector generator for the floatIBM compliance test.
//
// Reads no input.  Walks a hardcoded coverage matrix, calls the
// AP-101S-faithful primitives in ibmFloat.c, and emits NDJSON to
// stdout — one record per op invocation.  Consumed by the Node test
// runner ext/sim/test/test_floatIBM.coffee.
//
// Record format:
//   {"op": "<op>",
//    "kind": "dp"|"sp",
//    "a": "<hex>",
//    "b": "<hex>" | null,
//    "result": "<hex>",
//    "exc":    "<flag>",
//    "cc":     0 | 1 | 3 | null,
//    "tag":    "<short label for diagnostics>" }
//
// CC is computed per POO §8.7 based on the result and the op's CC rule:
//   add/sub/cvfl              : 00 zero, 01 >0, 11 <0
//   compare                   : op returns CC directly
//   cvfx                      : 00 hi16==0, 01 hi16>0 (sign clear), 11 hi16<0
//   mul/div/mul_qe            : null (CC unchanged per POO)
// Underflow/overflow/divide:
//   the writeback decision is the CPU layer's job (depends on PSW
//   masks).  For test-vector purposes we record the *would-be* result
//   that the math produced.  See ibmFloat.h for the contract.
//
// Build:
//   cc -std=c11 -Wall -Wextra refgen.c ibmFloat.c -o refgen -lm
//
// Run:
//   ./refgen > vectors.ndjson

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "ibmFloat.h"

static const char *
exc_name(ibm_fp_exc_t e) {
    switch (e) {
    case IBM_FP_OK:                 return "OK";
    case IBM_FP_EXP_OVERFLOW:       return "EXP_OVERFLOW";
    case IBM_FP_EXP_UNDERFLOW:      return "EXP_UNDERFLOW";
    case IBM_FP_SIGNIFICANCE:       return "SIGNIFICANCE";
    case IBM_FP_DIVIDE:             return "DIVIDE";
    case IBM_FP_CONVERT_OVERFLOW:   return "CONVERT_OVERFLOW";
    }
    return "?";
}

// ---- CC computation ----
//
// POO §8.7 condition-code rules.
//
// "load-style" CC: 00 zero, 01 positive, 11 negative.  Drives add/sub
// and CVFL.  For add/sub the CC is set on the *result* even on
// SIGNIFICANCE (true zero, CC=0) and on UNDERFLOW (true zero when
// masked off, CC=0).  On OVERFLOW the operands are unchanged so the
// CC isn't really set; we still record the computed CC for trace.
static int
cc_load_style(uint64_t result_dp, int dp /* 1 if DP, else SP */) {
    uint64_t magnitude = dp
        ? (result_dp & 0x7FFFFFFFFFFFFFFFULL)
        : ((uint32_t)result_dp & 0x7FFFFFFFU);
    if (magnitude == 0) return 0;
    int sign = dp
        ? (int)((result_dp >> 63) & 1)
        : (int)(((uint32_t)result_dp >> 31) & 1);
    return sign ? 3 : 1;
}

// CVFX CC per POO §8.13: based on bits 0..15 (high half) of the int32
// result.  00 if hi16 zero, 11 if negative, 01 if positive.
// AP-101S anomaly: 0x41100000 -> 0x00010000 with CC=00.  Hi16 of
// 0x00010000 is 0x0001, which is non-zero positive, so naive POO would
// say CC=01 — but the real machine reports CC=00.  We emit the
// anomalous CC so the runner grades against actual hardware.
static int
cc_cvfx(int32_t result, uint32_t input_fp) {
    if (input_fp == 0x41100000U) return 0;  // §8.13 anomaly
    uint16_t hi = (uint16_t)((uint32_t)result >> 16);
    if (hi == 0) return 0;
    if (hi & 0x8000U) return 3;
    return 1;
}

// ---- NDJSON emission ----

// `defer_to` is null for vectors the runner should grade against the
// floatIBM.coffee primitives, or a string like "cpu_instr" for vectors
// that exercise behavior the primitives can't model on their own (e.g.,
// hardware anomalies emulated at the instruction-handler layer).  The
// runner treats deferred vectors as GAP automatically.
static void
emit(const char *op, const char *kind, const char *a_hex,
     const char *b_hex_or_null, const char *r_hex,
     const char *exc, int cc, int has_cc, const char *tag,
     const char *defer_to) {
    printf("{\"op\":\"%s\",\"kind\":\"%s\",\"a\":\"%s\",\"b\":",
           op, kind, a_hex);
    if (b_hex_or_null) printf("\"%s\"", b_hex_or_null);
    else               printf("null");
    printf(",\"result\":\"%s\",\"exc\":\"%s\",\"cc\":", r_hex, exc);
    if (has_cc) printf("%d", cc);
    else        printf("null");
    printf(",\"tag\":\"%s\"", tag);
    if (defer_to) printf(",\"defer_to\":\"%s\"", defer_to);
    printf("}\n");
}

static void
fmt_dp(uint64_t v, char *buf) {
    snprintf(buf, 17, "%016" PRIX64, v);
}

static void
fmt_sp(uint32_t v, char *buf) {
    snprintf(buf, 9, "%08" PRIX32, v);
}

// ---- Vector helpers per op ----

static void
vec_dp_addsub(const char *op, int sub, uint64_t a, uint64_t b,
              const char *tag) {
    char ah[17], bh[17], rh[17];
    fmt_dp(a, ah); fmt_dp(b, bh);
    uint64_t r = 0;
    ibm_fp_exc_t e = sub ? ibm_dp_sub_exc(a, b, &r) : ibm_dp_add_exc(a, b, &r);
    fmt_dp(r, rh);
    int cc = cc_load_style(r, /*dp=*/1);
    emit(op, "dp", ah, bh, rh, exc_name(e), cc, /*has_cc=*/1, tag, NULL);
}

static void
vec_dp_muldiv(const char *op,
              ibm_fp_exc_t (*fn)(uint64_t, uint64_t, uint64_t *),
              uint64_t a, uint64_t b, const char *tag) {
    char ah[17], bh[17], rh[17];
    fmt_dp(a, ah); fmt_dp(b, bh);
    uint64_t r = 0;
    ibm_fp_exc_t e = fn(a, b, &r);
    fmt_dp(r, rh);
    // mul/div leave CC unchanged.
    emit(op, "dp", ah, bh, rh, exc_name(e), 0, /*has_cc=*/0, tag, NULL);
}

static void
vec_dp_cmp(const char *op, int anomalous, uint64_t a, uint64_t b,
           const char *tag) {
    char ah[17], bh[17], rh[5];
    fmt_dp(a, ah); fmt_dp(b, bh);
    int cc = anomalous ? ibm_dp_cmp_anomalous(a, b) : ibm_dp_cmp(a, b);
    snprintf(rh, sizeof(rh), "----");  // compare has no result value
    // Anomalous compare is reproduced in floatIBM.coffee's
    // compE_anomalous (since step 9) — no defer needed.
    emit(op, "dp", ah, bh, rh, "OK", cc, /*has_cc=*/1, tag, NULL);
}

static void
vec_sp_addsub(const char *op, int sub, uint32_t a, uint32_t b,
              const char *tag) {
    char ah[9], bh[9], rh[9];
    fmt_sp(a, ah); fmt_sp(b, bh);
    uint32_t r = 0;
    ibm_fp_exc_t e = sub ? ibm_sp_sub_exc(a, b, &r) : ibm_sp_add_exc(a, b, &r);
    fmt_sp(r, rh);
    int cc = cc_load_style((uint64_t)r, /*dp=*/0);
    emit(op, "sp", ah, bh, rh, exc_name(e), cc, /*has_cc=*/1, tag, NULL);
}

static void
vec_sp_muldiv(const char *op,
              ibm_fp_exc_t (*fn)(uint32_t, uint32_t, uint32_t *),
              uint32_t a, uint32_t b, const char *tag) {
    char ah[9], bh[9], rh[9];
    fmt_sp(a, ah); fmt_sp(b, bh);
    uint32_t r = 0;
    ibm_fp_exc_t e = fn(a, b, &r);
    fmt_sp(r, rh);
    emit(op, "sp", ah, bh, rh, exc_name(e), 0, /*has_cc=*/0, tag, NULL);
}

static void
vec_sp_cmp(const char *op, int anomalous, uint32_t a, uint32_t b,
           const char *tag) {
    char ah[9], bh[9], rh[5];
    fmt_sp(a, ah); fmt_sp(b, bh);
    int cc = anomalous ? ibm_sp_cmp_anomalous(a, b) : ibm_sp_cmp(a, b);
    snprintf(rh, sizeof(rh), "----");
    // compE_anomalous now handles both DP and SP thresholds (since
    // step 9) — no defer needed.
    emit(op, "sp", ah, bh, rh, "OK", cc, /*has_cc=*/1, tag, NULL);
}

static void
vec_cvfx(uint32_t fp, const char *tag, int is_anomaly) {
    char ah[9], rh[9];
    fmt_sp(fp, ah);
    int32_t back = 0;
    ibm_fp_exc_t e = ibm_cvfx(fp, &back);
    snprintf(rh, sizeof(rh), "%08" PRIX32, (uint32_t)back);
    int cc = cc_cvfx(back, fp);
    // The §8.13 anomaly (0x41100000 -> 0x00010000 with CC=00) is the
    // cpu_instr layer's job — the math result here is correct, only the
    // CC is anomalous.
    const char *defer = is_anomaly ? "cpu_instr" : NULL;
    emit("cvfx", "sp", ah, NULL, rh, exc_name(e), cc, 1, tag, defer);
}

static void
vec_cvfl(int32_t v, const char *tag) {
    char ah[9], rh[9];
    snprintf(ah, sizeof(ah), "%08" PRIX32, (uint32_t)v);
    uint32_t fp = ibm_cvfl(v);
    fmt_sp(fp, rh);
    int cc = cc_load_style((uint64_t)fp, /*dp=*/0);
    emit("cvfl", "sp", ah, NULL, rh, "OK", cc, 1, tag, NULL);
}

// ---- Coverage matrix ----

int
main(void) {
    // Common DP constants
    const uint64_t DP_0    = 0x0000000000000000ULL;
    const uint64_t DP_1    = 0x4110000000000000ULL;
    const uint64_t DP_NEG1 = 0xC110000000000000ULL;
    const uint64_t DP_2    = 0x4120000000000000ULL;
    const uint64_t DP_HALF = 0x4080000000000000ULL;
    // (unused-but-named-for-future-use constants kept out to silence -Wunused;
    // re-add when corresponding vectors are written.)
    const uint64_t DP_BIG  = 0x7C10000000000000ULL;  // 0.1×16^60
    const uint64_t DP_TINY = 0x2010000000000000ULL;  // 0.1×16^-32
    const uint64_t DP_MAX  = 0x7FFFFFFFFFFFFFFFULL;  // largest positive

    const uint32_t SP_0    = 0x00000000U;
    const uint32_t SP_1    = 0x41100000U;
    const uint32_t SP_NEG1 = 0xC1100000U;
    const uint32_t SP_2    = 0x41200000U;
    const uint32_t SP_HALF = 0x40800000U;
    const uint32_t SP_BIG  = 0x7C100000U;
    const uint32_t SP_TINY = 0x20100000U;
    // SP_MAX reserved for future overflow boundary vectors
    const uint32_t SP_FF63 = 0x7FF00000U;            // 0.F × 16^63

    // ===== DP add =====
    vec_dp_addsub("dp_add", 0, DP_1,    DP_1,    "1+1");
    vec_dp_addsub("dp_add", 0, DP_1,    DP_NEG1, "1+(-1) -> sig zero");
    vec_dp_addsub("dp_add", 0, DP_0,    DP_0,    "0+0 -> sig zero");
    vec_dp_addsub("dp_add", 0, DP_1,    DP_0,    "1+0");
    vec_dp_addsub("dp_add", 0, DP_HALF, DP_HALF, "0.5+0.5 = 1");
    vec_dp_addsub("dp_add", 0, DP_BIG,  DP_BIG,  "big+big mostly fits");
    vec_dp_addsub("dp_add", 0, DP_MAX,  DP_MAX,  "max+max -> overflow");
    vec_dp_addsub("dp_add", 0, DP_1,    DP_TINY, "1 + tiny (tiny lost)");
    // Cancellation that leaves nonzero residual:
    //   1.0000001_hex × 16^0 + (-1.0_hex × 16^0) ≈ 1e-7
    vec_dp_addsub("dp_add", 0, 0x4110000010000000ULL, 0xC110000000000000ULL,
                  "near-cancel residual");

    // ===== DP sub =====
    vec_dp_addsub("dp_sub", 1, DP_1,    DP_1,    "1-1 -> sig");
    vec_dp_addsub("dp_sub", 1, DP_2,    DP_1,    "2-1 = 1");
    vec_dp_addsub("dp_sub", 1, DP_1,    DP_2,    "1-2 = -1");
    vec_dp_addsub("dp_sub", 1, DP_0,    DP_1,    "0-1 = -1");
    vec_dp_addsub("dp_sub", 1, DP_1,    DP_NEG1, "1-(-1) = 2");
    vec_dp_addsub("dp_sub", 1, DP_MAX,  DP_NEG1 | 0x7FFFFFFFFFFFFFFFULL,
                  "max-(-max) overflow");

    // ===== DP mul (full 56×56) =====
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_2, DP_HALF, "2×0.5 = 1");
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_1, DP_0,    "1×0 = 0");
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_HALF, DP_HALF, "0.5×0.5 = 0.25");
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_BIG,  DP_BIG,  "big×big -> overflow");
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_TINY, DP_TINY, "tiny×tiny -> underflow");
    vec_dp_muldiv("dp_mul", ibm_dp_mul_exc, DP_NEG1, DP_2,    "-1×2 = -2");

    // ===== DP mul quasi-extended (POO §8.17) =====
    vec_dp_muldiv("dp_mul_qe", ibm_dp_mul_qe_exc, DP_HALF, DP_HALF,
                  "qe 0.5×0.5 (matches full mul)");
    vec_dp_muldiv("dp_mul_qe", ibm_dp_mul_qe_exc,
                  0x7FFFFFFFFF000000ULL, 0x7FFFFFFFFF000000ULL,
                  "qe rounding-induced overflow (POO note)");
    vec_dp_muldiv("dp_mul_qe", ibm_dp_mul_qe_exc, DP_BIG, DP_BIG,
                  "qe big×big -> overflow");
    vec_dp_muldiv("dp_mul_qe", ibm_dp_mul_qe_exc, DP_TINY, DP_TINY,
                  "qe tiny×tiny -> underflow");

    // ===== DP div =====
    vec_dp_muldiv("dp_div", ibm_dp_div_exc, DP_1, DP_2, "1/2 = 0.5");
    vec_dp_muldiv("dp_div", ibm_dp_div_exc, DP_1, DP_0, "1/0 -> FP_DIVIDE");
    vec_dp_muldiv("dp_div", ibm_dp_div_exc, DP_0, DP_1, "0/1 = 0");
    vec_dp_muldiv("dp_div", ibm_dp_div_exc, DP_BIG, DP_TINY,
                  "big/tiny -> overflow");
    vec_dp_muldiv("dp_div", ibm_dp_div_exc, DP_TINY, DP_BIG,
                  "tiny/big -> underflow");

    // ===== DP compare =====
    vec_dp_cmp("dp_cmp", 0, DP_1, DP_1,    "1 == 1");
    vec_dp_cmp("dp_cmp", 0, DP_2, DP_1,    "2 > 1");
    vec_dp_cmp("dp_cmp", 0, DP_1, DP_2,    "1 < 2");
    vec_dp_cmp("dp_cmp", 0, DP_0, 0x8000000000000000ULL, "+0 == -0");
    vec_dp_cmp("dp_cmp", 0, DP_NEG1, DP_1, "-1 < 1");

    // §8.11 anomaly cases — both correct and faithful variants.
    vec_dp_cmp("dp_cmp",      0, 0x423FFFFF00001234ULL, 0x423FFFFF00801234ULL,
               "anom ex1 correct");
    vec_dp_cmp("dp_cmp_anom", 1, 0x423FFFFF00001234ULL, 0x423FFFFF00801234ULL,
               "anom ex1 faithful");
    vec_dp_cmp("dp_cmp",      0, 0xBEFFFFFFFB076890ULL, 0xBF10000000307689ULL,
               "anom ex2 correct");
    vec_dp_cmp("dp_cmp_anom", 1, 0xBEFFFFFFFB076890ULL, 0xBF10000000307689ULL,
               "anom ex2 faithful");
    vec_dp_cmp("dp_cmp",      0, 0x4010000000001234ULL, 0x3FFFFFFFF8012340ULL,
               "anom ex3 correct");
    vec_dp_cmp("dp_cmp_anom", 1, 0x4010000000001234ULL, 0x3FFFFFFFF8012340ULL,
               "anom ex3 faithful");

    // ===== SP add/sub =====
    vec_sp_addsub("sp_add", 0, SP_1, SP_1, "1+1");
    vec_sp_addsub("sp_add", 0, SP_1, SP_NEG1, "1+(-1) -> sig");
    vec_sp_addsub("sp_add", 0, SP_HALF, SP_HALF, "0.5+0.5 = 1");
    vec_sp_addsub("sp_add", 0, SP_FF63, SP_FF63, "0.F×16^63 + same -> overflow");
    vec_sp_addsub("sp_sub", 1, SP_1, SP_1, "1-1 -> sig");
    vec_sp_addsub("sp_sub", 1, SP_2, SP_1, "2-1 = 1");
    vec_sp_addsub("sp_sub", 1, SP_1, SP_2, "1-2 = -1");

    // ===== SP mul/div =====
    vec_sp_muldiv("sp_mul", ibm_sp_mul_exc, SP_2, SP_HALF, "2×0.5");
    vec_sp_muldiv("sp_mul", ibm_sp_mul_exc, SP_BIG, SP_BIG, "big×big -> overflow");
    vec_sp_muldiv("sp_mul", ibm_sp_mul_exc, SP_TINY, SP_TINY, "tiny×tiny -> underflow");
    vec_sp_muldiv("sp_mul", ibm_sp_mul_exc, SP_1, SP_0, "1×0 = 0");
    vec_sp_muldiv("sp_div", ibm_sp_div_exc, SP_1, SP_2, "1/2");
    vec_sp_muldiv("sp_div", ibm_sp_div_exc, SP_1, SP_0, "1/0 -> FP_DIVIDE");
    vec_sp_muldiv("sp_div", ibm_sp_div_exc, SP_0, SP_1, "0/1 = 0");

    // ===== SP compare =====
    vec_sp_cmp("sp_cmp", 0, SP_1, SP_1, "1 == 1");
    vec_sp_cmp("sp_cmp", 0, SP_2, SP_1, "2 > 1");
    vec_sp_cmp("sp_cmp", 0, SP_1, SP_2, "1 < 2");
    // POO §8.12 (short compare) does NOT document the §8.11 anomaly
    // (it only mentions "Exponent overflow, exponent underflow, or
    // lost significance cannot occur").  So no SP-specific anomaly
    // vectors here.  CER/CE still go through compE_anomalous in
    // cpu_instr.coffee (uniform with CEDR/CED) but the SP-shaped
    // operands won't naturally land at the DP threshold.

    // ===== CVFX / CVFL =====
    vec_cvfx(0x00000000U, "cvfx zero", 0);
    vec_cvfx(0x41100000U, "cvfx 0x41100000 anomaly (CC=00)", 1);
    vec_cvfx(0xC1100000U, "cvfx -16", 0);
    vec_cvfx(0x44800000U, "cvfx pos overflow", 0);
    vec_cvfx(0xC4800000U, "cvfx neg INT32_MIN edge", 0);
    vec_cvfx(0x4F100000U, "cvfx chr too high", 0);

    vec_cvfl(0,           "cvfl 0");
    vec_cvfl(1,           "cvfl 1");
    vec_cvfl(-1,          "cvfl -1");
    vec_cvfl(12345,       "cvfl 12345");
    vec_cvfl(-67890,      "cvfl -67890");
    vec_cvfl(65536,       "cvfl 65536 (= 1.0 fp)");
    vec_cvfl(0x7FFFFFFF,  "cvfl INT32_MAX");
    vec_cvfl((int32_t)0x80000000, "cvfl INT32_MIN");

    return 0;
}
