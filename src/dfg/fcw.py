#!/usr/bin/env python3
#
# Format Control Word (FCW) primitives — the DEU display bytecode.
# 
# An FCW is a 16-bit word the DEU interprets to draw one element.  The `FCW`
# class is the word type: storage is the 16-bit int (`word`), and the per-op
# bit fields are read/write properties, so the class doubles as the format
# reference (see its docstring).  The module-level functions translate deck
# directive values (character cells, text, line endpoints) into FCWs.
# 
# Screen geometry: the DEU is a stroke-written vector display; the beam is
# positioned on a 1536-unit screen-coordinate grid.  A character CELL column XC /
# row YC maps to a screen X/Y; `nnnA` in the deck is an absolute screen-coordinate
# override.
#
import re
from dataclasses import dataclass

from .deck import atom, group_parts
from .model import Error


def _field(shift, width, doc):
    """Read/write property over `width` bits of FCW.word at bit `shift`."""
    mask = (1 << width) - 1

    def fget(self):
        return (self.word >> shift) & mask

    def fset(self, v):
        self.word = (self.word & ~(mask << shift)) | ((v & mask) << shift)

    return property(fget, fset, doc=doc)


def _flag(bit, doc):
    """Read/write boolean property over one bit of FCW.word."""

    def fget(self):
        return bool(self.word & (1 << bit))

    def fset(self, v):
        self.word = (self.word | (1 << bit)) if v else (self.word & ~(1 << bit))

    return property(fget, fset, doc=doc)


def _signed(shift, width, doc):
    """Read/write two's-complement property over `width` bits at `shift`."""
    mask = (1 << width) - 1
    sign = 1 << (width - 1)

    def fget(self):
        v = (self.word >> shift) & mask
        return v - (1 << width) if v & sign else v

    def fset(self, v):
        self.word = (self.word & ~(mask << shift)) | ((v & mask) << shift)

    return property(fget, fset, doc=doc)


