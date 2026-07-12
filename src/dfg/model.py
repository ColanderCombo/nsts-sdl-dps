#!/usr/bin/env python3
#
# The encoded-compool data model: annotated word segments.
#
# The encoder emits a list of `Segment`s rather than a bare word list.  Each
# segment groups the words produced by one deck directive (or one structural role)
# with the `--`/`-` comment cards that annotate them — so the same structure drives
# both the encoded image and its self-documenting HAL source.
#
# One word position is not a plain literal:
#   * `Padr(name)`  — points at a live compool variable; the DFG emits a typed
#     `NAME(name)` initializer and the HAL/S compiler relocates it.
#


class Error(Exception):
    """The generator cannot derive a word the deck requires."""


class Padr:
    """A dynamic-field pointer word — a reference to compool variable `name`."""
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "Padr(%r)" % self.name


class Segment:
    """A run of words with the comment cards that describe them.

    section  — 'header' | 'kvt' | 'static' | 'ddt' (which region it belongs to)
    comments — annotation lines (content only, e.g. '-- XC = 5'); each becomes a
               `C` comment card in the HAL output.  None => annotate per word.
    words    — list of int | fcw.FCW | Padr, in emission order (FCW marks a
               DEU-interpreted word; a bare int is GPC-side data)
    meta     — optional per-segment hints for the emitter (e.g. header labels)
    """
    __slots__ = ("section", "comments", "words", "meta")

    def __init__(self, section, comments, words, meta=None):
        self.section = section
        self.comments = None if comments is None else list(comments)
        self.words = list(words)
        self.meta = meta


def flatten(segments):
    """The full ordered word list (ints and `Padr`s) across all segments."""
    return [w for seg in segments for w in seg.words]
