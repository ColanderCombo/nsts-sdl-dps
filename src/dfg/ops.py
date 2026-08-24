#!/usr/bin/env python3
#
# DDT operations — the deck's dynamic directives as typed op objects.
# 
# Parsing the VARY section of a deck (`ddt.build_ddt`) yields one `DDTOp` per
# directive, holding its parsed arguments.  `build(deu)` then translates the op
# into the words it emits — the GPC-side DDT control words plus any inline
# `FCW` payload — and the `--` annotation comments that accompany them in the
# HAL output.  An op's PADR referents come from `padr_names()`, so a
# directive's argument parsing, pointer names, emitted words, and comments all
# live in one class.
# 
# Emission is stateful: the DFG models the DEU's mode registers and emits
# deltas, so `build` runs in deck order over a shared `DeuState` (character
# size, rotation, blink/intensity, spacing axis, inherited VPARM attributes).
# 
# `IF` blocks are structured: after parsing, `IfOp.body` holds the ops between
# the IF and its matching ENDIF, with `else_op`/`endif_op` references.  The
# flat op list stays canonical — deck flow control (`TEST=(var,bit,N)` skips,
# `BR=n`) counts every directive as one structure, ELSE/ENDIF included, so the
# tree is a view, not the sequence.
# 
from dataclasses import dataclass, field

from . import fcw
from .deck import atom, vparm_attrs
from .fcw import FCW
from .model import Error, Padr


def _oneline(v):
    """A directive value collapsed to one line for an error message — a
    continuation-card value keeps its embedded newline in `value` (the
    comment echo is verbatim), which would wrap the diagnostic."""
    return " ".join(str(v).split())


def immed(*payload):
    """An inline IMMED UPDATE instruction: the 0x5000|n header word followed
    by its n FCWs of payload — the GPC copies the payload verbatim to the
    DEU."""
    return [0x5000 | len(payload), *payload]


@dataclass
class DeuState:
    """The DFG's model of the DEU mode registers, threaded through
    `DDTOp.build` in deck order.  Mode FCWs LATCH: a register keeps its value
    until the next write, so the DFG tracks each register here and a mode
    directive emits the register's new composed value (an INT=D must carry
    the current blink state along, a SPACE must step at the current size's
    pitch, and so on).

    Register state, grouped by the FCW that writes it:
      FCW1 (attribute register)  — `intensity` (INT=D/ON), `blink` (BLINK=E),
        `axis_y` (AXIS=Y spacing direction).  The dash bit rides per-word
        (DASH=E) and is never latched.
      FCW2 (char/beam mode)      — `size_large` (SIZE=L), `rotated`
        (rotation active; the upright bit is cleared while set).

    `vparm` is GPC-side, not a DEU register: a VPARM that omits
    ATTR/CONV/FMT/SIGN inherits them from the previous VPARM.
    `w0fn(op)` derives an RTC/TEST/BLT/IF op's condition word from the
    compool, reading its rule fields (None -> refuse)."""
    w0fn: object = None
    intensity: bool = False
    blink: bool = False
    axis_y: bool = False
    size_large: bool = False
    rotated: bool = False
    vparm: dict = field(default_factory=dict)

    def fcw1(self, dash=False):
        """The FCW1 attribute-register word for the current state (the
        per-word dash bit ORed in on request)."""
        return FCW.attr_mode(intensity=self.intensity, blink=self.blink,
                             dash=dash)

    def fcw2(self, rotated=None, alt=False):
        """The FCW2 mode-register word for the current state (`rotated`
        overrides the tracked rotation, for transition words)."""
        return FCW.char_mode(large=self.size_large,
                             rotated=self.rotated if rotated is None
                             else rotated, alt=alt)

    def enter_rotation(self):
        """Latch rotation ON; the words that accompany the transition — the
        FCW2 mode switch as an IMMED(1) on OFF->ON only (a second consecutive
        rotation op carries no mode word)."""
        words = [] if self.rotated else immed(self.fcw2(rotated=True))
        self.rotated = True
        return words


