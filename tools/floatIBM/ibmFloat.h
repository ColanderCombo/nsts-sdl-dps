/*
 * License:     The author (Donald Schmidt) declares that this program
 *              is in the Public Domain (U.S. law) and may be used or
 *              modified for any purpose whatever without licensing.
 * Filename:    tools/floatIBM/ibmFloat.h
 * Purpose:     IBM hex floating-point helpers — AP-101S reference.
 *
 *              Forked from ext/virtualagc/XCOM-I/ibmFloat.{c,h} (which
 *              targets host-side compile-time literal arithmetic in the
 *              HAL/S-FC compiler).  This local copy adds:
 *                - exception-aware arithmetic (ibm_*_exc returning
 *                  ibm_fp_exc_t per AP-101S POO §8.8 semantics)
 *                - SP (24-bit fraction) variants
 *                - quasi-extended multiply per POO §8.17
 *                - hardware-faithful compare with the §8.11 anomaly
 *                - CVFX/CVFL conversion helpers
 *
 *              Based on the floating-point logic in the Hyperion/
 *              Hercules IBM 390 & z/Series emulator:
 *                https://github.com/hercules-390/hyperion/blob/master/float.c
 *
 *              This is the *reference* implementation used by the
 *              Node-side test runner that grades ext/sim/gpc/floatIBM.coffee.
 *              Behavior here is intended to match AP-101S exactly,
 *              including documented hardware quirks.
 *
 * Reference:   http://www.ibibio.org/apollo/Shuttle.html
 *              IBM-85-C67-001 (AP-101S Principles of Operation), §8
 */

#ifndef IBM_FLOAT_H
#define IBM_FLOAT_H

#include <stddef.h>
#include <stdint.h>
#include <assert.h>

// NOTE: 
//       IBM floating point exponents are base-16 (hex).  Standard IEEE754
//       implementations (like C's float/double types) use base-2.  Because
//       it's hex, you'll see a lot of 4-bit ops to handle hexadecimal digits.
//
//       IEEE754 double's have 53 mantissa bits, and IBM dp has 56.
//
// Format constants:
//
#define IBM_DP_MANT_BITS         56     // mantissa width in bits
#define IBM_DP_MANT_HEXDIGITS    14     // = MANT_BITS / 4
#define IBM_DP_EXP_BIAS          64     // characteristic bias
#define IBM_DP_EXP_MAX           0x7F   // max biased exponent

// Bit masks on the 64-bit packed form:
//
#define IBM_DP_SIGN_BIT          0x8000000000000000ULL
#define IBM_DP_MAGNITUDE_MASK    0x7FFFFFFFFFFFFFFFULL  // ~SIGN_BIT
#define IBM_DP_MANT_MASK         0x00FFFFFFFFFFFFFFULL  // bits 0..55
#define IBM_DP_EXP_FIELD_MASK    0x7F00000000000000ULL  // bits 56..62

// During add/sub, mantissas are temporarily widened to 60 bits with a
// 4-bit guard digit at the bottom.  These masks identify the top hex
// digit of the 56-bit and 60-bit forms (used to detect normalization
// status), and the overflow region above the 60-bit field.
#define IBM_DP_TOP_HEX_56        0x00F0000000000000ULL  // bits 52..55
#define IBM_DP_TOP_HEX_60        0x0F00000000000000ULL  // bits 56..59
#define IBM_DP_OVERFLOW_60       0xF000000000000000ULL  // bits 60..63

// Overflow sentinel returned when a result exceeds the representable
// range: as a packed uint64 with sign=1, exp=0x7F, mant=0.  The (msw,
// lsw) form has msw = 0xff000000 and lsw = 0.
#define IBM_DP_OVERFLOW_PACKED   0xFF00000000000000ULL
#define IBM_DP_OVERFLOW_MSW      0xff000000U

// Field accessors
//
#define IBM_DP_SIGN(packed)  ((uint8_t)(((packed) >> 63) & 1u))
#define IBM_DP_EXP(packed)   ((int)(((packed) >> IBM_DP_MANT_BITS) \
                                    & IBM_DP_EXP_MAX))
#define IBM_DP_MANT(packed)  ((packed) & IBM_DP_MANT_MASK)

// True zero per S/360 semantics: BOTH mantissa AND biased exponent are
// zero.  An operand like FIXER (mant=0, exp=0x4E) is NOT zero — it has a
// nonzero exponent and participates in alignment.  Sign bit is ignored
// (0x8000000000000000 is "negative zero" but still numerically zero).
#define IBM_DP_IS_TRUE_ZERO(packed) (((packed) & IBM_DP_MAGNITUDE_MASK) == 0)

