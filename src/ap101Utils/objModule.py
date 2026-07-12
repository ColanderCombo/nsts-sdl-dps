#!/usr/bin/env python3
#
# IBM AP-101S / System-360 object module codec.
#
# This is the single card<->record codec for AP-101 object files: it decodes a
# punched-card object deck into typed records and a structured per-module view
# (ESD / TXT / RLD / SYM / END).  It is the one place that understands the wire
# format; the assembler's writer and the linker's reader both build on it, and
# the linker's *semantic* model (assembled sections, link-time base addresses)
# is a separate layer on top of this decode.
#
# HAL-S/FC Object Modules
#   ref. USA-001556/p.143
#        HAL-S/FC SDL Interface Control Document sect.2.3 Object Code
#
# Generic IBM Object Modules
#   ref. IBM-C28-6538-3 - IBM System 360, Linkage Editor (1966-10)
#   ref. OS/360 object file format; "MVS Program Management: Advanced
#        Facilities" (iea2b270.pdf) Appendix A.
#

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar
import warnings

from .addrcon import AddrCon


CARD_SIZE = 80


# 
# Low-level byte helpers
#
# All multi-byte object-module fields are unsigned big-endian, and many are
# 24-bit (addresses, lengths) -- which struct cannot express -- so the integer
# codec is int.from_bytes/int.to_bytes throughout: _u16/_u24/_u32 below for the
# fixed-width reads, bytearrayToInteger for a slice of arbitrary width, and
# int.to_bytes(n, "big") inline on the encode side.
# 

def _u16(data, start: int) -> int:
  return int.from_bytes(data[start:start + 2], "big")


def _u24(data, start: int) -> int:
  return int.from_bytes(data[start:start + 3], "big")


def _u32(data, start: int) -> int:
  return int.from_bytes(data[start:start + 4], "big")


def bytearrayToInteger(data) -> int:
  """Big-endian integer from a byte sequence of any width."""
  return int.from_bytes(data, "big")


def isBytearrayBlank(data) -> bool:
  """True if every byte is an EBCDIC blank (0x40) or binary zero (0x00)."""
  return all(b in (0x40, 0x00) for b in data)


def bytearrayToAscii(data) -> str:
  """EBCDIC->ASCII for names/text.  Binary 0x00/0x01/0x02 (which occur in
  HAL/FC compiler-generated SYM names) map to placeholder a/b/c so decoded
  names stay byte-stable and printable."""
  return bytes(data).decode("ebcdicvagc").translate({0: "a", 1: "b", 2: "c"})


def asciiToEbcdicBytes(s: str, length: int) -> bytes:
  """ASCII->EBCDIC, fixed width, blank-padded (0x40).  Non-ASCII and missing
  positions become EBCDIC blanks.  The inverse of bytearrayToAscii for the
  printable range, used when encoding names/identifiers into cards."""
  return s[:length].encode("ebcdicvagc", "ebcdicblank").ljust(length, b"\x40")


def blankCard(cardType: str, seqNum: int) -> bytearray:
  """An 80-column card filled with EBCDIC blanks, framed with the 0x02 lead
  byte, the 3-character record type, and an 8-digit sequence number."""
  card = bytearray([0x40] * CARD_SIZE)
  card[0] = 0x02
  card[1:4] = asciiToEbcdicBytes(cardType, 3)
  card[72:80] = asciiToEbcdicBytes(f"{seqNum % 100000000:08d}", 8)
  return card


# ===========================================================================
# ESD - External Symbol Dictionary
#
# The external symbol dictionary (ESD) contains the external symbols that are
# defined or referred to in the module. An external symbol dictionary entry
# identifies a symbol and its position within the module. Each entry in the
# external symbol dictionary is classified as one of the following:

#   1. external name - is a name that can be referred to by any control
#   section or separately assembled or compiled module. It has a defined value
#   within the module.
#
#       a. control section name is the symbolic name of a control section.
#          The external symbol dictionary entry specifies the name, the
#          assembled origin, and the length of a control section. The defined
#          value of the symbol is the address of the first byte of the control
#          section.
#
#       b. entry name - is a name within a control section. The external symbol
#          dictionary entry specifies the assembled address of the name and
#          identifies the control section to which it belongs.
#
#       c. blank or named common area - is a control section used to reserve a
#          main storage area (containing no data or instructions) for control
#          sections provided by other modules. The reserved storage areas can
#          also be used as communication centers within a program. The external
#          symbol dictionary entry specifies the name and length of the common
#          area. If it is a blank common area, the name field contains blanks.
#
#       d. private code - is an unnamed control section. The external symbol
#          dictionary entry Specifies the assembled address and assigned length
#          of the control section.  The name field contains blanks.  Since it
#          has no name, it cannot be referred to by other control sections.
#
#   2. external reference - is a symbol that is defined as an external name
#      in another separately assembled or compiled module but is referred to
#      in the module being processed. The external symbol dictionary entry
#      specifies the name.
# ===========================================================================