class DDTOp:
    """One DDT structure — a dynamic-section deck directive.  This is the
    unit the GPC interpreter steps through and the unit deck flow control counts in: a
    `TEST=(var,bit,N)` skip and a `BR=n` both measure N in structures.

    Each one of the subclasses corresponds to a CASE inside DCI#FMT's top
    level DO block.  CASE 'x' notes in the comments indicate the actual
    case label.

    Subclasses parse their directive value in `parse()`, name their PADR
    referents in `padr_names()`, and emit their words + comments in
    `build(deu)`.

    An IMMED UPDATE instruction can span several ops: consecutive position
    fields (XC/YC) share one 0x5000|n header, attached by the builder to the
    FIRST op of the run (`immed_n` = the run's total FCW count); the run's
    remaining ops hold FCW payload only.

    kind       — deck directive keyword ('TEST', 'BR', 'XC', 'RATE', ...)
    rule       — the directive's argument rule, from the LANGUAGE table
    value      — the directive's raw value text, where a pass needs it
    args       — parsed top-level argument list (subscript-aware), or None
    comments   — annotation lines (become `C` cards in the HAL output)
    words      — list of int | fcw.FCW | Padr
    immed_n    — FCW count of the IMMED run this op heads (None otherwise)
    if_on      — IF ops: True for `IF(...) = ON`
    conv       — VPARM ops: the effective (inherited) deck CONV letter, or None
    skip_at    — words index of an unresolved branch-skip slot (BR/ELSE/IF=ON)
    typelen_at — words index of an unresolved TEST type/length slot (TEST/IF)
    run_break  — a branch label precedes this op (breaks an IMMED run)

    Every directive's rule lives in the LANGUAGE table at the end of this
    module.  A rule in field form (`name[:int][?]` per positional argument)
    is bound by the default `parse()` to the attribute `name` (None when an
    optional argument is absent), so later passes read named fields.

    Unresolved slots are emitted as 0 and MUST be filled by the resolve
    passes in `ddt`, which raise `Error` on anything they cannot fill — no
    placeholder survives into the output."""

    def __init__(self, key=None, value=None, rule=None):
        self.key = key
        self.kind = key.split("(")[0].strip() if key else "?"
        self.rule = rule
        self.value = value
        self.args = None
        self.comments = []
        self.words = []
        self.immed_n = None
        self.if_on = None
        self.conv = None
        self.skip_at = None
        self.typelen_at = None
        self.run_break = False
        self.parse()

    def __repr__(self):
        return "%s(%r, %d words)" % (type(self).__name__, self.kind,
                                     len(self.words))

    def parse(self):
        """Derive parse-time fields from key/value: bind the rule's
        arguments (when it is a field form) and any slot indexes
        (subclasses)."""
        if self.rule and self.rule[0].islower():
            self._bind(fcw.argsplit(self.value))

    def _bind(self, args):
        """Bind `args` to named attributes per the rule, validating as we
        go.  An empty argument (`a,,b`) is an absent one; arguments past the
        rule stay available in `args`."""
        self.args = args
        for i, tok in enumerate(self.rule.split()):
            name, _, ty = tok.rstrip("?").partition(":")
            v = args[i] if i < len(args) else None
            if v == "":
                v = None
            if v is None and not tok.endswith("?"):
                raise Error("missing '%s' in %s = %s"
                            % (name, self.kind, self.value))
            if ty == "int" and v is not None and not isinstance(v, int):
                raise Error("'%s' must be a number in %s = %s"
                            % (name, self.kind, self.value))
            setattr(self, name, v)

    def padr_names(self):
        """This op's PADR referent names, in emission order."""
        return []

    def build(self, deu):
        """Emit `words` and `comments` (called once, in deck order), reading
        and updating the shared `DeuState`."""
        raise NotImplementedError

    def _vs(self):
        return str(self.value).strip()


class RawOp(DDTOp):
    """An op whose words/comments are supplied directly — the DDT terminator
    (ALIGN) and PAD fill, which come from layout arithmetic, not a deck
    directive."""

    def __init__(self, kind, comments, words):
        super().__init__()
        self.kind = kind
        self.comments = list(comments)
        self.words = list(words)

    def build(self, deu):
        pass


# ---- position fields (share an IMMED header across a run) -------------------
class PosOp(DDTOp):
    """XC/YC position field.  Emits its coordinate FCW only; the builder
    merges consecutive PosOps into one IMMED run (lead NOOP + 0x5000|n
    header on the first op)."""

    def build(self, deu):
        self.comments = ["-- %s = %s" % (self.kind, self._vs())]
        self.words = [fcw.xpos(self.value) if self.kind == "XC"
                      else fcw.ypos(self.value)]


