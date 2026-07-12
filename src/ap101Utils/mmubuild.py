#!/usr/bin/env python3
#
# MMU (mass-memory) load-build cards: parser and expansion tree.
# 
# Format:
# 
#   * Statement cards: [LABEL] VERB,KEY=VAL,...; terminated by ; and
#     optionally continued across cards (non-blank col 72).  LABEL (cols 1-8) is
#     the CSECT/symbol the statement acts on.  Verbs: ALLOC, ALOCDESC, DIRECTRY,
#     DATA, PHASE, COPY, DATASET, SYSTEM, DMMD, BUILD, IPL, LOADMOD, NOLMDATA.
#   * Member-list cards: rows of MEMBER [SYSID|flag] [description] -- one
#     member name per row, naming another card to expand.
# 
# Three-level content expansion, rooted at the master list MMXMEM:
# 
#     MMXMEM ─▶ MMX<system> lists ─▶ MMUDAT*/MMUSYS* statement members
# 
# A LOADMOD,MEMBER=X statement pulls a CONCARD output member into the MMU image
# 
# Note: ADDR / MMDIR / RPL values are kept as raw tokens.
#
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

from . import cards

# card column geometry (0-based slices); same layout as concard cards
_LABEL = cards.LABEL      # cols 1-8   label / CSECT
_BODY = slice(0, 71)      # cols 1-71  content (label + op + operand)

MMU_VERBS = frozenset({
    "ALLOC", "ALOCDESC", "DIRECTRY", "DATA", "PHASE", "COPY", "DATASET",
    "SYSTEM", "DMMD", "BUILD", "IPL", "LOADMOD", "NOLMDATA",
})
# Verbs that mark a member as a CONCARD linkedit deck, not an MMU card set
_CONCARD_VERBS = frozenset({
    "INCLUDE", "OVERLAY", "INSERT", "BANK", "LIBRARY", "NOCALLER", "MAP",
    "STACK", "CLEAR", "ADDRMAX",
})

_STMT_RE = re.compile(
    r"\s*(?:([A-Za-z#$@][A-Za-z0-9#$@_]*)\s+)?"   # optional label
    r"([A-Za-z][A-Za-z0-9]*)\s*([,;])")           # verb then , or ;
_SYSID_RE = re.compile(r"^SYS[0-9A-Z]$")


#
# Statements
#

@dataclass
class Statement:
    line_no: int
    source: str              # member it came from
    label: str               # cols 1-8 (CSECT/symbol), '' if none
    verb: str
    params: dict[str, str] = field(default_factory=dict)  # KEY -> raw value
    args: list[str] = field(default_factory=list)         # positional pieces
    text: str = ""           # ALOCDESC free-text description, else ''
    raw: str = ""            # stitched operand source (after the verb, incl ';')


def _split_params(operand: str) -> tuple[dict[str, str], list[str]]:
    """Split K=V,K=V,FLAG on top-level commas, honoring () and '' nesting.
    Returns (params, positional-args)."""
    params: dict[str, str] = {}
    args: list[str] = []
    depth = 0
    quote = False
    buf = ""

    def flush(piece: str) -> None:
        piece = piece.strip()
        if not piece:
            return
        eq = piece.find("=")
        if eq > 0 and "(" not in piece[:eq] and "'" not in piece[:eq]:
            params[piece[:eq].strip().upper()] = piece[eq + 1:].strip()
        else:
            args.append(piece)

    for ch in operand:
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth = max(0, depth - 1)
        elif not quote and depth == 0 and ch == ",":
            flush(buf)
            buf = ""
            continue
        buf += ch
    flush(buf)
    return params, args


def _stitch(lines: list[str]) -> list[tuple[int, str]]:
    """Join continuation cards (non-blank col 72) into logical statements"""
    out: list[tuple[int, str]] = []
    cur: str | None = None
    cur_line = 0
    prev_cont = False
    for i, raw in enumerate(lines, start=1):
        body = raw[_BODY]
        cont = cards.continues(raw)
        if prev_cont and cur is not None:
            cur += body.strip()
        else:
            if cur is not None:
                out.append((cur_line, cur))
            cur = body.rstrip()
            cur_line = i
        prev_cont = cont
    if cur is not None:
        out.append((cur_line, cur))
    return out


