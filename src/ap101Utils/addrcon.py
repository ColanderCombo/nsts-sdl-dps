#!/usr/bin/env python3
#
# AP-101 address constant types: YCON, ZCON, ACON.
# 
# AddrCon  — single-value relocation (YCON/ACON/ZCON hw0)
# ZCon     — full 2-halfword ZCON indirect pointer
#
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


# RLD flag-type bytes from OBJECTGE.xpl

RLD_YCON      = 0x00     # halfword address constant
RLD_ZCON_CODE = 0x04     # ZCON code address (2-byte, no sign bit -- 0x84 negates)
RLD_ZCON_ADDR = 0x10     # ZCON address
RLD_ACON      = 0x1C     # 4-byte (fullword) address constant
RLD_BSR_ONLY  = 0x20     # ZCON: patch BSR field only
RLD_DSR_ONLY  = 0x40     # ZCON: patch DSR field only
RLD_ZCON_DATA = 0x50     # ZCON data address
RLD_SIGN      = 0x80     # bit 7 (V): interpret the text as a negative disp.

# The AP-101S main store is 256K halfwords, so no address constant the linkage
# editor resolves -- YCON, ZCON or fullword ACON -- carries more than an 18-bit
# halfword address:
ADDRESS_FIELD_BITS = 18
ADDRESS_FIELD_MASK = (1 << ADDRESS_FIELD_BITS) - 1

_RLD_FLAG_TYPES: dict[int, str] = {
    RLD_YCON: "YCON",
    RLD_ZCON_CODE: "ZCON/code",
    RLD_ZCON_ADDR: "ZCON/addr",
    RLD_ACON: "ACON",
    RLD_BSR_ONLY: "BSR-only",
    RLD_DSR_ONLY: "DSR-only",
    RLD_ZCON_DATA: "ZCON/data",
}