# ---- rate group header -------------------------------------------------------
class RateOp(DDTOp):
    """CASE 21: rate-group header [0x5400|rate][count].  The count word is
    the group's worst-case FCW draw, filled by `ddt._resolve_rate_count`."""

    def build(self, deu):
        self.comments = ["-- RATE = %s" % self.value]
        self.words = [0x5400 | int(self.value), 0]
        self.value = self._vs()


# ---- remote directives: fixed opcode + one PADR ------------------------------
_PADR = object()
_REMOTE = {                                    # directive: [word spec]
    "XCR":     [0x2000, _PADR],                # CASE 8  remote X coordinate
    "YCR":     [0x2400, _PADR],                # CASE 9  remote Y coordinate
    "FCWSR":   [0x0800, _PADR],                # CASE 2  array-element pointer
    "SBC":     [0x3400, _PADR],                #         status byte check
    "COLORR":  [0x6800, _PADR],                # CASE 26 remote color
    "ANGLER":  [0x3800, _PADR],                # CASE 14 remote angle
    "CARRTN":  [0x5001, FCW.carrtn()],         #         carriage return (IMMED 1)
    "CIRCR":   [0x4000, _PADR],                # CASE 16 remote circle
    "LSITE":   [0x6401, _PADR],                # CASE 25 land-site (opcode + low flag bit)
    "TRANSXR": [0x1800, _PADR],                # CASE 6  remote X translate
    "TRANSYR": [0x1C00, _PADR],                # CASE 7  remote Y translate
}
_REMOTE_COMMENT = {"CARRTN": "-- CARRTN -- CARRIAGE RETURN"}


class RemoteOp(DDTOp):
    """A `_REMOTE`-table directive: a fixed opcode word then a single PADR
    pointer to the variable it reads (`var`; CARRTN takes none)."""

    def padr_names(self):
        return [self.var] if self.rule else []

    def build(self, deu):
        self.comments = [_REMOTE_COMMENT.get(
            self.kind, "-- %s = %s" % (self.kind, "" if self.value is None
                                       else self._vs()))]
        names = iter(self.padr_names())
        # FCW table entries are copied — the spec instance is shared and
        # FCWs are mutable.
        self.words = [Padr(next(names)) if t is _PADR
                      else (FCW(t.word) if isinstance(t, FCW) else t)
                      for t in _REMOTE[self.kind]]


class AnglerOp(RemoteOp):
    """CASE 14 remote angle.  Enabling rotation also switches the mode FCW —
    on the OFF->ON transition only (a second consecutive remote angle
    carries no mode word)."""

    def build(self, deu):
        super().build(deu)
        self.words += deu.enter_rotation()


# ---- inline IMMED directives (mode / drawing payload) ------------------------
class SpcharOp(DDTOp):
    """IMMED(1) special-character glyph FCW."""

    def build(self, deu):
        self.comments = ["-- SPCHAR = %s" % self._vs()]
        self.words = immed(fcw.spchar_word(self.value))


class VdispOp(DDTOp):
    """IMMED(1) value-display FCW."""

    def build(self, deu):
        self.comments = ["-- VDISP = %s" % self._vs()]
        self.words = immed(FCW.value_display(fcw.num(self.value)))


class HexOp(DDTOp):
    """CASE 24 HEXBITS sub-bit attribute: HEX=(start,nbits) -> one word,
    0x6000 | (start-1)<<5 | (nbits-1) (bits 0-5 opcode BIN'011000', 6-10
    start-1, 11-15 count-1).  Emits nothing at runtime — the following
    VPARM's draw carries the chars (see `ddt._rate_count`)."""

    def build(self, deu):
        self.comments = ["-- HEX = %s" % self.value]
        self.words = [0x6000 | ((self.start - 1) & 0x1F) << 5
                      | ((self.nbits - 1) & 0x1F)]


class IntOp(DDTOp):
    """IMMED(1) intensity mode FCW (latches into the attribute state)."""

    def build(self, deu):
        deu.intensity = self._vs() in ("D", "ON")
        self.comments = ["-- INT = %s" % self._vs()]
        self.words = immed(deu.fcw1())


class SizeOp(DDTOp):
    """Character size: IMMED(2) major/minor pitch pair + IMMED(1) mode FCW."""

    def build(self, deu):
        deu.size_large = self._vs() == "L"
        if deu.size_large:
            self.comments = ["-- SIZE = L -- CHANGE CHARACTER SIZE TO LARGE"]
            self.words = immed(FCW.major_increment(fcw.COL_PITCH_L),
                               FCW.minor_increment(-fcw.ROW_PITCH_L)) \
                + immed(FCW.char_mode(large=True))
        else:
            self.comments = ["-- SIZE = S -- CHANGE CHARACTER SIZE TO SMALL"]
            self.words = immed(FCW.major_increment(fcw.COL_PITCH),
                               FCW.minor_increment(-fcw.ROW_PITCH)) \
                + immed(FCW.char_mode())


