#!/usr/bin/env python3
#
# Assembler for the IBM AP-101 computer.
#
# This file is based on ASM101S/ASM101S.py from virtualagc by Ronald Burkey:
#   https://github.com/virtualagc/virtualagc/blob/master/ASM101S/ASM101S.py
# 
# It's been significantly refactored for asm101, including using the typer
# cli framework.
#
import sys
import os
import gc
import json
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional

from .expressions import (
    svGlobals, svGlobalLocals, asmContext, SymbolTable, SymbolicVar, definedNormalSymbols, error, getErrorCount,
    evalBooleanExpression, svReplace, svDeclare, svSet,
    evalArithmeticExpression
)
from .cardreader import joinOperand
from .larkparse import Aif, Equ, parse
from .statement import Statement
from ap101Utils.objModule import Module
from . import model101tables
from .model101 import (
    generateObjectCode, sectionOffset,
    symtab, sects, entries, extrns, literalPools, _num
)

PROGRAM = "ASM101"
VERSION = "0.01"


class AssemblyError(Exception):
  def __init__(self, message: str, error_count: int = 0, max_severity: int = 0):
    super().__init__(message)
    self.error_count = error_count
    self.max_severity = max_severity


@dataclass
class MacroDef:
  """A captured macro definition (a value in 'Assemble.macros').

  The body itself stays in 'self.source'; this records the index span of the
  definition's cards so an invocation can re-read it.  'end', 'firstIndex', and
  'fromLibrary' are filled in when the matching MEND card is reached, so they
  are None on a definition that is still open."""
  start: int                        # self.source index of the MACRO card
  prototype: int                    # self.source index of the prototype card
  end: int | None = None            # self.source index of the MEND card
  firstIndex: int | None = None     # first_index_of_file at definition time
  fromLibrary: bool | None = None   # defined in a macro library / COPY member?



# Stage A — Front-end: source → self.source (assemble.py)
# -------------
# 1. Init. Macro libraries (self.libraries) are searched on demand, not
#    pre-loaded; read each primary file (_read_source_file).
# 2. Per card (_read_source_file, recursive): build a Statement (splits cols 
#    1–71/73–80, derives continues), append to self.source.
# 3. Field parse + continuation join (_parse_line → parse_fields → joinOperand): 
#    split name/operation, join continuation cards into one operand 
#    (proto/invoke/standard modes).
# 4. COPY expands inline via a recursive _read_source_file(..., copy=True).
# 5. Macros: MACRO…MEND captured into self.macros; on invocation, the body is 
#    re-read recursively with a fresh new_locals scope; on-demand library macros 
#    are loaded lazily and cached (_no_library_member). library_scan mode marks 
#     non-declaration lines skip.
# 6. Conditional assembly (operates here, pass _passCount = -1): 
#       GBL/LCL (svDeclare), 
#       SET (svSet), 
#       AIF/AGO/ACTR (branch budget guard), 
#       ANOP, MEXIT, MNOTE. 
#    Then svReplace substitutes &VARS into name/operation/operand.
# 7. Output: a flat self.source of fully-expanded, substituted Statements.
#
# Stage B — Codegen: generateObjectCode(source, macros) (model101.py)
# --------------
# 8. Pass 0 (≈2251): parse each operand once with the operation's grammar 
#    rule → stmt.ast (reused by all later passes); create preliminary symtab 
#    entries (forward refs, 4-byte placeholder positions).
# 9. Pass loop while passCount < 3 or repeatPass (2369), 
#       flags collect(1,2)/asis(2)/compile(3+):
#   - Pass 1 (collect): walk lines, size each instruction (SRS=2 vs RS=4 guess) 
#     into sects[].scratch; then optimizeScratch() resolves the SRS↔RS ambiguity 
#     by looking at stmt.ast displacements and cascades pos1 adjustments; 
#     re-derive sects[].used.
#   - Pass 2 (asis): replay with settled sizes, write sects[].memory, finalize 
#     symbol addresses, lay out CSECTs contiguously (sects[].offset).
#   - Pass 3+ (compile): actually encode bytes (generateRR/RS0/RS1/SRS, 
#     CPU.encode, IOP iop_instr.encode) into stmt.assembled; emit 
#     RLD/relocations (gated on passCount == 3). Snapshot the symtab; repeat 
#     if forward-branch condensation changed addresses (the "compile-pass 
#     fixpoint"), capped at maxPasses.
# 10. Append literal pools to their CSECTs.
#
# Stage C — Output (assemble.py)
# ---------------
# 11. writeObjectModule adapts sects/symtab/entries/extrns/relocations into 
#     ap101Utils.objModule.Module.from_assembly (ESD/TXT/RLD/END cards).
# 12. writeListing re-walks self.source: ESD page, source-line render 
#     (listing_prefix re-derives address+objcode hex per line, assigns 
#     printedLineNumber), literal-pool dumps, and the cross-reference page 
#     (LEN/VALUE/DEFN/REFERENCES).
#