class EsdType(IntEnum):
  # ESD type byte (column 9 of each 16-byte entry).  Each member carries its
  # listing display spelling, so the type byte, its name, and its printed form
  # live in one place.  These are the OS/360 type codes as defined by the
  # F-Assembler lineage (AS037F1 IEUFI: SDCON=0, LDCON=1, ERCON=2, PCCON=4,
  # CMCON=5, DXCON=6, VCCON=8, WXCONA=10) and matched by HAL/S-FC's ESD_CHAR
  # table — the codes are NON-contiguous: there is no 3, and WX is 0x0A.
  # AP-101 objects in practice only ever use SD/LD/ER; the rest are defined
  # for completeness and to decode foreign/older decks correctly.
  def __new__(cls, value, display):
    obj = int.__new__(cls, value)
    obj._value_ = value
    obj.display = display
    return obj

  SD = 0x00, "SD"          # control section definition (CSECT/START)
  LD = 0x01, "LD"          # label (entry) definition
  ER = 0x02, "ER"          # external reference
  PC = 0x04, "PC"          # private code (unnamed control section)
  CM = 0x05, "CM"          # common control section
  XD_PR = 0x06, "XD/PR"    # external dummy section / pseudo-register
  VC = 0x08, "VC"          # V-type adcon (punched as ER on the card)
  WX = 0x0A, "WX"          # weak external reference


# Note: the RLD flag byte is intentionally NOT modeled as an enum.  Its AP-101
# bit assignments encode ZCON relocation kinds (HAL/S-FC OBJECTGE.xpl
# ASSEMBLE_RLD_FLAGS) rather than the plain OS/360 type/length/sign fields; the
# authoritative interpreter is ap101Utils.addrcon.AddrCon, and RldEntry keeps the
# flag byte raw.  Module.from_assembly selects the byte from the relocation
# letter via AddrCon.for_type: YCON 0x00, ZCON 0x04, ordinary adcon 0x1C.

class SymRefType(IntEnum):
  # SYM organization byte, bits 6-4, for a *reference* (non-data) item.
  SPACE = 0b000
  CONTROL = 0b001
  DUMMY = 0b010
  COMMON = 0b011
  INSTRUCTION = 0b100
  CCW = 0b101
  RELOCATABLE = 0b110     # HAL/S-FC emits this for relocatable references


class SymDataType(IntEnum):
  # ref. OBJECTGE.xpl/MIT_SYM_DATA
  #
  def __new__(cls, value, letter, lenBytes):
    obj = int.__new__(cls, value)
    obj._value_ = value
    obj.letter = letter		# same as DC type
    obj.lenBytes = lenBytes # width of len field
    return obj

  CHAR        = 0x00, "C", 2
  HEX         = 0x04, "X", 1
  BINARY      = 0x08, "B", 1
  FIXED       = 0x10, "F", 1
  HALFWORD    = 0x14, "H", 1
  FLOAT_SHORT = 0x18, "E", 1
  FLOAT_LONG  = 0x1C, "D", 1
  ADCON       = 0x20, "AQ", 1	# XXX ?
  YCON        = 0x24, "Y", 1
  SCON        = 0x28, "S", 1
  VCON        = 0x2C, "V", 1
  PACKED      = 0x30, "P", 1
  ZONED       = 0x34, "z", 1 	# XXX ?
  EXT_FLOAT   = 0x38, "L", 1
  ZCON        = 0x84, "Z", 0


# 
# Decoded item dataclasses 
# 

