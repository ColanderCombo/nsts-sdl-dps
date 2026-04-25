"""AP-101 address constant types: YCON, ZCON, ACON.

AddrCon  — single-value relocation (YCON/ACON/ZCON hw0)
ZCon     — full 2-halfword ZCON indirect pointer
"""
from __future__ import annotations

from .addr import Addr


#=============================================================================
# RLD flag decoding
#=============================================================================

def sector_decode(hw_value: int, sector: int = 0) -> int:
    """Decode a 16-bit sector-encoded halfword to an absolute halfword address.
    If bit 15 is set, uses the given sector register value.
    If bit 15 is clear, the address is in sector 0."""
    if hw_value & 0x8000:
        return (sector << 15) | (hw_value & 0x7FFF)
    return hw_value


_RLD_FLAG_TYPES: dict[int, str] = {
    0x00: "YCON",
    0x04: "ZCON/code",
    0x10: "ZCON/addr",
    0x1C: "ACON",
    0x20: "BSR-only",
    0x40: "DSR-only",
    0x50: "ZCON/data",
}


def rld_flag_type_name(flags: int) -> str:
    """Decode the RLD flag type field to a human-readable name."""
    ft = flags & 0x7F
    if ft in _RLD_FLAG_TYPES:
        return _RLD_FLAG_TYPES[ft]
    typ = (flags >> 4) & 0x07
    return _RLD_FLAG_TYPES.get(typ << 4, f"type={ft:02X}")


#=============================================================================
# AddrCon — single-value address constant
#=============================================================================

class AddrCon:
    """AP-101 address constant as described by an RLD flag byte.

    Encapsulates the type (YCON/ZCON/ACON), sign, direction, and length,
    and provides encode/decode/apply/reverse operations.

    Flag byte layout (OBJECTGE.xpl):
      bit 7: sign (V)   bits 6-4: type   bits 3-2: LL   bit 1: direction (S)   bit 0: continuation (T)
    """

    _LEN_4 = frozenset([0x1C, 0x9C])
    _LEN_2 = frozenset([0x00, 0x04, 0x10, 0x50, 0x80, 0x90, 0xA0, 0xC0, 0xD0])
    _ZCON_TYPES = frozenset([0x04, 0x10, 0x50])

    def __init__(self, flags: int, ll_length: int = 2):
        self.flags = flags
        self.sign: int = (flags >> 7) & 1
        self.direction: int = (flags >> 1) & 1
        self.flag_type: int = flags & 0x7F
        self.kind: str = rld_flag_type_name(flags)
        self.is_negative: bool = bool(self.sign)
        self.is_zcon: bool = self.flag_type in self._ZCON_TYPES

        if flags in self._LEN_4:
            self.length = 4
        elif flags in self._LEN_2:
            self.length = 2
        else:
            self.length = ll_length

    @property
    def mask(self) -> int:
        return (1 << (self.length * 8)) - 1

    def encode(self, target: Addr) -> int:
        """Encode a target address to the value written by this relocation.
        All 16-bit relocations use sector-encoded halfwords (bit 15 set
        if address is in sector 1+).  32-bit relocations use raw values."""
        if self.length == 2:
            return target.sector_encode()
        return target.hw

    def apply(self, existing: int, target: Addr) -> int:
        """Apply this relocation: compute new value from existing and target.

        Per OBJECTGE.xpl: V (sign) is the sign of the YCON in the text
        record — V=1 means existing is the absolute value of a negative
        number.  S (direction) is the direction of relocation.  So:
            result = (V==0 ? +existing : -existing)
                   + (S==0 ? +value : -value)
        """
        value = self.encode(target)
        signed_existing = -existing if self.sign else existing
        signed_value = -value if self.direction else value
        return (signed_existing + signed_value) & self.mask

    def reverse(self, existing: int, result: int, sector: int = 0) -> int:
        """Reverse this relocation: given existing and result, recover
        the target halfword address.  For sector 1+ ZCONs, pass the
        sector register value to fully decode."""
        signed_existing = -existing if self.sign else existing
        signed_value = (result - signed_existing) & self.mask
        target_raw = (-signed_value & self.mask) if self.direction else signed_value
        return sector_decode(target_raw, sector) if self.length == 2 else target_raw

    def __repr__(self) -> str:
        sign = '-' if self.sign else '+'
        return f"AddrCon({self.kind}({sign}), len={self.length})"


