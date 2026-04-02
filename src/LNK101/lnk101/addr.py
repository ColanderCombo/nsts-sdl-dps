#
# Addresses convertable between byte/halfwords
# 
# 
# The AP-101 processor *generally* uses halfword (16-bit) adresses
#   - The NIA is halfwords
#   - Branch/call targets are halfword 
#   - Data may use byte or halfword addressing depending on instruction
# 
# When reading object files:
#   - ESD section addresses/lengths are in BYTES
#   - RLD relocation offsets are in BYTES
#   - LD label offsets are in BYTES
# 
# When writing to FCM:
#   - Code address relocations (flags 0x00, 0x04) write HALFWORD addresses
#   - ZCON uses 0x04 (2-byte, no negation) - addresses are never negated
#   - This conversion happens in _applyRelocationValue()
# 
class Addr:
    __slots__ = ('_b',)

    def __init__(self, b=0):
        self._b = int(b)

    @classmethod
    def from_hw(cls, hw):
        return cls(int(hw) << 1)

    @property
    def bytes(self):
        return self._b

    @property
    def hw(self):
        return self._b >> 1

    @property
    def sector(self):
        """AP-101 sector number (0-7)."""
        return self._b >> 16

    def sector_encode(self):
        """Encode halfword for 16-bit AP-101: 0x8000 | (hw & 0x7FFF) if sector > 0."""
        hw = self.hw
        return 0x8000 | (hw & 0x7FFF) if hw >= 0x8000 else hw

    def align(self, boundary):
        """Round up to next multiple of boundary (bytes)."""
        r = self._b % boundary
        return Addr(self._b + (boundary - r)) if r else Addr(self._b)

    def end(self, length):
        return Addr(self._b + int(length))

    # Arithmetic
    def __add__(self, other):
        if isinstance(other, (AddrDisp, Addr)):
            return Addr(self._b + other._b)
        return Addr(self._b + int(other))
    def __radd__(self, other):
        return Addr(int(other) + self._b)
    def __sub__(self, other):
        if isinstance(other, Addr):
            return AddrDisp(self._b - other._b)
        if isinstance(other, AddrDisp):
            return Addr(self._b - other._b)
        return Addr(self._b - int(other))
    def __rsub__(self, other):
        return AddrDisp(int(other) - self._b)
    def __mul__(self, other):
        return Addr(self._b * int(other))
    def __rmul__(self, other):
        return Addr(int(other) * self._b)

    # Comparison (works with Addr, AddrDisp, int, and None)
    def __eq__(self, other):
        if other is None: return False
        if isinstance(other, (Addr, AddrDisp)):
            return self._b == other._b
        return self._b == other
    def __ne__(self, other):
        return not self.__eq__(other)
    def __lt__(self, other):
        if isinstance(other, (Addr, AddrDisp)):
            return self._b < other._b
        return self._b < other
    def __le__(self, other):
        if isinstance(other, (Addr, AddrDisp)):
            return self._b <= other._b
        return self._b <= other
    def __gt__(self, other):
        if isinstance(other, (Addr, AddrDisp)):
            return self._b > other._b
        return self._b > other
    def __ge__(self, other):
        if isinstance(other, (Addr, AddrDisp)):
            return self._b >= other._b
        return self._b >= other

    def __hash__(self):
        return hash(self._b)
    def __int__(self):
        return self._b
    def __index__(self):
        return self._b
    def __bool__(self):
        return self._b != 0
    @property
    def x(self):
        """5-digit uppercase hex halfword string, e.g. '100EA'."""
        return f"{self._b >> 1:05X}"

    def __repr__(self):
        return f"Addr(0x{self._b:06X}/hw:{self._b >> 1:05X})"
    def __format__(self, spec):
        return format(self._b, spec)


#=============================================================================
# AP-101 encoding helpers
#=============================================================================

