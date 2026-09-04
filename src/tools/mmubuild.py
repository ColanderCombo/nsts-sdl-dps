#!/usr/bin/env python3
#
# Mass Memory Unit (MMU) build cards
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
# DIRECTRY: a table the mass-memory build writes.  The statement declares the
# shape and is followed, under the same label, by the statement that fills it:
#
#   DMMD=NO   a free-standing MMU-resident directory, filled by a DATA card
#             (HDATA= is its tape-image address; ENTRY=(first,step)).
#   DMMD=YES  a directory that lives inside a linked phase -- CSECT= names the
#             csect and OFFSET= the halfword within it -- filled by a DMMD card
#             whose PN= list is ((content, phase), ...) pairs.  Each entry
#             resolves the phase to its own ALLOC address and length, so the
#             table can only be built once the tape layout is known; the linked
#             load module carries the csect's plain INITIAL values and the
#             build stamps this over them.
#
# `Directory.image()` renders a DMMD table.  Neither the csect's own initials
# nor the value an unused slot is filled with are anywhere in the cards, so
# the fill is a caller-supplied parameter and defaults to zero.
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

from ap101Utils import cards
from ap101Utils.mmbstamp import mm16

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
# Directories
#

HW_PER_BLOCK = 512        # halfwords in one mass-memory block

_PN_RE = re.compile(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)")


