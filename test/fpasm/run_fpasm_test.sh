#!/bin/bash
# Run fpasm_test under gpc with --verbose; split stdout (outfile6) from
# the exception log lines (filtered stderr).  Used by ctest.
#
# Args:
#   $1 = gpc binary or 'gpc;run' command (passed as one arg, may contain ';')
#   $2 = .fcm path
#   $3 = .sym.json path
#   $4 = output dir for actuals
set -eu
GPC_CMD="$1"
FCM="$2"
SYM="$3"
OUTDIR="$4"

mkdir -p "$OUTDIR"
ACTUAL_OUT="$OUTDIR/fpasm_test.actual.out6"
ACTUAL_EXC="$OUTDIR/fpasm_test.actual.exc"
RAW_ERR="$OUTDIR/fpasm_test.raw.err"

# Split the command on ';' so 'gpc;run' becomes 'gpc run'.
# CMake's add_test escapes our ';' as '\;' when it's inside a quoted arg —
# strip those backslashes first.
GPC_CMD="${GPC_CMD//\\;/;}"
IFS=';' read -ra CMD_PARTS <<< "$GPC_CMD"

"${CMD_PARTS[@]}" \
    --max-steps 5000 --no-trace --verbose \
    --symbols "$SYM" \
    --outfile6="$ACTUAL_OUT" \
    "$FCM" 2>"$RAW_ERR" >/dev/null

# Extract just the deterministic exception-code lines from stderr.
# Format: "GPC: unhandled program check, code=0x000C (no handler...)"
grep -oE 'code=0x[0-9A-F]+' "$RAW_ERR" >"$ACTUAL_EXC" || true