class BlinkOp(DDTOp):
    """IMMED(1) blink mode FCW (carries the intensity state along)."""

    def build(self, deu):
        deu.blink = self._vs() == "E"
        self.comments = ["-- BLINK = %s -- %sABLE BLINKING"
                         % (self._vs(), "EN" if deu.blink else "DIS")]
        self.words = immed(deu.fcw1())


class DashOp(DDTOp):
    """IMMED(1) dashed-line mode FCW.  In the 0x3800 mode family like
    INT/BLINK; both E/D and ON/OFF spellings occur in decks.  The dash bit
    rides per-word and is not latched."""

    def build(self, deu):
        if self._vs() in ("E", "ON"):
            self.comments = ["-- DASH = E -- ENABLE DASHING"]
            self.words = immed(deu.fcw1(dash=True))
        else:
            self.comments = ["-- DASH = %s -- DISABLE DASHING" % self._vs()]
            self.words = immed(deu.fcw1())


class AltcharOp(DDTOp):
    """Alternate-charset symbol: IMMED(3) [FCW2' selecting the alternate
    character set][the glyph][FCW2 restore].

    The value is decimal; its hex form is the alternate character set
    symbol number (STS-83-0020V1-34), which names the symbols but not their
    shapes:

      14 filled circle (alternate landing site 1; landing sites)
      15 filled diamond (alternate landing site 2)
      16 cross, large (IIP, main plot)      17 cross, small (IIP, inset)
      18 Shuttle planform (rotated)         19 Shuttle symbol (rotated)
      1C heading arrow (rotated; up at zero)

    Used by displays 0540G, 0543G and 3041G.  Which FCW2 bits select the
    set is unconfirmed (see `fcw.FCW.altchar`)."""

    def build(self, deu):
        n = fcw.num(self.value)
        self.comments = ["-- ALTCHAR = %s" % self._vs()]
        self.words = immed(deu.fcw2(alt=True), FCW.glyph_single(n),
                           deu.fcw2())


class TransoffOp(DDTOp):
    """Translate off: IMMED(3) zero X- and Y-translate FCWs + the FCW2
    restore.  TRANSXR/TRANSYR at runtime set FCW2's translate bits;
    TRANSOFF resets both axes."""

    def build(self, deu):
        self.comments = ["-- TRANSOFF"]
        self.words = immed(FCW.translate_x(0), FCW.translate_y(0),
                           deu.fcw2())


class ColorOp(DDTOp):
    """IMMED(1) FCW3 replacement: COLOR=n selects color n; COLOR=DEU
    restores the DEU default."""

    def build(self, deu):
        word = FCW.color_mode(None if self._vs() == "DEU"
                              else fcw.num(self.value))
        self.comments = ["-- COLOR = %s" % self._vs()]
        self.words = immed(word)


class AxisOp(DDTOp):
    """IMMED(3) spacing-direction change (latches into the axis state)."""

    def build(self, deu):
        axis = self._vs()
        deu.axis_y = axis == "Y"
        self.comments = ["-- AXIS = %s -- Direction of Spacing is the %s axis"
                         % (axis, axis)]
        self.words = immed(*fcw.axis_fcws(self.value))


class LineOp(DDTOp):
    """IMMED(1) line/row position step (direction from the axis state)."""

    def build(self, deu):
        self.comments = ["-- LINE = %s" % self._vs()]
        self.words = immed(fcw.line_word(self.value, deu.axis_y))


class SpaceOp(DDTOp):
    """IMMED(2) character-pitch spacing: SPACE=n spaces n char cells at the
    CURRENT character size's major increment (the same increments the SIZE
    FCW pairs set)."""

    def build(self, deu):
        pitch = fcw.COL_PITCH_L if deu.size_large else fcw.COL_PITCH
        self.comments = ["-- SPACE = %s" % self._vs()]
        self.words = immed(FCW.major_increment(pitch * fcw.num(self.value)),
                           FCW.noop())


