#!/usr/bin/env python3
#
# Stack CSECT sizing: longest weighted call chain over the whole load module.
#
# The AP-101/S linkage editor creates one @0<prog> (@n<task>) STACK CSECT per
# PROGRAM/TASK named by a CON80 ` STACK $0<prog>` card, sized as follows:
#
#   frame(B)    = B's SYM STACKEND value in BYTES (the block's frame
#                 high-water, MAXTEMP), rounded up to a word.  Blocks with no
#                 STACKEND get the 36-byte (18-halfword) base save-area frame.
#   depth(B)    = frame(B) + max(depth(C)) over B's callees -- a DAG longest
#                 path.  A callee that is itself a PROGRAM/TASK (has its own
#                 stack CSECT) switches stacks at dispatch => contributes 0.
#   @0<prog> hw = ceil(depth($0<prog>) / 2)
#
# Call edges come from RLDs (position CSECT -> referenced symbol), resolved:
#   #Q<x> QCON stub  -> its single ER (the real runtime routine)
#   LD alt entries   -> their owning SD (HREM->IREM, ETOH->#0ETOH, ...)
#   #Z<x> ZCON       -> #C<x> comsub code
#   #E<x> entry stub -> $0<x> / #C<x> (or the defining module's code blocks)
#   data references (#D/#P/...) resolve to no code block and drop out.
#
from collections import defaultdict

BASE_FRAME_BYTES = 36           # 18-hw register save area (AMAIN base frame)


def _isCode(name: str) -> bool:
    # $0 program main, #C comsub code, #E entry stub, #Q QCON stub,
    # A1-A9/B0-B6... nested blocks.  (Runtime routines have plain names the
    # heuristic can't see; they enter the graph via their STACKEND frames and
    # keep their outgoing RLD edges regardless -- see StackSizer.__init__.)
    if name[:2] in ('$0', '#C', '#E', '#Q'):
        return True
    return len(name) >= 2 and name[0].isalpha() and name[1].isdigit()


class StackSizer:
    """Builds the call graph from loaded modules; sizes @-stack CSECTs."""

    def __init__(self, modules, stackNames):
        self.stackSet = set(stackNames)
        self.frame = {}                     # code CSECT -> frame BYTES
        self.edges = defaultdict(set)       # CSECT name -> referenced sym names
        self.qconTarget = {}                # #Q<x> stub -> routine it points at
        self.ldOwner = {}                   # LD (alt entry) name -> owning SD
        self.modBlocksOf = {}               # SD/ER name -> defining module blocks
        codeBlocks = set()

        for m in modules:
            self.frame.update(m.stackSizes)         # STACKEND values, in BYTES
            sds = [s.name for s in m.sections.values() if s.type == 'SD']
            ers = [e.name for e in m.externals.values()]
            modCode = [n for n in sds if _isCode(n)]
            codeBlocks.update(modCode)
            for n in sds:
                self.modBlocksOf.setdefault(n, modCode)
                # a #Q<x> QCON stub (tiny SD) points at its one routine via
                # its single ER
                if n.startswith('#Q') and len(ers) == 1:
                    self.qconTarget[n] = ers[0]
            for s in m.lds:
                if s.ldId in m.sections:
                    self.ldOwner[s.name] = m.sections[s.ldId].name
            # RLD edges from ALL CSECTs: runtime routines have plain names
            # (CPASP, CPAS, ...) yet call further routines; edges from data
            # CSECTs are harmless because nothing resolves INTO a data CSECT.
            for r in m.relocations:
                pos = m.sections.get(r.posId)
                rel = m.sections.get(r.relId) or m.externals.get(r.relId) \
                    or m.ldsByEsdId.get(r.relId)
                if pos is None or rel is None or pos.type != 'SD':
                    continue
                self.edges[pos.name].add(rel.name)

        self.codeBlocks = codeBlocks | set(self.frame)

    def _asBlock(self, name):
        if name in self.ldOwner:            # alt entry -> owning SD
            name = self.ldOwner[name]
        return name if name in self.codeBlocks else None

    def _resolve(self, rn):
        """A referenced symbol -> the code block(s) it calls, or []."""
        if rn.startswith('#Q'):             # QCON stub -> real runtime routine
            t = self._asBlock(self.qconTarget.get(rn, ''))
            return [t] if t else []
        if rn in self.ldOwner:              # direct call of an alt entry
            t = self._asBlock(rn)
            return [t] if t else []
        if rn in self.codeBlocks and (rn[:2] in ('$0', '#C') or
                                      (rn[0].isalpha() and rn[1:2].isdigit())):
            return [rn]                     # direct block reference
        if rn.startswith('#Z'):             # ZCON indirect -> comsub code
            c = '#C' + rn[2:]
            return [c] if c in self.codeBlocks else []
        if rn.startswith('#E'):             # entry stub -> program/proc main
            for cand in ('$0' + rn[2:], '#C' + rn[2:]):
                if cand in self.codeBlocks:
                    return [cand]
            return [b for b in self.modBlocksOf.get(rn, ())
                    if not b.startswith('#E')]
        return []

    def _callees(self, block):
        out = []
        for rn in self.edges.get(block, ()):
            out.extend(c for c in self._resolve(rn) if c != block)
        return out

    def _fsize(self, block):                # frame bytes, word-aligned
        return (self.frame.get(block, BASE_FRAME_BYTES) + 3) & ~3

    def _depth(self, block, memo, root=False):
        # a callee with its own stack CSECT switches stacks => contributes 0
        if not root and block[0] == '$' and ('@' + block[1:]) in self.stackSet:
            return 0
        if block in memo:
            return memo[block]
        memo[block] = 0                     # cycle guard
        best = 0
        for c in self._callees(block):
            best = max(best, self._depth(c, memo))
        memo[block] = self._fsize(block) + best
        return memo[block]

    def sizeHW(self, stackName):
        """Halfword size for a '@<x>' stack CSECT, or None if its program
        block ('$<x>') is not in the link."""
        block = '$' + stackName[1:]
        if block not in self.codeBlocks:
            return None
        depthBytes = self._depth(block, {}, root=True)
        return (depthBytes + 1) // 2
