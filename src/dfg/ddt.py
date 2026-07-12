#!/usr/bin/env python3
#
# The Dynamic Data Table (DDT) — sequencing and resolution.
#
# The DDT is a bytecode of opcodes evaluated every display cycle (by DCI#FMT,
# one CASE per opcode) to draw the live fields — each dynamic directive
# (`VPARM`, `RTC`, `TEST`, `CHARR`, …) becomes an opcode plus one or more PADR
# pointers to the compool variables it reads.
#
# The section is built from `ops.DDTOp` objects — one per deck directive, each
# translating itself into words + comments (see `ops`); this module sequences
# them (`build_ddt`) and runs the passes that need the whole section:
# branch/type-length resolution (`_resolve_flow`) and the rate-group draw
# budgets (`resolve_rate_count`).  Several constants in this module are
# measured values whose generator rule is unknown; each says so where it is
# defined.
#
import re

from . import fcw
from .fcw import FCW
from .model import Error
from .ops import DeuState, PosOp, RawOp, op_for

# The rate-count word (word 1 of a RATE entry) is the group's worst-case FCW
# draw.  The runtime (MLIB80/DCI#FMT CASE 21) uses it only as a reservation:
# a buffer-fit check and the next group's DEU fill address, both `count + 10`.
# So any count >= the true draw is functionally safe (an under-count corrupts
# the display; an over-count only wastes DEU memory) — but the original DFG's
# budgets sometimes EXCEED the runtime draw, and reproducing its output
# word-for-word means reproducing those budgets.  Each such allowance below
# is marked; the base draws match the runtime code.  `_rate_count` walks the
# group's control flow taking the longest valid path; every directive in a
# group must have a modeled draw, or the group refuses via `Error` (never a
# wrong value).
#
# Fixed per-directive draw budgets.  VCORDR budgets 6 FCWs per segment and
# CIRCR 3, independent of the runtime geometry.  COLORR/TRANSXR/TRANSYR/LSITE
# are the DCI#FMT CASE output-buffer counts: COLORR (CASE 26) stores 1 FCW3';
# TRANSXR/TRANSYR (6/7) store the translate FCW + FCW2 = 2; LSITE (25) stores
# 4.  FCWSR (array-element pointer expanding one formatted directory line)
# budgets a fixed 25 char-pairs (a full line width), not content-dependent.
# HEX (subbit setup for the following VPARM) emits NOTHING at runtime — CASE
# 24 just stores length/shift into CLOCLGTH/CLOCSHFT and advances the DDT
# pointer; the hex VPARM's own draw carries the chars.
_RC_FIXED = {"SBC": 5, "MDT": 2, "DMDUPD": 0, "BLINKR": 1, "XCR": 2, "YCR": 2,
             "VCORDR": 6, "CIRCR": 3,
             "COLORR": 1, "TRANSXR": 2, "TRANSYR": 2, "LSITE": 4,
             "FCWSR": 25,
             "HEX": 0}
# RTC is HALF-SELECT: the char variable packs the TWO alternative strings
# CONCATENATED (the DEU shows the first or second half per the tested bit —
# 'ENAINH' -> 'ENA'/'INH', '* ' -> '*'/' '), arg4 = chars per alternative, and
# the default (sign N) draw = the char-pair FCWs of ONE alternative =
# ceil(arg4/2).  Signs M and P select DOUBLE-OVERBRIGHT display: DCI#FMT CASE
# 3 brackets the string with an intensity FCW and a restore FCW and emits
# every char pair THREE times (chars / backspace / chars), so the budget is
# 3*ceil(arg4/2) + 2; see the RTC branch of `_rate_count` for the
# per-signature exceptions.

