#!/usr/bin/env python3
"""Linker.dropMapDuplicateCsects: a deck INCLUDE of a csect the MAP-imported
base phase already defines contributes no TEXT.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from lnk101.linker import AddrDisp, Addr, Linker, LoadModule, Relocation, Section


def mapImport(name, addr, lds=()):
    m = LoadModule(f"<map:PHASE03/{name}>", name=f"PHASE03/{name}",
                   external=True)
    m.addSection(Section(name, 1, 'SD', address=AddrDisp(addr),
                         length=AddrDisp(8), module=m,
                         baseAddress=Addr(addr)))
    for i, (ld, a) in enumerate(lds):
        m.addSection(Section(ld, 10 + i, 'LD', module=m,
                             address=AddrDisp(a), baseAddress=Addr(a), ldId=1))
    return m


def deckCopy(name, lds=()):
    m = LoadModule(f"obj/{name}.obj", name=name)
    m.addSection(Section(name, 1, 'SD', address=AddrDisp(0),
                         length=AddrDisp(8), module=m))
    for i, (ld, a) in enumerate(lds):
        m.addSection(Section(ld, 10 + i, 'LD', module=m,
                             address=AddrDisp(a), ldId=1))
    m.relocations.append(Relocation(relId=1, posId=1, flags=0x04,
                                    address=AddrDisp(0), module=m))
    return m


def run(fn, modules):
    class Fake:
        pass
    f = Fake()
    f.modules = list(modules)
    f.warnings = []
    fn(f)
    return f


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    return bool(cond)


def main():
    drop = Linker.dropMapDuplicateCsects
    ok = True

    # 1. the import carries every global LD -> the deck copy is deleted and
    #    its now-empty module leaves the link
    imp = mapImport('MAPCSECT', 0x1000,
                    [('MAPENT1', 0x1000), ('MAPENT2', 0x1040)])
    dup = deckCopy('MAPCSECT', [('MAPENT1', 0), ('MAPENT2', 0x40)])
    f = run(drop, [imp, dup])
    ok &= check(f.modules == [imp], "duplicate module dropped from the link")
    ok &= check(not f.warnings, "no warning when the import is complete")

    # 2. an LD the import does not carry would be orphaned -> keep the copy
    imp = mapImport('MAPCSECT', 0x1000, [('MAPENT1', 0x1000)])
    dup = deckCopy('MAPCSECT', [('MAPENT1', 0), ('MAPENT2', 0x40)])
    f = run(drop, [imp, dup])
    ok &= check(len(f.modules) == 2 and dup.sections,
                "duplicate KEPT when the import lacks an LD")
    ok &= check(any('MAPENT2' in w for w in f.warnings),
                "the orphaned LD is named in the warning")

    # 3. a csect no import defines is untouched, and a module keeps the
    #    csects that are not duplicates
    imp = mapImport('MAPCSECT', 0x1000, [('MAPENT1', 0x1000)])
    other = deckCopy('OWNCSECT')                     # carries a posId=1 RLD
    other.addSection(Section('MAPCSECT', 2, 'SD', address=AddrDisp(0),
                             length=AddrDisp(8), module=other))
    other.relocations.append(Relocation(relId=1, posId=2, flags=0x04,
                                        address=AddrDisp(0), module=other))
    f = run(drop, [imp, other])
    ok &= check(len(f.modules) == 2, "a partly-duplicate module stays")
    ok &= check([s.name for s in other.sections.values()] == ['OWNCSECT'],
                "only the duplicate SD is deleted")
    ok &= check([r.posId for r in other.relocations] == [1],
                "the deleted SD's relocations go with it, the survivor's stay")

    # 4. no MAP imports at all -> nothing happens
    a, b = deckCopy('MAPCSECT'), deckCopy('OWNCSECT')
    f = run(drop, [a, b])
    ok &= check(f.modules == [a, b], "no-op without a MAP import")

    print("OK: all duplicate-csect checks passed" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
