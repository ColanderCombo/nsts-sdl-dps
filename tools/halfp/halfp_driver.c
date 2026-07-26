/*
 * halfp_driver — batch-mode driver around ibmFloat.c for Python tests.
 *
 * Reads commands from stdin, one per line. For each command writes
 * exactly one result line to stdout.  All conversions delegate to
 * ext/virtualagc/XCOM-I/ibmFloat.c.
 *
 * Commands:
 *   D <16hex>     DP packed hex -> decimal string (17 sig digits)
 *   S <8hex>      SP packed hex -> decimal string (8 sig digits)
 *   d <decimal>   decimal -> DP packed hex  (16 uppercase hex chars)
 *   s <decimal>   decimal -> SP packed hex  (8 uppercase hex chars,
 *                                            DP-then-truncate-LSW)
 *   M <16hex>     DP packed hex -> HAL/S MONITOR(12)-style string
 *                                  (16 sig digits, leading ' '/'-')
 *   m <8hex>      SP packed hex -> HAL/S MONITOR(12)-style string
 *                                  (7 sig digits, leading ' '/'-',
 *                                   lsw forced to 0)
 *   + <16hex> <16hex>   DP add  (a + b)
 *   - <16hex> <16hex>   DP sub  (a - b)
 *   x <16hex> <16hex>   DP mul  (a * b)   [letter 'x' to avoid shell glob]
 *   / <16hex> <16hex>   DP div  (a / b)
 *   ^ <16hex> <16hex>   DP pow  (a ** b)  [via library pow(), not exact]
 *   _ <16hex>           DP negate (-a)
 *
 * On parse error: emits "ERR <reason>". Output buffering is line-flushed
 * so a Python parent driving us via pipes doesn't deadlock.
 */

#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ibmFloat.h"

/* parse exactly N uppercase hex digits (case-insensitive in input) */
static int
parse_hex_n(const char *s, int n, uint64_t *out)
{
    uint64_t v = 0;
    for (int i = 0; i < n; i++) {
        char c = s[i];
        int d;
        if (c >= '0' && c <= '9')      d = c - '0';
        else if (c >= 'a' && c <= 'f') d = 10 + c - 'a';
        else if (c >= 'A' && c <= 'F') d = 10 + c - 'A';
        else return -1;
        v = (v << 4) | (uint64_t)d;
    }
    *out = v;
    return 0;
}

static void
cmd_dp_to_string(const char *arg)
{
    uint64_t packed;
    if (strlen(arg) != 16 || parse_hex_n(arg, 16, &packed) != 0) {
        printf("ERR expected 16 hex digits\n");
        return;
    }
    uint32_t msw = (uint32_t)(packed >> 32);
    uint32_t lsw = (uint32_t)(packed & 0xFFFFFFFFu);
    char buf[64];
    ibm_dp_to_string(msw, lsw, 17, 17, buf, sizeof(buf));
    printf("%s\n", buf);
}

static void
cmd_sp_to_string(const char *arg)
{
    uint64_t packed;
    if (strlen(arg) != 8 || parse_hex_n(arg, 8, &packed) != 0) {
        printf("ERR expected 8 hex digits\n");
        return;
    }
    uint32_t msw = (uint32_t)packed;
    char buf[64];
    ibm_dp_to_string(msw, 0, 8, 8, buf, sizeof(buf));
    printf("%s\n", buf);
}

static void
cmd_dp_from_string(const char *arg)
{
    /* Models MONITOR(10) -> XXXTOD: see PASS1.PROCS/SCAN.xpl:690 (the
     * MONITOR(10) call) and MONITOR.ASM/MONITOR.bal:4123 (V#XTOD =
     * V(XXXTOD)). PREP_LITERAL then writes the 64-bit FPR6 to the
     * literal table via STD (PASS1.PROCS/PREPLITE.xpl:75-77), so every
     * numeric literal is stored at full DP regardless of declared type. */
    uint32_t msw, lsw;
    ibm_dp_from_string(arg, &msw, &lsw);
    printf("%08X%08X\n", msw, lsw);
}

static void
cmd_sp_from_string(const char *arg)
{
    /* HAL/S-FC SP from literal: convert as DP then truncate LSW.
     * Matches the IBM /360 STE opcode ("70"): writes the high 32 bits
     * (sign + 7-bit exponent + top 24 mantissa bits) and silently
     * drops the low 32. No round/normalize. See PASS2.PROCS/GENERATE.xpl:3166
     * for the codegen site. */
    uint32_t msw, lsw;
    ibm_dp_from_string(arg, &msw, &lsw);
    (void)lsw;
    printf("%08X\n", msw);
}

