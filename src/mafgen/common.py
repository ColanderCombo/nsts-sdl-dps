"""Shared rendering vocabulary: letter-spaced banner text, branch-alias
and condition-code tables, and SDF statement-kind text."""


def w(s):
    """DASS 'wide' text: letter-space every character (spaces between words
    widen to three) -- w("MEMORY MAP") == "M E M O R Y   M A P"."""
    return " ".join(s)


#
# line geometry
#
# Pad widths for the dump-line columns, named for what sits at the pad
# rather than what the listing.py docstring calls the following field:
# COL_MARK is the patch-star slot, so the hex the docstring puts at 36
# starts one past it, and COL_NOTE carries rendered values and the
# ALIGNMENT GAP text, which the geometry comment does not enumerate.
COL_MARK = 35          # patch star ('*'/' '), hex halfwords follow
COL_EA = 48            # resolved effective address, 6 hex
COL_MNEM = 65          # mnemonic (7 wide incl. @/# suffixes) + operands
COL_NOTE = 89          # rendered value / ALIGNMENT GAP / continuation
COL_COMMENT = 95       # symbolic operand, branch alias, immediate value
LINE_WIDTH = 132       # comment joins wrap here and continue at COL_COMMENT


def addr_range(a, n):
    """The address column: 'start-end' over n halfwords, or a blank-padded
    single address.  Both forms occupy the same width."""
    return f"{a:06X}-{a + n - 1:06X}" if n > 1 else f"{a:06X}       "



# SDF statement type -> the kind text MAFGEN prints on ST# lines (subtype
# collapses match the DASS vocabulary: EXIT/REPEAT/GOTO etc.); the
# statement category's context bits prepend THEN/ELSE
_STMT_TYPES = {
    0: "NULL", 1: "EXIT/REPEAT/GOTO", 2: "CALL", 3: "READ/READALL/WRITE",
    4: "ASSIGN", 5: "IF", 6: "CLOSE", 7: "RETURN", 8: "END",
    9: "SCHEDULE", 10: "CANCEL/TERMINATE", 11: "WAIT",
    12: "UPDATE PRIORITY", 13: "SET/SIGNAL/RESET", 14: "SEND ERROR",
    15: "ON ERROR", 16: "FILE", 17: "DO", 18: "DO WHILE/DO UNTIL",
    19: "DO FOR", 20: "DO CASE", 22: "BLOCK", 31: "%NAMEBIAS",
    32: "%SVC", 33: "%NAMECOPY", 34: "%COPY", 35: "%SVCI", 36: "%NAMEADD",
}
_STMT_CONTEXT = {1: "ELSE ", 2: "THEN "}


def stmt_kind(category, stype):
    return (_STMT_CONTEXT.get(category & 7, "")
            + _STMT_TYPES.get(stype, f"TYPE{stype}"))



#
# branch alias comments
#
# The alias mnemonic depends on the condition mask and on what last set
# the condition code: BH/BL/BNE/BE/BNL/BLE after compares, BO/BNO
# after bit tests, BP/BM/BNZ/BZ/BNM/BNP otherwise.
# Stores, shifts, address loads and branches leave the CC untouched, so
# the context skips them.
_ALIAS_CMP = {0: "NOP",
              1: "BH", 2: "BL", 3: "BNE", 4: "BE", 5: "BNL", 6: "BLE",
              7: "B"}
_ALIAS_TEST = {0: "NOP",
               1: "BO", 2: "BM", 3: "BNZ", 4: "BZ", 5: "BNM", 6: "BNO",
               7: "B"}
_ALIAS_ARITH = {0: "NOP",
                1: "BP", 2: "BM", 3: "BNZ", 4: "BZ", 5: "BNM", 6: "BNP",
                7: "B"}
def alias_for(cc, mask):
    """The branch alias for ``mask`` under condition-code context ``cc``,
    or '' when the mask has none.  cc is 'CMP', 'TEST', 'UNDEF', or None
    for plain arithmetic."""
    table = (_ALIAS_CMP if cc == "CMP"
             else _ALIAS_TEST if cc == "TEST" else _ALIAS_ARITH)
    return table.get(mask) or ""


_CC_CMP = {"C", "CH", "CHI", "CR", "CIS", "CIST", "CE", "CER", "CED",
           "CEDR", "CBH"}
_CC_TEST = {"TB", "TRB", "TH", "TD"}
# CC-transparent: stores, shifts, address/stack arithmetic, branches/calls
_CC_KEEP = {"ST", "STH", "STB", "STE", "STED", "STDM", "STM", "STXA",
            "LM", "LDM", "SSM", "ISPB", "LPS",
            "STXAR", "SLL", "SRL", "SRA", "SLDL", "SRDL", "SRDA",
            "LA", "LHI", "IAL", "SVC", "SCAL", "BAL", "BALR", "BC", "BCF",
            "BCB", "BCT", "BCTB", "BCR", "BCTR", "BIX", "BVC", "BVCF",
            "PC", "NOP"}

# spelled-out (letter-spaced) category banners for non-SDF csects
_BANNER = {
    "CHECKSUM": w("CHECKSUM"),
    "NONHAL": w("NONHAL"),
    "LIBCODE": w("HAL LIBRARY CODE"),
    "LIBDATA": w("HAL LIBRARY DATA"),
    "QCON": w("HAL LIBRARY ZCON"),
    "PATCH": w("PATCH"),
    "MSC": w("MSC"),
    "BCE": w("BCE"),
}

# categories rendered from assembler sidecars rather than SDFs.  MSC/BCE
# (IOP program) csects are ordinary assembler members whose flavor comes
# from the control deck's AS TYPE clause, and LIBCODE covers the RUNASM
# runtime-library members (bare-named ones classified by their sidecar
# living in build/lib/runtime/RUN) -- all behave as NONHAL everywhere
# except the header banner.
_NONHAL_CATS = ("NONHAL", "MSC", "BCE", "LIBCODE")
_ASM_CATS = ("NONHAL", "MSC", "BCE", "LIBCODE", "PATCH")