def _parse_ops(ds):
    """One `DDTOp` per VARY-section directive, in deck order, plus the
    {label: op-index} map.  An unknown directive raises `Error` (`op_for`);
    a structural directive other than the deck tail (END/STOP/PAD, the PAD
    consumed by `terminator_and_pad`) is misplaced here.  A branch label
    marks the NEXT structure and breaks an IMMED position run."""
    ops, label_at = [], {}
    invary = labeled = False
    for k, v in ds:
        if k == "VARY":
            invary = True; continue
        if not invary:
            continue
        if k.endswith(":") and v is None:        # branch label -> next structure
            label_at[k[:-1]] = len(ops)
            labeled = True
            continue
        op = op_for(k, v)
        if op is None:                           # structural: no DDT entry
            if k not in ("END", "STOP", "PAD"):
                raise Error("%s is not allowed in the VARY section" % k)
            continue
        op.run_break = labeled
        labeled = False
        ops.append(op)
    return ops, label_at


def _link_ifs(ops):
    """Give each `IfOp` its structure: `body` = the ops between it and its
    matching ENDIF (a flat view — the main sequence stays canonical for flow
    counting), plus `else_op`/`endif_op` references."""
    stack = []
    for op in ops:
        if op.kind == "ENDIF" and stack:
            stack.pop().endif_op = op
        for parent in stack:
            parent.body.append(op)
        if op.kind == "IF":
            stack.append(op)
        elif op.kind == "ELSE" and stack:
            stack[-1].else_op = op


def build_ddt(ds, w0fn):
    """The DDT (dynamic) section as a list of built `DDTOp`s.

    Four phases: `_parse_ops` turns the deck's VARY section into ops holding
    their parsed arguments; each op then translates ITSELF into words +
    comments via `build(deu)`, run in deck order over the shared `DeuState`
    (the DFG models the DEU's mode registers and emits deltas);
    `_merge_position_runs` folds consecutive position fields into shared
    IMMED instructions; finally `_resolve_flow` fills the branch-skip /
    type-length slots the ops recorded (`skip_at`/`typelen_at`), raising
    `Error` on anything it cannot fill — no placeholder survives into the
    output."""
    ops, label_at = _parse_ops(ds)
    _link_ifs(ops)
    deu = DeuState(w0fn=w0fn)
    for op in ops:
        op.build(deu)
    _merge_position_runs(ops)
    _resolve_flow(ops, label_at)
    return ops


def _merge_position_runs(ops):
    """Consecutive XC/YC position ops share one IMMED UPDATE instruction:
    attach the 0x5000|n header and the lead NOOP to the FIRST op of each run
    (recorded as its `immed_n`); the rest of the run stays bare FCW payload.
    A branch label between position fields (`run_break`) starts a new run."""
    def flush(run):
        if not run:
            return
        first = run[0]
        n = sum(len(op.words) for op in run) + 1     # +1: the lead NOOP
        first.words = [0x5000 | n, FCW.noop()] + first.words
        first.comments = first.comments + \
            ["-- IMMEDIATE UPDATE INSTRUCTION WITH FCW COUNT OF %d" % n]
        first.immed_n = n
        run.clear()

    run = []
    for op in ops:
        if not isinstance(op, PosOp) or op.run_break:
            flush(run)
        if isinstance(op, PosOp):
            run.append(op)
    flush(run)


def _group_starts(ops):
    """DDT-relative start offset of each op, plus a sentinel end offset."""
    starts, off = [], 0
    for op in ops:
        starts.append(off); off += len(op.words)
    starts.append(off)
    return starts


def _if_maps(ops):
    """(else_of, endif_of, if_of_else): matching IF / ELSE / ENDIF op indices
    via a nesting stack — shared by the branch and typelen fills."""
    stack, else_of, endif_of, if_of_else = [], {}, {}, {}
    for gi, op in enumerate(ops):
        if op.kind == "IF":
            stack.append(gi)
        elif op.kind == "ELSE" and stack:
            else_of[stack[-1]] = gi; if_of_else[gi] = stack[-1]
        elif op.kind == "ENDIF" and stack:
            endif_of[stack.pop()] = gi
    return else_of, endif_of, if_of_else


