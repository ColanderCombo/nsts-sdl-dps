#!/usr/bin/env python3
#
# Read a display deck into an ordered list of directives.
# 
# A deck is a card file of comma/newline-separated directives, either
# `KEY=value` or a bare flag (`STAT`, `VARY`, `END`).  Values that are groups
# `(a,b,c)` are kept whole.  Card columns 73-80 hold a sequence/revision tail and
# `*/`…/`*@`… are trailing comments — both are stripped.
# 
# The card language is defined by the embedded lark grammar below.  Directive
# and group values are returned as VERBATIM source slices (a group's spaces
# and card breaks are significant — CHAR text glyphs, argument spans), so the
# parse tree only delimits spans; text is never reassembled from tokens.
#
import os
import re

import lark

from ap101Utils import cards

# The deck card language.  A statement is an optional branch label
# (`MIDWAY:`) plus a directive — `KEY = value` or a bare flag (`STAT`).
# Parentheses protect `,` `\n` `=` (a group value spans cards), and a key may
# embed a group (`IF (var,bit,width) = ON`).  TEXT is permissive on purpose:
# decks carry stray prose that must flow through as inert statements, so only
# the structural characters `( ) , = \n` end a run of text — the first
# top-level `=` splits key from value, later ones are value content.  The
# higher-priority LABEL beats TEXT only at a statement start (the contextual
# lexer never offers it inside values/groups).
_GRAMMAR = r"""
deck: stmt? (SEP stmt?)*
stmt: LABEL directive? | directive
directive: key (EQ value)?
key: TEXT (group | TEXT)*
value: (TEXT | EQ | group)*
group: "(" (TEXT | EQ | SEP | group)* ")"

LABEL.2: /\w+:(?=[\s,]|$)/
TEXT: /[^(),=\n]+/
EQ: "="
SEP: /[,\n]/
%ignore /[ \t]+/
"""
_PARSER = lark.Lark(_GRAMMAR, parser="lalr", start=["deck", "group"],
                    propagate_positions=True)


def _slice(text, node):
    """The verbatim source span of a parse node (token or rule tree)."""
    if isinstance(node, lark.Token):
        return str(node)
    if node.meta.empty:
        return ""
    return text[node.meta.start_pos:node.meta.end_pos]


# Deck search path: the display source decks live beside the HAL sources.
_DEFAULT_DECK_DIRS = ("SSSRC", "APPLSRC")


def deck_dirs(root=None):
    """Directories searched for a deck by name.  `root` defaults to the
    OI340600 code tree ($DFG_DECK_ROOT or code/OI340600 under the repo)."""
    if root is None:
        root = os.environ.get("DFG_DECK_ROOT")
    if root is None:
        here = os.path.dirname(__file__)
        root = os.path.join(here, "..", "..", "code", "OI340600")
    return [os.path.join(root, d) for d in _DEFAULT_DECK_DIRS]


def find_deck(name, root=None):
    """Absolute path of the deck named `name`, or None."""
    for d in deck_dirs(root):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def resolve_deck(name_or_path, root=None):
    """Path of the deck: an explicit path is used as-is, a bare name is
    searched for on the deck dirs.  None if neither resolves."""
    if os.path.sep in name_or_path and os.path.exists(name_or_path):
        return name_or_path
    return find_deck(name_or_path, root)


def _strip(line):
    """The code part of a card ('' for a `*` full-line comment): the
    positional col-72 cut + trimmed-tail handling (`cards.code_text`)."""
    if line[:1] == "*":
        return ""
    return cards.code_text(line)


def read_cards(path):
    """The deck's code cards as raw text lines (comment tails removed), in
    order — the verbatim directive text echoed into the generated HAL."""
    lines = open(path, errors="replace").read().splitlines()
    return [s for s in map(_strip, lines) if s]


def directives(name_or_path, root=None):
    """(KEY, value_or_None) directives in deck order.  A bare token
    (`STAT`/`VARY`/`END`/`CARRTN`) gives (TOKEN, None); a group value keeps
    its parentheses.  Inline `*/`…/`*@`… comments are dropped to end-of-line."""
    path = resolve_deck(name_or_path, root)
    if path is None:
        raise FileNotFoundError("no deck named %r on %s" % (name_or_path, deck_dirs(root)))
    text = []
    for s in read_cards(path):
        # Drop inline `*/`/`*@` comments to END of line — not to the first
        # comma: comment bodies hold commas and parens that would unbalance
        # the grammar's group rule.
        s = re.sub(r"\*[/@].*$", "", s)
        if s.strip():
            text.append(s)
    return _split_directives("\n".join(text))


def _split_directives(blob):
    """Parse `blob` into (KEY, value_or_None) pairs via the deck grammar.
    A branch label (`MIDWAY:`) is its own pair — it marks the structure the
    NEXT directive emits (a `TEST=(...,MIDWAY)` elsewhere targets it) — and
    keeps its colon.  A `KEY=` with an empty value yields '' (not None)."""
    res = []
    for stmt in _PARSER.parse(blob, start="deck").children:
        if not isinstance(stmt, lark.Tree):
            continue                             # a bare SEP separator
        for node in stmt.children:
            if isinstance(node, lark.Token):     # the LABEL prefix
                res.append((str(node), None))
                continue
            children = node.children             # [key] or [key, EQ, value]
            k = _slice(blob, children[0]).strip()
            if len(children) == 1:
                if k:                            # else a blank run between separators
                    res.append((k, None))
            else:
                res.append((k, _slice(blob, children[2]).strip()))
    return res


def group_parts(v):
    """Top-level comma split of a `(...)` group value into stripped parts —
    subscript-aware: commas inside a nested group (a 2-D array ref like
    `VAR$(1,2)`) never split.  A parenless value is split the same way;
    empty parts survive (`(a,,b)` -> ['a', '', 'b'])."""
    t = v if v.startswith("(") else "(%s)" % v
    parts, cur = [], []
    for c in _PARSER.parse(t, start="group").children:
        if isinstance(c, lark.Token) and c.type == "SEP" and c == ",":
            parts.append(cur); cur = []
        else:
            cur.append(c)
    parts.append(cur)
    def span(run):
        if not run:
            return ""
        a = run[0].start_pos if isinstance(run[0], lark.Token) \
            else run[0].meta.start_pos
        b = run[-1].end_pos if isinstance(run[-1], lark.Token) \
            else run[-1].meta.end_pos
        return t[a:b].strip()
    return [span(run) for run in parts]


def atom(s):
    """Typed directive argument: a plain integer becomes an int; anything
    else (names, labels, `nnnA` coordinates) stays the stripped string."""
    s = s.strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def vparm_attrs(val):
    """Parse the inner of `VPARM=(NAME=x,ATTR=H,FMT=5.0,…)` into a dict."""
    a = {}
    for p in group_parts(val.strip()):
        if "=" in p:
            k, v = p.split("=", 1)
            a[k.strip()] = v.strip()
    return a


# Directives that edit an existing deck (change-history noise) — dropped before
# encoding.
EDIT_DIRECTIVES = frozenset(("ADD", "CHANGE", "DELETE", "CR", "PCR", "RESEQ",
                             "RENUM", "RESEQUENCED"))


def encodable_directives(name_or_path, root=None):
    """`directives()` with the deck-editing directives removed."""
    return [(k, v) for k, v in directives(name_or_path, root)
            if k not in EDIT_DIRECTIVES]
