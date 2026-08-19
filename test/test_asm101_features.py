#!/usr/bin/env python3
#
# Feature / regression tests for the src/asm101 assembler fork.
#
# Run:
#     build/venv/bin/python test/test_asm101_features.py
# or:  
#     ctest -R asm101_features
#
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "asm101"

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append(f"{name}: {detail}" if detail else name)


# =====================================================================
# UNIT: parser / evaluator behaviors (direct import)
# =====================================================================
def unit_tests():
  from asm101.larkparse import parse
  from asm101.model101tables import ARGS_MSC

  # --- MSC table: @BXC and @CALL are distinct entries  ---
  check("msc_bxc_and_call_distinct",
        "@BXC" in ARGS_MSC and "@CALL" in ARGS_MSC
        and "@BXC@CALL" not in ARGS_MSC,
        f"@BXC={'@BXC' in ARGS_MSC} @CALL={'@CALL' in ARGS_MSC}")

  from asm101.larkparse import split_top_level, first_blank_outside
  from asm101.expressions import (
      svDeclare, svSet, svReplace,
      evalArithmeticExpression, evalCharacterExpression,
      evalBooleanExpression, SymbolTable, SymbolicVar,
  )
  import asm101.expressions as _exprmod

  def scope(d=None):
    """Build a SymbolTable test scope: each &VAR value wrapped in a
    SymbolicVar (already-wrapped values pass through, so this is safe to
    apply more than once).  Parented to the global table so a lookup falls
    back to svGlobals, matching production local scopes."""
    st = SymbolTable(parent=_exprmod.svGlobals)
    for k, v in (d or {}).items():
      st[k] = v if isinstance(v, SymbolicVar) else SymbolicVar(v)
    return st
  from asm101.statement import Statement
  _exprmod.asmContext.passCount = 1

  # The evaluator/error-collector takes a Statement; tests that don't care
  # about a real source line pass a throwaway one as the error sink.
  def props():
    return Statement()

  # --- grammar: RS operand with an omitted index, "D2(,B2)" ---
  check("rsAll_empty_index",
        parse("F0,0(,B2)", "rs") is not None,
        "F0,0(,B2) should parse as RS with omitted X2")
  check("rsAll_explicit_index",
        parse("F0,0(0,B2)", "rs") is not None)

  # --- grammar: ZCON DC forms.  The real decks use Z(,sym,flags) (leading
  # comma) as well as Z(sym,,flags); the dcOperand rule had no Z alternative
  # at all (stale generated parser), so every `DC Z(...)` failed to parse.
  from asm101.larkparse import Sym, BinOp
  def zcon(text):
    ast = parse(text, "dc")
    if ast is None:
      return None
    return ast[-1]     # Lark dc yields a flat list of DcSuboperands
  # The reloc target is now an arith node (zexpr): a bare Sym, or Sym +/- a
  # constant addend.  `zname` pulls the symbol name out for the simple cases.
  def zname(s):
    e = getattr(s, "zexpr", None)
    if isinstance(e, Sym):
      return e.name
    if isinstance(e, BinOp) and isinstance(e.left, Sym):
      return e.left.name
    return None
  z = zcon("Z(,STM4,X'8')")
  check("zcon_leading_comma", z is not None and zname(z) == "STM4"
        and z.flags is not None,
        f"got {z!r}")
  check("zcon_no_flags", (lambda s: s is not None and zname(s) == "EXTTEMP2"
                          and s.flags is None)(zcon("Z(,EXTTEMP2)")))
  check("zcon_symbol_first",
        (lambda s: s is not None and zname(s) == "FOO")(zcon("Z(FOO,,0)")))
  check("zcon_mixed_with_other_suboperand",
        (lambda s: s is not None and zname(s) == "BAR")(zcon("Y(X),Z(,BAR,8)")))
  # Reloc target with an addend (Sym + constant) -- `Z(,FIOBRU+4,0)` etc.
  check("zcon_addend",
        (lambda s: s is not None and isinstance(s.zexpr, BinOp)
         and zname(s) == "FOO" and s.zexpr.op == "+")(zcon("Z(,FOO+4,0)")))

  # --- grammar: EQU value,length,type ---
  check("equ_multi_operand",
        parse("1,1,C'#'", "equ") is not None,
        "EQU operand value,length,type should parse")
  check("equ_plain",
        parse("5", "equ") is not None)

  # --- grammar: T' type attribute on a subscripted symbol ---
  # Recover the subscript index list from either parser shape: the old
  # positional ('T'', name, '(', i [, ',', j], ')') or the clean Lark
  # Tattr(Var(name, (i, ...))).  The test intent -- the T' parses and
  # keeps its index/indices -- holds for both.
  def _tprime_indices(ast):
    from asm101.larkparse import Tattr, Var
    if isinstance(ast, Tattr) and isinstance(ast.operand, Var):
      return ast.operand.idxs
    if isinstance(ast, (list, tuple)) and len(ast) >= 5 \
            and ast[0] == "T'" and ast[2] == "(":
      return [ast[3]] + ([ast[5]] if len(ast) == 7 else [])
    return None
  tast = parse("T'&SYSLIST(1)", "cexpr")
  check("Tprime_subscript_parses", tast is not None)
  check("Tprime_subscript_keeps_index",
        (lambda ix: ix is not None and len(ix) == 1)(_tprime_indices(tast)),
        f"got {tast!r}")
  check("Tprime_in_aif",
        parse("(T'&SYSLIST(1) EQ 'O').LOOP", "aif") is not None)
  # Two-subscript T'&X(i,j): the CASE/DO macros guard
  # `AIF (T'&SYSLIST(1,2) EQ 'O' AND ...)` to detect whether operand 1 is a
  # parenthesized sublist.  characterExpression already accepted it, but the
  # aifAll path's relational-T' operand stopped at the first subscript and the
  # trailing `(...,j)` made the whole AIF fail to parse ("Unrecognized AIF
  # operand").  Now both parse and the two-subscript form keeps both indices.
  t2 = parse("T'&SYSLIST(1,2)", "cexpr")
  check("Tprime_two_subscript_parses",
        (lambda ix: ix is not None and len(ix) == 2)(_tprime_indices(t2)),
        f"got {t2!r}")
  check("Tprime_two_subscript_in_aif",
        parse("(T'&SYSLIST(1,2) EQ 'O' AND &NBR NE 1).NOTSUBL", "aif")
        is not None)

  # --- SETC value may exceed 8 characters (HLASM limit is 255) ---
  sv = scope()
  svDeclare("LCLC", "&M1", sv, props())
  sv["&M1"] = SymbolicVar("")
  for _ in range(20):
    svSet("SETC", "&M1", "'&M1'.' '", sv, props())
  check("setc_grows_past_8", len(sv["&M1"].value) == 20,
        f"len(&M1)={len(sv['&M1'].value)}, expected 20")

  # --- doubled-quote escape in a character expression is kept, not dropped ---
  # The old evaluator dropped content at a '' (so 'X''8000''+' -> 'X8000+'),
  # which made every macro-built `SVC X'8000'+N` assemble to 0000 (the symbol
  # X8000 is undefined).  The clean Lark side keeps it -> 'X'8000'+', and the
  # SVC assembles to its real encoding C9FB 80xx (validated vs the IBM OI301700
  # BILDNEW5 listing).  Asserted per active parser so the check is honest both
  # ways during the transition; fastparse (now retired) exhibited the drop.
  cq = parse("'X''8000''+'", "cexpr")
  _cqv = evalCharacterExpression(cq, scope(), props()) if cq else None
  check("charexpr_doubled_quote_kept", _cqv == "X'8000'+",
        f"got {_cqv!r}, expected \"X'8000'+\"")

  # --- SET operands tolerate a trailing comment ---
  sv = scope()
  svDeclare("LCLA", "&A", sv, props())
  p = props()
  svSet("SETA", "&A", "&A+1                  INCREMENT POINTER", sv, p)
  check("seta_with_comment", sv["&A"].value == 1 and not p.errors,
        f"&A={sv['&A'].value} errors={p.errors}")
  sv = scope()
  svDeclare("LCLC", "&C", sv, props())
  p = props()
  svSet("SETC", "&C", "'HELLO'   THIS IS A COMMENT", sv, p)
  check("setc_with_comment", sv["&C"].value == "HELLO" and not p.errors,
        f"&C={sv['&C'].value!r} errors={p.errors}")

  # --- Multi-value SET: `&ARR(k) SETx v1,v2,...` assigns the sub-operands to
  # CONSECUTIVE array elements starting at k (IBM macro feature).  This was
  # unimplemented -- only &ARR(k) got v1, the rest stayed empty/zero -- which
  # silently broke MACSMITH's TEXT macro: its &T9 letter table ('A','B',...,
  # 'Z') names message data-insert sublabels MSGnnnA/MSGnnnB/...; with only
  # &T9(1)='A' set, every insert past the first got an empty suffix, leaving
  # those sublabels undefined and the base MSGnnn label multiply-defined. ---
  # The top-level comma splitter must respect quotes/parens and stop at the
  # comment; commas inside substrings/attributes/comments are NOT separators.
  check("splitset_quoted_list",
        split_top_level("'A','B','C'") == ["'A'", "'B'", "'C'"])
  check("splitset_numeric_list",
        split_top_level("48,49,50,51") == ["48", "49", "50", "51"])
  check("splitset_substring_not_split",
        split_top_level("'&STR'(2,K'&STR-2)    SET CURRENT MSG")
        == ["'&STR'(2,K'&STR-2)"])
  check("splitset_attr_subscript_not_split",
        split_top_level("K'&SYSLIST(&I,3)") == ["K'&SYSLIST(&I,3)"])
  check("splitset_comment_comma_not_split",
        split_top_level("'MSGTTT'       MSG #,LIST 6") == ["'MSGTTT'"])

  sv = scope()
  svDeclare("LCLC", "&T9(26)", sv, props())
  p = props()
  svSet("SETC", "&T9(1)",
        ",".join("'%s'" % chr(ord("A") + i) for i in range(26)), sv, p)
  t9 = sv["&T9"].value
  check("setc_multivalue_fills_array",
        t9[0] == "A" and t9[1] == "B" and t9[25] == "Z"
        and not p.errors,
        f"&T9[:3]={t9[:3]} [25]={t9[25]!r} errs={p.errors}")
  sv = scope()
  svDeclare("LCLA", "&CC(60)", sv, props())
  p = props()
  svSet("SETA", "&CC(37)", "43,45,47,46", sv, p)
  cc = sv["&CC"].value
  check("seta_multivalue_offset_index",
        cc[36:40] == [43, 45, 47, 46] and not p.errors,
        f"&CC[36:40]={cc[36:40]} errs={p.errors}")
  # A single subscripted value with a comment is unchanged (not mis-split).
  sv = scope()
  svDeclare("LCLC", "&T3(264)", sv, props())
  p = props()
  svSet("SETC", "&T3(5)", "'MSGTTT'       MSG #,LIST 6", sv, p)
  check("setc_subscript_single_with_comment",
        sv["&T3"].value[4] == "MSGTTT" and not p.errors,
        f"&T3[4]={sv['&T3'].value[4]!r} errs={p.errors}")

  # --- T' of an omitted operand is 'O', a self-defining term / integer is 'N'
  # (the SBR/#SPLIT macros branch on `T'&x EQ 'N'`), otherwise 'C' ---
  def tprime(text, sv):
    ast = parse(text, "cexpr")
    return evalCharacterExpression(ast, scope(sv), props())
  check("tprime_omitted_is_O", tprime("T'&X", {"&X": ""}) == "O")
  check("tprime_present_is_C", tprime("T'&X", {"&X": "ABC"}) == "C")
  check("tprime_selfdef_is_N", tprime("T'&X", {"&X": "5"}) == "N")
  check("tprime_signed_is_N", tprime("T'&X", {"&X": "-82"}) == "N")
  syslist = scope({"&SYSLIST": ["", "H"]})
  check("tprime_syslist_omitted", tprime("T'&SYSLIST(1)", syslist) == "O")
  check("tprime_syslist_present", tprime("T'&SYSLIST(2)", syslist) == "C")
  check("tprime_syslist_past_end", tprime("T'&SYSLIST(9)", syslist) == "O")
  # Two-subscript T'&SYSLIST(i,j): element j of operand i's sublist.  A scalar
  # operand is a one-element sublist (j>1 is omitted -> 'O'); a real sublist's
  # present element is 'C'.  This is what `CASE 2,1,5` exercises (operand 1 is
  # scalar '2', so T'&SYSLIST(1,2) is 'O' -> the .NOTSUBL branch).
  sub = scope({"&SYSLIST": ["2", ("A", "B")]})   # op1 scalar, op2 a 2-elem sublist
  check("tprime_two_subscript_scalar_op_is_O",
        tprime("T'&SYSLIST(1,2)", sub) == "O")
  check("tprime_two_subscript_sublist_present",
        tprime("T'&SYSLIST(2,2)", sub) == "C")
  check("tprime_two_subscript_sublist_past_end",
        tprime("T'&SYSLIST(2,9)", sub) == "O")

  # --- svReplace must NOT substitute the operand of a T' reference ---
  repl = svReplace(props(), "(T'&SYSLIST(1) EQ 'O').X", scope({"&SYSLIST": ["", "H"]}))
  check("svreplace_preserves_Tprime", "T'&SYSLIST(1)" in repl,
        f"got {repl!r}")

  # --- ENTRY/EXTRN (idlist): a trailing comment is stripped before parsing ---
  check("idlist_trailing_comment",
        parse("FSVC0030  ALTERNATE ENTRY POINT", "idlist") == ["FSVC0030"])
  check("idlist_extrn_with_literal_comment",
        parse("FPMMPC1V            =X'001FFFFF'", "idlist") == ["FPMMPC1V"])
  check("idlist_plain_pair", parse("MENU,MENUA", "idlist") == ["MENU", "MENUA"])

  # --- DS reserves storage for the bare A/Y types (no value operand) ---
  check("ds_bare_Y", (parse("Y", "ds") or [None])[0].type == "Y")
  check("ds_bare_A", (parse("A", "ds") or [None])[0].type == "A")
  check("ds_bare_YL2",
        (lambda s: s is not None and s[0].type == "Y" and s[0].length == ("L", "2"))
        (parse("YL2", "ds")))
  check("dc_Y_value_still_parses", parse("Y(LBL)", "ds") is not None)
  check("dc_A_hex_still_parses", parse("A'1F'", "ds") is not None)

  # --- D' defined attribute in AIF (D is overloaded with the D-float type) ---
  check("dprime_attr_with_comment",
        parse("(D'&SAVAREA).RESTORE  HAS SAVE AREA BEEN EXTRN'ED", "aif") is not None)
  check("dprime_float_comment_preserved",      # D'1.5' is a quote, comment after
        first_blank_outside("D'.232830643653869628E-9' SCALE") == 25)
  check("dprime_attr_then_comment_blank",      # D'SYM is an attribute, blank after
        first_blank_outside("D'FOO  COMMENT") == 5)

  # --- LCL/GBL declaration list continued across cards joins seamlessly: the
  # first card ends "...&A," padded with blanks, the next resumes "&B,&C" -- the
  # join leaves "&A,  &B,&C", which must still declare every variable ---
  cont = scope()
  svDeclare("LCLA", "&XPCT,  &XSA1,&XSA2,&P", cont, props())
  check("lcl_continuation_join",
        all(cont.lookup(v) is not None for v in ("&XPCT", "&XSA1", "&XSA2", "&P")),
        f"declared: {[v for v in ('&XPCT','&XSA1','&XSA2','&P') if cont.lookup(v) is not None]}")

  # --- svReplace: SETB renders as its bit "0"/"1", not Python "False"/"True"
  # (bool is an int subclass, so the int branch used to catch it first), and
  # CONCATENATED variables (&A&B&C, no separators -- e.g. the FCW2 binary
  # string BL.10'&#EORB&#INCRB...') must ALL substitute, not every other one
  # (re-parsing the already-modified text merged a neighbour's replacement
  # into the name). ---
  bits = scope({"&A": False, "&B": True, "&C": False, "&D": True, "&E": False})
  check("svreplace_setb_is_bit",
        svReplace(props(), "&A &B", bits) == "0 1",
        f"got {svReplace(props(), '&A &B', bits)!r}")
  _concat = svReplace(props(), "BL.5'&A&B&C&D&E'", bits)
  check("svreplace_concat_all_substituted", _concat == "BL.5'01010'",
        f"got {_concat!r}")
  # Regression: indexed array refs (bare and variable index) and the join
  # character still resolve.
  arr = scope({"&ARR": ["X", "Y", "Z"], "&J": 2, "&N": 14})
  check("svreplace_index_literal", svReplace(props(), "&ARR(2)", arr) == "Y")
  check("svreplace_index_variable", svReplace(props(), "&ARR(&J)", arr) == "Y")
  check("svreplace_join_char", svReplace(props(), "&N.X", arr) == "14X")

  # --- array elements coerce to int in an arithmetic context ---
  ast = parse("&SYSLIST(1)", "arith_only")
  v = evalArithmeticExpression(ast, scope({"&SYSLIST": ["5", "9"]}), props())
  check("array_element_int_coercion", v == 5,
        f"got {v!r} (type {type(v).__name__})")

  # --- K' count attribute applies to SETA integers, not just strings ---
  # K' is the number of characters in the value as substituted: K'14 == 2.
  def kprime(text, sv):
    return evalArithmeticExpression(
        parse(text, "arith_only"), scope(sv), props())
  check("kprime_of_int", kprime("K'&N", {"&N": 14}) == 2,
        "K'14 should be 2 (chars in the decimal value)")
  check("kprime_of_string", kprime("K'&N", {"&N": "ABCDE"}) == 5)
  check("kprime_of_bool", kprime("K'&N", {"&N": True}) == 1)

  # --- SETA division truncates toward zero (integer, not float) ---
  def seta(expr):
    sv = scope()
    svDeclare("LCLA", "&A", sv, props())
    p = props()
    svSet("SETA", "&A", expr, sv, p)
    return sv["&A"].value, p.errors
  v, e = seta("7/2")
  check("seta_div_truncates", v == 3 and not e, f"got {v!r} errs={e}")
  v, e = seta("0/100")
  check("seta_div_zero_numerator_is_int", v == 0 and isinstance(v, int) and not e,
        f"got {v!r} (type {type(v).__name__}) errs={e}")

  # --- SETA of a SETC holding a SIGNED integer string ---
  # XPOS strips one '-' off the POS-generated "--285", leaving "-285", then
  # does "&#X SETA &XFLD"; the SETC value is a negative integer string, which
  # must coerce just like an unsigned one (previously only .isdigit() passed).
  sv = scope()
  svDeclare("LCLC", "&S", sv, props())
  sv["&S"] = SymbolicVar("-285")
  svDeclare("LCLA", "&A", sv, props())
  p = props()
  svSet("SETA", "&A", "&S", sv, p)
  check("seta_of_negative_string", sv["&A"].value == -285 and not p.errors,
        f"&A={sv['&A'].value!r} errors={p.errors}")

  # --- unary / doubled leading minus in arithmetic ---
  # The grammar admits a leading sign on a factor; eval folds it.  A lone
  # signed literal (-285) is still captured by `constant`, so existing ASTs
  # are unchanged, but -&V and the doubled "--285" (from POS's "XPOS -&L"
  # with a negative coordinate) now evaluate.
  def arith(text, sv):
    return evalArithmeticExpression(
        parse(text, "arith_only"), scope(sv), props())
  check("unary_minus_literal_unchanged", arith("-285", {}) == -285)
  check("unary_double_minus_folds", arith("--285", {}) == 285)
  check("unary_minus_on_variable", arith("-&V", {"&V": 285}) == -285)
  check("unary_double_minus_on_variable", arith("--&V", {"&V": 285}) == 285)
  check("unary_minus_in_expression", arith("-&V+1", {"&V": 285}) == -284)

  # --- two-subscript sublist &X(i,j) as an arithmetic VALUE (the CASE macro's
  # `&CASENO SETA &SYSLIST(1,&NBR)`).  Resolution matches the K'/N' path:
  # operand i, then element j of its sublist; &SYSLIST(0) is the macro name; a
  # scalar operand is a one-element sublist.  Previously only the single-
  # subscript &X(i) form evaluated (&X(i,j) -> "Eval error type 3"). ---
  sl1 = scope({"&SYSLIST": ["1"], "&SYSLIST0": "CASE"})          # CASE 1
  check("arith_two_subscript_scalar", arith("&SYSLIST(1,1)", sl1) == 1)
  slp = scope({"&SYSLIST": [("2", "1", "5")], "&SYSLIST0": "CASE"})  # CASE (2,1,5)
  check("arith_two_subscript_sublist_1", arith("&SYSLIST(1,1)", slp) == 2)
  check("arith_two_subscript_sublist_3", arith("&SYSLIST(1,3)", slp) == 5)
  check("arith_two_subscript_var_index",
        arith("&SYSLIST(1,&N)", {**slp, "&N": 2}) == 1)

  # --- two-subscript &X(i,j) in a MODEL STATEMENT (svReplace).  DOPROC emits
  # `LR &SYSLIST(&I,1),&SYSLIST(&I,2)`; nameSet0 used to drop the (i,j) and
  # svReplace rendered &SYSLIST bare (the whole nested arg list, Python repr)
  # with "(i,j)" left as garbage text -> "Could not parse operands" /
  # "Unrecognized line".  Now both subscripts apply (same semantics as the
  # arith path: operand i, element j; &SYSLIST(0)=macro name; scalar=1-elem;
  # out-of-range=null). ---
  _dosl = scope({"&SYSLIST": [("R3", "3"), "", "", "", "", ""], "&SYSLIST0": "DO"})
  check("svreplace_two_subscript_model_stmt",
        svReplace(props(), "LR    &SYSLIST(1,1),&SYSLIST(1,2)", _dosl)
        == "LR    R3,3",
        f"got {svReplace(props(), 'LR    &SYSLIST(1,1),&SYSLIST(1,2)', _dosl)!r}")
  check("svreplace_two_subscript_oor_null",
        svReplace(props(), "X&SYSLIST(2,1)Y", _dosl) == "XY")
  check("svreplace_two_subscript_macro_name",
        svReplace(props(), "&SYSLIST(0,1)", _dosl) == "DO")
  check("svreplace_two_subscript_scalar",
        svReplace(props(), "&P(1,1)/&P(1,2)", scope({"&P": "VAL"})) == "VAL/")
  from asm101.model101 import parseSignedInt
  check("parse_signed_int_double", parseSignedInt("--285") == 285)
  check("parse_signed_int_single", parseSignedInt("-285") == -285)
  check("parse_signed_int_plain", parseSignedInt("285") == 285)
  check("parse_signed_int_nonint", parseSignedInt("2A5") is None)

  # --- DC-style instruction literals =H/=F/=E/=D/=Y (fastparse _lconstant).
  # Only =C/=X/=B were ported before; CASE/DO/SCHEDULE/DCI decks need the
  # numeric and address forms.  H/F/E/D are constant-valued (parser-only port);
  # =Y(expr) routes through ordinary arithmetic so a bare label, a label offset
  # (=Y(label-1)) and a label difference (=Y(END-START)) all evaluate -- the
  # old Y branch read symtab[sym]["pos1"] (a key labels never carry) and only
  # ever handled a bare symbol; it was dead code since TatSu removal. ---
  from asm101.model101 import evalLiteralAttributes as _evalLit
  def litbytes(text, symtab=None):
    ast = parse(text, "rs")
    if ast is None or "L2" not in ast:
      return None
    attr = _evalLit({"errors": []}, ast, symtab or {})
    return attr.assembled.hex().upper() if attr else None
  _h = litbytes("R1,=H'58'")
  check("lit_h_halfword", _h == "003A", f"=H'58' -> {_h!r}, want 003A")
  check("lit_f_fullword", litbytes("R2,=F'64'") == "00000040")
  check("lit_e_ibm_short", litbytes("R4,=E'1.0'") == "41100000")
  check("lit_d_ibm_double", litbytes("R4,=D'1.0'") == "4110000000000000")
  # =Y forms (symtab carries hashed-clean values; low 16 bits = halfword off)
  from asm101.model101 import SymtabEntry
  _yst = {"LSYM": SymtabEntry(type="DATA", value=1, address=1),
          "LBEG": SymtabEntry(type="DATA", value=0),
          "LEND": SymtabEntry(type="DATA", value=2)}
  check("lit_y_bare_label", litbytes("R3,=Y(LSYM)", _yst) == "0001")
  check("lit_y_label_diff", litbytes("R4,=Y(LEND-LBEG)", _yst) == "0002")
  check("lit_y_label_offset", litbytes("R1,=Y(LSYM-1)", _yst) == "0000")
  # operand reconstruction (pool key / listing): Y uses the captured raw text
  # (parens), the rest use the quoted form.
  _yast = parse("R3,=Y(LEND-LBEG)", "rs")
  check("lit_y_operand_string",
        _evalLit({"errors": []}, _yast, _yst).operand == "=Y(LEND-LBEG)",
        f"got {_evalLit({'errors':[]}, _yast, _yst).operand!r}")

  # --- lconstant coverage: the Lark front-end's clean `Lcon` node must
  # parse and evaluate through evalLiteralAttributes for every literal type,
  # including =B (no driver deck exercises it) and the L/S modifiers, alongside
  # the deck-seen types.  (This was a fastparse-vs-Lark byte-identical
  # differential until fastparse was retired; byte-exactness for the
  # deck-relevant types is pinned by the lit_* checks above.) ---
  from asm101 import larkparse as _lk
  _lit_syms = {n: SymtabEntry(type="RELOCATABLE", value=v, dsect=False)
               for n, v in [("LEND", 2), ("LBEG", 0), ("LSYM", 1)]}
  for _t in ["R1,=X'5555'", "=X'0000002D'", "=F'32'", "=F'0'", "=E'46.1'",
             "=H'58'", "R0,=D'1.0'", "=C'WORD'", "=C'A''B'", "=B'1010'",
             "=B'101010101'", "=CL4'AB'", "=FS2'1.5'", "=Y(LEND-LBEG)"]:
    _ast = _lk.parse(_t, "rs")
    _ac = _evalLit({"errors": []}, _ast, _lit_syms) if _ast else None
    check("lconstant_%s" % re.sub(r"\W+", "_", _t).strip("_"),
          _ac is not None and _ac.assembled is not None,
          f"{_t!r}: parsed={_ast is not None} attr={_ac!r}")

  # --- literalIndex: a literal pool dedups/matches on the OPERAND text (its
  # key), not whole-dict equality.  A forward =Y(label) literal's value shifts
  # pass-to-pass as the label resolves (RS->SRS condensation); whole-dict
  # matching flagged that as "Literal has changed value" and never converged.
  from asm101.model101 import literalIndex, LiteralPool, Literal
  _pool = LiteralPool()
  _pool.add(Literal(operand="=Y(LATE)", value=4, T="Y", L=2,
                    assembled=bytearray(b"\x00\x04")))
  _same_op_diff_val = Literal(operand="=Y(LATE)", value=2, T="Y", L=2,
                              assembled=bytearray(b"\x00\x02"))
  check("literal_index_matches_by_operand",
        literalIndex(_pool, _same_op_diff_val) == 0,
        "same operand, changed value, must match the existing slot")
  check("literal_index_distinct_operand",
        literalIndex(_pool, Literal(operand="=Y(OTHER)", value=4, T="Y", L=2,
                                    assembled=bytearray(b"\x00\x04"))) == -1)

  # --- Statement: the typed per-line object.  Every well-known item is a real
  # dataclass field or property, set and read as an attribute; the property-
  # backed empty/fullComment are overridden by the macro/MNOTE paths (which the
  # byte baselines may not exercise).  (Statement is imported above.)
  # Statement splits the card itself: text=cols1-71, identification=cols73-80.
  _card = ".* a dotted comment" + " " * 53 + "SEQ00042"   # 80-col card
  _st = Statement(line=_card, depth=2)
  check("statement_card_split",
        _st.text == _card[:71] and _st.identification == "SEQ00042",
        f"text/id split off: {_st.text!r} / {_st.identification!r}")
  # text-derived flags computed once at construction
  check("statement_derived_flags",
        _st.dotComment is True and _st.empty is False
        and _st.fullComment is False,
        f"derived flags off: {_st.dotComment},{_st.empty},{_st.fullComment}")
  # property-backed key set as an attribute updates the property
  _st.empty = True
  _st.fullComment = True
  check("statement_property_setter_routes",
        _st.empty is True and _st.fullComment is True,
        "setting a property-backed attribute must update the property")
  # reassigning text must NOT recompute the frozen flags (MNOTE rewrites text)
  _st.text = "*now looks like a comment"
  check("statement_flags_frozen_vs_text",
        _st.dotComment is True and _st.empty is True,
        "derived flags must stay frozen when text is reassigned")
  # `continues` is derived from a non-blank col 72 AND a continuation card
  # (cols 1-15 blank) following -- both conditions required.
  _cont = Statement(line="X" * 71 + "C", nextline=" " * 15 + "MORE")
  check("statement_continues_derived", _cont.continues is True,
        "col-72 flag + continuation card must yield continues=True")
  _ord = Statement(line="X" * 71 + "C", nextline="LABEL OP ARG")
  check("statement_continues_needs_cont_card", _ord.continues is False,
        "col-72 flag with an ordinary next card must yield continues=False")
  # is_blank_or_comment: the comment/blank skip predicate (consolidates the
  # empty/fullComment/dotComment trio checked across the parse/codegen passes).
  check("statement_blank_or_comment",
        Statement(line="   ").is_blank_or_comment is True
        and Statement(line="* c").is_blank_or_comment is True
        and Statement(line=".* c").is_blank_or_comment is True
        and Statement(line="LBL LH R1,X").is_blank_or_comment is False,
        "is_blank_or_comment must hold for blank/'*'/'.*' lines only")
  # parse_fields() splits text -> name/operation and returns the operand column
  _pf = Statement(line="LBL      LH    R1,X")
  _col = _pf.parse_fields()
  check("statement_parse_fields",
        _pf.name == "LBL" and _pf.operation == "LH"
        and _pf.text[_col:].startswith("R1,X"),
        f"parse_fields -> name={_pf.name!r} op={_pf.operation!r} col={_col}")
  # objcode_hex(): up to 8 bytes, space before each even byte; DC packs them.
  _oc = Statement(); _oc.operation = "LH"; _oc.assembled = bytearray(b"\x9a\xf7")
  _ocdc = Statement(); _ocdc.operation = "DC"
  _ocdc.assembled = bytearray(b"\x00\x01\x00\x02")
  check("statement_objcode_hex",
        _oc.objcode_hex() == " 9AF7" and _ocdc.objcode_hex() == " 00010002"
        and Statement().objcode_hex() == "",
        f"objcode_hex -> {_oc.objcode_hex()!r} / {_ocdc.objcode_hex()!r}")
  # listing_address(): EQU->symbol value, USING->base, LTORG->blank, else loc.
  _sym = {"SYM": SymtabEntry(type="EQU", value=5)}
  _ea = Statement(); _ea.operation = "EQU"; _ea.name = "SYM"
  _ua = Statement(); _ua.operation = "USING"; _ua.using = 0x100
  _la = Statement(); _la.operation = "LTORG"
  from ap101Utils.addr import Addr
  _pa = Statement(); _pa.pos1 = Addr(0x10)      # halfword 8 at offset 0
  _id = lambda v: v
  check("statement_listing_address",
        _ea.listing_address(_sym, 0, _id) == "0000005"
        and _ua.listing_address(_sym, 0, _id) == "0000100"
        and _la.listing_address(_sym, 0, _id) == ""
        and _pa.listing_address(_sym, 0, _id) == "00008"
        and Statement().listing_address(_sym, 0, _id) == "",   # pos1 None
        "listing_address EQU/USING/LTORG/loc forms")
  # listing_prefix(): address + objcode, plus adr1/adr2 columns when present.
  _lp = Statement(); _lp.pos1 = Addr(0x10); _lp.assembled = bytearray(b"\x9a\xf7")
  check("statement_listing_prefix_base",
        _lp.listing_prefix(_sym, 0, _id) == "00008 9AF7",
        f"base prefix -> {_lp.listing_prefix(_sym, 0, _id)!r}")
  _lp.adr1 = 0xE; _lp.adr2 = 0xB
  _full = _lp.listing_prefix(_sym, 0, _id)
  check("statement_listing_prefix_adr",
        _full.startswith("00008 9AF7") and "000E" in _full
        and _full.endswith("000B"),
        f"prefix with adr1/adr2 -> {_full!r}")

  # --- L' length attribute of an EQU-defined symbol ---
  # POS recovers a display coordinate as "&L SETA L'&#-1025" where &# is a
  # SETC holding the symbol's name; the length attribute comes from a
  # 3-operand EQU captured during macro expansion (PDEF: &N.X EQU x,x+1025).
  # Pin the recovered coordinate, not just absence-of-error: a plausible-but-
  # wrong length (e.g. returning the count of "P11X" = 4) must be caught.
  saved = _exprmod.asmContext.symbolAttributes
  try:
    _exprmod.asmContext.symbolAttributes = {"P11X": {"length": 740}}
    def larith(text, sv):
      return evalArithmeticExpression(
          parse(text, "arith_only"), scope(sv), props())
    check("lprime_bare_symbol", larith("L'P11X", {}) == 740,
          f"L'P11X should be 740")
    check("lprime_via_setc_var", larith("L'&#", {"&#": "P11X"}) == 740,
          "L'&# (SETC='P11X') should resolve the symbol's length")
    check("lprime_coordinate_recovery", larith("L'&#-1025", {"&#": "P11X"}) == -285,
          "POS coordinate L'P11X-1025 should be -285")
    check("lprime_unknown_symbol_errors",
          larith("L'&#", {"&#": "NOPE"}) is None,
          "L' of an unknown symbol must error (return None), not guess")
  finally:
    _exprmod.asmContext.symbolAttributes = saved

  # --- boolean OR/AND chains of 3+ terms evaluate (not just pairs) ---
  def boolean(text):
    return evalBooleanExpression(
        parse(text, "bool_only"), scope(), props())
  check("bool_or_chain_all_false",
        boolean("('0' EQ '1') OR ('0' EQ 'ON') OR ('0' EQ 'YES')") is False)
  check("bool_or_chain_one_true",
        boolean("('1' EQ '1') OR ('0' EQ 'ON') OR ('0' EQ 'YES')") is True)
  check("bool_and_chain",
        boolean("('0' EQ '0') AND ('1' EQ '1') AND ('2' EQ '2')") is True)

  # --- macro-invocation sublist with a SUBSCRIPTED element, e.g.
  # `(CIST,DEC(BASEREG),EQ,C'WO')`.  The grammar's `listItem` is only
  # `'(' list ')' | /[^ ,()]*/`; the bare regex stops at the '(' of
  # DEC(BASEREG) and orphaned the subscript, so the whole sublist failed to
  # parse and returned [''] -- which made the IF macro's CIST/compare form
  # (and any macro fed a `(...,X(Y),...)` sublist) drop its operands and emit
  # an INVALID CONDITION MNEMONIC.  `_listItem` now absorbs a balanced
  # parenthesized subscript into the literal token. ---
  def inv(text):
    r = parse(text, "oinv")
    return r["pi"] if isinstance(r, dict) and "pi" in r else r
  # A subscripted bare item (B(C)) is absorbed as ONE element, not split; a
  # nested SUBLIST element stays a sublist and renders to "(B,C)", not flattened
  # or absorbed as text -- checked via the rendered macro-arg value.
  check("sublist_subscripted_element",
        inv("(A,B(C),D)")[0].macroArg() == (None, ("A", "B(C)", "D")),
        f"got {inv('(A,B(C),D)')[0].macroArg()!r}")
  check("sublist_subscripted_element_cist",
        inv("(CIST,DEC(BASEREG),EQ,C'WO')")[0].macroArg()
        == (None, ("CIST", "DEC(BASEREG)", "EQ", "C'WO'")),
        f"got {inv(chr(40)+'CIST,DEC(BASEREG),EQ,C'+chr(39)+'WO'+chr(39)+chr(41))[0].macroArg()!r}")
  check("sublist_nested_sublist_unaffected",
        inv("(A,(B,C),D)")[0].macroArg() == (None, ("A", "(B,C)", "D")),
        f"got {inv('(A,(B,C),D)')[0].macroArg()!r}")

  # --- macro-arg VALUE conversion (MacroArg.macroArg()).  An argument's value
  # is a string (scalar) or a tuple of strings (sublist); a nested sublist
  # element renders to its source-like "(e1,...)" so &P / &P(k) and any re-parse
  # round-trip.  (The DOTESTS regression: DO FROM=((R3),(R5)) must reach DOPROC
  # as &FROM=("(R3)","(R5)"), not an empty operand.)  Flat elements pass
  # through unchanged. ---
  def macarg(text):
    return parse(text, "oinv")["pi"][0].macroArg()
  check("macarg_flat_sublist_unchanged",
        macarg("FROM=(R3,5)") == ("&FROM", ("R3", "5")),
        f"got {macarg('FROM=(R3,5)')!r}")
  check("macarg_nested_keyword_sublist_rendered",
        macarg("FROM=((R3),(R5))") == ("&FROM", ("(R3)", "(R5)")),
        f"got {macarg('FROM=((R3),(R5))')!r}")
  check("macarg_nested_positional_sublist_rendered",
        macarg("((R3),(R5))") == (None, ("(R3)", "(R5)")),
        f"got {macarg('((R3),(R5))')!r}")
  check("macarg_deeply_nested_sublist_rendered",
        macarg("FROM=(((A),(B)),C)") == ("&FROM", ("((A),(B))", "C")),
        f"got {macarg('FROM=(((A),(B)),C)')!r}")

  # --- a register field may be a PARENTHESIZED expression, e.g. (R0)/(R12).
  # IBM's machine-operator processor (IEUF8M) routes every register field
  # through the general expression evaluator (IEUF8V), which handles
  # parenthesized grouping, so `(R0)` evaluates to the register number.  The
  # structured-macro decks emit this via EXITIF (TRB,(R0),X'2411',NZ) -> STKINS
  # -> PUSHINS -> `TRB (R0),...`.  Bare registers are tried first, so ordinary
  # instructions are unchanged. ---
  _ri_paren = parse("(R0),X'2411'", "ri")
  _ri_r2 = None if _ri_paren is None else _ri_paren.get("R2")
  check("riAll_parenthesized_register",
        # fastparse keeps the explicit grouping node; the Lark front-end folds
        # `(R0)` to the inner register expression -- both denote register R0,
        # and the behavioral equivalence is pinned by
        # paren_register_byte_identical_to_bare below.
        _ri_r2 == [("(", (("R0", []), []), ")")]   # fastparse shape
        or _ri_r2 == _lk.Sym("R0"),                # clean Lark shape
        f"got R2={_ri_r2!r}")
  check("rrAll_bare_register_unchanged",
        parse("R2,R3", "rr") is not None)

  # --- macro-local scoping: a global array declared in one macro must NOT
  # leak into another macro that uses the same name as an undeclared local
  # scalar (the POS `&L` vs MACSMITH `GBLC &L(264)` bug).  A SETA to an
  # undeclared scalar auto-declares it local even when a same-named global
  # array exists. ---
  saved = dict(_exprmod.svGlobals)
  try:
    scopeA = scope()                                # a macro's local scope
    svDeclare("GBLC", "&ZZ(8)", scopeA)        # global array &ZZ
    check("global_array_declared",
          isinstance(_exprmod.svGlobals.get("&ZZ").value, list))
    scopeB = scope()                                # a different macro's locals
    p = props()
    svSet("SETA", "&ZZ", "5", scopeB, p)       # undeclared scalar use
    check("undeclared_scalar_shadows_global_array",
          scopeB.get("&ZZ").value == 5 and not p.errors,
          f"&ZZ(local)={scopeB.get('&ZZ')!r} errors={p.errors}")
    check("global_array_unmodified",
          isinstance(_exprmod.svGlobals.get("&ZZ").value, list),
          "the leaked global array must be untouched by the local SETA")
    # A macro that DID declare the global still writes through to it.
    scopeC = scope()
    svDeclare("GBLA", "&YY", scopeC)
    svSet("SETA", "&YY", "7", scopeC, props())
    check("declared_global_writes_through",
          _exprmod.svGlobals.get("&YY").value == 7,
          f"&YY(global)={_exprmod.svGlobals.get('&YY')!r}")
    # Two GBLC array declarations of the same name differing only in
    # DIMENSION (MACSMITH GBLC &T(264) vs FAZ2MAC GBLC &T(250)) are a
    # compatible re-declaration, not a "change of type"; the shared array
    # grows to the larger bound regardless of load order.
    sDscope = scope()                               # macro local scope (svLocals)
    sDstmt = props()                           # error sink (stmt)
    svDeclare("GBLC", "&WW(250)", sDscope, sDstmt)
    svDeclare("GBLC", "&WW(264)", sDscope, sDstmt)
    check("array_redeclare_compatible_dims",
          not sDstmt.errors
          and len(_exprmod.svGlobals.get("&WW", []).value) == 264,
          f"errors={sDstmt.errors} len={len(_exprmod.svGlobals.get('&WW', []).value)}")
  finally:
    _exprmod.svGlobals.clear()
    _exprmod.svGlobals.update(saved)