class ReptOp(DDTOp):
    """IMMED(2) repeat + fill-glyph FCW pair."""

    def build(self, deu):
        self.comments = ["-- REPT = %s" % (self.value or "")]
        self.words = immed(*fcw.rept_words(self.value))


class CharOp(DDTOp):
    """Dynamic literal text: one IMMED run of character FCWs."""

    def build(self, deu):
        self.comments = ["-- CHAR = %s" % (self.value or "")]
        self.words = immed(*fcw.chars(fcw.text_of(self.value)))


# ---- value / pointer directives ----------------------------------------------
# VPARM descriptor word (0x14xx): output attribute (intensity class) and
# conversion (numeric base / type) of a displayed value.
_ATTR_CODE = {"S": 1, "H": 2, "B": 2, "F": 3, "I": 3, "D": 4}
_CONV_CODE = {"G": 1, "I": 2, "S": 2, "H": 3, "C": 4, "D": 5, "P": 0xA}


def descriptor(a):
    """VPARM descriptor FCW: 0x1400 | ATTR<<4 | CONV."""
    return 0x1400 | (_ATTR_CODE.get(a.get("ATTR", "H"), 2) << 4) \
        | _CONV_CODE.get(a.get("CONV", "I"), 2)


class VparmOp(DDTOp):
    """CASE 5 displayed value: [descriptor][fmtword][PADR].  A VPARM that
    omits ATTR/CONV/FMT/SIGN inherits them from the preceding VPARM
    (`deu.vparm`)."""

    def parse(self):
        self.attrs = vparm_attrs(self.value)

    def padr_names(self):
        return [self.attrs.get("NAME", "?")]

    def build(self, deu):
        a = {**deu.vparm, **self.attrs}          # ATTR/CONV/FMT/SIGN inherit
        deu.vparm = a
        f = str(a.get("FMT", "1.0")).split(".")
        digits = int(f[0]); decimals = int(f[1]) if len(f) > 1 else 0
        low = {"P": 0x04, "U": 0x08, "R": 0x10}.get(a.get("SIGN"), 0x18)
        if a.get("ZEROES") == "YES":             # leading-zero fill flag
            low |= 0x40
        fmtword = (digits << 12) | (decimals << 8) | low
        self.conv = a.get("CONV")
        self.comments = ["-- VPARM = %s" % (self.value or "")]
        self.words = [descriptor(a), fmtword, Padr(self.padr_names()[0])]


class CharrOp(DDTOp):
    """CASE 10 remote characters: [0x2800|count][PADR]."""

    def padr_names(self):
        return [self.var]

    def build(self, deu):
        self.comments = ["-- CHARR = %s" % (self.value or "")]
        self.words = [0x2800 | self.n, Padr(self.var)]


class DmdupdOp(DDTOp):
    """CASE 23 demand-update group list.  The numeric form supplies the
    DEFAULT table itself: `DMDUPD=(1,1,…)` is treated as
    `(CZ2B_DMDUPD_ADD,0,1,1,…)`."""

    def parse(self):
        toks = fcw.argsplit(self.value)
        if isinstance(toks[0], int):             # numeric: default table, K=1
            self.k, self.ngroups, self.rest = 1, toks[0], toks[1:]
            self.table = "CZ2B_DMDUPD_ADD"
        else:                                    # named: (TABLE,K[,NGROUPS,…])
            self.table = toks[0]
            self.k = toks[1]
            self.ngroups = toks[2] if len(toks) > 2 else 0
            self.rest = toks[3:]

    def padr_names(self):
        names = [self.table]
        for g in range(self.ngroups):
            names += [self.rest[g * 3 + 1], self.rest[g * 3 + 2]]
        return names

    def build(self, deu):
        self.comments = ["-- DMDUPD = %s" % (self.value or "")]
        words = [0x5C00 | (self.ngroups << 4) | ((self.k - 1) & 0xF),
                 Padr(self.table)]
        for g in range(self.ngroups):            # per group: [count][vA][vB]
            words += [self.rest[g * 3], Padr(self.rest[g * 3 + 1]),
                      Padr(self.rest[g * 3 + 2])]
        self.words = words


class MdtOp(DDTOp):
    """CASE 17 MDISC: [0x4400|axis][X-table PADR][2nd-table PADR]; the low
    bit is the second coordinate's axis (Z=1)."""

    def padr_names(self):
        return [self.xtab, self.tab2]

    def build(self, deu):
        self.comments = ["-- MDT = %s" % (self.value or "")]
        self.words = [0x4400 | (1 if "Z" in self.tab2.upper() else 0),
                      Padr(self.xtab), Padr(self.tab2)]