# Write side: the assembler's relocation-type letter -> the RLD flag byte it
# punches.  The decode table above and this map are the two directions of the
# same (asymmetric) relationship -- the decoder also recognizes foreign/HAL-S
# kinds the assembler never emits -- but both key off the named bytes above.
_FLAG_FOR_TYPE: dict[str, int] = {
    'Y': RLD_YCON,
    'A': RLD_ACON,
    'Z': RLD_ZCON_CODE,
    # A Z-con whose relocated address sits in the data subfield
    # ('Z(,sym,flags)' -- code subfield empty) relocates as ZCON/data, so
    # the linker writes HW0 and patches the DSR field (flight FCMNINIT
    # '=Z(,FPMXQETB+2,0)' links 8B6C 0001: target 0x18B6C, sector 1 -> DSR=1;
    # punching ZCON/code left HW1's DSR unpatched).
    'ZD': RLD_ZCON_DATA,
    # The two-target form's middle subfield ('Z(code,data,flags)'): a
    # DSR-only RLD -- no HW0 write, the linker just patches HW1's DSR
    # with the data target's sector.
    'ZS': RLD_DSR_ONLY,
    # An ordinary adcon carrying a negative addend.  The object text holds the
    # addend's magnitude and the RLD's V bit tells the linkage editor to
    # subtract it:
    'A-': RLD_ACON | RLD_SIGN,
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

    # The named type bytes plus their sign-bit-set / foreign variants.  (0x9C =
    # ACON|sign; 0x80/0x90/0xA0/0xC0/0xD0 are signed YCON/ZCON variants.)
    _LEN_4 = frozenset([RLD_ACON, 0x9C])
    _LEN_2 = frozenset([RLD_YCON, RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA,
                        0x80, 0x90, 0xA0, 0xC0, 0xD0])
    _ZCON_TYPES = frozenset([RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA])

    @classmethod
    def for_type(cls, type_letter: str, ll_length: int = 2) -> "AddrCon":
        """Build the AddrCon for an assembler relocation-type letter ('Y'/'A'/'Z').
        The object writer reads `.flags` from it to punch the RLD; an unknown
        letter falls back to ACON (the 4-byte adcon), matching the legacy writer."""
        return cls(_FLAG_FOR_TYPE.get(type_letter, RLD_ACON), ll_length)

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

    @property
    def field_mask(self) -> int:
        """The bits of the relocated storage this constant actually owns.

        A 2-byte YCON/ZCON owns its whole halfword.  A fullword ACON owns only
        the 18-bit address field: the remaining 14 bits may belong to an IOP 
        opcode sharing the word."""
        return ADDRESS_FIELD_MASK if self.length == 4 else self.mask

    def encode(self, target: Addr) -> int:
        """Encode a target address to the value written by this relocation.
		ZCON *always* sets 0x8000 (even if target in sector 0)
		YCON+ACON set 0x800 if the target is in sector 1+."""
        if self.length == 2:
            if self.is_zcon:
                return 0x8000 | (target.hw & 0x7FFF)
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
        field = self.field_mask
        carried = existing & self.mask & ~field
        current = existing & field
        signed_existing = -current if self.sign else current
        signed_value = -value if self.direction else value
        return carried | ((signed_existing + signed_value) & field)

    def reverse(self, existing: int, result: int, sector: int = 0) -> int:
        """Reverse this relocation: given existing and result, recover
        the target halfword address.  For sector 1+ ZCONs, pass the
        sector register value to fully decode."""
        field = self.field_mask
        current = existing & field
        signed_existing = -current if self.sign else current
        signed_value = ((result & field) - signed_existing) & field
        target_raw = (-signed_value & field) if self.direction else signed_value
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
    def from_image(cls, image: bytes | bytearray, byte_offset: int) -> ZCon:
        """Read a ZCON (4 bytes) from a memory image."""
        hw0 = (image[byte_offset] << 8) | image[byte_offset + 1]
        hw1 = (image[byte_offset + 2] << 8) | image[byte_offset + 3]
        return cls(hw0, hw1)

    @classmethod
    def prelink(cls, addend: int, flags: int = 0) -> ZCon:
        """The assembler's pre-link ZCON: HW0 holds the (masked) addend --
        the linker adds the resolved symbol address via the 'Z' RLD -- and
        the DC Z(...) source flags operand is HW1's high byte (the XC/C/CB/CD
        control bits); the BSR/DSR byte stays 0 until the linker patches it."""
        return cls(hw0=addend & 0xFFFF, hw1=(flags & 0xFF) << 8)

    def write_to_image(self, image: bytearray, byte_offset: int) -> None:
        """Write both halfwords back to a memory image."""
        image[byte_offset:byte_offset + 2] = self.hw0.to_bytes(2, 'big')
        image[byte_offset + 2:byte_offset + 4] = self.hw1.to_bytes(2, 'big')

    def to_bytes(self) -> bytearray:
        """The 4-byte big-endian ZCON image."""
        out = bytearray(4)
        self.write_to_image(out, 0)
        return out

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

    def apply(self, target: Addr, flag_type: int,
              flags: int | None = None) -> None:
        """Apply a ZCON relocation: update HW0 address and/or HW1 sector bits.

        flag_type is the RLD flag byte with sign bit masked (flags & 0x7F):
          0x04, 0x10: code address — write HW0, patch BSR if CB set
          0x50:       data address — write HW0, patch DSR
          0x20:       BSR-only     — patch BSR in HW1 only
          0x40:       DSR-only     — patch DSR in HW1 only

        flags is the unmasked flag byte: bit 7 is the sign (V), meaning HW0
        holds the absolute value of a negative displacement, to be subtracted
        rather than added.  Building the AddrCon from flag_type alone dropped
        the bit, leaving every negative-displacement ZCON 2*displacement too
        high.  Defaults to flag_type (unsigned) for callers that omit it.
        """
        sector = target.sector
        if flags is None:
            flags = flag_type

        if flag_type in (RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA):
            con = AddrCon(flags)
            self.hw0 = con.apply(self.hw0, target)

        if flag_type == RLD_BSR_ONLY:
            self.set_bsr(sector)
        elif flag_type == RLD_DSR_ONLY:
            self.set_dsr(sector)
        elif flag_type in (RLD_ZCON_CODE, RLD_ZCON_ADDR) and sector > 0:
            # not gated on the CB flag bit: a code ZCON resolving into a
            # non-zero sector takes the BSR patch whether CB is set or not
            self.set_bsr(sector)
        elif flag_type == RLD_ZCON_DATA and sector > 0:
            self.set_dsr(sector)

    def format_fields(self) -> str:
        """Format HW1 fields for display."""
        return f"XC={self.xc} C={self.c} BSR={self.bsr} DSR={self.dsr}"

    def __repr__(self) -> str:
        return (f"ZCon({self.hw0:04X},{self.hw1:04X} "
                f"target={self.target_hw:05X} "
                f"BSR={self.bsr} DSR={self.dsr})")