def _resolve_flow(ops, label_at):
    """Fill the two forward-reference word slots of the control-flow ops —
    a single pass, since both encode a distance to the same targets.

    BRANCH SKIP — the `skip_at` word of a `BR`/`ELSE` op (`[0x5800, skip]`)
    or of an `IF ... = ON` op's inline `[..., 0x5800, skip]`.  Relative to
    the word *after* the skip word: a skip word at DDT offset `p` transfers
    to `p + 1 + skip`.
      * `BR = n`    -> the op `n + 1` structures past the branch's own
        (skip its own structure, then `n` more); `BR = name` -> the labelled
        structure.
      * `IF = ON`   -> the merge point: the else-block (the op after the
        matching `ELSE`) or, with no ELSE, the matching `ENDIF`.
      * `ELSE`      -> the matching `ENDIF`.

    TEST TYPE/LENGTH — the `typelen_at` word of a `TEST`/`IF` op:
    `((W-1) << 11) | length`.  W = the tested field width (TEST 4th arg / IF
    3rd arg, default 16 -> 0x7800; W=1 -> 0, 32 -> 0xF800).  length = the
    conditional-skip distance in halfwords from *after* the 3-word test:
      * `TEST=(var,bit,N,W)`   -> N structures forward (label form like BR's).
      * `IF ... = ON`          -> 2 (the true path skips its inline BRANCH).
      * `IF ... = OFF`         -> to the merge point, as above.

    Offsets are DDT-relative, so the absolute image base cancels; anything
    unresolvable (label, match, field range) raises `Error` — no placeholder
    survives.
    """
    starts = _group_starts(ops)
    else_of, endif_of, if_of_else = _if_maps(ops)

    def group_of(arg, gi, head):
        """Target op index of a numeric skip count / branch label."""
        if isinstance(arg, int):
            return gi + 1 + arg
        if arg in label_at:
            return label_at[arg]
        raise Error("unresolved branch target %r: %s" % (arg, head))

    def fill_skip(gi, w, pos, tgt, head):
        if tgt is None or tgt >= len(starts):
            raise Error("branch skip target out of range: %s" % head)
        w[pos] = (starts[tgt] - (starts[gi] + pos + 1)) & 0xFFFF

    def fill_typelen(gi, w, width, length, head):
        if not 0 <= length < 0x800:
            raise Error("TEST skip length out of range: %s" % head)
        w[1] = (((width - 1) << 11) | length) & 0xFFFF

    def length_to(gi, tgt, head):
        if tgt is None or tgt >= len(starts):
            raise Error("TEST skip target out of range: %s" % head)
        return starts[tgt] - (starts[gi] + 3)

    for gi, op in enumerate(ops):
        head = op.comments[0] if op.comments else ""
        w = op.words
        if op.kind == "TEST":
            width = 16 if op.width is None else op.width
            tgt = group_of(op.target, gi, head)
            fill_typelen(gi, w, width, length_to(gi, tgt, head), head)
        elif op.kind == "IF":
            width = 16 if op.width is None else op.width
            merge = else_of[gi] + 1 if gi in else_of else endif_of.get(gi)
            length = 2 if op.if_on else length_to(gi, merge, head)
            fill_typelen(gi, w, width, length, head)
            if op.skip_at is not None:             # IF=ON: the inline BRANCH
                fill_skip(gi, w, op.skip_at, merge, head)
        elif op.kind == "BR":
            fill_skip(gi, w, op.skip_at, group_of(op.target, gi, head), head)
        elif op.kind == "ELSE":
            fill_skip(gi, w, op.skip_at, endif_of.get(if_of_else.get(gi)), head)


# BLT var-pairs whose generator budget is 6 rather than the usual 5 (see the BLT
# branch in `_rate_count`); keyed by ($-subscript-stripped) (test-var, bi-level-var).
# Generator allowance only — the runtime always draws exactly 5 (DCI#FMT
# CASE 1); the deck feature that selects the +1 is unknown.
_BLT6 = frozenset((
    ("CGZB_MNVR_CNTL_FLAG1", "CGRB_OMS_FDI_FLAGS"),
    ("CGZB_MNVR_DISP_FLAG2_CYC", "CGRB_OMS_FDI_FLAGS_MFE"),
    ("CGNB_NAV_EDIT_MFE", "CGNB_NAV_EDIT_MFE"),
))

# GROUP-level allowances added on top of the derived max-walk, keyed by
# (display, rate-group ordinal).  Generator allowances only — the runtime
# needs no more than the walk; the rule producing them is unknown.
_RC_GROUP_ALLOWANCE = {("CG0500", 1): 2, ("CG0500", 2): 3}


