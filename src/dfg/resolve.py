#!/usr/bin/env python3
#
# Resolve dynamic-field references against the INCLUDEd compools' templates.
#
# Services, all keyed off `compool.allocate()`'s type model:
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
def name_type(info):
    """The HAL `NAME <type>` string for a PADR referent, or None if unresolved.
    A PADR points at ONE element, so VECTOR/MATRIX/untyped all reduce to SCALAR
    (HAL's untyped default) — the type must match the referent's or PASS2
    rejects the INITIAL with DI108.  A STRUCTURE-instance referent (the MDT
    S99X/S99Z tables) needs `NAME <template>-STRUCTURE`; for a MULTI-COPY
    referent we add the copy count (`-STRUCTURE(2)`) — PASS2 rejects the
    copyless form with DI110 (copiness mismatch), and the copy-qualified
    pointer is object-identical (same base address, same RLD)."""
    if info is None:
        return None
    if info.get("struct") and not info.get("type"):
        n = info.get("array") or 1
        return ("%s-STRUCTURE(%d)" % (info["struct"], n) if n > 1
                else "%s-STRUCTURE" % info["struct"])
    t = info.get("type")
    if t == "BIT":
        return "BIT(%d)" % info.get("n", 16)
    if t == "BOOLEAN":
        return "BOOLEAN"
    if t == "CHARACTER":
        return "CHARACTER(%d)" % info.get("n", 1)
    if t in ("SCALAR", "VECTOR", "MATRIX", None):
        return "SCALAR DOUBLE" if info.get("prec") == "DOUBLE" else "SCALAR"
    if t == "INTEGER":
        return "INTEGER DOUBLE" if info.get("prec") == "DOUBLE" else "INTEGER"
    return t


def includes(ds):
    """The deck's INCLUDE'd compool names, in deck order."""
    return [v.strip() for k, v in ds if k == "INCLUDE" and v]


def _models_and_fields(ds):
    """(compool list, {cp: model}, {cp: field-instance index}) for a deck."""
    compools = includes(ds)
    models = {cp: ca.allocate(cp) for cp in compools}
    fidx = {cp: ca.struct_field_instances(m) if m else {}
            for cp, m in models.items()}
    return compools, models, fidx


def _find_info(base, compools, models, fidx):
    """The compool info dict for a referent base name: a top-level var first,
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
    """The REPLACE-macro body for `name`, or None.  Templates carry the
    REPLACE macros; the declaration exists only under the body name."""
    for cp in compools:
        b = ca.replace_aliases(cp).get(name)
        if b is not None:
            return b.strip()
    return None


def _find_member_info(name, compools, models, fidx):
    """Field info for a `struct.member` reference (else `_find_info` of the base).

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
    if cands and all((fi.get("type"), fi.get("n")) ==
                     (cands[0][1].get("type"), cands[0][1].get("n")) for _, fi in cands):
        return cands[0][1]                        # all instances agree on type/width
    return None


def base_of(name):
    """The bare variable of a possibly-qualified/subscripted reference.
    An `@n` suffix (a DMDUPD watch's bit offset, e.g. `VAR@5`) is
    generator-side only and never part of the referent."""
    return name.split(".")[0].split("$")[0].split("(")[0].split("@")[0].strip()


def char_inits(ds):
    """{char-var: INITIAL string} from the INCLUDEd compool templates —
    the RTC string tables whose INITIAL text sizes their draw."""
    out = {}
    for cp in includes(ds):
        text = ca.read_template_text(cp)
        if not text:
            continue
        # no DECLARE anchor: factored declares list continuation names bare
        # (`DECLARE A CHARACTER(2) INITIAL('* '), B CHARACTER(2) INITIAL('-+');`)
        for m in re.finditer(r"(\w+)\s+CHARACTER\s*\(\s*\d+\s*\)"
                             r"\s+INITIAL\s*\(\s*'([^']*)'\s*\)", text):
            out.setdefault(m.group(1), m.group(2))
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
        info = _find_member_info(name, compools, models, fidx)
        ty = name_type(info)
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
        info = _find_member_info(name, compools, models, fidx)
        if info is None:
            return None
        if info.get("type") == "BIT":
            n = info.get("n", 16)
        elif info.get("type") == "BOOLEAN":
            n = 1
        else:
            return None                          # only bit discretes use this w0
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
