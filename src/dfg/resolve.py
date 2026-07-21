#!/usr/bin/env python3
#
# Resolve dynamic-field references against the INCLUDEd compools' SDFs.
#
# Services, all keyed off `compool.allocate()`'s type model (SdfSymbol-valued;
# HAL/S BOOLEAN arrives as BIT(1), see compool.py):
#   * `padr_types(ds, names)` — the HAL `NAME <type>` each PADR must declare
#     (must match the referent's type, or PASS2 rejects the INITIAL list).
#   * `w0_resolver(ds)` — the RTC/TEST/BLT condition word: a discrete's
#     hardware bit position, from its declared bit width.
#   * `char_inits(ds)` — {char-var: INITIAL} for the RTC string tables.
#   * `alias_body(name, compools)` — a REPLACE-macro alias's body.
#
import re

from . import compool as ca


# ---- NAME <type> for a PADR pointer ----------------------------------------
def name_type(sym):
    """The HAL `NAME <type>` string for a PADR referent (an SdfSymbol), or
    None if unresolved.
    A PADR points at ONE element, so VECTOR/MATRIX/untyped all reduce to SCALAR
    (HAL's untyped default) — the type must match the referent's or PASS2
    rejects the INITIAL with DI108.  A STRUCTURE-instance referent (the MDT
    S99X/S99Z tables) needs `NAME <template>-STRUCTURE`; for a MULTI-COPY
    referent we add the copy count (`-STRUCTURE(2)`) — PASS2 rejects the
    copyless form with DI110 (copiness mismatch), and the copy-qualified
    pointer is object-identical (same base address, same RLD)."""
    if sym is None:
        return None
    kind = sym.kind
    if kind == "STRUCTURE" and sym.template:
        n = sym.copies
        return ("%s-STRUCTURE(%d)" % (sym.template, n) if n > 1
                else "%s-STRUCTURE" % sym.template)
    if kind == "BIT":
        return "BIT(%d)" % (sym.width or 16)
    if kind == "CHARACTER":
        return "CHARACTER(%d)" % (sym.width or 1)
    if kind in ("SCALAR", "VECTOR", "MATRIX", "STRUCTURE", None):
        # a template-less STRUCTURE reduces to HAL's untyped default too
        return "SCALAR DOUBLE" if sym.precision == "DOUBLE" else "SCALAR"
    if kind == "INTEGER":
        return "INTEGER DOUBLE" if sym.precision == "DOUBLE" else "INTEGER"
    return kind


def _type_width(sym):
    """(kind, effective width) — the referent identity used to test whether
    several candidate struct-field instances agree."""
    if sym.kind == "BIT":
        return "BIT", sym.width or 16
    if sym.kind == "CHARACTER":
        return "CHARACTER", sym.width or 1
    if sym.kind in ("INTEGER", "SCALAR", "VECTOR", "MATRIX", "EVENT"):
        return sym.kind, None
    return None, None


def includes(ds):
    """The deck's INCLUDE'd compool names, in deck order."""
    return [v.strip() for k, v in ds if k == "INCLUDE" and v]


def missing_sdf_includes(ds):
    """The deck's INCLUDE'd compools that have NO member in the SDF library,
    in deck order.  Diagnostic only: a missing SDF is the usual root cause of
    a w0 / rate-count refusal (the build must compile the compool — display
    INCLUDEs are not part of the link's own template closure)."""
    return [cp for cp in includes(ds) if not ca.has_sdf(cp)]


def _models_and_fields(ds):
    """(compool list, {cp: model}, {cp: field-instance index}) for a deck."""
    compools = includes(ds)
    models = {cp: ca.allocate(cp) for cp in compools}
    fidx = {cp: ca.struct_field_instances(m) if m else {}
            for cp, m in models.items()}
    return compools, models, fidx


def _find_info(base, compools, models, fidx):
    """The referent SdfSymbol for a base name: a top-level var first,
    then a unique struct-field instance."""
    for cp in compools:                          # (1) top-level declared var
        m = models.get(cp)
        if m and base in m.get("info", {}):
            return m["info"][base]
    for cp in compools:                          # (2) unique struct-field ref
        cands = fidx.get(cp, {}).get(base, [])
        if len(cands) == 1:
            return cands[0][1]                    # (ivar, fi) -> fi
    body = alias_body(base, compools)             # (3) REPLACE-macro alias
    if body is not None:
        return _find_info(base_of(body), compools, models, fidx)
    return None


def alias_body(name, compools):
    """The REPLACE-macro body for `name`, or None.  The SDFs carry the
    REPLACE macros; the declaration exists only under the body name."""
    for cp in compools:
        b = ca.replace_aliases(cp).get(name)
        if b is not None:
            return b.strip()
    return None