@dataclass
class FCW:
    """A 16-bit DEU Format Control Word — one drawing op the DEU interprets
    directly.  In the compiled display the static section is a pure FCW
    stream; in the DDT, FCWs appear as IMMED payloads copied through to the 
    DEU.  

    `op` selects the operation; the fields below are grouped by op.    

      op   meaning                        fields
      0x0  no-op (word 0) / REPEAT        rept_count
      0x1  branch to a 12-bit DEU address (`Branch`); doubles as the
           fill-address I/O header word.  Encoding for targets >= 0x1000
           is unknown (see `deu_return`)
      0x2  unknown (unused)
      0x3  mode-register write            `reg` selects the register:
             reg 0  FCW2 char/beam mode   mode_code, upright, altchar,
                                          altchar_rot
             reg 1  FCW3 color            color_select, color
             reg 2  FCW1 attributes       intensity, blink, dash, axis_y
             reg 3  value display         vdisp
      0x4  character rotation             quarter_turns
      0x5  major-axis step                major_step
      0x6  unknown (unused)
      0x7  minor-axis step                minor_step
      0x8  X beam position / translate    x, translate
      0x9  Y beam position / translate    y, translate
      0xA  vector slope word              y_major, sign_differ, slope
      0xB  vector major-extent word       major_negative, major_len
      0xC  glyph pair                     g1, g2
    """
    word: int = 0

    def __repr__(self):
        return "FCW(0x%04X)" % (self.word & 0xFFFF)

    def __int__(self):
        return self.word & 0xFFFF

    __index__ = __int__

    def __bool__(self):
        return bool(self.word)

    @property
    def op(self):
        """The operation selector: the top nibble, except that glyph pairs
        occupy the whole top quadrant (2-bit prefix 11) — any word >= 0xC000
        reports op 0xC."""
        n = (self.word >> 12) & 0xF
        return 0xC if n >= 0xC else n

    @op.setter
    def op(self, v):
        self.word = (self.word & 0x0FFF) | ((v & 0xF) << 12)

    # ---- op 0x0 — no-op / REPEAT ----------------------------------------
    # Word 0x0000 is a no-op (position-run lead / fill).  A REPEAT draws the
    # FOLLOWING glyph FCW `rept_count` times; the count is stored
    # subtractively against a fixed base, so this is a whole-word codec
    # rather than a bit field.
    @property
    def rept_count(self):
        """REPEAT count (op 0x0, word != 0): word = 0x0841 - count."""
        return (0x0841 - self.word) & 0xFFFF

    @rept_count.setter
    def rept_count(self, v):
        self.word = (0x0841 - v) & 0xFFFF

    # ---- op 0x3 — mode-register writes ------------------------------------
    reg = _field(10, 2, "op 0x3: destination register — 0 FCW2 (char/beam "
                        "mode), 1 FCW3 (color), 2 FCW1 (attributes), "
                        "3 value display")
    # FCW2 (reg 0) — character / beam mode
    mode_code = _field(0, 4, "FCW2: beam mode — 5 begin vector, 6 chars "
                             "small, 7 chars large (rotation clears the "
                             "upright bit, giving 2/3)")
    upright = _flag(2, "FCW2: upright characters; cleared while rotation "
                       "is active")
    altchar = _flag(6, "FCW2: select the alternate character set")
    altchar_rot = _flag(5, "FCW2: alternate-set variant used while "
                           "rotation is active")
    # FCW3 (reg 1) — color
    color_select = _flag(7, "FCW3: color select enable (off = DEU default)")
    color = _field(0, 7, "FCW3: color code (the DEU default word carries "
                         "code 40)")
    # FCW1 (reg 2) — drawing attributes
    intensity = _flag(3, "FCW1: double intensity (INT=D/ON)")
    blink = _flag(8, "FCW1: blink (BLINK=E)")
    dash = _flag(9, "FCW1: dashed lines (DASH=E/ON)")
    axis_y = _flag(5, "FCW1: spacing direction is the Y axis (AXIS=Y)")
    # value display (reg 3)
    vdisp = _field(0, 10, "VDISP: value-display code")

    # ---- op 0x4 — character rotation --------------------------------------
    quarter_turns = _field(10, 2, "op 0x4: rotation in 90-degree steps")

    # ---- op 0x5 / 0x7 — spacing steps --------------------------------------
    major_step = _signed(0, 11, "op 0x5: step along the major (spacing) "
                                "axis in screen units, 11-bit two's "
                                "complement — +19 small pitch, +24 large, "
                                "-27 rows under AXIS=Y")
    minor_step = _signed(0, 8, "op 0x7: step along the minor axis, 8-bit "
                               "two's complement — -27 small, -32 large, "
                               "+19 under AXIS=Y; bit 11 is always set "
                               "(meaning unknown)")

    # ---- op 0x8 / 0x9 — beam position --------------------------------------
    x = _field(0, 12, "op 0x8: screen X, 0..1535 — cell column c sits at "
                      "1042 + 19c; the `nnnA` absolute form at 1024 + nnn")
    y = _field(0, 12, "op 0x9: screen Y — cell row r sits at 366 - 27r "
                      "(rows run downward); the `nnnA` form at 1902 - nnn")
    translate = _flag(11, "op 0x8/0x9: write the X/Y TRANSLATE register "
                          "instead of the beam position (coordinates never "
                          "exceed 0x5FF, freeing this bit; 0x8800/0x9800 "
                          "zero the translation)")

    # ---- op 0xA / 0xB — vector (line) words --------------------------------
    y_major = _flag(11, "op 0xA: the major axis is Y (|dy| > |dx|)")
    sign_differ = _flag(10, "op 0xA: dx and dy have opposite signs")
    slope = _field(0, 10, "op 0xA: minor/major ratio at 9-bit resolution, "
                          "LSB always 0 — 2*round(minor*512/major), "
                          "saturating at 2*511 (a 45-degree line stores "
                          "1022)")
    major_negative = _flag(11, "op 0xB: the major-axis delta is negative")
    major_len = _field(0, 11, "op 0xB: |major-axis delta| in screen units")

    # ---- op 0xC — glyphs ----------------------------------------------------
    g1 = _field(7, 7, "op 0xC: first glyph of the pair (0 in the "
                      "single-glyph form)")
    g2 = _field(0, 7, "op 0xC: second glyph (or the only one)")

    # ---- constructors (one per op, from field values) ----------------------
    @classmethod
    def noop(cls):
        """The all-zero no-op FCW (position-run lead / null IMMED payload)."""
        return cls(0)

    @classmethod
    def repeat(cls, count):
        """REPEAT FCW: draw the FOLLOWING glyph FCW `count` times."""
        f = cls()
        f.rept_count = count
        return f

    @classmethod
    def attr_mode(cls, intensity=False, blink=False, dash=False, axis_y=False):
        """FCW1 attribute-mode word."""
        f = cls()
        f.op, f.reg = 0x3, 2
        f.intensity, f.blink, f.dash, f.axis_y = intensity, blink, dash, axis_y
        return f

    @classmethod
    def char_mode(cls, large=False, rotated=False, alt=False):
        """FCW2 character-mode word: mode code 6 small / 7 large; rotation
        clears the upright bit; `alt` selects the alternate character set
        (its rotated variant while rotated)."""
        f = cls()
        f.op, f.reg = 0x3, 0
        f.mode_code = 7 if large else 6
        if rotated:
            f.upright = False
        if alt:
            f.altchar = True
            f.altchar_rot = rotated
        return f

    @classmethod
    def vector_begin(cls):
        """FCW2 begin-vector mode word (mode code 5); drawing is closed
        with a plain `char_mode()` restore."""
        f = cls()
        f.op, f.reg, f.mode_code = 0x3, 0, 5
        return f

    @classmethod
    def color_mode(cls, code=None):
        """FCW3 color word: select + code, or the DEU default (select bit
        off, default color code 40) for code None."""
        f = cls()
        f.op, f.reg = 0x3, 1
        f.color_select = code is not None
        f.color = 40 if code is None else code
        return f

    @classmethod
    def color_clear(cls):
        """FCW3 with select off and code 0 — the static-preamble initial
        value."""
        f = cls()
        f.op, f.reg = 0x3, 1
        return f

    @classmethod
    def value_display(cls, n):
        """Value-display FCW (the `vdisp` field)."""
        f = cls()
        f.op, f.reg, f.vdisp = 0x3, 3, n
        return f

    @classmethod
    def rotation(cls, quarter_turns):
        """Character-rotation FCW (turns = angle / 90)."""
        f = cls()
        f.op, f.quarter_turns = 0x4, quarter_turns
        return f

    @classmethod
    def major_increment(cls, units):
        """Major-axis step FCW (op 0x5, the `major_step` field): signed
        screen units."""
        f = cls()
        f.op, f.major_step = 0x5, units
        return f

    @classmethod
    def minor_increment(cls, units):
        """Minor-axis step FCW (op 0x7, the `minor_step` field): signed
        screen units.  Bit 11 is always set (meaning unknown)."""
        f = cls(0x0800)
        f.op, f.minor_step = 0x7, units
        return f

    @classmethod
    def x_position(cls, x):
        """X beam-position FCW (op 0x8), screen units."""
        f = cls()
        f.op, f.x = 0x8, x
        return f

    @classmethod
    def y_position(cls, y):
        """Y beam-position FCW (op 0x9), screen units."""
        f = cls()
        f.op, f.y = 0x9, y
        return f

    @classmethod
    def translate_x(cls, x=0):
        """X TRANSLATE-register write (position op + the translate bit)."""
        f = cls.x_position(x)
        f.translate = True
        return f

    @classmethod
    def translate_y(cls, y=0):
        """Y TRANSLATE-register write."""
        f = cls.y_position(y)
        f.translate = True
        return f

    @classmethod
    def glyph_pair(cls, g1, g2):
        """Two-glyph character FCW (drawn g1 then g2)."""
        f = cls()
        f.op, f.g1, f.g2 = 0xC, g1, g2
        return f

    @classmethod
    def glyph_single(cls, g):
        """Single-glyph character FCW (the glyph rides in the g2 slot)."""
        return cls.glyph_pair(0, g)

    @classmethod
    def carrtn(cls):
        """Carriage-return glyph FCW (CR = glyph 0x0D)."""
        return cls.glyph_single(0x0D)

    @classmethod
    def deu_return(cls):
        """The static-section terminator 0x19EE: branch back into the DEU's
        own program.  Not built as a `Branch`: the encoding of targets
        >= 0x1000 is unknown."""
        return cls(0x19EE)