class BltOp(DDTOp):
    """CASE 1 bi-level test: [w0][cond1 PADR][cond2 PADR][attr].  w0 packs
    both conds' hardware bit positions (compool-derived);
    attr = 0x6000 | (len1-1)<<5 | (len2-1)."""

    def padr_names(self):
        return [self.var, self.var2]

    def build(self, deu):
        w0 = deu.w0fn(self) if deu.w0fn else None
        if w0 is None:
            raise Error("cannot derive condition word: BLT = %s"
                        % _oneline(self.value))
        ml = [p for p in (self.len1, self.len2) if p is not None] or [16, 16]
        ml1, ml2 = (ml + ml)[:2]
        attr = 0x6000 | (((ml1 - 1) & 0x7F) << 5) | ((ml2 - 1) & 0x1F)
        self.comments = ["-- BLT = %s" % (self.value or "")]
        self.words = [w0, Padr(self.var), Padr(self.var2), attr]


class BlinkrOp(DDTOp):
    """CASE 11 remote blink: [0x2C00|bit-1][PADR]."""

    def padr_names(self):
        return [self.var]

    def build(self, deu):
        self.comments = ["-- BLINKR = %s" % (self.value or "")]
        self.words = [0x2C00 | ((self.bit - 1) & 0x3FF), Padr(self.var)]


class RtcOp(DDTOp):
    """CASE 3 RTEXT remote-text check: [w0][cond PADR][char PADR][attr].
    The char variable packs the two alternative strings concatenated
    (half-select); attr = 0x6000 | (width-1)<<5."""

    def padr_names(self):
        return [self.var, self.text]

    def build(self, deu):
        w0 = deu.w0fn(self) if deu.w0fn else None
        if w0 is None:
            raise Error("cannot derive condition word: RTC = %s"
                        % _oneline(self.value))
        maxlen = next((p for p in (self.sign, self.width)
                       if isinstance(p, int)), 16)
        attr = 0x6000 | (((maxlen - 1) & 0x7F) << 5)
        self.comments = ["-- RTC = %s" % (self.value or "")]
        self.words = [w0, Padr(self.var), Padr(self.text), attr]


class TestOp(DDTOp):
    """CASE 18 discrete test: [w0][type/length][cond PADR]; word 1 is the
    conditional-skip type/length slot, filled by `ddt._resolve_flow` from
    `target` (a skip count or a label) and `width`."""

    def parse(self):
        super().parse()
        self.typelen_at = 1

    def padr_names(self):
        return [self.var]

    def build(self, deu):
        w0 = deu.w0fn(self) if deu.w0fn else None
        if w0 is None:
            raise Error("cannot derive condition word: TEST = %s"
                        % _oneline(self.value))
        self.comments = ["-- TEST = %s" % (self.value or "")]
        self.words = [w0, 0, Padr(self.var)]


class BrOp(DDTOp):
    """CASE 22 unconditional branch: [0x5800][skip]; the skip slot is
    filled by `ddt._resolve_flow` from `target` (a skip count or a label —
    `value` keeps the verbatim text for the comment echo)."""

    def parse(self):
        self.value = self._vs() if self.value else ""
        self.target = atom(self.value) if self.value else ""
        self.skip_at = 1

    def build(self, deu):
        self.comments = ["-- BR = %s" % self.value]
        self.words = [0x5800, 0]


class VcordrOp(DDTOp):
    """CASE 4 remote vector: [0x1000][x1][y1][x2][y2] — four coordinate
    PADRs."""

    def padr_names(self):
        return [self.x1, self.y1, self.x2, self.y2]

    def build(self, deu):
        self.comments = ["-- VCORDR = %s" % (self.value or "")]
        self.words = [0x1000] + [Padr(n) for n in self.padr_names()]


class VcordOp(DDTOp):
    """Static vector drawn from the DDT (VCORD/VCORDA): IMMED(5) of the
    line's FCWs + IMMED(1) mode restore (the current character size)."""

    def build(self, deu):
        v6 = fcw.vector(fcw.coords(self.value), absolute=(self.kind == "VCORDA"))
        self.comments = ["-- %s = %s" % (self.kind, self.value or "")]
        self.words = immed(*v6[:5]) \
            + immed(FCW.char_mode(large=deu.size_large))