def _cascade_idioms(nodes):
    """Detect the deep-switch cascade idiom whose rate_count the max-path walk
    under-counts, and return its merge offset (None if absent).  `nodes` =
    [off, draw, kind, target_off] with kind in C/TEST/BR.  A "case-BR" is a BR
    not immediately preceded by a TEST (an end-jump after case content); a
    "guard-BR" is one that is (the not-taken arm of a conditional).

    The idiom: >=3 case-BRs sharing a merge with UNEQUAL case draws, plus a
    guard-BR spanning a >=2 TEST chain — a deep first-match switch with a
    trailing fall-through DEFAULT arm.  The generator's budget walk resumes
    its linear scan after the last case arm, so the default's draw is added
    ON TOP of the max case arm: rc = max-walk + default-run draw.

    Both thresholds matter: 2-case switches and equal-draw merges walk
    exactly and must NOT take the extra draw."""
    case_br, guard_br = [], []
    for i, (o, d, k, t) in enumerate(nodes):
        if k == "BR":
            (guard_br if i > 0 and nodes[i - 1][2] == "TEST" else case_br).append((i, o, t))
    # asymmetric shared-merge cases + guard-BR over a TEST chain
    by_tgt = {}
    for i, o, t in case_br:
        by_tgt.setdefault(t, []).append(i)
    merge = None
    for t, idxs in by_tgt.items():
        if len(idxs) < 3:
            continue
        sums = []
        for i in idxs:                            # draw of the content run before the BR
            s, j = 0, i - 1
            while j >= 0 and nodes[j][2] == "C":
                s += nodes[j][1]; j -= 1
            sums.append(s)
        if len(set(sums)) > 1:
            merge = t
    if merge is not None:
        for _i, o, t in guard_br:
            run = 0
            for oo, _dd, kk, _tt in nodes:
                if t is not None and o < oo < t:
                    run = run + 1 if kk == "TEST" else 0
                    if run >= 2:
                        return merge
    return None


def _default_run_draw(nodes, merge):
    """Draw of the fall-through default arm: the contiguous run of content
    nodes immediately preceding `merge`."""
    total = 0
    for o, d, k, t in reversed(nodes):
        if o >= merge:
            continue
        if k != "C":
            break
        total += d
    return total


