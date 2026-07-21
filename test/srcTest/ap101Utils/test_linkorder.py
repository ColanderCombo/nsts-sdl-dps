#!/usr/bin/env python3
#
# Tests for ap101Utils.linkorder (the linkorder.json pins loader).
# Synthetic data only -- the real pins file lives outside the repo.
#
# Run:  python -m pytest test/srcTest/ap101Utils/test_linkorder.py
#  or:  python test/srcTest/ap101Utils/test_linkorder.py
#
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from ap101Utils.linkorder import LinkOrder, McPins

PINS = {
    "zconPool": ["#ZAAA", "#ZBBB", "#QX", "#ZANCHOR", "#ZLAST"],
    "orphanFlush": ["$0ONE", "$0TWO"],
    "poolBlocks": [
        {"after": "#ZANCHOR", "waves": [["MODA", "MODB"], ["MODC"]]},
    ],
    "mc": {
        "alpha": {
            "anchor": "#DMARKER",
            "streams": {"0": ["#XA", "+2", "#XB"], "2": ["$0C"]},
            "pool": ["#ZP1", "#ZP2"],
            "poolAfter": "#ZLAST",
        },
        "beta": {
            "anchor": "#DOTHER",
            "preblock": ["#EPRE", "@0PRE"],
            "preblockAfter": "#DOTHER",
            "wave1Order": ["W1", "W2"],
            "compoolOrder": ["#PCA"],
            "codeOrder": ["C1"],
            "codeBank": 3,
        },
    },
}


class TestEmpty(unittest.TestCase):
    """No pins file: deterministic fallbacks, every pinned behavior inert."""

    def test_inert(self):
        lo = LinkOrder()
        self.assertEqual(lo.zcon_sort_key("#ZFOO"), (1, 0, 0, "#ZFOO"))
        self.assertEqual(lo.pool_block_override({"M": ["#QA"]}, set()), {})
        self.assertEqual(lo.wave_modules(), set())
        self.assertIsNone(lo.mc_for({"#DMARKER"}))
        self.assertEqual(lo.orphanFlush, [])
        self.assertEqual(McPins().codeBank, 2)

    def test_fallback_sorts_by_name(self):
        lo = LinkOrder()
        names = ["#ZB", "#QA", "#ZA"]
        self.assertEqual(sorted(names, key=lo.zcon_sort_key),
                         ["#QA", "#ZA", "#ZB"])


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        json.dump(PINS, self.tmp)
        self.tmp.close()
        self.lo = LinkOrder.load(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink()

    def test_mc_sections(self):
        alpha = self.lo.mc["alpha"]
        self.assertEqual(alpha.streams, {0: ["#XA", "+2", "#XB"],
                                         2: ["$0C"]})  # int bank keys
        beta = self.lo.mc["beta"]
        self.assertEqual(beta.streams, {})
        self.assertEqual(beta.codeBank, 3)
        self.assertEqual(beta.preblockAfter, "#DOTHER")

    def test_mc_for_matches_anchor(self):
        self.assertEqual(self.lo.mc_for({"#DMARKER", "#DX"}).name, "alpha")
        self.assertEqual(self.lo.mc_for({"#DOTHER"}).name, "beta")
        self.assertIsNone(self.lo.mc_for({"#DNONE"}))
        # first match in file order wins
        self.assertEqual(self.lo.mc_for({"#DMARKER", "#DOTHER"}).name,
                         "alpha")

    def test_pool_order_beats_names(self):
        # Known names sort by pool position; unknowns after, by name.
        names = ["#ZNEW", "#ZLAST", "#ZAAA", "#ANEW"]
        self.assertEqual(sorted(names, key=self.lo.zcon_sort_key),
                         ["#ZAAA", "#ZLAST", "#ANEW", "#ZNEW"])

    def test_override_wins(self):
        # Pin #ZLAST (pool ordinal 4) ahead of #ZBBB (ordinal 1).
        ov = {"#ZLAST": (0, 0, 1, "#ZLAST")}
        self.assertEqual(sorted(["#ZLAST", "#ZBBB"],
                                key=lambda n: self.lo.zcon_sort_key(n, ov)),
                         ["#ZLAST", "#ZBBB"])

    def test_pool_block_sequence(self):
        # Wave #Z thunks in wave order, then the wave's NEW #Q ERs
        # EBCDIC-sorted (cp037: letters sort before digits).
        qers = {"MODA": ["#QVV1S3P", "#QVV10S3"], "MODB": ["#QBASE"],
                "MODC": ["#QVV10S3", "#QC"]}
        ov = self.lo.pool_block_override(qers, {"#QBASE"})
        anchor = PINS["zconPool"].index("#ZANCHOR")
        seq = ["#ZMODA", "#ZMODB", "#QVV1S3P", "#QVV10S3", "#ZMODC", "#QC"]
        self.assertEqual(ov, {n: (0, anchor, i + 1, n)
                              for i, n in enumerate(seq)})
        # The whole block sorts between the anchor and the next pool member.
        key = self.lo.zcon_sort_key
        self.assertLess(key("#ZANCHOR"), key("#ZMODA", ov))
        self.assertLess(key("#ZMODA", ov), key("#ZLAST"))

    def test_mc_pool_after(self):
        ov = self.lo.pool_override(self.lo.mc["alpha"])
        after = PINS["zconPool"].index("#ZLAST")
        self.assertEqual(ov["#ZP1"], (0, after, 1000, "#ZP1"))
        self.assertEqual(ov["#ZP2"], (0, after, 1001, "#ZP2"))

    def test_malformed(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        bad.write("[1, 2]")
        bad.close()
        try:
            with self.assertRaises(ValueError):
                LinkOrder.load(bad.name)
        finally:
            Path(bad.name).unlink()


if __name__ == "__main__":
    unittest.main()
