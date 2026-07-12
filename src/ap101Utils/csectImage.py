#!/usr/bin/env python3
#
# CSECT obj utilities
# 
#   * csect_words / reloc_offsets — rebuild a csect's halfword image (and
#     its relocation sites) from an AP-101 object module.
#   * CorpusReference — reference word images from a corpus JSON extracted
#     from flight-load disassemblies.
#   * diff_words — compare two images, skipping stated positions (typically
#     relocated words, whose values are link-time).
# 
# The corpus JSON maps load -> csect -> {"hal": source-unit name, "start"/"end":
# absolute halfword addresses, "words": {offset: "hex"}, "padr": [offsets]}.
# That is the format the DFG workbench's extract_corpus.py produces, but nothing
# here is display-specific: any {csect: words} reference dump fits.
#
import json
import os


# ---- object-module image extraction -----------------------------------------
def _module(obj):
    """Accept a path or an already-read objModule object file."""
    if isinstance(obj, (str, os.PathLike)):
        from ap101Utils import objModule as OM
        obj = OM.read(os.fspath(obj))
    return obj.modules[0] if hasattr(obj, "modules") else obj

def csect_words(obj, index=0, name=None):
    """The halfword image (list of ints) of a module's index-th SD csect,
    rebuilt from its text records.  obj is an object-file path, a read
    object file, or a module.  With name the SD is selected by its (8-char)
    csect name instead; KeyError if the module has no such csect."""
    from ap101Utils.objModule import scatterText
    mod = _module(obj)
    sds = [e for e in mod.esdEntries if e.type.name == "SD"]
    if name is not None:
        sd = next((e for e in sds if e.name == name[:8]), None)
        if sd is None:
            raise KeyError("no csect %r in %s (has %s)"
                           % (name, mod.name, [e.name for e in sds]))
    else:
        sd = sds[index]
    buf = scatterText((tr for tr in mod.textRecords if tr.esdId == sd.esdId),
                      size=sd.length)
    return [int.from_bytes(buf[i:i + 2], "big") for i in range(0, sd.length, 2)]


def reloc_offsets(obj):
    """Halfword offsets of a module's relocation (RLD) sites."""
    mod = _module(obj)
    return {r.address // 2 for r in mod.relocations}


# ---- image comparison --------------------------------------------------------
def diff_words(got, want, skip=()):
    """[(offset, got, want)] where the two images disagree.  Positions in
    skip (e.g. relocation sites) and None entries in either image (gaps in
    a partial reference) are ignored; a length mismatch is NOT flagged here —
    compare lengths separately."""
    skip = set(skip)
    out = []
    for i in range(min(len(got), len(want))):
        if i in skip or got[i] is None or want[i] is None:
            continue
        if (got[i] & 0xFFFF) != (want[i] & 0xFFFF):
            out.append((i, got[i] & 0xFFFF, want[i] & 0xFFFF))
    return out


# ---- corpus reference --------------------------------------------------------
def _default_corpus():
    """corpus.json from the DFG workbench, or $DFG_CORPUS."""
    env = os.environ.get("DFG_CORPUS")
    if env:
        return env
    here = os.path.dirname(__file__)
    return os.path.join(here, "..", "..", "..", "pfs", "notes", "dfg", "corpus.json")


class CorpusReference:
    """Reference word images from a flight-load corpus, keyed by the csect's
    source-unit ("hal") name."""

    def __init__(self, path=None):
        self.path = path or _default_corpus()
        self._corpus = None
        self._cache = {}

    @property
    def corpus(self):
        if self._corpus is None:
            with open(self.path) as f:
                self._corpus = json.load(f)
        return self._corpus

    def _csect(self, hal):
        for load in self.corpus.values():
            for c in load.values():
                if c["hal"] == hal:
                    return c
        return None

    def names(self):
        """Every source-unit name the corpus carries an image for."""
        return sorted({c["hal"] for load in self.corpus.values()
                       for c in load.values()})

    def image(self, hal):
        """The reference word list for hal (ints, None for gaps), or None."""
        if hal in self._cache:
            return self._cache[hal]
        c = self._csect(hal)
        if c is None:
            self._cache[hal] = None
            return None
        w = {int(k): v for k, v in c["words"].items()}
        n = c["end"] - c["start"] + 1
        img = [int(w[i], 16) if i in w else None for i in range(n)]
        self._cache[hal] = img
        return img

    def word_at(self, hal, offset):
        img = self.image(hal)
        if img and 0 <= offset < len(img) and img[offset] is not None:
            return img[offset]
        return 0

    def padr_offsets(self, hal):
        """The corpus's recorded relocation (PADR) offsets."""
        c = self._csect(hal)
        return sorted(int(p) for p in c["padr"]) if c else []

    def known(self, hal):
        return self._csect(hal) is not None