#define IBM_DP_PACK(sign, exp, mant)                              \
    (((uint64_t)((sign) & 1u) << 63)                              \
   | ((uint64_t)((exp) & IBM_DP_EXP_MAX) << IBM_DP_MANT_BITS)     \
   | ((mant) & IBM_DP_MANT_MASK))

#define IBM_DP_RENORMALIZE_56(mant, exp) do {                     \
    assert(mant !=0 && "IBM_DP_RENORMALIZE_56: mant == 0");       \
    while (((mant) & IBM_DP_TOP_HEX_56) == 0) {                   \
        (mant) <<= 4;                                             \
        (exp)  -= 1;                                              \
    }                                                             \
} while (0)

// Conversion to/from C double:
//  prefer ibm_dp_from_string for decimal literals to preserve all 56 bits.
void   ibm_dp_from_double(uint32_t *msw, uint32_t *lsw, double d);
double ibm_dp_to_double(uint32_t msw, uint32_t lsw);

// Conversion from decimal string:
//  based on XXXTOD.bal
void ibm_dp_from_string(const char *s, uint32_t *msw, uint32_t *lsw);

// Conversion to decimal string, direct from the IBM hex DP without
// going through IEEE754:
void ibm_dp_to_string(uint32_t msw, uint32_t lsw,
                      int sig_digits, int pad_to_digits,
                      char *out, size_t out_len);

// HAL/S MONITOR(12)-style formatting:
//   zero (mantissa==0, including IBM overflow sentinel) -> " 0.0"
//   positive -> " D.DDD...DDE±NN"   (leading space)
//   negative -> "-D.DDD...DDE±NN"
// precision==0 selects SP layout (7 sig digits, 6 frac).
// Anything else selects DP (16 sig digits, 15 frac).
void ibm_dp_to_hal_string(uint32_t msw, uint32_t lsw, int precision,
                          char *out, size_t out_len);

// Legacy DP API — silent saturation on overflow, asserts on /0.
// Retained for the string-conversion code which doesn't model exceptions
// (compile-time literal arithmetic never throws FP interrupts).
uint64_t ibm_dp_add(uint64_t a, uint64_t b);
uint64_t ibm_dp_sub(uint64_t a, uint64_t b);
uint64_t ibm_dp_mul(uint64_t a, uint64_t b);
uint64_t ibm_dp_div(uint64_t a, uint64_t b);
uint64_t ibm_dp_addsub(uint64_t a, uint64_t b,
                       int subtract_b, int normalize);

// =====================================================================
// AP-101S exception-aware API
// =====================================================================

// Exception flag.  Numeric values match POO §2.5.2 interrupt codes so the
// CPU layer can use the value directly as the program-check int code.
typedef enum {
    IBM_FP_OK                 = 0,
    IBM_FP_EXP_OVERFLOW       = 0x000B,   // pri 21, non-maskable
    IBM_FP_EXP_UNDERFLOW      = 0x0009,   // pri 22, PSW bit 22
    IBM_FP_SIGNIFICANCE       = 0x0005,   // pri C4, PSW bit 23
    IBM_FP_DIVIDE             = 0x000C,   // pri C3, non-maskable
    IBM_FP_CONVERT_OVERFLOW   = 0x000A,   // pri C5, non-maskable
} ibm_fp_exc_t;

// Result-vs-exception contract:
//   OK              - *result valid; caller writes back, sets CC.
//   EXP_OVERFLOW    - *result holds the would-be (exponent-wrapped)
//                     value for trace/test visibility.  Per POO, the
//                     CPU layer MUST NOT write back to operand storage.
//   EXP_UNDERFLOW   - *result = true zero (0).  CPU layer writes back
//                     IFF PSW exp-underflow mask bit 22 is 0.
//   SIGNIFICANCE    - *result = true zero (0).  CPU layer ALWAYS writes
//                     back, sets CC=00.  Mask bit 23 only gates the
//                     program interrupt itself.
//   FP_DIVIDE       - *result = 0xDEADBEEF...  CPU layer MUST NOT write
//                     back; CC unchanged; division suppressed.

// DP arithmetic (64-bit packed: sign 1, char 7, frac 56)
ibm_fp_exc_t ibm_dp_add_exc   (uint64_t a, uint64_t b, uint64_t *result);
ibm_fp_exc_t ibm_dp_sub_exc   (uint64_t a, uint64_t b, uint64_t *result);
ibm_fp_exc_t ibm_dp_mul_exc   (uint64_t a, uint64_t b, uint64_t *result);
ibm_fp_exc_t ibm_dp_div_exc   (uint64_t a, uint64_t b, uint64_t *result);

