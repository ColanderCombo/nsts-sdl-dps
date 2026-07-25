#!/usr/bin/env python3
#
# PASS System Identifier and I-Load Identifier -- the two 4-halfword EBCDIC
# stamps in low core.
#
# Source: USA002869 "PASS User's Guide" OI32 (2006-10-26), sections 8.3.3
# SYSTEM IDENTIFIER and 8.3.4 I-LOAD IDENTIFIER.
#
#   System Identifier  hw 0x001C-0x001F   MM.NNP.JJA
#       MM  2 decimal   flight number (00-63; missions 1-50 modulo 50,
#                       51-63 reserved for non-mission releases)
#       NN  2 alnum     update number
#       P   1 alpha     PASS MMU area: A/B/C == area 1/2/3
#       JJ  2 decimal   load level (01 = RC1, 02 = FL)
#       A   1 alpha     UPF/paper-patch level, initialised to '0'
#
#   I-Load Identifier  hw 0x0020-0x0023   XX.Y.Z.AS.BB
#       XX  2 decimal   flight number (01-99)
#       Y   1 decimal   I-Load cycle number
#       Z   1 decimal   I-Load cycle revision number
#       A   1 decimal   I-Load Data Set (IDS) number
#       S   1 decimal   spare, initialised to '0'
#       BB  2 alnum     post-IDS change level
#
# The stored form is simply the format string with the '.' separators removed,
# encoded EBCDIC -- verified against all four worked examples in the guide:
#   43.02B.010   -> F4F3F0F2C2F0F1F0        43.2.0.00.00 -> F4F3F2F0F0F0F0F0
#   47.02A.01A   -> F4F7F0F2C1F0F1C1        47.2.0.00.0A -> F4F7F2F0F0F0F0C1
#
# ! SPEC vs DATA on the I-Load BB field: 8.3.4 says "BB = OO - ZZ ... two alpha
# characters", but its own STS-79 example is '0A' and all eight OI34 DASS
# configs read '0B'.  The data wins: BB is validated as alphanumeric.  Do not
# "fix" this to alpha-only.
#
from .codepages import *          # noqa: F401  (registers the ebcdicvagc codec)

# Halfword addresses and extents in GPC memory.
SYSTEM_ID_HW = 0x001C
ILOAD_ID_HW = 0x0020
IDENT_HW = 4                      # each identifier is 4 halfwords == 8 bytes
IDENT_BYTES = IDENT_HW * 2

_CODEC = "ebcdicvagc"


class IdentError(ValueError):
    """Malformed System / I-Load identifier."""


def _check(cond, msg):
    if not cond:
        raise IdentError(msg)


def _digits(s, field):
    _check(s.isdigit(), f"{field}: {s!r} must be decimal digits")
    return s


def _alnum(s, field):
    _check(s.isalnum() and s.isascii() and s.upper() == s,
           f"{field}: {s!r} must be upper-case alphanumeric")
    return s


def _alpha_or_zero(s, field):
    # The guide initialises these one-character levels to '0' and otherwise
    # allows a single alpha; accept either rather than rejecting real data.
    _check(len(s) == 1 and s.isascii() and (s.isdigit() or s.isupper()),
           f"{field}: {s!r} must be a digit or one upper-case letter")
    return s


# --------------------------------------------------------------- System ID

def parse_system_id(text):
    """'MM.NNP.JJA' -> the 8 stored bytes (EBCDIC)."""
    parts = text.strip().upper().split(".")
    _check(len(parts) == 3, f"system id {text!r}: expected MM.NNP.JJA")
    mm, nnp, jja = parts
    _check(len(mm) == 2 and len(nnp) == 3 and len(jja) == 3,
           f"system id {text!r}: expected MM.NNP.JJA field widths 2/3/3")
    _digits(mm, "MM (flight number)")
    _alnum(nnp[:2], "NN (update number)")
    _check(nnp[2] in "ABC", f"P (MMU area): {nnp[2]!r} must be A, B or C")
    _digits(jja[:2], "JJ (load level)")
    _alpha_or_zero(jja[2], "A (UPF/paper-patch level)")
    return (mm + nnp + jja).encode(_CODEC)


def format_system_id(raw):
    """The 8 stored bytes -> 'MM.NNP.JJA'."""
    _check(len(raw) == IDENT_BYTES,
           f"system id: expected {IDENT_BYTES} bytes, got {len(raw)}")
    t = bytes(raw).decode(_CODEC)
    return f"{t[0:2]}.{t[2:4]}{t[4]}.{t[5:7]}{t[7]}"


def system_id_fields(text):
    """'MM.NNP.JJA' -> dict of the spec's named fields (validated)."""
    parse_system_id(text)
    mm, nnp, jja = text.strip().upper().split(".")
    return {"flight": mm, "update": nnp[:2], "mmu_area_letter": nnp[2],
            "mmu_area": "ABC".index(nnp[2]) + 1,
            "load_level": jja[:2], "patch_level": jja[2]}


# ---------------------------------------------------------------- I-Load ID

def parse_iload_id(text):
    """'XX.Y.Z.AS.BB' -> the 8 stored bytes (EBCDIC)."""
    parts = text.strip().upper().split(".")
    _check(len(parts) == 5, f"i-load id {text!r}: expected XX.Y.Z.AS.BB")
    xx, y, z, as_, bb = parts
    _check(len(xx) == 2 and len(y) == 1 and len(z) == 1
           and len(as_) == 2 and len(bb) == 2,
           f"i-load id {text!r}: expected field widths 2/1/1/2/2")
    _digits(xx, "XX (flight number)")
    _digits(y, "Y (I-Load cycle number)")
    _digits(z, "Z (I-Load cycle revision)")
    _digits(as_[0], "A (IDS number)")
    _digits(as_[1], "S (spare)")
    _alnum(bb, "BB (post-IDS change level)")   # see the spec-vs-data note above
    return (xx + y + z + as_ + bb).encode(_CODEC)


def format_iload_id(raw):
    """The 8 stored bytes -> 'XX.Y.Z.AS.BB'."""
    _check(len(raw) == IDENT_BYTES,
           f"i-load id: expected {IDENT_BYTES} bytes, got {len(raw)}")
    t = bytes(raw).decode(_CODEC)
    return f"{t[0:2]}.{t[2]}.{t[3]}.{t[4]}{t[5]}.{t[6:8]}"


def iload_id_fields(text):
    """'XX.Y.Z.AS.BB' -> dict of the spec's named fields (validated)."""
    parse_iload_id(text)
    xx, y, z, as_, bb = text.strip().upper().split(".")
    return {"flight": xx, "cycle": y, "cycle_revision": z,
            "ids": as_[0], "spare": as_[1], "post_ids_level": bb}


def is_blank(raw):
    """True when the identifier is unwritten (a load module reads all zero)."""
    return not raw or set(bytes(raw)) <= {0x00}
