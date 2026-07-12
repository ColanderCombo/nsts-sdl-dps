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
from __future__ import annotations
import bisect


class Addr:
    """An absolute byte address, convertible to halfwords."""
    __slots__ = ('_b',)

    def __init__(self, b: int = 0):
        self._b = int(b)

    @classmethod
    def from_hw(cls, hw: int) -> Addr:
        return cls(int(hw) << 1)

    @property
    def bytes(self) -> int:
        return self._b

    @property
    def hw(self) -> int:
        return self._b >> 1

    @property
    def sector(self) -> int:
        """AP-101 sector number (0-15)."""
        return self._b >> 16

    def sector_encode(self) -> int:
        """Encode halfword for 16-bit AP-101: 0x8000 | (hw & 0x7FFF) if sector > 0."""
        hw = self.hw
        return 0x8000 | (hw & 0x7FFF) if hw >= 0x8000 else hw

    def align(self, boundary: int) -> Addr:
        """Round up to next multiple of boundary (bytes)."""
        r = self._b % boundary
        return Addr(self._b + (boundary - r)) if r else Addr(self._b)

    def end(self, length: int | AddrDisp) -> Addr:
        return Addr(self._b + int(length))

    # Arithmetic
    def __add__(self, other: Addr | AddrDisp | int) -> Addr:
        if isinstance(other, (AddrDisp, Addr)):
            return Addr(self._b + other._b)
        return Addr(self._b + int(other))
    def __radd__(self, other: int) -> Addr:
        return Addr(int(other) + self._b)
    def __sub__(self, other: Addr | AddrDisp | int) -> Addr | AddrDisp:
        if isinstance(other, Addr):
            return AddrDisp(self._b - other._b)
        if isinstance(other, AddrDisp):
            return Addr(self._b - other._b)
        return Addr(self._b - int(other))
    def __rsub__(self, other: int) -> AddrDisp:
        return AddrDisp(int(other) - self._b)
    def __mul__(self, other: int) -> Addr:
        return Addr(self._b * int(other))
    def __rmul__(self, other: int) -> Addr:
        return Addr(int(other) * self._b)

    # Comparison (works with Addr, AddrDisp, int, and None)
    def __eq__(self, other: object) -> bool:
        if other is None: return False
        if isinstance(other, (Addr, AddrDisp)):
            return self._b == other._b
        return self._b == other
    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)
    def __lt__(self, other: Addr | AddrDisp | int) -> bool:
        if isinstance(other, (Addr, AddrDisp)):
            return self._b < other._b
        return self._b < other
    def __le__(self, other: Addr | AddrDisp | int) -> bool:
        if isinstance(other, (Addr, AddrDisp)):
            return self._b <= other._b
        return self._b <= other
    def __gt__(self, other: Addr | AddrDisp | int) -> bool:
        if isinstance(other, (Addr, AddrDisp)):
            return self._b > other._b
        return self._b > other
    def __ge__(self, other: Addr | AddrDisp | int) -> bool:
        if isinstance(other, (Addr, AddrDisp)):
            return self._b >= other._b
        return self._b >= other

    def __hash__(self) -> int:
        return hash(self._b)
    def __int__(self) -> int:
        return self._b
    def __index__(self) -> int:
        return self._b
    def __bool__(self) -> bool:
        return self._b != 0
    @property
    def x(self) -> str:
        """5-digit uppercase hex halfword string, e.g. '100EA'."""
        return f"{self._b >> 1:05X}"

    def __repr__(self) -> str:
        return f"Addr(0x{self._b:06X}/hw:{self._b >> 1:05X})"
    def __format__(self, spec: str) -> str:
        return format(self._b, spec)