class AngleOp(DDTOp):
    """Character rotation.  Numeric ANGLE=n emits the rotation FCW (with the
    mode switch on the OFF->ON transition only); ANGLE=0 restores the mode
    and emits the ANGZERO entry [4C00, 4000].  A variable ANGLE is the
    CASE 14 remote form ([0x3800][PADR]).

    Any angle is accepted: op 0x4 carries 12 bits at 360/4096 per unit."""

    def parse(self):
        self.value = self._vs() if self.value else ""
        self.numeric = fcw.is_num(self.value)

    def padr_names(self):
        return [] if self.numeric else [self.value]

    def build(self, deu):
        self.comments = ["-- ANGLE = %s" % self.value]
        if self.numeric:
            n = float(self.value)
            if n == 0:
                self.words = immed(deu.fcw2(rotated=False)) + [0x4C00, 0x4000]
                deu.rotated = False
                return
            self.words = immed(FCW.rotation(n)) + deu.enter_rotation()
        else:                                    # variable -> CASE 14 remote
            self.words = [0x3800, Padr(self.value)] + deu.enter_rotation()


# ---- conditional blocks --------------------------------------------------------
class IfOp(DDTOp):
    """Conditional block: `IF(cond,bit[,width]) = ON/OFF` emits a TEST
    (CASE 18); the =ON form appends an inline skip-if-false BRANCH.  The
    type/length and branch-skip slots are filled by `ddt._resolve_flow`
    against the matching ELSE/ENDIF.

    After `ddt.build_ddt` structures the op list, `body` holds the ops
    between this IF and its ENDIF (a flat view — they remain in the main
    sequence for flow counting), with `else_op`/`endif_op` references."""

    def parse(self):
        self._bind(fcw.argsplit(self.key[self.key.find("("):]))
        self.if_on = self._vs() == "ON"
        self.typelen_at = 1
        self.body, self.else_op, self.endif_op = [], None, None

    def padr_names(self):
        return [self.var]

    def build(self, deu):
        w0 = deu.w0fn(self) if deu.w0fn else None
        if w0 is None:
            raise Error("cannot derive condition word: %s = %s"
                        % (self.key, _oneline(self.value)))
        self.comments = ["-- %s = %s" % (self.key, self._vs())]
        self.words = [w0, 0, Padr(self.var)]
        if self.if_on:                           # =ON: TEST + inline BRANCH
            self.words += [0x5800, 0]
            self.skip_at = len(self.words) - 1


class ElseOp(DDTOp):
    """End of an IF's true block: an unconditional branch over the else
    block, resolved to the matching ENDIF."""

    def parse(self):
        self.skip_at = 1

    def build(self, deu):
        self.comments = ["-- ELSE"]
        self.words = [0x5800, 0]


class EndifOp(DDTOp):
    """Branch target only — no words."""

    def build(self, deu):
        self.comments = ["-- ENDIF"]
        self.words = []


