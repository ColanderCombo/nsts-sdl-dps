#!/usr/bin/env python3
#
# Compool TYPE resolution from the compiler's SDF library — the PASS3-written
# Simulation Data Files carrying each compilation unit's full symbol table.
# The SDFLIB is the generator's ONLY source of compool information, and only
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
#     SDF carries the macro, and the referent is emitted as its body
#     (the declared name).
#
# The type record is `ap101Utils.sdf.SdfSymbol`, straight out of the SDF —
# there is no intermediate model.  A compool's model is
#   {"info": {var: SdfSymbol}, "vstruct": {var: template-name},
#    "structs": {template-name: [(leaf-field, SdfSymbol)]}}
# HAL/S BOOLEAN arrives as BIT(1) (the SDF has no separate BOOLEAN code) —
# identical for dfg's uses (bit positions; NAME BIT(1) == BOOLEAN to PASS2).
#
import os

from ap101Utils.sdf import SdfLibrary


def _default_sdflib():
    """$DFG_SDFLIB (a directory, or an os.pathsep-separated search path);
    else gen/SDFLIB under the OI340600 build tree; else the union of the
    per-phase build/mmu/PHASE*/gen/SDFLIB directories (a con80build
    --build-all tree has no single SSW SDFLIB -- members shared across
    phases are byte-identical, so first-hit-wins is sound)."""
    env = os.environ.get("DFG_SDFLIB")
    if env:
        return env
    here = os.path.dirname(__file__)
    build = os.path.join(here, "..", "..", "build")
    ssw = os.path.join(build, "OI340600", "gen", "SDFLIB")
    if os.path.isdir(ssw) and any(f.startswith("##") for f in os.listdir(ssw)):
        return ssw
    import glob
    phases = sorted(glob.glob(os.path.join(build, "mmu", "PHASE*", "gen",
                                           "SDFLIB")))
    return os.pathsep.join(phases) if phases else ssw


_LIB = None


def set_sdflib(path):
    """Point the resolver at an SDF library (None re-selects the default);
    clears the caches."""
    global _LIB, _GLOBAL_STRUCTS
    _LIB = SdfLibrary(str(path) if path else _default_sdflib())
    _GLOBAL_STRUCTS = None
    _REPLACE_CACHE.clear()


def _lib():
    global _LIB
    if _LIB is None:
        _LIB = SdfLibrary(_default_sdflib())
    return _LIB


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


def _unit(compool):
    """The SdfUnit for a compool reference (full HAL name, compressed member
    name, or legacy alias), or None."""
    want = compool.strip().upper()
    return _lib().unit(_ALIASES.get(want, want))


_REPLACE_CACHE = {}


def replace_aliases(compool):
    """{alias: body} REPLACE macros carried by the compool's SDF.
    A deck may reference an alias (`CGZV_HS_X_HAC`) whose declaration exists
    only under the body name (`CGZV_HS_X_COORD$3`)."""
    key = compool.strip().upper()
    if key not in _REPLACE_CACHE:
        unit = _unit(compool)
        _REPLACE_CACHE[key] = unit.replaces() if unit is not None else {}
    return _REPLACE_CACHE[key]


def has_sdf(compool):
    """True when the SDF library has a member for the compool reference
    (full HAL name, compressed member name, or legacy alias).  Diagnostic
    probe: every dfg refusal ultimately traces to type/INITIAL data the
    SDFs carry, so a deck INCLUDE with no SDF member is the usual cause."""
    return _unit(compool) is not None


def char_initial_strings(compool):
    """{char-var: INITIAL string} for a compool — the RTC string tables,
    from the SDF's Initialization Table."""
    unit = _unit(compool)
    return unit.char_initials() if unit is not None else {}


def _keep(sym):
    """dfg policy: a simple scalar CONSTANT is a compile-time literal with no
    storage — not a referenceable variable — and is dropped from the model.
    An aggregate CONSTANT (ARRAY/STRUCTURE/CHARACTER data table) IS allocated
    and stays visible."""
    if not sym.constant or sym.copies != 1:
        return True
    if sym.kind == "STRUCTURE" and sym.template:
        return True
    return sym.kind not in (None, "INTEGER", "SCALAR", "BIT")


def allocate(compool):
    """Return the type model {info, vstruct, structs} for a compool
    (SdfSymbol-valued, see the module header), or None if it has no SDF
    member."""
    unit = _unit(compool)
    if unit is None:
        return None
    structs = {name: [(s.name, s) for s in leaves]
               for name, leaves in unit.structures().items()}
    vstruct, vinfo = {}, {}
    for sym in unit.variables():
        if not _keep(sym):
            continue
        vinfo[sym.name] = sym
        if sym.kind == "STRUCTURE" and sym.template:
            vstruct[sym.name] = sym.template
    # struct templates named in another (INCLUDEd) compool's SDF
    if any(t not in structs for t in vstruct.values()):
        for sname, leaves in global_structs().items():
            structs.setdefault(sname, leaves)
    return {"vstruct": vstruct, "info": vinfo, "structs": structs}


_GLOBAL_STRUCTS = None


def global_structs():
    """Union of every SDF member's structure templates, so a compool that
    instantiates a template defined in another (INCLUDEd) compool can still
    be resolved.  Cached."""
    global _GLOBAL_STRUCTS
    if _GLOBAL_STRUCTS is None:
        _GLOBAL_STRUCTS = {}
        for unit in _lib().units():
            for name, leaves in unit.structures().items():
                _GLOBAL_STRUCTS.setdefault(name, [(s.name, s) for s in leaves])
    return _GLOBAL_STRUCTS


def struct_field_instances(model):
    """Reverse index for resolving a *bare* struct-member reference used in a
    deck (a field of a struct rather than a top-level var).  Map field name ->
    [(instance_var, field SdfSymbol)] for every leaf member of a struct that
    has a top-level instance.  One entry resolves unambiguously; >1 signals
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
