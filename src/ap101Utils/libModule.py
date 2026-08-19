#!/usr/bin/env python3
#
# AP-101 loadable-module ".lib" container: writer, reader, and dump tool.
#
# The real SDL flow stored linkage-editor output as OS/360 load modules in a
# library (MAFGEN's `LM=OFTMP` reads one); this module recreates that boundary
# for the con80build world.  The record VOCABULARY follows the IBM load-module
# format described in GC28-6538 (CESD, control/text, RLD, IDR, EOM); the byte
# layouts are our own (the byte-level PLM is not in our spec archive, and the
# AP-101 extensions below could not be byte-faithful anyway) but CESD entries
# reuse the documented 16-byte ESD item image from the object-module format.
#
# File framing (all integers big-endian, the AP-101 native order):
#
#   magic    8 bytes  b"AP101LIB"
#   version  u16      format version (VERSION below)
#   records: u8 type, u24 payload-length, payload ... terminated by EOM.
#
# Record types (IBM-derived codes where the concept matches):
#
#   0x80 IDR   - identification: module name, linker id string, creation info
#   0x20 CESD  - composite ESD: n 16-byte entries (objModule.EsdEntry image).
#                Entry addresses are ABSOLUTE BYTE addresses in GPC memory;
#                ids are 1-based positions in this record.  SD = placed
#                section, LD = entry label (ldid -> owning SD id), ER =
#                unresolved external carried for the record.
#   0x01 TEXT  - one contiguous extent: u24 start (byte addr), u24 length
#                (bytes), data.  A module has one TEXT per contiguous run of
#                placed sections (banks/overlays produce separate extents).
#   0x02 RLD   - relocation dictionary, retained from the input objects with
#                addresses rebased: n entries of u8 flags, u24 position
#                (absolute byte addr), u16 rel CESD id (0 = unresolved ER by
#                name only), u16 pos CESD id.
#   0x40 SYM   - reserved (not emitted yet); would carry SYM card payloads.
#   0x0D EOM   - end of module (empty payload).
#
# AP-101 extension records (0xA0 range -- NOT part of any IBM format):
#
#   0xA1 PROT  - per-halfword store-protect bitmap for one TEXT extent:
#                u24 start (byte addr), u24 count (halfwords), then
#                ceil(count/8) bytes, MSB-first (bit i = halfword start/2+i).
#                Granularity matches the hardware (each halfword has its own
#                store-protect bit; ISPB/SPON/SPOFF/SET/CLEAR semantics).
#   0xA2 REGN  - named layout regions (CON80 OVERLAY): n entries of
#                u24 start (byte addr), u24 end (byte addr, exclusive),
#                u8 bank, 8-byte EBCDIC name.
#   0xA3 PHAS  - phase metadata: u8 flags (bit0 = NOCALLER), u8 count of
#                phase numbers, count u8 phase numbers, 8-byte EBCDIC root
#                (CON80 member) name, u24 entry byte address (0xFFFFFF =
#                none), 8-byte EBCDIC entry symbol name (blank = none).
#
from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import codepages  # noqa: F401  (registers the ebcdicvagc codec)
from .objModule import EsdEntry, EsdType

MAGIC = b"AP101LIB"
VERSION = 1

R_TEXT = 0x01
R_RLD = 0x02
R_EOM = 0x0D
R_CESD = 0x20
R_SYM = 0x40
R_IDR = 0x80
R_PROT = 0xA1
R_REGN = 0xA2
R_PHAS = 0xA3
R_XRES = 0xA4          # cross-phase resolution marker: u32 patch count
R_LSTA = 0xA5          # linkage-editor carried state: UTF-8 JSON object

_NAMES = {R_TEXT: "TEXT", R_RLD: "RLD", R_EOM: "EOM", R_CESD: "CESD",
          R_SYM: "SYM", R_IDR: "IDR", R_PROT: "PROT", R_REGN: "REGN",
          R_PHAS: "PHAS", R_XRES: "XRES", R_LSTA: "LSTA"}

NO_ADDR = 0xFFFFFF