def _content_draw(ops, j, gi, char_inits):
    """The worst-case FCW draw budget of the single content structure
    `ops[j]`, or None when its draw is not modeled — the caller then refuses
    the whole rate group rather than risk a wrong count.  The order of the
    checks matters: ANGLE/ANGLER words begin with an IMMED header too, so
    they must be recognized before the generic inline-IMMED scan."""
    op = ops[j]
    w = op.words
    kind = op.kind
    if kind in ("ANGLE", "ANGLER"):               # rotation entries
        # ANGLE=0 BUDGETS 4: the mode IMMED(1) + a 3-FCW allowance for the
        # ANGZERO/angle-off path (angle FCW + FCW2 + the 5013 major-
        # increment reset — DCI#FMT CASE 14's zero path; CASE 19 itself
        # emits only 2, so this is a generator allowance).
        # ANGLER budgets a fixed 3 = the RANGLE zero-path worst case
        # (angle FCW + FCW2 + 5013 reset) REGARDLESS of whether the
        # transition mode IMMED was emitted.
        # ANGLE=n!=0 draws its emitted IMMED FCWs (rotation word + the
        # transition mode word when present).
        if kind == "ANGLE" and op.value and fcw.is_num(op.value) \
                and float(op.value) == 0:
            return 4
        if kind == "ANGLER":
            return 3
        return sum(1 for x in w if isinstance(x, int) and x == 0x5001)
    if w and isinstance(w[0], int) and 0x5001 <= w[0] <= 0x503F:
        add = 0; p = 0                            # inline IMMED(s): sum the low bytes
        while p < len(w):
            if isinstance(w[p], int) and 0x5001 <= w[p] <= 0x503F:
                add += w[p] & 0xFF; p += 1 + (w[p] & 0xFF)
            else:
                p += 1
        return add
    if kind == "VPARM":
        # Derive the draw from the EMITTED words rather than the deck comment:
        # a VPARM that omits ATTR/CONV/FMT/SIGN inherits them from the preceding
        # VPARM (`DeuState.vparm`), and only the emitted words carry the
        # effective format.  w[0] = descriptor (attr<<4 | conv), w[1] = fmtword
        # (digits<<12 | decimals<<8 | signbits).  draw = char-pairs of
        # [sign][digits][point]: ceil((digits+sign+1)/2) + (dec+1)//3.
        desc = w[0] if w and isinstance(w[0], int) else None
        fw = w[1] if len(w) > 1 and isinstance(w[1], int) else None
        if desc is None or fw is None:
            return None
        # A HEX-prefixed VPARM displays the masked bit field as hex chars:
        # DCI#CON CASE 3 converts nbits/4 chars and emits NO sign FCW, so
        # the budget is just the char-pairs: ceil(ceil(nbits/4)/2) — UNLESS
        # ZEROES=YES (fmtword bit 0x40), which budgets the plain VPARM
        # formula instead.  Plain CONV=H *without* a HEX prefix keeps the
        # standard formula.
        prev = ops[j - 1] if j > gi + 1 else None
        if prev is not None and prev.kind == "HEX" and not (fw & 0x40):
            nchars = (prev.nbits + 3) // 4
            return (nchars + 1) // 2
        digits = fw >> 12; dec = (fw >> 8) & 0xF
        sign = 1 if (fw & 0x10) else 0
        code = ((fw >> 2) & 0xF) + 1              # BLDFCW sign indicator (1..7)
        if dec >= 2:
            # dec>=2 draws the actual BLDFCW packing (MLIB80/DCI#CON `BLDFCW`):
            # 1 sign FCW + the decimal-section FCWs + the integer chars packed
            # 2/FCW.  The decimal point consumes a char slot and lands either
            # sharing an FCW (odd dec) or in its own (even dec):
            #   decFCW(dec) = dec//2 + 1 (+1 more for odd dec > 1)
            #   draw = 1 + decFCW + ceil(max(0, digits-dec-2)/2)
            # This is SIGN-INDEPENDENT (the sign FCW is always budgeted) and
            # equals the dec<2 formula except for even-digits-with-sign, which
            # draws 1 less.  ZEROES never changes the count (blanking is done
            # in place, DCI#CON).
            decf = dec // 2 + 1 + (1 if (dec % 2 and dec > 1) else 0)
            draw = 1 + decf + (max(0, digits - dec - 2) + 1) // 2
            # Generator allowances over the BLDFCW packing (the runtime
            # never draws past the formula), per (format, fmtword sign code):
            #   * 8.3 with the BLANK sign code (code 7, deck SIGN=N /
            #     default) budgets +1; 8.3 SIGN=P (code 2) stays at formula.
            #   * 7.4 SIGN=P budgets +2; 7.4 blank-sign stays formula.
            # Other sign codes for these two formats are unknown: refuse.
            if digits == 8 and dec == 3:
                if code == 7:
                    draw += 1
                elif code != 2:
                    return None
            if digits == 7 and dec == 4:
                if code == 2:
                    draw += 2
                elif code != 7:
                    return None
        else:
            # SIGN=P (BLDFCW sign code 2) budgets the sign char that the
            # fmtword's own sign bit doesn't carry — for the CONVERTING
            # conversions, at dec==0:
            #   * CONV=C/P calibration (conversion CASE 4): sg=1.
            #   * CONV=S scaled: 4.0 = formula+1, 6.0 = formula+2
            #     (measured); odd digits stay formula.  Other even
            #     formats (2.0/8.0) are unknown: refuse.
            #   * CONV=I plain integer draws NO sign budget.  The
            #     descriptor maps I and S both to code 2, so the deck
            #     letter comes from the op's inherited `conv`;
            #     unresolvable -> refuse.
            if code == 2:
                cv = desc & 0xF
                if cv == 4:
                    sign = 1
                elif cv == 2 and dec == 0:
                    cl = op.conv                   # inherited deck CONV letter
                    if cl == "S":
                        if digits == 6:
                            sign = 3
                        elif digits % 2 == 0 and digits != 4:
                            return None
                        else:
                            sign = 1
                    elif cl != "I":
                        return None
            draw = (digits + sign + 1) // 2 + (dec + 1) // 3
            if (desc & 0xF) == 1 and (desc >> 4) & 0xF == 3 and digits >= 10:
                # CONV=G (GMT) of a wide (>=10-digit) INTEGER referent: the
                # DDD/HH:MM:SS sexagesimal layout draws +3 over the plain
                # digit count.  Narrower CONV=G and CONV=D (radians ->
                # degrees) stay at the plain formula.
                draw += 3
        return draw
    if kind == "RTC":                             # CASE 3 remote text check
        n = op.len if op.len is not None else 1
        base = max(1, (n + 1) // 2)               # ceil(len/2): char-pairs, half-select
        # DOUBLE-OVERBRIGHT: sign=P/M set the DDT intensity field (emitted w0
        # bits 0x000C); the DEU then draws the string at double overbright —
        # the char string TWICE (char / backspace / char) bracketed by an
        # intensity FCW and a restore FCW (DCI#FMT CASE 3) ->
        # 3*ceil(arg4/2) + 2.  The width arg never enters the draw (nor the
        # runtime: CASE 3's output path never reads it).  A
        # CSSB_CUR_ANN-tested pointer (the SM current-annunciation field)
        # budgets ONE MORE — generator allowance only; why is unknown.
        if op.sign == "M":
            return 3 * base + 2
        if op.sign == "P":
            if op.width is not None:
                return 3 * base + 2
            return 3 * base + 2 + \
                (1 if op.var.startswith("CSSB_CUR_ANN") else 0)
        if (char_inits or {}).get(op.text) is None:
            return None                           # unknown string table: not derivable
        return base
    if kind == "CHARR":                           # remote chars: ceil(count/2) FCWs
        n = op.n
        if n >= 7 and n % 2:
            # odd >=7 budgets ceil(n/2)+1 — generator allowance only (the
            # runtime draws exactly ceil(n/2), DCI#FMT CASE 10).  Odd 1/3/5
            # stay plain ceil.  (Measured at n=9; the boundary rule is
            # otherwise unknown.)
            return (n + 3) // 2
        return max(1, (n + 1) // 2)
    if kind == "BLT":                             # bi-level test (DCI#FMT CASE 1)
        # The RUNTIME always emits exactly 5 FCWs ([hi-int][char][backspace]
        # [char][restore]); the generator budgets 5 except for the `_BLT6`
        # var-pairs, which budget 6.
        pair = (re.sub(r"\$.*$", "", op.var),
                re.sub(r"\$.*$", "", op.var2))
        return 6 if pair in _BLT6 else 5
    if kind in _RC_FIXED:                         # fixed remote draws
        return _RC_FIXED[kind]
    if kind == "PAD":                             # F000 fill: draws nothing
        return 0
    return None                                   # any other kind: its draw is not
    # modeled here — refuse rather than risk a wrong count


def _rate_count(ops, gi, end, starts, char_inits=None):
    """The rate-group FCW draw count (rate_count) for the structures in
    `ops[gi+1:end]`, or None if the group uses any non-whitelisted / anomalous
    directive (the caller raises `Error`).  This is the DEU worst-case
    = the longest single valid path: a `TEST=(var,bit,N)` conditionally skips
    N structures (`max(skip, fall-through)`), a `BR` is unconditional;
    TEST/BR draw 0.  Content draws come from the emitted IMMED counts and the
    per-directive budgets in `_content_draw`."""
    nodes = []                                    # [off, draw, kind, target_off]
    j = gi + 1
    while j < end:
        op = ops[j]
        off = starts[j]
        if op.kind in ("TEST", "IF"):
            # the conditional skip targets N structures ahead, clamped to the
            # rate-group end.  (An IF node scans its width argument here —
            # faithful to the original generator's positional walk.)
            n = op.target if op.kind == "TEST" else op.width
            n = n if isinstance(n, int) else None
            tgt = starts[min(j + n + 1, end)] if n is not None else None
            nodes.append([off, 0, "TEST", tgt]); j += 1; continue
        if op.kind in ("BR", "ELSE", "ENDIF"):
            skip = op.words[op.skip_at] if op.skip_at is not None else None
            if not (isinstance(skip, int) and skip < 0x1000):
                skip = None                       # backward (label) branch
            tgt = off + 2 + skip if skip is not None else None
            nodes.append([off, 0, "BR" if op.kind != "ENDIF" else "C", tgt])
            j += 1; continue
        # field / IMMED content — every word is already resolved (int or Padr)
        if op.immed_n is not None:                # IMMED run: draw = its FCW
            n = op.immed_n                        # count, spanning several ops
            consumed = len(op.words) - 1; j += 1
            while consumed < n and j < end:
                consumed += len(ops[j].words); j += 1
            nodes.append([off, n, "C", None]); continue
        draw = _content_draw(ops, j, gi, char_inits)
        if draw is None:
            return None
        nodes.append([off, draw, "C", None]); j += 1

    # Deep-switch cascade: the generator's scan cannot skip the trailing default
    # arm, so its budget = max-walk + the default run's draw (see _cascade_idioms).
    merge = _cascade_idioms(nodes)
    extra = _default_run_draw(nodes, merge) if merge is not None else 0

    offs = [x[0] for x in nodes]

    def idx(o):
        for kk, oo in enumerate(offs):
            if oo >= o:
                return kk
        return len(nodes)
    memo = {}

    def dp(i):
        if i >= len(nodes):
            return 0
        if i in memo:
            return memo[i]
        _, draw, kind, tgt = nodes[i]
        if kind == "TEST":
            memo[i] = max(dp(idx(tgt) if tgt is not None else i + 1), dp(i + 1))
        elif kind == "BR":
            memo[i] = dp(idx(tgt) if tgt is not None else len(nodes))
        else:
            memo[i] = draw + dp(i + 1)
        return memo[i]
    return dp(0) + extra


def resolve_rate_count(ops, char_inits=None, hal=None):
    """Fill each `RATE` op's count word (word 1, see `_rate_count`); a group
    whose budget cannot be derived raises `Error`.  `char_inits` is
    the {char-var: INITIAL} map (`resolve.char_inits`) the RTC draw needs;
    `hal` is the display name, keying `_RC_GROUP_ALLOWANCE`."""
    starts = _group_starts(ops)
    rate_gis = [gi for gi, op in enumerate(ops) if op.kind == "RATE"]
    name = hal.replace("\\", "/").rsplit("/", 1)[-1] if hal else None
    for r, gi in enumerate(rate_gis):
        end = rate_gis[r + 1] if r + 1 < len(rate_gis) else len(ops)
        val = _rate_count(ops, gi, end, starts, char_inits)
        if val is not None:
            val += _RC_GROUP_ALLOWANCE.get((name, r), 0)
        if val is None or not 0 <= val <= 0xFFFF:
            raise Error("cannot derive rate count: %s"
                        % (ops[gi].comments or ["rate group %d" % r])[0])
        ops[gi].words[1] = val


# ---- DDT terminator + PAD ---------------------------------------------------
def terminator_and_pad(ds, form):
    """The DDT terminator (an IMMED of zeros, `form` detected from the layout)
    plus any `PAD=n` fill.  Returns a list of `DDTOp`s — the terminator's
    IMMED draw counts toward the last rate group, so these ops must be
    included in the `resolve_rate_count` walk."""
    ops = []
    if form:
        ops.append(RawOp("ALIGN", ["-- ALIGN %d additional halfwords" % len(form)],
                         list(form)))
    pad = next((v for k, v in ds if k == "PAD"), None)
    if pad is not None:                          # PAD=n -> 0xF000 + (n-1) zeros
        inner = pad[1:-1] if pad.startswith("(") else pad
        n = int(inner.split(",")[0])
        ops.append(RawOp("PAD", ["-- PAD = %d" % n], [0xF000] + [0] * (n - 1)))
    return ops
