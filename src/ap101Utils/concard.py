#!/usr/bin/env python3
#
# CON80 CONCARD linkage-editor control cards
# 
# CON80 members are 80-column fixed-format control cards for the AP-101 linkage 
# editor.  
# 
# A top-level "build" card such as OFTMP names the PHASE structure of a 
# flight-software load and pulls in other cards and object modules.
# 
# This module parses those cards and walks the INCLUDE CONCARDS(...) tree to
# build a dependency graph whose edges are:
# 
#   * card -> card          (INCLUDE CONCARDS(member), recursed)
#   * card -> object        (INSERT name and INCLUDE otherlib(m,...))
#   * card -> library       (LIBRARY name(member) autocall declarations)
# 
# What the graph does **not** encode is source-level compile order.  CON80 is a
# *linkedit* deck: a module's position reflects memory layout, not a compile
# dependency.  COMPOOL-before-importer and INCL80 fragment ordering live in the
# HAL/S source and the compiler database, not here.  Accordingly:
# 
#   * ConcardGraph.object_modules returns an *unordered* worklist -- the
#     set of object/CSECT names a build pulls in; compile them in any order.
#   * ConcardGraph.link_order returns the order the object generator
#     visits cards (pre-order of the INCLUDE tree); that is the only ordering the
#     cards actually carry.
# 
# Card layout (cols 1-based):
#     1       * marks a comment card (*@ change history, *P summary).
#     1-8     optional location-counter / label field (e.g. 00000, 57FFF).
#     9-71    operation and operand area.
#     72      IBM continuation indicator (non-blank => next card continues).
#     73-80   identification / sequence number.
# 
# Only INCLUDE / INSERT / LIBRARY carry edges, plus PHASE which is
# read for context. 
#
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

from . import cards

# CON80 card fields
_LABEL = cards.LABEL      # cols 1-8   location counter / label
_BODY = slice(8, 71)      # cols 9-71  operation + operand area

# Verb is the leading alphabetic word; a comma or space ends it
# ("PHASE,PH=2;" -> verb PHASE, "INSERT  FCMBOOT" -> verb INSERT).
_VERB_RE = re.compile(r"\s*([A-Za-z][A-Za-z0-9]*)\b[ ,]*(.*)")
# library-style operand: NAME(member[,member...])
_LIBREF_RE = re.compile(r"([A-Z0-9#@$]+)\(([^)]*)\)")

# Verbs that contribute dependency edges.
EDGE_VERBS = frozenset({"INCLUDE", "INSERT", "LIBRARY", "RESERVE"})
# The library name that names *other CON80 cards* (recursed); anything else in
# an INCLUDE is an external object-module library (leaves).
CONCARD_LIBRARY = "CONCARDS"

# Verbs that drive the *placement* of object modules in the load's memory
# image: BANK/OVERLAY set the location counter / region, INSERT and STACK
# place a module's csects, SET/CLEAR declare the preceding block's
# store-protection, MAP imports an earlier phase's regions, and RESERVE
# allocates without loading text.
LAYOUT_VERBS = frozenset({"BANK", "OVERLAY", "INSERT", "CLEAR", "SET",
                          "STACK", "MAP", "RESERVE"})


#
# Directive -- one logical card
#

@dataclass
class Directive:
    line_no: int
    raw: str
    is_comment: bool = False
    is_continuation: bool = False
    label: str = ""
    verb: str = ""
    operand: str = ""

    @property
    def is_directive(self) -> bool:
        return bool(self.verb)


def _library_ref(operand: str) -> tuple[str, list[str]] | None:
    """Parse NAME(m1,m2,...) into (NAME, [members]) or None."""
    m = _LIBREF_RE.match(operand)
    if not m:
        return None
    members = [x.strip() for x in m.group(2).split(",") if x.strip()]
    return m.group(1), members


def parse_cards(text: str) -> list[Directive]:
    out: list[Directive] = []
    prev_continues = False
    for i, raw in enumerate(text.splitlines(), start=1):
        d = Directive(line_no=i, raw=raw)

        if prev_continues:
            d.is_continuation = True
            prev_continues = cards.continues(raw)
            out.append(d)
            continue
        prev_continues = cards.continues(raw)

        if raw[:1] == "*":
            d.is_comment = True
            out.append(d)
            continue

        d.label = raw[_LABEL].strip()
        m = _VERB_RE.match(raw[_BODY])
        if m:
            d.verb = m.group(1).upper()
            rest = m.group(2).split()
            d.operand = rest[0] if rest else ""
        out.append(d)
    return out


