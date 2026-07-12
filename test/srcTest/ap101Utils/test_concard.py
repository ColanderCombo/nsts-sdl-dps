#!/usr/bin/env python3
#
# Tests for ap101Utils.concard: CON80 control-card parser + dependency DAG.
#
# The unit tests run on synthetic cards (no external data).  The integration
# test reconciles the graph against an independent directive re-count of the
# real OI340600 CON80 deck, and is skipped when that deck is absent.
#
# Run:  python -m pytest test/srcTest/ap101Utils/test_concard.py
#  or:  python test/srcTest/ap101Utils/test_concard.py
#
import collections
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from ap101Utils.concard import (
    ConcardDeck, build_graph, parse_cards, _library_ref,
)

DECK_PATH = REPO / "code" / "OI340600" / "CON80"

# A CON80 card is 80 cols: 1-8 label, 9-71 body, 72 continuation, 73-80 seq.
def card(body: str, label: str = "", cont: bool = False, seq: str = "X 000100") -> str:
    line = f"{label:<8}{body:<63}"[:71]
    line = line + ("X" if cont else " ") + seq[:8]
    return line


class TestParse(unittest.TestCase):

    def test_comment_and_change_history(self):
        ds = parse_cards("* a banner comment\n*@ PCR=123; change\n")
        self.assertTrue(all(d.is_comment for d in ds))
        self.assertTrue(all(not d.is_directive for d in ds))

    def test_verb_operand_and_label(self):
        d = parse_cards(card("INCLUDE CONCARDS(PHASE01)"))[0]
        self.assertEqual(d.verb, "INCLUDE")
        self.assertEqual(d.operand, "CONCARDS(PHASE01)")
        self.assertEqual(d.label, "")

        d = parse_cards(card("OVERLAY BOOT", label="00000"))[0]
        self.assertEqual(d.label, "00000")
        self.assertEqual(d.verb, "OVERLAY")
        self.assertEqual(d.operand, "BOOT")

    def test_insert_drops_inline_comment(self):
        d = parse_cards(card("INSERT  FCMBOOT           *IPL BOOTSTRAP LOADER"))[0]
        self.assertEqual(d.verb, "INSERT")
        self.assertEqual(d.operand, "FCMBOOT")

    def test_comma_form_verb(self):
        # "PHASE,PH=2;" -- verb terminated by comma, operand is the remainder.
        d = parse_cards(card("PHASE,PH=2;"))[0]
        self.assertEqual(d.verb, "PHASE")
        self.assertEqual(d.operand, "PH=2;")

    def test_continuation_is_flagged_not_parsed(self):
        text = card("DMMD,ID=3,PN=((0660,27)", cont=True) + "\n" + \
               card("(0760,31),(0770,32),", cont=False)
        ds = parse_cards(text)
        self.assertFalse(ds[0].is_continuation)
        self.assertTrue(ds[1].is_continuation)
        # The continuation line must NOT yield a spurious verb.
        self.assertFalse(ds[1].is_directive)

    def test_library_ref(self):
        self.assertEqual(_library_ref("CONCARDS(PHASE01)"), ("CONCARDS", ["PHASE01"]))
        self.assertEqual(_library_ref("SYSLIBL1(A,B,C)"), ("SYSLIBL1", ["A", "B", "C"]))
        self.assertIsNone(_library_ref("FCMBOOT"))


class TestGraphSynthetic(unittest.TestCase):

    def _deck(self, tmp: Path):
        (tmp / "ROOT").write_text(
            card("PHASE 1,2") + "\n" +
            card("INCLUDE CONCARDS(CHILD)") + "\n" +
            card("INSERT  OBJ1") + "\n" +
            card("LIBRARY ZCONLIB(ZCON)") + "\n")
        (tmp / "CHILD").write_text(
            card("INSERT  OBJ1") + "\n" +          # shared leaf, two parents
            card("INCLUDE SYSLIBL1(OBJ2,OBJ3)") + "\n")
        (tmp / "ORPHAN").write_text(card("INSERT  NOPE") + "\n")
        return ConcardDeck(tmp)

    def test_build(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            g = build_graph(self._deck(Path(td)), root="ROOT")
            self.assertEqual(g.link_order(), ["ROOT", "CHILD"])
            self.assertEqual(g.object_modules(), ["OBJ1", "OBJ2", "OBJ3"])
            self.assertEqual(g.libraries(), ["ZCONLIB"])
            self.assertEqual(g.unreached, {"ORPHAN"})
            self.assertFalse(g.cycles)
            # OBJ1 is one node despite two INSERT edges.
            self.assertEqual(sum(1 for n in g.nodes.values() if n.name == "OBJ1"), 1)
            self.assertEqual(sum(1 for e in g.edges if e.child == "OBJ1"), 2)

    def test_cycle_detected_not_crashed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "A").write_text(card("INCLUDE CONCARDS(B)") + "\n")
            (tmp / "B").write_text(card("INCLUDE CONCARDS(A)") + "\n")
            g = build_graph(ConcardDeck(tmp), root="A")
            g.topo_order()
            self.assertTrue(g.cycles)


@unittest.skipUnless(DECK_PATH.is_dir(), f"CON80 deck not present at {DECK_PATH}")
class TestRealDeck(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.deck = ConcardDeck(DECK_PATH)
        cls.g = build_graph(cls.deck, root="OFTMP")

    def test_no_missing_no_cycles(self):
        self.assertFalse(any(n.missing for n in self.g.nodes.values()))
        self.g.topo_order()
        self.assertFalse(self.g.cycles)

    def test_edges_reconcile_with_recount(self):
        reachable = {n.name for n in self.g.nodes.values() if n.kind == "concard"}
        ins = ic = ie = lib = 0
        for c in reachable:
            for d in self.deck.read(c):
                if not d.is_directive:
                    continue
                if d.verb == "INSERT" and d.operand:
                    ins += 1
                elif d.verb == "INCLUDE":
                    ref = _library_ref(d.operand)
                    if ref:
                        l, m = ref
                        if l == "CONCARDS":
                            ic += len(m)
                        else:
                            ie += len(m)
                elif d.verb == "LIBRARY" and _library_ref(d.operand):
                    lib += 1
        ek = collections.Counter(e.kind for e in self.g.edges)
        self.assertEqual(ins, ek["insert"])
        self.assertEqual(ic, ek["include"])
        self.assertEqual(ie, ek["library-include"])
        self.assertEqual(lib, ek["library"])

    def test_link_order_phases_ascending(self):
        phases = [n for n in self.g.link_order()
                  if len(n) == 7 and n.startswith("PHASE") and n[5:].isdigit()]
        self.assertEqual(phases, sorted(phases))
        self.assertEqual(phases[0], "PHASE01")

    def test_mmu_decks_are_orphans(self):
        # The MMU/MMX memory-management decks are a separate build root.
        self.assertIn("MMBUILD", self.g.unreached)


if __name__ == "__main__":
    unittest.main()
