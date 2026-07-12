#!/usr/bin/env python3
#
# Compool TYPE resolution from the compiler's TEMPLIB — the binary TEMPLATE
# members carrying each compool's normalized, fully-resolved declarations.
# TEMPLIB is the generator's ONLY source of compool information, and only
# TYPES are read — never offsets or addresses (placement is the compiler's,
# via the emitted `NAME(var)` initializers).
#
# Why a preprocessor needs types at all — where they go:
#   * resolve.padr_types — a PADR is emitted as `DECLARE ... NAME <type>
#     INITIAL(NAME(var));`; HAL/S requires <type> to match the referent
#     (PASS2 DI108/DI109, structure copiness DI110).
#   * resolve.w0_resolver — the RTC/TEST/BLT condition words encode the
#     tested discrete's hardware bit position, which depends on its declared
#     BIT(n) width: HEX data words the compiler never sees.
#   * resolve.char_inits — an RTC rate budget is derived only when its
#     string table is declared with an INITIAL.
#   * resolve.alias_body — a deck may reference a REPLACE-macro alias; the
#     template carries the macro, and the referent is emitted as its body
#     (the declared name).
#
# Template format: `<TEMPLIB>/@@<7-char>`, EBCDIC binary card records
# (`cards.ebcdic_text`); tokens flow across cards.  Grammar (`;`-terminated):
#   NAME: EXTERNAL COMPOOL RIGID ;
#   STRUCTURE sname [RIGID|DENSE] : 1 field TYPE , 1 field TYPE , ... ;
#   DECLARE v TYPE [INITIAL(...)] [, v2 TYPE ...] ;
# TYPE ::= INTEGER [SINGLE|DOUBLE] | BIT(n) | BOOLEAN | EVENT LATCHED | CHARACTER(n)
#        | SCALAR [SINGLE|DOUBLE] | NAME ... | [ARRAY(k)] base | sname-STRUCTURE[(k)]
#
import os
import re

from ap101Utils import cards


def _default_templib():
    """gen/TEMPLIB under the OI340600 build tree, or $DFG_TEMPLIB."""
    env = os.environ.get("DFG_TEMPLIB")
    if env:
        return env
    here = os.path.dirname(__file__)
    return os.path.join(here, "..", "..", "build", "OI340600", "gen", "TEMPLIB")


TEMPDIR = _default_templib()


def set_templib(path):
    """Point the resolver at a different TEMPLIB (clears the caches)."""
    global TEMPDIR, _GLOBAL_STRUCTS
    TEMPDIR = path
    _GLOBAL_STRUCTS = None
    _REPLACE_CACHE.clear()


# A few decks INCLUDE a compool by a legacy alias that is neither the compool's
# HAL name nor its compressed member name (the DFG carried an external alias
# table).  Map those to the real compool name.
_ALIASES = {
    "CGZ_FL2_8": "CGZ_FL2_MC2_8",
    "CVY_ADT_COMPOOL": "CVY_ADT_DATA",
    "CGB_IH1_HFE_INPUT_BUFFER": "CGB_IH1_HFE1_INPUT_BUFFER",
    # The CVXARFCO member's real unit (its leading CVX_ARF_FALSE_COMPOOL is a
    # macro-stub shell).  Without the alias the CVXV_* type lookups miss and
    # the emitted PADR falls back to NAME BIT(16), which PASS2 rejects with
    # DI109 when the referent is typed.
    "CVX_ARF_COMPOOL": "CVX_ARF_COMMON_COMPOOL",
}