// AP-101S §8.17 quasi-extended multiply: each operand mantissa is
// pre-truncated to 31 bits with round-into-bit-31 from bit 32, then
// multiplied.  Used for MEDR/MED.
ibm_fp_exc_t ibm_dp_mul_qe_exc(uint64_t a, uint64_t b, uint64_t *result);

// DP compare returns CC: 0=equal, 1=a>b, 3=a<b.  No exceptions per POO.
int ibm_dp_cmp           (uint64_t a, uint64_t b);
int ibm_dp_cmp_anomalous (uint64_t a, uint64_t b);  // hardware bug-faithful

// SP arithmetic (32-bit packed: sign 1, char 7, frac 24).
// AP-101S short FP is 24-bit throughout, NOT "DP and truncate".
ibm_fp_exc_t ibm_sp_add_exc(uint32_t a, uint32_t b, uint32_t *result);
ibm_fp_exc_t ibm_sp_sub_exc(uint32_t a, uint32_t b, uint32_t *result);
ibm_fp_exc_t ibm_sp_mul_exc(uint32_t a, uint32_t b, uint32_t *result);
ibm_fp_exc_t ibm_sp_div_exc(uint32_t a, uint32_t b, uint32_t *result);

// SP compare returns CC: 0=equal, 1=a>b, 3=a<b.  No exceptions per POO.
int ibm_sp_cmp           (uint32_t a, uint32_t b);
int ibm_sp_cmp_anomalous (uint32_t a, uint32_t b);

// CVFX: short FP -> two's-complement int32 with binary point between
// bits 15 and 16.  Raises CONVERT_OVERFLOW per POO §8.13 when the FP
// value is outside .7FFFFF * 16^4 .. -.800000 * 16^4.
ibm_fp_exc_t ibm_cvfx(uint32_t fp, int32_t *fixed);

// CVFL: int32 (binary point between bits 15/16) -> short FP.  Cannot
// raise per POO §8.14.
uint32_t ibm_cvfl(int32_t fixed);

// =====================================================================
// SP packing — same shape as IBM_DP_PACK but at 24-bit fraction width.
// =====================================================================

#define IBM_SP_MANT_BITS         24
#define IBM_SP_MANT_HEXDIGITS    6
#define IBM_SP_SIGN_BIT          0x80000000U
#define IBM_SP_MAGNITUDE_MASK    0x7FFFFFFFU
#define IBM_SP_MANT_MASK         0x00FFFFFFU
#define IBM_SP_EXP_FIELD_MASK    0x7F000000U
#define IBM_SP_TOP_HEX_24        0x00F00000U   // bits 20..23
#define IBM_SP_TOP_HEX_28        0x0F000000U   // bits 24..27 (after <<4 guard)
#define IBM_SP_OVERFLOW_28       0xF0000000U   // bits 28..31

#define IBM_SP_OVERFLOW_PACKED   0xFF000000U
#define IBM_SP_IS_TRUE_ZERO(p)   (((p) & IBM_SP_MAGNITUDE_MASK) == 0)
#define IBM_SP_SIGN(p)           ((uint8_t)(((p) >> 31) & 1u))
#define IBM_SP_EXP(p)            ((int)(((p) >> IBM_SP_MANT_BITS) \
                                        & IBM_DP_EXP_MAX))
#define IBM_SP_MANT(p)           ((p) & IBM_SP_MANT_MASK)

#define IBM_SP_PACK(sign, exp, mant)                              \
    (((uint32_t)((sign) & 1u) << 31)                              \
   | ((uint32_t)((exp) & IBM_DP_EXP_MAX) << IBM_SP_MANT_BITS)     \
   | ((mant) & IBM_SP_MANT_MASK))

#define IBM_SP_RENORMALIZE_24(mant, exp) do {                     \
    assert(mant != 0 && "IBM_SP_RENORMALIZE_24: mant == 0");      \
    while (((mant) & IBM_SP_TOP_HEX_24) == 0) {                   \
        (mant) <<= 4;                                             \
        (exp)  -= 1;                                              \
    }                                                             \
} while (0)

// Sentinel stuffed into *result on FP_DIVIDE so callers that
// accidentally use it crash loud rather than silently propagate.
#define IBM_FP_DIVIDE_SENTINEL_DP 0xDEADBEEFDEADBEEFULL
#define IBM_FP_DIVIDE_SENTINEL_SP 0xDEADBEEFU

#endif // IBM_FLOAT_H