#
# OFTMP phase segments
#
# `PHASE n[,m,...]` cards partition a master deck (OFTMP) into phase
# segments: the FIRST number is the segment's own phase id (the id MAP cards
# reference); the rest are the other MMU phases the same content serves
# (SSW's `PHASE 2,3,...,18` -- resident in every OPS).  A segment's cards
# run to the next PHASE card; the deck prologue (ADDRMAX, LIBRARY, ...
# before the first PHASE card) applies to every segment.
#
# A phase-scoped virtual member "<member>@<n>" (e.g. "OFTMP@2") is accepted
# anywhere a root member name is (build_graph, expand_directives,
# layout_program, lnk101 --concard-root): it reads as the prologue followed
# by phase n's segment.

def split_phase_root(root: str) -> tuple[str, int | None]:
    """'OFTMP@2' -> ('OFTMP', 2); a plain member name -> (name, None)."""
    base, sep, num = root.partition("@")
    if sep and num.isdigit():
        return base, int(num)
    return root, None


def phase_numbers(operand: str) -> list[int]:
    return [int(t) for t in operand.replace(",", " ").split() if t.isdigit()]


def phase_segments(deck: 'ConcardDeck', member: str = "OFTMP"):
    """Ordered [(numbers, [Directive])] per PHASE card of `member`.  The
    leading entry ([], prologue) holds the cards before the first PHASE;
    each segment's directive list starts with its own PHASE card."""
    segs: list[tuple[list[int], list[Directive]]] = [([], [])]
    for d in deck.read(member):
        if d.is_directive and d.verb == "PHASE":
            segs.append((phase_numbers(d.operand), []))
        segs[-1][1].append(d)
    return segs


#
# ConcardDeck -- a directory of CON80 members
#

class ConcardDeck:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_dir():
            raise NotADirectoryError(f"CON80 deck not found: {self.path}")
        self.members: set[str] = {
            p.name for p in self.path.iterdir() if p.is_file()
        }
        self._cache: dict[str, list[Directive]] = {}

    def has(self, name: str) -> bool:
        base, phase = split_phase_root(name)
        if phase is None:
            return base in self.members
        return base in self.members and any(
            nums and nums[0] == phase
            for nums, _ in phase_segments(self, base))

    def read(self, name: str) -> list[Directive]:
        if name not in self._cache:
            base, phase = split_phase_root(name)
            if phase is not None:
                segs = phase_segments(self, base)
                body = next((d for nums, d in segs
                             if nums and nums[0] == phase), [])
                self._cache[name] = segs[0][1] + body
            else:
                text = (self.path / base).read_text(errors="replace")
                self._cache[name] = parse_cards(text)
        return self._cache[name]


#
# Dependency graph
#

@dataclass
class Node:
    name: str
    kind: str            # 'concard' | 'object' | 'library'
    missing: bool = False  # concard referenced but not present in the deck


@dataclass
class Edge:
    parent: str
    child: str
    kind: str            # 'include' | 'insert' | 'library-include' | 'library'
    line_no: int
    library: str | None = None   # external/autocall library name
    phase: str | None = None
    overlay: str | None = None


