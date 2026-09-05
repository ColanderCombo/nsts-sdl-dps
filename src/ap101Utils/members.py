"""Source member names and the filenames that hold them.

A delivered PDS member has a name of up to eight characters with no dot in
it.  On disk the member may be stored under the bare name (`GKFHOR`) or with
an extension saying what it holds: `.asm` for AP-101 assembly, `.hal` for
HAL/S, `.dfg` for a display deck.  Tools name members by the bare name and
find the file under whichever spelling the tree uses.
"""
from pathlib import Path

SUFFIXES = (".asm", ".hal", ".dfg")


def _basename(path) -> str:
    return Path(str(path).replace("\\", "/")).name


def name(path) -> str:
    """The member name of a file: its basename up to the first dot.

    `SSSRC/FAZ2.asm`, `FAZ2` and `gen/pp/FAZ2.pp.hal` all give `FAZ2`."""
    return _basename(path).split(".", 1)[0]


def filename(path, suffix: str) -> str:
    """The member name of `path` with `suffix` appended once: `FAZ2` and
    `FAZ2.hal` both give `FAZ2.hal`."""
    return name(path) + suffix


def spellings(member, suffixes=SUFFIXES) -> list[str]:
    """The filenames a member may be stored under, in search order: the
    name as given, the bare name with each suffix, then the bare name."""
    given, bare = _basename(member), name(member)
    return list(dict.fromkeys([given, *(bare + s for s in suffixes), bare]))


def find(member, dirs, suffixes=SUFFIXES):
    """The file holding `member` on `dirs`, or None.  The first directory
    holding any spelling wins; within it the spellings are tried in
    `spellings()` order."""
    for d in dirs:
        for s in spellings(member, suffixes):
            p = Path(d) / s
            if p.is_file():
                return p
    return None