def _find_member_info(name, compools, models, fidx):
    """The referent SdfSymbol for a `struct.member` reference (else
    `_find_info` of the base).

    A qualified `INST.MEMBER` names the struct instance `INST` and its member; the
    member lives in `fidx` under every instance of that struct type, so we pick the
    candidate whose instance matches `INST` (falling back to a unique type/width if
    the instance isn't recorded).  This is what the RTC/TEST/BLT `w0` needs — the
    tested member's `BIT(n)` width, which `_find_info(base_of(...))` misses because
    `base_of` yields the struct, not the member."""
    if "." not in name:
        return _find_info(base_of(name), compools, models, fidx)
    inst = base_of(name)
    member = name.split(".")[-1].split("$")[0].split("(")[0].strip()
    cands = [(ivar, fi) for cp in compools
             for (ivar, fi) in fidx.get(cp, {}).get(member, [])]
    for ivar, fi in cands:                        # exact: the named instance
        if ivar == inst:
            return fi
    if cands and all(_type_width(fi) == _type_width(cands[0][1])
                     for _, fi in cands):
        return cands[0][1]                        # all instances agree on type/width
    return None


def base_of(name):
    """The bare variable of a possibly-qualified/subscripted reference.
    An `@n` suffix (a DMDUPD watch's bit offset, e.g. `VAR@5`) is
    generator-side only and never part of the referent."""
    return name.split(".")[0].split("$")[0].split("(")[0].split("@")[0].strip()


def char_inits(ds):
    """{char-var: INITIAL string} from the INCLUDEd compools —
    the RTC string tables whose INITIAL text sizes their draw."""
    out = {}
    for cp in includes(ds):
        for name, text in ca.char_initial_strings(cp).items():
            out.setdefault(name, text)
    return out


def padr_types(ds, names):
    """(name, NAME-type) per PADR name — the referent's TYPE only (the OFFSET is
    left to the compiler via the `NAME(var)` initializer).  A qualified
    `INST.MEMBER` referent must yield the MEMBER's type (`_find_member_info`),
    not the struct instance's (untyped -> SCALAR): PASS2 rejects the mismatch
    with DI109."""
    compools, models, fidx = _models_and_fields(ds)
    out = []
    for name in names:
        sym = _find_member_info(name, compools, models, fidx)
        ty = name_type(sym)
        # A `$k`-subscripted referent names ONE copy of a multi-copy
        # structure: the pointer is copyless (`-STRUCTURE`), or PASS2 raises
        # the DI110 copiness conflict.
        if ty and "$" in name and re.search(r"-STRUCTURE\(\d+\)$", ty):
            ty = ty[:ty.rindex("(")]
        out.append((name, ty))
    return out


# ---- RTC/TEST/BLT condition word (w0) --------------------------------------
# w0 carries a condition discrete's HARDWARE bit position in its 16-bit word.  A
# BIT(n) field is right-justified (low n bits), so its first hardware bit is
# (16-n) and the deck's 1-based bit selects within it -> bitpos = (16-n)+(bit-1).
# n>16 generalizes right-justification across ceil(n/16) halfwords: first bit =
# (-n)%16 and bitpos runs linearly past 15 into the next word.
#   RTC  (CASE 3):  0x0C00 | bitpos<<4 | ((len-1) | sign<<2)   len/sign nibble
#   TEST (CASE 18): 0x4800 | bitpos                            (typelen word=ref)
#   BLT  (CASE 1):  0x0400 | bitpos1<<5 | bitpos2              two 5-bit fields
_RTC_SIGN_C = {"": 0, "N": 0, "P": 1, "M": 3}


def w0_resolver(ds):
    """Return w0fn(op) -> int|None deriving an RTC/TEST/BLT/IF op's
    condition word from the INCLUDEd compools, reading the op's SPEC fields
    (None => refuse)."""
    compools, models, fidx = _models_and_fields(ds)

    def bitpos(name, bit):
        sym = _find_member_info(name, compools, models, fidx)
        if sym is None or sym.kind != "BIT":
            return None                          # only bit discretes use this w0
        n = sym.width or 16                      # BOOLEAN is BIT(1) in the SDF
        return (-n) % 16 + (bit - 1)

    def w0fn(op):
        if op.kind == "RTC":
            bp = bitpos(op.var, op.bit)
            length = op.len if op.len is not None else 1
            sign = op.sign if op.sign is not None else ""
            if bp is None or sign not in _RTC_SIGN_C:
                return None
            nib = ((length - 1) & 0x3) | (_RTC_SIGN_C[sign] << 2)
            return 0x0C00 | ((bp & 0x3F) << 4) | (nib & 0xF)
        if op.kind == "BLT":
            bp1 = bitpos(op.var, op.bit)
            bp2 = bitpos(op.var2, op.bit2)
            if bp1 is None or bp2 is None:
                return None
            return 0x0400 | ((bp1 & 0x1F) << 5) | (bp2 & 0x1F)
        bp = bitpos(op.var, op.bit)              # TEST / IF
        return None if bp is None else 0x4800 | (bp & 0x3FF)

    return w0fn