@dataclass
class ConcardGraph:
    root: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unreached: set[str] = field(default_factory=set)   # deck members not reached
    cycles: list[list[str]] = field(default_factory=list)

    # adjacency helpers 

    def _children(self, name: str, kinds: set[str] | None = None) -> list[str]:
        """Distinct child names of name, in first-seen (source) order."""
        seen: list[str] = []
        for e in self.edges:
            if e.parent != name:
                continue
            if kinds is not None and self.nodes[e.child].kind not in kinds:
                continue
            if e.child not in seen:
                seen.append(e.child)
        return seen

    # orderings 

    def link_order(self) -> list[str]:
        """Card names in the order the object generator processes them:
        a pre-order walk of the INCLUDE CONCARDS tree from the root.  For a
        top-level build card this reproduces the PHASE 1..N sequence."""
        order: list[str] = []
        seen: set[str] = set()

        def dfs(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            order.append(name)
            for child in self._children(name, kinds={"concard"}):
                dfs(child)

        dfs(self.root)
        return order

    def object_modules(self) -> list[str]:
        return sorted(n.name for n in self.nodes.values() if n.kind == "object")

    def libraries(self) -> list[str]:
        return sorted(n.name for n in self.nodes.values() if n.kind == "library")

    def library_members(self) -> list[str]:
        seen: list[str] = []
        for e in self.edges:
            if e.kind == "library-include" and e.child not in seen:
                seen.append(e.child)
        return seen

    def topo_order(self) -> list[str]:
        WHITE, GREY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self.nodes}
        order: list[str] = []
        stack_names: list[str] = []

        def visit(name: str) -> None:
            color[name] = GREY
            stack_names.append(name)
            for child in self._children(name):
                if color[child] == WHITE:
                    visit(child)
                elif color[child] == GREY:
                    i = stack_names.index(child)
                    self.cycles.append(stack_names[i:] + [child])
            stack_names.pop()
            color[name] = BLACK
            order.append(name)

        if self.root in color:
            visit(self.root)
        for name in self.nodes:
            if color[name] == WHITE:
                visit(name)
        return order

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "nodes": [
                {"name": n.name, "kind": n.kind, **({"missing": True} if n.missing else {})}
                for n in self.nodes.values()
            ],
            "edges": [
                {k: v for k, v in {
                    "parent": e.parent, "child": e.child, "kind": e.kind,
                    "library": e.library, "phase": e.phase, "overlay": e.overlay,
                    "line": e.line_no,
                }.items() if v is not None}
                for e in self.edges
            ],
            "unreached": sorted(self.unreached),
            "cycles": self.cycles,
        }

    def to_dot(self) -> str:
        shapes = {"concard": "box", "object": "ellipse", "library": "cylinder"}
        lines = ["digraph concards {", "  rankdir=LR;",
                 '  node [fontname="monospace"];']
        for n in self.nodes.values():
            style = ',style=dashed' if n.missing else ''
            lines.append(f'  "{n.name}" [shape={shapes.get(n.kind, "box")}{style}];')
        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            key = (e.parent, e.child)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'  "{e.parent}" -> "{e.child}";')
        lines.append("}")
        return "\n".join(lines)


def build_graph(deck: ConcardDeck, root: str = "OFTMP") -> ConcardGraph:
    if not deck.has(root):
        raise KeyError(f"root member {root!r} not in deck {deck.path}")

    g = ConcardGraph(root=root)

    def node(name: str, kind: str, missing: bool = False) -> Node:
        n = g.nodes.get(name)
        if n is None:
            n = Node(name, kind, missing)
            g.nodes[name] = n
        return n

    node(root, "concard")
    walked: set[str] = set()

    def walk(card: str) -> None:
        # Walk each card's body once: the edge *to* it is added at the INCLUDE
        # site, but skip if already encountered:
        if card in walked:
            return
        walked.add(card)
        phase: str | None = None
        overlay: str | None = None

        for d in deck.read(card):
            if not d.is_directive:
                continue
            if d.verb == "PHASE":
                phase = d.operand or phase
                continue
            if d.verb == "OVERLAY":
                overlay = d.operand or overlay
                continue
            if d.verb not in EDGE_VERBS:
                continue

            if d.verb in ("INSERT", "RESERVE"):
                # RESERVE = the LE's allocation-only variant of INSERT
                # (CR091094): the member is still fetched (for its ESD size),
                # so it carries the same worklist edge.
                if not d.operand:
                    continue
                node(d.operand, "object")
                g.edges.append(Edge(card, d.operand, "insert", d.line_no,
                                    phase=phase, overlay=overlay))
                continue

            ref = _library_ref(d.operand)
            if ref is None:
                continue
            lib, members = ref

            if d.verb == "LIBRARY":
                node(lib, "library")
                g.edges.append(Edge(card, lib, "library", d.line_no,
                                    library=lib, phase=phase, overlay=overlay))
                continue

            # INCLUDE
            if lib == CONCARD_LIBRARY:
                for child in members:
                    present = deck.has(child)
                    node(child, "concard", missing=not present)
                    g.edges.append(Edge(card, child, "include", d.line_no,
                                        phase=phase, overlay=overlay))
                    if present:
                        walk(child)
            else:
                for child in members:
                    node(child, "object")
                    g.edges.append(Edge(card, child, "library-include",
                                        d.line_no, library=lib,
                                        phase=phase, overlay=overlay))

    walk(root)

    reached_cards = {n.name for n in g.nodes.values() if n.kind == "concard"}
    g.unreached = deck.members - reached_cards
    return g