@dataclass
class EsdEntry:
  """One External Symbol Dictionary entry, decoded from a 16-byte image."""
  esdId: int
  typeByte: int
  type: EsdType | int             # EsdType when recognized, else the raw byte
  name: str
  address: int | None = None      # SD/LD/PC/CM: defined address (bytes)
  length: int | None = None       # SD/PC/CM: control-section length (bytes)
  ldid: int | None = None         # LD: ESD ID of the owning control section
  alignment: int | None = None    # XD/PR only
  remote: bool = False            # ER/LD/WX remote-COMPOOL flag (CR11094)
  flagByte: int = 0               # raw column-12 byte (kept verbatim for round-trip)

  @property
  def typeName(self) -> str:
    return (self.type.display if isinstance(self.type, EsdType)
            else f"0x{self.typeByte:02X}")

  @classmethod
  def sd(cls, esdId: int, name: str, address: int, length: int) -> "EsdEntry":
    """A control-section (SD) entry."""
    return cls(esdId=esdId, typeByte=int(EsdType.SD), type=EsdType.SD,
               name=name, address=address, length=length)

  @classmethod
  def er(cls, esdId: int, name: str) -> "EsdEntry":
    """An external-reference (ER) entry."""
    return cls(esdId=esdId, typeByte=int(EsdType.ER), type=EsdType.ER,
               name=name)

  @classmethod
  def ld(cls, esdId: int, name: str, address: int, ldid: int) -> "EsdEntry":
    """A label/entry (LD) entry owned by the control section ldid."""
    return cls(esdId=esdId, typeByte=int(EsdType.LD), type=EsdType.LD,
               name=name, address=address, ldid=ldid)

  @classmethod
  def from_image(cls, item: bytes, esdId: int) -> "EsdEntry":
    """Decode one 16-byte ESD entry image (esdId is positional in the card)."""
    typeByte = item[8]
    try:
      esdType: EsdType | int = EsdType(typeByte)
    except ValueError:
      esdType = typeByte
    entry = cls(esdId=esdId, typeByte=typeByte, type=esdType,
                name=bytearrayToAscii(item[0:8]))

    if esdType not in (EsdType.ER, EsdType.WX, EsdType.XD_PR):
      entry.address = _u24(item, 9)

    # Column 12 is OS/360 ESD filler in this era (AMODE/RMODE are an MVS/XA
    # addition the AP-101/F-Assembler lineage never uses); keep it raw.  The
    # only meaning carried here is the remote-COMPOOL flag on ER/LD/WX.
    flagByte = item[12]
    entry.flagByte = flagByte
    if esdType == EsdType.XD_PR:
      entry.alignment = flagByte
    elif esdType in (EsdType.ER, EsdType.LD, EsdType.WX):
      if flagByte == 0x01:
        entry.remote = True             # CR11094: remote COMPOOL flag

    size = item[13:16]
    if esdType in (EsdType.SD, EsdType.PC, EsdType.CM):
      entry.length = bytearrayToInteger(size)
    elif esdType == EsdType.LD:
      entry.ldid = bytearrayToInteger(size[1:])
    return entry

  def to_image(self) -> bytes:
    """Encode this entry as its 16-byte ESD image (the inverse of
    from_image).  Field placement matches AP-101 / OS-360 ESD entries."""
    img = bytearray([0x40] * 16)
    img[0:8] = asciiToEbcdicBytes(self.name, 8)
    img[8] = self.typeByte
    # A negative section-relative offset (e.g. PCGEN's FIOIPR EQU *-FIOBUS1
    # virtual base, used for bus-indexed addressing) is stored as a 24-bit
    # two's-complement value in the 3-byte address field.
    img[9:12] = ((self.address or 0) & 0xFFFFFF).to_bytes(3, "big")
    if self.type == EsdType.LD:
      img[12] = 0x01 if self.remote else 0x40
      img[13] = 0x40
      img[14:16] = (self.ldid or 0).to_bytes(2, "big")
    elif self.type in (EsdType.ER, EsdType.WX):
      img[12] = 0x01 if self.remote else 0x40
      # columns 13-15 stay blank
    else:                                   # SD / PC / CM
      img[12] = self.flagByte
      img[13:16] = (self.length or 0).to_bytes(3, "big")
    return bytes(img)


@dataclass
class TextRecord:
  """TXT record payload: object code destined for a control section."""
  esdId: int
  address: int                    # relative byte address within the section
  data: bytes


def scatterText(records, size=None):
  """Assemble one section's contiguous byte image from its TXT records
  (scattered loads, possibly out of order).  With size (the ESD-declared
  section length) the image is exactly that long and a record overrunning
  it raises — never silently truncated or grown; with size=None the image
  grows to fit (discovery mode, for objects whose ESD lengths are absent
  or untrusted).  The caller filters records to one section."""
  buf = bytearray(size or 0)
  for tr in records:
    end = tr.address + len(tr.data)
    if size is not None and end > size:
      raise ValueError(
        "TXT record overruns its section: %04X+%d > declared %d"
        % (tr.address, len(tr.data), size))
    if end > len(buf):
      buf.extend(bytes(end - len(buf)))
    buf[tr.address:end] = tr.data
  return buf