@dataclass
class Directory:
    """A DIRECTRY statement together with the statement that fills it.

    `filler` is the DATA card of a DMMD=NO directory or the DMMD card of a
    DMMD=YES one, matched by label within the same member.
    """

    directry: Statement
    filler: Optional[Statement] = None

    # --- the DIRECTRY card

    @property
    def label(self) -> str:
        return self.directry.label

    @property
    def source(self) -> str:
        return self.directry.source

    @property
    def csect(self) -> str:
        """The csect the table is stamped into, '' for a free-standing one."""
        return self.directry.params.get("CSECT", "")

    @property
    def offset(self) -> int:
        return int(self.directry.params.get("OFFSET", 0))

    @property
    def size(self) -> int:
        """Halfwords per entry."""
        return int(self.directry.params["SIZE"])

    @property
    def entries(self) -> int:
        """Slot capacity -- the table is this many entries whatever it holds."""
        return int(self.directry.params["ENTRIES"])

    @property
    def phase(self) -> int:
        return int(self.directry.params["PH"])

    @property
    def is_dmmd(self) -> bool:
        return self.directry.params.get("DMMD", "NO").upper().startswith("Y")

    # --- the DMMD card

    @property
    def pn(self) -> list[tuple[int, int]]:
        """The DMMD PN= list as (content number, phase) pairs, in card order."""
        if self.filler is None or self.filler.verb != "DMMD":
            return []
        return [(int(a), int(b))
                for a, b in _PN_RE.findall(self.filler.params.get("PN", ""))]

    @property
    def ident(self) -> Optional[int]:
        """The DMMD ID= -- the entry size the table's own header repeats."""
        v = self.filler.params.get("ID") if self.filler else None
        return int(v) if v is not None else None

    @property
    def fmts(self) -> Optional[int]:
        """The DMMD FMTS= -- how many of the slots the card fills."""
        v = self.filler.params.get("FMTS") if self.filler else None
        return int(v) if v is not None else None

    @property
    def lengths(self) -> bool:
        """LNG=YES -- an entry carries its content's length as a third field."""
        if self.filler is None:
            return False
        return self.filler.params.get("LNG", "NO").upper().startswith("Y")

    # --- the table

    def header(self) -> list[int]:
        """The four halfwords ahead of the slots.

        The declaring csect is two halfwords of (capacity, entry size) and the
        build overwrites the capacity with the count it actually filled; the
        directory block proper then opens with its own (entry size, count),
        which is exactly the DMMD card's own ID= and FMTS=.  Nothing in the
        cards says why the pair is repeated in the opposite order.
        """
        n = len(self.pn)
        return [n, self.size, self.ident if self.ident is not None else self.size, n]

    def image(self, phase_alloc: dict, fill=None,
              hw_per_block: int = HW_PER_BLOCK) -> list[int]:
        """The stamped table: header, one entry per PN pair, then `fill`.

        `phase_alloc` is phase -> (mm16 tape address, blocks) for the PASS
        area this directory's system builds -- `MMUBuild.phase_allocations`.
        An entry is (content number, tape address) and, with LNG=YES, the
        content's length in halfwords, which is its allocation's blocks.
        `fill` is the unused-slot value, one entry wide; it defaults to zero.
        """
        if fill is None:
            fill = (0,) * self.size
        if not self.is_dmmd:
            raise ValueError(f"{self.label}: DIRECTRY,DMMD=NO carries no PN "
                             f"list; its content comes from its DATA card")
        pn = self.pn
        if len(pn) > self.entries:
            raise ValueError(f"{self.label}: {len(pn)} entries will not fit "
                             f"{self.entries} slots")
        if self.fmts is not None and self.fmts != len(pn):
            raise ValueError(f"{self.label}: FMTS={self.fmts} but the PN list "
                             f"holds {len(pn)} entries")
        if len(fill) != self.size:
            raise ValueError(f"{self.label}: unused-slot fill is {len(fill)} "
                             f"halfwords, entries are {self.size}")
        words = self.header()
        for content, phase in pn:
            if phase not in phase_alloc:
                raise ValueError(f"{self.label}: no ALLOC for phase {phase}")
            addr, blks = phase_alloc[phase]
            entry = [content, addr]
            if self.lengths:
                entry.append(blks * hw_per_block)
            if len(entry) != self.size:
                raise ValueError(f"{self.label}: built a {len(entry)}-halfword "
                                 f"entry for SIZE={self.size}")
            words += entry
        words += list(fill) * (self.entries - len(pn))
        want = len(self.header()) + self.size * self.entries
        if len(words) != want:
            raise ValueError(f"{self.label}: built {len(words)} halfwords, "
                             f"expected {want}")
        return words


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
class LoadmodAlloc:
    """One placement of a LOADMOD member: the member, the function whose
    allocation carries it, and where that allocation is."""
    member: str              # DEUCFLM
    label: str               # DMACDFT1 -- the function
    sysid: str | None        # SYS5
    addr: str                # 44408, the five-digit FTSBB card form
    blocks: int              # BLKS
    halfwords: int | None    # SYSTEM HWDS, the expected member length
    init: str | None         # ALLOC INIT fill
    sys_source: str          # the MMUSYS member holding the LOADMOD card
    alloc_source: str        # the MMUDAT member holding the ALLOC card


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

    def loadmod_allocations(self) -> list["LoadmodAlloc"]:
        """Where each LOADMOD member goes on the tape.

        A LOADMOD statement names a member; the place is the ALLOC card of
        the function the statement sits under.  Both are keyed by that
        function's label, in two members -- the PHASE/SYSTEM cards in
        MMUSYSnx, the ALLOC card in MMUDATnx -- and the join turns
        `LOADMOD,MEMBER=DEUCFLM` into "8 blocks at 44408".  The SYSTEM
        card's HWDS is the expected member length.

        One member may be allocated several times: the DEU loads are
        carried in three copies, one per PASS area, one function each.
        """
        alloc = {st.label: st for st in self.allocations() if st.label}
        system = {st.label: st for st in self.systems() if st.label}
        # A LOADMOD card carries no label; the function it belongs to is
        # the labelled card before it in the same member.
        label_at: dict[tuple[str, int], str] = {}
        for src in {st.source for st in self.loadmods()}:
            cur = ""
            for st in self.statements(src):
                if st.label:
                    cur = st.label
                label_at[(src, st.line_no)] = cur
        out: list[LoadmodAlloc] = []
        for st in self.loadmods():
            member = st.params.get("MEMBER")
            label = label_at.get((st.source, st.line_no), "")
            a = alloc.get(label)
            if not member or a is None:
                continue
            sy = system.get(label)
            hwds = sy.params.get("HWDS") if sy else None
            out.append(LoadmodAlloc(
                member=member, label=label,
                sysid=a.params.get("SYSID"),
                addr=a.params.get("ADDR", ""),
                blocks=int(a.params.get("BLKS", "0") or 0),
                halfwords=int(hwds, 16) if hwds else None,
                init=a.params.get("INIT"),
                sys_source=st.source, alloc_source=a.source))
        return out

    def directories(self, member: str | None = None) -> list[Directory]:
        """DIRECTRY statements paired with the DATA/DMMD card that fills them.

        The pairing is by label within one member: a directory's filler is the
        next DATA (DMMD=NO) or DMMD (DMMD=YES) statement of the same label.
        """
        out: list[Directory] = []
        stmts = self.statements(member)
        for i, st in enumerate(stmts):
            if st.verb != "DIRECTRY":
                continue
            want = "DMMD" if st.params.get("DMMD", "NO").upper().startswith("Y") \
                else "DATA"
            filler = next((s for s in stmts[i + 1:]
                           if s.label == st.label and s.source == st.source
                           and s.verb == want), None)
            out.append(Directory(st, filler))
        return out

    def phase_allocations(self, sysid: str) -> dict[int, tuple[int, int]]:
        """phase -> (mm16 tape address, ALLOC blocks) for one SYSID.

        A PHASE card names the member a phase is built from; the ALLOC card of
        the same label in the SYSID's own MMUDAT member gives that member its
        place on the tape.  Roll-in phases share one label across the systems,
        so the SYSID filter is what separates the three PASS copies.
        """
        phase_of: dict[str, int] = {}
        for st in self._by_verb("PHASE"):
            if "PH" not in st.params or not st.label:
                continue
            ph = int(st.params["PH"])
            if phase_of.setdefault(st.label, ph) != ph:
                raise ValueError(f"{st.label} is phase {phase_of[st.label]} in "
                                 f"one system and {ph} in another")
        out: dict[int, tuple[int, int]] = {}
        for st in self.allocations():
            if st.params.get("SYSID") != sysid or "ADDR" not in st.params:
                continue
            ph = phase_of.get(st.label)
            if ph is None:
                continue
            entry = (mm16(st.params["ADDR"]), int(st.params.get("BLKS", 1)))
            if out.setdefault(ph, entry) != entry:
                raise ValueError(f"phase {ph} has two {sysid} allocations: "
                                 f"{out[ph]} and {entry}")
        return out

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
    directories: Annotated[bool, typer.Option("--directories",
        help="list DIRECTRY statements and the cards that fill them")] = False,
    image: Annotated[Optional[str], typer.Option("--image", metavar="LABEL",
        help="render one DMMD directory's table as halfwords")] = None,
    sysid: Annotated[str, typer.Option("--sysid", metavar="SYSID",
        help="which PASS copy --image resolves phase addresses in")] = "SYS1",
    orphans: Annotated[bool, typer.Option("--orphans",
        help="MMU members not reached from the root")] = False,
    dot: Annotated[bool, typer.Option("--dot", help="emit Graphviz DOT")] = False,
    as_json: Annotated[bool, typer.Option("--json",
        help="emit the tree as JSON")] = False,
) -> None:
    import json
    import sys

    if sum((tree, statements, alloc, systems, loadmods, datasets, orphans,
            directories, image is not None, dot, as_json)) > 1:
        raise typer.BadParameter(
            "--tree/--statements/--alloc/--systems/--loadmods/--datasets/"
            "--directories/--image/--orphans/--dot/--json are mutually "
            "exclusive")
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
    elif directories:
        for d in b.directories():
            kind = "DMMD" if d.is_dmmd else "DATA"
            where = f"{d.csect}+{d.offset:04X}" if d.csect else "(free-standing)"
            print(f"{d.source}:{d.directry.line_no}: {d.label:8} {where} "
                  f"PH={d.phase} SIZE={d.size} ENTRIES={d.entries} {kind}"
                  + (f" filled={len(d.pn)}" if d.is_dmmd else "")
                  + ("" if d.filler else "  NO FILLER CARD"), file=out)
    elif image is not None:
        d = next((x for x in b.directories() if x.label == image), None)
        if d is None:
            raise typer.BadParameter(f"no DIRECTRY statement labelled {image}")
        try:
            words = d.image(b.phase_allocations(sysid))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
        print(f"{d.label} {d.csect or '(free-standing)'}+{d.offset:04X}  "
              f"{len(words)} halfwords  ({sysid})", file=out)
        for i in range(0, len(words), 8):
            print(f"  +{i:04X}  " + " ".join(f"{w:04X}"
                                             for w in words[i:i + 8]), file=out)
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
