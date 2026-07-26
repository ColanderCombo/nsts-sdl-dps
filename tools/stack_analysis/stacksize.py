#!/usr/bin/env python3
"""Prototype: compute @0<prog> stack CSECT sizes by longest weighted path
through the whole-load-module call DAG, validate against the DASS oracle.

Model (from reverse-engineering + spec/MLIB80 evidence):
  - Each code CSECT has a frame size = its SYM STACKEND value (BYTES).
  - A call edge B->C means block B references block C's code entry.
  - depth(B) = STACKEND(B) + max(depth(C) for C in callees(B)), a DAG longest path.
  - Calling a PROGRAM (block that has its own @0 stack) switches stacks => contributes 0.
  - @0<prog> size (hw) = ceil(depth($0<prog>) / 2).
"""
import sys, glob, re, os, collections
sys.path.insert(0, 'src')
from ap101Utils.objModule import ObjectFile, EsdType, SymRefType

OBJDIRS = ['build/phase02.out/obj',
           'build/lib/runtime/RUN', 'build/lib/runtime/ZCON']
DASS = '../pfs/PFS/mafgen/DASS_SSW_(PostIPL).ASC'

# ---- oracle (halfwords) ----
oracle = {}
pat = re.compile(r'(@0[A-Z0-9]{6})\s+\*\*\*\*\s+[0-9A-F]+\(\s*(\d+)\)\s+STACK')
for line in open(DASS, errors='replace'):
    m = pat.search(line)
    if m: oracle[m.group(1)] = int(m.group(2))
PROGSET = set(oracle)                       # @0<prog6> names = the programs

# ---- load objects ----
# block name -> frame bytes (STACKEND); block name -> set(callee block names)
frame = {}
edges = collections.defaultdict(set)
defmod = {}                                 # SD name -> module index (first definer)
mod_blocks = collections.defaultdict(list)  # module idx -> [code csect names]

def is_code(name):
    # program main $0, comsub code #C, entry #E, runtime #Q, nested <letter><digit>
    if name[:2] in ('$0', '#C', '#E', '#Q'):
        return True
    return len(name) >= 2 and name[0].isalpha() and name[1].isdigit()

qcon_target = {}                            # #Q<x> stub -> real routine name it points to
ld_owner = {}                               # LD (alt entry) name -> owning code CSECT
modules = []
allobj = [f for d in OBJDIRS for f in glob.glob(d + '/*.obj')]
for fn in sorted(allobj):
    of = ObjectFile(fn)
    for m in of.modules:
        mi = len(modules); modules.append(m)
        esd = {e.esdId: e for e in m.esdEntries}
        # STACKEND per code CSECT
        cur = None
        for s in m.symbols:
            if s.name is None: continue
            if s.symType == SymRefType.CONTROL: cur = s.name
            elif s.name == 'STACKEND' and cur is not None:
                frame[cur] = s.offsetInCSECT or 0
        sds = [e.name.strip() for e in m.esdEntries if e.type == EsdType.SD]
        ers = [e.name.strip() for e in m.esdEntries if e.type == EsdType.ER]
        for nm in sds:
            defmod.setdefault(nm, mi)
            if is_code(nm): mod_blocks[mi].append(nm)
        # a #Q<x> QCON stub (tiny SD) points at its one real routine via its ER
        for nm in sds:
            if nm[:2] == '#Q' and len(ers) == 1:
                qcon_target[nm] = ers[0]
        # LD (alternate entry, e.g. HREM in IREM) -> its owning code CSECT
        for e in m.esdEntries:
            if e.type == EsdType.LD:
                owner = esd.get(e.ldid)
                if owner is not None:
                    ld_owner[e.name.strip()] = owner.name.strip()
        # RLD edges: pos csect -> referenced symbol
        for r in m.relocations:
            pe, re_ = esd.get(r.posId), esd.get(r.relId)
            if pe is None or re_ is None: continue
            pn, rn = pe.name.strip(), re_.name.strip()
            # keep edges from ALL CSECTs: runtime routines have plain names
            # (CPASP, CPAS, ...) that is_code() can't recognize, yet they call
            # further routines (CPASP -> #QCPAS -> CPAS).  Edges from data
            # CSECTs are harmless: nothing resolves INTO a data CSECT, so they
            # are never reached by the longest-path walk.
            edges[pn].add(rn)

CODE_BLOCKS = set(frame) | {b for bs in mod_blocks.values() for b in bs}