@dataclass
class RldEntry:
  """One (continuation-expanded) RLD relocation entry.

  flags is kept raw.  The AP-101 flag byte encodes ZCON relocation kinds
  (see HAL/S-FC OBJECTGE.xpl ASSEMBLE_RLD_FLAGS) and does not follow the plain
  OS/360 type/length/sign decomposition, so it is NOT decomposed here — the
  authoritative interpreter is ap101Utils.addrcon.AddrCon, which both the linker
  and objdump use.  Only bit 0 (continuation: "next entry reuses this R/P
  pointer"), which drives the short-record expansion below, is exposed."""
  relId: int                      # R pointer: ESD ID of referenced symbol
  posId: int                      # P pointer: ESD ID of containing section
  flags: int
  address: int                    # byte offset within the P section

  @property
  def continuation(self) -> bool:
    return (self.flags & 1) != 0

  def to_bytes(self) -> bytes:
    """Encode this entry as its 8-byte full RLD record (R, P, flag, addr)."""
    return (self.relId.to_bytes(2, "big") + self.posId.to_bytes(2, "big")
            + bytes([self.flags & 0xFF]) + self.address.to_bytes(3, "big"))

  @classmethod
  def expand(cls, payloads, errors) -> list["RldEntry"]:
    """Expand a sequence of RLD-record payloads into entries, honoring
    continuation: a flag with bit 0 set means the next entry is a 4-byte
    short record that reuses the previous R/P pointers.  Continuation runs
    may span card boundaries, so this takes all RLD payloads of a module."""
    out = []
    prevRel = prevPos = 0
    prevCont = False
    for payload in payloads:
      j = 0
      n = len(payload)
      while j < n:
        if prevCont and out:
          if j + 4 > n:
            break
          flags = payload[j]
          addr = _u24(payload, j + 1)
          out.append(cls(prevRel, prevPos, flags, addr))
          prevCont = (flags & 1) != 0
          j += 4
          continue
        if j + 8 > n:
          break
        relId = _u16(payload, j)
        posId = _u16(payload, j + 2)
        flags = payload[j + 4]
        addr = _u24(payload, j + 5)
        out.append(cls(relId, posId, flags, addr))
        prevRel, prevPos = relId, posId
        prevCont = (flags & 1) != 0
        j += 8
    return out


@dataclass
class SymEntry:
  """One parsed packed SYM-record symbol (debug/relocation dictionary)."""
  isDataType: bool
  offsetInCSECT: int
  organization: int
  packedOffset: int
  symType: SymRefType | int | None = None     # reference items only
  name: str | None = None
  dataTypeByte: int | None = None             # data items only
  dataType: SymDataType | int | None = None
  length: int | None = None
  multiplicity: int | None = None
  scale: int | None = None
  cluster: bool = False

  @property
  def symbolType(self) -> str:
    """Convenience display name: 'DATA' or the reference-type name."""
    if self.isDataType:
      return "DATA"
    if isinstance(self.symType, SymRefType):
      return self.symType.name
    return f"0x{self.symType:X}" if self.symType is not None else "?"

  @property
  def dataLetter(self) -> str | None:
    """One-letter listing designator for a data item's type."""
    if self.dataTypeByte is None:
      return None
    if isinstance(self.dataType, SymDataType):
      return self.dataType.letter
    return f"0x{self.dataTypeByte:02X}"

  @classmethod
  def parse_stream(cls, packed: bytes, errors: list) -> list["SymEntry"]:
    """Parse the concatenated payload of all SYM cards into SymEntry items.

    Field consumption (and therefore the offset of each subsequent symbol)
    follows the historical readObject101S logic exactly, so the parse stays
    in lock-step with real HAL/S-FC decks."""
    out = []
    total = len(packed)
    offset = 0
    idx = 0
    while offset < total:
      start = offset
      org = packed[offset]
      offset += 1
      if offset + 3 > total:
        errors.append(f"Error: SYM overrun reading CSECT offset for symbol #{idx}")
        break
      offsetInCSECT = _u24(packed, offset)
      offset += 3

      isData = (org & 0b10000000) != 0
      multiplicity = isData and (org & 0b01000000) != 0
      cluster = isData and (org & 0b00100000) != 0
      scaling = isData and (org & 0b00010000) != 0

      sym = cls(isDataType=isData, offsetInCSECT=offsetInCSECT,
                organization=org, packedOffset=start, cluster=cluster)

      if not isData:
        raw = (org >> 4) & 0b111
        try:
          sym.symType = SymRefType(raw)
        except ValueError:
          errors.append(f"Error: Unknown SYM type 0x{raw:X} (org=0x{org:02X}) at symbol #{idx}")
          break

      hasName = (org & 0b00001000) == 0
      nameLen = 1 + (org & 0b111)
      if hasName:
        if offset + nameLen > total:
          errors.append(f"Error: SYM overrun reading name for symbol #{idx}")
          break
        sym.name = bytearrayToAscii(packed[offset:offset + nameLen])
        offset += nameLen

      if not isData:
        out.append(sym)
        idx += 1
        continue

      if offset >= total:
        errors.append(f"Error: SYM overrun reading datatype for symbol #{idx}")
        break
      dataByte = packed[offset]
      offset += 1
      sym.dataTypeByte = dataByte
      try:
        dt = SymDataType(dataByte)
      except ValueError:
        dt = None
      sym.dataType = dt if dt is not None else dataByte

      # A known type's lenBytes is the width of its trailing length field
      # (2 for C, 0 for the Z-CON, 1 for the rest); an unknown type
      # carries no length field (and may desync the rest of the stream).
      lenWidth = dt.lenBytes if dt is not None else 0
      if lenWidth and offset + lenWidth > total:
        errors.append(f"Error: SYM overrun reading length for symbol #{idx}")
        break
      if lenWidth == 2:
        sym.length = _u16(packed, offset) + 1
        offset += 2
      elif lenWidth == 1:
        sym.length = packed[offset] + 1
        offset += 1

      if multiplicity:
        if offset + 3 > total:
          errors.append(f"Error: SYM overrun reading multiplicity for symbol #{idx}")
          break
        sym.multiplicity = _u24(packed, offset)
        offset += 3
      if scaling:
        if offset + 2 > total:
          errors.append(f"Error: SYM overrun reading scale for symbol #{idx}")
          break
        sym.scale = _u16(packed, offset)
        offset += 2

      out.append(sym)
      idx += 1
    return out


