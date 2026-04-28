#!/usr/bin/env python3
#
# Truth-table tests for AddrCon.apply() / reverse().
#
# Per OBJECTGE.xpl (PASS2.PROCS): the YCON sign V indicates whether the
# existing TXT halfword should be treated as positive or negative; the
# direction S indicates the direction of relocation.  So:
#     result = (V==0 ? +existing : -existing) + (S==0 ? +value : -value)
#
# Run:  python -m pytest test/test_addrcon.py
#  or:  python test/test_addrcon.py
#
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "LNK101"))

from lnk101.addr import Addr
from lnk101.addrcon import AddrCon


def flags_for(sign: int, direction: int) -> int:
    # YCON, length 2: bits 6-4 type=000, bits 3-2 LL=00, bit 1 = direction, bit 7 = sign
    return (sign << 7) | (direction << 1)


class TestAddrConApply(unittest.TestCase):

    def _check(self, sign, direction, existing, target_hw, expected):
        con = AddrCon(flags_for(sign, direction), 2)
        self.assertEqual(con.sign, sign)
        self.assertEqual(con.direction, direction)
        target = Addr.from_hw(target_hw)
        got = con.apply(existing, target)
        self.assertEqual(
            got, expected,
            f"V={sign} S={direction} existing=0x{existing:04X} target=0x{target_hw:05X}: "
            f"got 0x{got:04X}, expected 0x{expected:04X}")

    def test_v0_s0_sector0(self):
        # Standard positive YCON, target in sector 0: result = existing + value
        self._check(0, 0, existing=0x0014, target_hw=0x023F4, expected=0x2408)

    def test_v0_s1_sector0(self):
        # V=0, S=1: result = existing - value
        self._check(0, 1, existing=0x4000, target_hw=0x023F4, expected=0x4000 - 0x23F4)

    def test_v1_s0_sector0_issue11(self):
        # The issue #11 case: V=1, S=0 — result = -existing + value = value - existing
        # existing=0x14, target #PCZ2COM @ hw 0x023F4 → expected 0x023F4 - 0x14 = 0x23E0
        self._check(1, 0, existing=0x0014, target_hw=0x023F4, expected=0x23E0)

    def test_v1_s1_sector0(self):
        # V=1, S=1: result = -existing - value
        self._check(1, 1, existing=0x0014, target_hw=0x023F4,
                    expected=(-0x0014 - 0x23F4) & 0xFFFF)

    def test_v0_s0_sector8(self):
        # Target in sector 8 (hw >= 0x8000): sector_encode sets bit 15.
        # result = existing + (0x8000 | (target & 0x7FFF))
        self._check(0, 0, existing=0x0019, target_hw=0x448F8, expected=0xC911)

    def test_v1_s0_sector8(self):
        # V=1 with sector-1+ target: result = (sector-encoded value) - existing
        self._check(1, 0, existing=0x0019, target_hw=0x448F8,
                    expected=(0xC8F8 - 0x0019) & 0xFFFF)


class TestAddrConZconBit15(unittest.TestCase):
    """ZCON addresses (flag 0x04/0x10/0x50) are always BSR/DSR-relative,
    so bit 15 must always be set in the encoded halfword — even when the
    target is in sector 0."""

    def _check_zcon(self, flag_type, existing, target_hw, expected):
        from lnk101.addrcon import AddrCon
        from lnk101.addr import Addr
        con = AddrCon(flag_type, 2)
        target = Addr.from_hw(target_hw)
        got = con.apply(existing, target)
        self.assertEqual(
            got, expected,
            f"flag=0x{flag_type:02X} existing=0x{existing:04X} target=0x{target_hw:05X}: "
            f"got 0x{got:04X}, expected 0x{expected:04X}")

    def test_zcon_data_sector0(self):
        # flag 0x50 (ZCON/data) to sector-0 target #PCMDKMD+A @ hw 0x3858
        # Reference build emits 0xB858 (bit 15 set), our linker was emitting 0x3858.
        self._check_zcon(0x50, existing=0, target_hw=0x3858, expected=0xB858)

    def test_zcon_code_sector0(self):
        self._check_zcon(0x04, existing=0, target_hw=0x3858, expected=0xB858)

    def test_zcon_addr_sector0(self):
        self._check_zcon(0x10, existing=0, target_hw=0x3858, expected=0xB858)

    def test_zcon_data_sector8(self):
        # Sector-1+ target already had bit 15 set; behavior unchanged.
        self._check_zcon(0x50, existing=0, target_hw=0x448F8, expected=0xC8F8)


