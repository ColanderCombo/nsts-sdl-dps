#!/usr/bin/env python3
#
# The static (background) FCW section.
#
# Static FCWs are drawn once when the display is called up: fixed labels,
# lines, and legends built entirely from constants in the deck (the STAT
# section, up to VARY).  The section is emitted as annotated `Segment`s of
# `FCW` words; the dynamic (DDT) section is `ddt`'s job.
#
from . import fcw
from .fcw import Branch, FCW
from .model import Segment

# One row per stateless static-content directive: value -> (comment, words);
# `ay` is the latched AXIS=Y spacing state.  Argument rules live in
# `ops.LANGUAGE`; XC/YC (position runs), VDISP (the preamble slot), and AXIS
# (the latch itself) are stateful and live in `build_static`'s loop.
_CONTENT = {
    "CHAR":   lambda v, ay: ("-- CHAR = %s" % (v or ""),
                             fcw.chars(fcw.text_of(v))),
    "CARRTN": lambda v, ay: ("-- CARRTN -- CARRIAGE RETURN", [FCW.carrtn()]),
    "SPCHAR": lambda v, ay: ("-- SPCHAR = %s" % str(v).strip(),
                             [fcw.spchar_word(v)]),
    "VCORD":  lambda v, ay: ("-- VCORD = %s" % (v or ""),
                             fcw.vector(fcw.coords(v), absolute=False)),
    "VCORDA": lambda v, ay: ("-- VCORDA = %s" % (v or ""),
                             fcw.vector(fcw.coords(v), absolute=True)),
    "LINE":   lambda v, ay: ("-- LINE = %s" % str(v).strip(),
                             [fcw.line_word(v, ay)]),
    "SPACE":  lambda v, ay: ("-- SPACE = %s" % str(v).strip(),
                             [fcw.space_word(v, ay)]),
    "REPT":   lambda v, ay: ("-- REPT = %s" % (v or ""), fcw.rept_words(v)),
}


def build_static(ds, crtfmt=False):
    """Return the static section as a list of `Segment`s, or None for an
    external-background display (no inline static content).

    Layout: 2 DEULOC words + 5 setup FCWs + a VDISP slot + per-directive content
    + the 0x19EE 'branch back to the DEU' terminator.  Each XPOS/YPOS run is led
    by a 0x0000 NOOP; the first run reuses the VDISP slot when it is 0x0000.

    `crtfmt` builds a critical-format (CRTFMT= deck) body instead: no DEULOC
    slot, and the terminator is the 0x111E exit-to-dynamics branch + two
    compatibility zeros."""
    content = []              # one Segment per content directive
    vdisp = None              # VDISP value, placed in the preamble's slot below
    in_pos_run = False        # the last content was an XC/YC position word
    first_run = not crtfmt    # the first run's lead can be the VDISP slot —
                              # a CFT has no slot, so every run takes its lead
    axis_y = False            # current spacing direction (AXIS=Y is stateful)
    instat = False
    saw_vary = any(k == "VARY" for k, _ in ds)

    def emit(comment, words):
        content.append(Segment(
            "static", comment if isinstance(comment, list) else [comment],
            words))

    for k, v in ds:
        if k == "STAT":
            instat = True; continue
        if k == "VARY":
            break
        if not instat:
            continue
        if k in ("XC", "YC"):
            words = []
            if not in_pos_run:                 # new position run: lead NOOP,
                if not (first_run and vdisp is None):
                    words.append(FCW.noop())   # unless the VDISP slot leads
                first_run = False
            in_pos_run = True
            words.append(fcw.xpos(v) if k == "XC" else fcw.ypos(v))
            emit("-- %s = %s" % (k, str(v).strip()), words)
            continue
        if k == "VDISP":
            if crtfmt:                         # no slot in a CFT: inline FCW
                emit("-- VDISP = %s" % (str(v).strip()),
                     [FCW.value_display(fcw.num(v))])
                in_pos_run = False
                continue
            vdisp = fcw.num(v); continue       # fills the preamble slot; does
                                               # not interrupt a position run
        if k == "AXIS":
            axis = str(v).strip()
            axis_y = axis == "Y"
            emit("-- AXIS = %s -- Direction of Spacing is the %s axis"
                 % (axis, axis), fcw.axis_fcws(v))
        elif k in _CONTENT:
            emit(*_CONTENT[k](v, axis_y))
        else:
            # Inert: a directive with no static rendering (ITEM, KEYS, …) or
            # stray comment prose, which the deck grammar passes through as
            # statements — and a comma-split prose fragment does not even
            # keep its `*` lead (`*  AIDB SARB106, SDFSRDF12;` in CG0180).
            # The static section is therefore comment-tolerant: an unknown
            # key here is silently inert, as in the original DFG.
            continue
        in_pos_run = False

    if not content and vdisp is None:
        return None                            # external background
    if axis_y:                                  # static left in AXIS=Y -> reset to X
        emit("-- Reset spacing to the X axis",
             [FCW.minor_increment(-fcw.ROW_PITCH),
              FCW.major_increment(fcw.COL_PITCH), FCW.attr_mode()])
    if crtfmt:
        # The static->dynamic stitch: exit the critical-format area into the
        # dynamic portion of the display via the 0x11E table slot.
        emit("--  branch to location 286('11E'x) in the DEU", [Branch(0x11E)])
        emit("-- extra HWs of zero for compatibility with earlier DFG", [0, 0])
    else:
        # 0x19EE = branch to location 6638 in the DEU (return control after
        # drawing).
        emit("-- Branch to location %d in the DEU" % 0x19EE,
             ([] if saw_vary else [FCW.noop()])  # static-only NOOP before the branch
             + [FCW.deu_return()])

    # The fixed preamble: DEULOC pad, the 5 setup FCWs, the VDISP slot.
    # A CFT takes neither the DEULOC slot nor the VDISP slot.
    head = [] if crtfmt else \
        [Segment("static", ["-- NO DEULOC two additional halfwords"], [0, 0])]
    tail = [] if crtfmt else \
        [Segment("static", ["-- VDISP = %d" % vdisp],
                 [FCW.value_display(vdisp)]) if vdisp is not None
         else Segment("static", [], [FCW.noop()])]
    return head + [
        Segment("static",
                ["-- STAT  ** Set up FCWs at beginning of static section",
                 "-- Major Increment FCW"],
                [FCW.major_increment(fcw.COL_PITCH)]),
        Segment("static", ["-- Minor Increment FCW"],
                [FCW.minor_increment(-fcw.ROW_PITCH)]),
        Segment("static", ["--  FCW 1"], [FCW.attr_mode()]),
        Segment("static", ["--  FCW 2"], [FCW.char_mode()]),
        Segment("static", ["--  FCW 3"], [FCW.color_clear()]),
    ] + tail + content