def parse_statements(text: str, source: str = "") -> list[Statement]:
    stmts: list[Statement] = []
    raw_lines = [ln.rstrip("\n") for ln in text.splitlines()]
    for line_no, body in _stitch(raw_lines):
        if body[:1] == "*" or not body.strip():
            continue
        m = _STMT_RE.match(body)
        if not m:
            continue
        verb = m.group(2).upper()
        if verb not in MMU_VERBS:
            continue
        label = (m.group(1) or "").strip()
        operand = body[m.end():]
        semi = operand.find(";")
        operand = operand[:semi] if semi >= 0 else operand

        st = Statement(line_no=line_no, source=source, label=label, verb=verb,
                       raw=operand.strip())
        if m.group(3) == ";" or not operand.strip():
            pass                          # bare verb, e.g. BUILD;
        elif verb == "ALOCDESC":
            # opaque free text (may contain commas/spaces); strip optional quotes
            st.text = operand.strip().strip("'")
        else:
            st.params, st.args = _split_params(operand)
        stmts.append(st)
    return stmts


#
# Member-list rows
#

@dataclass
class MemberRef:
    member: str
    sysid: str | None = None   # MMXMEM rows annotate each list with a SYSID
    flags: list[str] = field(default_factory=list)   # e.g. RPL
    comment: str = ""


def parse_member_list(text: str) -> list[MemberRef]:
    refs: list[MemberRef] = []
    for raw in text.splitlines():
        body = raw[_BODY]
        if body[:1] == "*" or not body.strip():
            continue
        toks = body.split()
        ref = MemberRef(member=toks[0])
        rest = toks[1:]
        words: list[str] = []
        for t in rest:
            if t == "*":
                break                      # '*' begins an inline comment
            if _SYSID_RE.match(t) and ref.sysid is None:
                ref.sysid = t
            elif t == "RPL":
                ref.flags.append(t)
            else:
                words.append(t)
        ref.comment = " ".join(words)
        refs.append(ref)
    return refs


#
# Deck + classification
#

class MMUDeck:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_dir():
            raise NotADirectoryError(f"CON80 deck not found: {self.path}")
        self.members: set[str] = {p.name for p in self.path.iterdir() if p.is_file()}
        self._text: dict[str, str] = {}
        self._class: dict[str, str] = {}

    def has(self, name: str) -> bool:
        return name in self.members

    def text(self, name: str) -> str:
        if name not in self._text:
            self._text[name] = (self.path / name).read_text(errors="replace")
        return self._text[name]

    def classify(self, name: str) -> str:
        """'data' | 'list' | 'concard' | 'empty' (by content, not by name)."""
        if name not in self._class:
            self._class[name] = self._classify(name)
        return self._class[name]

    def _classify(self, name: str) -> str:
        rows = 0
        for raw in self.text(name).splitlines():
            body = raw[_BODY]
            if body[:1] == "*" or not body.strip():
                continue
            rows += 1
            m = _STMT_RE.match(body)
            if m and m.group(2).upper() in MMU_VERBS:
                return "data"
            # CONCARD verbs (e.g. TEXTGPH) appear as the 1st token, or the 2nd
            # after a location-counter label like "00000 OVERLAY".
            toks = body.split()
            if any(t.upper() in _CONCARD_VERBS for t in toks[:2]):
                return "concard"
        return "list" if rows else "empty"


#
# Expansion tree / aggregate views
#

@dataclass
class Node:
    name: str
    kind: str            # 'list' | 'data' | 'concard' | 'missing'


@dataclass
class Edge:
    parent: str
    child: str
    sysid: str | None = None
    flags: list[str] = field(default_factory=list)
    comment: str = ""
    kind: str = "expand"   # 'expand' (list row) | 'loadmod' (LOADMOD MEMBER=)