class TestUnresolvedRldHandling(unittest.TestCase):
    """Issue #22: when an RLD's target is unresolved, the IBM linker leaves
    YCON/ACON TXT completely untouched, but for ZCON address types
    (0x04/0x10/0x50) it still sets bit 15 of HW0 (the structural marker).
    This mirrors the linker's per-flag-type branching for unresolved RLDs.
    """

    def _apply_unresolved(self, flag, existing_hw0, existing_hw1=0x0000):
        """Mimic linker behavior for an unresolved RLD: returns (hw0, hw1)."""
        image = bytearray(existing_hw0.to_bytes(2, 'big')
                          + existing_hw1.to_bytes(2, 'big'))
        flag_type = flag & 0x7F
        if flag_type in (0x04, 0x10, 0x50):
            image[0] |= 0x80
        return (image[0] << 8) | image[1], (image[2] << 8) | image[3]

    def test_zcon_data_unresolved_sets_bit15(self):
        # The DGL4SUP @ 0AA3C case: OBJ has 0x0038, IBM linker writes 0x8038.
        hw0, hw1 = self._apply_unresolved(0x50, 0x0038, 0x0000)
        self.assertEqual(hw0, 0x8038)
        self.assertEqual(hw1, 0x0000)

    def test_zcon_data_unresolved_bit15_idempotent(self):
        # If OBJ already had bit 15 (shouldn't happen, but defensive):
        # OR is idempotent, ADD would have wrapped.
        hw0, _ = self._apply_unresolved(0x50, 0x8038)
        self.assertEqual(hw0, 0x8038)

    def test_zcon_code_unresolved_sets_bit15(self):
        hw0, _ = self._apply_unresolved(0x04, 0x0038)
        self.assertEqual(hw0, 0x8038)
        hw0, _ = self._apply_unresolved(0x10, 0x0038)
        self.assertEqual(hw0, 0x8038)

    def test_ycon_unresolved_v0_untouched(self):
        # The 0x8F/0x91/0x95 V=0 YCON cases: TXT bytes stay put.
        hw0, _ = self._apply_unresolved(0x00, 0x0004)
        self.assertEqual(hw0, 0x0004)

    def test_ycon_unresolved_v1_untouched(self):
        # The CGL4SUP @ 0x89 V=1 YCON case: pre-358c1d1 bug wrote FFFC;
        # post-fix leaves the absolute-value 0x0004 untouched.
        hw0, _ = self._apply_unresolved(0x80, 0x0004)
        self.assertEqual(hw0, 0x0004)

    def test_bsr_only_unresolved_untouched(self):
        # 0x20 (BSR-only) targets HW1; we never modify it when unresolved.
        hw0, hw1 = self._apply_unresolved(0x20, 0x8038, 0x0011)
        self.assertEqual((hw0, hw1), (0x8038, 0x0011))

    def test_dsr_only_unresolved_untouched(self):
        # 0x40 (DSR-only) ditto.
        hw0, hw1 = self._apply_unresolved(0x40, 0x8038, 0x0011)
        self.assertEqual((hw0, hw1), (0x8038, 0x0011))


class TestAddrConReverse(unittest.TestCase):
    """Round-trip: apply then reverse should recover the target hw."""

    def _roundtrip(self, sign, direction, existing, target_hw):
        con = AddrCon(flags_for(sign, direction), 2)
        target = Addr.from_hw(target_hw)
        result = con.apply(existing, target)
        # reverse needs the right sector to decode bit-15 back; pass target's sector
        recovered = con.reverse(existing, result, sector=target.sector)
        self.assertEqual(
            recovered, target_hw,
            f"V={sign} S={direction} target=0x{target_hw:05X}: "
            f"existing=0x{existing:04X} result=0x{result:04X} recovered=0x{recovered:05X}")

    def test_roundtrip_all_combos(self):
        for sign in (0, 1):
            for direction in (0, 1):
                # sector-0 target
                self._roundtrip(sign, direction, 0x0014, 0x023F4)
                # sector-8 target (exercises sector_encode)
                self._roundtrip(sign, direction, 0x0019, 0x448F8)


if __name__ == "__main__":
    unittest.main()