class Branch(FCW):
    """An op-0x1 DEU-address word: `0x1000 | target` — jump the DEU
    interpreter to DEU memory address `target`.  The same encoding doubles
    as the fill-address I/O header word ("load the following words at
    `target`").  Every CFIT slot is one of these (a display's DEULOC= is
    the address OF such a slot).  The encoding of targets >= 0x1000 is
    unknown (see `FCW.deu_return`)."""

    def __init__(self, target):
        if not 0 <= target < 0x1000:
            raise ValueError("branch target 0x%X outside the 12-bit range"
                             % target)
        super().__init__(0x1000 | target)

    @property
    def target(self):
        return self.word & 0x0FFF

    def __repr__(self):
        return "Branch(0x%03X)" % self.target


# ---- screen geometry constants ----------------------------------------------
GRID = 1536          # screen-coordinate wrap (both axes)
COL_PITCH = 19       # screen units per character column (24 under SIZE=L)
ROW_PITCH = 27       # screen units per character row (32 under SIZE=L)
COL_PITCH_L = 24
ROW_PITCH_L = 32
COL_ORIGIN = 1042    # screen X of cell column 0
ROW_ORIGIN = 366     # screen Y of cell row 0 (rows run downward)
ABS_X_ORIGIN = 1024  # `nnnA` absolute-form X origin
ABS_Y_ORIGIN = 1902  # `nnnA` absolute-form Y origin