class AddrDisp:
    """A signed displacement (distance) in bytes between two addresses."""
    __slots__ = ('_b',)

    def __init__(self, b: int = 0):
        self._b = int(b)

    @classmethod
    def from_hw(cls, hw: int) -> AddrDisp:
        return cls(int(hw) << 1)

    @property
    def bytes(self) -> int:
        return self._b

    @property
    def hw(self) -> int:
        return self._b >> 1

    # Arithmetic
    def __add__(self, other: Addr | AddrDisp | int) -> Addr | AddrDisp:
        if isinstance(other, Addr):
            return Addr(self._b + other._b)
        if isinstance(other, AddrDisp):
            return AddrDisp(self._b + other._b)
        return AddrDisp(self._b + int(other))
    def __radd__(self, other: Addr | int) -> Addr | AddrDisp:
        if isinstance(other, Addr):
            return Addr(other._b + self._b)
        return AddrDisp(int(other) + self._b)
    def __sub__(self, other: AddrDisp | int) -> AddrDisp:
        if isinstance(other, AddrDisp):
            return AddrDisp(self._b - other._b)
        return AddrDisp(self._b - int(other))
    def __neg__(self) -> AddrDisp:
        return AddrDisp(-self._b)
    def __abs__(self) -> AddrDisp:
        return AddrDisp(abs(self._b))
    def __mul__(self, other: int) -> AddrDisp:
        return AddrDisp(self._b * int(other))
    def __rmul__(self, other: int) -> AddrDisp:
        return AddrDisp(int(other) * self._b)

    # Comparison
    def __eq__(self, other: object) -> bool:
        if other is None: return False
        return self._b == (other._b if isinstance(other, AddrDisp) else other)
    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)
    def __lt__(self, other: AddrDisp | int) -> bool:
        return self._b < (other._b if isinstance(other, AddrDisp) else other)
    def __le__(self, other: AddrDisp | int) -> bool:
        return self._b <= (other._b if isinstance(other, AddrDisp) else other)
    def __gt__(self, other: AddrDisp | int) -> bool:
        return self._b > (other._b if isinstance(other, AddrDisp) else other)
    def __ge__(self, other: AddrDisp | int) -> bool:
        return self._b >= (other._b if isinstance(other, AddrDisp) else other)

    def __hash__(self) -> int:
        return hash(self._b)
    def __int__(self) -> int:
        return self._b
    def __index__(self) -> int:
        return self._b
    def __bool__(self) -> bool:
        return self._b != 0
    def __repr__(self) -> str:
        sign = '-' if self._b < 0 else '+'
        return f"AddrDisp({sign}0x{abs(self._b):X})"
    def __format__(self, spec: str) -> str:
        return format(self._b, spec)


#=============================================================================
# Address-to-section map
#=============================================================================

class AddressMap:
    def __init__(self) -> None:
        self._entries: list[tuple[int, int, str]] = []
        self._starts: list[int] = []
        self._dirty: bool = False

    def add(self, start_hw: int, end_hw: int, name: str) -> None:
        """Add a named region. end_hw is inclusive."""
        self._entries.append((start_hw, end_hw, name))
        self._dirty = True

    def _build(self) -> None:
        if self._dirty:
            self._entries.sort()
            self._starts = [s for s, _, _ in self._entries]
            self._dirty = False

    def lookup(self, hw: int) -> tuple[str, int] | None: # [name, offset_within_region]
        self._build()
        idx = bisect.bisect_right(self._starts, hw) - 1
        if idx >= 0:
            start, end, name = self._entries[idx]
            if hw <= end:
                return (name, hw - start)
        return None

    def format(self, hw: int) -> str | None:
        """format as 'NAME' or 'NAME+offset'"""
        hit = self.lookup(hw)
        if hit:
            name, off = hit
            return f"{name}+{off:X}" if off else name
        return None

    def add_csect_table(self, csect_table: dict) -> None:
        for name, entry in csect_table.items():
            if "start" not in entry:
                continue
            start: int = entry["start"]
            end: int = entry.get("end", start)
            self.add(start, end, name)
            for ld_name, ld_val in entry.get("contents", {}).items():
                off = ld_val.get("offset", ld_val) if isinstance(ld_val, dict) else ld_val
                self.add(start + off, end, ld_name)

    def add_global_symbols(self, globalSymbols: dict) -> None:
        for name, (section, module, byteAddr) in globalSymbols.items():
            if byteAddr is None:
                continue
            start = byteAddr.hw
            if section.type == 'LD' and section.ldId is not None:
                parent = module.sections.get(section.ldId)
                end = (parent.baseAddress.hw + parent.length.hw - 1) \
                      if parent and parent.baseAddress else start
            elif section.length:
                end = start + section.length.hw - 1
            else:
                end = start
            self.add(start, end, name)

    def add_sym_json(self, sym_data: dict) -> None:
        for s in sym_data.get("sections", []):
            size = s.get("size", 1)
            self.add(s["address"], s["address"] + size - 1, s["name"])
        for s in sym_data.get("symbols", []):
            self.add(s["address"], s["address"], s["name"])