static void
cmd_dp_to_hal_string(const char *arg)
{
    uint64_t packed;
    if (strlen(arg) != 16 || parse_hex_n(arg, 16, &packed) != 0) {
        printf("ERR expected 16 hex digits\n");
        return;
    }
    uint32_t msw = (uint32_t)(packed >> 32);
    uint32_t lsw = (uint32_t)(packed & 0xFFFFFFFFu);
    char buf[64];
    /* precision=8 selects DP layout (16 sig digits). */
    ibm_dp_to_hal_string(msw, lsw, 8, buf, sizeof(buf));
    printf("%s\n", buf);
}

static void
cmd_sp_to_hal_string(const char *arg)
{
    uint64_t packed;
    if (strlen(arg) != 8 || parse_hex_n(arg, 8, &packed) != 0) {
        printf("ERR expected 8 hex digits\n");
        return;
    }
    uint32_t msw = (uint32_t)packed;
    char buf[64];
    /* precision=0 selects SP layout (7 sig digits); lsw forced to 0
     * to match the SP-on-AP-101 case where only MSW is meaningful. */
    ibm_dp_to_hal_string(msw, 0, 0, buf, sizeof(buf));
    printf("%s\n", buf);
}

/* Parse "<16hex> <16hex>" into two uint64_t values. Returns 0 on success. */
static int
parse_two_dp(const char *arg, uint64_t *a, uint64_t *b)
{
    /* Skip leading spaces, expect 16 hex, space(s), 16 hex. */
    while (*arg == ' ' || *arg == '\t') arg++;
    if (parse_hex_n(arg, 16, a) != 0) return -1;
    arg += 16;
    while (*arg == ' ' || *arg == '\t') arg++;
    if (parse_hex_n(arg, 16, b) != 0) return -1;
    return 0;
}

static void
cmd_dp_arith(char op, const char *arg)
{
    uint64_t a, b, r;
    if (parse_two_dp(arg, &a, &b) != 0) {
        printf("ERR expected two 16-hex operands\n");
        return;
    }
    switch (op) {
        case '+': r = ibm_dp_add(a, b); break;
        case '-': r = ibm_dp_sub(a, b); break;
        case 'x': r = ibm_dp_mul(a, b); break;
        case '/': {
            /* Guard against divide-by-zero (mantissa==0). */
            if ((b & 0x00FFFFFFFFFFFFFFULL) == 0) {
                printf("ERR divide by zero\n");
                return;
            }
            r = ibm_dp_div(a, b);
            break;
        }
        case '^': {
            /* Library pow() — not bit-exact to original IBM, by design. */
            uint32_t a_msw = (uint32_t)(a >> 32), a_lsw = (uint32_t)a;
            uint32_t b_msw = (uint32_t)(b >> 32), b_lsw = (uint32_t)b;
            double da = ibm_dp_to_double(a_msw, a_lsw);
            double db = ibm_dp_to_double(b_msw, b_lsw);
            uint32_t r_msw, r_lsw;
            ibm_dp_from_double(&r_msw, &r_lsw, pow(da, db));
            r = ((uint64_t)r_msw << 32) | r_lsw;
            break;
        }
        default:
            printf("ERR unknown arith op\n");
            return;
    }
    printf("%016llX\n", (unsigned long long)r);
}

static void
cmd_dp_neg(const char *arg)
{
    uint64_t a;
    while (*arg == ' ' || *arg == '\t') arg++;
    if (parse_hex_n(arg, 16, &a) != 0) {
        printf("ERR expected 16 hex digits\n");
        return;
    }
    /* Toggle sign bit, but preserve the canonical "true zero" representation
     * (sign bit on a zero packed value becomes "negative zero", which the
     * special-case code in ibm_dp_to_string already collapses to 0.0). */
    uint64_t r = a ^ 0x8000000000000000ULL;
    printf("%016llX\n", (unsigned long long)r);
}

int
main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);

    char line[1024];
    while (fgets(line, sizeof(line), stdin) != NULL) {
        size_t n = strlen(line);
        while (n > 0 && (line[n-1] == '\n' || line[n-1] == '\r')) line[--n] = '\0';
        if (n == 0) continue;

        char cmd = line[0];
        const char *arg = line + 1;
        while (*arg == ' ' || *arg == '\t') arg++;

        switch (cmd) {
            case 'D': cmd_dp_to_string(arg);     break;
            case 'S': cmd_sp_to_string(arg);     break;
            case 'd': cmd_dp_from_string(arg);   break;
            case 's': cmd_sp_from_string(arg);   break;
            case 'M': cmd_dp_to_hal_string(arg); break;
            case 'm': cmd_sp_to_hal_string(arg); break;
            case '+':
            case '-':
            case 'x':
            case '/':
            case '^': cmd_dp_arith(cmd, arg);    break;
            case '_': cmd_dp_neg(arg);           break;
            default:  printf("ERR unknown cmd '%c'\n", cmd); break;
        }
    }
    return 0;
}