def _eb(name: str, width: int = 8) -> bytes:
  return name.ljust(width)[:width].encode("ebcdicvagc")


def _un_eb(b: bytes) -> str:
  return b.decode("ebcdicvagc").rstrip()


def _u24(v: int) -> bytes:
  return int(v).to_bytes(3, "big")


@dataclass
class Extent:
  """One contiguous run of loaded memory: absolute byte address + data,
  plus the per-halfword store-protect bitmap (parallel to the data;
  protect[i] covers halfword address (address//2)+i)."""
  address: int                       # absolute BYTE address
  data: bytes
  protect: 'list[bool] | None' = None   # len == len(data)//2 when present

  @property
  def hwLength(self) -> int:
    return len(self.data) // 2


@dataclass
class LibRld:
  flags: int
  position: int                      # absolute BYTE address of the fixup
  relId: int                         # CESD id of the target (0 = none)
  posId: int                         # CESD id of the containing SD


@dataclass
class Region:
  name: str
  start: int                         # absolute BYTE address
  end: int                           # absolute BYTE address, exclusive
  bank: int = 0


@dataclass
class LibModule:
  """One AP-101 loadable module (the content of one .lib file)."""
  name: str = ""
  linkerId: str = ""                 # IDR provenance string
  created: str = ""                  # ISO timestamp text (informational)
  cesd: 'list[EsdEntry]' = field(default_factory=list)   # ids = index+1
  extents: 'list[Extent]' = field(default_factory=list)
  rlds: 'list[LibRld]' = field(default_factory=list)
  regions: 'list[Region]' = field(default_factory=list)
  phaseRoot: str = ""                # CON80 root member this was built from
  phaseNumbers: 'list[int]' = field(default_factory=list)
  nocall: bool = False
  entryAddress: 'int | None' = None  # absolute BYTE address
  entryName: str = ""
  # Cross-phase resolution state (lnk101.phaseresolve): once resolved, the
  # extent addends are consumed -- re-link the phase to redo resolution.
  crossResolved: bool = False
  xresCount: int = 0
  # Linkage-editor carried state: the allocation state the ONE-JOB flight LE
  # would hold at the end of this phase's deck segment, serialized so the
  # next phase's link CONTINUES it instead of re-deriving layout from the
  # module's artifacts.  Cumulative over the phase's own MAP-carded parents.
  # Keys so far:
  #   zconPoolNext  byte addr where the Z1 ZCON pool cursor stands after
  #                 this phase's pool content + its closing 2-hw checksum
  #                 slot (0/absent = no pool state to carry)
  linkState: dict = field(default_factory=dict)

  # ---- convenience queries (the MAP/phasing consumer surface) ----

  def symbols(self):
    """Yield (name, type, absolute byte address, length) for SD/LD entries."""
    for e in self.cesd:
      if e.type in (EsdType.SD, EsdType.LD):
        yield e.name.rstrip(), e.type, e.address, (e.length or 0)

  def occupiedIntervals(self):
    """[(startByte, endByte)] of every TEXT extent (for occupancy)."""
    return [(x.address, x.address + len(x.data)) for x in self.extents]

  def cesdEntry(self, esdId: int) -> 'EsdEntry | None':
    return self.cesd[esdId - 1] if 1 <= esdId <= len(self.cesd) else None

  # ---- writing ----

  def _records(self):
    idr = _eb(self.name) + _eb(self.linkerId, 16) + _eb(self.created, 24)
    yield R_IDR, idr

    if self.cesd:
      yield R_CESD, b"".join(e.to_image() for e in self.cesd)

    for x in self.extents:
      yield R_TEXT, _u24(x.address) + _u24(len(x.data)) + bytes(x.data)
      if x.protect is not None:
        bits = bytearray((x.hwLength + 7) // 8)
        for i, p in enumerate(x.protect):
          if p:
            bits[i >> 3] |= 0x80 >> (i & 7)
        yield R_PROT, _u24(x.address) + _u24(x.hwLength) + bytes(bits)

    if self.rlds:
      yield R_RLD, b"".join(
        struct.pack(">B3sHH", r.flags, _u24(r.position), r.relId, r.posId)
        for r in self.rlds)

    if self.regions:
      yield R_REGN, b"".join(
        _u24(r.start) + _u24(r.end) + bytes([r.bank & 0xFF]) + _eb(r.name)
        for r in self.regions)

    phas = bytes([1 if self.nocall else 0, len(self.phaseNumbers)])
    phas += bytes(p & 0xFF for p in self.phaseNumbers)
    phas += _eb(self.phaseRoot)
    phas += _u24(self.entryAddress if self.entryAddress is not None
                 else NO_ADDR)
    phas += _eb(self.entryName)
    yield R_PHAS, phas

    if self.crossResolved:
      yield R_XRES, self.xresCount.to_bytes(4, "big")

    if self.linkState:
      import json
      yield R_LSTA, json.dumps(self.linkState, sort_keys=True).encode("utf-8")

    yield R_EOM, b""

  def write(self, path) -> None:
    with open(path, "wb") as f:
      f.write(MAGIC + VERSION.to_bytes(2, "big"))
      for rtype, payload in self._records():
        f.write(bytes([rtype]) + _u24(len(payload)) + payload)

  # ---- reading ----

  @classmethod
  def read(cls, path) -> "LibModule":
    blob = Path(path).read_bytes()
    if blob[:8] != MAGIC:
      raise ValueError(f"{path}: not an AP101LIB file")
    version = int.from_bytes(blob[8:10], "big")
    if version > VERSION:
      raise ValueError(f"{path}: unsupported .lib version {version}")
    mod = cls()
    protByAddr = {}
    pos = 10
    while pos + 4 <= len(blob):
      rtype = blob[pos]
      length = int.from_bytes(blob[pos + 1:pos + 4], "big")
      payload = blob[pos + 4:pos + 4 + length]
      pos += 4 + length
      if rtype == R_EOM:
        break
      mod._readRecord(rtype, payload, protByAddr, path)
    for x in mod.extents:
      bits = protByAddr.get(x.address)
      if bits is not None:
        x.protect = bits[:x.hwLength]
    return mod

  def _readRecord(self, rtype, payload, protByAddr, path):
    if rtype == R_IDR:
      self.name = _un_eb(payload[0:8])
      self.linkerId = _un_eb(payload[8:24])
      self.created = _un_eb(payload[24:48])
    elif rtype == R_CESD:
      for base in range(0, len(payload) - 15, 16):
        self.cesd.append(
          EsdEntry.from_image(payload[base:base + 16], len(self.cesd) + 1))
    elif rtype == R_TEXT:
      addr = int.from_bytes(payload[0:3], "big")
      length = int.from_bytes(payload[3:6], "big")
      self.extents.append(Extent(addr, payload[6:6 + length]))
    elif rtype == R_PROT:
      addr = int.from_bytes(payload[0:3], "big")
      count = int.from_bytes(payload[3:6], "big")
      bits = payload[6:]
      protByAddr[addr] = [
        bool(bits[i >> 3] & (0x80 >> (i & 7))) if (i >> 3) < len(bits)
        else False
        for i in range(count)]
    elif rtype == R_RLD:
      for base in range(0, len(payload) - 7, 8):
        flags = payload[base]
        position = int.from_bytes(payload[base + 1:base + 4], "big")
        relId, posId = struct.unpack(">HH", payload[base + 4:base + 8])
        self.rlds.append(LibRld(flags, position, relId, posId))
    elif rtype == R_REGN:
      for base in range(0, len(payload) - 14, 15):
        start = int.from_bytes(payload[base:base + 3], "big")
        end = int.from_bytes(payload[base + 3:base + 6], "big")
        bank = payload[base + 6]
        name = _un_eb(payload[base + 7:base + 15])
        self.regions.append(Region(name, start, end, bank))
    elif rtype == R_XRES:
      self.crossResolved = True
      self.xresCount = int.from_bytes(payload[0:4], "big")
    elif rtype == R_LSTA:
      import json
      self.linkState = json.loads(payload.decode("utf-8"))
    elif rtype == R_PHAS:
      self.nocall = bool(payload[0] & 1)
      n = payload[1]
      self.phaseNumbers = list(payload[2:2 + n])
      p = 2 + n
      self.phaseRoot = _un_eb(payload[p:p + 8])
      addr = int.from_bytes(payload[p + 8:p + 11], "big")
      self.entryAddress = None if addr == NO_ADDR else addr
      self.entryName = _un_eb(payload[p + 11:p + 19])
    # Unknown record types are skipped: forward compatibility for future
    # extensions (a reader must tolerate records it does not understand).

  # ---- dump ----

  def dump(self, out=sys.stdout, verbose=False):
    w = out.write
    w(f"Load module {self.name or '(unnamed)'}\n")
    if self.linkerId or self.created:
      w(f"  IDR: {self.linkerId}  {self.created}\n")
    if self.phaseRoot or self.phaseNumbers:
      nums = ",".join(str(p) for p in self.phaseNumbers)
      w(f"  PHAS: root={self.phaseRoot or '-'} phases=[{nums}]"
        f"{' NOCALLER' if self.nocall else ''}\n")
    if self.entryAddress is not None or self.entryName:
      ea = f"0x{self.entryAddress:06X}" if self.entryAddress is not None \
           else "-"
      w(f"  ENTRY: {self.entryName or '-'} @ {ea} (byte)\n")
    if self.crossResolved:
      w(f"  XRES: cross-phase resolved ({self.xresCount} sites patched)\n")
    if self.linkState:
      st = " ".join(f"{k}=0x{v:X}" if isinstance(v, int) else f"{k}={v}"
                    for k, v in sorted(self.linkState.items()))
      w(f"  LSTA: {st}\n")

    counts = {}
    for e in self.cesd:
      counts[e.typeName] = counts.get(e.typeName, 0) + 1
    w(f"  CESD: {len(self.cesd)} entries "
      f"({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))})\n")
    if verbose:
      for e in self.cesd:
        extra = f" len={e.length}" if e.type == EsdType.SD else \
                f" sd={e.ldid}" if e.type == EsdType.LD else ""
        addr = f"0x{e.address:06X}" if e.address is not None else "------"
        w(f"    {e.esdId:4d} {e.typeName:3s} {e.name:<8s} {addr}{extra}\n")

    totalProt = 0
    for x in self.extents:
      nprot = sum(x.protect) if x.protect else 0
      totalProt += nprot
      w(f"  TEXT: 0x{x.address:06X}..0x{x.address + len(x.data):06X} "
        f"({x.hwLength} hw)  protected {nprot}/{x.hwLength} hw\n")
    w(f"  RLD: {len(self.rlds)} entries\n")
    for r in self.regions:
      w(f"  REGN: {r.name:<8s} 0x{r.start:06X}..0x{r.end:06X} "
        f"bank {r.bank}\n")
    if self.extents:
      totalHw = sum(x.hwLength for x in self.extents)
      w(f"  total {totalHw} hw in {len(self.extents)} extent(s), "
        f"{totalProt} hw protected\n")


def regionIntervals(lib: 'LibModule', names, extend: bool = True):
    """BYTE intervals [(start, end), ...] of the named REGN records."""
    wanted = set(names)
    recs = sorted(lib.regions, key=lambda r: r.start)
    out = []
    for i, r in enumerate(recs):
        if r.name not in wanted:
            continue
        end = r.end
        if extend and i + 1 < len(recs):
            end = max(end, recs[i + 1].start)
        out.append((r.start, end))
    return out


def main(argv=None):
  args = list(sys.argv[1:] if argv is None else argv)
  verbose = "-v" in args
  args = [a for a in args if a != "-v"]
  if not args or args[0] in ("-h", "--help"):
    print("usage: python -m ap101Utils.libModule [-v] <file.lib> ...")
    return 0
  for path in args:
    LibModule.read(path).dump(verbose=verbose)
  return 0


if __name__ == "__main__":
  sys.exit(main())