#
# Layout program -- the ordered placement instruction stream
#

@dataclass
class LayoutOp:
    """One placement instruction, in the order the object generator executes it.

    verb is one of LAYOUT_VERBS or PHASE; operand is its argument
    (a bank number for BANK, an overlay/module name for OVERLAY/INSERT/STACK, the
    phase list for PHASE, empty for CLEAR/SET).  origin is the location-field
    (cols 1-8) value parsed as a hex **halfword** address when present -- on an
    OVERLAY card it is the region's absolute load address (e.g. LOWCORE's
    00000 OVERLAY PSA -> 0, 001A8 OVERLAY Z1 -> 424); None when the
    field is blank (sequential placement).  
    """
    verb: str
    operand: str
    card: str
    line_no: int
    origin: int | None = None   # <addr> field
                                # OVERLAY => absolute load address
                                # None => continues from last entry


def _parse_origin(label: str) -> int | None:
    if not label:
        return None
    try:
        return int(label, 16)
    except ValueError:
        return None


def expand_directives(deck: ConcardDeck, root: str = "OFTMP"):
    """Yield (card, Directive) over `root`'s expansion in execution order,
    recursing into INCLUDE CONCARDS(...) members exactly as the linkage
    editor reads them (each member at most once per path)."""
    if not deck.has(root):
        raise KeyError(f"root member {root!r} not in deck {deck.path}")

    stack: list[str] = []

    def walk(card: str):
        if card in stack:
            return
        stack.append(card)
        for d in deck.read(card):
            if not d.is_directive:
                continue
            yield card, d
            if d.verb == "INCLUDE":
                ref = _library_ref(d.operand)
                if ref and ref[0] == CONCARD_LIBRARY:
                    for child in ref[1]:
                        if deck.has(child):
                            yield from walk(child)
        stack.pop()

    yield from walk(root)


def has_nocall(deck: ConcardDeck, root: str = "OFTMP") -> bool:
    """True when the linkedit described by `root` carries a NOCALLER card
    (anywhere in its CONCARDS expansion).  NOCALLER = the linkage editor's
    NCAL option: automatic library call is disabled and unresolved external
    references are left in the load module without failing the linkedit."""
    return any(d.verb == "NOCALLER" for _, d in expand_directives(deck, root))


def library_no_call(deck: ConcardDeck,
                    root: str = "OFTMP") -> tuple[set[str], set[str]]:
    """The BLANK-ddname LIBRARY cards of `root`'s expansion, as
    (restricted-no-call SYMBOLS, never-call MEMBERS).

    The linkage editor's LIBRARY statement has three forms:

      * `LIBRARY ddname(member,...)`  -- an additional call library.  Not
        returned here; `_library_ref` parses it for the dependency graph.
      * `LIBRARY *(er,...)`  -- RESTRICTED NO-CALL.  The named ERs are 
        never resolved by automatic library call and no diagnostic is 
        printed: the load module ships with the assembled text field as 
        the compiler/assembler left it and no RLD fixup applied. 
      * `LIBRARY  (member,...)`  -- blank ddname, no star: NEVER-CALL.  The
        named library members are never fetched by automatic library call
    """
    restricted: set[str] = set()
    never: set[str] = set()
    for _, d in expand_directives(deck, root):
        if d.verb != "LIBRARY":
            continue
        operand = (d.operand or "").strip()
        star = operand.startswith("*")
        body = operand[1:].lstrip() if star else operand
        if not body.startswith("("):
            continue                      # ddname(member,...): a real library
        names = {s.strip() for s in body[1:].split(")")[0].split(",")}
        (restricted if star else never).update(n for n in names if n)
    return restricted, never