def sector_decode(hw_value):
    """Decode a 16-bit sector-encoded halfword to an absolute halfword address.
    Sector 0: value as-is.  Sector 1+: bit 15 set, offset in bits 14-0."""
    if hw_value & 0x8000:
        return 0x8000 + (hw_value & 0x7FFF)
    return hw_value


def decode_zcon_hw1(hw1):
    """Decode the second halfword of a ZCON entry.
    Returns dict with XC, C, CB, BSR, DSR fields."""
    return {
        "XC":  (hw1 >> 9) & 1,
        "C":   (hw1 >> 8) & 1,
        "CB":  (hw1 >> 9) & 1,   # CB = XC in ZCON context
        "BSR": (hw1 >> 4) & 0xF,
        "DSR":  hw1 & 0xF,
    }


def format_zcon_fields(zcon):
    """Format ZCON decoded fields for display."""
    return (f"XC={zcon['XC']} C={zcon['C']} "
            f"BSR={zcon['BSR']} DSR={zcon['DSR']}")


# RLD flag type names (bits 6-4 of flag byte, sign bit masked)
RLD_FLAG_TYPES = {
    0x00: "YCON",
    0x04: "ZCON/code",
    0x10: "ZCON/addr",
    0x1C: "ACON",
    0x20: "BSR-only",
    0x40: "DSR-only",
    0x50: "ZCON/data",
}


def rld_flag_type_name(flags):
    """Decode the RLD flag type field to a human-readable name."""
    ft = flags & 0x7F
    if ft in RLD_FLAG_TYPES:
        return RLD_FLAG_TYPES[ft]
    # Fall back to just the type bits
    typ = (flags >> 4) & 0x07
    return RLD_FLAG_TYPES.get(typ << 4, f"type={ft:02X}")


class AddrDisp:
    #
    # A signed displacement (distance) in bytes between two addresses.
    #
    __slots__ = ('_b',)

    def __init__(self, b=0):
        self._b = int(b)

    @classmethod
    def from_hw(cls, hw):
        return cls(int(hw) << 1)

    @property
    def bytes(self):
        return self._b

    @property
    def hw(self):
        return self._b >> 1

    # Arithmetic
    def __add__(self, other):
        if isinstance(other, Addr):
            return Addr(self._b + other._b)
        if isinstance(other, AddrDisp):
            return AddrDisp(self._b + other._b)
        return AddrDisp(self._b + int(other))
    def __radd__(self, other):
        if isinstance(other, Addr):
            return Addr(other._b + self._b)
        return AddrDisp(int(other) + self._b)
    def __sub__(self, other):
        if isinstance(other, AddrDisp):
            return AddrDisp(self._b - other._b)
        return AddrDisp(self._b - int(other))
    def __neg__(self):
        return AddrDisp(-self._b)
    def __abs__(self):
        return AddrDisp(abs(self._b))
    def __mul__(self, other):
        return AddrDisp(self._b * int(other))
    def __rmul__(self, other):
        return AddrDisp(int(other) * self._b)

    # Comparison
    def __eq__(self, other):
        if other is None: return False
        return self._b == (other._b if isinstance(other, AddrDisp) else other)
    def __ne__(self, other):
        return not self.__eq__(other)
    def __lt__(self, other):
        return self._b < (other._b if isinstance(other, AddrDisp) else other)
    def __le__(self, other):
        return self._b <= (other._b if isinstance(other, AddrDisp) else other)
    def __gt__(self, other):
        return self._b > (other._b if isinstance(other, AddrDisp) else other)
    def __ge__(self, other):
        return self._b >= (other._b if isinstance(other, AddrDisp) else other)

    def __hash__(self):
        return hash(self._b)
    def __int__(self):
        return self._b
    def __index__(self):
        return self._b
    def __bool__(self):
        return self._b != 0
    def __repr__(self):
        sign = '-' if self._b < 0 else '+'
        return f"AddrDisp({sign}0x{abs(self._b):X})"
    def __format__(self, spec):
        return format(self._b, spec)