class Assemble:
  """
  Assembler for IBM AP-101
    
  This class reads source files, expands macros, and generates
  object code in IBM object module format.
  """
    
  # All pseudo-ops ("assembler instructions"). See Appendix E of the 
  # "IBM System/360 Operating System Assembler Language" manual.
  # Gives the minimum and maximum number of comma-delimited operands 
  # in the operand field. -1 for maximum means "no limit".
  PSEUDO_OPS = {
      "ACTR": [1, 1],
      "AGO": [1, 1],
      "AIF": [1, 1],
      "ANOP": [0, 0],
      "CCW": [4, 4],
      "CNOP": [2, 2],
      "COM": [0, 0],
      "COPY": [1, 1],
      "CSECT": [0, 0],
      "CXD": [0, 0],
      "DC": [1, -1],
      "DROP": [1, 16],
      "DS": [1, -1],
      "DSECT": [0, 0],
      "DXD": [1, -1],
      "EJECT": [0, 0],
      "END": [0, 1],
      "ENTRY": [1, -1],
      "EQU": [1, 1],
      "EXTRN": [1, -1],
      "GBLA": [1, -1],
      "GBLB": [1, -1],
      "GBLC": [1, -1],
      "ICTL": [1, 3],
      "ISEQ": [2, 2],
      "LCLA": [1, -1],
      "LCLB": [1, -1],
      "LCLC": [1, -1],
      "LTORG": [0, 0],
      "MACRO": [0, 0],
      "MEND": [0, 0],
      "MEXIT": [0, 0],
      "MNOTE": [2, 2],
      "ORG": [0, 1],
      "PRINT": [1, 3],
      "PUNCH": [1, 1],
      "REPRO": [0, 0],
      "SETA": [1, 1],
      "SETB": [1, 1],
      "SETC": [1, 1],
      "SPACE": [0, 1],
      "SPOFF": [0, 0],
      "SPON": [0, 0],
      "START": [0, 1],
      "TITLE": [1, 1],
      "USING": [2, 17]
  }

  # Macro-language op families; the base sets live in model101tables so the
  # generator and codegen's 'ignore' set share one definition.  LANGUAGE is
  # the full set of ops that are part of the macro language itself (and so
  # are not listed by default); GENERATOR_OPS are dropped from the stream
  # the assembly stage reads (see '_codegen_source').
  MACRO_DECLARE_OPS = model101tables.MACRO_DECLARE_OPS
  MACRO_SET_OPS = model101tables.MACRO_SET_OPS
  GENERATOR_OPS = model101tables.GENERATOR_OPS
  MACRO_LANGUAGE_OPS = (MACRO_DECLARE_OPS | MACRO_SET_OPS
                        | frozenset({"AIF", "AGO", "ANOP", "SPACE", "MEXIT", "MNOTE"}))
  # Declare+set union, precomputed for the per-line library-scan test.
  MACRO_SV_OPS = MACRO_DECLARE_OPS | MACRO_SET_OPS

  def __init__(
      self,
      source_files: list[Path],
      object_file: Path,
      libraries: Optional[list[Path]] = None,
      sysparm: str = "PASS",
      tolerable_severity: int = 1,
      verbose: bool = False,
      debug_info: bool = True,
      march: str = "ap101s",
  ):
    self.source_files = source_files
    self.object_file = object_file
    self.debug_info = debug_info
    self.march = march
    self.libraries = libraries or []
    self._log_path_base = source_files[0].parent if source_files else Path.cwd()
    self.sysparm = sysparm
    self.tolerable_severity = tolerable_severity
    self.verbose = verbose
    self.current_date = datetime.today().strftime('%m/%d/%y')
        
    # Assembly state
    self.source = []  # All source lines with stmt
    self.macros = {}  # Macro definitions
    # Operation names confirmed to have no on-demand library member, so we
    # don't re-stat the library for every machine op / pseudo-op occurrence:
    self._no_library_member = set()
    # Per-macro (body text, line correspondence, sequence prescan); see
    # _read_source_file.
    self._macro_read_cache = {}
    self.sequence_global_locals = {}  # Sequence symbols for global-local scope
    self.metadata = {}  # Assembly metadata (TITLE, etc.)
    self.sysndx = -1  # Macro invocation counter
    # Max AIF/AGO branches taken per file/macro expansion before we declare
    # a runaway conditional-assembly loop (IBM ACTR default).
    self.ACTR_LIMIT = 4096

    asmContext.reset()
    svGlobals["&SYSPARM"] = SymbolicVar(self.sysparm)
    
  def _log(self, message: str) -> None:
    if self.verbose:
      print(message, file=sys.stderr)

  def _relpath(self, path, base=None) -> str:
    """Path relative to 'base' (default: the primary source's directory),
    absolute if relativizing fails or is longer."""
    absolute = str(path)
    try:
      relative = os.path.relpath(absolute, base or str(self._log_path_base))
    except ValueError:
      return absolute
    return relative if len(relative) <= len(absolute) else absolute

  def _macro_body_map(self, name):
    """self.source indices of macro 'name's body cards, in the order the
    expansion reader sees them: a generated statement with lineNumber k came
    from body card _macro_body_map(name)[k-1].  Mirrors the body
    reconstruction in _read_source_file (the MACRO card, the prototype and
    its continuations, and the MEND card are not part of the body stream)."""
    cache = getattr(self, "_body_map_cache", None)
    if cache is None:
      cache = self._body_map_cache = {}
    if name in cache:
      return cache[name]
    mw = self.macros.get(name)
    idxs = []
    if mw is not None and mw.end is not None:
      continue_prototype = False
      for i in range(mw.start, mw.end + 1):
        if i == mw.start or i == mw.end:
          continue
        if i == mw.prototype:
          continue_prototype = self.source[i].continues
          continue
        if continue_prototype:
          if not self.source[i].continues:
            continue_prototype = False
          continue
        idxs.append(i)
    cache[name] = idxs
    return idxs

  def _diag_path(self, path) -> str:
    """Path as shown in a diagnostic: relative to the working directory (so
    'file:line' is clickable where the build ran), absolute if shorter."""
    return self._relpath(path, base=os.curdir)

  def _diag_origin(self, stmt):
    """(display-path, line-number) a diagnostic on 'stmt' should point at.
    A card read from a file reports that file:line directly; a
    macro-generated card reports the macro-definition card it was generated
    from, resolved through nested definitions until a real file is found."""
    s = stmt
    for _ in range(16):  # nested-definition depth guard
      if s.file is not None:
        return self._diag_path(s.file), s.lineNumber
      body = self._macro_body_map(s.macro) if s.macro else []
      k = s.lineNumber - 1
      if 0 <= k < len(body):
        s = self.source[body[k]]
      else:
        break
    return f"<macro {s.macro}>", s.lineNumber

  def _invocation_chain(self, stmt):
    """The macro-invocation statements that produced generated card 'stmt',
    innermost first, ending at depth 0 in a primary source file.  The
    invoker of a depth-d expansion is the nearest preceding card at depth
    d-1 whose operation is the macro's name -- matching on the name skips
    the cards an on-demand library-member load appended between the
    invocation card and the expansion (and the member's own prototype card,
    which repeats the name but sits inside a MACRO definition)."""
    chain = []
    cur = stmt
    idx = stmt.n
    while cur.depth > 0 and idx > 0:
      found = None
      while idx > 0:
        idx -= 1
        s = self.source[idx]
        if (s.depth == cur.depth - 1 and not s.inMacroDefinition
                and s.operation == cur.macro):
          found = s
          break
      if found is None:
        break
      chain.append(found)
      cur = found
    return chain

  def _codegen_source(self) -> list:
    """The generated-statement stream the assembly stage consumes: 'self.source'
    with the conditional-assembly directives the generator already performed
    ('GENERATOR_OPS') removed -- the asm101 version of SYSUT2.

    Order is preserved, so every surviving statement keeps its original
    'stmt.n'; codegen uses 'n' only as the ESD-ordering key and the
    cross-reference line id (never to index the list), so the gaps left by the
    dropped lines are harmless.  'self.source' itself is untouched -- the
    intolerable-error scan and the listing still see the complete record.

    'inMacroDefinition' template lines are deliberately KEPT (codegen skips them
    anyway): a continued macro INVOCATION's continuation card is displaced to
    after its expansion, and codegen relies on the interleaved on-demand
    macro-definition lines to absorb the dangling continuation flag before the
    expanded body -- dropping them would swallow the first generated statement.
    A dropped directive that is itself continued takes its continuation cards
    with it, so no orphaned continuation card is ever left behind."""
    ops = self.GENERATOR_OPS
    out = []
    dropping_continuation = False
    for s in self.source:
      if dropping_continuation:
        # A continuation card of a directive we just dropped -- drop it too,
        # following the chain until the statement's last card.
        dropping_continuation = s.continues
        continue
      if s.operation in ops:
        dropping_continuation = s.continues
        continue
      out.append(s)
    return out
    
  def _parse_line(self, lines: list, line_number: int,
                  in_macro_definition: bool, in_macro_proto: bool,
                  library_scan: bool = False) -> int:
    skipped = 0
    stmt = self.source[-1]
    stmt.operand = None
        
    if stmt.is_blank_or_comment:
      return 0

    # Split the card into name/operation (line-local; Statement owns it) and
    # get the column where the operand field begins.
    j = stmt.parse_fields()
    operation = stmt.operation

    if operation == "MACRO":
      if in_macro_definition:
        error(stmt, "Nested MACRO definitions")
      return 0
    if operation == "MEND":
      if not in_macro_definition:
        error(stmt, "MEND without preceding MACRO")
      return 0
    if in_macro_definition and not in_macro_proto:
      return 0

    if (not library_scan and not in_macro_proto and operation != "" and
            operation not in self.macros and
            operation[:1] not in (".", "&", "=", "*")):
      self._load_library_macro(operation)

    operand = ""
    if in_macro_proto:
      in_macro_proto = False
      mode, what = {"proto": True}, "macro-prototype cards"
    elif operation in self.macros:
      mode, what = {"invoke": True}, "macro-invocation operands"
    elif operation in self.PSEUDO_OPS and self.PSEUDO_OPS[operation][1] == 0:
      mode = None     # no operand to extract
    else:
      mode, what = {}, "macro-invocation operands"
    if mode is not None:
      success, field, skipped = joinOperand(lines, line_number, j, **mode)
      if success:
        operand = field
      else:
        error(stmt, f"Cannot parse {what}")
    stmt.operand = operand
        
    return skipped
    
  def _charge_branch(self, branch_budget, macroname, filename, line_number):
    """Spend one AIF/AGO branch from the per-expansion budget; raise on a
    runaway conditional-assembly loop (an AIF condition that never settles)."""
    branch_budget -= 1
    if branch_budget <= 0:
      where = macroname if macroname else filename
      raise AssemblyError(
          f"Conditional-assembly loop in {where} (near line "
          f"{line_number + 1}): more than {self.ACTR_LIMIT} AIF/AGO "
          f"branches taken. Check for an AIF condition that never "
          f"becomes true.")
    return branch_budget

  def _branch_to(self, target, sequence, from_where, stmt, line_number):
    """Resolve an AIF/AGO branch to sequence symbol 'target', returning the
    new (line_number, skip_to_seq).  A known target in this expansion jumps
    the line cursor; an as-yet-unseen one is searched for forward via
    skip_to_seq; a target in another macro is an error (no move)."""
    if target in sequence:
      if from_where != sequence[target][0]:
        error(stmt, "Target out of this macro")
        return line_number, None
      return sequence[target][1] - 1, None
    return line_number, target

  def _read_source_file(self, from_where: str, sv_locals: dict, sequence: dict,
                        copy: bool = False, printable: bool = True,
                        depth: int = 0, library_scan: bool = False) -> None:
    first_index_of_file = len(self.source)
    in_macro_proto = False
    in_macro_definition = False
    line_correspondence = []
    # ACTR-style conditional-assembly loop counter.  Every AGO and every
    # taken AIF branch decrements the budget; if it is exhausted we have
    # almost certainly hit a runaway AIF/AGO loop, so fail with a diagnostic
    # rather than hanging silently.  (IBM's ACTR default is 4096.)
    branch_budget = self.ACTR_LIMIT
        
    if from_where in self.macros:
      # Load the macro definition.  Body text, line correspondence, and the
      # sequence prescan depend only on the MacroDef, so they are built once
      # and shared across invocations.  'sequence' gets a copy: the lazy
      # record in the execution loop mutates it.
      filename = None
      macroname = from_where
      macro_where = self.macros[macroname]
      cached = self._macro_read_cache.get(macroname)
      if cached is None:
        # _macro_body_map drops the MACRO card, the prototype and its
        # continuations, and the MEND card.
        idxs = self._macro_body_map(macroname)
        body_source = [
            (self.source[i].text + ("X" if self.source[i].continues else " "))
            .ljust(80)
            for i in idxs
        ]
        body_correspondence = [i - macro_where.firstIndex for i in idxs]
        prescan = {}
        for _seqLn, _seqLine in enumerate(body_source):
          if _seqLine[:1] == "." and _seqLine[:2] != ".*":
            _seqToks = _seqLine.split()
            if _seqToks and _seqToks[0] not in prescan:
              prescan[_seqToks[0]] = (from_where, _seqLn)
        cached = (body_source, body_correspondence, prescan)
        self._macro_read_cache[macroname] = cached
      this_source, line_correspondence, _prescan = cached
      sequence = dict(_prescan)
    else:
      try:
        with open(from_where, "rt") as f:
          this_source = [ln.rstrip("\r\n") for ln in f]
        for i in range(len(this_source)):
          line_correspondence.append(i)
        filename = from_where
        macroname = None
      except FileNotFoundError:
        raise AssemblyError(f"Source file '{from_where}' does not exist")
        
    skip_count = 0
    line_number = -1
    skip_to_seq = None
    just_branched = False
    # 'this_source' is fixed for this read (the macro body / file lines were
    # assembled above and are only read below), so its length is loop-invariant.
    nLines = len(this_source)

    # Pre-scan a file for sequence symbols so both forward and backward
    # AGO/AIF targets resolve; the lazy scan below records a label only when
    # its line is executed, which cannot resolve a backward jump to a label
    # a forward jump skipped over.  A sequence symbol occupies the name
    # field (a leading ".", but not the ".*" comment); first occurrence
    # wins.  A file read scans into the caller's dict; a macro read uses its
    # cached prescan from above.
    if macroname is None:
      for _seqLn in range(len(this_source)):
        _seqLine = this_source[_seqLn]
        if _seqLine[:1] == "." and _seqLine[:2] != ".*":
          _seqToks = _seqLine.split()
          if _seqToks and _seqToks[0] not in sequence:
            sequence[_seqToks[0]] = (from_where, _seqLn)

    while line_number + 1 < nLines:
      line_number += 1
      line = this_source[line_number]
            
      # When skipping to a sequence symbol (forward AGO/AIF target), check
      # if this line defines it.  The symbol must be at the start of the
      # line, with the next character a space (variable field padding) or
      # the line ending there -- not exactly one space, since field gaps
      # vary (e.g. ".DUPLOOP AIF" vs ".DUP     ANOP").
      if skip_to_seq is not None:
        seq_len = len(skip_to_seq)
        if not (line.startswith(skip_to_seq) and 
                (len(line) == seq_len or line[seq_len] == ' ')):
          continue
      skip_to_seq = None
            
      # Continuation detection.  IBM's rule is "col 72 (index 71)
      # non-blank", but these hand-keyed decks are inconsistently
      # formatted: some lines are only 79 columns wide, so an 8-char
      # sequence number bleeds left into col 72 (e.g. "...000200AA") and
      # looks like a continuation flag on a complete statement.  A width
      # test alone is also wrong, since a genuine continuation may be a
      # 79-col line with the same bleed.
      #
      # The reliable discriminator is the NEXT card: a real continuation is
      # followed by a continuation card (cols 1-15 blank, operand resuming
      # at col 16), whereas a bled sequence number is followed by an
      # ordinary statement (name/op in cols 1-15).  So require both a
      # non-blank col 72 AND a continuation card following.
      cardImage = line
      _nxt = (this_source[line_number + 1]
              if line_number + 1 < nLines else "")
      line = cardImage[:80]
      stmt = Statement(
          line=cardImage,
          nextline=_nxt,
          file=filename,
          macro=macroname,
          lineNumber=line_number + 1,
          inMacroDefinition=in_macro_definition,
          copy=copy,
          printable=printable,
          depth=depth,
          n=len(self.source),
      )
      self.source.append(stmt)
            
      if skip_count > 0:
        skip_count -= 1
        stmt.skip = True
        continue
      if stmt.is_blank_or_comment:
        continue
      # Skip continuation cards: a line is consumed by its predecessor's
      # operand parse, so suppress it here.  The first_index_of_file guard
      # stops this reaching across the read boundary: when a nested
      # file/macro read begins from inside '_parse_line' of a CONTINUED
      # invocation line, self.source[-2] is that outer continuation card,
      # not a line of THIS read -- skipping it would consume the new
      # member's MACRO header.  The just_branched guard: after a taken
      # AIF/AGO branch self.source[-2] is the CONTINUED branching statement
      # itself (its continuation cards were never appended -- the cursor
      # jumped), and the first card at the target is a labeled statement,
      # never a continuation -- without the guard the branch-target card
      # is silently eaten.
      if (not just_branched and
              len(self.source) - 1 > first_index_of_file and
              self.source[-2].continues):
        continue
      just_branched = False
            
      skip_count = self._parse_line(this_source, line_number,
                                   in_macro_definition, in_macro_proto,
                                   library_scan=library_scan)
      name = stmt.name
      if name[:1] == ".":
        sequence[name] = (from_where, line_number)
      operation = stmt.operation
      operand = stmt.operand
            
      if operation == "MACRO":
        in_macro_proto = True
        in_macro_definition = True
        macro_start = len(self.source) - 1
        stmt.inMacroDefinition = True
      elif in_macro_proto:
        in_macro_proto = False
        macro_name = operation
        self.macros[macro_name] = MacroDef(
            start=macro_start, prototype=len(self.source) - 1)
      elif operation == "MEND":
        macro_def = self.macros[macro_name]
        macro_def.end = len(self.source) - 1
        macro_def.firstIndex = first_index_of_file
        # fromLibrary: True if from a macro library or COPY'd member rather
        # than the primary source -- the listing echoes a library/COPY macro's
        # invocation line but not a source macro's.
        macro_def.fromLibrary = bool(library_scan or copy)
        in_macro_definition = False
        continue
      elif operation == "COPY":
        found = False
        if line[0] == " ":
          fname = line.split()[1]
        else:
          fname = line.split()[2]
        for library in self.libraries:
          for candidate in (fname + ".asm", fname):
            fcopy = os.path.join(library, candidate)
            if os.path.exists(fcopy) and os.path.isfile(fcopy):
              found = True
              self._log(f"  COPY {fname} from {self._relpath(library)}")
              self._read_source_file(fcopy, sv_locals, sequence,
                                    copy=True, printable=printable,
                                    depth=depth,
                                    library_scan=library_scan)
              break
          if found:
            break
        if not found:
          raise AssemblyError(f"File {fname} for COPY not found")
        continue
            
      if in_macro_definition:
        continue

      # Macro-library scan mode: a library member contributes only its
      # macro definitions and COPY (handled above) and global
      # SET/declaration statements (which emit no code).  Everything else
      # at the top level is ignored -- marked so codegen skips it, and
      # short-circuited here so it is not expanded now.
      if library_scan and operation not in self.MACRO_SV_OPS:
        stmt.skip = True
        continue

      # Handle macro-language related pseudo-ops
      if operation == "MEXIT":
        break
      if operation in self.MACRO_DECLARE_OPS:
        svDeclare(operation, operand, sv_locals, stmt)
        continue
      if operation in self.MACRO_SET_OPS:
        svSet(operation, name, operand, sv_locals, stmt)
        continue
      if operation == "AGO":
        branch_budget = self._charge_branch(
            branch_budget, macroname, filename, line_number)
        # The AGO operand is a sequence symbol optionally followed by a
        # comment ("AGO .GEN   LOOP BACK").  A sequence symbol never
        # contains a space, so take only the first whitespace-delimited
        # token; otherwise the trailing comment is glued onto the target
        # and never matches a recorded sequence symbol.  Comment-strip
        # like SET/AIF.
        ago_op = operand.lstrip()
        if ago_op.startswith("("):
          # Computed AGO: 'AGO (expr).seq1,.seq2,...,.seqN' branches to
          # the expr-th sequence symbol (1-based); out of range falls
          # through.  Used e.g. by CHAR to dispatch on a character's
          # length ('AGO (&N).ONELIST,.TWOLIST,...').
          paren_depth = 0
          close = -1
          for k, ch in enumerate(ago_op):
            if ch == "(":
              paren_depth += 1
            elif ch == ")":
              paren_depth -= 1
              if paren_depth == 0:
                close = k
                break
          target = ""
          if close < 0:
            error(stmt, "Unbalanced ( in computed AGO")
          else:
            ast = parse(ago_op[1:close], "arith")
            idx = None
            if ast is not None:
              idx = evalArithmeticExpression(ast, sv_locals,
                                             stmt)
            if idx is None:
              error(stmt, "Cannot evaluate computed-AGO index")
            else:
              targets = [t.split()[0]
                         for t in ago_op[close + 1:].split(",")
                         if t.strip()]
              if 1 <= idx <= len(targets):
                target = targets[idx - 1]
                  # else: out of range -> fall through (target "")
          if target == "":
            continue
        else:
          fields = operand.split()
          target = fields[0] if fields else ""
        _pre_branch_ln = line_number
        line_number, skip_to_seq = self._branch_to(
            target, sequence, from_where, stmt, line_number)
        if skip_to_seq is not None or line_number != _pre_branch_ln:
          # A taken branch invalidates the pending continuation-card skip:
          # skip_count belongs to THIS (continued) statement's continuation
          # cards, which the cursor move leaves behind.  Letting it
          # linger silently eats the branch-TARGET card, so a macro line
          # reached from a multi-card AIF never runs.
          skip_count = 0
          just_branched = True
        continue
      if operation == "AIF":
        operand = operand.rstrip()
        ast = parse(operand, "aif")
        if isinstance(ast, Aif):
          expression, target = ast.cond, ast.target
        else:
          expression = target = None
        if expression is not None:
          pass_fail = evalBooleanExpression(expression, sv_locals, stmt)
          if pass_fail is None:
            error(stmt, f"Cannot evaluate {expression}")
            continue
          if not pass_fail:
            continue
          branch_budget = self._charge_branch(
              branch_budget, macroname, filename, line_number)
          # Conditional test passed - go to sequence symbol
          _pre_branch_ln = line_number
          line_number, skip_to_seq = self._branch_to(
              target, sequence, from_where, stmt, line_number)
          if skip_to_seq is not None or line_number != _pre_branch_ln:
            # See the AGO site: a taken branch invalidates the pending
            # continuation-card skip or it eats the branch-target card.
            skip_count = 0
            just_branched = True
          continue
        else:
          error(stmt, f"Unrecognized AIF operand: {operand}")
        continue
      if operation == "ANOP":
        continue
      if operation == "ACTR":
        # ACTR n sets the conditional-assembly loop counter (the budget
        # of AIF/AGO branches before a runaway loop is declared) for this
        # expansion; e.g. MLIB80's ENDCASE raises it (ACTR 30000).
        # Consume it -- it generates no object code and must not reach
        # codegen (where it would be "Unrecognized line").  Take the
        # first token (operand carries field padding / a comment) so
        # arith_only's end-anchor doesn't reject it.
        tok = svReplace(stmt, operand, sv_locals).split()
        expr = parse(tok[0], "arith_only") if tok else None
        n = evalArithmeticExpression(expr, sv_locals, stmt) \
            if expr is not None else None
        if n is not None and n > 0:
          branch_budget = n
        continue
      if operation == "MNOTE":
        ast = parse(operand, "mnote")
        if ast is None:
          error(stmt, f"Cannot parse MNOTE: {operand}")
        else:
          msg = svReplace(stmt, ast.msg, sv_locals)
          if ast.comment:
            pass
          elif ast.severity is not None:
            error(stmt, msg, severity=ast.severity)
          else:
            error(stmt, msg, severity=1)
          stmt.fullComment = True
          stmt.text = msg
          stmt.name = ""
          stmt.operation = ""
          stmt.operand = ""
          stmt.mnote = True
        continue
            
      # Symbolic-variable replacement.  Test the PARSED fields, not the raw
      # card image: `line` is only the FIRST card of a continued statement,
      # so a variable appearing only on a continuation card (FCMBMTMC's
      # `BMTENT ...,`/`  0C,&AERRLBL`) would otherwise skip substitution and
      # pass the raw `&VAR` text through as a macro argument.
      if "&" in name or "&" in operation or "&" in operand:
        name = svReplace(stmt, name, sv_locals)
        # The real assembler RE-SCANS the substituted card, so a name field
        # ends at the first blank.  A SETC value with deliberate trailing
        # blanks (PCGEN: &SYM SETC '&SYSLIST(3,1)'.'  ') must define the
        # CLEAN symbol -- keying symtab on the padded text made every such
        # ENTRY silently drop its LD from the ESD (FIOADCNS/FIOADCCL PC/CM/
        # LS entries, 88 hw unresolved in the composed SSW).
        name = name.split(" ", 1)[0]
        raw_operation = operation
        operation = svReplace(stmt, operation, sv_locals)
        operand = svReplace(stmt, operand, sv_locals)
        stmt.name = name
        stmt.operation = operation
        stmt.operand = operand
        if (name.strip() == "" and operation.strip() == "" and operand.strip() == ""):
          stmt.empty = True
        # When the operation field is itself a symbolic variable (e.g. GENERATE
        # emits '&COPY(&J)', which substitutes to a DSECT-mapping macro name),
        # the pre-substitution on-demand library load in _parse_line skipped it
        # (the raw field started with '&').  Now that the real name is known,
        # attempt the load so the invocation below resolves.
        if ("&" in raw_operation and operation != ""
                and operation not in self.macros
                and operation not in self.PSEUDO_OPS
                and operation[:1] not in (".", "&", "=", "*")):
          self._load_library_macro(operation)

      # Capture the length attribute of an EQU-defined symbol so that
      # L'symbol resolves DURING macro expansion (Pass -1), before the
      # symbol table is built.  Position-defined symbols use the 3-operand
      # EQU form (PDEF emits "&N.X EQU &X,&X+1025,C'@'"); the POS macro
      # later reads L'&N.X in AIF branch decisions, so the value cannot be
      # deferred.  The length operand is a macro-time constant (already
      # pure arithmetic by now), so it evaluates with empty symbolic-
      # variable and symbol-table context; define-before-use holds for
      # these symbols in practice.
      if (operation == "EQU" and name != "" and name[:1] not in [".", "&"]):
        eqast = parse(operand, "equ")
        if isinstance(eqast, Equ) and eqast.length is not None:
          lattr = evalArithmeticExpression(eqast.length, {}, stmt, {}, severity=0)
          if lattr is not None:
            asmContext.symbolAttributes.setdefault(name, {})["length"] = lattr
        # The 3rd EQU operand is a character self-defining term whose value
        # names the symbol's T' type attribute (PDEF emits 'EQU 1,1,C'#'' to
        # type a position symbol '#'); capture it so T'symbol resolves during
        # macro expansion, like the length attribute above.
        if isinstance(eqast, Equ) and eqast.tattr is not None:
          tval = evalArithmeticExpression(eqast.tattr, {}, stmt, {}, severity=0)
          if tval is not None and 0 <= tval < 256:
            asmContext.symbolAttributes.setdefault(name, {})["type"] = \
                bytes((tval,)).decode("ebcdicvagc")

      def _define_symbol(symbol):
        definedNormalSymbols[symbol] = {
            "label": True,
            "fromWhere": from_where,
            "lineNumber": line_number,
            "fromLine": line_correspondence[line_number]
        }

      if (name != "" and name[:1] not in [".", "&"]
          and operation not in ["TITLE", "CSECT", "DSECT"]
          and operation not in self.macros):
        if name not in definedNormalSymbols:
          _define_symbol(name)
        else:
          error(stmt, f"Already defined: {name}")
      elif operation == "EXTRN":
        # The operand carries a trailing comment (e.g. the PROGRAM macro emits
        # 'EXTRN FC$BCEMD   GENERATE EXTRN FOR SAVE AREA NAME'); take only the
        # first blank-delimited token so the comment is not glued onto the last
        # symbol -- otherwise 'D'sym' (RETURN's save-area test) never matches.
        tokens = operand.split()
        symbols = tokens[0].split(",") if tokens else []
        for symbol in symbols:
          symbol = symbol.strip()
          if symbol and symbol not in definedNormalSymbols:
            _define_symbol(symbol)
            
      if operation in self.macros:
        self.sysndx += 1
        macrostats = self.macros[operation]
        # Get formal parameters from macro prototype
        formals = self.source[macrostats.prototype].operand
        pformals = parse(formals, "oproto")
        if operand.strip() == "":
          poperands = []
        else:
          poperands = parse(operand, "oinv")
        if isinstance(poperands, dict) and "pi" in poperands:
          poperands = poperands["pi"]
        else:
          poperands = []
        if isinstance(pformals, dict) and "pi" in pformals:
          pformals = list(pformals["pi"])
        else:
          pformals = []

        # Build dictionary of formal parameter replacements
        new_locals = SymbolTable({
            "parent": [from_where, line_number,
                      line_correspondence[line_number], sv_locals]
        }, parent=svGlobals)
        fname = self.source[macrostats.prototype].name
        if fname != "":
          new_locals[fname] = SymbolicVar(name)
                
        # Fill in default values
        syslist0 = name
        syslist = []
        for i in range(len(pformals) - 1, -1, -1):
          pformal = pformals[i]
          if isinstance(pformal, str):
            new_locals[pformal] = SymbolicVar('', omitted=True)
          elif isinstance(pformal, (list, tuple)):
            if (len(pformal) != 3 or pformal[1] != "=" or
                    pformal[0][:1] != "&" or
                    not isinstance(pformal[2], str)):
              error(stmt, f"Unrecognized format for formal parameter {pformal}")
              continue
            new_locals[pformal[0]] = SymbolicVar(pformal[2], omitted=True)
            del pformals[i]
          else:
            error(stmt, f"Implementation error in formal parameter {pformal}")
            continue
                
        # Apply actual parameter replacements
        i = 0
        for suboperand in poperands:
          key, value = suboperand.macroArg()
          if key is None:
            syslist.append(value)
            if i >= len(pformals):
              continue
            new_locals[pformals[i]] = SymbolicVar(value, omitted=False)
            i += 1
          else:
            new_locals[key] = SymbolicVar(value, omitted=False)

        # omitted=False marks &SYSLIST as parameter-valued: a null element
        # (out-of-range subscript, or an explicitly-empty positional operand)
        # used arithmetically is 0, not an eval error (Assembler H GC26-3758-3
        # p.19: "A null value is treated as zero"; HLASM SC26-4940 Table 58
        # extends the rule to &SYSLIST(n) / &SYSLIST(n,m)).
        new_locals["&SYSLIST"] = SymbolicVar(syslist, omitted=False)
        new_locals["&SYSLIST0"] = SymbolicVar(syslist0)
        new_locals["&SYSNDX"] = SymbolicVar(self.sysndx)

        self._read_source_file(operation, new_locals, sequence,
                               copy=copy, printable=printable,
                               depth=depth + 1)
      continue
    
  def _load_library_macro(self, name: str) -> bool:
    """
    Fetch a library macro on demand, by name (IBM SYSLIB FIND model).

    Mirrors AS037F1: a referenced-but-undefined operation is
    looked up in the library as the member of that exact name (member name =
    macro name).  The whole member is read (so any helper macros it defines
    come along), with 'library_scan=True' so only its macro definitions and
    global SET/declarations are taken -- never its open code.  A miss is
    cached so subsequent occurrences do not re-stat the library.

    Returns True if 'name' is defined as a macro afterward.
    """
    if name in self.macros:
      return True
    if name in self._no_library_member:
      return False
    for library in self.libraries:
      for candidate in (name, name + ".asm"):
        path = os.path.join(library, candidate)
        if os.path.isfile(path):
          self._log(f"  on-demand macro {name} from {self._relpath(library)}")
          # Give the library scan its OWN throwaway sequence namespace rather
          # than the shared 'self.sequence_global_locals' that the primary
          # source uses.  In library_scan mode the member's open-code AIF/AGO
          # are never executed (gated out before the branch handlers), so its
          # file-level sequence symbols are only ever written, never read --
          # recording them in the shared dict pollutes the primary source's
          # namespace.  (Concretely: GENERATE defines a '.END' sequence symbol;
          # leaking it made FPMIHPC2's open-code 'AGO .END' resolve to the
          # "wrong file" and silently no-op, so its end-of-routine deferred
          # block re-ran forever until the ACTR budget tripped.)
          self._read_source_file(path, svGlobalLocals, {},
                                copy=False, printable=False, depth=0,
                                library_scan=True)
          if name in self.macros:
            return True
    self._no_library_member.add(name)
    return False

  def assemble(self) -> None:
    # Disable the cyclic garbage collector for the assembly. 
    # This speeds up processing for very large macro-expanded files:
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
      self._assemble()
    finally:
      if gc_was_enabled:
        gc.enable()

  def _assemble(self) -> None:
    self._log(f"Assembling {len(self.source_files)} source file(s)...")

    #
    # Read source files
    #
    source_file_names = []
    for source_file in self.source_files:
      source_file_names.append(source_file.stem)
      self._log(f"Reading source file {self._relpath(source_file)}")
      self._read_source_file(str(source_file), svGlobalLocals,
                            self.sequence_global_locals,
                            copy=False, printable=True, depth=0)

    # Split macro GENERATION from ASSEMBLY, mirroring the IBM F-assembler's
    # SYSUT2 (see AS037F1 IEUF3): the front-end above has already "performed"
    # all conditional assembly -- evaluating AIF/AGO/SET* and materialising each
    # re-executed loop iteration into self.source.  The assembly stage needs
    # only the GENERATED statements, so hand codegen a stream with the
    # conditional-assembly directives removed.  self.source keeps the FULL
    # record (the intolerable-error scan below and writeListing read it).
    codegen_source = self._codegen_source()
    self._log(f"Generating object code ({len(codegen_source)} of {len(self.source)} lines)...")
    self.metadata = generateObjectCode(codegen_source, self.macros, log=self._log,
                                       march=self.march)
        
    # Check for errors.  Intolerance is judged per source LINE, not per
    # occurrence.  Codegen passes (pass >= 1) RE-evaluate and RE-log every
    # statement, so a forward reference unresolved early but resolved once
    # the symbol table is complete logs a transient error in an early pass
    # and none in the final pass -- it assembled correctly, so it must not
    # count.  Pre-passes (pass <= 0: macro expansion at -1, preliminary scan
    # at 0) run ONCE, so their errors never get a later pass to clear them.
    # Hence: a line is intolerable if it carries a sev>tolerable error AT THE
    # FINAL PASS, or any sev>tolerable error from a pass <= 0.
    error_count, _max_severity = getErrorCount()
    final_pass = self.metadata.get("passCount", 0)

    def _intolerable(rec):
      return rec.severity > self.tolerable_severity and \
          (rec.passCount <= 0 or rec.passCount == final_pass)

    def _line_max_intolerable(errs):
      worst = 0
      for rec in errs:
        if _intolerable(rec):
          worst = max(worst, rec.severity)
      return worst

    unclosed_macro = (len(self.source) > 0 and self.source[-1].inMacroDefinition)

    intolerables = 0
    max_intolerable = 0
    for line in self.source:
      w = _line_max_intolerable(line.errors)
      if w > 0:
        intolerables += 1
        max_intolerable = max(max_intolerable, w)
    if unclosed_macro:
      error_count += 1
      intolerables += 1
      max_intolerable = 255

    if intolerables > 0:
      # Compiler-style diagnostics: one 'file:line: error: message' per
      # intolerable diagnostic, a source excerpt, and (for macro-generated
      # lines) the expansion/invocation chain back to the primary source.
      # Only lines whose errors ABORT the assembly are reported -- transient
      # early-pass errors and tolerated severities are not repeated here
      # (the listing remains the complete record).
      error_lines = []
      REPORT_LIMIT = 50   # sites shown in full; the rest are counted

      def _line_diags(errs):
        # The diagnostics that make this line intolerable (the _intolerable
        # predicate), deduplicated across codegen passes.
        seen, out = set(), []
        for rec in errs:
          if _intolerable(rec) and rec.message not in seen:
            seen.add(rec.message)
            out.append(rec)
        return out

      shown = 0
      for line in self.source:
        diags = _line_diags(line.errors)
        if not diags:
          continue
        shown += 1
        if shown > REPORT_LIMIT:
          continue
        where, ln = self._diag_origin(line)
        for rec in diags:
          sev = "" if rec.severity == 255 else f" (severity {rec.severity})"
          error_lines.append(f"{where}:{ln}: error{sev}: {rec.message}")
        error_lines.append(f"{ln:6d} | {line.text.rstrip()}")
        # A generated line's card image is the raw macro-body text; when
        # substitution changed the operand, show what it became.
        if line.depth > 0 and line.operand and line.operand not in line.text:
          expanded = f"{line.name:<8} {line.operation:<5} {line.operand}".rstrip()
          error_lines.append(f"       | expands to: {expanded}")
        for inv in self._invocation_chain(line):
          w, l = self._diag_origin(inv)
          error_lines.append(
              f"  note: in expansion of macro {inv.operation}, "
              f"invoked from {w}:{l}")
        error_lines.append("")

      if shown > REPORT_LIMIT:
        error_lines.append(
            f"... {shown - REPORT_LIMIT} more line(s) with intolerable "
            f"errors not shown (see listing for the full record)")
      if unclosed_macro:
        error_lines.append("error: no closing MEND for MACRO")

      error_lines.append(
          f"{','.join(source_file_names)}: assembly aborted: "
          f"{error_count} error(s), {intolerables} line(s) with intolerable "
          f"errors (severity > {self.tolerable_severity}).")

      raise AssemblyError("\n".join(error_lines), error_count, max_intolerable)
        
    # Write object file
    self._log(f"Writing object file: {self._relpath(self.object_file)}")
    self.writeObjectModule()
    if self.debug_info:
      self.writeDebugSymbols()
    self._log(f"Assembly complete: {self._relpath(self.object_file)}")
    
  def writeObjectModule(self) -> None:
    def origin(sectName) -> int:
      sect = sects.get(sectName)
      return sect.offset.bytes if sect and sect.offset is not None else 0

    sections = [(name, d.esdSeq,
                 d.offset.bytes if d.offset is not None else 0,
                 bytes(d.memory[:d.used]))
                for name, d in sects.items() if not d.dsect]
    # A symbol registered as an external on an early pass (e.g. a Z-con
    # target seen before its definition) may have ended up defined in this
    # assembly; emitting an ER for it would make the linker demand an export
    # nothing provides.  Keep only names still EXTERNAL at end of assembly --
    # the original assembler likewise dropped EXTRNs of defined symbols.
    externs = [(name, n) for name, n in extrns.items()
               if (sym := symtab.get(name)) is None
               or sym.type == "EXTERNAL"]
    relocations = [(r.symbol, r.section, r.type, r.address.bytes)
                   for r in self.metadata.get('relocations', [])]

    ldEntries = [(name, (sym.address or 0) * 2 + origin(sym.section or ''),
                  sym.section or '')
                 for name in entries if (sym := symtab.get(name))]

    # END entry point: the FIRST entry in source order, if it resolves --
    # not the first that resolves -- addressed without its section offset.
    # Skip a NEGATIVE-valued symbol: a PCGEN-style virtual base (e.g.
    # 'FIOIPR EQU *-FIOBUS1', used only as a bus-indexed addressing origin) is
    # never a real program entry point, and FIOADCNS's 'END' has no operand
    # anyway -- so leave the END entry-address blank rather than emit a bogus
    # (and unencodable) negative entry point.
    end = None
    if entries and (firstName := next(iter(entries))) and (sym := symtab.get(firstName)):
      _entAddr = (sym.address or 0) * 2
      if _entAddr >= 0:
        end = (_entAddr, sym.section or '')

    # Runtime stack frame: an AMAIN/AENTRY routine defines a 'STACK' DSECT whose
    # byte size (STACKEND-STACK) is the frame the linkage editor reserves in the
    # routine's @0 stack.  Emit the compiler-style STACKEND SYM marker for the
    # primary code CSECT (the END entry's section, else the first coded one) so
    # the linker sizes assembler-routine stacks the same way it does compiler
    # objects.  Modules with no 'STACK' DSECT push no HAL/S frame and get none.
    stack_frames = []
    stackDsect = sects.get('STACK')
    if stackDsect is not None and stackDsect.dsect and stackDsect.used.bytes > 0:
      primary = (end[1] if end else
                 next((name for name, _s, _o, img in sections if img), None))
      if primary:
        stack_frames.append((primary, stackDsect.used.bytes))

    module = Module.from_assembly(sections, externs, ldEntries,
                                  end=end, relocations=relocations,
                                  stack_frames=stack_frames)
    with open(self.object_file, 'wb') as f:
      f.write(self._protCards())
      f.write(module.to_bytes(ident="ASM101 0.00"))

  def _protCards(self) -> bytes:
    """Free-format ' PROT <csect> <s>-<e>[,...]' control cards recording the
    SPON/SPOFF store-protect ranges (halfword offsets, hex, end-exclusive).
    One card set per managed csect; a bare ' PROT <csect>' card (no ranges)
    means the source manages protection and protects nothing.  Like HAL/S-FC's
    ' STACK' cards these precede the object module proper and are carried as
    objModule.ControlRecord statements."""
    out = bytearray()

    def emit(csect, parts):
      text = f" PROT {csect} {','.join(parts)}" if parts else f" PROT {csect}"
      out.extend(text.ljust(80)[:80].encode("ebcdicvagc"))

    for csect in self.metadata.get('protManaged', []):
      merged = []
      for s, e in sorted(self.metadata.get('protRanges', {}).get(csect, [])):
        sHw, eHw = s // 2, (e + 1) // 2
        if merged and sHw <= merged[-1][1]:
          merged[-1][1] = max(merged[-1][1], eHw)
        else:
          merged.append([sHw, eHw])
      parts, width = [], 0
      for sHw, eHw in merged:
        piece = f"{sHw:X}-{eHw:X}"
        if width + len(piece) > 56 and parts:    # keep cards within col 71
          emit(csect, parts)
          parts, width = [], 0
        parts.append(piece)
        width += len(piece) + 1
      emit(csect, parts)
    return bytes(out)

  # Map a SymtabEntry.type to the asmg.json 'kind' for a defined symbol.
  # CSECTs are carried in the 'sections' list instead; EXTERNAL (ER imports)
  # and any unknown type are not defined here and are skipped.
  _ASMG_KIND = {"INSTRUCTION": "code", "DATA": "data", "EQU": "equ"}

  def writeDebugSymbols(self) -> None:
    sections = [
        {"name": name, "esdSeq": d.esdSeq, "size": d.used.hw,
         "dsect": bool(d.dsect)}
        for name, d in sects.items()
    ]

    symbols = []
    for name, sym in symtab.items():
      kind = self._ASMG_KIND.get(sym.type)
      if kind is None:        # CSECT (in 'sections'), EXTERNAL imports, unknowns
        continue
      rec = {
          "name": name,
          "kind": kind,
          "scope": "global" if sym.entry else "local",
          "section": sym.section,
          "offset": sym.address,          # halfword offset within section
          "line": sym.n,                  # defining source statement index
      }
      if isinstance(sym.value, int):       # absolute EQU value (not an address)
        rec["value"] = sym.value
      symbols.append(rec)

    symbols.sort(key=lambda s: (s["section"] or "",
                                s["offset"] if s["offset"] is not None else -1,
                                s["name"]))

    # Per-suboperand DC/DS statement records (model101 dataStmts):
    # src/mafgen segments each csect into code and data regions with these
    # and renders the typed DC lines.  A DSECT record describes a layout,
    # not storage, so it is dropped.
    real = {name for name, d in sects.items() if not d.dsect}
    datastmts = sorted(
        (list(rec) for rec in self.metadata.get("dataStmts", [])
         if rec[0] in real),
        key=lambda r: (r[0], r[1], r[2]))

    data = {
        "version": 2,
        "tool": f"{PROGRAM} {VERSION}",
        "source": self.source_files[-1].stem if self.source_files else None,
        "sections": sections,
        "symbols": symbols,
        # [section, startByte, endByte, 'DC'|'DS', letter, elements, label]
        "data": datastmts,
        # literal-pool slots: [section, startByte, endByte, T, L] -- the
        # source type of each =constant, for pool-reference comments
        "literals": sorted(
            (list(r) for r in self.metadata.get("literals", [])
             if r[0] in real),
            key=lambda r: (r[0], r[1])),
    }
    debug_file = self.object_file.with_name(self.object_file.stem + ".asmg.json")
    with open(debug_file, "w") as f:
      json.dump(data, f, indent=1)
    self._log(f"Wrote debug symbols: {self._relpath(debug_file)}")

  def _subtitle(self, left_cols: str) -> str:
    """A listing subtitle: the column legend left-justified to width 95,
    followed by the shared PROGRAM / VERSION / date tag."""
    return f"{left_cols:<95}{PROGRAM} {VERSION:>15} {self.current_date}"

  def writeListing(self, listing_file: Path) -> None:
    """
    Write an assembly listing to a file.

    Args:
        listing_file: Path for the output listing file.
    """
    self._log(f"Writing listing to {self._relpath(listing_file)}")
        
    # Configuration
    lines_per_page = 80
    page_separator = "\f" + ('-' * 120)
        
    with open(listing_file, "w") as f:
      # State for listing generation
      in_copy = False
      member_name = ""
      rvl = 0  # Source of these values unknown; 0/0/0 placeholder
      concat = 0
      nest = 0
      printed_line_number = 0
      page_number = 0
      lines_this_page = 1000  # Force new page on first output

      def page_break(title, subtitle):
        """Start a new listing page: bump the counter, emit the form-feed
        separator (not before the first page), the title, and the subtitle."""
        nonlocal page_number, lines_this_page
        page_number += 1
        if page_number > 1:
          f.write(page_separator + "\n")
        f.write(f"         {title:<100}  PAGE {page_number:4d}\n")
        f.write(subtitle + "\n")
        lines_this_page = 0

      # Build IDs for symbols
      id_counter = 0
      ids = {}

      # ==== External Symbol Dictionary ====
      title = "EXTERNAL SYMBOL DICTIONARY".center(100)
      subtitle = self._subtitle('SYMBOL   TYPE  ID  ADDR  LENGTH  LD ID')
            
      for symbol in symtab:
        if symbol.startswith("_"):
          continue
        entry = symtab[symbol]
        ld_id = "    "
        if entry.type == "CSECT" and not entry.dsect:
          module_type = "SD"
          id_counter += 1
          ids[symbol] = id_counter
          pid = f"{id_counter:04d}"
        elif entry.entry is not None:
          module_type = "LD"
          pid = "    "
          ld_id = f"{ids.get(entry.section, 0):04X}"
        elif entry.type == "EXTERNAL":
          module_type = "ER"
          id_counter += 1
          ids[symbol] = id_counter
          pid = f"{id_counter:04d}"
        else:
          continue

        address = "      "
        if entry.address is not None:
          addr_val = entry.address
          if entry.preliminaryOffset is not None:
            addr_val += entry.preliminaryOffset
          address = f"{addr_val:06X}"
                    
        length = "      "
        if symbol in sects:
          length = f"{(sects[symbol].used + 1).hw:06X}"
                    
        if lines_this_page >= lines_per_page:
          page_break(title, subtitle)

        f.write(f"{symbol:<10}{module_type:<3}{pid:<5}{address:<7}{length:<7}{ld_id}".rstrip() + "\n")
        lines_this_page += 1
            
      # ==== Source Listing ====
      title = ""
      subtitle = ""
      literal_pool_number = 0
      continuation = False
            
      for i in range(len(self.source)):
        stmt = self.source[i]
        skip = False

        if stmt.empty:
          continue

        # Macro definitions fetched on demand (and library-scan-skipped
        # lines) are interleaved into the source but read with
        # printable=False.  Skip them so they neither print nor toggle
        # COPY-member boundary tracking (a non-printable def landing
        # inside a COPY'd member would close and spuriously re-open it
        # with a blank name).  Printable in-definition lines (a COPY'd
        # macro library such as MACSMITH) are still listed.
        if not stmt.printable and (
                stmt.inMacroDefinition or
                stmt.skip):
          continue

        if continuation:
          continuation = stmt.continues
          lines_this_page += 1
          continue
        continuation = stmt.continues
                
        operation = stmt.operation

        if operation == "SPACE":
          space = 1  # placeholder; count actually depends on the operand
          printed_line_number += space
          stmt.printedLineNumber = printed_line_number
          lines_this_page += space
        elif operation == "TITLE":
          title = (stmt.operand or "").rstrip()[1:-1]
          subtitle = self._subtitle('  LOC  OBJECT CODE   ADR1 ADR2      SOURCE STATEMENT')
          printed_line_number += 1
          stmt.printedLineNumber = printed_line_number
          lines_this_page = 1000
          skip = True
                    
        if lines_this_page >= lines_per_page:
          page_break(title, subtitle)
          if skip:
            continue
                        
        if stmt.depth > 0:
          depth_star = "+"
        else:
          depth_star = ' '
                    
        if operation in self.MACRO_LANGUAGE_OPS:
          continue
        if stmt.fullComment and stmt.text.startswith("*/"):
          continue  # "Modern" comment
                    
        if stmt.copy:
          if not in_copy:
            member_name = Path(stmt.file or "").stem
            if stmt.printable:
              lines_this_page += 1
              f.write(f"         START OF COPY MEMBER {member_name:<8} RVL {rvl:02d} CONCATENATION NO. {concat:03d}  NEST {nest:03d}\n")
            in_copy = True
        else:
          if in_copy:
            if stmt.printable:
              lines_this_page += 1
              f.write(f"           END OF COPY MEMBER {member_name:<8} RVL {rvl:02d} CONCATENATION NO. {concat:03d}  NEST {nest:03d}\n")
            in_copy = False
                        
        if stmt.printable:
          # 'offset' is the section's byte offset (0 when unplaced).
          offset = sectionOffset(stmt.section) if stmt.section else 0
          prefix = stmt.listing_prefix(symtab, offset, _num)

          # A macro-invocation line is echoed in the listing only for
          # library/COPY-provided macros, not for macros defined in the
          # primary source.  'fromLibrary is False' means a fully-defined
          # (MEND seen) primary-source macro; None (definition never closed)
          # falls through, matching the old len-guarded array check.
          if operation in self.macros and not stmt.inMacroDefinition:
            macro_where = self.macros[operation]
            if macro_where.fromLibrary is False:
              continue
          if operation in self.macros and stmt.depth > 0:
            continue
                        
          if stmt.depth == 0:
            identification = stmt.identification[:8]
          else:
            identification = f"{stmt.depth:02d}-"
            suffix = ""
            if stmt.macro is not None:
              suffix = stmt.macro
            identification = identification + suffix[:5]
                        
          if stmt.dotComment:
            pass
          elif stmt.fullComment or stmt.inMacroDefinition:
            printed_line_number += 1
            stmt.printedLineNumber = printed_line_number
            lines_this_page += 1
            text = stmt.text.rstrip()
            if (identification.strip() == "" or
                (stmt.fullComment and depth_star != " " and
                 not stmt.mnote)):
              f.write(f"{prefix:<30}{printed_line_number:5d}{depth_star}{text}\n")
            else:
              f.write(f"{prefix:<30}{printed_line_number:5d}{depth_star}{text:<71} {identification}\n")
          elif operation == "":
            continue
          else:
            name = stmt.name
            if name.startswith("."):
              name = ""
            printed_line_number += 1
            stmt.printedLineNumber = printed_line_number
            lines_this_page += 1
            operand_str = str(stmt.operand).rstrip()
            mid = f"{prefix:<30}{printed_line_number:5d}{depth_star}{name:<8} {operation:<5} {operand_str}"
            identification = stmt.identification[:8] if stmt.depth == 0 else identification
            f.write(f"{mid:<108}{identification}\n")
                        
          if operation == "LTORG" and literal_pool_number < len(literalPools):
            pool = literalPools[literal_pool_number]
            # Each literal's byte offset within the pool is the parallel
            # 'pool.offsets' list (filled in by the layout pass), and the pool
            # itself sits at byte 'pool.offset' in CSECT 'pool.sect'.  Emit
            # each at its halfword listing address -- section offset +
            # halfword, mirroring the instruction lines above -- sorted by
            # address, which is the layout order (descending alignment, not
            # source order).
            reordered = {}
            for idx, entry in enumerate(pool.literals):
              if idx >= len(pool.offsets) or pool.offsets[idx] is None:
                continue
              paddress = (pool.offset + pool.offsets[idx]).hw \
                  + sectionOffset(pool.sect)
              reordered[paddress] = entry
            for k in sorted(reordered):
              attributes = reordered[k]
              prefix = f"{k:05X} "
              bytes_data = attributes.assembled
              for m in range(attributes.L):
                if m < len(bytes_data):
                  prefix += f"{bytes_data[m]:02X}"
              prefix = f"{prefix[:30]:<50}"
              f.write(prefix + " " + str(attributes.operand) + "\n")
            literal_pool_number += 1
            
      # ==== Cross Reference ====
      lines_this_page = 1000
      for symbol in sorted(symtab, key=self._sort_order):
        sym_props = symtab[symbol]
        if symbol.startswith("_") or symbol.startswith("."):
          continue
        if lines_this_page >= lines_per_page:
          page_number += 1
          f.write(page_separator + "\n")
          f.write(f"{'':<45}{'CROSS REFERENCE':<66}PAGE {page_number:4d}\n")
          f.write(self._subtitle('SYMBOL    LEN    VALUE   DEFN   REFERENCES') + "\n")
          lines_this_page = 0
                    
        if sym_props.type in ["EQU", "CSECT", "EXTERNAL"]:
          length = 1
        elif sym_props.properties is not None and sym_props.properties.scratch is not None:
          scratch = sym_props.properties.scratch
          if scratch.get("length", 0) < 2:
            length = 1
          else:
            length = scratch["length"] // 2
        else:
          length = 2

        value = sym_props.value if sym_props.value is not None else 0
        defn = "     "
        if sym_props.properties is not None and sym_props.properties.printedLineNumber is not None:
          defn = f"{sym_props.properties.printedLineNumber:5d}"

        section_name = sym_props.section
        if section_name:
          value += sectionOffset(section_name)
        value = _num(value)  # numeric image of a (possibly Reloc) address

        if sym_props.type in ["INSTRUCTION", "DATA"]:
          line = f"{symbol:<8} {length:5d}   {value & 0xFFFFFF:06X} {defn}"
        else:
          line = f"{symbol:<8} {length:5d} {value & 0xFFFFFFFF:08X} {defn}"
                    
        num_refs = 0
        if sym_props.references is not None and len(sym_props.references) > 0:
          line += " "
          for n in sorted(sym_props.references):
            if n < len(self.source) and self.source[n].printedLineNumber is not None:
              if num_refs == 15:
                f.write(line + "\n")
                lines_this_page += 1
                line = " " * 30
                num_refs = 0
              line += f" {self.source[n].printedLineNumber:5d}"
              num_refs += 1
        f.write(line + "\n")
        lines_this_page += 1

    self._log(f"Listing written: {self._relpath(listing_file)}")
    
  def _sort_order(self, s: str) -> str:
    """
    A peculiar collation for sorting the symbol table on the printout.
    It's not EBCDIC, nor ASCII. The alphanumeric ordering seems normal,
    but the other "letters" (#, @, $) follow the alphanumerics.
    """
    converted = ""
    for c in s:
      if c == "$":
        converted += 'a'
      elif c == "#":
        converted += 'b'
      else:
        converted += c
    return converted