@dataclass
class MMUBuild:
    deck: MMUDeck
    root: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unreached: set[str] = field(default_factory=set)

    # adjacency

    def children(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.parent == name]

    # aggregate views

    def statements(self, member: str | None = None) -> list[Statement]:
        """Parsed statements of one data member, or of every reached one."""
        names = [member] if member else [
            n.name for n in self.nodes.values() if n.kind == "data"]
        out: list[Statement] = []
        for nm in names:
            if self.deck.has(nm):
                out.extend(parse_statements(self.deck.text(nm), nm))
        return out

    def _by_verb(self, verb: str) -> list[Statement]:
        return [s for s in self.statements() if s.verb == verb]

    def allocations(self) -> list[Statement]:
        """ALLOC statements (source order; ADDR kept as a raw token)."""
        return self._by_verb("ALLOC")

    def systems(self) -> list[Statement]:
        return self._by_verb("SYSTEM")

    def datasets(self) -> list[Statement]:
        return self._by_verb("DATASET")

    def loadmods(self) -> list[Statement]:
        """LOADMOD statements -- cross-links to CONCARD linkedit members."""
        return self._by_verb("LOADMOD")

    def sysids(self) -> list[str]:
        s = {st.params["SYSID"] for st in self.statements()
             if "SYSID" in st.params}
        return sorted(s)

    # serialization

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "nodes": [{"name": n.name, "kind": n.kind} for n in self.nodes.values()],
            "edges": [
                {k: v for k, v in {
                    "parent": e.parent, "child": e.child, "sysid": e.sysid,
                    "flags": e.flags or None, "comment": e.comment or None,
                    "kind": e.kind,
                }.items() if v is not None}
                for e in self.edges
            ],
            "sysids": self.sysids(),
            "unreached": sorted(self.unreached),
        }

    def to_dot(self) -> str:
        shapes = {"list": "box", "data": "ellipse",
                  "concard": "component", "missing": "octagon"}
        out = ["digraph mmubuild {", "  rankdir=LR;",
               '  node [fontname="monospace"];']
        for n in self.nodes.values():
            out.append(f'  "{n.name}" [shape={shapes.get(n.kind, "box")}];')
        seen: set[tuple[str, str]] = set()
        for e in self.edges:
            key = (e.parent, e.child)
            if key in seen:
                continue
            seen.add(key)
            lbl = e.sysid or ("loadmod" if e.kind == "loadmod" else "")
            attr = f' [label="{lbl}"]' if lbl else ""
            out.append(f'  "{e.parent}" -> "{e.child}"{attr};')
        out.append("}")
        return "\n".join(out)


def build(deck: MMUDeck, root: str = "MMXMEM") -> MMUBuild:
    """Expand the MMU content tree from `root` (default the master list MMXMEM).

    List members are expanded by their rows; data members become leaves but are
    scanned for LOADMOD,MEMBER=X cross-links to CONCARD decks.
    """
    if not deck.has(root):
        raise KeyError(f"root member {root!r} not in deck {deck.path}")

    b = MMUBuild(deck=deck, root=root)

    def node(name: str) -> Node:
        n = b.nodes.get(name)
        if n is None:
            kind = deck.classify(name) if deck.has(name) else "missing"
            n = Node(name, kind)
            b.nodes[name] = n
        return n

    walked: set[str] = set()

    def walk(name: str) -> None:
        if name in walked:
            return
        walked.add(name)
        n = node(name)

        if n.kind == "list":
            for ref in parse_member_list(deck.text(name)):
                node(ref.member)
                b.edges.append(Edge(name, ref.member, sysid=ref.sysid,
                                    flags=ref.flags, comment=ref.comment))
                walk(ref.member)
        elif n.kind == "data":
            for st in parse_statements(deck.text(name), name):
                if st.verb == "LOADMOD" and "MEMBER" in st.params:
                    member = st.params["MEMBER"]
                    node(member)
                    b.edges.append(Edge(name, member, kind="loadmod"))

    node(root)
    walk(root)

    # Orphans: MM-named members that classify as MMU content but weren't reached.
    reached = set(b.nodes)
    for m in deck.members:
        if m.startswith("MM") and m not in reached and deck.classify(m) in ("list", "data"):
            b.unreached.add(m)
    return b


#
# CLI
#

def _print_tree(b: MMUBuild, out) -> None:
    seen: set[str] = set()

    def rec(name: str, depth: int, ann: str = "") -> None:
        n = b.nodes[name]
        tag = {"data": "", "list": " [list]", "concard": " [concard]",
               "missing": " [MISSING]"}.get(n.kind, "")
        suffix = f"  {ann}" if ann else ""
        elide = name in seen and bool(b.children(name))
        print("  " * depth + name + tag + suffix + (" ..." if elide else ""), file=out)
        if name in seen:
            return
        seen.add(name)
        for e in b.children(name):
            a = e.sysid or ("LOADMOD" if e.kind == "loadmod" else "")
            if e.flags:
                a = (a + " " + " ".join(e.flags)).strip()
            rec(e.child, depth + 1, a)

    rec(b.root, 0)