#=============================================================================
# ZCon — full 2-halfword ZCON
#=============================================================================

class ZCon:
    """AP-101 ZCON: a 2-halfword (4-byte) indirect address pointer.

    HW0: sector-encoded code/data halfword address
    HW1: flags — XC(9) C(8) CB(9) CD(8) BSR(7-4) DSR(3-0)

    A ZCON is written by up to three RLD entries, all pointing at HW0:
      - Address RLD (0x04/0x10/0x50): writes the target address into HW0
      - BSR-only (0x20): patches BSR field in HW1
      - DSR-only (0x40): patches DSR field in HW1
    The address RLD may also implicitly patch BSR/DSR in HW1.
    """

    def __init__(self, hw0: int = 0, hw1: int = 0):
        self.hw0 = hw0
        self.hw1 = hw1

    @classmethod
    def from_image(cls, image: bytes, byte_offset: int) -> ZCon:
        """Read a ZCON (4 bytes) from a memory image."""
        hw0 = (image[byte_offset] << 8) | image[byte_offset + 1]
        hw1 = (image[byte_offset + 2] << 8) | image[byte_offset + 3]
        return cls(hw0, hw1)

    def write_to_image(self, image: bytearray, byte_offset: int) -> None:
        """Write both halfwords back to a memory image."""
        image[byte_offset:byte_offset + 2] = self.hw0.to_bytes(2, 'big')
        image[byte_offset + 2:byte_offset + 4] = self.hw1.to_bytes(2, 'big')

    # --- HW0: target address ---

    @property
    def target_hw(self) -> int:
        """Absolute halfword address decoded from HW0 using BSR for sector."""
        return sector_decode(self.hw0, self.bsr)

    # --- HW1: decoded fields ---

    @property
    def xc(self) -> int: return (self.hw1 >> 9) & 1
    @property
    def c(self) -> int: return (self.hw1 >> 8) & 1
    @property
    def cb(self) -> int: return self.xc
    @property
    def cd(self) -> int: return self.c
    @property
    def bsr(self) -> int: return (self.hw1 >> 4) & 0xF
    @property
    def dsr(self) -> int: return self.hw1 & 0xF

    def set_bsr(self, sector: int) -> None:
        """Patch BSR field (bits 7-4) in HW1."""
        self.hw1 = (self.hw1 & 0xFF0F) | ((sector & 0xF) << 4)

    def set_dsr(self, sector: int) -> None:
        """Patch DSR field (bits 3-0) in HW1."""
        self.hw1 = (self.hw1 & 0xFFF0) | (sector & 0xF)

    # --- Apply relocations ---

    def apply(self, target: Addr, flag_type: int) -> None:
        """Apply a ZCON relocation: update HW0 address and/or HW1 sector bits.

        flag_type is the RLD flag byte with sign bit masked (flags & 0x7F):
          0x04, 0x10: code address — write HW0, patch BSR if CB set
          0x50:       data address — write HW0, patch DSR
          0x20:       BSR-only     — patch BSR in HW1 only
          0x40:       DSR-only     — patch DSR in HW1 only
        """
        sector = target.sector

        if flag_type in (0x04, 0x10, 0x50):
            con = AddrCon(flag_type)
            self.hw0 = con.apply(self.hw0, target)

        if flag_type == 0x20:
            self.set_bsr(sector)
        elif flag_type == 0x40:
            self.set_dsr(sector)
        elif flag_type in (0x04, 0x10) and sector > 0 and self.cb:
            self.set_bsr(sector)
        elif flag_type == 0x50 and sector > 0:
            self.set_dsr(sector)

    def format_fields(self) -> str:
        """Format HW1 fields for display."""
        return f"XC={self.xc} C={self.c} BSR={self.bsr} DSR={self.dsr}"

    def __repr__(self) -> str:
        return (f"ZCon({self.hw0:04X},{self.hw1:04X} "
                f"target={self.target_hw:05X} "
                f"BSR={self.bsr} DSR={self.dsr})")
