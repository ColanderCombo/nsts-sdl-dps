#!/usr/bin/env python3
#
# Tests for tools.mmubuild: MMU (mass-memory) load-build card parser
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

from tools.mmubuild import (
    Directory, HW_PER_BLOCK, MMUDeck, build, parse_statements,
    parse_member_list, _split_params,
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


class TestDirectory(unittest.TestCase):
    """The DIRECTRY/DMMD pass, on synthetic cards.

    A DIRECTRY card declares a table the mass memory build writes: how wide an
    entry is and how many slots there are.  The card that fills it is a DATA
    card (DMMD=NO, a free-standing table) or a DMMD card (DMMD=YES, a table
    stamped into a linked phase's csect) of the same label.  A DMMD entry
    names its content by number and its location by phase, so the table can
    only be built once the phase-to-tape-address map is known.
    """

    # two slots' worth of shape, so a table is small enough to write out
    HEAD = "DIRTAB   DIRECTRY,CSECT=#XXTAB,OFFSET=0,SIZE=3,ENTRIES=4,DMMD=YES,PH=9;"
    FILL = "DIRTAB   DMMD,ID=3,FMTS=2,PN=((0110,31),(0220,32)),LNG=YES;"
    # phase -> (mm16 tape address, ALLOC blocks)
    ALLOC = {31: (0x1F00, 3), 32: (0x1F03, 2)}

    def _dir(self, head=None, fill=None):
        stmts = parse_statements(card(head or self.HEAD) + "\n"
                                 + card(fill or self.FILL))
        return Directory(stmts[0], stmts[1])

    def test_pairs_the_two_cards(self):
        d = self._dir()
        self.assertTrue(d.is_dmmd)
        self.assertEqual((d.csect, d.offset, d.size, d.entries, d.phase),
                         ("#XXTAB", 0, 3, 4, 9))
        self.assertEqual(d.pn, [(110, 31), (220, 32)])
        self.assertEqual((d.ident, d.fmts, d.lengths), (3, 2, True))

    def test_header_is_the_two_cards_own_numbers(self):
        # (count, SIZE) from the DIRECTRY card, then (ID, FMTS) from the DMMD
        self.assertEqual(self._dir().header(), [2, 3, 3, 2])

    def test_image_resolves_each_entry_through_its_phase(self):
        img = self._dir().image(self.ALLOC, fill=(0, 0xFFFF, 0))
        self.assertEqual(img, [2, 3, 3, 2,
                               110, 0x1F00, 3 * HW_PER_BLOCK,
                               220, 0x1F03, 2 * HW_PER_BLOCK,
                               0, 0xFFFF, 0,
                               0, 0xFFFF, 0])
        # 4 header halfwords + SIZE x ENTRIES, whatever is filled
        self.assertEqual(len(img), 4 + 3 * 4)

    def test_size_two_drops_the_length_field(self):
        d = self._dir(head=self.HEAD.replace("SIZE=3", "SIZE=2"),
                      fill=self.FILL.replace("ID=3", "ID=2")
                                    .replace(",LNG=YES", ""))
        img = d.image(self.ALLOC, fill=(0, 0))
        self.assertFalse(d.lengths)
        self.assertEqual(img[:8], [2, 2, 2, 2, 110, 0x1F00, 220, 0x1F03])

    def test_refuses_a_phase_with_no_allocation(self):
        with self.assertRaises(ValueError):
            self._dir().image({31: (0x1F00, 3)})

    def test_refuses_more_entries_than_slots(self):
        pn = ",".join(f"({n:04d},31)" for n in range(110, 170, 10))
        d = self._dir(fill=f"DIRTAB   DMMD,ID=3,FMTS=6,PN=({pn}),LNG=YES;")
        with self.assertRaises(ValueError):
            d.image(self.ALLOC)

    def test_refuses_a_count_the_card_contradicts(self):
        d = self._dir(fill=self.FILL.replace("FMTS=2", "FMTS=3"))
        with self.assertRaises(ValueError):
            d.image(self.ALLOC)

    def test_a_data_directory_has_no_pn_list(self):
        stmts = parse_statements(
            card("DIRTAB   DIRECTRY,SIZE=2,ENTRIES=8,DMMD=NO,PH=2;") + "\n"
            + card("DIRTAB   DATA,ENTRY=(1,1),HDATA=2000;"))
        d = Directory(stmts[0], stmts[1])
        self.assertFalse(d.is_dmmd)
        self.assertEqual(d.csect, "")
        self.assertEqual(d.pn, [])
        with self.assertRaises(ValueError):
            d.image({})


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
        # counts for the delivered deck
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

    def test_every_directry_card_has_its_filler(self):
        ds = self.b.directories()
        raw = sum(1 for st in self.b.statements() if st.verb == "DIRECTRY")
        self.assertEqual(len(ds), raw)
        # a DIRECTRY with no filler declares a table nothing writes, which
        # would render as an empty one rather than fail
        self.assertEqual([d.label for d in ds if d.filler is None], [])
        self.assertEqual(sorted(d.filler.verb for d in ds).count("DMMD"),
                         sum(1 for d in ds if d.is_dmmd))

    def test_a_dmmd_directory_renders(self):
        for d in (x for x in self.b.directories() if x.is_dmmd):
            img = d.image(self.b.phase_allocations("SYS1"))
            self.assertEqual(len(img), 4 + d.size * d.entries)
            self.assertEqual(img[:4], [len(d.pn), d.size, d.size, len(d.pn)])

    def test_phase_allocations_separate_the_pass_copies(self):
        # the roll-in display phases share one label across the systems, so
        # without the SYSID filter a phase would take whichever came last
        a1 = self.b.phase_allocations("SYS1")
        a2 = self.b.phase_allocations("SYS2")
        shared = set(a1) & set(a2)
        self.assertTrue(shared)
        self.assertEqual([p for p in shared if a1[p][0] == a2[p][0]], [])

    def test_sysids(self):
        self.assertEqual(self.b.sysids(),
                         ["SYS0", "SYS1", "SYS2", "SYS3", "SYS4", "SYS5", "SYS7", "SYS8"])


if __name__ == "__main__":
    unittest.main()