def num(v):
    """The integer value of a directive value; Error if not a plain integer."""
    s = str(v).strip()
    if not re.fullmatch(r"-?\d+", s):
        raise Error("expected a number, got %r" % (s,))
    return int(s)


def coord(v):
    """Parse the `n` / `nnnA` coordinate sublanguage -> (value, absolute):
    a plain number is in directive units (cells, rows, columns); the `A`
    suffix is an absolute screen-coordinate override."""
    s = str(v).strip()
    absolute = s.endswith("A")
    t = s[:-1] if absolute else s
    if not re.fullmatch(r"-?\d+", t):
        raise Error("bad coordinate %r" % (s,))
    return int(t), absolute


# ---- coordinate FCWs ---------------------------------------------------------
def cell_x(col):
    """Screen X of character-cell column `col`."""
    return (COL_ORIGIN + COL_PITCH * col) % GRID


def cell_y(row):
    """Screen Y of character-cell row `row` (rows run downward)."""
    return (ROW_ORIGIN - ROW_PITCH * row) % GRID


def xpos(xc):
    """X-coordinate FCW for character column XC (`nnnA` = absolute screen X
    from the absolute origin)."""
    n, absolute = coord(xc)
    return FCW.x_position((ABS_X_ORIGIN + n) % GRID if absolute else cell_x(n))


def ypos(yc):
    """Y-coordinate FCW for character row YC (`nnnA` = absolute screen Y
    from the absolute origin)."""
    n, absolute = coord(yc)
    return FCW.y_position((ABS_Y_ORIGIN - n) % GRID if absolute else cell_y(n))


# ---- glyph FCWs: 0xCxxx packs one or two 7-bit DEU glyphs -------------------
def spchar(n):
    """SPCHAR code -> DEU CHAR-ROM glyph.  Two linear regimes: n-11 for n<47,
    n+45 for n>=47 (e.g. 12->01, 47->5C, 82->7F)."""
    return (n - 11 if n < 47 else n + 45) & 0x7F


_DEU = {"_": 22, "[": 2, "]": 1, "$": 0x5C}      # DEU glyphs that differ from ASCII


def glyph(ch):
    return _DEU.get(ch, ord(ch) & 0x7F)


def rept_char(c):
    """REPT fill argument: a literal char -> its glyph; a multi-digit token -> a
    SPCHAR code; blank -> the space glyph."""
    if c.strip() == "":
        return 0x20                              # blank fill
    c = c.strip()
    return glyph(c) if len(c) == 1 else spchar(int(c))


def rept_words(v):
    """REPT=(count,skip[,fill]) -> its two words: the repeat FCW and the
    fill-glyph FCW (space when no fill is given).  The same pair in the
    static section and as a DDT inline IMMED(2)."""
    p = (v[1:-1] if v.startswith("(") else v).split(",")
    return [FCW.repeat(int(p[0])),
            FCW.glyph_single(rept_char(p[2] if len(p) > 2 else " "))]


def spchar_word(v):
    """SPCHAR=n -> its single glyph FCW."""
    return FCW.glyph_single(spchar(num(v)))


def chars(t):
    """Pack text into character FCWs, two glyphs per word (DEU glyph set).
    A trailing odd glyph occupies the single-glyph form."""
    t = t.replace("\\(", "(").replace("\\)", ")")   # unescape literal parens
    out = []
    for i in range(0, len(t), 2):
        if i + 1 < len(t):
            out.append(FCW.glyph_pair(glyph(t[i]), glyph(t[i + 1])))
        else:
            out.append(FCW.glyph_single(glyph(t[i])))
    return out