def template_name(compool):
    """Compiler compresses a compool name to @@ + 7 chars (underscores
    dropped as needed).  A DFG `INCLUDE=` names a compool either by its full HAL
    name (`CZ2_COMMON`) or by that compressed MEMBER name (`CZ2COM`); resolve
    both — first try the member directly (@@<name>), then scan TEMPLIB for the
    member whose decoded text starts with '<compool>:'."""
    want = _ALIASES.get(compool.strip().upper(), compool.strip().upper())
    for cand in ("@@" + want, "@@" + want.replace("_", "")):
        p = os.path.join(TEMPDIR, cand)
        if os.path.exists(p):
            return p
    if not os.path.isdir(TEMPDIR):
        return None
    for fn in os.listdir(TEMPDIR):
        if not fn.startswith("@@"):
            continue
        head = open(os.path.join(TEMPDIR, fn), "rb").read(90).decode("ebcdicvagc")
        if head.lstrip().startswith(want + ":") or head.lstrip().startswith(want + " :"):
            return os.path.join(TEMPDIR, fn)
    return None


def read_template_text(compool):
    path = template_name(compool)
    if not path:
        return None
    text = cards.ebcdic_text(open(path, "rb").read())
    return re.sub(r"\s+", " ", text).strip()


_REPLACE_CACHE = {}


def replace_aliases(compool):
    """{alias: body} REPLACE macros carried by the compool's template.
    A deck may reference an alias (`CGZV_HS_X_HAC`) whose declaration exists
    only under the body name (`CGZV_HS_X_COORD$3`)."""
    key = compool.strip().upper()
    if key not in _REPLACE_CACHE:
        _REPLACE_CACHE[key] = dict(re.findall(
            r'REPLACE\s+(\w+)\s+BY\s+"([^"]+)"',
            read_template_text(compool) or ""))
    return _REPLACE_CACHE[key]


def tokenize(text):
    # keep ( ) , : ; - as separate tokens; HEX '...' / '...' as one token
    text = re.sub(r'"[^"]*"', " ", text)            # drop REPLACE-macro bodies
    text = re.sub(r"'[^']*'", " ", text)            # drop string literals
    return re.findall(r"[A-Za-z0-9_#]+|[():;,+*-]", text)


_PRIM_KW = frozenset(("BIT", "BOOLEAN", "INTEGER", "SCALAR", "CHARACTER",
                      "EVENT", "NAME", "MATRIX", "VECTOR"))
_TYPE_KW = _PRIM_KW | {"ARRAY"}
# tokens that terminate a type spec ⇒ the declaration has no explicit type
# (HAL/S default is SCALAR): separators + trailing data attributes.
_TYPE_END = frozenset((",", ";", ")", "INITIAL", "CONSTANT", "LOCK", "ACCESS",
                       "AUTOMATIC", "STATIC", "REMOTE", "ALIGNED", "RIGID",
                       "DENSE", "LATCHED", "NONHAL", "ACCESSED"))