# ---- the directive language -----------------------------------------------
# Every deck directive in one table: its DDTOp class (None = structural, no
# DDT entry) and its argument rule.  A rule in lowercase field form
# (`name[:int][?]` per positional argument) is machine-bound to op
# attributes by `DDTOp._bind`; any other rule documents the value's form,
# parsed by the op's builder — NUM `fcw.num`, COORD `fcw.coord`'s n/nnnA,
# TEXT verbatim (glyphs and spacing significant).  A branch label (`NAME:`)
# is not a directive (`ddt._parse_ops` handles it), and the deck-editing
# directives (`deck.EDIT_DIRECTIVES`) are stripped before encoding.
LANGUAGE = {
    # deck structure — no DDT entry (consumed by encode / kvt / static)
    "HEADER":   (None, "TEXT"),          # display page name nnnnF (+ aliases)
    "CRTFMT":   (None, "TEXT"),          # critical-format background name
    "INCLUDE":  (None, "TEXT"),          # referenced compool
    "DEULOC":   (None, "NUM"),           # external background's CFIT slot addr
    "KEYS":     (None, "TEXT"),          # keyboard-entry class flag
    "ITEM":     (None, "(n[,class[,fmt[,min,max]]])"),   # KVT entry (kvt.py)
    "PAD":      (None, "NUM"),           # trailing fill halfwords
    "STAT":     (None, None),            # begin the static section
    "VARY":     (None, None),            # begin the dynamic (DDT) section
    "END":      (None, None),            # end of deck
    "STOP":     (None, None),

    # position / drawing content (static section and DDT)
    "XC":       (PosOp,     "COORD"),    # beam X: column, or nnnA absolute
    "YC":       (PosOp,     "COORD"),    # beam Y: row, or nnnA absolute
    "CHAR":     (CharOp,    "TEXT"),     # literal text
    "SPCHAR":   (SpcharOp,  "NUM"),      # special-character glyph code
    "CARRTN":   (RemoteOp,  None),       # carriage return
    "VCORD":    (VcordOp,   "(x1,y1,x2,y2)"),   # line, char-cell coords
    "VCORDA":   (VcordOp,   "(x1,y1,x2,y2)"),   # line, absolute screen coords
    "LINE":     (LineOp,    "COORD"),    # step down n rows, or nnnA units
    "SPACE":    (SpaceOp,   "COORD"),    # advance n columns, or nnnA units
    "REPT":     (ReptOp,    "(count,skip[,fill])"),   # repeated fill glyph
    "AXIS":     (AxisOp,    "X|Y"),      # spacing direction (latched)
    "VDISP":    (VdispOp,   "NUM"),      # value-display code

    # mode switches (latched into DeuState)
    "INT":      (IntOp,     "D|N|ON|OFF"),        # double intensity
    "SIZE":     (SizeOp,    "S|L"),               # character size
    "BLINK":    (BlinkOp,   "E|D"),               # blink
    "DASH":     (DashOp,    "E|D|ON|OFF"),        # dashed lines (per-word)
    "COLOR":    (ColorOp,   "NUM|DEU"),           # FCW3 color select
    "ALTCHAR":  (AltcharOp, "NUM"),               # alternate-charset glyph
    "TRANSOFF": (TransoffOp, None),               # reset X/Y translate
    "ANGLE":    (AngleOp,   "NUM|var"),           # rotation (deg) / remote

    # dynamic fields (one DCI#FMT CASE each; a `var` becomes a PADR)
    "VPARM":    (VparmOp,  "(NAME=var[,ATTR=,CONV=,FMT=d.d,SIGN=,ZEROES=])"),
    "HEX":      (HexOp,    "start:int nbits:int"),
    "CHARR":    (CharrOp,  "var n:int"),
    "RTC":      (RtcOp,    "var bit:int text len:int? sign? width:int?"),
    "BLT":      (BltOp,    "var bit:int var2 bit2:int len1:int? len2:int?"),
    "MDT":      (MdtOp,    "xtab tab2"),
    "DMDUPD":   (DmdupdOp, "(TABLE,K[,n,count,varA,varB..]) | (n,counts..)"),
    "BLINKR":   (BlinkrOp, "var bit:int"),
    "VCORDR":   (VcordrOp, "x1 y1 x2 y2"),
    "XCR":      (RemoteOp, "var"),       # CASE 8  remote X coordinate
    "YCR":      (RemoteOp, "var"),       # CASE 9  remote Y coordinate
    "FCWSR":    (RemoteOp, "var"),       # CASE 2  array-element pointer
    "SBC":      (RemoteOp, "var"),       #         status byte check
    "COLORR":   (RemoteOp, "var"),       # CASE 26 remote color
    "ANGLER":   (AnglerOp, "var"),       # CASE 14 remote angle
    "CIRCR":    (RemoteOp, "var"),       # CASE 16 remote circle
    "LSITE":    (RemoteOp, "var"),       # CASE 25 land-site
    "TRANSXR":  (RemoteOp, "var"),       # CASE 6  remote X translate
    "TRANSYR":  (RemoteOp, "var"),       # CASE 7  remote Y translate

    # flow control (N counts DDT structures; `LABEL:` marks the next one)
    "RATE":     (RateOp,   "NUM"),       # rate-group header (1..6)
    "TEST":     (TestOp,   "var bit:int target width:int?"),
    "BR":       (BrOp,     "N|LABEL"),
    "IF":       (IfOp,     "var bit:int width:int?"),   # IF(...) = ON|OFF
    "ELSE":     (ElseOp,   None),
    "ENDIF":    (EndifOp,  None),
}


def op_for(k, v):
    """The DDTOp for directive `k = v`: None for a structural directive
    (no DDT entry); Error for a directive not in the LANGUAGE."""
    kind = k.split("(")[0].strip()
    if kind not in LANGUAGE:
        raise Error("unknown directive %s"
                    % (k if v is None else "%s = %s" % (k, v)))
    cls, rule = LANGUAGE[kind]
    return cls(k, v, rule) if cls else None
