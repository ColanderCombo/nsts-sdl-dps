#!/usr/bin/env python3
#
# Fixed-format 80-column card geometry.
# 
# Input data generally comes in as IBM PDS members in a semi-standardized
# 80-column punch card derived format:
# 
#     cols 1-71   app-specific input
#     col    72   continuation character (in some variants)
#     cols 73-80  identification / sequence number — present on version
#                 tracked decks (blank between 72 and 73 not required.)
# 
# The DIALECTS stay in their own modules — field layout, comment forms, and
# continuation/stitching semantics differ per card language:
# 
#     asm101.statement/.cardreader  assembler cards (text cols 1-71, col-72
#                                   flag disambiguated by next-card lookahead)
#     ap101Utils.concard            CON80 linkage control cards (label 1-8)
#     ap101Utils.mmubuild           MMU load-build cards (label 1-8, stitched)
#     dfg.deck                      display decks (free-form comma directives)
#     dfg.compool                   compiler templates (`ebcdic_text` records)
# 
# This module holds only the geometry those dialects share.
# 
import re

LABEL = slice(0, 8)       # cols 1-8: label / location field
CODE = slice(0, 72)       # cols 1-72: everything before the ident field
IDENT = slice(72, 80)     # cols 73-80: identification / sequence number
CONT_COL = 71             # col 72: IBM continuation indicator

# A sequence tail left SHORT of col 73 by trailing-blank trimming: files that
# passed through editors lose the padding between code and the ident field,
# so the tail can sit anywhere after the last code token (whitespace-separated).
_TRIMMED_TAIL = re.compile(r"\s+\d{6}[0-9A-Z]{2}\s*$")


def code(line):
    return line[:72]


def code_text(line):
    return _TRIMMED_TAIL.sub("", line.rstrip("\r\n")[:72]).rstrip()


def continues(line):
    return len(line) > CONT_COL and line[CONT_COL] != " "


def ebcdic_text(raw):
    return "".join(raw[i + 1:i + 80].decode("ebcdicvagc")
                   for i in range(0, len(raw), 80))