@dataclass
class EndInfo:
  """Decoded END record."""
  entryAddress: int | None = None
  esdId: int | None = None
  length: int | None = None
  entryName: str | None = None
  idrType: str | None = None      # '1', '2', or ' '
  translator: str | None = None
  processor: str | None = None


# 
# Card / record layer
# 

@dataclass
class Record:
  """Base class for one 80-column object card (image[0] == 0x02)."""
  CARD_TYPE: ClassVar[str | None] = None
  image: bytes

  @classmethod
  def from_image(cls, image: bytes) -> "Record":
    record = Record(image=image)
    if not record.valid:
      return record
    for subtype in Record.__subclasses__():
      if subtype.CARD_TYPE == record.cardType:
        return subtype(image=image)
    return record

  @property
  def valid(self) -> bool:
    return len(self.image) > 0 and self.image[0] == 0x02

  @property
  def cardType(self) -> str:
    if len(self.image) < 4:
      return ""
    return bytes(self.image[1:4]).decode("ebcdicvagc")

  @property
  def numBytesData(self) -> int:
    """Payload byte count (columns 11-12)."""
    return _u16(self.image, 10)

  @property
  def raw(self) -> array:
    return array("B", self.image)

  @property
  def title(self) -> str:
    if len(self.image) < 80:
      return ""
    return bytes(self.image[72:80]).decode("ebcdicvagc")

  @property
  def sequence(self) -> str:
    return self.title

  def __str__(self) -> str:
    payload = self.image if not self.valid else self.image[4:72]
    hex_data = "".join(f"{b:02X}" for b in payload)
    separator = " " if self.valid else "x"
    return f"{self.cardType}:{separator}{hex_data} {self.title}"


class ControlRecord(Record):
  """A free-format linker control-statement card (column 1 is not 0x02) that
  can be interleaved with the object cards in a deck.  This is NOT an object-
  module record (object modules proper are ESD/TXT/RLD/SYM/END), nor a GOFF
  header.  HAL/S-FC's PASS2 emits one for each callable PROGRAM: a card of the
  form " STACK <csect>" written straight to the object stream (OBJECTGE.xpl,
  S1 = ' STACK ' || ESD_TABLE(...)), followed by a small entry-stub module.
  The linker reads stack sizes from the SYM STACKEND symbol, so these cards
  are carried for fidelity but contribute nothing to the decoded module."""
  @property
  def valid(self) -> bool:
    return False

  @property
  def cardType(self) -> str:
    return "CTL"

  @property
  def text(self) -> str:
    return bytearrayToAscii(self.image)

  def __str__(self) -> str:
    return f"CTL: {self.text.rstrip()}"


class EsdRecord(Record):
  CARD_TYPE = "ESD"
  MAX_ENTRIES = 3                  # 16-byte entries that fit in one card

  @classmethod
  def encode(cls, entries: list, seqNum: int) -> bytes:
    """Build one ESD card from up to MAX_ENTRIES consecutive entries."""
    card = blankCard(cls.CARD_TYPE, seqNum)
    card[10:12] = (len(entries) * 16).to_bytes(2, "big")
    card[14:16] = entries[0].esdId.to_bytes(2, "big")
    for i, e in enumerate(entries):
      card[16 + i * 16:16 + i * 16 + 16] = e.to_image()
    return bytes(card)

  @property
  def firstEsdId(self) -> int:
    """Binary ESD ID of the first entry on this card (columns 15-16)."""
    return _u16(self.image, 14)

  @property
  def esdEntries(self) -> list[EsdEntry]:
    size = min(self.numBytesData, 48)
    region = self.image[16:72]
    out = []
    for n, base in enumerate(range(0, size, 16)):
      if base + 16 > len(region):
        break
      out.append(EsdEntry.from_image(region[base:base + 16],
                                     self.firstEsdId + n))
    return out

  def __str__(self) -> str:
    lines = [super().__str__()]
    for e in self.esdEntries:
      lines.append(f"{' ' * 16}{e}")
    return "\n".join(lines)