def layout_program(deck: ConcardDeck, root: str = "OFTMP") -> list[LayoutOp]:
    ops = []
    for card, d in expand_directives(deck, root):
        if d.verb in LAYOUT_VERBS or d.verb == "PHASE":
            ops.append(LayoutOp(d.verb, d.operand, card, d.line_no,
                                origin=_parse_origin(d.label)))
        elif d.verb == "INCLUDE":
            # A LIBRARY include (`INCLUDE SYSLIBL1(member)`) loads the member
            # at this point in the deck: an INSERT-like sequential placement,
            # EXCEPT that a member whose csect is already defined (e.g. by an
            # earlier phase's MAP import) REPLACES it in place.  CONCARDS
            # includes are the deck expansion itself (handled by
            # expand_directives), not placements.
            ref = _library_ref(d.operand)
            if ref and ref[0] != CONCARD_LIBRARY:
                for m in ref[1]:
                    ops.append(LayoutOp("INCLUDE", m, card, d.line_no,
                                        origin=_parse_origin(d.label)))
        elif d.verb == "CHANGE":
            # LE CHANGE oldname(newname): rename an external symbol (SD, LD,
            # or ER) in the module supplied by the NEXT module-reading card
            # (an INCLUDE) -- the CESD rename chain applies only to that one
            # module (LE PLM p.28/35).  The operand keeps the raw `old(new)`
            # text; the linker parses it.
            ops.append(LayoutOp("CHANGE", d.operand, card, d.line_no))
    return ops


#
# CLI
#

def _print_tree(g: ConcardGraph, out) -> None:
    seen: set[str] = set()

    def rec(name: str, depth: int) -> None:
        n = g.nodes[name]
        tag = {"object": " (obj)", "library": " (lib)"}.get(n.kind, "")
        if n.missing:
            tag = " (MISSING)"
        elide = name in seen and bool(g._children(name))
        print("  " * depth + name + tag + (" ..." if elide else ""), file=out)
        if name in seen:
            return
        seen.add(name)
        for child in g._children(name):
            rec(child, depth + 1)

    rec(g.root, 0)


app = typer.Typer(
    help="Read CON80 CONCARDs and build/inspect the build dependency DAG.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.command()
def main(
    deck_dir: Annotated[str, typer.Argument(metavar="DECK",
        help="path to the CON80 directory")],
    root: Annotated[str, typer.Option("--root", "-r",
        help="top-level build card")] = "OFTMP",
    tree: Annotated[bool, typer.Option("--tree",
        help="print the INCLUDE tree (default)")] = False,
    objects: Annotated[bool, typer.Option("--objects",
        help="list object modules to compile (unordered worklist)")] = False,
    link_order: Annotated[bool, typer.Option("--link-order",
        help="list cards in object-generator processing order")] = False,
    layout: Annotated[bool, typer.Option("--layout",
        help="list the flat placement program (BANK/OVERLAY/INSERT/...)")] = False,
    libraries: Annotated[bool, typer.Option("--libraries",
        help="list autocall libraries")] = False,
    orphans: Annotated[bool, typer.Option("--orphans",
        help="list deck members not reachable from the root")] = False,
    dot: Annotated[bool, typer.Option("--dot", help="emit Graphviz DOT")] = False,
    as_json: Annotated[bool, typer.Option("--json",
        help="emit the graph as JSON")] = False,
) -> None:
    import json
    import sys

    if sum((tree, objects, link_order, layout, libraries, orphans, dot,
            as_json)) > 1:
        raise typer.BadParameter(
            "--tree/--objects/--link-order/--layout/--libraries/--orphans/"
            "--dot/--json are mutually exclusive")

    try:
        deck = ConcardDeck(deck_dir)
        g = build_graph(deck, root)
    except (KeyError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(2)

    out = sys.stdout
    if objects:
        for name in g.object_modules():
            print(name, file=out)
    elif link_order:
        for name in g.link_order():
            print(name, file=out)
    elif layout:
        for op in layout_program(deck, root):
            print(f"{op.verb:8} {op.operand}".rstrip(), file=out)
    elif libraries:
        for name in g.libraries():
            print(name, file=out)
    elif orphans:
        for name in sorted(g.unreached):
            print(name, file=out)
    elif dot:
        print(g.to_dot(), file=out)
    elif as_json:
        json.dump(g.to_dict(), out, indent=2)
        out.write("\n")
    else:
        _print_tree(g, out)
        cardNodes = [n for n in g.nodes.values() if n.kind == "concard"]
        print(f"\n{len(cardNodes)} cards, {len(g.object_modules())} object modules, "
              f"{len(g.libraries())} libraries, {len(g.edges)} edges; "
              f"{len(g.unreached)} deck members unreached.", file=sys.stderr)
        if g.cycles:
            print(f"WARNING: {len(g.cycles)} cycle(s) detected.", file=sys.stderr)


if __name__ == "__main__":
    app()