# =====================================================================
# INTEGRATION: assemble checked-in fixtures via the module CLI
# =====================================================================
def assemble(args, timeout=60):
  """Run the asm101 CLI; return (returncode, stdout+stderr)."""
  env = os.environ | {"PYTHONUTF8": "1"}
  r = subprocess.run(
      [sys.executable, "-m", "asm101", *args],
      capture_output=True, text=True, env=env, timeout=timeout,
  )
  return r.returncode, (r.stdout + r.stderr)


def integration_tests():
  with tempfile.TemporaryDirectory(prefix="asm101_feat_") as td:
    td = Path(td)

    def asm(fixture, extra=()):
      obj = td / (Path(fixture).stem + ".obj")
      return assemble(["-o", str(obj), *extra, str(FIX / fixture)])

    # Each of these used to crash (uncaught exception -> non-zero with a
    # traceback) before the corresponding fix; a clean exit proves the fix.
    for fx, label in [
        ("feat_dc_dup.asm", "dc_duplication_factor"),
        ("feat_dc_y_x.asm", "dc_y_and_x_suboperands"),
        ("feat_bare_drop.asm", "bare_drop"),
        ("feat_rs_empty_index.asm", "rs_omitted_index"),
        ("feat_equ_multi.asm", "equ_value_length_type"),
        ("feat_length_attr.asm", "lprime_length_attribute"),
        ("feat_inst_literals.asm", "inst_literals_h_f_e_y"),
        # An EQU ahead of the first CSECT used to raise KeyError(None) with
        # no current section: an absolute equate (a register equate such as
        # `R6 EQU 6`) crashed in currentHash(), and an `EQU *` crashed in the
        # secondary-section rebase.  Both now resolve with the location
        # counter taken as 0.
        ("equ_outside_csect_crashes.asm", "equ_before_first_csect"),
        ("lhi_with_equ_outside_csect_crash.asm", "lhi_equ_before_first_csect"),
        ("equ_star_before_first_csect.asm", "equ_star_before_first_csect"),
    ]:
      rc, out = asm(fx)
      check(label, rc == 0, f"rc={rc}\n{out.strip()[-400:]}")

    # --march gates the AP-101S-only instructions (LXA/LXAR, STXA/STXAR,
    # LDM, STDM, DIAG, CED/CEDR -- see instrdefs.AP101S_ONLY for provenance).
    # The default target (ap101s) accepts them; --march ap101b must reject
    # each with a diagnostic, not a traceback, and must not disturb ops
    # common to both machines.
    rc, out = asm("feat_march_s_only.asm")
    check("march_default_accepts_s_only", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    rc, out = asm("feat_march_s_only.asm", extra=["--march", "ap101b"])
    check("march_ap101b_rejects_s_only",
          rc != 0 and "Traceback" not in out
          and "AP-101S-only" in out,
          f"rc={rc}\n{out.strip()[-400:]}")
    rc, out = asm("feat_dc_dup.asm", extra=["--march", "ap101b"])
    check("march_ap101b_accepts_common_ops", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")

    # A section-scoped pseudo-op (ORG, LTORG) ahead of the first CSECT has
    # no control section to act on.  A *labeled* one used to crash in
    # commonProcessing (KeyError on the label, registering it in a section
    # absent from symtab); both must now be a clean diagnostic (intolerable
    # error, non-zero exit) -- NOT a traceback.
    for fx, msg in [
        ("org_before_first_csect.asm", "ORG outside any control section"),
        ("ltorg_before_first_csect.asm", "LTORG outside any control section"),
    ]:
      rc, out = asm(fx)
      check(f"{Path(fx).stem}_diagnosed",
            rc != 0 and "Traceback" not in out and msg in out,
            f"rc={rc}\n{out.strip()[-400:]}")

    # Forward =Y(label) whose value shifts pass-to-pass (RS->SRS condense).
    # Whole-dict pool matching flagged it "Literal has changed value" every
    # pass and never converged; literalIndex keys on the operand and the
    # final-pass write refreshes the slot, so it converges to LATE=hw4.
    ylst = td / "ychurn.lst"
    rc, out = assemble(
        ["-o", str(td / "ychurn.obj"), "-l", str(ylst),
         str(FIX / "feat_y_literal_churn.asm")])
    check("y_literal_churn_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("y_literal_churn_no_change_error",
          "changed value" not in out and "did not converge" not in out,
          f"forward =Y must converge without churn:\n{out.strip()[-400:]}")
    check("y_literal_churn_late_resolves", _xref_value(ylst, "LATE") == 4,
          f"LATE at halfword {_xref_value(ylst, 'LATE')}, expected 4")
    # The literal-pool dump printed after each LTORG lists every pooled
    # literal at its layout address with its assembled bytes.  This was dead
    # for ages: it read a per-entry "offset" key that never existed (offsets
    # live in the parallel pool.offsets list), so the block emitted nothing.
    # Now wired to pool.offsets; =Y(LATE) resolves to halfword 4 -> bytes 0004.
    _paddr, _pbytes = _pool_literal(ylst, "=Y(LATE)")
    check("ltorg_dump_emits_literal", _pbytes == "0004",
          f"=Y(LATE) pool dump -> bytes {_pbytes!r} at {_paddr!r}, want 0004")

    # USING with a forward-referenced base symbol: the collect-pass snapshot
    # holds the pre-pass 'preliminary' estimate (4 bytes per labeled
    # statement), which disagrees with the settled layout whenever unlabeled
    # statements precede the base.  optimizeScratch must re-resolve the base
    # (refreshUsing) or the displacement computes negative and the
    # instruction wrongly stays RS -- RUNASM SQRT's 'USING A,R1'/'A R6,A'
    # assembled 06F1 0000 where the flight assembler emits SRS 0601.
    ulst = td / "ufwd.lst"
    rc, out = assemble(
        ["-o", str(td / "ufwd.obj"), "-l", str(ulst),
         str(FIX / "feat_using_fwd_srs.asm")])
    check("using_fwd_srs_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _utxt = ulst.read_text()
    check("using_fwd_srs_condenses",
          re.search(r"^0000[0-9A-F] 0601 .*A     R6,AA", _utxt, re.M)
          is not None,
          "'A R6,AA' under forward USING must condense to SRS 0601:\n"
          + "\n".join(l for l in _utxt.splitlines() if "R6,AA" in l))

    # The OTHER half of that pair: 'BALR Rn,0' / 'USING *,Rn' establishes the
    # base from the LOCATION COUNTER, not from a symbol, so re-resolving the
    # snapshot against the symbol table is not enough -- the '*' itself has to
    # be re-resolved against the settled layout.  Ambiguous statements ahead of
    # the USING are sized long by the collect pass, so its '*' is high by the
    # whole not-yet-condensed excess; leave it stale and every displacement
    # under that base computes negative, so NOTHING over it condenses and each
    # reference stays an RS long form one halfword too big.
    slst = td / "ustar.lst"
    rc, out = assemble(
        ["-o", str(td / "ustar.obj"), "-l", str(slst),
         str(FIX / "feat_using_star_srs.asm")])
    check("using_star_srs_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _stxt = slst.read_text()
    check("using_star_srs_condenses",
          re.search(r"^0000C 3404 .*ST    R4,WORD1", _stxt, re.M) is not None,
          "'ST R4,WORD1' over a 'USING *,R0' base must condense to SRS 3404 "
          "(D2 counted in fullwords), not RS 34F0 0002:\n"
          + "\n".join(l for l in _stxt.splitlines() if "R4,WORD1" in l))
    check("using_star_srs_second_ref",
          re.search(r"^0000D 1E08 .*L     R6,WORD2", _stxt, re.M) is not None,
          "'L R6,WORD2' must condense to SRS 1E08 at the settled address:\n"
          + "\n".join(l for l in _stxt.splitlines() if "R6,WORD2" in l))

    # The SRS memory window stops at D=54, one short of the encodable 55 and
    # two short of the 56 at which D turns into the register-designated shift
    # count: the original assembler never spent the D field's last two values
    # on a memory reference, and a displacement of exactly 55 halfwords is
    # emitted long.  This is a SIZE rule, so the check is on the span as well
    # as on the bytes: 54 condenses to one halfword, 55 stays two.
    mclst = td / "srsmc.lst"
    rc, out = assemble(
        ["-o", str(td / "srsmc.obj"), "-l", str(mclst),
         str(FIX / "feat_srs_mem_ceiling.asm")])
    check("srs_mem_ceiling_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _mctxt = mclst.read_text()
    check("srs_mem_ceiling_54_condenses",
          re.search(r"^00000 BAD9\s.*STH   R2,AT54", _mctxt, re.M) is not None,
          "D=54 must condense to the SRS form BAD9:\n"
          + "\n".join(l for l in _mctxt.splitlines() if "R2,AT54" in l))
    check("srs_mem_ceiling_55_stays_long",
          re.search(r"^00001 BAF1 0037\s.*STH   R2,AT55", _mctxt, re.M)
          is not None,
          "D=55 is past the memory window and must stay RS long BAF1 0037:\n"
          + "\n".join(l for l in _mctxt.splitlines() if "R2,AT55" in l))
    check("srs_mem_ceiling_span", _xref_value(mclst, "SPAN") == 3,
          f"the two stores must occupy 1 + 2 halfwords, so SPAN lands at "
          f"halfword 3, not {_xref_value(mclst, 'SPAN')}")

    # An explicitly-coded base register with an expression displacement must
    # encode RS AM=1, matching the flight CASEN computed-goto idiom
    # 'LH R2,#@LBn-*-3(,R2)' -> 9AF6 (every OI30-listing instance; the
    # USING-resolved-base AM=0 rule must NOT swallow it -- it used to emit
    # 9AF2, caught by the DASS/OI30-listing fidelity comparison).
    eblst = td / "eb.lst"
    rc, out = assemble(
        ["-o", str(td / "eb.obj"), "-l", str(eblst),
         str(FIX / "feat_explicit_base_am1.asm")])
    check("explicit_base_am1_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _ebtxt = eblst.read_text()
    check("explicit_base_am1_encodes_9AF6",
          re.search(r"^00000 9AF6 .*LH    R2,TBL", _ebtxt, re.M) is not None,
          "'LH R2,expr(,R2)' must encode AM=1 9AF6:\n"
          + "\n".join(l for l in _ebtxt.splitlines() if "LH" in l))
    # ... and an IN-SRS-RANGE expression displacement must STILL be RS AM=1,
    # not the 1-hw SRS form (flight: FCMSVC 99F5 000C, FIOPDISP+1A7
    # 9AF6 000A -- SRS condensation of explicit-base displacements is for
    # number displacements only).
    check("explicit_base_am1_no_srs_condense",
          re.search(r"^00002 9AF6 0003 .*LH    R2,NEAR", _ebtxt, re.M)
          is not None,
          "in-SRS-range 'LH R2,expr(,R2)' must stay RS AM=1:\n"
          + "\n".join(l for l in _ebtxt.splitlines() if "LH" in l))

    # R3 coded as an explicit base carries the ABSOLUTE displacement (flight
    # FCMISYNC 'L R4,TPSAE2OP-TPSASTRT(R3)' -> 1CF3 0088); the
    # b2==3-as-PC-sentinel conflation used to PC-relativize it (1CF7).
    r3lst = td / "r3b.lst"
    rc, out = assemble(
        ["-o", str(td / "r3b.obj"), "-l", str(r3lst),
         str(FIX / "feat_r3_base_absolute.asm")])
    check("r3_base_absolute_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    check("r3_base_absolute_encodes_1CF3_0088",
          re.search(r"^00000 1CF3 0088 ", r3lst.read_text(), re.M) is not None,
          "'L R4,expr(R3)' must encode AM=0 with the absolute value:\n"
          + "\n".join(l for l in r3lst.read_text().splitlines() if " L " in l))
    check("la_r3_base_encodes_E9F3_00B0",
          re.search(r"^00002 E9F3 00B0 ", r3lst.read_text(), re.M) is not None,
          "'LA R1,expr(R3)' must take the AM=0 based form (flight "
          "FPMIHPC2+157 E9F3 00B0):\n"
          + "\n".join(l for l in r3lst.read_text().splitlines() if "LA" in l))

    # A $-forced branch to a label strictly inside the csect encodes RS AM=0
    # with an absolute target -- the 16-bit field MUST carry a Y-type RLD
    # naming the csect (flight links FCMISYNC 'BNZ$ FCMNOIOS' as C3F3 8EFE =
    # base+offset; our linked images branched to the unrelocated offset).
    # The end-of-csect label case rode a different, always-relocated path.
    dbrobj = td / "dbr.obj"
    rc, out = assemble(
        ["-o", str(dbrobj), str(FIX / "feat_dollar_branch_rld.asm")])
    check("dollar_branch_rld_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _rlds = []
    _data = dbrobj.read_bytes()
    for _i in range(0, len(_data), 80):
        _card = _data[_i:_i + 80]
        if _card[1:4].decode("cp037", errors="replace") == "RLD":
            _n = int.from_bytes(_card[10:12], "big")
            _b = _card[16:16 + _n]
            _rlds += [(int.from_bytes(_b[j:j+2], "big"),
                       int.from_bytes(_b[j+5:j+8], "big"))
                      for j in range(0, _n, 8)]
    check("dollar_branch_rld_emitted", (1, 2) in _rlds,
          f"B$ INSIDE displacement (byte 2) must be relocated by ESD 1; "
          f"RLDs={_rlds}")

    # An EXPLICITLY CODED R3 is 'no base' too, so the AM=0 field is an
    # ABSOLUTE ADDRESS and its relocatable displacement must carry a Y RLD
    # naming the csect that CONTAINS THE TARGET.  Unrelocated it keeps the
    # assembler's own contiguous-layout address and never learns where the
    # linker put that csect: the reference then lands short by whatever gap
    # the linker leaves between the two sections.
    r3robj = td / "r3reloc.obj"
    rc, out = assemble(
        ["-o", str(r3robj), str(FIX / "feat_r3_reloc_other_csect.asm")])
    check("r3_reloc_other_csect_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _esd, _rr = {}, []
    _data = r3robj.read_bytes()
    for _i in range(0, len(_data), 80):
        _card = _data[_i:_i + 80]
        _kind = _card[1:4].decode("cp037", errors="replace")
        _n = int.from_bytes(_card[10:12], "big")
        if _kind == "ESD":
            _sid = int.from_bytes(_card[14:16], "big")
            for _k in range(_n // 16):
                _esd[_sid + _k] = _card[16 + 16*_k:24 + 16*_k] \
                    .decode("cp037", errors="replace").strip()
        elif _kind == "RLD":
            _b = _card[16:16 + _n]
            _rr += [(_esd.get(int.from_bytes(_b[j:j+2], "big")),
                     int.from_bytes(_b[j+5:j+8], "big"))
                    for j in range(0, _n, 8)]
    check("r3_reloc_names_target_csect", ("TWO", 2) in _rr,
          f"'LA$ 1,TABLE(Z3)' must relocate byte 2 against TWO, the csect "
          f"that contains TABLE; RLDs={_rr}")
    check("r3_reloc_same_csect", ("ONE", 6) in _rr,
          f"'LA$ 2,LOCAL(Z3)' must relocate byte 6 against its own csect; "
          f"RLDs={_rr}")
    check("r3_reloc_real_base_exempt", len(_rr) == 2,
          f"'LA 4,LOCAL(0)' is over a REAL base register and must NOT be "
          f"relocated; RLDs={_rr}")

    # A self-relative RS target at ZERO displacement takes the subtractive
    # I-bit form like backward ones: 'BAL R3,*+2' = E3F7 0800 (i=1, d=0),
    # never the additive E3F7 0000 (FPMRES+30, 3/3 OI30 instances).
    srzlst = td / "srz.lst"
    rc, out = assemble(
        ["-o", str(td / "srz.obj"), "-l", str(srzlst),
         str(FIX / "feat_self_rel_zero_ibit.asm")])
    check("self_rel_zero_ibit_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    check("self_rel_zero_ibit_encodes_0800",
          re.search(r"^00000 E3F7 0800 ", srzlst.read_text(), re.M)
          is not None,
          "'BAL R3,*+2' must encode the subtractive form E3F7 0800:\n"
          + "\n".join(l for l in srzlst.read_text().splitlines()
                      if "BAL" in l))
    # A real R3 base with an index (not self-relative) keeps zero
    # displacement ADDITIVE: RUNLST ITOC 'STH R4,0(R5,3)' = BCF7 A000,
    # not the subtractive A800 (the fold must gate on selfRelB2).
    check("zero_disp_indexed_base_stays_additive",
          re.search(r"^00003 BCF7 A000 ", srzlst.read_text(), re.M)
          is not None,
          "'STH R4,0(R5,3)' must stay additive BCF7 A000:\n"
          + "\n".join(l for l in srzlst.read_text().splitlines()
                      if "STH" in l))

    # The comma form 'expr(,Rn)' on an @/# op names the BASE (index slot
    # explicitly empty); the atStar bare-paren heuristic must not move it
    # into X2 (flight FPMDISP 'STDM@ 0,...(,R3)' = 90FF 10B3, ours 70B3).
    atclst = td / "atc.lst"
    rc, out = assemble(
        ["-o", str(td / "atc.obj"), "-l", str(atclst),
         str(FIX / "feat_at_comma_base.asm")])
    check("at_comma_base_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _atctxt = atclst.read_text()
    check("at_comma_base_stdm",
          re.search(r"^00000 90FF 10B3 ", _atctxt, re.M) is not None,
          "'STDM@ 0,expr(,R3)' must keep R3 as base (x2=0):\n"
          + "\n".join(l for l in _atctxt.splitlines() if "STDM@" in l))
    check("at_comma_base_lps",
          re.search(r"^00002 CDFF 10B2 ", _atctxt, re.M) is not None,
          "'LPS@ expr(,R3)' must keep R3 as base (x2=0):\n"
          + "\n".join(l for l in _atctxt.splitlines() if "LPS@" in l))

    # An F/H constant with a decimal exponent is an INTEGER (mantissa x
    # 10^exp) -- the fraction path clamped FCMCBLKS 'DC F'900E6'' to
    # 7FFFFFFF (IBM OI30: 35A4E900); pure fractions keep the scaled path.
    felst = td / "fe.lst"
    rc, out = assemble(
        ["-o", str(td / "fe.obj"), "-l", str(felst),
         str(FIX / "feat_dc_f_exponent.asm")])
    check("dc_f_exponent_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _fetxt = felst.read_text()
    for _pat, _label in [
        (r"^00000 35A4E900 ", "dc_f_exponent_900e6"),
        (r"^00002 6B49D200 ", "dc_f_exponent_1800e6"),
        (r"^00004 07D0 ", "dc_h_exponent_2e3"),
        (r"^00006 FFFFFA24 ", "dc_f_exponent_negative"),
        (r"^00008 40000000 ", "dc_f_fraction_path_kept"),
    ]:
      check(_label, re.search(_pat, _fetxt, re.M) is not None,
            "F/H decimal-exponent values:\n"
            + "\n".join(l for l in _fetxt.splitlines() if " DC " in l))

    # A multi-suboperand DC must emit each suboperand once: the DC buffer
    # reset per STATEMENT + handlers emitting dcBuffer[:dcBufferPtr] laid
    # down Y,Y,H for 'DC Y(X),H'130'' (FCMCBLKS FCMHTABL +6 hw) and skewed
    # the RLD position of an adcon following another suboperand.
    dcmobj = td / "dcm.obj"
    rc, out = assemble(
        ["-o", str(dcmobj), str(FIX / "feat_dc_multi_suboperand.asm")])
    check("dc_multi_suboperand_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _dtxt = b""
    _drlds = []
    _ddata = dcmobj.read_bytes()
    for _i in range(0, len(_ddata), 80):
        _card = _ddata[_i:_i + 80]
        _kind = _card[1:4].decode("cp037", errors="replace")
        if _kind == "TXT":
            _dtxt += _card[16:16 + int.from_bytes(_card[10:12], "big")]
        elif _kind == "RLD":
            _n = int.from_bytes(_card[10:12], "big")
            _b = _card[16:16 + _n]
            _drlds += [(_b[j + 4], int.from_bytes(_b[j+5:j+8], "big"))
                       for j in range(0, _n, 8)]
    check("dc_multi_suboperand_bytes",
          _dtxt.hex() == "00010082000000020007000000010005",
          f"Y,H | A,H | Y | H,Y must emit once each; TXT={_dtxt.hex()}")
    check("dc_multi_suboperand_rld_pos", (0x00, 0x0E) in _drlds,
          f"Y(LOC) after H'1' relocates at byte 0xE; RLDs={_drlds}")

    # A comma-separated nominal-value list in ONE F/H constant emits one field
    # per value (as E/D already did).  Emitting only the first value left flight
    # DCI#DATA's `DC H'0,1,160,37,0,0,0'` (CLOCMSGL) 6 hw short, so #DDCICYC was
    # 670 not 676 and the whole bank-0 tail cascaded off by -6.
    mvlst = td / "mv.lst"
    rc, out = assemble(
        ["-o", str(td / "mv.obj"), "-l", str(mvlst),
         str(FIX / "feat_dc_multivalue.asm")])
    check("dc_multivalue_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("dc_multivalue_h_list_7hw", _xref_value(mvlst, "HAFTER") == 7,
          f"H'0,1,160,37,0,0,0' -> 7 hw; HAFTER at "
          f"{_xref_value(mvlst, 'HAFTER')}, expected 7")
    check("dc_multivalue_f_list_3fw", _xref_value(mvlst, "FAFTER") == 14,
          f"F'1,2,3' -> 3 fullwords after align; FAFTER at "
          f"{_xref_value(mvlst, 'FAFTER')}, expected 14")
    # Byte-exact (catches a wrong-VALUE variant, not just a wrong count): the
    # H list's 7 halfwords are 0,1,160,37,0,0,0.
    _mvtxt = b""
    for _i in range(0, len(_mv := (td / "mv.obj").read_bytes()), 80):
        _c = _mv[_i:_i + 80]
        if _c[1:4].decode("cp037", errors="replace") == "TXT":
            _mvtxt += _c[16:16 + int.from_bytes(_c[10:12], "big")]
    check("dc_multivalue_h_bytes",
          _mvtxt[:14].hex() == "0000000100a00025000000000000",
          f"H'0,1,160,37,0,0,0' bytes = {_mvtxt[:14].hex()}")

    # An IOP long-format 'a' (18-bit) field spans both halfwords (a[17:16]
    # in hw1's low bits), so its reloc must be a fullword ACON (flag 0x1C)
    # over the whole instruction -- the old 2-byte Y RLD on hw2 dropped
    # address bit 16 at link (flight FIOCBLKS '#BU FIOBADFA' = F001 D91A,
    # target 1D91A; ours branched to 0D902).
    iopaobj = td / "iopa.obj"
    rc, out = assemble(
        ["-o", str(iopaobj), str(FIX / "feat_iop_a_field_acon.asm")])
    check("iop_a_field_acon_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _iorlds = []
    _iodata = iopaobj.read_bytes()
    for _i in range(0, len(_iodata), 80):
        _card = _iodata[_i:_i + 80]
        if _card[1:4].decode("cp037", errors="replace") == "RLD":
            _n = int.from_bytes(_card[10:12], "big")
            _b = _card[16:16 + _n]
            _iorlds += [(_b[j + 4], int.from_bytes(_b[j+5:j+8], "big"))
                        for j in range(0, _n, 8)]
    check("iop_a_field_acon_rld", (0x1C, 2) in _iorlds,
          f"IOP 'a' field needs a fullword ACON RLD (flag 0x1C) at the "
          f"instruction start (byte 2); RLDs={_iorlds}")
    # ... and #RDL's 18-bit address is an 'a' field too -- its descriptor
    # mislabel ('c') dropped the RLD entirely (FIODEUPG '#RDL FIOWCE').
    check("iop_rdl_address_rld", (0x1C, 6) in _iorlds,
          f"#RDL's address field must relocate like #TDL's; RLDs={_iorlds}")

    # N' of a nested-sublist argument counts the sublist's elements: a nested
    # sublist subscripted out of &SYSLIST re-renders to its "(a,b,...)" source
    # text, and N' used to answer 1 for it -- MLIB80 TFBCD's .BCELOOP then set
    # only the first bus bit of every multi-bus FIOBCD mask (flight/OI30
    # TBCD0079 = 00000E00 vs our 00000800, FIOCBLKS fidelity trace).
    nplst = td / "np.lst"
    rc, out = assemble(
        ["-o", str(td / "np.obj"), "-l", str(nplst),
         str(FIX / "feat_nprime_nested_sublist.asm")])
    check("nprime_nested_sublist_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _nptxt = nplst.read_text()
    for _pat, _label in [
        (r"^00000 00000004 .*AL1\(4\)", "nprime_whole_arg_counts_4"),
        (r"^00002 00000003 .*AL1\(3\)", "nprime_nested_sublist_counts_3"),
        (r"^00004 00000001 .*AL1\(1\)", "nprime_scalar_element_counts_1"),
    ]:
      check(_label, re.search(_pat, _nptxt, re.M) is not None,
            "N' sublist counts (4,3,1) expected:\n"
            + "\n".join(l for l in _nptxt.splitlines() if "AL1" in l))

    # An AIF branch taken FROM a continued statement must not eat the
    # branch-target card: the pending continuation-card skip (skip_count and
    # the source[-2].continues guard both assume linear flow) silently
    # swallowed the labeled target -- MLIB80 STKINS '.SGLOPR GETCC &P1(1)',
    # reached from its two-card AIF, never ran, so every IF (cond) compiled
    # with a stale/zero &CCVAL mask (flight DC24 vs our DB24, FCMISYNC+A6).
    cbrlst = td / "cbr.lst"
    rc, out = assemble(
        ["-o", str(td / "cbr.obj"), "-l", str(cbrlst),
         str(FIX / "feat_branch_over_continuation.asm")])
    check("branch_over_continuation_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    check("branch_over_continuation_target_runs",
          "GG=[9]" in cbrlst.read_text(),
          "the branched-to SETTER must execute (GG=[9]):\n"
          + "\n".join(l for l in cbrlst.read_text().splitlines()
                      if "GG=" in l or "FALLTHROUGH" in l))

    # A bare paren register over a RELOCATABLE displacement is an INDEX
    # (base comes from the covering USING) for ANY register, R0-R3
    # included: flight FCMDSCRM 'LH R5,TDWASBT(R3)' under 'USING TFDWA,R0'
    # is 9DF4 6004 (AM=1, x2=R3, b=R0) where R3-as-base encoded 9DF3 0004
    # (wrong runtime EA; same-deck (R5)/(R7) already converted).  With no
    # covering USING the reference is current-section self-relative; the
    # $-forced AM=0 form has no index field, so the register is dropped
    # but the 16-bit displacement keeps its Y RLD (FCMTRACE 'BL$
    # FCMWRAP(R3)' = C2F3 001C, linked 98BC; R3-as-base suppressed it).
    pxlst = td / "px.lst"
    pxobj = td / "px.obj"
    rc, out = assemble(
        ["-o", str(pxobj), "-l", str(pxlst),
         str(FIX / "feat_paren_index_reloc.asm")])
    check("paren_index_reloc_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _pxtxt = pxlst.read_text()
    check("paren_index_reloc_lh_9DF4_6004",
          re.search(r"^00000 9DF4 6004 ", _pxtxt, re.M) is not None,
          "'LH R5,TDWASBT(R3)' under USING TFDWA,R0 must take the index "
          "form 9DF4 6004:\n"
          + "\n".join(l for l in _pxtxt.splitlines() if "LH " in l))
    check("paren_index_reloc_st_37F4_6004",
          re.search(r"^00002 37F4 6004 ", _pxtxt, re.M) is not None,
          "'ST R7,TDWASBT(R3)' under USING TFDWA,R0 must take the index "
          "form 37F4 6004 (FPMERLOG/FPMEVENQ analogue):\n"
          + "\n".join(l for l in _pxtxt.splitlines() if "ST " in l))
    check("paren_index_reloc_dollar_bytes",
          re.search(r"^00004 C2F3 0006 ", _pxtxt, re.M) is not None,
          "'BL$ TARG(R3)' (no USING) must keep the AM=0 self-relative "
          "bytes C2F3 0006:\n"
          + "\n".join(l for l in _pxtxt.splitlines() if "BL" in l))
    _pxrlds = _rld_entries(pxobj)
    check("paren_index_reloc_dollar_rld",
          any(e[0] == 1 and e[3] == 10 for e in _pxrlds),
          f"'BL$ TARG(R3)' displacement halfword (byte 10) must carry a "
          f"Y RLD against the csect (ESD 1); RLDs={_pxrlds!r}")
    check("paren_index_reloc_la_exempt",
          re.search(r"^00007 EDF3 0006 ", _pxtxt, re.M) is not None,
          "LA is exempt from the index conversion -- its paren register "
          "is the BASE (IBM BILDNEW5 'LA$ B1,STM4(Z3)' E9F3 14DA):\n"
          + "\n".join(l for l in _pxtxt.splitlines() if "LA " in l))

    # Forward branches condense only when the SELF-CONDENSED displacement
    # is under 54 (not the full 56-hw field): flight FCMBMAN+95 keeps
    # 'BC 07,#@LB27' RS at exactly 54 (C7F7 0036) while its 8 twins
    # condense; FPMOPSCN+C condenses at 53 (DCD4), FIOPDHF+13 at 48.
    stklst = td / "stk.lst"
    rc, out = assemble(
        ["-o", str(td / "stk.obj"), "-l", str(stklst),
         str(FIX / "feat_srs_estimate_sticky.asm")])
    check("srs_fwd54_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _stktxt = stklst.read_text()
    check("srs_fwd54_boundary_stays_rs",
          re.search(r"^00000 C7F7 0036 ", _stktxt, re.M) is not None,
          "first branch (self-condensed displacement exactly 54) must "
          "stay RS C7F7 0036 (flight FCMBMAN bytes):\n"
          + "\n".join(l for l in _stktxt.splitlines() if "BC " in l))
    check("srs_fwd54_under_condense",
          re.search(r"^00002 DF", _stktxt, re.M) is not None
          and re.search(r"^00009 DF", _stktxt, re.M) is not None,
          "the 8 later branches (under the window) condense:\n"
          + "\n".join(l for l in _stktxt.splitlines() if "BC " in l))

    # The @/# indirect form over a NUMERIC displacement keeps the bare
    # paren register as the BASE alone -- the bare-paren index swap must
    # not duplicate it into hw2's X2 field (flight FCMTRACE +0019
    # 'ST@# R4,0(R2)' = 34F6 1800, x2=0; ours emitted 34F6 5800).
    anblst = td / "anb.lst"
    rc, out = assemble(
        ["-o", str(td / "anb.obj"), "-l", str(anblst),
         str(FIX / "feat_atpound_numeric_base.asm")])
    check("atpound_numeric_base_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    check("atpound_numeric_base_encodes_34F6_1800",
          re.search(r"^00000 34F6 1800 ", anblst.read_text(), re.M)
          is not None,
          "'ST@# R4,0(R2)' must keep R2 as base only (x2=0):\n"
          + "\n".join(l for l in anblst.read_text().splitlines()
                      if "ST@#" in l))

    # DC E rounds at the 24-bit single-precision fraction -- not the
    # truncated msw of the 56-bit double conversion (flight FPMUPMTU
    # 'DC E'0.015'' = 3F3D70A4, ours 3F3D70A3; OI30 FCMLINIT FCM26P04
    # 'E'26.041667'' = 421A0AAB confirms).  D stays bit-identical; the
    # =E literal path shares the rule.
    erlst = td / "er.lst"
    rc, out = assemble(
        ["-o", str(td / "er.obj"), "-l", str(erlst),
         str(FIX / "feat_dc_e_round.asm")])
    check("dc_e_round_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for _label, _want, _desc in [
        ("E15", "3F3D70A4", "E'0.015' rounds up at 24 bits"),
        ("E26", "421A0AAB", "E'26.041667' rounds up at 24 bits"),
        ("D15", "3F3D70A3D70A3D71", "D'0.015' unchanged (56-bit round)"),
    ]:
      _got = _listing_code(erlst, _label)
      check(f"dc_e_round_{_label}", _got == _want, f"{_desc}: {_got!r}")
    _eaddr, _ebytes = _pool_literal(erlst, "=E'0.1'")
    check("dc_e_round_literal", _ebytes == "4019999A",
          f"=E'0.1' pool slot must round at 24 bits -> 4019999A; "
          f"got {_ebytes!r} at {_eaddr!r}")

    # Literal-pool mechanics (three flight-traced fixes): (a) a mid-csect
    # LTORG occupies its pool's size so following statements start past it
    # (IBM FIOLGERR: pool 0x86, trailing ZCONs 0x88/0x8A, csect 140 hw;
    # ours overlapped and sized 136); (b) the end-of-source pool origin is
    # FULLWORD-aligned (IBM FPMRES pool at hw 0x44, FPMWAIT at 0x56, one
    # gap halfword); (c) a relocatable =Y literal gets a pool-slot Y RLD
    # (FPMREL =Y(FPMXQELE) 0000+RLD; FIOERRLC =Y(FIOADBST+8) 0008+RLD).
    lplst = td / "lp.lst"
    lpobj = td / "lp.obj"
    rc, out = assemble(
        ["-o", str(lpobj), "-l", str(lplst),
         str(FIX / "feat_ltorg_pool.asm")])
    check("ltorg_pool_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _lptxt = lplst.read_text()
    check("ltorg_pool_advances_lc",
          re.search(r"^00004 00000000 .*ZC1      DC", _lptxt, re.M)
          is not None,
          "the DC after a 2-hw LTORG pool at hw 2 must land at hw 4:\n"
          + "\n".join(l for l in _lptxt.splitlines() if "ZC1" in l))
    check("ltorg_pool_csect_size",
          re.search(r"^LP        SD 0001 000000 00000E$", _lptxt, re.M)
          is not None,
          "csect must size 0xE hw (code 0xB + gap + 2-hw end pool):\n"
          + "\n".join(l for l in _lptxt.splitlines() if " SD " in l))
    check("ltorg_pool_end_pool_fullword",
          re.search(r"^00006 91F7 0004      000C ", _lptxt, re.M)
          is not None,
          "=Y(LOCLBL) must sit at hw 0xC (fullword origin, gap at 0xB):\n"
          + "\n".join(l for l in _lptxt.splitlines() if "91F7" in l))
    _lprlds = _rld_entries(lpobj)
    check("ltorg_pool_y_local_rld",
          (1, 1, 0x00, 24) in _lprlds,
          f"=Y(LOCLBL) pool slot (byte 0x18) needs a Y RLD against the "
          f"csect; RLDs={_lprlds!r}")
    check("ltorg_pool_y_extrn_rld",
          (2, 1, 0x00, 26) in _lprlds,
          f"=Y(EXTV+8) pool slot (byte 0x1A) needs a Y RLD against the "
          f"EXTRN; RLDs={_lprlds!r}")
    # ... slot images: local = its halfword address (000B), EXTRN = the
    # bare addend (0008).
    _lptxtb = b""
    _lpdata = lpobj.read_bytes()
    for _i in range(0, len(_lpdata), 80):
        _card = _lpdata[_i:_i + 80]
        if _card[1:4] == b"\xe3\xe7\xe3":      # EBCDIC 'TXT'
            _lptxtb += _card[16:16 + int.from_bytes(_card[10:12], "big")]
    check("ltorg_pool_slot_images",
          _lptxtb[24:28].hex() == "000b0008",
          f"pool slots at 0x18 must be 000B 0008; TXT={_lptxtb.hex()}")

    # sects[].used must not ratchet across compile passes: a pass that
    # converges SMALLER must not keep the previous pass's high-water mark
    # (flight FIOPDHF = 294 hw, ours sized 295 with a phantom trailing
    # 0000).  The fixture's first branch condenses RS->SRS only on the
    # pass AFTER the second one does, so the stale 'used' kept the longer
    # layout's size.
    nrlst = td / "nr.lst"
    rc, out = assemble(
        ["-o", str(td / "nr.obj"), "-l", str(nrlst),
         str(FIX / "feat_used_no_ratchet.asm")])
    check("used_no_ratchet_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    _nrtxt = nrlst.read_text()
    check("used_no_ratchet_size_54",
          re.search(r"^TC        SD 0001 000000 000036$", _nrtxt, re.M)
          is not None,
          "csect must size 0x36 hw (both branches SRS), not the stale "
          "0x37:\n" + "\n".join(l for l in _nrtxt.splitlines()
                                if " SD " in l))
    check("used_no_ratchet_first_branch_srs",
          re.search(r"^00000 DFD4 ", _nrtxt, re.M) is not None,
          "first branch must condense to SRS DFD4 (self-condensed "
          "displacement 53, inside the 54 forward window):\n"
          + "\n".join(l for l in _nrtxt.splitlines() if " B " in l))

    # CPU-csect alignment gaps fill with C9FB (the SVC opcode halfword),
    # covered by TXT -- RUNLST STBYTE '0000D C9FB' (DS 0F), CASV/IREM/
    # CPASP (LTORG), ITOC (DC F); DASS FPMRES +0043 'C9FB *** ALIGNMENT
    # GAP ***'.  CNOP keeps its own executable-NOP fill (D800/C000).
    gfobj = td / "gf.obj"
    rc, out = assemble(
        ["-o", str(gfobj), str(FIX / "feat_gap_fill.asm")])
    check("gap_fill_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    _gftxt = b""
    _gfdata = gfobj.read_bytes()
    for _i in range(0, len(_gfdata), 80):
        _card = _gfdata[_i:_i + 80]
        if _card[1:4] == b"\xe3\xe7\xe3":      # EBCDIC 'TXT'
            _gftxt += _card[16:16 + int.from_bytes(_card[10:12], "big")]
    check("gap_fill_c9fb",
          _gftxt.hex() ==
          "9af700020005c9fb00010007000bc9fb00000009000dd800c7e2",
          "LTORG gap (hw 3) and DC F gap (hw 7) must fill C9FB, the "
          f"CNOP gap must keep D800; TXT={_gftxt.hex()}")

    # DC A(symbol) address constant.  Before the fix the A-type DC branch
    # only handled the self-defining A'hex' form; the A(expr) form fell
    # through to 4 zero bytes with NO relocation, so an A(EXTRN)/A(label)
    # silently became an unlinkable zero.  (This is the rank-1 finding from
    # AS037F1_COMPARISON.md; it had quietly broken 26 adcons in BILDNEW5.)
    aobj = td / "adcon.obj"
    alst = td / "adcon.lst"
    rc, out = assemble(
        ["-o", str(aobj), "-l", str(alst), str(FIX / "feat_dc_a_adcon.asm")])
    check("dc_a_adcon_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    # A(THERE): THERE is at halfword 8, so the 4-byte adcon value is 0x08.
    check("dc_a_local_value", _listing_code(alst, "HERE") == "00000008",
          f"A(THERE) -> {_listing_code(alst, 'HERE')!r}, expected 00000008")
    # A(EXTSYM): external -> data is 0, value supplied by the RLD.
    check("dc_a_extern_value", _listing_code(alst, "EREF") == "00000000",
          f"A(EXTSYM) -> {_listing_code(alst, 'EREF')!r}, expected 00000000")
    # DC 2A'1F': dup factor (previously an infinite loop) -> two fullwords.
    check("dc_a_dup_no_hang",
          _listing_code(alst, "HEX") == "0000001F0000001F",
          f"2A'1F' -> {_listing_code(alst, 'HEX')!r}")
    # Two 4-byte (flag 0x1C) RLDs: A(THERE) vs the section (ESDID 1) and
    # A(EXTSYM) vs the external (ESDID 2); the self-defining HEX gets none.
    rld = _rld_entries(aobj)
    fourByte = [e for e in rld if e[2] == 0x1C]
    check("dc_a_emits_two_4byte_rlds", len(fourByte) == 2,
          f"4-byte RLDs={fourByte!r} (all={rld!r})")
    check("dc_a_extern_rld_targets_esdid2",
          any(e[0] == 2 and e[2] == 0x1C for e in rld),
          f"expected a 4-byte RLD against ESDID 2 (EXTSYM); got {rld!r}")

    # DC Z(...) / =Z(...) ZCON relocation subfields: 'Z(,sym...)' (code
    # subfield EMPTY) relocates the DATA address subfield -> ZCON/data RLD
    # (flag 0x50, linker patches DSR: flight FCMNINIT '=Z(,FPMXQETB+2,0)'
    # links 8B6C 0001); 'Z(sym...)' relocates the CODE subfield -> ZCON/code
    # (0x04) exactly as before.  The =Z literal's pool slot carries the same
    # data-subfield flag.
    zobj = td / "zcon.obj"
    rc, out = assemble(["-o", str(zobj), str(FIX / "feat_dc_z_zcon.asm")])
    check("dc_z_zcon_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    zrld = _rld_entries(zobj)
    check("dc_z_data_subfield_rld_0x50",
          any(e[0] == 2 and e[2] == 0x50 and e[3] == 0 for e in zrld),
          f"'Z(,EXTSYM,flags)' at byte 0 must punch ZCON/data (0x50) "
          f"against ESDID 2; got {zrld!r}")
    check("dc_z_code_subfield_rld_0x04",
          any(e[0] == 2 and e[2] == 0x04 and e[3] == 4 for e in zrld),
          f"'Z(EXTSYM,,flags)' at byte 4 must keep ZCON/code (0x04); "
          f"got {zrld!r}")
    check("dc_z_literal_data_subfield_rld_0x50",
          any(e[0] == 2 and e[2] == 0x50 and e[3] > 8 for e in zrld),
          f"the '=Z(,EXTSYM+2,0)' literal-pool slot must punch ZCON/data "
          f"(0x50); got {zrld!r}")

    # A relocatable addend on the 18-bit IOP 'a' field carries its SIGN in the
    # RLD, never in the field.  IBM's assembler writes the addend's MAGNITUDE
    # for both signs -- '#LBR@ SYM-2' and '#LBR@ SYM+2' assemble to the same
    # FA00 0002 -- and only the RLD's V bit (0x9C vs 0x1C) tells the linkage
    # editor to subtract.  asm101 used to two's-complement -2 into the 18-bit
    # field as 0x3FFFE, whose top two bits ARE the low two bits of the opcode
    # halfword: it emitted FA03 FFFE, and the link then carried the target
    # address across the halfword boundary and corrupted the opcode.
    iobj = td / "iopaddend.obj"
    ilst = td / "iopaddend.lst"
    rc, out = assemble(["-o", str(iobj), "-l", str(ilst),
                        str(FIX / "feat_iop_addr_addend.asm")])
    check("iop_addend_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    itxt = b""
    for _i in range(0, len(idata := iobj.read_bytes()), 80):
      _card = idata[_i:_i + 80]
      if _card[1:4] == b"\xe3\xe7\xe3":        # EBCDIC 'TXT'
        itxt += _card[16:16 + int.from_bytes(_card[10:12], "big")]
    # bytes 0x0/0x4 = the two NEGATIVE sites, 0x8/0xC = the two POSITIVE ones.
    check("iop_negative_addend_is_magnitude",
          itxt[0:8].hex() == "fa000002fd000002",
          f"'#LBR@ FA-2' / '#MOUT@ FB-2' must assemble FA00 0002 / "
          f"FD00 0002, not the two's complement FA03 FFFE; "
          f"TXT={itxt.hex()}")
    check("iop_positive_addend_unchanged",
          itxt[8:16].hex() == "fa000002fd000014",
          f"'#LBR@ FA+2' / '#MOUT@ FB+20' must stay FA00 0002 / "
          f"FD00 0014; TXT={itxt.hex()}")
    irld = _rld_entries(iobj)
    # ESDID 2 = FA, 3 = FB (ER ids follow the SD in source order).
    check("iop_negative_addend_rld_sets_sign_bit",
          sorted((e[0], e[2], e[3]) for e in irld if e[2] & 0x80)
          == [(2, 0x9C, 0), (3, 0x9C, 4)],
          f"the two negative sites need signed ACON RLDs (0x1C|0x80) at "
          f"bytes 0/4; got {irld!r}")
    check("iop_positive_addend_rld_unsigned",
          sorted((e[0], e[2], e[3]) for e in irld if not e[2] & 0x80)
          == [(2, 0x1C, 8), (2, 0x1C, 16), (3, 0x1C, 12)],
          f"the positive sites (and the bare '#BU FA' at byte 16) must "
          f"keep the plain ACON RLD 0x1C; got {irld!r}")

    # ... and the link-time half: a signed ACON must SUBTRACT the magnitude
    # the field carries, with the arithmetic confined to a[17:0] so neither
    # the sign-bit negation nor a base-address carry can reach the opcode.
    from ap101Utils.addr import Addr
    from ap101Utils.addrcon import AddrCon, RLD_ACON, RLD_SIGN
    _neg = AddrCon(RLD_ACON | RLD_SIGN, 4)
    _pos = AddrCon(RLD_ACON, 4)
    check("iop_signed_acon_subtracts_the_addend",
          (_neg.apply(0xFA000002, Addr.from_hw(0x1234)),
           _neg.apply(0xFD000014, Addr.from_hw(0x5678)))
          == (0xFA001232, 0xFD005664),
          f"got {_neg.apply(0xFA000002, Addr.from_hw(0x1234)):08X} "
          f"{_neg.apply(0xFD000014, Addr.from_hw(0x5678)):08X}")
    check("iop_signed_acon_reverses",
          _neg.reverse(0xFA000002, 0xFA001232) == 0x1234,
          f"reverse() must recover the target 0x1234; got "
          f"{_neg.reverse(0xFA000002, 0xFA001232):#x}")
    # a[17:16] live in the low two bits of the opcode halfword, so a target
    # over 64K MUST still carry into them -- that is why the RLD is a fullword
    # ACON over the whole instruction and not a 2-byte one on hw2.
    check("iop_acon_carry_reaches_a17_a16",
          (_pos.apply(0xFA000000, Addr.from_hw(0x1FFFE)),
           _pos.apply(0xFA000002, Addr.from_hw(0xFFFE)))
          == (0xFA01FFFE, 0xFA010000),
          f"got {_pos.apply(0xFA000000, Addr.from_hw(0x1FFFE)):08X} "
          f"{_pos.apply(0xFA000002, Addr.from_hw(0xFFFE)):08X}")
    # ... but never past bit 17 into the opcode itself.
    check("iop_acon_carry_stops_below_the_opcode",
          _pos.apply(0xFA03FFFE, Addr.from_hw(0x1234)) == 0xFA001232,
          f"an 18-bit overflow must wrap inside a[17:0], leaving FA00 "
          f"intact; got {_pos.apply(0xFA03FFFE, Addr.from_hw(0x1234)):08X}")

    # ESD ordering: SD (control sections) and ER (external refs) are
    # assigned IDs in SOURCE-APPEARANCE order, interleaved; LD (ENTRY)
    # definitions take trailing IDs.  Matches real AP101S 3.0 listings
    # (ASMLIB-BOS).  Source order: SECTA, XFIRST, EHERE, SECTB, YSECOND.
    eobj = td / "esdorder.obj"
    assemble(["-o", str(eobj), str(FIX / "feat_esd_interleave.asm")])
    esd = _esd_entries(eobj)
    check("esd_interleaved_appearance_order",
          [(t, nm) for _id, t, nm in esd] ==
          [("SD", "SECTA"), ("ER", "XFIRST"), ("SD", "SECTB"),
           ("ER", "YSECOND"), ("LD", "EHERE")],
          f"expected SD/ER interleaved by appearance + LD last; got {esd!r}")

    # Symbol attribution and RLD relocation ESDIDs across a SECOND csect.
    # The expression evaluator re-projects every relocatable value onto the
    # FIRST csect, so each RLD site has to recover the true owning section
    # from the flat offset.  Three ways that went wrong, all invisible while
    # the linker keeps the csects contiguous and all off by the placement gap
    # when an OVERLAY boundary separates them:
    #   * 'EQU *' was FILED against the first csect at a module-global
    #     halfword count.  Its value then depended on a preliminaryOffset
    #     that is still 0 during passes 1-2, so a pass-3 forward reference
    #     read the un-re-based value, resolved it to the first csect, and
    #     punched that ESDID -- while pass 4 rewrote the text word correctly.
    #   * a reference from a secondary csect to a symbol in that SAME csect
    #     was punched against the first csect (resolveCSect excluded the
    #     current section outright instead of merely deprioritizing it).
    #   * an EXTRN referenced from a secondary csect was punched against the
    #     first csect: its addend is not an offset into any csect, but it
    #     range-matched one anyway.
    qobj = td / "equsect.obj"
    rc, out = assemble(["-o", str(qobj), str(FIX / "feat_equ_second_csect.asm")])
    check("equ_second_csect_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    qsyms = {s["name"]: (s["section"], s["offset"])
             for s in _asmg_symbols(qobj)}
    check("equ_second_csect_attribution",
          qsyms.get("S2EQU") == ("SEC2", 1),
          f"'S2EQU EQU *' inside SEC2 -> {qsyms.get('S2EQU')!r}, "
          f"expected ('SEC2', 1); all symbols {qsyms!r}")
    check("equ_first_csect_attribution",
          qsyms.get("S1EQU") == ("SEC1", 1),
          f"'S1EQU EQU *' inside SEC1 -> {qsyms.get('S1EQU')!r}")
    qesd = {i: nm for i, t, nm in _esd_entries(qobj)}
    qrld = sorted((qesd.get(p_), a, qesd.get(r), f)
                  for r, p_, f, a in _rld_entries(qobj))
    check("equ_second_csect_rld_targets",
          qrld == [("SEC1", 4, "SEC2", 0x00),    # Y(S2EQU) fwd ref
                   ("SEC1", 6, "SEC2", 0x00),    # Y(S2LABEL) fwd ref
                   ("SEC2", 4, "SEC2", 0x00),    # Y(S2EQU) self-reference
                   ("SEC2", 6, "SEC2", 0x00),    # Y(S2LABEL) self-reference
                   ("SEC2", 8, "SEC1", 0x00),    # Y(S1EQU) back-reference
                   ("SEC2", 12, "EXTSYM", 0x1C)],  # A(EXTSYM) from 2nd csect
          f"(position, address, relocated-against, flag) = {qrld!r}")

    # RLDs describe the FINAL compile pass, not pass 3.  The fixpoint reruns
    # pass 3, 4, 5, ... rewriting the image from scratch each time, so a value
    # that settles later used to get a correct text word and a relocation
    # entry frozen from pass 3.  The EQU chain in the fixture resolves one
    # link per pass and needs seven; at pass 3 'E1' still lands inside S1.
    # Asserting the exact list pins the other direction too: entries are
    # discarded and re-derived per pass, so each site appears exactly once.
    lobj = td / "latesettle.obj"
    rc, out = assemble(["-o", str(lobj), "-v",
                        str(FIX / "feat_rld_late_settle.asm")])
    check("rld_late_settle_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("rld_late_settle_needs_many_passes",
          "converged after 7 passes" in out,
          "the fixture must still settle AFTER pass 3 or it tests nothing; "
          f"got {[l for l in out.splitlines() if 'converged' in l]!r}")
    lesd = {i: nm for i, t, nm in _esd_entries(lobj)}
    lrld = sorted((lesd.get(p_), a, lesd.get(r))
                  for r, p_, f, a in _rld_entries(lobj))
    check("rld_late_settle_targets",
          lrld == [("S1", 0, "S2"),     # DC Y(E1): reaches S2 only by pass 7
                   ("S1", 2, "S1")],    # DC Y(S1TOP): stable control
          f"(position, address, relocated-against) = {lrld!r}; a pass-3-frozen "
          "RLD gives ('S1',0,'S1'), and accumulating across passes repeats "
          "each site once per compile pass")

    # DC B'...' binary constant.  Before the fix this was a no-op stub that
    # emitted no bytes and did not advance the location counter (it dropped
    # BILDNEW5/GPCIPL's 27-entry JOBTABLE of 32-bit schedule masks entirely).
    # Bits pack MSB-first; implicit length rounds up to a halfword; an
    # explicit BLn fixes the width; dup replicates.
    blst = td / "dcb.lst"
    rc, out = assemble(
        ["-o", str(td / "dcb.obj"), "-l", str(blst), str(FIX / "feat_dc_b.asm")])
    check("dc_b_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("dc_b_32bit_mask", _listing_code(blst, "M32") == "04200040",
          f"B'..32..' -> {_listing_code(blst, 'M32')!r}, expected 04200040")
    check("dc_b_implicit_halfword", _listing_code(blst, "W8") == "00A5",
          f"B'10100101' -> {_listing_code(blst, 'W8')!r}, expected 00A5")
    check("dc_b_explicit_length", _listing_code(blst, "L2") == "0005",
          f"BL2'101' -> {_listing_code(blst, 'L2')!r}, expected 0005")
    check("dc_b_dup", _listing_code(blst, "DUP") == "000F000F",
          f"2B'1111' -> {_listing_code(blst, 'DUP')!r}, expected 000F000F")
    # AFTER lands at halfword 5: M32(2hw)+W8(1hw)+L2(1hw)+DUP(2hw) = 6 hw,
    # so AFTER is at hw6 -- proves the B-cons advanced the location counter.
    check("dc_b_advances_lc", _xref_value(blst, "AFTER") == 6,
          f"AFTER at halfword {_xref_value(blst, 'AFTER')}, expected 6")

    # Phase 1: relocatability classification (classify()/unhash), IEUF8V-style.
    # Y(A) simple -> RLD; Y(B-A) same-section -> cancels to absolute (no RLD);
    # Y(A*2) and Y(A+B) -> complex -> diagnosed (was a silent garbage value).
    # Pins fires-on-illegal AND silent-on-legal + paired-term cancellation.
    rc, out = asm("feat_reloc_complex.asm")
    check("reloc_complex_aborts", rc != 0,
          f"complex Y-cons must be intolerable; rc={rc}\n{out.strip()[-300:]}")
    check("reloc_complex_diagnosed",
          out.count("Complex relocatable") >= 2 and "Y-type" in out,
          f"expected complex diagnostics for Y(A*2) and Y(A+B):\n{out.strip()[-400:]}")
    # At --tolerable 255 the module still writes: exactly one RLD (the SIMPLE
    # Y(A)); the absolute Y(B-A) and the two complex cons emit none.
    robj = td / "reloc.obj"
    rc, out = assemble(
        ["-o", str(robj), "--tolerable", "255", str(FIX / "feat_reloc_complex.asm")])
    check("reloc_legal_silent_one_rld", len(_rld_entries(robj)) == 1,
          f"expected 1 RLD (simple Y(A) only); got {_rld_entries(robj)!r}")

    # '$' long-format modifier: B is SRS (1 halfword), B$ is RS (2
    # halfwords), so the label after both branches lands at halfword 3.
    lst = td / "lf.lst"
    rc, out = assemble(
        ["-o", str(td / "lf.obj"), "-l", str(lst), str(FIX / "feat_long_format.asm")])
    check("long_format_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    mid = _xref_value(lst, "MID")
    check("long_format_B$_is_RS", mid == 3,
          f"MID at halfword {mid}, expected 3 (B=1hw + B$=2hw)")

    # Register-form conditional branches (BNZR/BZR/... == BCR mask,R2).
    # These were "Unrecognized line" before the BRANCH_ALIASES_R fix.
    rc, out = asm("feat_bxxr.asm")
    check("bxxr_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")

    # A parenthesized register field, e.g. TRB (R0),... , evaluates the
    # grouped expression to the register number (matching IBM IEUF8M ->
    # IEUF8V).  `(R0)` must assemble byte-identically to bare `R0`, and a
    # grouped expression `(R0+1)` must evaluate to register 1.
    plst = td / "paren_reg.lst"
    rc, out = assemble(
        ["-o", str(td / "paren_reg.obj"), "-l", str(plst),
         str(FIX / "feat_paren_register.asm")])
    check("paren_register_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    bare = _listing_code(plst, "BARE")
    paren = _listing_code(plst, "PAREN")
    expr = _listing_code(plst, "EXPR")
    check("paren_register_byte_identical_to_bare",
          bare is not None and paren == bare,
          f"BARE={bare!r} PAREN={paren!r}")
    check("paren_register_grouped_expr_evaluates",
          expr is not None and expr[:4] == "B3E1",
          f"(R0+1) -> {expr!r}, expected register 1 (B3E1...)")

    # IOP #DLY (BCE delay-from-memory) is flight-pinned to FCMMGBOV: the
    # skeleton `FCMGNDLC #DLY FCMGNDLC+1` assembles to C800 (mafgen DASS_G16,
    # FCMMGBOV+0307).  C800 = 11001 00000000000, i.e. opcode 11001 and a ZERO
    # 'd' -- which is only reachable because 'd' is PC-RELATIVE:
    # d = (GNDLC+1) - (IC+1) = 0.  A literal 'd' would carry the operand's
    # offset (non-zero) instead.  This pins the FULL assembler path (PC-relative
    # field extraction + descriptor encode), independent of the byte golden.
    dlst = td / "iop.lst"
    rc, out = assemble(
        ["-o", str(td / "iop.obj"), "-l", str(dlst),
         str(FIX / "feat_iop_encoding.asm")])
    check("iop_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("iop_dly_pc_relative_C800", _listing_code(dlst, "GNDLC") == "C800",
          f"#DLY GNDLC+1 -> {_listing_code(dlst, 'GNDLC')!r}, expected C800")

    # The BCE short memory-reference ops (#WIX/#SSC/#SST/#LTO) also take a
    # code/data ADDRESS in 'd' and are PC-RELATIVE (d = target-(IC+1)), proven
    # against flight listings (BFS #WIX, FIOMMUPG #SST, BILDNEW5 #SSC/#LTO).
    # Each fixture target is HERE-4, so the displacement is a NON-ZERO -5 (0x7FB)
    # regardless of layout -- a careless d=0 case would let the old literal bug
    # (which emitted the operand's offset) masquerade as correct.  #SSC/#SST also
    # carry a 1-bit 'm' index field set by "(1)"; the old code only handled a
    # field named 'i', so m was stuck at 0.  Assert both m=1 and m=0.
    for label, want, why in [
        ("WIXHERE", "27FB", "#WIX d=-5 pc-relative (was literal -> 2000)"),
        ("SSCM1",   "4FFB", "#SSC (1): m=1 + d=-5 (was 4000)"),
        ("SSCM0",   "47FB", "#SSC: m=0 + d=-5"),
        ("SSTM1",   "5FFB", "#SST (1): m=1 + d=-5 (flight-exact, FIOMMUPG)"),
        ("LTOHERE", "B800", "#LTO d=0 (flight-exact, BILDNEW5)"),
    ]:
      got = _listing_code(dlst, label)
      check(f"iop_bce_memref_{label}", got == want,
            f"{why}: {label} -> {got!r}, expected {want}")

    # The IOP @CNOP/#CNOP are the same alignment pseudo-op as CPU CNOP, in
    # MSC/BCE context (they share the location counter); they route to the CNOP
    # handler instead of the IOP encoder (which has no descriptor for them and
    # used to crash).  ACN/BCN follow an odd-positioned counter, so @CNOP 2 /
    # #CNOP 2 each emit one pad halfword and advance the counter to the next
    # fullword -- but the LABEL names the pad, i.e. the counter BEFORE it
    # (halfword 1 and 3), not the aligned address after it.  See
    # feat_cnop_prepad.asm for the derivation of that rule.
    cnlst = td / "iopcnop.lst"
    rc, out = assemble(
        ["-o", str(td / "iopcnop.obj"), "-l", str(cnlst),
         str(FIX / "feat_iop_cnop.asm")])
    check("iop_cnop_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("iop_cnop_at_aligns_fullword", _xref_value(cnlst, "ACN") == 1,
          f"@CNOP 2 -> ACN at halfword {_xref_value(cnlst, 'ACN')}, expected 1")
    check("iop_cnop_hash_aligns_fullword", _xref_value(cnlst, "BCN") == 3,
          f"#CNOP 2 -> BCN at halfword {_xref_value(cnlst, 'BCN')}, expected 3")

    # A BCE-looking name that is really an MLIB80 macro (e.g. #ORG), assembled
    # WITHOUT the macro library, is not an instruction -- it must read as a plain
    # "Unrecognized line", the SAME diagnostic as any unknown operation, and must
    # not crash or masquerade as an "Unimplemented IOP pseudo-op".
    rc, out = asm("iop_unimpl_pseudo.asm")
    check("iop_unimpl_pseudo_diagnosed",
          rc != 0 and "Traceback" not in out
          and "Unrecognized line" in out
          and "Unimplemented IOP pseudo-op" not in out,
          f"rc={rc}\n{out.strip()[-400:]}")

    # CNOP n aligns the location counter (1=odd halfword, 2=fullword) and pads
    # the gap with an executable NOP.  AFULL sits at halfword 1 -- the pad it
    # generates -- and moves the counter to halfword 2; BHALF stays at
    # halfword 3 (the counter is already odd, so CNOP 1 emits nothing).
    clst = td / "cnop.lst"
    rc, out = assemble(
        ["-o", str(td / "cnop.obj"), "-l", str(clst),
         str(FIX / "feat_cnop.asm")])
    check("cnop_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("cnop2_fullword_align", _xref_value(clst, "AFULL") == 1,
          f"AFULL at halfword {_xref_value(clst, 'AFULL')}, expected 1")
    check("cnop1_halfword_noop", _xref_value(clst, "BHALF") == 3,
          f"BHALF at halfword {_xref_value(clst, 'BHALF')}, expected 3")

    # A CNOP's pad is the CNOP's OWN object code, so the statement's location
    # -- and any label on it -- is the counter BEFORE the pad, not the aligned
    # address after it.  (A DC/DS names its POST-alignment start instead: that
    # gap belongs to nobody.)  A labelled CNOP is a branch target, so this is
    # object code, not listing cosmetics.  The pad is typed by context: D800
    # (BCF 0,0) for the CPU form, C000 (the DLYI-0 no-op) for the IOP @/#
    # forms.
    plst = td / "cnoppre.lst"
    rc, out = assemble(
        ["-o", str(td / "cnoppre.obj"), "-l", str(plst),
         str(FIX / "feat_cnop_prepad.asm")])
    check("cnop_prepad_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want_hw, want_code, why in [
        ("PADCPU", 1, "D800", "CNOP 2 from an odd counter: names its own pad"),
        ("NOPADC", 2, None,   "already fullword: no pad, pre = aligned"),
        ("PADIOP", 3, "C000", "@CNOP 2 pads with the IOP no-op, not D800"),
        ("ODDOK",  5, None,   "CNOP 1 with an odd counter: no pad"),
        ("PADODD", 6, "D800", "CNOP 1 from an even counter: one pad halfword"),
        ("AFTER",  7, "0000", "the pads really advanced the counter"),
    ]:
      got_hw = _xref_value(plst, label)
      check(f"cnop_prepad_{label.lower()}_addr", got_hw == want_hw,
            f"{why}: {label} at halfword {got_hw}, expected {want_hw}")
      got_code = _listing_code(plst, label)
      check(f"cnop_prepad_{label.lower()}_code", got_code == want_code,
            f"{why}: {label} object code {got_code!r}, expected {want_code!r}")

    # DC bit-length ("L.n") packing.  PACK is the pinned reference value:
    # YL.2(3),YL.7(5),YL.7(6) packs MSB-first to 0xC286.  SYM lands at
    # halfword 4, proving PACK(2)+MIXB(2)+MIXA(4) = 8 bytes were emitted.
    blst = td / "bl.lst"
    rc, out = assemble(
        ["-o", str(td / "bl.obj"), "-l", str(blst),
         str(FIX / "feat_bit_length.asm")])
    check("bit_length_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    pack = _listing_code(blst, "PACK")
    check("bit_length_pack_msb_first", pack == "C286",
          f"PACK object code {pack!r}, expected 'C286'")
    sym = _xref_value(blst, "SYM")
    check("bit_length_byte_count", sym == 4,
          f"SYM at halfword {sym}, expected 4 (PACK+MIXB+MIXA = 8 bytes)")

    # Unary / doubled leading minus.  "--285" must fold to +285 in both an
    # EQU value and a DC F/H value; the packed DC BL.5'10000',FL.11'--285'
    # is 5-bit 10000 + 11-bit 285 = 0x811D.  This is the XPOS/YPOS path POS
    # unblocks once L' resolves.
    ulst = td / "um.lst"
    rc, out = assemble(
        ["-o", str(td / "um.obj"), "-l", str(ulst),
         str(FIX / "feat_unary_minus.asm")])
    check("unary_minus_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    upack = _listing_code(ulst, "PACK")
    check("unary_minus_dc_folds_to_811D", upack == "811D",
          f"PACK object code {upack!r}, expected '811D' (--285 -> +285)")

    # Final-pass intolerable-error counting.  A forward reference to an EQU
    # symbol (skipped by the preliminary scan) logs a transient pass-1
    # "Cannot evaluate Y-type constant" but resolves by the final pass, so
    # it must NOT abort the assembly; EARLYREF pins the resolved value
    # Y(LATEEQU)=halfword 1.  Conversely, a genuinely undefined symbol
    # errors in the final pass and MUST still abort.
    flst = td / "fref.lst"
    rc, out = assemble(
        ["-o", str(td / "fref.obj"), "-l", str(flst),
         str(FIX / "feat_fwd_equ_ref.asm")])
    check("fwd_equ_ref_tolerated", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    eref = _listing_code(flst, "EARLYREF")
    check("fwd_equ_ref_value", eref == "0001",
          f"EARLYREF object code {eref!r}, expected '0001'")
    rc, out = asm("undefined_symbol_errors.asm")
    check("undefined_symbol_still_errors",
          rc != 0 and "intolerable" in out,
          f"genuinely-undefined symbol must abort; rc={rc}\n{out.strip()[-300:]}")

    # Compiler-style diagnostics: an aborting assembly reports each
    # intolerable line as 'file:line: error: message' with a source excerpt --
    # NOT a dump of the whole expanded deck.  A macro-generated line points at
    # the macro-definition card, shows the substituted operand, and traces the
    # invocation back to the primary source (fixture: DC Y(&SYM) at line 4,
    # invoked as 'GENBAD NOSUCH' at line 6).
    rc, out = asm("feat_diag_format.asm")
    check("diag_file_line_prefix",
          rc != 0 and re.search(r"feat_diag_format\.asm:4: error: ", out),
          f"expected 'feat_diag_format.asm:4: error: ...'; rc={rc}\n"
          f"{out.strip()[-400:]}")
    check("diag_expands_to",
          "expands to:" in out and "Y(NOSUCH)" in out,
          f"expected substituted-operand note 'expands to: ... Y(NOSUCH)':\n"
          f"{out.strip()[-400:]}")
    check("diag_invocation_note",
          re.search(r"in expansion of macro GENBAD, invoked from .*"
                    r"feat_diag_format\.asm:6", out),
          f"expected invocation-chain note pointing at line 6:\n"
          f"{out.strip()[-400:]}")
    check("diag_no_full_dump", "DIAGT    CSECT" not in out,
          "error report must not echo non-erroring source lines")

    # Overflow/carry branches (BOV/BOC/BVC).  Object code pinned to the
    # OI301700 ground-truth listing: all encode in the BVCF family (SRS
    # forward, bb=01) with disp=2.  BO/BNO are the distinct condition-code
    # family (BCF, bb=00) and must be unchanged.
    olst = td / "ovc.lst"
    rc, out = assemble(
        ["-o", str(td / "ovc.obj"), "-l", str(olst),
         str(FIX / "feat_ovfl_carry_branch.asm")])
    check("ovfl_carry_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want, desc in [
        ("BR1", "D909", "BOV -> BVCF mask 1 (overflow)"),
        ("T1",  "DA09", "BOC -> BVCF mask 2 (carry)"),
        ("T2",  "DE09", "BVC 6 -> BVCF mask 6 (explicit)"),
        ("T3",  "D908", "BO -> BCF mask 1 (condition-code, unchanged)"),
        ("T4",  "DE08", "BNO -> BCF mask 6 (condition-code, unchanged)"),
    ]:
      got = _listing_code(olst, label)
      check(f"ovfl_carry_{label}", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")

    # Branch on Count, backward -> BCTB (SRS 11011 rrr dddddd 11).  BCT is
    # in ARGS_RS_ONLY, which forced the RS path and suppressed BCTB; every
    # backward BCT was mis-encoded (e.g. STM1255 BCT R4,STM1250 -> D45C
    # garbage instead of DC23).  The codegen now gates the short backward
    # form on the `$` long-format flag, not on forceRS.
    blst = td / "bctb.lst"
    rc, out = assemble(
        ["-o", str(td / "bctb.obj"), "-l", str(blst),
         str(FIX / "feat_bctb_branch.asm")])
    check("bctb_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want, desc in [
        ("C1", "DC0F", "BCT R4,B1 backward disp 3 -> BCTB R4"),
        ("C2", "DA0F", "BCT R2,B2 backward disp 3 -> BCTB R2"),
        ("B3", "DF07", "BCT R7,B3 branch-to-self disp 1 -> BCTB R7"),
    ]:
      got = _listing_code(blst, label)
      check(f"bctb_{label}", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")

    # Forward BC under a covering USING -> PC-relative SRS (BCF).  With
    # `USING T,R1` findB2D2 resolves every local target to base register
    # R1, so the forward BC used to fall through to the generic SRS path,
    # which had reassigned d2 to the target's displacement from the USING
    # base register and emitted THAT as the SRS displacement -- a
    # base-relative value, not the PC-relative one a SRS branch requires.
    # Before the fix C1 encoded DF08 (disp 2, reaching hw3) and C2 DA14
    # (disp 5, reaching hw8), both overshooting while still "assembling".
    # This is the IFPROC `BC mask,#@LBn` miscompile in IFTESTS (whose
    # `USING *,R12` + `R12 EQU 1` is the same covering-USING idiom): every
    # such branch silently overshot, and only the few whose base-relative
    # displacement reached >= 56 even tripped a visible range error.  The
    # fix emits forward BC via the early PC-relative path (d = d2 -
    # (currentHash+1), from the original hashed d2 before findB2D2's
    # reassignment).
    uslst = td / "bcf_using.lst"
    rc, out = assemble(
        ["-o", str(td / "bcf_using.obj"), "-l", str(uslst),
         str(FIX / "feat_bcf_using.asm")])
    check("bcf_using_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want, desc in [
        ("C1", "DF04", "BC 07,FWD1 hw0->hw2 PC-rel disp 1 -> BCF mask 7"),
        ("C2", "DA08", "BC 02,FWD2 hw2->hw5 PC-rel disp 2 -> BCF mask 2"),
    ]:
      got = _listing_code(uslst, label)
      check(f"bcf_using_{label}", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")
    # Decisive PC-relative reach check (the error count lies; a base-
    # relative misencode reaches the wrong address while still assembling):
    # decode each BCF and confirm it reaches its target's listing address.
    _phys = {}
    for line in uslst.read_text(errors="replace").splitlines():
      mm = re.match(r"\s*([0-9A-Fa-f]{5}) .*?\s\d+ (FWD\d)\b", line)
      if mm:
        _phys[mm.group(2)] = int(mm.group(1), 16)
    for label, tgt in [("C1", "FWD1"), ("C2", "FWD2")]:
      o = int(_listing_code(uslst, label), 16)
      loc = None
      for line in uslst.read_text(errors="replace").splitlines():
        mr = re.match(r"\s*([0-9A-Fa-f]{5}) .*?\s\d+ " + label + r"\b",
                      line)
        if mr:
          loc = int(mr.group(1), 16)
          break
      disp = (o >> 2) & 0x3F
      ss = o & 3
      reach = (loc + 1) + disp if ss in (0, 1) else (loc + 1) - disp
      check(f"bcf_using_reach_{label}", reach == _phys[tgt],
            f"{label}->{tgt}: reach {reach:#x} != target {_phys[tgt]:#x}")

    # EXTRN symbol + constant offset as an instruction operand
    # (`LH R4,FAZ2STRT+3`).  unhash() of hashcode+offset recovers the
    # EXTRN name and the offset; codegen emits the offset as the RS
    # displacement and leaves the symbol to a Y-type RLD.  Before the fix
    # this raised "Could not interpret operand" and emitted no object
    # code, drifting every following label's address (which in turn
    # mis-sized short backward branches).  Ground truth (OI301700
    # GPCERAS): LH R4,FAZ2STRT+3 -> 9CF3 0003, LH R5,FAZ2STRT+1 -> 9DF3 0001.
    eolst = td / "eo.lst"
    rc, out = assemble(
        ["-o", str(td / "eo.obj"), "-l", str(eolst),
         str(FIX / "feat_extrn_offset.asm")])
    check("extrn_offset_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("extrn_offset_no_interp_error",
          "Could not interpret operand" not in out,
          "EXTRN+offset operand raised an interpret error")
    eotext = eolst.read_text(errors="replace")
    for label, want, desc in [
        ("L0", "9CF3 0003", "LH R4,FAZ2STRT+3 -> offset 3 in displacement"),
        ("L1", "9DF3 0001", "LH R5,FAZ2STRT+1 -> offset 1 in displacement"),
        ("L2", "9CF3 0000", "LH R4,FAZ2STRT (bare) -> offset 0"),
    ]:
      line = next((l for l in eotext.splitlines()
                   if re.search(r"\d+ " + label + r"\s", l)), "")
      check(f"extrn_offset_{label}", want in line,
            f"{desc}: expected {want!r} in listing line {line.strip()!r}")

    # Address-drift / SRS-RS branch-condense fixpoint.  TOP's forward
    # branch is sized RS by the pass-1 optimizeScratch over-estimate but
    # condenses to SRS in the compile pass; that shrinks FAR by one
    # halfword.  Without the compile-pass fixpoint (symtab re-derived per
    # pass, repeated until the table settles) TOP keeps the stale
    # displacement (DC7C) and overshoots FAR by one; with it TOP
    # re-resolves to DC78 (disp 30) and lands exactly on FAR.  Both TOP
    # (forward) and CHK (backward) must reach FAR's settled address.
    dlst = td / "drift.lst"
    rc, out = assemble(
        ["-o", str(td / "drift.obj"), "-l", str(dlst),
         str(FIX / "feat_drift.asm")])
    check("drift_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    check("drift_converges", "did not converge" not in out,
          f"compile fixpoint did not settle:\n{out.strip()[-300:]}")
    dtext = dlst.read_text(errors="replace")
    far = _xref_value(dlst, "FAR")
    for label, want, ss, desc in [
        ("TOP", "DC78", "fwd", "TOP BE FAR condenses RS->SRS, disp 30"),
        ("CHK", "DC0A", "bwd", "CHK BE FAR backward, disp 2"),
    ]:
      got = _listing_code(dlst, label)
      check(f"drift_{label}_code", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")
    # Decode TOP's SRS displacement and confirm it reaches FAR's address
    # (not FAR+1, the pre-fix overshoot).
    top = _listing_code(dlst, "TOP")
    if top and far is not None:
      hw = int(top, 16)
      top_addr = _xref_value(dlst, "TOP")
      disp = (hw >> 2) & 0x3F
      reach = (top_addr + 1) + disp        # SRS forward (ss=00)
      check("drift_top_reaches_far", reach == far,
            f"TOP reaches {reach:#x}, FAR at {far:#x}")

    # ORG sets the location counter; backward `ORG *-1` lets the next
    # statement overwrite the previous halfword (as the message-table
    # macros do after an FCW2 control word).  A/B are consecutive halfwords;
    # after them the counter is at halfword 2, `ORG *-1` backs it to 1, so C
    # must be defined at B's address (1), not 2.  Before the fix ORG's `*-1`
    # operand was dropped at load time (the operand-skip test checked the
    # minimum operand count, 0 for optional-operand ORG) so ORG was a no-op.
    olst = td / "org.lst"
    rc, out = assemble(["-o", str(td / "org.obj"), "-l", str(olst),
                        str(FIX / "feat_org.asm")])
    check("org_assembles", rc == 0, f"rc={rc}\n{out.strip()[-300:]}")
    b_addr = _xref_value(olst, "B")
    c_addr = _xref_value(olst, "C")
    check("org_backward_sets_counter",
          b_addr == 1 and c_addr == 1,
          f"B@{b_addr} C@{c_addr} (ORG *-1 should put C at B's address 1)")

    # Bare-operand indirect (@) / indexed (#) addressing must encode RS
    # AM=1.  A low base register (R0 via USING) on an odd opcode sets
    # forceAM0, which blocks the AM=1 path; combined with the operand
    # having no explicit index/base register, the bare form used to fall
    # through to "Could not interpret line as SRS or RS" and emit nothing.
    # @/# force AM=1 since the ia/i bits exist only there.  Ground-truth
    # bit layout (generateRS1): byte1 = opcode|AM(0b100)|b2; byte2 high
    # nibble carries ia (bit4) and i (bit3).  PTR is at displacement 0.
    atlst = td / "at.lst"
    rc, out = assemble(
        ["-o", str(td / "at.obj"), "-l", str(atlst),
         str(FIX / "feat_at_indirect.asm")])
    check("at_indirect_assembles", rc == 0
          and "SRS or RS" not in out,
          f"rc={rc}\n{out.strip()[-400:]}")
    # Without the fix the assembly aborts and writes no listing, so guard
    # the byte checks on a clean run (the assembles check above is the
    # regression signal in that case).
    attext = atlst.read_text(errors="replace") if atlst.exists() else ""
    # LDM's R1 is a FIXED part of the opcode (=0, per PASS MAFGEN: LDM->68xx),
    # so LDM@# encodes 68 regardless of the source R1; only LXA@ (which has a
    # real x/R1 field) carries R1=1.  The @ test here is about the AM=1 ia/i
    # mode bits in the second halfword, not R1.
    for label, want, desc in [
        ("L0", "68FC 1800", "LDM@# R1,PTR bare -> AM=1, ia=1 i=1, x2=0 (R1 fixed 0)"),
        ("L1", "41FC 1000", "LXA@ R1,PTR bare -> AM=1, ia=1 i=0"),
        ("L2", "68FC 7800", "LDM@# R1,PTR(R3) indexed -> AM=1, x2=3 (R1 fixed 0)"),
    ]:
      line = next((l for l in attext.splitlines()
                   if re.search(r"\d+ " + label + r"\s", l)), "")
      check(f"at_indirect_{label}", want in line,
            f"{desc}: expected {want!r} in listing line {line.strip()!r}")

    # A multi-suboperand DC's label names its first suboperand.  Each
    # suboperand handler calls commonProcessing (which assigns the label),
    # so `PTR DC Y(SYM),X'2'` used to put PTR on the trailing X byte (odd
    # address) instead of the leading Y halfword.  SYM is 4 bytes
    # (halfwords 0-1) so PTR's halfword address must be 2, not 3.
    dylst = td / "dy.lst"
    rc, out = assemble(
        ["-o", str(td / "dy.obj"), "-l", str(dylst),
         str(FIX / "feat_dc_y_label.asm")])
    check("dc_y_label_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    ptr = _xref_value(dylst, "PTR")
    check("dc_y_label_on_first_suboperand", ptr == 2,
          f"PTR (DC Y(SYM),X'2') at halfword {ptr}, expected 2 (the Y, "
          f"not 3 = the trailing X byte)")

    # An X constant with no length modifier occupies ceil(digits/2) bytes
    # rounded up to a halfword, value right-justified (AP-101S stores hex
    # constants halfword-wide).  A single digit -> 2 bytes (X'8' -> 0008,
    # not 08 which alignment turned into 0x0800); even-byte constants are
    # unchanged.
    xhlst = td / "xh.lst"
    rc, out = assemble(
        ["-o", str(td / "xh.obj"), "-l", str(xhlst),
         str(FIX / "feat_dc_x_halfword.asm")])
    check("dc_x_halfword_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want, desc in [
        ("A0", "0008", "X'8' -> halfword, right-justified"),
        ("A1", "000F", "X'F' -> halfword, right-justified"),
        ("A2", "0011", "X'0011' -> 2 bytes, unchanged"),
        ("A3", "AAAAAAAA", "X'AAAAAAAA' -> 4 bytes, unchanged"),
    ]:
      got = _listing_code(xhlst, label)
      check(f"dc_x_halfword_{label}", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")

    # An AGO target must ignore a trailing comment on the operand.  The AGO
    # operand is a sequence symbol optionally followed by a comment
    # ("AGO .GEN   LOOP BACK" in MACSMITH's TABLGEN/MSGLINES); the handler
    # took the whole operand verbatim, so the comment glued onto the symbol
    # never matched the recorded ".GEN" and the backward loop-jump degraded
    # into a forward skip that found nothing -- the conditional-assembly
    # counting loop then ran exactly once.  GENVALS loops 4x emitting one
    # halfword each, so TOP (EQU * after the loop) must land at halfword 4.
    agolst = td / "ago.lst"
    rc, out = assemble(
        ["-o", str(td / "ago.obj"), "-l", str(agolst),
         str(FIX / "feat_ago_comment.asm")])
    check("ago_comment_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    top = _xref_value(agolst, "TOP")
    check("ago_comment_loop_runs_fully", top == 4,
          f"TOP (after a 4-iteration AGO loop) at halfword {top}, expected "
          f"4; a smaller value means the loop ran fewer times because the "
          f"AGO target carried its comment")

    # C-type (character) DC.  The handler was an unimplemented stub
    # (commonProcessing + `pass`) that emitted NO object code, so every
    # `DC C'...'` silently vanished -- and the shared length-modifier
    # computation fed the ('L',n) AST to the arithmetic evaluator, which
    # failed, breaking explicit lengths (CLn/XLn) for all types.  Cases:
    # plain, explicit length (right-pad with EBCDIC blanks 0x40 / truncate),
    # duplication, and the '' escaped quote (-> EBCDIC 7D).
    dcclst = td / "dcc.lst"
    rc, out = assemble(
        ["-o", str(td / "dcc.obj"), "-l", str(dcclst),
         str(FIX / "feat_dc_char.asm")])
    check("dc_char_assembles", rc == 0, f"rc={rc}\n{out.strip()[-400:]}")
    for label, want, desc in [
        ("C0", "E7E8", "C'XY' -> EBCDIC XY"),
        ("C1", "C1C24040", "CL4'AB' -> AB + 2 EBCDIC blanks"),
        ("C2", "C1", "CL1'AB' -> truncated to A"),
        ("C3", "E9E9E9", "3C'Z' -> ZZZ via duplication"),
        ("C4", "C17DC2", "C'A''B' -> A'B (escaped quote)"),
    ]:
      got = _listing_code(dcclst, label)
      check(f"dc_char_{label}", got == want,
            f"{desc}: object code {got!r}, expected {want!r}")


# =====================================================================
# INTEGRATION: macro-library behaviors (a -L directory of fixtures)
# =====================================================================
def maclib_tests():
  with tempfile.TemporaryDirectory(prefix="asm101_mac_") as td:
    td = Path(td)

    # ACTR-style loop cap: an infinite AIF/AGO macro must fail fast with a
    # diagnostic instead of hanging.  The subprocess timeout guards "hang".
    rc, out = assemble(
        ["-o", str(td / "actr.obj"),
         "-L", str(FIX / "maclib_actr"), str(FIX / "actr_main.asm")],
        timeout=60)
    check("actr_loop_cap_fires", rc != 0 and "Conditional-assembly loop" in out,
          f"rc={rc}\n{out.strip()[-400:]}")

    # ACTR n directive: sets the conditional-assembly loop budget for this
    # expansion (MLIB80 ENDCASE raises it to 30000 for CASE branch-vector
    # generation -- CASETEST hit "Unrecognized line" on the ACTR + the 4096
    # default cap).  ACTRDIR takes 5000 AIF branches (> the 4096 default)
    # under ACTR 8000, so it completes and emits DC H'1' (0001) only if the
    # directive was both consumed (not "Unrecognized line") and honored.
    adobj = td / "actrdir.obj"
    rc, out = assemble(
        ["-o", str(adobj),
         "-L", str(FIX / "maclib_actr"), str(FIX / "actrdir_main.asm")],
        timeout=60)
    check("actr_directive_raises_budget",
          rc == 0 and "Conditional-assembly loop" not in out
          and "Unrecognized line" not in out,
          f"ACTR 8000 must let the 5000-branch loop finish: "
          f"rc={rc}\n{out.strip()[-400:]}")
    check("actr_directive_emits_after_loop",
          b"\x00\x01" in (adobj.read_bytes() if adobj.exists() else b""),
          "the DC H'1' after the ACTR-budgeted loop must emit 0001")

    # Continuation detection must use the card's TRUE width, not a line
    # padded to 80.  A hand-keyed deck line that is only 79 columns wide and
    # whose 8-char sequence number bled left into column 72 (e.g.
    # "...0000020A") was padded to 80 and then mistaken for a continuation
    # card, silently swallowing the following line.  Here a 79-col BALR with
    # col-72 bleed precedes `USING *,R1`; if the USING is swallowed the SI
    # `TB OK,X'88'` has no base register and is dropped (its B309 encoding
    # never appears).  Expect the USING honored and TB emitted (B3 09).
    scobj = td / "seqcol72.obj"
    rc, out = assemble(
        ["-o", str(scobj), str(FIX / "feat_seqnum_col72.asm")])
    check("seqnum_col72_not_continuation", rc == 0,
          f"79-col line w/ seqnum in col 72 must not swallow next: "
          f"rc={rc}\n{out.strip()[-400:]}")
    check("seqnum_col72_using_honored",
          b"\xb3\x09" in (scobj.read_bytes() if scobj.exists() else b""),
          "USING after a col-72-bleed line must survive so TB OK,X'88' "
          "resolves its base register and emits B309")

    # Computed AGO (`AGO (&N).seq1,...,.seqN` -> the &N-th sequence symbol)
    # AND a backward jump to a sequence symbol that an earlier forward jump
    # skipped over.  CAGO dispatches on &N; its &N=2 path forward-jumps to
    # .MID (skipping .EMIT) then backward-jumps to .EMIT.  Both fixes are
    # needed: without computed AGO the whole `(&N)....` is one bogus target
    # (nothing emitted); without the sequence-symbol pre-scan the backward
    # jump to the skipped .EMIT runs off the macro end.  These are the two
    # bugs behind DCHAR dropping the data byte for every blank message
    # character (message text assembled as all field-code-0).  Expect
    # C1=H'11'(000B), C2=H'22'(0016, via the backward path), C3=H'33'(0021).
    sqlst = td / "seqsym.lst"
    rc, out = assemble(
        ["-o", str(td / "seqsym.obj"), "-l", str(sqlst),
         "-L", str(FIX / "maclib_seqsym"), str(FIX / "seqsym_main.asm")],
        timeout=60)
    check("seqsym_assembles", rc == 0, f"rc={rc}\n{out.strip()[-300:]}")
    # Each label is on a DS 0H; the dispatched DC is the next line at the
    # same halfword address, so check the object code by address.
    sqtext = sqlst.read_text(errors="replace")
    for label, want in [("C1", "000B"), ("C2", "0016"), ("C3", "0021")]:
      addr = _xref_value(sqlst, label)
      found = addr is not None and re.search(
          r"^%05X %s\b" % (addr, want), sqtext, re.MULTILINE) is not None
      check(f"seqsym_{label}_code", found,
            f"computed-AGO/backward-skip: {label}@{addr} should hold {want}")

    # Macro-library scan mode: a directory may contain non-macro program
    # decks (PROGDECK) alongside real macros (MYMAC).  The deck defines no
    # macro, so its (error-producing) top-level code must be ignored, while
    # the macro is still usable.
    rc, out = assemble(
        ["-o", str(td / "scan.obj"),
         "-L", str(FIX / "maclib_scan"), str(FIX / "scan_main.asm")])
    check("scan_mode_ignores_nonmacro_deck", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")

    # Macro-body continuation join + boolean OR-chain eval: GATEMAC has a
    # continued 3-term SETB gating an AIF.  With ANCTL2=1 the SETB must be
    # true so the AIF skips the `MNOTE 8,'NOT GATED'`.  Before the fixes,
    # the SETB was truncated to its first card (parse error) or the 3-term
    # OR failed to evaluate, leaving the gate false and firing the MNOTE.
    rc, out = assemble(
        ["-o", str(td / "cb.obj"),
         "-L", str(FIX / "maclib_contbool"), str(FIX / "contbool_main.asm")])
    check("contbool_assembles_and_gates", rc == 0 and "NOT GATED" not in out,
          f"rc={rc}\n{out.strip()[-500:]}")

    # Compile-fixpoint convergence with a MACRO-EXPANDED multiply-defined
    # EQU.  DUPEQU emits `DUP EQU *`; invoking it twice defines DUP at two
    # different offsets, so the two definitions write conflicting `*`
    # addresses every compile pass while the end-of-pass table
    # (last-def-wins) is stable -- exactly the BILDNEW5 DCHAR/MSG13x
    # message-table pattern.  A per-EQU "value changed" repeat trigger
    # reacted to that intra-pass churn forever and pinned the assembly at
    # the 50-pass maxPasses cap; the end-of-pass snapshot fixpoint settles
    # it.  `--tolerable 255` lets the (tolerated) duplicate-label notice by
    # and exercise the compile loop, mirroring the real-deck invocation.
    rc, out = assemble(
        ["--tolerable", "255", "-o", str(td / "dupequ.obj"),
         "-L", str(FIX / "maclib_dupequ"), str(FIX / "dupequ_main.asm")],
        timeout=60)
    check("dupequ_converges",
          rc == 0 and "did not converge" not in out,
          f"multiply-defined EQU must not stall the fixpoint: rc={rc}\n"
          f"{out.strip()[-400:]}")

    # On-demand load of a library macro triggered from inside a CONTINUED
    # macro invocation, then correct expansion of its body.  M1's body
    # invokes M2 via a multi-card (continued) `M2 1,2,...,22`; M2 is not yet
    # loaded, so it is fetched on demand while self.source[-2] is that
    # continuation card.  Two distinct bugs this guards (both were live):
    #   1. assemble.py read-boundary: the continuation-skip used the GLOBAL
    #      source list, so M2's first card (its MACRO header) was skipped,
    #      the prototype was never seen, and MEND fired with no macro name
    #      (UnboundLocalError -- a hard crash).
    #   2. model101.py Pass 0: the `continuation` flag set by the continued
    #      invocation was not reset across the on-demand-inserted MACRO..MEND
    #      lines, so the expanded `DC X'BEEF'` was treated as a continuation
    #      card and skipped -- its `ast` never set (KeyError in codegen).
    # rc==0 guards (1); the BEEF bytes in the object guard (2): the expanded
    # body must actually emit, not be silently dropped.
    clobj = td / "contload.obj"
    rc, out = assemble(
        ["-o", str(clobj),
         "-L", str(FIX / "maclib_contload"), str(FIX / "contload_main.asm")],
        timeout=60)
    check("contload_assembles", rc == 0,
          f"continued invocation of an on-demand macro must not crash: "
          f"rc={rc}\n{out.strip()[-400:]}")
    beef = clobj.exists() and (b"\xbe\xef" in clobj.read_bytes())
    check("contload_expands_body", beef,
          "expanded macro body (DC X'BEEF') must emit, not be dropped as a "
          "stray continuation card")

    # &SYSLIST past the supplied operand count must be a NULL string (IBM),
    # not an error.  WALK loops `AIF ('&SYSLIST(&N)' EQ '').DONE`, emitting
    # one `DC C'&SYSLIST(&N)'` per operand and stepping &N until the
    # reference goes null.  When out-of-range &SYSLIST instead errored and
    # left the literal "&SYSLIST(k)" text unsubstituted, the EQ-'' test was
    # never true and the macro spun until the ACTR cap -- the exact way the
    # IF/ELSE/ENDIF structured macros (MLIB80/IFPROC) hung.  Invoked with
    # AB,CD,EF: must terminate AND emit exactly those three EBCDIC pairs.
    slobj = td / "syslist.obj"
    rc, out = assemble(
        ["-o", str(slobj),
         "-L", str(FIX / "maclib_syslist"), str(FIX / "syslist_main.asm")],
        timeout=60)
    check("syslist_walk_terminates",
          rc == 0 and "Conditional-assembly loop" not in out,
          f"&SYSLIST past operands must be null so the walk terminates: "
          f"rc={rc}\n{out.strip()[-400:]}")
    sb = slobj.read_bytes() if slobj.exists() else b""
    walked = (b"\xc1\xc2" in sb and b"\xc3\xc4" in sb and b"\xc5\xc6" in sb)
    check("syslist_walk_emits_operands", walked,
          "WALK must emit C'AB', C'CD', C'EF' (EBCDIC C1C2/C3C4/C5C6) -- one "
          "per supplied operand, no more, no fewer")

    # Sublist propagation through nested macros + two-subscript K' + BC mask
    # arithmetic + model-statement generation -- the chain the structured
    # comparison-form IF (`IF (LACR,R2,R12,NP)`) relies on.  OUTER takes a
    # sublist (LR,R2,R3) and re-passes it positionally to MID (IF->IFPROC);
    # MID re-passes the single sublist operand &SYSLIST(2) to EMIT
    # (IFPROC->STKINS); EMIT pulls opcode/operands by SINGLE subscript
    # (&Q1(1)/&Q1(2)/&Q1(3)), measures with two-subscript K'&SYSLIST(1,1),
    # generates `LR R2,R3` as a model statement, and emits `BC 07-&K,LABEL`
    # (mask = 7 - K'LR' = 5).  Each link was independently broken: the
    # sublist rendered as Python repr (re-pass lost it), &SYSLIST(1,1) didn't
    # parse, and the BC mask `07-2` was rejected.  Expect LR R2,R3 = 1AE3.
    spobj = td / "subpass.obj"
    rc, out = assemble(
        ["-o", str(spobj),
         "-L", str(FIX / "maclib_subpass"), str(FIX / "subpass_main.asm")],
        timeout=60)
    check("subpass_assembles", rc == 0,
          f"sublist re-pass + two-subscript K' + BC mask must assemble: "
          f"rc={rc}\n{out.strip()[-400:]}")
    spb = spobj.read_bytes() if spobj.exists() else b""
    check("subpass_emits_generated_instr", b"\x1a\xe3" in spb,
          "the model statement built from the re-passed sublist must emit "
          "LR R2,R3 (1AE3); a lost sublist or unparsed subscript drops it")

    # Two-subscript &SYSLIST(i,j) used DIRECTLY in a model statement (the
    # DOPROC `LR &SYSLIST(&I,1),&SYSLIST(&I,2)` shape, distinct from the
    # subpass path's SETC-then-emit).  nameSet0 dropped the (i,j) so
    # svReplace rendered &SYSLIST bare (whole nested arg list, Python repr)
    # with "(i,j)" left as garbage; now both subscripts apply.  Expect
    # MODELSUB (R2,R3) -> LR R2,R3 = 1AE3.
    msobj = td / "modeltwosub.obj"
    rc, out = assemble(
        ["-o", str(msobj),
         "-L", str(FIX / "maclib_modeltwosub"),
         str(FIX / "modeltwosub_main.asm")], timeout=60)
    check("model_two_subscript_assembles", rc == 0,
          f"two-subscript in a model statement must assemble: "
          f"rc={rc}\n{out.strip()[-400:]}")
    msb = msobj.read_bytes() if msobj.exists() else b""
    check("model_two_subscript_emits", b"\x1a\xe3" in msb,
          "&SYSLIST(1,1),&SYSLIST(1,2) on (R2,R3) must emit LR R2,R3 (1AE3)")

    # PROC/EXECUTE/ENDPROC structured-macro DIAGNOSTICS.  These macros are a
    # negative test: they DELIBERATELY emit `MNOTE 4` for malformed input
    # (omitted/over-8-char procedure name or return register, and a return
    # register that disagrees with a previous EXECUTE/ENDPROC).  This pins
    # the macro engine's diagnostics -- the MLIB80 PROCTEST driver deck is a
    # pure diagnostic deck and can never reach rc=0 at the default tolerance
    # BY DESIGN; its pass criterion is "produces exactly these diagnostics".
    # The deck drives every error path once (4 name, 4 return-register, 2
    # mismatch = 10) and otherwise assembles clean, so:
    #   * --tolerable 4 -> rc=0 (the sev-4 MNOTEs are the only diagnostics)
    #   * --tolerable 3 -> rc=1 (they really are severity 4, not lower)
    # A regression that dropped a check, fired a spurious one, or changed the
    # severity would move one of these counts.
    pmlst = td / "proctest.lst"
    rc4, out4 = assemble(
        ["--tolerable", "4", "-o", str(td / "proctest.obj"),
         "-l", str(pmlst),
         "-L", str(FIX / "maclib_proc"), str(FIX / "proctest_main.asm")],
        timeout=60)
    check("proctest_diagnostics_tolerated_at_4", rc4 == 0,
          f"the 10 intended sev-4 MNOTEs (and nothing else) must let the deck "
          f"assemble at --tolerable 4: rc={rc4}\n{out4.strip()[-400:]}")
    rc3, out3 = assemble(
        ["--tolerable", "3", "-o", str(td / "proctest3.obj"),
         "-L", str(FIX / "maclib_proc"), str(FIX / "proctest_main.asm")],
        timeout=60)
    check("proctest_diagnostics_are_severity_4", rc3 != 0,
          f"the PROC MNOTEs must be severity 4 (abort below 4): rc={rc3}")
    # Count the three distinct diagnostics in the listing.
    pmtext = pmlst.read_text(errors="replace") if pmlst.exists() else ""
    n_name = pmtext.count("PROCEDURE NAME IS OMITTED OR GREATER THAN 8")
    n_reg = pmtext.count("RETURN REGISTER IS OMITTED OR GREATER THAN 8")
    n_mism = pmtext.count("RETURN REGISTER DOES NOT MATCH THAT DEFINED")
    check("proctest_name_diagnostics", n_name == 4,
          f"expected 4 procedure-name MNOTEs (2 omitted + 2 over-8), got {n_name}")
    check("proctest_return_register_diagnostics", n_reg == 4,
          f"expected 4 return-register MNOTEs (2 omitted + 2 over-8), got {n_reg}")
    check("proctest_mismatch_diagnostics", n_mism == 2,
          f"expected 2 return-register-mismatch MNOTEs, got {n_mism}")

    # A null macro parameter (omitted operand, or the explicitly-empty `,,`
    # positional) used in ARITHMETIC context is 0, not an error -- the
    # MLIB80/BMTENT `AIF (&DLAYFLG NE 0)` shape that aborted FCMBMTPG when
    # &DLAYFLG was passed empty.  NULLP branches on &B NE 0 and &C EQ 2;
    # each invocation must take the null-is-zero arm, and a supplied value
    # must still compare normally.  Expected emission, in order:
    #   NULLP X,,2 -> 0B0B C2C2   (explicit empty mid-list)
    #   NULLP X    -> 0B0B 0C0C   (fully omitted)
    #   NULLP X,1,2-> B1B1 C2C2   (supplied values unaffected)
    npobj = td / "nullparm.obj"
    rc, out = assemble(
        ["-o", str(npobj), str(FIX / "nullparm_arith.asm")], timeout=60)
    check("nullparm_arith_assembles", rc == 0,
          f"null macro parameter in AIF arithmetic must be 0, not an error: "
          f"rc={rc}\n{out.strip()[-400:]}")
    npb = npobj.read_bytes() if npobj.exists() else b""
    # SPON/SPOFF -> ' PROT' control-card capture.  Expected halfword ranges
    # (hex, end-exclusive): PROTA protects hw 1-3 and 4-5 (the SPON at hw 4
    # runs to end of csect); PROTB opens protected (state persists across the
    # CSECT switch) for hw 0-1; PROTC is managed with nothing protected
    # (bare card).  Byte offsets: each DC X'nnnn' is one halfword.
    spobj = td / "sponprot.obj"
    rc, out = assemble(["-o", str(spobj), str(FIX / "feat_spon_prot.asm")],
                       timeout=60)
    check("spon_prot_assembles", rc == 0, f"rc={rc}\n{out.strip()[-300:]}")
    from ap101Utils import objModule as _om
    prots = {}
    if spobj.exists():
        for r in _om.ObjectFile(str(spobj)).controlStatements:
            t = r.text.split()
            if t and t[0] == "PROT":
                prots[t[1]] = t[2] if len(t) > 2 else ""
    check("spon_prot_ranges",
          prots.get("PROTA") == "1-3,4-5" and prots.get("PROTB") == "0-1"
          and prots.get("PROTC") == "",
          f"PROT cards: {prots!r} (want PROTA='1-3,4-5' PROTB='0-1' "
          f"PROTC='')")

    check("nullparm_arith_branches",
          b"\x0b\x0b\xc2\xc2" in npb and b"\x0b\x0b\x0c\x0c" in npb
          and b"\xb1\xb1\xc2\xc2" in npb,
          "expected 0B0BC2C2 (empty mid-list), 0B0B0C0C (omitted), "
          "B1B1C2C2 (supplied) in the object text")

    # Subscripted SET symbols with NO LCLC/GBLC declaration -- FCMBMTMC's
    # comfault-mask tables (`&APLHRM(1) SETC 'E0000000'` ... with no
    # declaration at all, read back as X'&APLHRM(&AHRINDX)').  `&X(k) SETx`
    # must implicitly declare a local ARRAY and grow it on write (incl. the
    # multi-value consecutive-element form).  GENMASK 1 -> E000 (element 1,
    # implicit declaration); GENMASK 3 -> F800 (multi-value growth).
    saobj = td / "setcarr.obj"
    rc, out = assemble(
        ["-o", str(saobj), str(FIX / "feat_setc_implicit_array.asm")],
        timeout=60)
    check("setc_implicit_array_assembles", rc == 0,
          f"undeclared subscripted SETC must implicitly declare an array: "
          f"rc={rc}\n{out.strip()[-400:]}")
    sab = saobj.read_bytes() if saobj.exists() else b""
    check("setc_implicit_array_values", b"\xe0\x00\xf8\x00" in sab,
          "expected E000 (element 1) then F800 (multi-value growth to "
          "element 3) in the object text")

    # A macro argument whose '&' rides ONLY the continuation card (FCMBMTMC's
    # `BMTENT ...,` / `  0C,&AERRLBL` shape) must still substitute -- the
    # guard must test the merged fields, not the first card image.  And the
    # continued invocation's dangling continuation flag must not swallow the
    # first EXPANDED statement in codegen (the generated DC is the very first
    # statement of BENT's expansion here).  Expected: X1 DC XL.8'0C',
    # YL.8(FOO-TAB) -> 0C01.
    ccobj = td / "contcard.obj"
    rc, out = assemble(
        ["-o", str(ccobj), str(FIX / "feat_cont_card_macarg.asm")],
        timeout=60)
    check("cont_card_macarg_assembles", rc == 0,
          f"variable on a continuation card must substitute: "
          f"rc={rc}\n{out.strip()[-400:]}")
    ccb = ccobj.read_bytes() if ccobj.exists() else b""
    check("cont_card_macarg_expansion_emitted", b"\x0c\x01" in ccb,
          "expected 0C01 (XL.8'0C',YL.8(FOO-TAB)): the expanded DC must not "
          "be absorbed as a continuation card")

    # virtualagc/virtualagc#1331 (ASM101S macro-processing bugs) as
    # regression pins.  Multilevel sublists survive verbatim and extra
    # subscripts index into them (Assembler H GC26-3758-3 p.13); expected
    # values were cross-checked against z390's mz390 in the issue thread.
    # ASM101S rendered RON's third invocation as
    # (10,((,(100,((,,200),(,,300))),)),30).
    snlst = td / "syslist_nesting.lst"
    rc, out = assemble(
        ["--tolerable", "255", "-o", str(td / "syslist_nesting.obj"),
         "-l", str(snlst), str(FIX / "feat_macro_syslist_nesting.asm")],
        timeout=60)
    check("syslist_nesting_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    sntext = snlst.read_text(errors="replace") if snlst.exists() else ""
    for want in [
        "+(10,20,30)",                            # flat sublist verbatim
        "+(10,(100,200,300),30)",                 # nested + GBLA substitution
        "SYSLIST(2)     = >(10,(100,200,300),30)<",
        "SYSLIST(2,1)   = >10<",
        "SYSLIST(2,2)   = >(100,200,300)<",
        "SYSLIST(2,2,1) = >100<",
        "SYSLIST(2,2,3) = >300<",
        "SYSLIST(2,9)   = ><",                    # past the end -> null
        "SYSLIST(9)     = ><",
        "SYSLIST(1,1)   = >1<",                   # scalar = 1-element sublist
        "N SYSLIST=3 N(1)=1 N(2)=3 N(3)=3",       # omitted elements count
    ]:
        check(f"syslist_nesting[{want.strip('+')[:24]}]", want in sntext,
              f"missing {want!r} in listing")

    # EBCDIC collation ('A'<'1', shorter-is-less), null &SYSLIST(1) = 0 in
    # arithmetic (the FPMSWTCH `AIF (&SYSLIST(1) LE 0 ...)` guard), keyword
    # arguments binding (ASM101S dropped ACALL=YES and quoted TITLE=), and
    # operand scan after the continuation join.
    mrlst = td / "macro_relations.lst"
    rc, out = assemble(
        ["--tolerable", "255", "-o", str(td / "macro_relations.obj"),
         "-l", str(mrlst), str(FIX / "feat_macro_relations.asm")],
        timeout=60)
    check("macro_relations_assembles", rc == 0,
          f"rc={rc}\n{out.strip()[-400:]}")
    mrtext = mrlst.read_text(errors="replace") if mrlst.exists() else ""
    check("macro_relations_collation",
          "FAIL:" not in mrtext and mrtext.count("OK:") == 3,
          f"collation MNOTEs wrong: "
          f"{[l for l in mrtext.splitlines() if 'FAIL:' in l or 'OK:' in l]}")
    for want, label in [
        ("CC INVALID: ><", "null_cc_is_zero"),
        ("CC VALID: >3<", "numeric_cc_in_range"),
        ("CC INVALID: >8<", "numeric_cc_out_of_range"),
        ("AMAIN NAME=ACOS ACALL=YES TITLE=", "keyword_arg_binds"),
        ("AMAIN NAME= ACALL=NO TITLE='TEST ROUTINE'",
         "quoted_keyword_arg_binds"),
        ("IFPROC P1=>(CH,R4,GE,TPCTPRI)< P7=>ZZ< P9=>LAST< NS=10",
         "continued_invocation_operands"),
    ]:
        check(f"macro_relations_{label}", want in mrtext,
              f"missing {want!r} in listing")

    # A whole sublist used as an arithmetic term is a program error to
    # diagnose (HLASM SC26-4940 Table 58), not a Python TypeError -- the
    # opening traceback of the issue.
    rc, out = assemble(
        ["-o", str(td / "sublist_diag.obj"),
         str(FIX / "feat_macro_sublist_arith_diag.asm")], timeout=60)
    check("sublist_arith_diagnosed",
          rc != 0 and "Traceback" not in out
          and "Cannot be interpreted as an integer value" in out,
          f"want clean intolerable diagnostic: rc={rc}\n{out.strip()[-400:]}")


def _xref_value(listing_path, symbol):
  """Pull a symbol's VALUE (hex halfword address) from the cross reference."""
  try:
    text = Path(listing_path).read_text(errors="replace")
  except OSError:
    return None
  for line in text.splitlines():
    m = re.match(r"\s*" + re.escape(symbol) + r"\s+\d+\s+([0-9A-Fa-f]+)\s", line)
    if m:
      return int(m.group(1), 16)
  return None


def _listing_code(listing_path, label):
  """Return the object-code hex field of the listing line whose statement
  carries `label` (e.g. 'C286'), or None.  Listing line format is:
      <hwaddr> <objcode>   <stmt#> <label> <op> ...
  """
  try:
    text = Path(listing_path).read_text(errors="replace")
  except OSError:
    return None
  for line in text.splitlines():
    # <hwaddr> <objcode> [<target> <operand> ...] <stmt#> <label> <op> ...
    # The bracketed operand/target fields appear for instructions (e.g.
    # branches) but not for plain data, so skip over them lazily.
    m = re.match(
        r"\s*[0-9A-Fa-f]+ ([0-9A-Fa-f]+)\b.*?\s\d+ "
        + re.escape(label) + r"(?:\s|$)", line)
    if m:
      return m.group(1).upper()
  return None


def _pool_literal(listing_path, operand):
  """Return (hwaddr, objcode) for the literal-pool dump line whose operand is
  `operand` (e.g. "=Y(LATE)"), or (None, None).  The dump (one line per pooled
  literal, printed after each LTORG) has the format:
      <hwaddr> <objcode>                          <operand>
  """
  try:
    text = Path(listing_path).read_text(errors="replace")
  except OSError:
    return (None, None)
  for line in text.splitlines():
    m = re.match(r"\s*([0-9A-Fa-f]{5}) ([0-9A-Fa-f]+)\s+"
                 + re.escape(operand) + r"\s*$", line)
    if m:
      return (m.group(1).upper(), m.group(2).upper())
  return (None, None)


def _rld_entries(obj_path):
  """Decode the RLD cards of an asm101 object module into a list of
  (relId, posId, flags, address) tuples.  Cards are 80-byte EBCDIC images;
  'RLD' is EBCDIC 0xD9D3C4; each entry is relId(2) posId(2) flag(1) addr(3),
  and the RLD-data byte count is in card columns 11-12 (index 10-11)."""
  try:
    data = Path(obj_path).read_bytes()
  except OSError:
    return []
  out = []
  for i in range(0, len(data), 80):
    c = data[i:i + 80]
    if c[1:4] != b"\xd9\xd3\xc4":
      continue
    n = (c[10] << 8) | c[11]
    o = 16
    while o + 8 <= 16 + n:
      out.append((int.from_bytes(c[o:o + 2], "big"),
                  int.from_bytes(c[o + 2:o + 4], "big"),
                  c[o + 4],
                  int.from_bytes(c[o + 5:o + 8], "big")))
      o += 8
  return out


def _asmg_symbols(obj_path):
  """The 'symbols' list of the asmg.json debug sidecar asm101 writes beside
  its object, each {name, kind, section, offset, ...}."""
  side = Path(obj_path).with_suffix(".asmg.json")
  try:
    return json.loads(side.read_text())["symbols"]
  except (OSError, ValueError, KeyError):
    return []


def _esd_entries(obj_path):
  """Decode the ESD cards into (esdId, typeName, name) tuples in ESD-ID order.
  'ESD' is EBCDIC 0xC5E2C4; each 16-byte item is name(8) type(1) ..., the
  first item's ESD ID is in card index 14-15, the byte count in index 10-11."""
  from ap101Utils.asciiToEbcdic import asciiToEbcdic
  e2a = {v: chr(k) for k, v in enumerate(asciiToEbcdic)}
  TYPES = {0: "SD", 1: "LD", 2: "ER", 3: "PC", 4: "CM", 5: "XD", 6: "WX"}
  try:
    data = Path(obj_path).read_bytes()
  except OSError:
    return []
  out = []
  for i in range(0, len(data), 80):
    c = data[i:i + 80]
    if c[1:4] != b"\xc5\xe2\xc4":
      continue
    n = ((c[10] << 8) | c[11]) // 16
    first = (c[14] << 8) | c[15]
    for j in range(n):
      o = 16 + j * 16
      name = "".join(e2a.get(b, ".") for b in c[o:o + 8]).rstrip()
      out.append((first + j, TYPES.get(c[o + 8], c[o + 8]), name))
  return out


def main():
  try:
    unit_tests()
  except Exception as e:  # an import/parse blow-up is itself a failure
    import traceback
    _failures.append("unit_tests raised: " + "".join(
        traceback.format_exception_only(type(e), e)).strip())
  integration_tests()
  maclib_tests()

  total = _passes + len(_failures)
  if _failures:
    print(f"FAILED {len(_failures)}/{total} asm101 feature checks:")
    for f in _failures:
      print("  - " + f)
    sys.exit(1)
  print(f"OK: all {total} asm101 feature checks passed")


if __name__ == "__main__":
  main()