class TxtRecord(Record):
  CARD_TYPE = "TXT"
  MAX_BYTES = 56                   # object-code bytes that fit in one card

  @classmethod
  def encode(cls, esdId: int, address: int, data: bytes, seqNum: int) -> bytes:
    """Build one TXT card carrying up to MAX_BYTES of object code."""
    card = blankCard(cls.CARD_TYPE, seqNum)
    card[5:8] = address.to_bytes(3, "big")
    card[10:12] = len(data).to_bytes(2, "big")
    card[14:16] = esdId.to_bytes(2, "big")
    card[16:16 + len(data)] = data
    return bytes(card)

  @property
  def textRecord(self) -> TextRecord:
    size = min(self.numBytesData, 56)
    return TextRecord(esdId=_u16(self.image, 14),
                      address=_u24(self.image, 5),
                      data=bytes(self.image[16:16 + size]))

  def __str__(self) -> str:
    tr = self.textRecord
    return (f"{super().__str__()}\n{' ' * 16}"
            f"addr={tr.address:06X} esdId={tr.esdId} len={len(tr.data)}")


class RldRecord(Record):
  CARD_TYPE = "RLD"
  MAX_ENTRIES = 7                  # 8-byte full records that fit in one card

  @classmethod
  def encode(cls, entries: list, seqNum: int) -> bytes:
    """Build one RLD card from up to MAX_ENTRIES full (8-byte) records."""
    card = blankCard(cls.CARD_TYPE, seqNum)
    card[10:12] = (len(entries) * 8).to_bytes(2, "big")
    for i, e in enumerate(entries):
      card[16 + i * 8:16 + i * 8 + 8] = e.to_bytes()
    return bytes(card)

  @property
  def payload(self) -> bytes:
    size = min(self.numBytesData, 56)
    return bytes(self.image[16:16 + size])

  def __str__(self) -> str:
    return f"{super().__str__()}\n{' ' * 16}rld {len(self.payload)} byte(s)"


class SymRecord(Record):
  CARD_TYPE = "SYM"

  @property
  def payload(self) -> bytes:
    size = min(self.numBytesData, 56)
    return bytes(self.image[16:16 + size])


class EndRecord(Record):
  CARD_TYPE = "END"

  @classmethod
  def encode(cls, end: "EndInfo", ident: str, seqNum: int) -> bytes:
    """Build the END card.  The optional assembler IDR string is placed at
    columns 37-55"""
    card = blankCard(cls.CARD_TYPE, seqNum)
    if end.entryAddress is not None:
      card[5:8] = end.entryAddress.to_bytes(3, "big")
    if end.esdId is not None:
      card[14:16] = end.esdId.to_bytes(2, "big")
    if ident:
      card[37:56] = asciiToEbcdicBytes(ident, 19)
    return bytes(card)

  @property
  def endInfo(self) -> EndInfo:
    d = self.image
    info = EndInfo()
    if not isBytearrayBlank(d[5:8]):
      info.entryAddress = _u24(d, 5)
    if not isBytearrayBlank(d[14:16]):
      info.esdId = _u16(d, 14)
    if d[28] == 0x00:
      info.length = _u32(d, 28)
    flagField = d[32]
    if flagField in (0xF1, 0xF2):       # IDR type 1 or 2
      if flagField == 0xF1:
        info.entryAddress = _u24(d, 5)
        info.esdId = _u16(d, 14)
      elif not isBytearrayBlank(d[16:24]):
        info.entryName = bytearrayToAscii(d[16:24])
      info.idrType = bytes((flagField,)).decode("ebcdicvagc")
      info.translator = bytearrayToAscii(d[33:52])
      info.processor = bytearrayToAscii(d[52:71])
    elif flagField == 0x40:
      info.idrType = " "          # no IDR data
    else:
      info.idrType = "?"
    return info

  def __str__(self) -> str:
    return f"{super().__str__()}\n{' ' * 16}{self.endInfo}"