def prog_name(block):  # $0AIESIP / #CDISPLA / A2AIESIP -> @0AIESIP
    return '@0' + block[2:]

def as_block(t):                                # map a target name to its code CSECT
    if t in ld_owner: t = ld_owner[t]           # alt entry (HREM/ETOH/CAS) -> owning SD
    return t if t in CODE_BLOCKS else None

def resolve(rn):
    """A referenced symbol -> the code block(s) it calls, or [] if data/none."""
    if rn[:2] == '#Q':                          # QCON stub -> real runtime routine
        t = as_block(qcon_target.get(rn, ''))
        return [t] if t else []
    if rn in ld_owner:                          # direct call of an alt entry (e.g. ETOH)
        t = as_block(rn)
        return [t] if t else []
    if rn in CODE_BLOCKS and (rn[:2] in ('$0', '#C') or
                              (rn[0].isalpha() and rn[1:2].isdigit())):
        return [rn]                             # direct block reference
    if rn[:2] == '#Z':                          # ZCON indirect -> comsub code
        c = '#C' + rn[2:]
        return [c] if c in CODE_BLOCKS else []
    if rn[:2] == '#E':                          # entry stub -> program/proc main
        for cand in ('$0' + rn[2:], '#C' + rn[2:]):
            if cand in CODE_BLOCKS:
                return [cand]
        mi = defmod.get(rn)
        return [b for b in mod_blocks.get(mi, ()) if b[:2] != '#E']
    return []

def callees(block):
    out = []
    for rn in edges.get(block, ()):
        out.extend(c for c in resolve(rn) if c != block)   # exclude self-loops
    return out

BASE = 36                                       # 18-hw standard save area (fields 1-5)

# ---- optional: override frames with the real compiler's (DASS prologue IAL) ----
# Validates the ALGORITHM independently of per-block codegen drift (the DASS was
# built from OF290103 source; we compile OI340600 — a few blocks legitimately
# differ).  Frame hw = the IAL R0,X'..' in each code CSECT's prologue.
frame_before = dict(frame)
if '--dass-frames' in sys.argv:
    sec = None
    hdr = re.compile(r'^\s*[0-9A-F]{6}-[0-9A-F]{6}\s+([$#@A-Z0-9]{1,8})\s+\*\*\*\*')
    ial = re.compile(r"IAL\s+R0,X'([0-9A-F]+)'")
    dass_ial = {}
    for line in open(DASS, errors='replace'):
        m = hdr.match(line)
        if m:
            sec = m.group(1).strip(); continue
        if sec and (is_code(sec) or sec in CODE_BLOCKS) and sec not in dass_ial:
            m = ial.search(line)
            if m: dass_ial[sec] = int(m.group(1), 16)
    n = 0
    for b, hw in dass_ial.items():
        if frame.get(b) != hw * 2:
            frame[b] = hw * 2; n += 1
    print(f"[--dass-frames] {len(dass_ial)} DASS IAL frames, {n} overrides applied")

def fsize(block):                               # frame bytes, rounded up to a word
    return (frame.get(block, BASE) + 3) & ~3    # no STACKEND => base frame, not 0

# longest weighted path (DAG; no recursion in flight sw)
memo = {}
def depth(block, root):
    # a callee that is itself a program => separate stack, contributes 0
    if not root and prog_name(block) in PROGSET and block[:2] == '$0':
        return 0
    if block in memo: return memo[block]
    memo[block] = 0                              # guard (shouldn't cycle)
    w = fsize(block)
    best = 0
    for c in callees(block):
        best = max(best, depth(c, False))
    memo[block] = w + best
    return memo[block]

# compute @0<prog>
pred = {}
for prog in PROGSET:
    b = '$0' + prog[2:]
    memo.clear()
    d = depth(b, True)
    pred[prog] = (d + 1) // 2                    # bytes -> halfwords, round up

# ---- report ----
ok = 0; rows = []
for prog in sorted(oracle):
    exp, got = oracle[prog], pred.get(prog, 0)
    hit = exp == got
    ok += hit
    rows.append((prog, exp, got, hit))
for prog, exp, got, hit in rows:
    print(f"  {prog:10} oracle={exp:4}  pred={got:4}  {'OK' if hit else 'X  diff='+str(got-exp)}")
print(f"\nEXACT: {ok}/{len(oracle)}   pred_total={sum(pred.values())} oracle_total={sum(oracle.values())}")