def _fmt(st: Statement) -> str:
    parts = []
    if st.label:
        parts.append(f"{st.label}:")
    parts.append(st.verb)
    if st.text:
        parts.append(repr(st.text))
    if st.params:
        parts.append(",".join(f"{k}={v}" for k, v in st.params.items()))
    if st.args:
        parts.append(" ".join(st.args))
    return " ".join(parts)


app = typer.Typer(
    help="Read MMU (mass-memory) load-build cards from a CON80 directory.",
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
        help="content-tree root member")] = "MMXMEM",
    tree: Annotated[bool, typer.Option("--tree",
        help="print the member expansion tree (default)")] = False,
    statements: Annotated[bool, typer.Option("--statements",
        help="dump parsed statements (all members, or one with --member)")] = False,
    member: Annotated[Optional[str], typer.Option("--member", metavar="MEMBER",
        help="restrict --statements to one member")] = None,
    alloc: Annotated[bool, typer.Option("--alloc",
        help="list ALLOC statements grouped by SYSID")] = False,
    systems: Annotated[bool, typer.Option("--systems",
        help="list SYSTEM statements")] = False,
    loadmods: Annotated[bool, typer.Option("--loadmods",
        help="list LOADMOD links to linkedit (CONCARD) members")] = False,
    datasets: Annotated[bool, typer.Option("--datasets",
        help="list DATASET statements")] = False,
    orphans: Annotated[bool, typer.Option("--orphans",
        help="MMU members not reached from the root")] = False,
    dot: Annotated[bool, typer.Option("--dot", help="emit Graphviz DOT")] = False,
    as_json: Annotated[bool, typer.Option("--json",
        help="emit the tree as JSON")] = False,
) -> None:
    import json
    import sys

    if sum((tree, statements, alloc, systems, loadmods, datasets, orphans,
            dot, as_json)) > 1:
        raise typer.BadParameter(
            "--tree/--statements/--alloc/--systems/--loadmods/--datasets/"
            "--orphans/--dot/--json are mutually exclusive")
    if member is not None and not statements:
        raise typer.BadParameter("--member only applies with --statements")

    try:
        deck = MMUDeck(deck_dir)
        b = build(deck, root)
    except (KeyError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(2)

    out = sys.stdout
    if statements:
        for st in b.statements(member):
            print(f"{st.source}:{st.line_no}: {_fmt(st)}", file=out)
    elif alloc:
        cur = None
        for st in sorted(b.allocations(), key=lambda s: s.params.get("SYSID", "")):
            sysid = st.params.get("SYSID", "?")
            if sysid != cur:
                cur = sysid
                print(f"\n# SYSID {sysid}", file=out)
            print(f"  {st.label or '-':8} ADDR={st.params.get('ADDR','?'):>6} "
                  f"BLKS={st.params.get('BLKS','?'):>4} "
                  f"{'RPL='+st.params['RPL'] if 'RPL' in st.params else ''}"
                  f"  ({st.source})", file=out)
    elif systems:
        for st in b.systems():
            print(f"{st.source}:{st.line_no}: {_fmt(st)}", file=out)
    elif loadmods:
        for st in b.loadmods():
            lm = st.params.get("MEMBER", "?")
            mark = "" if deck.has(lm) else "  (not in deck)"
            print(f"{lm}  <- {st.source}:{st.line_no}{mark}", file=out)
    elif datasets:
        for st in b.datasets():
            print(f"{st.source}:{st.line_no}: {_fmt(st)}", file=out)
    elif orphans:
        for m in sorted(b.unreached):
            print(m, file=out)
    elif dot:
        print(b.to_dot(), file=out)
    elif as_json:
        json.dump(b.to_dict(), out, indent=2)
        out.write("\n")
    else:
        _print_tree(b, out)
        kinds = {}
        for n in b.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        print(f"\n{kinds.get('list',0)} lists, {kinds.get('data',0)} data members, "
              f"{kinds.get('concard',0)} linkedit decks, {len(b.sysids())} systems "
              f"({', '.join(b.sysids())}); {len(b.unreached)} MMU members unreached.",
              file=sys.stderr)


if __name__ == "__main__":
    app()
