#!/usr/bin/env python3
#
# `"linkInfo": "placement"` in a CSECT table: the entry supplies an address
# and no linkage.
#
# A CSECT table carries a section the configuration does not load when a
# ZCON in this configuration points at a module loaded in another (fcmcmp's
# load_not_in_config).  Such an entry is placed and its symbols are defined,
# so the symbol table lnk101 writes is complete for the AP-101S emulators
# that read it.  Its contents resolve no relocation: no module supplied them
# and the original link left each site as assembled.
#
# Run:  python -m pytest test/srcTest/lnk101/test_placement_only.py
#  or:  python test/srcTest/lnk101/test_placement_only.py
#
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from lnk101.linker import Linker


def _args(**kw):
    a = types.SimpleNamespace(link_order=None, force=False, strict_compools=False,
                              base_address=0)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


TABLE = {
    "#DDG9LIG": {"start": 0x0500, "end": 0x0520,
                 "contents": {"TFCMG9A": 4}},
    "#DDPLLIG": {"start": 0x0500, "end": 0x0530,
                 "linkInfo": "placement",
                 "contents": {"TFCMPFD1": 30, "TFCMPFD2": 34}},
}


class TestPlacementOnly(unittest.TestCase):

    def _load(self, table, undefined):
        linker = Linker(_args())
        for name in undefined:
            linker.undefinedSymbols[name].add(("test.obj", None))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(table, f)
            path = f.name
        linker.loadExternalSyms(path)
        Path(path).unlink()
        return linker

    def _defined(self, linker):
        # addSection keeps LDs in `module.lds`, apart from `sections`.
        names = set()
        for module in linker.modules:
            names.update(s.name.strip() for s in module.sections.values())
            names.update(s.name.strip() for s in module.lds)
        return names

    def test_placement_only_symbols_are_recorded(self):
        linker = self._load(TABLE, ["TFCMPFD1"])
        self.assertIn("TFCMPFD1", linker.placementOnlySymbols)
        self.assertIn("TFCMPFD2", linker.placementOnlySymbols)

    def test_ordinary_entry_contributes_nothing_to_the_set(self):
        linker = self._load(TABLE, ["TFCMG9A"])
        self.assertNotIn("TFCMG9A", linker.placementOnlySymbols)

    def test_the_symbol_table_is_unchanged(self):
        # A placement-only entry is placed and its contents are defined, so
        # the symbol table holds every name the table lists.
        linker = self._load(TABLE, ["TFCMPFD1"])
        defined = self._defined(linker)
        self.assertIn("#DDPLLIG", defined)
        self.assertIn("TFCMPFD1", defined)
        self.assertIn("TFCMPFD2", defined)

    def test_a_table_without_the_field_behaves_as_before(self):
        plain = {k: {kk: vv for kk, vv in v.items() if kk != "linkInfo"}
                 for k, v in TABLE.items()}
        linker = self._load(plain, ["TFCMPFD1"])
        self.assertEqual(linker.placementOnlySymbols, set())
        self.assertIn("TFCMPFD1", self._defined(linker))


if __name__ == "__main__":
    unittest.main()
