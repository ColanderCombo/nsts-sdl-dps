#!/usr/bin/env python3
#
# Tests for ap101Utils.mmubuild: MMU (mass-memory) load-build card parser
# and expansion tree.  Unit tests use synthetic cards; the integration test
# reconciles against the real OI340600 CON80 deck and skips if it is absent.
#
# Run:  python test/srcTest/ap101Utils/test_mmubuild.py
#
import collections
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from ap101Utils.mmubuild import (
    MMUDeck, build, parse_statements, parse_member_list, _split_params,
)

DECK_PATH = REPO / "code" / "OI340600" / "CON80"


def card(body: str, cont: bool = False, seq: str = " 000100AA") -> str:
    line = f"{body:<71}"[:71]
    return line + ("X" if cont else " ") + seq[:8]


class TestParse(unittest.TestCase):

    def test_labeled_alloc(self):
        st = parse_statements(card("DMACKLST ALLOC,ADDR=31000,BLKS=224,SYSID=SYS0;"))[0]
        self.assertEqual(st.label, "DMACKLST")
        self.assertEqual(st.verb, "ALLOC")
        self.assertEqual(st.params, {"ADDR": "31000", "BLKS": "224", "SYSID": "SYS0"})

    def test_bare_verb(self):
        st = parse_statements(card("         BUILD;"))[0]
        self.assertEqual(st.verb, "BUILD")
        self.assertEqual(st.label, "")
        self.assertEqual(st.params, {})

    def test_alocdesc_is_opaque_freetext(self):
        # Unquoted free text with a comma must NOT be comma-split.
        st = parse_statements(card("DMACKLST ALOCDESC,TEXT & GRAPHICS, REV;"))[0]
        self.assertEqual(st.verb, "ALOCDESC")
        self.assertEqual(st.text, "TEXT & GRAPHICS, REV")
        self.assertEqual(st.params, {})

    def test_quoted_alocdesc(self):
        st = parse_statements(card("VMARPLTG ALOCDESC,'TEXT & GRAPHICS LOG';"))[0]
        self.assertEqual(st.text, "TEXT & GRAPHICS LOG")

    def test_continuation_stitch(self):
        text = (card("SMARDD2A DMMD,ID=3,PN=((0660,27),", cont=True) + "\n" +
                card("               (0760,31)),LNG=YES;"))
        st = parse_statements(text)[0]
        self.assertEqual(st.verb, "DMMD")
        self.assertEqual(st.params["ID"], "3")
        self.assertEqual(st.params["PN"], "((0660,27),(0760,31))")
        self.assertEqual(st.params["LNG"], "YES")

    def test_split_params_respects_nesting(self):
        params, args = _split_params("PH=(10,2,13,3),SYSID=SYS1,MMDIR=44000")
        self.assertEqual(params["PH"], "(10,2,13,3)")
        self.assertEqual(params["SYSID"], "SYS1")
        self.assertEqual(args, [])

    def test_member_list_rows(self):
        text = (card("MMXTAG0  SYS0  TEXT AND GRAPHICS") + "\n" +
                card("MMUSYS0  RPL") + "\n" +
                card("MMUDAT1  * a comment only"))
        refs = parse_member_list(text)
        self.assertEqual(refs[0].member, "MMXTAG0")
        self.assertEqual(refs[0].sysid, "SYS0")
        self.assertEqual(refs[0].comment, "TEXT AND GRAPHICS")
        self.assertEqual(refs[1].flags, ["RPL"])
        self.assertEqual(refs[2].comment, "")   # '*' starts an inline comment


class TestClassifyAndBuild(unittest.TestCase):

    def _deck(self, tmp: Path):
        (tmp / "ROOT").write_text(card("MMXSUB  SYS1  a subsystem"))
        (tmp / "MMXSUB").write_text(card("DATA1") + "\n" + card("DECK1"))
        (tmp / "DATA1").write_text(
            card("AAA ALLOC,ADDR=1000,BLKS=4,SYSID=SYS1;") + "\n" +
            card("    LOADMOD,MEMBER=DECK1;"))
        # DECK1 is a CONCARD linkedit deck, not MMU cards
        (tmp / "DECK1").write_text(card("         INSERT FOO") + "\n" +
                                   card("         INCLUDE CONCARDS(BAR)"))
        (tmp / "MMUORPH").write_text(card("ZZZ ALLOC,ADDR=9,BLKS=1,SYSID=SYS1;"))
        return MMUDeck(tmp)

    def test_classification_by_content(self):
        with tempfile.TemporaryDirectory() as td:
            deck = self._deck(Path(td))
            self.assertEqual(deck.classify("MMXSUB"), "list")
            self.assertEqual(deck.classify("DATA1"), "data")
            self.assertEqual(deck.classify("DECK1"), "concard")

    def test_build_tree_and_loadmod_crosslink(self):
        with tempfile.TemporaryDirectory() as td:
            deck = self._deck(Path(td))
            b = build(deck, root="ROOT")
            self.assertEqual(b.nodes["DATA1"].kind, "data")
            self.assertEqual(b.nodes["DECK1"].kind, "concard")
            self.assertEqual(len(b.allocations()), 1)
            lm = b.loadmods()
            self.assertEqual(lm[0].params["MEMBER"], "DECK1")
            # MMU-named member not reached from ROOT is an orphan
            self.assertEqual(b.unreached, {"MMUORPH"})


@unittest.skipUnless(DECK_PATH.is_dir(), f"CON80 deck not present at {DECK_PATH}")
class TestRealDeck(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.deck = MMUDeck(DECK_PATH)
        cls.b = build(cls.deck, root="MMXMEM")

    def test_verb_counts_reconcile(self):
        raw = collections.Counter()
        for n in self.b.nodes.values():
            if n.kind == "data":
                for st in parse_statements(self.deck.text(n.name), n.name):
                    raw[st.verb] += 1
        self.assertEqual(raw["ALLOC"], len(self.b.allocations()))
        self.assertEqual(raw["SYSTEM"], len(self.b.systems()))
        self.assertEqual(raw["LOADMOD"], len(self.b.loadmods()))
        # Advisor's reconciliation targets for the real deck.
        self.assertEqual(raw["ALLOC"], 198)
        self.assertEqual(raw["SYSTEM"], 11)
        self.assertEqual(raw["LOADMOD"], 9)

    def test_loadmod_reaches_oftmp(self):
        targets = {s.params.get("MEMBER") for s in self.b.loadmods()}
        self.assertIn("OFTMP", targets)
        self.assertEqual(self.b.nodes["OFTMP"].kind, "concard")

    def test_orphans(self):
        # MMXMINI1 (mini variant) and MMBUILD (the standalone BUILD; trigger).
        self.assertIn("MMXMINI1", self.b.unreached)

    def test_sysids(self):
        self.assertEqual(self.b.sysids(),
                         ["SYS0", "SYS1", "SYS2", "SYS3", "SYS4", "SYS5", "SYS7", "SYS8"])


if __name__ == "__main__":
    unittest.main()