# ---- vector FCWs: a straight line from (x1,y1) to (x2,y2) --------------------
def vector(args, absolute):
    """Line FCW group for VCORD (char coords) / VCORDA (absolute screen coords):
    [begin-vector, XPOS1, YPOS1, Avec, Bvec, char-mode restore] — move to the
    start, then draw.

    The major axis is the larger delta (X on a tie); Bvec carries its signed
    extent, Avec the slope.  The slope encodes minor/major at 9-bit resolution
    inside the 10-bit field (LSB always 0): slope = 2*round(minor*512/major),
    round-half-up = (minor*1024 + major)//(2*major).  The 9-bit value
    saturates at 511, so a 45 deg line stores 1022."""
    x1, y1, x2, y2 = [int(x) for x in args]
    if absolute:
        xp1 = FCW.x_position((ABS_X_ORIGIN + x1) % GRID)
        yp1 = FCW.y_position((ABS_Y_ORIGIN - y1) % GRID)
        dx, dy = (x2 - x1), -(y2 - y1)
    else:
        xp1, yp1 = xpos(x1), ypos(y1)
        dx, dy = COL_PITCH * (x2 - x1), -ROW_PITCH * (y2 - y1)
    if abs(dy) > abs(dx):
        major, minor = dy, abs(dx)
    else:
        major, minor = dx, abs(dy)
    av = FCW()
    av.op = 0xA
    av.y_major = abs(dy) > abs(dx)
    av.sign_differ = dx * dy < 0
    av.slope = 2 * min((minor * 1024 + abs(major)) // (2 * abs(major)), 511) \
        if minor else 0
    bv = FCW()
    bv.op = 0xB
    bv.major_negative = major < 0
    bv.major_len = abs(major)
    return [FCW.vector_begin(), xp1, yp1, av, bv, FCW.char_mode()]


# ---- axis (spacing direction) mode switch ----------------------------------
def axis_fcws(v):
    """AXIS=Y / AXIS=X mode-switch FCW triplet (same in static and DDT):
    the FCW1 axis flag, then the major and minor steps for that direction
    (X: +1 column / -1 row;  Y: -1 row / +1 column)."""
    if str(v).strip() == "Y":
        return [FCW.attr_mode(axis_y=True), FCW.major_increment(-ROW_PITCH),
                FCW.minor_increment(COL_PITCH)]
    return [FCW.attr_mode(), FCW.major_increment(COL_PITCH),
            FCW.minor_increment(-ROW_PITCH)]


def line_word(v, axis_y=False):
    """LINE=n / LINE=nnnA -> one step word moving the beam down n rows
    (`nnnA` = screen units, else the row pitch per row).  Under X-axis
    spacing rows are the MINOR axis (a plain `minor_increment(-units)`);
    under AXIS=Y they are the major axis, encoded
    0x5700 | ((256 - units) & 0xFF) — equal to `major_increment(-units)`
    for units <= 255; larger steps are unknown."""
    n, absolute = coord(v)
    units = n if absolute else ROW_PITCH * n
    if axis_y:
        return FCW(0x5700 | ((256 - units) & 0xFF))
    return FCW.minor_increment(-units)


def space_word(v, axis_y=False):
    """SPACE=n / SPACE=nnnA -> one step word advancing n character columns
    (`nnnA` = screen units, else the column pitch per column).  The column
    advance is the major axis under AXIS=X, the MINOR axis under AXIS=Y
    (XG0210: AXIS=Y,SPACE=5 -> 785F; XG3041: SPACE=160A)."""
    n, absolute = coord(v)
    units = n if absolute else COL_PITCH * n
    return FCW.minor_increment(units) if axis_y else FCW.major_increment(units)


def is_num(s):
    """True for a literal numeric directive value (angle/coord constant)."""
    try:
        float(str(s).strip())
        return True
    except ValueError:
        return False


def coords(v):
    """Split a group value `(a,b,c,d)` into its stripped elements."""
    inner = v[1:-1] if v and v.startswith("(") else (v or "")
    return [p.strip() for p in inner.split(",")]


def argsplit(v):
    """Top-level comma split of a `(...)` group value into typed arguments,
    subscript-aware: commas inside a `$(r,c)` subscript (2-D array refs like
    `VAR$(1,2)`) must not split.  The deck grammar's group splitter does the
    splitting; a plain-integer argument becomes an int, anything else stays
    the stripped string."""
    return [atom(p) for p in group_parts(v or "")]


def text_of(v):
    """The inside of a `CHAR=(...)` value, NOT stripped — leading/trailing spaces
    are display glyphs and must survive (they shift the two-glyphs-per-word
    packing)."""
    return v[1:-1] if v and v.startswith("(") else (v or "")