#
# Module
#
@dataclass
class Module:
  name: str = ""
  records: list = field(default_factory=list)
  esdEntries: list = field(default_factory=list)      # EsdEntry, in deck order
  textRecords: list = field(default_factory=list)     # TextRecord
  relocations: list = field(default_factory=list)     # RldEntry (expanded)
  symbols: list = field(default_factory=list)         # SymEntry
  end: EndInfo | None = None
  errors: list = field(default_factory=list)

  def section(self, esdId: int):
    for e in self.esdEntries:
      if e.esdId == esdId:
        return e
    return None

  @classmethod
  def from_records(cls, name: str, records: list) -> "Module":
    """Create a module from card records.  RLD and SYM payloads are 
           gathered across all their cards before decoding, since
       continuation runs and packed symbols can span card boundaries."""
    mod = cls(name=name, records=records)
    rld_payloads = []
    sym_payload = bytearray()
    for rec in records:
      if isinstance(rec, EsdRecord):
        mod.esdEntries.extend(rec.esdEntries)
      elif isinstance(rec, TxtRecord):
        mod.textRecords.append(rec.textRecord)
      elif isinstance(rec, RldRecord):
        rld_payloads.append(rec.payload)
      elif isinstance(rec, SymRecord):
        sym_payload += rec.payload
      elif isinstance(rec, EndRecord):
        mod.end = rec.endInfo
    mod.relocations = RldEntry.expand(rld_payloads, mod.errors)
    if sym_payload:
      mod.symbols = SymEntry.parse_stream(bytes(sym_payload), mod.errors)
    return mod

  @classmethod
  def from_assembly(cls, sections, externs, entries, end=None,
                    relocations=()) -> "Module":
    """Build a writable Module from an assembler's resolved output.

    This assigns ESD IDs, orders the ESD, resolves and sorts the RLD, 
      and lays out TXT/END.  All address and length values are BYTES 
      (the object module's native unit); a caller working in another 
       unit (e.g. halfwords) converts first.

      sections:    [(name, esdSeq, origin, image)] control sections in
                   definition order.  The SD length is len(image); a TXT
                   record is emitted only for a non-empty image (which must
                   be the live slice, not the whole buffer).
      externs:     [(name, esdSeq)] external references.  esdSeq orders them
                   against the sections (None sorts as 0).
      entries:     [(name, address, section)] entry points (LD), in source
                   order; address includes its section's origin.
      end:         (address, section) for the END entry point, or None.
      relocations: [(symbol, section, type, address)] RLD sites; type is
                   the AP-101S relocation letter ('Y'/'A'/'Z'), mapped to a
                   flag byte by AddrCon.  Sites whose symbol or section is
                   not in the ESD are dropped.

    The ESD is emitted in AP-101 SOURCE-APPEARANCE order with SD (control
    sections) and ER (external references) INTERLEAVED by esdSeq -- an EXTRN
    referenced before a later CSECT gets the lower ESD ID.  LD definitions 
      are NOT interleaved: they take trailing IDs and only reference their 
      owning section.
    """
    esdItems, esdIdMap, nextId = [], {}, 1

    appearance = [(esdSeq, 'SD', name, origin, image)
                  for name, esdSeq, origin, image in sections]
    appearance += [(esdSeq if esdSeq is not None else 0, 'ER', name, None, None)
                   for name, esdSeq in externs]
    appearance.sort(key=lambda a: a[0])

    for esdSeq, kind, name, origin, image in appearance:
      esdIdMap[name] = nextId
      if kind == 'SD':
        esdItems.append(EsdEntry.sd(nextId, name or '#MAIN',
                                    origin, len(image)))
      else:
        esdItems.append(EsdEntry.er(nextId, name))
      nextId += 1

    # Entry points (LD) last, in source order, each carrying its section's
    # ID; an entry whose section is unknown defaults to the first SD.
    for name, address, section in entries:
      esdIdMap[name] = nextId
      esdItems.append(EsdEntry.ld(nextId, name, address,
                                  esdIdMap.get(section, 1)))
      nextId += 1

    # RLD entries, dropping any whose symbol or section is unresolved, then
    # emitted in IBM OS/360 Assembler-F order: that assembler Shell-sorts the
    # pending-relocation table before punching, keyed (in priority) on
    # position-ESDID, then relocation-ESDID, then address, then flag (IEUFPP
    # ESORT over [posId, relId, addr, flag]).  Deterministic and Assembler-F-true.
    rldEntries = [
      RldEntry(relId=esdIdMap[symbol], posId=esdIdMap[section],
               flags=AddrCon.for_type(type).flags, address=address)
      for symbol, section, type, address in relocations
      if symbol in esdIdMap and section in esdIdMap
    ]
    rldEntries.sort(key=lambda e: (e.posId, e.relId, e.address, e.flags))

    # One TXT record per non-empty control section, in definition order; the
    # section's whole image is chunked into cards by to_bytes.
    textRecords = [
      TextRecord(esdId=esdIdMap.get(name, 1), address=0, data=image)
      for name, esdSeq, origin, image in sections
      if image
    ]

    endInfo = EndInfo()
    if end is not None:
      address, section = end
      endInfo = EndInfo(entryAddress=address,
                        esdId=esdIdMap.get(section, 1))

    return cls(esdEntries=esdItems, textRecords=textRecords,
               relocations=rldEntries, end=endInfo)

  def to_bytes(self, ident: str = "", start_seq: int = 1) -> bytes:
    """Serialize this module to a card deck in ESD, TXT, RLD, END order,
    chunking by each record type's per-card capacity and numbering cards
    sequentially from start_seq.  The inverse of from_records.

    NOTE: SYM cards are not emitted -- this writes the ESD/TXT/RLD/END
    records only."""
    if self.symbols:
      warnings.warn(
        f"Module {self.name!r} has {len(self.symbols)} SYM symbol(s) that "
        "to_bytes does not serialize; they will be omitted.",
        RuntimeWarning)
    out = bytearray()
    seq = start_seq
    for i in range(0, len(self.esdEntries), EsdRecord.MAX_ENTRIES):
      out += EsdRecord.encode(self.esdEntries[i:i + EsdRecord.MAX_ENTRIES], seq)
      seq += 1
    for tr in self.textRecords:
      step = TxtRecord.MAX_BYTES
      for off in range(0, len(tr.data), step):
        out += TxtRecord.encode(tr.esdId, tr.address + off,
                                tr.data[off:off + step], seq)
        seq += 1
    for i in range(0, len(self.relocations), RldRecord.MAX_ENTRIES):
      out += RldRecord.encode(self.relocations[i:i + RldRecord.MAX_ENTRIES], seq)
      seq += 1
    out += EndRecord.encode(self.end or EndInfo(), ident, seq)
    return bytes(out)


