#!/usr/bin/env python3
#
#
# The asm101 "card reader": turns the raw 80-column card-image stream into the
# single, comment-free field strings the grammar (asm.lark, via larkparse.py) then
# parses.  This is the lexical layer the declarative grammar cannot express -- it
# is about PHYSICAL card geometry, not the operand language:
# 
#   * `lineContinues(line, nextLine)` -- IBM continuation detection: a non-blank
#     column 72 (the continuation flag) AND a look-ahead confirming the next card
#     is a genuine continuation card (cols 1-15 blank, the operand resuming at
#     col 16), which disambiguates a real continuation from a sequence number
#     that bled left into col 72.
#   * `joinOperand(...)` -- merge an operand field that spans continuation cards
#     into one string (cols 16-71 of each follow-on card), discarding comments.
#     Simple in almost all cases; the macro prototype/invocation cards are hard
#     and lean on the grammar itself (`_macroOperandEnd` runs the parser on the
#     partial card to decide whether to keep joining).
# 
# The grammar/AST layer lives in larkparse.py + asm.lark; once a field is joined,
# callers parse it with `larkparse.parse(text, rule)` directly.
#
#
# joinOperand is originally from ASM101S/fieldParser.py in 
# virtualagc by Ronald Burkey:
#   https://github.com/virtualagc/virtualagc/blob/master/ASM101S/fieldParser.py
# 
#
from . import larkparse

#=============================================================================
# Card-image continuation + operand-joining mechanics.

def lineContinues(line, nextLine):
  # Whether `line` continues onto `nextLine`.  IBM's flag is a non-blank
  # column 72, but these hand-keyed decks are inconsistently padded: a 79-column
  # line has its sequence number bleed left into col 72, mimicking a
  # continuation flag on a complete statement.  The discriminator is the NEXT
  # card: a genuine continuation is followed by a continuation card (cols 1-15
  # blank, operand resuming at col 16), whereas a bled sequence number is
  # followed by an ordinary statement (name/op in cols 1-15).  So require BOTH
  # a non-blank col 72 AND a continuation card following
  if len(line) < 72 or line[71] == ' ':
    return False
  return nextLine.strip() != "" and nextLine[:15].strip() == ""

# Forms the merged operand field, taking into account continuation lines.
# Comments are discarded.  The arguments are:
#    `lines`    A list of source-code lines.
#    `index`    The starting index in `lines` of the macro prototype line.
#    `column`   The column in `lines[index]` at which the operand field starts.
#               If the operand field doesn't start on the first card, then
#               `column` is 71.
#    `proto`    True for macro-prototype lines
#    `invoke`   False for macro-argument lines.
# Returns True,operand,skipCount, or False,'',skipCount when `index` is out
# of range.
# `skipCount` is the number of continuation lines processed.
def _macroOperandEnd(operand, invoke):
  """Decide where one card's macro prototype/invocation operand ends and
  whether the statement's operand is therefore complete -- returning
  (trimmed_operand, done).

  The classification hinges on whether the card's content parses cleanly as a
  replacement/parameter list, so the check runs the (anchored) Lark parser for
  the matching rule rather than a plain blank scan -- a bare scan wrongly
  treats a card like `&ST(&NI)` (whose `&` is not an identifier start, so the
  list parse stops at the `(`) as complete instead of a blob to keep joining.

    blank found, content parses cleanly:
      - content ends in ',' -> list broken before the blank: trim, keep joining
      - else                -> trim the comment; operand complete
    blank found, content does NOT parse cleanly -> blob: rstrip, keep joining
    blank found, content empty -> blank/leading-space card: empty operand,
      complete for an invocation, not for a prototype
    no blank, parses cleanly  -> whole card is operand, continues
    no blank, does not parse  -> blob: rstrip, continues
  """
  rule = "oinv" if invoke else "oproto"
  b = larkparse.first_blank_outside(operand)
  if b is None:
    if larkparse.parse(operand, rule) is not None:
      return operand, False
    return operand.rstrip(), False
  content = operand[:b]
  if content == "":
    return "", invoke
  if larkparse.parse(content, rule) is None:
    return operand.rstrip(), False
  if content[-1] == ",":
    return content, False
  return content, True


def joinOperand(lines, index, column, proto=False, invoke=False):
  continuation = False
  skipCount = -1
  status = True
  done = False
  operand = ""
  while continuation or skipCount < 0:
    if index >= len(lines):
      status = False
      break
    skipCount += 1
    line = lines[index]
    if done:
      pass
    elif continuation:
      operand = operand + line[15:71]
    else:
      operand = line[column:71]
    nextLine = lines[index + 1] if index + 1 < len(lines) else ""
    continuation = lineContinues(line, nextLine)
    index += 1
    if done or not (invoke or proto):
      continue
    operand, done = _macroOperandEnd(operand, invoke)
  return status,operand,skipCount