def parse(tokens):
    """Return (structures, decls).  structures[name] = the structure's LEAF
    members as [(field, typeinfo)] (group nodes excluded, nesting flattened);
    decls = [(name, typeinfo)]."""
    structs = {}
    decls = []
    i = 0
    n = len(tokens)

    def parse_dims(j):
        """Consume a parenthesised, comma-separated dimension list starting at
        the '(' token; return (product_of_numeric_dims, j_after_close).  A
        symbolic dimension (named constant) contributes nothing to the
        product; within-dim arithmetic and '*' are skipped."""
        prod = 1
        j += 1                                      # past '('
        cur, have = 0, False

        def flush():
            nonlocal prod, cur, have
            if have:
                prod *= cur
            cur, have = 0, False
        while j < n and tokens[j] != ")":
            t = tokens[j]
            if t == ",":
                flush()
            elif t.isdigit():
                cur = cur + int(t) if have else int(t); have = True
            j += 1
        flush()
        return prod, j + 1                           # skip ')'

    def parse_typeinfo(j):
        """Parse a type spec starting at j; return (typeinfo, j_end)."""
        info = {"array": 1}
        if tokens[j] == "ARRAY":
            j += 1                                  # past ARRAY
            info["array"], j = parse_dims(j)
        # structure instance:  sname - STRUCTURE [ ( k ) ]
        if j + 1 < n and tokens[j + 1] == "-" and tokens[j + 2] == "STRUCTURE":
            info["struct"] = tokens[j]; j += 3
            if j < n and tokens[j] == "(":
                dim, j = parse_dims(j)
                info["array"] *= dim
            return info, j
        if j >= n or tokens[j] in _TYPE_END:
            return info, j                          # implicit type (HAL default
            #                                         SCALAR) — no type token
        t = tokens[j]; info["type"] = t; j += 1
        if t in ("DOUBLE", "SINGLE"):               # bare precision ⇒ SCALAR
            info["type"] = "SCALAR"; info["prec"] = t
        elif t in ("INTEGER", "SCALAR"):
            if j < n and tokens[j] in ("SINGLE", "DOUBLE"):
                info["prec"] = tokens[j]; j += 1
        elif t in ("BIT", "CHARACTER"):
            if j < n and tokens[j] == "(":
                info["n"] = int(tokens[j + 1]); j += 3
        elif t in ("MATRIX", "VECTOR"):             # reduce to SCALAR-per-element
            if j < n and tokens[j] == "(":
                _, j = parse_dims(j)
            if j < n and tokens[j] in ("SINGLE", "DOUBLE"):
                info["prec"] = tokens[j]; j += 1
        elif t == "EVENT":
            if j < n and tokens[j] == "LATCHED":
                j += 1
        elif t == "NAME":                           # NAME <referent-type...>
            # A NAME is a 1-word pointer; the trailing type describes the
            # REFERENT, not the name itself.  Consume it to advance.
            _, j = parse_typeinfo(j)
        return info, j

    def attr_before_sep(j, attr):
        """Is attribute token `attr` present before this item's terminating ,/;?"""
        depth = 0
        while j < n:
            t = tokens[j]
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
            elif depth == 0 and t in (",", ";"):
                return False
            elif t == attr:
                return True
            j += 1
        return False

    def skip_attrs(j):
        """Advance past trailing data attributes (INITIAL/CONSTANT/… with
        balanced value lists) to the next top-level ',' or ';'."""
        depth = 0
        while j < n:
            t = tokens[j]
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
            elif depth == 0 and t in (",", ";"):
                return j
            j += 1
        return j

    # header:  NAME : EXTERNAL COMPOOL RIGID ;
    while i < n and tokens[i] != ";":
        i += 1
    i += 1
    while i < n:
        if tokens[i] == "STRUCTURE":
            sname = tokens[i + 1]; i += 2
            while tokens[i] != ":":                 # header attrs: RIGID/DENSE
                i += 1
            i += 1
            raw = []                                # [(field, info)] leaves only
            while i < n and tokens[i] != ";":
                if tokens[i] == ",":
                    i += 1; continue
                level = int(tokens[i]) if tokens[i].isdigit() else 1
                i += 1                              # consume level number
                field = tokens[i]; i += 1
                node_dense = i < n and tokens[i] == "DENSE"
                if node_dense:
                    i += 1
                at_sep = i >= n or tokens[i] in (",", ";")
                # A sub-structure node (`1 GRP [DENSE] , 2 F1 ...`) has no type of
                # its own and is followed by DEEPER-level children.  Distinguish it
                # from an untyped LEAF (`1 QS , 1 QV ...` — QS is an implicit
                # SCALAR) by peeking the next member's level number.
                is_group = node_dense
                if at_sep and not node_dense:
                    k = i + 1 if (i < n and tokens[i] == ",") else i
                    nxt = int(tokens[k]) if k < n and tokens[k].isdigit() else level
                    is_group = nxt > level
                if is_group:
                    continue
                if at_sep:
                    info = {"array": 1}             # untyped leaf ⇒ implicit SCALAR
                else:
                    info, i = parse_typeinfo(i)
                raw.append((field, info))
                i = skip_attrs(i)                   # member attrs: REMOTE/LOCK/...
            structs[sname] = raw
            i += 1
        elif tokens[i] == "DECLARE":
            i += 1
            # Factored form:  DECLARE <type-attrs> INITIAL(...) , name1 , name2 ;
            # (the leading attribute group has no name and applies to every name).
            factored = None
            if i < n and tokens[i] in _TYPE_KW:
                factored, i = parse_typeinfo(i)
                i = skip_attrs(i)
                if i < n and tokens[i] == ",":
                    i += 1
            while i < n and tokens[i] != ";":
                if tokens[i] == ",":
                    i += 1; continue
                name = tokens[i]; i += 1
                if factored is not None:
                    info = dict(factored)
                    # a name in a factored DECLARE may carry its OWN array/type
                    # suffix (e.g. `EVENT, NAME1 ARRAY(7)`): apply it over the
                    # factored attributes.
                    if i < n and tokens[i] in _TYPE_KW:
                        own, i = parse_typeinfo(i)
                        if own.get("array", 1) != 1:
                            info["array"] = own["array"]
                        if own.get("type") in _PRIM_KW:
                            info["type"] = own["type"]
                            info.pop("struct", None)
                            for k in ("n", "prec"):
                                if k in own:
                                    info[k] = own[k]
                else:
                    info, i = parse_typeinfo(i)
                if attr_before_sep(i, "CONSTANT"):
                    info["constant"] = True         # named compile-time constant
                decls.append((name, info))
                i = skip_attrs(i)
                if i < n and tokens[i] == ",":
                    i += 1
            i += 1
        else:
            i += 1
    return structs, decls