# 
# ObjectFile: the deck of cards
# 

class ObjectFile:
  """An object deck.  records is every 80-column card in order (object cards
  plus any free-format control statements); modules groups the object cards
  into one or more decoded Modules; controlStatements holds the control
  cards (e.g. HAL/S-FC's " STACK <csect>"), which are not part of any module."""

  def __init__(self, source, *, name: str | None = None):
    if isinstance(source, (str, Path)):
      self.pathname = Path(source)
      blob = self.pathname.read_bytes()
      self.name = name or self.pathname.stem
    else:
      self.pathname = None
      blob = bytes(source)
      self.name = name or "<bytes>"
    self.records = self._split(blob)
    self.controlStatements = []
    self.modules = self._group(self.records)

  @staticmethod
  def _split(blob: bytes) -> list:
    """Split a deck into 80-column cards.  A card whose column 1 is 0x02 is an
    object-module card (ESD/TXT/RLD/SYM/END); any other card is a free-format
    control statement (ControlRecord)."""
    records = []
    pos = 0
    n = len(blob)
    while pos < n:
      card = blob[pos:pos + CARD_SIZE]
      if len(card) < CARD_SIZE:
        warnings.warn(
          f"Ignoring trailing partial card: {len(card)} byte(s)",
          RuntimeWarning)
        break
      records.append(Record.from_image(card) if card[0] == 0x02
                     else ControlRecord(image=card))
      pos += CARD_SIZE
    return records

  def _group(self, records: list) -> list:
    """Group object cards into modules: an ESD..END span is one module, and a
    new module starts at the first object card after an END.  Free-format
    control statements belong to no module; they are collected into
    controlStatements instead."""
    modules = []
    current = []
    moduleNum = 0

    def flush():
      nonlocal current
      if not current:
        return
      nm = self.name if moduleNum <= 1 else f"{self.name}#{moduleNum}"
      modules.append(Module.from_records(nm, current))
      current = []

    for rec in records:
      if isinstance(rec, ControlRecord):
        self.controlStatements.append(rec)
        continue
      if not current:
        moduleNum += 1
      current.append(rec)
      if isinstance(rec, EndRecord):
        flush()
    flush()
    return modules

  def __len__(self) -> int:
    return len(self.records)

  def __iter__(self):
    return iter(self.records)

  def __getitem__(self, index):
    return self.records[index]

def read(source) -> ObjectFile:
  return ObjectFile(source)


# 
# Stand-alone dump
# 

if __name__ == "__main__":
  import os
  from typing import Annotated

  os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
  import typer

  app = typer.Typer(
    help="Dump an AP-101 object file record by record",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
  )

  @app.command()
  def _dump(object_file: Annotated[str, typer.Argument(
      help="AP-101 object file (.obj)")]) -> None:
    objfile = ObjectFile(object_file)
    for index, record in enumerate(objfile):
      print(f"{index:>4d} {record}")
    for m in objfile.modules:
      if m.symbols:
        print("-" * 72)
        print(f"SYM summary for module {m.name}:")
        for s in m.symbols:
          print(f"\t{s.symbolType:<11} offset={s.offsetInCSECT:06X} "
                f"name={s.name!r}"
                + (f" type={s.dataLetter}" if s.isDataType else ""))

  app()