_GLOBAL_STRUCTS = None


def global_structs():
    """Union of every template's struct definitions, so a compool that names a
    struct defined in another (INCLUDEd) compool can still be sized.  Cached."""
    global _GLOBAL_STRUCTS
    if _GLOBAL_STRUCTS is None:
        _GLOBAL_STRUCTS = {}
        for fn in sorted(os.listdir(TEMPDIR)):       # sorted = reproducible
            if not fn.startswith("@@"):
                continue
            try:
                text = cards.ebcdic_text(open(os.path.join(TEMPDIR, fn), "rb").read())
                s, _ = parse(tokenize(re.sub(r"\s+", " ", text).strip()))
                for k, v in s.items():
                    _GLOBAL_STRUCTS.setdefault(k, v)
            except Exception:
                pass
    return _GLOBAL_STRUCTS


def allocate(compool):
    """Return the type model {info, vstruct, structs} for a compool, or None
    if the template is missing.  A simple scalar CONSTANT is a compile-time
    literal (inlined by the compiler, no storage — and so not a referenceable
    variable); an aggregate CONSTANT (ARRAY/STRUCTURE/CHARACTER data table)
    IS allocated and stays visible."""
    text = read_template_text(compool)
    if not text:
        return None
    structs, decls = parse(tokenize(text))
    # resolve struct instances whose type is defined in another (INCLUDEd) compool
    if any("struct" in info and info["struct"] not in structs for _, info in decls):
        structs = {**global_structs(), **structs}     # local definitions win
    vstruct, vinfo = {}, {}
    for name, info in decls:
        if info.get("constant") and info.get("array", 1) == 1 \
                and "struct" not in info \
                and info.get("type") in (None, "INTEGER", "SCALAR", "BIT", "BOOLEAN"):
            continue
        vinfo[name] = info
        if "struct" in info:
            vstruct[name] = info["struct"]
    return {"vstruct": vstruct, "info": vinfo, "structs": structs}


def struct_field_instances(model):
    """Reverse index for resolving a *bare* struct-member reference used in a
    deck (a field of a struct rather than a top-level var).  Map field name ->
    [(instance_var, field_info)] for every leaf member of a struct that has a
    top-level instance.  One entry resolves unambiguously; >1 signals
    ambiguity → caller skips."""
    inst = {}                                            # struct -> [instances]
    for var, sname in model["vstruct"].items():
        inst.setdefault(sname, []).append(var)
    out = {}
    for sname, ivars in inst.items():
        for fname, fi in model["structs"].get(sname, []):
            for ivar in ivars:
                out.setdefault(fname, []).append((ivar, fi))
    return out
