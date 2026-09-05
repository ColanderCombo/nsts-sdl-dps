#!/usr/bin/env python3
# Linker for AP-101 objects
#
# Ref:  http://bitsavers.informatik.uni-stuttgart.de/pdf/ibm/360/os/R01-08/C28-6538-3_Linkage_Editor_Oct66.pdf
#

program = "LNK101"

from .repro import version_string as _version_string
version = _version_string()

import sys
import os
import re
import json
import logging
from dataclasses import dataclass, field
from typing import NamedTuple
from pathlib import Path
from collections import defaultdict
from ap101Utils import objModule
from ap101Utils.addr import Addr, AddrDisp, AddressMap
from ap101Utils.addrcon import (
    AddrCon, ZCon, sector_decode,
    RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA, RLD_BSR_ONLY, RLD_DSR_ONLY,
)
from .linkconfig import LinkConfig
from ap101Utils.linkorder import LinkOrder


#=============================================================================
# Default library search paths (relative to top directory)
#=============================================================================
DEFAULT_LIB_PATHS = [
    "lib/runtime/RUN",
    "lib/runtime/ZCON",
]

def _find_top_dir():
    """Find the installation/build top directory.
    In a venv (build tree): sys.prefix is build/venv, top is build/venv/..
    In a direct install:    sys.prefix is <prefix>, top is <prefix>
    """
    venv_parent = Path(sys.prefix).parent
    if (venv_parent / "lib" / "runtime").is_dir():
        return venv_parent
    prefix = Path(sys.prefix)
    if (prefix / "lib" / "runtime").is_dir():
        return prefix
    return None

#=============================================================================
# AP-101 Memory Layout Constants (all in bytes)
#=============================================================================
SECTOR_SIZE = Addr(0x10000)    # 32768 halfwords per sector
PSA_END     = Addr(0x00280)    # PSA ends at halfword 0x140
ZCON_END    = Addr(0x01000)    # ZCON zone: first 2K halfwords of sector 0

# The mass memory build's staging-buffer fill (`INIT=C6C6` on every MMUDATn
# ALLOC card).  It is what the tape carries wherever the load module supplies
# no text -- see generateStackSections.
STACK_FILL_BYTE = 0xC6


#=============================================================================
# Logging
#=============================================================================
log = logging.getLogger("LNK101")

def error(msg, fatal=True):
    log.error(msg)
    if fatal:
        sys.exit(1)

#=============================================================================
# Object Module Classes
#=============================================================================

# Prefix -> memory zone for sector-based placement
_ZONE_BY_PREFIX = (
    ('#Q', 'ZCON'), 
    ('#Z', 'ZCON'),
    ('@',  'DATA'),                     # stack frames
    ('#D', 'DATA'), 
    ('#P', 'DATA'),                     # data / REMOTE compool
    ('#0', 'DATA'), 
    ('#E', 'DATA'),                     # external ref data
    ('#L', 'DATA'), 
    ('#X', 'DATA'),
    ('$',  'CODE'),                     # user program code
)

@dataclass
class Section:
    """Control Section (CSECT) or Label Definition (LD).
    All addresses and lengths are in BYTES.
    """
    name: str
    esdId: int
    type: str = 'SD'
    address: 'AddrDisp' = field(default_factory=AddrDisp)  # LD: byte offset within owning SD
    length: 'AddrDisp' = field(default_factory=AddrDisp)
    module: 'LoadModule | None' = None
    baseAddress: 'Addr | None' = None            # assigned during linking
    data: bytearray = field(default_factory=bytearray)
    ldId: int | None = None                      # LD only: ESD ID of owning SD
    # Store-protection for the csect's halfwords: True/False once decided
    # (CON80 SET/CLEAR block mark, or the class default at .lib emission);
    # None = undecided.  Per-halfword PROT refinement rides the module.
    protected: 'bool | None' = None
    # Name this csect was compiled/assembled under, when an LE
    # `CHANGE old(new)` card renamed it:
    origName: 'str | None' = None

    @property
    def zone(self):
        """Memory zone for sector-based placement: 'ZCON', 'DATA', or 'CODE'."""
        n = self.name.strip()
        for prefix, zone in _ZONE_BY_PREFIX:
            if n.startswith(prefix):
                return zone
        return 'CODE'


@dataclass
class External:
    """External Reference (ER or WX)."""
    name: str
    esdId: int
    weak: bool = False
    module: 'LoadModule | None' = None
    resolved: bool = False
    resolvedSection: 'Section | None' = None


@dataclass
class Relocation:
    """RLD relocation entry.  Address is a BYTE offset within the P section (posId).

    Flag byte layout (OBJECTGE.xpl):
      bit 7: sign (V)   bits 6-4: type   bits 3-2: LL   bit 1: direction (S)   bit 0: continuation (T)
    """
    relId: int            # R pointer — ESD ID of referenced symbol
    posId: int            # P pointer — ESD ID of containing section
    flags: int
    address: 'AddrDisp'   # BYTE offset within P section
    module: 'LoadModule | None' = None
    length: int = field(init=False)

    def __post_init__(self):
        self.length = ((self.flags >> 2) & 0x03) + 1


@dataclass
class LoadModule:
    filename: str
    name: str = ''
    sections: 'dict[int, Section]' = field(default_factory=dict)        # esdId -> SD Section
    sectionsByName: 'dict[str, Section]' = field(default_factory=dict)  # name -> Section (SD + LD)
    lds: 'list[Section]' = field(default_factory=list)                  # LD sections, ESD order
    ldsByEsdId: 'dict[int, Section]' = field(default_factory=dict)      # unambiguous LD ids only
    _ldIdDups: set = field(default_factory=set)
    externals: 'dict[int, External]' = field(default_factory=dict)      # esdId -> External
    externalsByName: 'dict[str, External]' = field(default_factory=dict)  # name -> External
    relocations: 'list[Relocation]' = field(default_factory=list)
    entryPoint: 'tuple[int, AddrDisp] | None' = None       # (esdId, byteOffset)
    entryName: str | None = None
    stackSizes: dict[str, int] = field(default_factory=dict)  # csectName -> frame BYTES (SYM STACKEND)
    # Source-level store-protect info from ' PROT' control cards (asm101's
    # SPON/SPOFF capture).  A csect listed in protManaged is FULLY specified
    # by its ranges (an empty range list = nothing protected); csects not
    # listed fall back to the CON80 SET/CLEAR block mark / class default.
    protManaged: set = field(default_factory=set)
    protRangesHw: dict = field(default_factory=dict)  # csect -> [(sHw, eHw)]
    external: bool = False                                 # True = address-only, not in image

    def __post_init__(self):
        if not self.name:
            self.name = Path(self.filename).stem

    def addSection(self, section: 'Section') -> None:
        # LD entries do NOT share the SD/ER id space: asm101-emitted objects
        # give each LD a real, unique, RLD-referenceable id, but halsfc-emitted
        # LD cards reuse ids positionally (a 49-LD compool parses as ids
        # 3/4/5/3/4/5/...), so keying LDs into `sections` clobbered both other
        # LDs and, potentially, real SDs.  LDs live in their own list; the
        # id lookup keeps only ids that stay unambiguous among LDs.
        if section.type == 'LD':
            self.lds.append(section)
            if section.esdId in self.ldsByEsdId:
                del self.ldsByEsdId[section.esdId]
                self._ldIdDups.add(section.esdId)
            elif section.esdId not in self._ldIdDups:
                self.ldsByEsdId[section.esdId] = section
        else:
            self.sections[section.esdId] = section
        self.sectionsByName[section.name] = section

    def allSections(self):
        """All SD and LD sections: SDs (id order) then LDs (ESD order)."""
        yield from self.sections.values()
        yield from self.lds

    def nextEsdId(self) -> int:
        """Next unused ESD id across sections, LDs, and externals."""
        used = [s.esdId for s in self.allSections()] + list(self.externals)
        return max(used, default=0) + 1

    def addExternal(self, ext: 'External') -> None:
        self.externals[ext.esdId] = ext
        self.externalsByName[ext.name] = ext

    def synthesizeMissingExternals(self):
        # Synthesize ER entries for RLD targets absent from the ESD table.
        # This never fires on current compiler/assembler output; it is a
        # loud safety net — an RLD pointing at a missing ESD id means a
        # malformed object.
        for reloc in self.relocations:
            if reloc.relId in self.sections or reloc.relId in self.externals \
                    or reloc.relId in self.ldsByEsdId:
                continue
            posName = None
            if reloc.posId in self.sections:
                posName = self.sections[reloc.posId].name
            if posName and posName.startswith('$'):
                inferredName = f'@{posName[1:]}'
            else:
                inferredName = f'@ESD{reloc.relId}'

            self.addExternal(External(inferredName, reloc.relId, False, self))
            log.warning(
                f"RLD references ESD#{reloc.relId} absent from the ESD "
                f"table (malformed object?); synthesized ER "
                f"'{inferredName}' (inferred from posId={reloc.posId})")

    @classmethod
    def load(cls, filename, quiet=False):
        """Parse an object file into LoadModule(s).
        Returns (modules, errors, warnings).
        """
        objfile = objModule.ObjectFile(filename)

        errors, warnings = [], []
        for cmod in objfile.modules:
            for err in cmod.errors:
                if "Error" in err:
                    errors.append(f"{filename}: {err}")
                elif not quiet:
                    warnings.append(f"{filename}: {err}")

        modules = []
        for cmod in objfile.modules:
            module = cls(filename, name=cmod.name)

            for e in cmod.esdEntries:
                name = e.name.strip()
                if e.type == objModule.EsdType.SD:
                    length = AddrDisp(e.length or 0)
                    module.addSection(Section(name, e.esdId, 'SD',
                                              AddrDisp(e.address or 0), length,
                                              module, data=bytearray(length)))
                elif e.type == objModule.EsdType.LD:
                    module.addSection(Section(name, e.esdId, 'LD',
                                              AddrDisp(e.address or 0),
                                              module=module, ldId=e.ldid or 0))
                elif e.type in (objModule.EsdType.ER, objModule.EsdType.WX):
                    module.addExternal(
                        External(name, e.esdId, e.type == objModule.EsdType.WX, module))

            by_sect = {}
            for tr in cmod.textRecords:
                sect = module.sections.get(tr.esdId)
                if sect and sect.type == 'SD':
                    by_sect.setdefault(tr.esdId, []).append(tr)
            # The link editor fills object-text HOLES of HAL/S-FC
            # compiler csects with EBCDIC "FF" halfwords (C6C6); the
            # compiler emits no gap bytes, and uninitialized variables are
            # TXT-covered zeros.  Assembler csects carry their own C9FB
            # fill inside TXT, so only HAL-translated modules (END-card
            # IDR names the translator) get the fill, and only sections
            # that have text at all.
            halProduced = bool(cmod.end and
                               (cmod.end.translator or '').startswith('HAL/S'))
            for esdId, trs in by_sect.items():
                sect = module.sections[esdId]
                try:
                    sect.data = objModule.scatterText(trs, size=len(sect.data))
                except ValueError as e:
                    raise ValueError("%s %s: %s" % (filename, sect.name, e))
                if halProduced:
                    covered = bytearray(len(sect.data))
                    for tr in trs:
                        covered[tr.address:tr.address + len(tr.data)] = \
                            b'\x01' * len(tr.data)
                    for i, c in enumerate(covered):
                        if not c:
                            sect.data[i] = 0xC6

            for r in cmod.relocations:
                module.relocations.append(
                    Relocation(r.relId, r.posId, r.flags, AddrDisp(r.address), module))

            if (end := cmod.end) is not None:
                esdId = end.esdId or 0
                if end.entryAddress is not None:
                    module.entryPoint = (esdId, AddrDisp(end.entryAddress))
                elif esdId > 0 and end.idrType == '2':
                    module.entryPoint = (esdId, AddrDisp(0))
                if end.entryName:
                    module.entryName = end.entryName.strip()

            if module.sections or module.lds:
                module.synthesizeMissingExternals()
                modules.append(module)

        # ' PROT <csect> [<s>-<e>,...]' control cards: asm101's SPON/SPOFF
        # capture (halfword offsets, hex, end-exclusive).  Attach to the
        # module that owns the csect.
        for rec in objfile.controlStatements:
            toks = rec.text.split()
            if len(toks) < 2 or toks[0].upper() != 'PROT':
                continue
            csect = toks[1]
            ranges = []
            for piece in (toks[2].split(',') if len(toks) > 2 else []):
                try:
                    s, e = piece.split('-')
                    ranges.append((int(s, 16), int(e, 16)))
                except ValueError:
                    log.warning(f"{filename}: bad PROT range {piece!r}")
            for mod in modules:
                if csect in mod.sectionsByName:
                    mod.protManaged.add(csect)
                    mod.protRangesHw.setdefault(csect, []).extend(ranges)
                    break

        # Extract per-CSECT frame sizes from SYM STACKEND entries.  The
        # STACKEND value is the block's frame high-water (MAXTEMP) in BYTES.
        allSymbols = [s for cmod in objfile.modules for s in cmod.symbols]
        if modules and allSymbols:
            csectName = None
            for sym in allSymbols:
                if sym.name is None:
                    continue
                if sym.symType == objModule.SymRefType.CONTROL:
                    csectName = sym.name
                elif sym.name == 'STACKEND' and csectName is not None:
                    stackBytes = sym.offsetInCSECT or 0
                    for mod in modules:
                        if csectName in mod.sectionsByName:
                            mod.stackSizes[csectName] = stackBytes
                            break

        return modules, errors, warnings


class GlobalSymbol(NamedTuple):
    section: 'Section'
    module: 'LoadModule'
    address: 'Addr | None'


class Linker:
    def __init__(self, args):
        self.args = args
        self.modules = []
        self.globalSymbols: 'dict[str, GlobalSymbol]' = {}
        self.undefinedSymbols = defaultdict(set)  # name -> set of (filename, csect_or_None)
        # Link-order pins (--link-order JSON); empty = deterministic
        # defaults, no pinned autocall placement.
        lo = getattr(args, 'link_order', None)
        self.linkOrder = LinkOrder.load(lo) if lo else LinkOrder()
        # RESERVE-carded csects (allocation only, no text) -- see
        # assignAddressesFromConcard and saveLib.
        self.reservedCsects = set()
        self._concardProgram = None
        self._concardPlacement = None
        # Byte intervals reserved by MAP cards (earlier-phase content that is
        # already in memory): occupancy only, never part of this image.
        self.mappedIntervals = []
        # Earlier-phase OVERLAY region starts (name -> byte addr) from every
        # --map-lib load module: an origin-less OVERLAY card naming one of
        # these RE-OPENS the region for replacement content.
        self.phaseRegionStarts = {}
        # Symbols DEFINED by an earlier phase (every SD/LD name in every
        # --map-lib load module).  The original linkage editor processed the
        # master deck as one job, so a later segment's reference to a symbol
        # an earlier segment defined resolved in place and never triggered
        # autocall; a phase build must likewise not pull a duplicate library
        # copy of an already-resident symbol (the sites stay unresolved here
        # and phaseresolve patches them against the earlier phase's lib).
        self.phaseDefinedSymbols = set()
        # SD subset of the above with the base's byte address: used to pin a
        # RE-SUPPLIED base csect (this phase carries its own copy of base
        # content for configs that run without the base phase) at the base's
        # address instead of auto-placing it.
        self.phaseDefinedAddrs = {}
        # Byte LENGTH of those same base SDs -- needed to reserve the space of
        # a base-defined @-stack this link drops (see stackCsectNames and
        # _droppedStackIntervals).
        self.phaseDefinedSizes = {}
        # [lo, hi) byte extents of every SD in the MAP-carded phases' libs:
        # the co-resident base's memory footprint.  A roll-in phase's NEW
        # overlay content is packed into the FREE GAPS of this footprint
        # (see conlayout's roll-in pool).
        self.phaseDefinedExtents = []
        # Carried Z1 pool cursor (byte, 0 = none): the LE ran the
        # master deck as ONE job, so its pool cursor persisted across phase
        # segments -- a later phase's NEW ZCONs pack where the co-resident
        # earlier phases' cursor stands, never from the pool floor (which
        # would re-assign the base pool's addresses and clobber them at load
        # time).  Read from the MAP-carded parents' .lib linkState
        # (zconPoolNext); this link's saveLib carries it forward cumulatively.
        self.phaseDefinedZconNext = 0
        self.image: 'bytearray | None' = None
        self.imageBase = Addr(args.base_address)
        self.imageSize = AddrDisp(0)
        self.entryPoint: 'Addr | None' = None
        self.errors = []
        self.warnings = []
        self.appliedRelocations = []
        self.unresolvedRelocations = []
        self.csectTable = {}
        # The contents of every CSECT-table entry marked
        # "linkInfo": "placement": defined, so the symbol table carries
        # them, and never used to resolve a relocation (loadExternalSyms).
        self.placementOnlySymbols = set()
        self.deadERsLogged = set()
        # CON80 NOCALLER (NCAL): unresolved externals are tolerated and left
        # in the image (the linkage editor's automatic-library-call is off).
        self.nocall = getattr(args, 'nocall', False)
        self._libIndex = None          # lazy symbol->path index of -L dirs
    
    def loadInputFiles(self):
        """Load all input files and explicit libraries from args."""
        for filename in self.args.input_files:
            if not os.path.exists(filename):
                error(f"Input file not found: {filename}")
            if self.loadModule(filename) is None:
                error(f"Failed to load: {filename}")

        for libName in self.args.library:
            if not self._loadLibrary(libName):
                error(f"Library not found: {libName}")

    def _loadLibrary(self, libName):
        """Search -L paths for libName{,.obj,.OBJ} and load the first match."""
        for libPath in self.args.library_path:
            for ext in ['', '.obj', '.OBJ']:
                candidate = os.path.join(libPath, libName + ext)
                if os.path.exists(candidate) and self.loadModule(candidate):
                    return True
        return False

    def printErrors(self):
        for err in self.errors:
            log.error(err)
        self.errors.clear()

    def printWarnings(self):
        for warn in self.warnings:
            log.warning(warn)
        self.warnings.clear()

    def loadModule(self, filename):
        if not os.path.exists(filename):
            return None
        log.info(f"Loading {filename}...")
        modules, errors, warnings = LoadModule.load(filename, quiet=self.args.quiet)
        self.errors.extend(errors)
        self.warnings.extend(warnings)
        self.modules.extend(modules)
        return modules or None

    def _librarySymbolIndex(self):
        """symbol -> object path over every *.obj in the -L dirs, parsed once
        (first defining file in search order wins).  Replaces the old
        per-symbol directory rescan, which re-parsed every library object for
        every undefined symbol on every resolve iteration."""
        if self._libIndex is None:
            self._libIndex = {}
            nfiles = 0
            for libPath in self.args.library_path:
                if not os.path.isdir(libPath):
                    continue
                for fname in sorted(os.listdir(libPath)):
                    if not fname.lower().endswith('.obj'):
                        continue
                    candidate = os.path.join(libPath, fname)
                    try:
                        mods = objModule.ObjectFile(candidate).modules
                    except Exception:
                        continue
                    nfiles += 1
                    for cmod in mods:
                        for e in cmod.esdEntries:
                            if e.type in (objModule.EsdType.SD,
                                          objModule.EsdType.LD):
                                self._libIndex.setdefault(e.name.strip(),
                                                          candidate)
            log.info(f"Indexed {nfiles} library object(s), "
                     f"{len(self._libIndex)} symbol(s)")
        return self._libIndex

    def findModuleForSymbol(self, symbolName):
        # ZCON thunks: #QSQRT is in #QSQRT.obj (also extensionless, and
        # without the # prefix on some systems) -- keep this fast path so a
        # same-named file wins over the alphabetical index.
        bases = [symbolName] + \
                ([symbolName[1:]] if symbolName.startswith('#') else [])
        for libPath in self.args.library_path:
            for base in bases:
                for ext in ['.obj', '.OBJ', '']:
                    candidate = os.path.join(libPath, base + ext)
                    if os.path.exists(candidate):
                        if self._moduleDefinesSymbol(candidate, symbolName):
                            return candidate
        return self._librarySymbolIndex().get(symbolName)
    
    def _moduleDefinesSymbol(self, filename, symbolName):
        """Check if an object file defines the given symbol."""
        try:
            for cmod in objModule.ObjectFile(filename).modules:
                for e in cmod.esdEntries:
                    if (e.name.strip() == symbolName
                            and e.type in (objModule.EsdType.SD, objModule.EsdType.LD)):
                        return True
        except Exception:
            pass
        return False
    
    def buildGlobalSymbolTable(self):
        """Build the global symbol table from all loaded modules."""
        for module in self.modules:
            for section in module.allSections():
                name = section.name
                if not name or section.type not in ('SD', 'LD'):
                    continue
                if name in self.globalSymbols:
                    existing = self.globalSymbols[name]
                    # An SD and a same-name LD from the SAME module are one
                    # symbol -- the control section and its own entry-point
                    # label (`FOO CSECT` + `ENTRY FOO`), not a redefinition.
                    # Keep the SD as canonical and say nothing.
                    sameModule = existing.module is module
                    sdLdPair = {existing.section.type, section.type} == {'SD', 'LD'}
                    if sameModule and sdLdPair:
                        if existing.section.type != 'SD':
                            self.globalSymbols[name] = \
                                GlobalSymbol(section, module, None)
                    else:
                        self.warnings.append(
                            f"Symbol '{name}' defined in both "
                            f"{existing.module.filename} and {module.filename}")
                else:
                    self.globalSymbols[name] = GlobalSymbol(section, module, None)
                    detail = f"length=0x{section.length:X}" if section.type == 'SD' \
                             else f"offset=0x{section.address:X}"
                    log.debug(f"Symbol {section.type} '{name}' defined in "
                              f"{module.name} ESD#{section.esdId} {detail}")

        for module in self.modules:
            for esdId, ext in module.externals.items():
                if ext.name not in self.globalSymbols:
                    self._addUndefRef(ext.name, module, esdId)

    def _addUndefRef(self, symName, module, esdId):
        """Record that module references undefined symbol symName (ESD ID esdId).
        Uses RLD entries to identify which CSECT(s) contain the reference.

        If no RLD entry references the ER, the symbol is dead — emitted by the
        compiler (e.g., for an INCLUDE'd COMPOOL whose variables are never
        actually used) but never relocated against. Drop it silently with an
        informational message rather than failing the link."""
        # relId -> referencing csect names, computed once per module (its
        # relocations are fixed after load; this runs for every unresolved
        # external on every symbol-table rebuild).
        relmap = getattr(module, '_rldCsectsByRelId', None)
        if relmap is None:
            relmap = {}
            for r in module.relocations:
                s = module.sections.get(r.posId)
                if s is not None:
                    relmap.setdefault(r.relId, set()).add(s.name.strip())
            module._rldCsectsByRelId = relmap
        csects = relmap.get(esdId, set())
        if csects:
            for csect in csects:
                self.undefinedSymbols[symName].add((module.filename, csect))
        else:
            key = (module.filename, symName)
            if key not in self.deadERsLogged:
                self.deadERsLogged.add(key)
                log.info(f"Dropping dead ER '{symName}' in "
                         f"{Path(module.filename).name} (no RLD references)")

    def resolveExternals(self, searchLibraries=True):
        """Resolve external references against the global symbol table,
        optionally pulling library modules for missing symbols until the
        table stops growing."""
        newModulesLoaded = True
        iterations = 0
        maxIterations = 100  # backstop against a load/rebuild loop

        while newModulesLoaded and iterations < maxIterations:
            iterations += 1
            newModulesLoaded = False

            self._rebuildSymbolTable()

            if searchLibraries and self.undefinedSymbols:
                for symName in list(self.undefinedSymbols):
                    if symName in self.globalSymbols:
                        continue
                    # Already defined by an earlier phase: the LE
                    # resolved this in place (one multi-segment job); pulling
                    # a library copy here would duplicate a resident symbol
                    # at a phase-local address.  Leave the sites for the
                    # cross-phase resolution pass.
                    if symName in self.phaseDefinedSymbols:
                        continue

                    filename = self.findModuleForSymbol(symName)
                    if filename and \
                            not any(m.filename == filename for m in self.modules):
                        if self.loadModule(filename):
                            newModulesLoaded = True
                            log.info(f"Loaded {filename} for symbol '{symName}'")

        # Final pass: resolve all externals
        for module in self.modules:
            for esdId, ext in module.externals.items():
                if ext.name in self.globalSymbols:
                    ext.resolved = True
                    ext.resolvedSection = self.globalSymbols[ext.name].section
                elif not ext.weak:
                    self._addUndefRef(ext.name, module, esdId)

        return len(self.undefinedSymbols) == 0

    def _occupiedIntervals(self):
        """Byte intervals [lo, hi) of every placed SD section plus the
        MAP-reserved earlier-phase intervals, sorted.

        Deliberately NOT the co-resident phases' whole footprint: the LE's
        occupancy is per OVERLAY PATH, and a later segment REPLACES earlier
        content wholesale.  The deck's MAP/OVERLAY cards carry that tree;
        a flat union of parent extents would forbid every legitimate
        replacement."""
        return sorted(
            [(int(s.baseAddress), int(s.baseAddress + s.length))
             for s in self._placedSDs() if s.length > 0]
            + [(int(lo), int(hi)) for lo, hi in self.mappedIntervals])

    def _placeSections(self, sections, startAddr, label=""):
        """Place a list of sections sequentially starting at startAddr,
        skipping over ranges already occupied by placed sections (a CON80
        layout places csects all over; auto-placed stragglers must flow
        around them, not overwrite them).
        Returns the address after the last section placed.
        All addresses are in bytes.
        """
        occupied = self._occupiedIntervals()
        currentAddr = Addr(startAddr)
        for section in sections:
            currentAddr = currentAddr.align(4)
            # advance past occupied intervals that clash with this section
            moved = True
            while moved:
                moved = False
                for lo, hi in occupied:
                    if lo < currentAddr + section.length and int(currentAddr) < hi:
                        currentAddr = Addr(hi).align(4)
                        moved = True

            section.baseAddress = currentAddr
            currentAddr = currentAddr + section.length

            log.info(f"  {section.name:8s} @ 0x{section.baseAddress:06X} "
                     f"({section.length} bytes){' [' + label + ']' if label else ''}")
            log.debug(f"Section '{section.name}' ({section.zone}) "
                      f"@ {section.baseAddress.x} len=0x{section.length:X}(bytes) "
                      f"end=0x{currentAddr:06X}(byte)")
        return currentAddr

    @staticmethod
    def _findLDOwner(section, module, requireBaseAddress=False):
        """Find the SD section that owns an LD label.
        Tries ldId first, falls back to address-range scan."""
        owner = None
        if section.ldId is not None and section.ldId > 0:
            owner = module.sections.get(section.ldId)
        if owner is None or owner.type != 'SD':
            for candId, candSect in module.sections.items():
                if candSect.type != 'SD':
                    continue
                if requireBaseAddress and candSect.baseAddress is None:
                    continue
                if section.address < candSect.length:
                    owner = candSect
                    break
        return owner

    def _resolveLDLabels(self):
        """Resolve LD (label) addresses relative to their owning SD sections."""
        for module in self.modules:
            for section in module.lds:
                owner = self._findLDOwner(section, module, requireBaseAddress=True)
                if owner is None:
                    continue
                # LD address is module-absolute bytes; subtract the
                # owner's original address to get section-relative bytes.
                ldOffset = section.address - owner.address
                section.baseAddress = owner.baseAddress + ldOffset
                log.debug(f"Label '{section.name}' -> owner '{owner.name}' "
                          f"addr={section.baseAddress.x} "
                          f"(owner {owner.baseAddress.x} + offset 0x{ldOffset:X})")

    def _updateGlobalSymbols(self):
        for name, gsym in self.globalSymbols.items():
            if gsym.section.baseAddress is not None:
                self.globalSymbols[name] = \
                    gsym._replace(address=gsym.section.baseAddress)
                log.debug(f"Global symbol '{name}' -> "
                          f"addr={gsym.section.baseAddress.x}")

    def _poolBlockOverride(self):
        """Pinned pool-block ordering (linkorder poolBlocks).  Active only
        when wave modules are in the link; their #Q needs come from their
        own ESD ER lists, minus #Q names the MAP-imported base pool
        already defines."""
        cached = getattr(self, '_poolBlockOverrideCache', None)
        if cached is not None:
            return cached
        waveMods = self.linkOrder.wave_modules()
        qErs = {}
        for mod in self.modules:
            if mod.external or mod.name not in waveMods:
                continue
            qErs[mod.name] = [e.name for e in mod.externals.values()
                              if e.name.startswith('#Q')]
        override = {}
        if qErs:
            baseQ = {n for n in self.phaseDefinedSymbols
                     if n.startswith('#Q')}
            override = self.linkOrder.pool_block_override(qErs, baseQ)
        self._poolBlockOverrideCache = override
        return override

    def _mcPoolOverride(self):
        """Z1-pool ordering pinned by a per-MC section (linkorder mc):
        active when autocall is live and the link defines the section's
        anchor csect."""
        cached = getattr(self, '_mcPoolOverrideCache', None)
        if cached is not None:
            return cached
        override = {}
        if self.linkOrder.mc and self._autocallStems():
            defined = {s.name.strip()
                       for m in self.modules if not m.external
                       for s in m.sections.values() if s.type == 'SD'}
            mc = self.linkOrder.mc_for(defined)
            if mc is not None and mc.pool:
                override = self.linkOrder.pool_override(mc)
        self._mcPoolOverrideCache = override
        return override

    def _mcPoolFloor(self) -> int:
        """The per-MC Z1-pool floor pin (hw), 0 when none applies; same
        activation rule as _mcPoolOverride (autocall live + the mc
        section's anchor defined in this link)."""
        cached = getattr(self, '_mcPoolFloorCache', None)
        if cached is not None:
            return cached
        floor = 0
        if self.linkOrder.mc and self._autocallStems():
            defined = {s.name.strip()
                       for m in self.modules if not m.external
                       for s in m.sections.values() if s.type == 'SD'}
            mc = self.linkOrder.mc_for(defined)
            if mc is not None:
                floor = int(getattr(mc, 'poolFloor', 0) or 0)
        self._mcPoolFloorCache = floor
        return floor

    def _classifySections(self, sections):
        """Classify and sort sections into (zcon, data, code) groups.

        ZCONs are ordered by the canonical Z1-pool ordinal (--link-order
        pins), with pool blocks derived from the link's own module ESDs
        (_poolBlockOverride); DATA/CODE remain name-sorted.
        """
        override = dict(self._poolBlockOverride())
        override.update(self._mcPoolOverride())
        groups = {'ZCON': [], 'DATA': [], 'CODE': []}
        for s in sections:
            groups[s.zone].append(s)
        groups['ZCON'].sort(
            key=lambda s: self.linkOrder.zcon_sort_key(s.name, override))
        groups['DATA'].sort(key=lambda s: s.name.strip())
        groups['CODE'].sort(key=lambda s: s.name.strip())
        return groups['ZCON'], groups['DATA'], groups['CODE']

    def _placedSDs(self):
        """Every placed SD section of the non-external modules."""
        for module in self.modules:
            if module.external:
                continue
            for section in module.sections.values():
                if section.type == 'SD' and section.baseAddress is not None:
                    yield section

    def storeProtectRangesHw(self):
        """Absolute halfword [start, end) ranges that carry store protection.

        Explicit ' PROT' ranges when the assembler captured SPON/SPOFF, else
        the csect's SET/CLEAR mark, else the name-class default.  Feeds both
        the .lib PROT records and the .sym.json map.
        """
        from ap101Utils.conlayout import default_protected
        ranges = []
        for s in self._placedSDs():
            name = s.name.strip()
            if int(s.length) <= 0 or name in self.reservedCsects:
                continue
            nHw = (int(s.length) + 1) // 2
            baseHw = int(s.baseAddress) // 2
            m = s.module
            if m is not None and name in m.protManaged:
                for a, b in m.protRangesHw.get(name, []):
                    if a < nHw:
                        ranges.append((baseHw + a, baseHw + min(b, nHw)))
            elif (s.protected if s.protected is not None
                  else default_protected(name)):
                ranges.append((baseHw, baseHw + nHw))
        merged = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return merged

    def _computeImageSize(self):
        """Compute imageSize from the highest SD section end."""
        highestEnd = self.imageBase
        for section in self._placedSDs():
            end = section.baseAddress + section.length
            if end > highestEnd:
                highestEnd = end
        self.imageSize = highestEnd - self.imageBase  # AddrDisp
        return self.imageSize

    def _highestEndInRange(self, lo, hi):
        """Find the highest section end address within [lo, hi)."""
        best = lo
        for section in self._placedSDs():
            end = section.baseAddress + section.length
            if lo <= section.baseAddress < hi and end > best:
                best = end
        for mlo, mhi in self.mappedIntervals:
            if lo <= mlo < hi and mhi > best:
                best = mhi
        return best

    def _finishAddressAssignment(self):
        """Common post-placement: resolve LD labels, update symbols, compute size."""
        self._resolveLDLabels()
        self._updateGlobalSymbols()
        self._computeImageSize()
        return self.imageSize

    def assignAddresses(self):
        """Assign base addresses to all sections.
        All addresses are in BYTES.
        --compact: sequential layout with ZCON first.
        Otherwise: ZCON in sector 0 low, DATA in sectors 0-1, CODE in sector 2+.
        """
        allSections = []
        for module in self.modules:
            for section in module.sections.values():
                if section.type == 'SD' and section.baseAddress is None:
                    allSections.append(section)

        if self.args.compact:
            log.info("Address assignment: compact (sequential) layout")
            allSections.sort(key=lambda s: (0 if s.name.strip().startswith('#Q') else 1,
                                            s.name.strip()))
            self._placeSections(allSections, self.imageBase, "compact")
        else:
            log.info("Address assignment: sector-based layout")
            zcon, data, code = self._classifySections(allSections)

            log.debug(f"Section classification: {len(zcon)} ZCON, "
                  f"{len(data)} DATA, {len(code)} CODE")

            zconStart = max(self.imageBase, PSA_END)
            log.info(f"  --- ZCON zone: 0x{zconStart:06X}–0x{ZCON_END:06X} ---")
            zconEnd = self._placeSections(zcon, zconStart, "ZCON")
            if zconEnd > ZCON_END:
                log.warning(f"ZCON sections overflow ZCON zone: end 0x{zconEnd:06X} > "
                        f"limit 0x{ZCON_END:06X} "
                        f"({(zconEnd - ZCON_END).hw} halfwords over)")

            sector1End = 2 * SECTOR_SIZE
            log.info(f"  --- DATA zone: 0x{zconEnd:06X}– (sectors 0-1) ---")
            dataEnd = self._placeSections(data, zconEnd, "DATA")
            if dataEnd > sector1End:
                log.warning(f"DATA sections overflow sectors 0-1: end 0x{dataEnd:06X} > "
                        f"limit 0x{sector1End:06X}")

            codeStart = max(dataEnd, 2 * SECTOR_SIZE)
            log.info(f"  --- CODE zone: 0x{codeStart:06X}– (sector 2+) ---")
            self._placeSections(code, codeStart, "CODE")

        return self._finishAddressAssignment()

    def _collectModulePaths(self, config):
        """Populate config.modules with paths relative to the .lnk output dir."""
        if self.args.save_config:
            lnkDir = Path(self.args.save_config).resolve().parent
        else:
            lnkDir = Path.cwd()

        seenFiles = set()
        for module in self.modules:
            if module.filename not in seenFiles:
                seenFiles.add(module.filename)
                try:
                    relPath = str(Path(module.filename).resolve().relative_to(lnkDir))
                except ValueError:
                    relPath = module.filename
                config.modules.append(relPath)

    def generateConfig(self, includeAddresses=True):
        """Generate a LinkConfig from the current linker state.
        With includeAddresses=True (post-assignment), SD addresses are captured.
        With includeAddresses=False (pre-assignment), SD addresses are None.
        """
        config = LinkConfig()
        config.imageBase = self.imageBase
        config.entryPoint = self.args.entry
        if not config.entryPoint:
            for module in self.modules:
                if module.entryName:
                    config.entryPoint = module.entryName
                    break

        self._collectModulePaths(config)

        for module in self.modules:
            for section in module.allSections():
                if section.type == 'SD':
                    config.sections.append({
                        'name': section.name,
                        'module': module.name,
                        'type': 'SD',
                        'address': section.baseAddress if includeAddresses else None,
                        'length': int(section.length),
                    })
                elif section.type == 'LD':
                    owner = self._findLDOwner(section, module)
                    config.sections.append({
                        'name': section.name,
                        'module': module.name,
                        'type': 'LD',
                        'ldOffset': int(section.address),
                        'ldOwner': owner.name if owner else None,
                    })

        # Capture synthetic modules
        _SYNTHETIC = {
            '<defined-symbols>': ('definedSymbols',
                lambda s: {'name': s.name, 'address': s.baseAddress}
                           if s.baseAddress is not None else None),
            '<generated-stacks>': ('generatedStacks',
                lambda s: {'name': s.name, 'sizeBytes': int(s.length)}
                           if s.type == 'SD' else None),
        }
        for module in self.modules:
            spec = _SYNTHETIC.get(module.filename)
            if not spec:
                continue
            attr, mkEntry = spec
            for section in module.allSections():
                entry = mkEntry(section)
                if entry:
                    getattr(config, attr).append(entry)

        return config

    def assignAddressesFromConfig(self, config):
        """Assign section addresses using a merged LinkConfig.
        Config-specified addresses are applied first, then unassigned
        sections are auto-placed using sector-based rules.
        """
        self.imageBase = config.imageBase

        # Build lookup: (name, moduleName) -> config entry for SD sections
        cfgLookup = {(cs['name'], cs['module']): cs
                     for cs in config.sections if cs.get('type') == 'SD'}

        # First pass: apply config-specified addresses
        for module in self.modules:
            for section in module.sections.values():
                if section.type != 'SD':
                    continue
                ce = cfgLookup.get((section.name, module.name))
                if ce and ce.get('address') is not None:
                    section.baseAddress = Addr(ce['address'])
                    log.info(f"  {section.name:8s} @ 0x{section.baseAddress:06X} "
                             f"({section.length} bytes) [from config]")

        # Second pass: auto-place unassigned SD sections
        unassigned = [s for m in self.modules
                      for s in m.sections.values()
                      if s.type == 'SD' and s.baseAddress is None]

        # An auto-placed csect the co-resident base already DEFINES (an
        # external MAP import at a known address) is a RE-SUPPLY of base
        # content: some deck members carry base compools for memory configs
        # that run without the base phase, and their un-INSERTed sibling
        # sections (the module's #Z pool entry) ride along.  Such a csect
        # stays at the base address in EVERY config -- the one-job linkage
        # editor had a single copy.  Pin it there instead of packing it
        # into this phase's pool/zone.
        baseAddr = dict(self.phaseDefinedAddrs)
        baseAddr.update({s.name: int(s.baseAddress)
                         for m in self.modules if m.external
                         for s in m.sections.values()
                         if s.type == 'SD' and s.baseAddress is not None})
        resupplied = [s for s in unassigned if s.name in baseAddr]
        for s in resupplied:
            s.baseAddress = Addr(baseAddr[s.name])
            log.info(f"  {s.name:8s} @ 0x{int(s.baseAddress):06X} "
                     f"[re-supply of base copy]")
        if resupplied:
            unassigned = [s for s in unassigned if s.baseAddress is None]

        if self.args.compact or not unassigned:
            self._placeSections(unassigned,
                                self._highestEndInRange(0, float('inf')),
                                "auto-placed")
        else:
            zcon, data, code = self._classifySections(unassigned)
            if zcon:
                # placement is occupancy-aware: start at the zone floor and
                # flow around deck-placed content (the real LE packs library
                # ZCONs right after the PSA/LOWCORE region).  Anchor the pool
                # at the first deck-placed ZCON (the `OVERLAY Z1` origin) so
                # the canonically-ordered remainder packs AFTER it -- not
                # into the PSA checksum gap just below it.
                placedZconStarts = [int(s.baseAddress)
                                    for m in self.modules
                                    for s in m.sections.values()
                                    if s.type == 'SD' and s.baseAddress is not None
                                    and s.zone == 'ZCON']
                poolStart = min(placedZconStarts) if placedZconStarts else PSA_END
                if self.phaseDefinedZconNext:
                    # One-job pool continuation: the carried LE cursor (which
                    # already includes the parents' closing checksum slots)
                    # says where the pool stands.
                    poolStart = max(int(poolStart), self.phaseDefinedZconNext)
                mcFloor = self._mcPoolFloor()
                if mcFloor:
                    poolStart = max(int(poolStart), mcFloor * 2)
                self._placeSections(zcon, poolStart, "auto-ZCON")
            if data:
                self._placeSections(data,
                    max(self._highestEndInRange(0, 2 * SECTOR_SIZE), ZCON_END),
                    "auto-DATA")
            if code:
                self._placeSections(code,
                    max(self._highestEndInRange(2 * SECTOR_SIZE, float('inf')),
                        2 * SECTOR_SIZE),
                    "auto-CODE")

        return self._finishAddressAssignment()

    def assignAddressesFromConcard(self):
        """Assign SD section addresses from a CON80 layout"""
        from ap101Utils import conlayout

        # Every SD csect name -> its module's ordered (name, size_halfwords) list.
        module_csects: dict[str, list[tuple[str, int]]] = {}
        section_by_name: dict[str, object] = {}
        for module in self.modules:
            sds = [(s.name, int(s.length) // 2)
                   for s in module.sections.values() if s.type == 'SD']
            for name, _ in sds:
                module_csects.setdefault(name, sds)
            for s in module.sections.values():
                if s.type == 'SD':
                    section_by_name.setdefault(s.name, s)

        # Csects already defined by the co-resident base -- MAP imports plus
        # every SD in the MAP-carded phases' libs: a library INCLUDE of one
        # of these replaces it in place at its already-loaded address, and
        # the roll-in pool must NOT capture a re-supply INSERT of one (it
        # keeps whole-block flow and lands at the base address).
        existing = {n: a // 2 for n, a in self.phaseDefinedAddrs.items()}
        existing.update({s.name.strip(): int(s.baseAddress) // 2
                         for m in self.modules if m.external
                         for s in m.sections.values()
                         if s.type == 'SD' and s.baseAddress is not None})

        # Modules this job pulled by automatic library call (con80build
        # --autocall): the layout places their csects in the autocall waves
        # (conlayout's autocall wave/stream pins), not by deck cards.
        autocall_units: list[tuple[str, list[tuple[str, int]]]] = []
        stems = self._autocallStems()
        if stems:
            byname = {}
            for m in self.modules:
                if not m.external:
                    byname.setdefault(m.name, m)
            for stem in stems:
                m = byname.get(stem)
                if m is not None:
                    autocall_units.append(
                        (stem, [(s.name, int(s.length) // 2)
                                for s in m.sections.values()
                                if s.type == 'SD']))

        program = self._concardLayout()
        placement = conlayout.LayoutEngine(
            module_csects,
            occupied=[(lo // 2, hi // 2) for lo, hi in self.mappedIntervals]
            + self._droppedStackIntervals(),
            phase_regions={n: a // 2
                           for n, a in self.phaseRegionStarts.items()},
            existing=existing,
            base_occupied=[(lo // 2, hi // 2)
                           for lo, hi in self.phaseDefinedExtents],
            local_defs={s.name.strip()
                        for m in self.modules if not m.external
                        for s in m.sections.values() if s.type == 'SD'},
            link_order=self.linkOrder,
        ).run(program, autocall_units=autocall_units)

        placed = 0
        for name, hw in placement.addresses.items():
            s = section_by_name.get(name)
            if s is not None and s.baseAddress is None:
                s.baseAddress = Addr.from_hw(hw)
                s.protected = placement.protected.get(name)
                placed += 1
        self._concardPlacement = placement
        log.info(f"Placed {placed} section(s) from CON80 layout "
                 f"(root {self.args.concard_root}); "
                 f"{len(placement.unknown_inserts)} INSERT(s) had no loaded module")
        for line in placement.log:
            if line.startswith(("orphan-flush ", "autocall/",
                                "autocall-stream ")):
                log.debug(f"  layout: {line}")
            else:
                log.warning(f"  layout: {line}")

        # RESERVE-carded csects: allocation only (the MMU load-block reserve
        # bit).  The SD keeps its address+length (symbols resolve, occupancy
        # holds, CESD carries it) but NO text loads: blank the data and drop
        # the csect's own fixups so neither text copy, image fixups, the
        # retained RLD, nor the .lib extents carry any content for it.
        self.reservedCsects = set(placement.reserved)
        if self.reservedCsects:
            for m in self.modules:
                if m.external:
                    continue
                resIds = {esdId for esdId, s in m.sections.items()
                          if s.type == 'SD'
                          and s.name.strip() in self.reservedCsects}
                if not resIds:
                    continue
                for esdId in resIds:
                    m.sections[esdId].data = bytearray()
                m.relocations = [r for r in m.relocations
                                 if r.posId not in resIds]
            log.info(f"RESERVE: {len(self.reservedCsects)} allocation-only "
                     f"csect(s): {', '.join(sorted(self.reservedCsects))}")

        # Auto-place anything the deck did not name, after the highest occupied
        # address, using the sector-based rules (empty config => no overrides).
        return self.assignAddressesFromConfig(LinkConfig())

    def applyRelocations(self):
        """
        Apply all relocations to generate the final image.
        
        Address Convention:
            - Section baseAddress and imageOffset are in BYTES
            - targetAddr (from section lookup) is in BYTES
            - targetAddr.hw (written to code) is the byte address / 2
        """
        
        if self.image is None:
            self.image = bytearray(self.imageSize)
        
        #
        # Copy section text to output:
        #
        for section in self._placedSDs():
            offset = section.baseAddress - self.imageBase
            if log.isEnabledFor(logging.DEBUG) and section.length > 0:
                dataHex = section.data[:min(16, section.length)].hex()
                log.debug(f"COPY {section.module.name}/{section.name}: "
                          f"offset=0x{offset:06X}(byte) len={section.length} "
                          f"data={dataHex}...")
            off = int(offset)
            if off < 0:
                self.warnings.append(
                    f"{section.module.filename}: section {section.name} below "
                    f"image base (0x{int(section.baseAddress):06X}); not copied")
                continue
            end = min(off + len(section.data), len(self.image))
            self.image[off:end] = section.data[:end - off]

        if self.args.dump_unrelocated:
            # dump the image before we perform relocations for debugging:
            with open(self.args.dump_unrelocated, 'wb') as f:
                f.write(self.image)
            log.info(f"Wrote {len(self.image)} bytes (unrelocated) to {self.args.dump_unrelocated}")
        
        #
        # Apply relocations:
        #
        relocErrors = 0
        for module in self.modules:
            for reloc in module.relocations:
                # Find the section containing the relocation (P section)
                posSection = module.sections.get(reloc.posId)
                if posSection is None or posSection.baseAddress is None:
                    if posSection is None:
                        self.warnings.append(
                            f"{module.filename}: Unknown position section ESD#{reloc.posId}")
                    continue
                
                # Find what the relocation references (R section/external).
                # SD ids are authoritative, then ERs; LD ids last -- they are
                # real only in asm101-emitted objects (`DC Y(localEntry)`),
                # while halsfc LD "ids" are positional junk that must never
                # shadow an ER.
                targetAddr = Addr(0)
                resolved = True
                lenient = False     # unresolved-but-tolerated (NOCALLER / #P*)
                targetSection = module.sections.get(reloc.relId)
                if targetSection is None and reloc.relId not in module.externals:
                    targetSection = module.ldsByEsdId.get(reloc.relId)

                if targetSection is not None:
                    if targetSection.baseAddress is not None:
                        targetAddr = targetSection.baseAddress - targetSection.address
                    else:
                        resolved = False

                elif reloc.relId in module.externals:
                    ext = module.externals[reloc.relId]
                    # A placement-only symbol names a field of a section this
                    # configuration does not load.  The original link had no
                    # definition for it and left the site as assembled; the
                    # reference is an unresolved external here and is recorded
                    # in unresolvedRelocations.
                    if ext.name in self.placementOnlySymbols:
                        resolved = False
                        lenient = True
                    elif ext.resolved and ext.resolvedSection and ext.resolvedSection.baseAddress is not None:
                        targetAddr = ext.resolvedSection.baseAddress
                    else:
                        resolved = False
                        # Lenient #P* (REMOTE COMPOOL) references skip the
                        # apply step below — the IBM linker leaves existing
                        # TXT untouched for these (see issue #22).  Under
                        # NOCALLER every unresolved external is tolerated.
                        lenient = self.nocall or \
                            (ext.name.startswith('#P') and not self.args.strict_compools)
                        if not ext.weak and not lenient and not self.args.force:
                            self.errors.append(
                                f"{module.filename}: Unresolved external '{ext.name}' "
                                f"at offset 0x{reloc.address:06X}(byte)")
                            relocErrors += 1

                else:
                    self.warnings.append(
                        f"{module.filename}: Unknown relocation target ESD#{reloc.relId}")
                    resolved = False

                if not resolved and not lenient and not self.args.force:
                    continue

                # Calculate the image offset for this relocation (in bytes)
                # RLD address field contains byte offsets relative to P section
                # All internal addresses are in bytes, so this is a direct calculation
                imageOffset = posSection.baseAddress - self.imageBase + reloc.address

                if imageOffset < 0 or imageOffset >= len(self.image):
                    self.warnings.append(
                        f"{module.filename}: Relocation address out of bounds: 0x{imageOffset:06X}")
                    continue

                # Record unresolved relocations for analysis
                if not resolved and reloc.relId in module.externals:
                    ext = module.externals[reloc.relId]
                    con = AddrCon(reloc.flags, reloc.length)
                    existing = int.from_bytes(
                        self.image[imageOffset:imageOffset + con.length], 'big')
                    self.unresolvedRelocations.append({
                        "symbol": ext.name,
                        "imageOffset": int(imageOffset),
                        "imageOffsetHW": imageOffset.hw,
                        "flags": reloc.flags,
                        "length": con.length,
                        "sign": con.sign,
                        "direction": con.direction,
                        "section": posSection.name.strip(),
                        "sectionOffset": int(reloc.address),
                        "module": Path(module.filename).name,
                        "existing": existing,
                    })

                # Unresolved RLDs: behavior depends on RLD type (issue #22).
                #   ZCON address (0x04/0x10/0x50): 
                #     the IBM linker still sets bit 15 of HW0.
                #     HW1 (BSR/DSR) is left untouched since RF is unknown.
                #   YCON / ACON / BSR-only / DSR-only: TXT untouched.
                # A section the CSECT table does not name is not part of the
                # configuration that table describes, so the build being
                # reproduced had nothing to resolve the reference against and
                # left it alone.  We would otherwise place the section at
                # whatever address came next and patch a fabricated value,
                # which cannot match the image the table came from.  Treat it
                # exactly as an unresolved reference, which is what it is.
                # Truthiness, not "is not None": csectTable defaults to {}, so
                # a link with no table would otherwise find every section
                # absent.
                absent = (bool(self.csectTable)
                          and targetSection is not None
                          and targetSection.name.strip() not in self.csectTable)

                if not resolved or absent:
                    flagType = reloc.flags & 0x7F
                    if flagType in (RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA) \
                            and imageOffset < len(self.image):
                        self.image[imageOffset] |= 0x80
                    continue

                targetName = "???"
                if targetSection is not None:
                    targetName = targetSection.name
                elif reloc.relId in module.externals:
                    targetName = module.externals[reloc.relId].name

                log.debug(f"RELOC {module.name}: "
                      f"offset=0x{imageOffset:06X}(byte) flags=0x{reloc.flags:02X} "
                      f"target='{targetName}' addr={targetAddr.x}")

                flagType = reloc.flags & 0x7F
                sector = targetAddr.sector

                if flagType in (RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA,
                                RLD_BSR_ONLY, RLD_DSR_ONLY):
                    # ZCON relocation: read both halfwords, apply, write back
                    if imageOffset + 3 < len(self.image):
                        zcon = ZCon.from_image(self.image, imageOffset)
                        # Unmasked flags too: bit 7 is the sign, without
                        # which a negative displacement is added, not
                        # subtracted.
                        zcon.apply(targetAddr, flagType, reloc.flags)
                        zcon.write_to_image(self.image, imageOffset)
                        log.debug(f"  -> {zcon}")
                else:
                    # YCON / ACON: single-value relocation
                    self._applyRelocationValue(imageOffset, targetAddr, reloc)

                # Record the applied relocation for diagnostics / JSON output.
                if flagType not in (RLD_DSR_ONLY, RLD_BSR_ONLY):  # skip sector-only
                    con = AddrCon(reloc.flags, reloc.length)
                    finalVal = int.from_bytes(
                        self.image[imageOffset:imageOffset + con.length], 'big')
                    resolvedHW = sector_decode(finalVal, sector) if con.length == 2 else finalVal
                    self.appliedRelocations.append({
                        "address": imageOffset.hw,
                        "target": resolvedHW,
                        "targetName": targetName,
                        "flags": reloc.flags,
                    })

        return relocErrors == 0
    
    def _applyRelocationValue(self, imageOffset, targetAddr, reloc):
        """Apply a standard (non-sector-only) relocation to the image.
        targetAddr is an Addr. Caller handles 0x40/0x20 sector-only types."""
        assert self.image is not None
        con = AddrCon(reloc.flags, reloc.length)

        existing = int.from_bytes(
            self.image[imageOffset:imageOffset + con.length], 'big')

        newValue = con.apply(existing, targetAddr)

        log.debug(f"  -> {con}  existing=0x{existing:0{con.length*2}X} "
                  f"-> 0x{newValue:0{con.length*2}X}")

        self.image[imageOffset:imageOffset + con.length] = \
            newValue.to_bytes(con.length, 'big')
    
    def determineEntryPoint(self):
        """--entry symbol or numeric halfword address, else the first
        module-declared entry point, else the image base."""
        if self.args.entry:
            gsym = self.globalSymbols.get(self.args.entry)
            if gsym is not None and gsym.address is not None:
                self.entryPoint = gsym.address
                return
            try:
                self.entryPoint = Addr.from_hw(int(self.args.entry, 0))
                return
            except ValueError:
                self.warnings.append(f"Entry point symbol '{self.args.entry}' not found")

        for module in self.modules:
            if module.entryPoint:
                esdId, byteOffset = module.entryPoint
                section = module.sections.get(esdId) or module.ldsByEsdId.get(esdId)
                if section and section.baseAddress is not None:
                    self.entryPoint = section.baseAddress + byteOffset
                    return

            if module.entryName:
                gsym = self.globalSymbols.get(module.entryName)
                if gsym is not None and gsym.address is not None:
                    self.entryPoint = gsym.address
                    return

        self.entryPoint = self.imageBase

    def _getOrCreateSyntheticModule(self, filename: str,
                                    displayName: str) -> 'LoadModule':
        """Get or create a synthetic module for generated sections."""
        for m in self.modules:
            if m.filename == filename:
                return m
        module = LoadModule(filename)
        module.name = displayName
        self.modules.append(module)
        return module

    def _addSyntheticSection(self, module: 'LoadModule', name: str,
                             length: AddrDisp,
                             baseAddress: 'Addr | None' = None,
                             fill: int = 0) -> 'Section':
        """Add an SD section to a synthetic module. Returns the new Section."""
        section = Section(name, module.nextEsdId(), 'SD', AddrDisp(0), length, module,
                          data=bytearray([fill] * int(length)),
                          baseAddress=baseAddress)
        module.addSection(section)
        return section

    def _rebuildSymbolTable(self):
        """Clear and rebuild the global symbol table."""
        self.globalSymbols.clear()
        self.undefinedSymbols.clear()
        self.buildGlobalSymbolTable()

    def _concardLayout(self):
        """The CON80 layout program (cached), or [] without a deck."""
        if self._concardProgram is None:
            if self.args.concard:
                from ap101Utils import concard
                deck = concard.ConcardDeck(self.args.concard)
                self._concardProgram = concard.layout_program(
                    deck, self.args.concard_root)
            else:
                self._concardProgram = []
        return self._concardProgram

    def _deckStackNames(self):
        """@-stack names from the deck's ` STACK $0<prog>` cards ($ maps
        to @), deck order, deduplicated."""
        names = []
        for op in self._concardLayout():
            if op.verb == 'STACK' and op.operand and op.operand[0] in '$@':
                name = '@' + op.operand[1:]
                if name not in names:
                    names.append(name)
        return names

    def _deckMapPhases(self):
        """Phase numbers named by the deck's MAP cards."""
        phases = set()
        for op in self._concardLayout():
            if op.verb == 'MAP':
                tok = op.operand.split(',')[0].strip()
                if tok.isdigit():
                    phases.add(int(tok))
        return phases

    def _deckPhaseNumbers(self):
        """Phase numbers listed on the deck's PHASE card(s), card order."""
        out = []
        for op in self._concardLayout():
            if op.verb == 'PHASE':
                for tok in op.operand.replace(',', ' ').split():
                    if tok.isdigit() and int(tok) not in out:
                        out.append(int(tok))
        return out

    def applyConcardChanges(self):
        """Apply the deck's LE `CHANGE oldname(newname)` cards.

        The CESD rename chain built by CHANGE applies to the ONE module
        supplied by the next module-reading card -- in the CON80 decks always
        the following `INCLUDE SYSLIBL1(member)` (LE PLM p.28/35; several
        CHANGEs may stack before one INCLUDE).  The rename covers every ESD
        entry type: SD, LD, and ER (an ER rename redirects references inside
        the included module).

        When a rename makes the included module's SD collide with a same-named
        SD from another input module, the deck-included (renamed) copy is the
        survivor: in the original NCAL link the other member was never read at
        all, while our objdir glob supplies it as primary input.  The
        duplicate csect is deleted and its LDs are delinked onto the survivor
        at the same displacement (delete-type CESD entry + delinking, LE PLM
        p.38-39/44-45)."""
        pending: dict[str, str] = {}
        changes: list[tuple[str, dict[str, str]]] = []  # (member, {old: new})
        for op in self._concardLayout():
            if op.verb == 'CHANGE':
                m = re.match(r"([A-Z0-9#@$]+)\(([A-Z0-9#@$]+)\)", op.operand)
                if m:
                    pending[m.group(1)] = m.group(2)
                else:
                    log.warning(f"CHANGE operand not understood: "
                                f"{op.operand!r} (card {op.card})")
            elif op.verb == 'INCLUDE' and pending:
                changes.append((op.operand, pending))
                pending = {}
        if pending:
            log.warning(f"CHANGE card(s) without a following INCLUDE "
                        f"(ignored): {pending}")
        if not changes:
            return

        renamedSDs: list['Section'] = []
        for member, renames in changes:
            target = None
            for mod in self.modules:
                if not mod.external and member in mod.sectionsByName:
                    target = mod
                    break
            if target is None:
                log.warning(f"CHANGE: no loaded module supplies INCLUDE "
                            f"member {member!r}; renames {renames} skipped")
                continue
            for old, new in renames.items():
                hit = False
                for s in target.allSections():
                    if s.name == old:
                        if target.sectionsByName.get(old) is s:
                            del target.sectionsByName[old]
                        if s.origName is None:
                            s.origName = old
                        s.name = new
                        target.sectionsByName[new] = s
                        hit = True
                        if s.type == 'SD':
                            renamedSDs.append(s)
                ext = target.externalsByName.pop(old, None)
                if ext is not None:
                    ext.name = new
                    target.externalsByName[new] = ext
                    hit = True
                lvl = log.info if hit else log.warning
                lvl(f"CHANGE {old}({new}) applied to "
                    f"{Path(target.filename).name}"
                    f"{'' if hit else ': no such symbol (no-op)'}")

        # Duplicate-SD resolution: the renamed copy survives; any other input
        # module's same-named csect is deleted, its LDs delinked onto the
        # survivor at the same displacement.
        for winner in renamedSDs:
            for mod in list(self.modules):
                if mod.external or mod is winner.module:
                    continue
                dupIds = [i for i, s in mod.sections.items()
                          if s.name == winner.name]
                for esdId in dupIds:
                    dup = mod.sections[esdId]
                    for ld in [l for l in mod.lds if l.ldId == esdId]:
                        off = int(ld.address) - int(dup.address)
                        if ld.name not in winner.module.sectionsByName \
                                and 0 <= off < int(winner.length):
                            winner.module.addSection(Section(
                                ld.name, winner.module.nextEsdId(), 'LD',
                                AddrDisp(int(winner.address) + off),
                                module=winner.module, ldId=winner.esdId))
                            log.info(f"CHANGE: LD {ld.name} of deleted "
                                     f"duplicate {dup.name} delinked onto "
                                     f"survivor at +0x{off:X} bytes")
                        else:
                            log.warning(f"CHANGE: LD {ld.name} of deleted "
                                        f"duplicate {dup.name} NOT carried "
                                        f"(collision or out of range)")
                        mod.lds.remove(ld)
                        mod.ldsByEsdId.pop(ld.esdId, None)
                        if mod.sectionsByName.get(ld.name) is ld:
                            del mod.sectionsByName[ld.name]
                    del mod.sections[esdId]
                    if mod.sectionsByName.get(winner.name) is dup:
                        del mod.sectionsByName[winner.name]
                    mod.relocations = [r for r in mod.relocations
                                       if r.posId != esdId]
                    log.info(f"CHANGE: duplicate csect {winner.name} from "
                             f"{Path(mod.filename).name} deleted "
                             f"(survivor: {Path(winner.module.filename).name})")
                if dupIds and not mod.sections and not mod.lds:
                    self.modules.remove(mod)
                    log.info(f"CHANGE: module {Path(mod.filename).name} "
                             f"left empty; dropped from the link")

    def stackCsectNames(self):
        """@-stack CSECTs this link must create, in deck order.

        Primary source: the CON80 ` STACK $0<prog>` cards ($ maps to @) --
        SDL-mode objects carry no stack ERs at all, the cards are the only
        trigger.  Also any undefined @xxx ERs (NOSDL objects reference their
        own stack; tasks get synthesized @n ERs)."""
        deckNames = self._deckStackNames()
        wanted = list(deckNames)
        for symName in self.undefinedSymbols:
            if symName.startswith('@') and symName not in wanted:
                wanted.append(symName)
        # If a stack has already been generated in the base, use it
        # unless the current deck has an explicit STACK card.  In that
        # case the STACK replaces the MAP definition
        return [n for n in wanted
                if n in deckNames or n not in self.phaseDefinedSymbols]

    def _autocallStems(self):
        """Ordered module stems from con80build's autocall.json -- the ONE
        loader every autocall-conditioned behavior must go through (deferral
        exemption, placement waves, full-config-graph stack sizing).

        Policy: a missing, unreadable, or malformed file means NO autocall
        semantics at all -- never a half-armed mix (e.g. full-graph sizing
        keyed on file existence while the exemption parse failed).  NOCALLER
        + autocall is contradictory (NOCALLER *is* NCAL = autocall off):
        warn and disarm."""
        cached = getattr(self, '_autocallStemsCache', None)
        if cached is not None:
            return cached
        stems: list[str] = []
        aj = getattr(self.args, 'autocall_json', None)
        if aj and os.path.exists(aj):
            try:
                entries = json.loads(Path(aj).read_text())
                if not isinstance(entries, list):
                    raise ValueError("expected a top-level JSON list")
                for e in entries:
                    stem = Path(e["source"]).stem
                    if stem not in stems:
                        stems.append(stem)
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.warnings.append(
                    f"--autocall {aj}: unreadable ({e}); linking WITHOUT "
                    f"autocall semantics")
                stems = []
            if stems and self.nocall:
                self.warnings.append(
                    f"--autocall {aj} contradicts --nocall (NOCALLER = NCAL "
                    f"= automatic library call OFF); autocall DISARMED")
                stems = []
        self._autocallStemsCache = stems
        return stems

    def _droppedStackIntervals(self):
        """Halfword [lo, hi) extents of the base-defined @-stacks this link
        DROPS (stackCsectNames filters them out because a MAP'd/chained phase
        already generated them).

        Dropping is right -- but the space is still THEIRS, and this phase's
        sequential flow has to step over it.  Usually the base's region is
        MAP-imported anyway, so this adds nothing; the case that needs it is
        a phase that RE-SUPPLIES a base overlay via `INCLUDE CONCARDS(...)`
        instead of MAPping it, where the flow would otherwise pack content
        into the dropped stack's space.  Deliberately NARROW: a flat union
        of all base extents forbids legitimate replacement (see conlayout's
        base_occupied note)."""
        out, names = [], []
        for name in self._deckStackNames():
            if name not in self.phaseDefinedSymbols:
                continue                      # this link generates it itself
            addr = self.phaseDefinedAddrs.get(name)
            size = self.phaseDefinedSizes.get(name, 0)
            if addr is not None and size:
                names.append(f'{name}@{addr // 2:#x}')
                # +4 bytes = the trailing 2-hw checksum slot that closes the
                # base's block, same as mappedIntervals.
                out.append((addr // 2, (addr + size + 4) // 2))
            else:
                # base defines the stack but its CESD gives us no usable
                # extent: nothing gets reserved and the flow can pack into
                # the stack's space -- the overlap this method exists to
                # prevent.  Loud, not silent.
                self.warnings.append(
                    f"dropped base stack {name}: no reservable extent "
                    f"(addr={addr}, size={size}) -- flow may overlap it")
        if out:
            log.info(f"reserving {len(out)} dropped base @-stack extent(s): "
                     f"{', '.join(names)}")
        return out

    def generateStackSections(self):
        """Create the @-stack BSS CSECTs (named by stackCsectNames()).

        Sizes come from the longest-weighted-call-chain algorithm over the
        whole load module (stacksizer.py).  --generate-stacks N supplies a
        fallback size for stacks whose program block is not in the link."""
        from . import stacksizer

        wanted = [n for n in self.stackCsectNames()
                  if n not in self.globalSymbols]
        if not wanted:
            return

        stackSet = set(wanted)
        stackSet.update(self._deckStackNames())
        stackSet.update(n for n in self.phaseDefinedSymbols
                        if n.startswith('@'))
        sizerMods = list(self.modules)
        sizerSeen: set = set()      # MAP modules already loaded
        # scope the graph to the phases the DECK maps (con80build
        # passes every earlier-phase lib; unrelated phases' objdirs
        # must not deepen chains)
        mapPhases = self._deckMapPhases()
        for spec in self.args.map_lib:
            ph, _, libPath = spec.partition('=')
            if not ph.isdigit() or int(ph) not in mapPhases:
                continue
            phaseDir = os.path.join(os.path.dirname(libPath),
                                    Path(libPath).stem)
            objdir = os.path.join(phaseDir, 'obj')
            if not os.path.isdir(objdir):
                self.warnings.append(
                    f"autocall stack sizing: MAP phase {ph} objdir "
                    f"missing ({objdir}); its chains fall back to "
                    f"default frames (36B/depth 0)")
                continue
            # Only the objects that phase actually LINKED (its sym.json
            # module manifest) feed the sizer, and by the manifest's
            # recorded PATHS, not an objdir glob: a glob lets stale
            # autocalled .objs from an old run deepen chains, and it
            # MISSES every module linked from outside the objdir --
            # the HAL runtime library, without which the #Q stubs have
            # no target and calls through them drop out of the graph.
            # No manifest -> glob the objdir, loudly.
            paths = None
            symp = os.path.join(phaseDir, Path(libPath).stem
                                + '.sym.json')
            try:
                with open(symp) as f:
                    paths = [m['filename']
                             for m in json.load(f)['modules']]
            except (OSError, ValueError, KeyError) as e:
                self.warnings.append(
                    f"autocall stack sizing: no module manifest for "
                    f"MAP phase {ph} ({symp}: {e}); sizing over the "
                    f"FULL objdir (stale .objs may deepen chains)")
                paths = [os.path.join(objdir, fn)
                         for fn in sorted(os.listdir(objdir))
                         if fn.endswith('.obj')]
            # manifest paths are absolute or root-relative; the tree
            # may also have moved since that phase linked
            root = Path(phaseDir).resolve().parents[2]
            for fn in paths:
                if fn.startswith('<'):      # '<map:PHASEnn/NAME>' import
                    continue
                for cand in (fn, str(root / fn),
                             os.path.join(objdir,
                                          os.path.basename(fn))):
                    if os.path.exists(cand):
                        break
                else:
                    self.warnings.append(
                        f"autocall stack sizing: MAP phase {ph} "
                        f"module {fn} not found; its frames default "
                        f"to 36B/depth 0")
                    continue
                key = os.path.realpath(cand)
                if key in sizerSeen:        # linked by two MAP phases
                    continue
                sizerSeen.add(key)
                try:
                    mods, _, _ = LoadModule.load(cand, quiet=True)
                except Exception as e:
                    self.warnings.append(
                        f"autocall stack sizing: unreadable obj "
                        f"{fn} in MAP phase {ph} ({e}); its frames "
                        f"default to 36B/depth 0")
                    continue
                sizerMods.extend(mods)
        sizer = stacksizer.StackSizer(sizerMods, stackSet)
        fallbackHW = self.args.generate_stacks or 0
        added = False

        for symName in wanted:
            sizeHW = sizer.sizeHW(symName)
            source = "chain"
            if sizeHW is None:
                sizeHW, source = fallbackHW, "fallback"
                if sizeHW <= 0:
                    log.warning(f"Stack '{symName}': no program block "
                                f"'${symName[1:]}' in the link and no "
                                f"--generate-stacks fallback; skipped")
            if sizeHW <= 0:
                continue

            sizeBytes = AddrDisp.from_hw(sizeHW)
            module = self._getOrCreateSyntheticModule("<generated-stacks>", 
                                                      "<stacks>")
            self._addSyntheticSection(module, symName, sizeBytes,
                                      fill=STACK_FILL_BYTE)
            added = True
            log.info(f"Generated stack section '{symName}' ({sizeHW} HW / {sizeBytes} bytes, {source})")

        if added:
            self._rebuildSymbolTable()

    def _deferredCsects(self):
        """Csect names a LATER phase segment of the master deck INSERTs.

        The original linkedit was one job over every segment: a later segment
        can position csects of an object that entered the link for an
        earlier phase's INSERT of a sibling section.  A per-phase build must
        leave those sections to the phase that places them -- auto-placing
        them here pollutes this phase's image."""
        root = self.args.concard_root
        if not self.args.concard or '@' not in root:
            return set()
        master, _, ph = root.partition('@')
        if not ph.isdigit():
            return set()
        from ap101Utils import concard
        deck = concard.ConcardDeck(self.args.concard)
        order = [nums[0] for nums, _ in concard.phase_segments(deck, master)
                 if nums]
        phase = int(ph)
        if phase not in order:
            return set()
        own = {op.operand for op in self._concardLayout()
               if op.verb in ('INSERT', 'INCLUDE', 'STACK', 'RESERVE')
               and op.operand}
        deferred = set()
        for p in order[order.index(phase) + 1:]:
            for op in concard.layout_program(deck, f'{master}@{p}'):
                if op.verb == 'INSERT' and op.operand \
                        and op.operand not in own:
                    deferred.add(op.operand)
        return deferred

    def dropDeferredSections(self):
        """Remove sections owned by later phases (see _deferredCsects) from
        the loaded modules; references to them become plain ERs that
        cross-phase resolution patches against the owning phase's lib."""
        deferred = self._deferredCsects()
        if not deferred:
            return
        # Modules this job pulled by AUTOMATIC LIBRARY CALL are exempt:
        # autocall means THIS job resolves the reference with a resident
        # copy, even when a later phase's deck also INSERTs the csect for
        # its own configs.
        autocalled: set[str] = set(self._autocallStems())
        for module in self.modules:
            if module.external:
                continue
            if module.name in autocalled:
                continue
            doomed = [s for s in module.sections.values()
                      if s.name in deferred]
            if not doomed:
                continue
            doomedIds = {s.esdId for s in doomed}
            doomedLds = [ld for ld in module.lds if ld.ldId in doomedIds]
            nameById = {s.esdId: s.name for s in doomed}
            nameById.update({ld.esdId: ld.name for ld in doomedLds})
            for s in doomed:
                del module.sections[s.esdId]
                if module.sectionsByName.get(s.name) is s:
                    del module.sectionsByName[s.name]
            for ld in doomedLds:
                module.lds.remove(ld)
                module.ldsByEsdId.pop(ld.esdId, None)
                if module.sectionsByName.get(ld.name) is ld:
                    del module.sectionsByName[ld.name]
            dropIds = doomedIds | {ld.esdId for ld in doomedLds}
            kept = []
            for r in module.relocations:
                if r.posId in doomedIds:
                    continue           # a site inside dropped content
                kept.append(r)
                if r.relId in dropIds and r.relId not in module.externals:
                    module.addExternal(External(
                        nameById[r.relId], r.relId, False, module))
            module.relocations = kept
            if module.entryPoint and module.entryPoint[0] in dropIds:
                module.entryPoint = None
            log.info(f"{module.name}: deferred "
                     + ", ".join(sorted(s.name for s in doomed))
                     + " to a later phase")

    def generateZconStubs(self):
        """Synthesize Z1-pool ZCON csects for referenced-but-absent modules.

        The LE allocates a pool ZCON csect for EVERY referenced ZCON
        target, even when the owning module is not in the link (per-config
        display/keyboard modules that only app phases load).  The owning
        module's #Z section has an invariant shape -- 4 bytes, text
        00 00 0E 00, one ZCON RLD (flags 0x10) at offset 0 targeting the
        module's own #C<mod> compool -- so it can be synthesized without
        the object.  The #C target stays an unresolved ER here (NCAL) and
        cross-phase resolution patches the pool word against the phase that
        really carries the module.  Names the co-resident base already
        defines are excluded (they resolve cross-phase, no stub)."""
        wanted = [n for n in sorted(self.undefinedSymbols)
                  if n.startswith('#Z') and n not in self.globalSymbols
                  and n not in self.phaseDefinedSymbols]
        if not wanted:
            return False
        module = self._getOrCreateSyntheticModule("<generated-zcons>",
                                                  "<zcons>")
        for symName in wanted:
            section = Section(symName, module.nextEsdId(), 'SD',
                              AddrDisp(0), AddrDisp(4),
                              module, data=bytearray(b'\x00\x00\x0e\x00'))
            module.addSection(section)
            target = '#C' + symName[2:]
            ext = module.externalsByName.get(target)
            if ext is None:
                ext = External(target, module.nextEsdId(), False, module)
                module.addExternal(ext)
            module.relocations.append(
                Relocation(ext.esdId, section.esdId, 0x10, AddrDisp(0),
                           module))
            log.info(f"Generated ZCON pool stub '{symName}' -> {target}")
        self._rebuildSymbolTable()
        return True

    # '@' + this + characteristic name is a stack CSECT's name: @0 for a
    # PROGRAM, @1.. for its TASKs (PROGNAME.xpl's NUMSEQ, continued by
    # ALPHSEQ past ten).  The sequence char selects the 6-halfword PDE slot.
    STACK_SEQUENCE = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'

    def patchStackPDEs(self):
        """Bind each @-stack into its unit's PDE (#E<characteristic>) slot.

        The compiler emits each 6-halfword PDE slot with the stack address
        empty and NO relocation for it (both SDL and NOSDL): words 4-5 =
        0000 000n.  In the real load module the linkage editor fills slot
        k's word 4 with stack CSECT '@' + STACK_SEQUENCE[k] + name's
        halfword address and ORs X'8000' (PREALLOCATED) into word 5 —
        slot k lives at halfword 6*k of the PDE (TERMINAT.xpl's
        LOCCTR(PCEBASE) = (TASK#+1) * 6).

        Runs AFTER applyRelocations and writes the image directly: the
        current CON80 layout leaves some zero-filled sections overlapping
        the PDE region (low-core csects at address 0), so image bytes
        written during the section-copy phase can be wiped by a later
        overlapping copy — words 4-5 are rebuilt here from the PDE's own
        section data, which also restores the compiled 000n tail."""
        if self.image is None:
            return
        patched = 0

        def stackHw(name):
            # A stack linked in (NOSDL, or generateStackSections) is a
            # global symbol; under SDL a single-module relink may know it
            # only from the --external-syms csect table (nothing references
            # a stack, so loadExternalSyms never pulls it in).
            gsym = self.globalSymbols.get(name)
            if gsym is not None:
                addr = gsym.address
                if addr is None and gsym.section is not None:
                    addr = gsym.section.baseAddress
                if addr is not None:
                    return Addr(int(addr)).sector_encode()
            entry = self.csectTable.get(name)
            if isinstance(entry, dict) and 'start' in entry:
                return Addr.from_hw(int(entry['start'])).sector_encode()
            return None

        for pdeName, pde in list(self.globalSymbols.items()):
            if not pdeName.startswith('#E') or pde.section is None \
                    or pde.section.type != 'SD' \
                    or pde.section.baseAddress is None \
                    or (pde.module is not None and pde.module.external):
                continue
            data = pde.section.data
            characteristic = pdeName[2:]
            for k in range(len(data) // 12):
                if k >= len(self.STACK_SEQUENCE):
                    break
                stackName = '@' + self.STACK_SEQUENCE[k] + characteristic
                hw = stackHw(stackName)
                if hw is None:
                    continue
                at = 12 * k
                data[at + 8:at + 10] = hw.to_bytes(2, 'big')
                data[at + 10] |= 0x80
                off = int(pde.section.baseAddress - self.imageBase) + at
                if 0 <= off and off + 12 <= len(self.image):
                    self.image[off + 8:off + 12] = data[at + 8:at + 12]
                    patched += 1
                    # Record the bind as an applied relocation (2-byte
                    # YCON-like, LL=2 -> flags 0x04) so sym.json consumers
                    # (fidelity comparators, debuggers) see PDE word 4 as a
                    # relocated site rather than opaque content.
                    self.appliedRelocations.append({
                        "address": int(pde.section.baseAddress) // 2
                                   + 6 * k + 4,
                        "target": hw,
                        "targetName": stackName,
                        "flags": 0x04,
                    })
        if patched:
            log.info(f"Patched {patched} PDE slot(s) with preallocated "
                     f"stack addresses")

    def processDefinedSymbols(self):
        # handle -D symbols
        # we'll drop them into a single synthetic CSECT
        if not self.args.define:
            return

        added = False
        for defn in self.args.define:
            if '=' not in defn:
                self.warnings.append(f"Invalid --define format: {defn}")
                continue
            symName, valueStr = defn.split('=', 1)
            try:
                value = int(valueStr, 0)
            except ValueError:
                self.warnings.append(f"Invalid value in --define: {defn}")
                continue

            module = self._getOrCreateSyntheticModule("<defined-symbols>", "<defined>")
            self._addSyntheticSection(module, symName, AddrDisp(0),
                                      baseAddress=Addr(value))
            added = True
            log.info(f"Defined symbol '{symName}' = 0x{value:06X}")

        if added:
            self._rebuildSymbolTable()

    def loadMapLibs(self):
        """Resolve the deck's MAP <phase>,<member,...> cards against earlier-
        phase .lib load modules (--map-lib N=path).

        For each mapped member, the named REGN interval(s) of the phase's
        load module are imported as an address-only (external) LoadModule:
        every CESD SD/LD inside the interval becomes a resolvable symbol at
        its already-loaded address, and the interval is reserved so auto-
        placement flows around it."""
        from ap101Utils.libModule import LibModule
        from ap101Utils.objModule import EsdType

        libs = {}
        for spec in self.args.map_lib:
            phase, _, path = spec.partition('=')
            if not phase.isdigit() or not path:
                self.errors.append(f"--map-lib expects N=path, got {spec!r}")
                continue
            try:
                libs[int(phase)] = LibModule.read(path)
            except (OSError, ValueError) as e:
                self.errors.append(f"--map-lib {spec}: {e}")

        # Region origins from every supplied phase lib (lowest phase wins on
        # a name collision), for OVERLAY re-opens -- including regions the
        # deck deliberately does NOT MAP because it replaces their content.
        for p in sorted(libs):
            for r in libs[p].regions:
                self.phaseRegionStarts.setdefault(r.name, r.start)

        # Definitions of the CO-RESIDENT base phases, to suppress duplicate
        # autocall pulls (see phaseDefinedSymbols in __init__).  Only phases
        # this deck MAPs count: a MAP card declares the phase as part of this
        # phase's runtime environment (loaded together in every config that
        # loads this phase), so its whole symbol table was visible to the
        # config link's autocall.  Other supplied libs are sibling app
        # phases -- alternatives that are NEVER co-resident (the config link
        # never saw them), so their definitions must not suppress this
        # phase's own library pulls (e.g. PHASE06 needs its own #QACOS even
        # though PHASE04 also pulls one; each config gets its own pool copy
        # at a per-config address).
        mapPhases = self._deckMapPhases()
        # ... plus the deck-CHAIN phases: segments whose PHASE card lists
        # this phase carry content that is in THIS phase's job too (OFTMP
        # "PHASE 2,3,...,18" -> the phase-2 segment is part of every one of
        # those phases' one-job links), so their state flows here even with
        # no MAP card at all (e.g. PHASE13's deck has none).  Sibling app
        # phases list only themselves and stay excluded.
        selfPhases = self._deckPhaseNumbers()
        selfPhase = min(selfPhases) if selfPhases else None
        chainPhases = {p for p in libs
                       if selfPhase is not None and p != selfPhase
                       and selfPhase in libs[p].phaseNumbers}
        if chainPhases - mapPhases:
            log.info(f"one-job chain phases (no MAP card): "
                     f"{sorted(chainPhases - mapPhases)}")
        for p in sorted((mapPhases | chainPhases) & set(libs)):
            self.phaseDefinedZconNext = max(self.phaseDefinedZconNext,
                                            self._carriedZconNext(libs[p]))
            for e in libs[p].cesd:
                if e.type in (EsdType.SD, EsdType.LD):
                    self.phaseDefinedSymbols.add(e.name.strip())
                    if e.type == EsdType.SD and e.address is not None:
                        name = e.name.strip()
                        self.phaseDefinedAddrs.setdefault(name, e.address)
                        self.phaseDefinedSizes.setdefault(name, e.length or 0)
                        self.phaseDefinedExtents.append(
                            (e.address, e.address + (e.length or 0)))

        for op in self._concardLayout():
            if op.verb != 'MAP':
                continue
            toks = op.operand.split(',')
            if not toks or not toks[0].isdigit():
                self.warnings.append(f"MAP with unparsable operand "
                                     f"{op.operand!r} ({op.card})")
                continue
            phase, members = int(toks[0]), [t for t in toks[1:] if t]
            lib = libs.get(phase)
            if lib is None:
                self.warnings.append(
                    f"MAP {op.operand}: no --map-lib for phase {phase}; "
                    f"symbols stay unresolved")
                continue
            for member in members:
                if member == '*':
                    # `MAP p,*` (the deck's "MAPALL" idiom): map the WHOLE
                    # phase -- every content extent its load module placed,
                    # not just named OVERLAY regions (autocalled content
                    # lands outside any REGN).  Without this the phase's
                    # occupancy is invisible and new overlays land on top
                    # of its content.
                    intervals = lib.occupiedIntervals()
                else:
                    intervals = [(r.start, r.end) for r in lib.regions
                                 if r.name == member]
                if not intervals:
                    self.warnings.append(
                        f"MAP {phase},{member}: no such overlay region in "
                        f"{lib.name}")
                    continue

                module = LoadModule(f"<map:{lib.name}/{member}>",
                                    name=f"{lib.name}/{member}",
                                    external=True)
                inside = lambda a: a is not None and any(
                    lo <= a < hi for lo, hi in intervals)
                nSyms = 0
                # `address` (the module-relative field) is set to the lib's
                # absolute byte address on BOTH the SDs and the LDs so that
                # _resolveLDLabels' recomputation (owner.baseAddress +
                # (ld.address - owner.address)) is exact.  Leaving it at the
                # default 0 collapses every imported LD onto its owner SD's
                # base, corrupting every link-time cross-phase LD reference.
                for e in lib.cesd:
                    if e.type == EsdType.SD and inside(e.address):
                        module.addSection(Section(
                            e.name.strip(), e.esdId, 'SD',
                            address=AddrDisp(e.address),
                            length=AddrDisp(e.length or 0), module=module,
                            baseAddress=Addr(e.address)))
                        nSyms += 1
                    elif e.type == EsdType.LD and inside(e.address):
                        module.addSection(Section(
                            e.name.strip(), e.esdId, 'LD', module=module,
                            address=AddrDisp(e.address),
                            baseAddress=Addr(e.address), ldId=e.ldid))
                        nSyms += 1
                self.modules.append(module)
                # Each base content run carries a trailing 2-hw checksum
                # slot the LE wrote for the protected block; a later
                # phase's flow placement continues AFTER it (same rule as
                # the Z1 pool cursor and conlayout's base_occupied ck+2).
                self.mappedIntervals.extend(
                    (lo, hi + 4) for lo, hi in intervals)
                log.info(f"MAP {phase},{member}: {nSyms} symbol(s), "
                         + ", ".join(f"0x{lo:06X}..0x{hi:06X}"
                                     for lo, hi in intervals))

    def dropMapDuplicateCsects(self):
        """Delete a csect the MAP-imported base phase already defines.

        The LE keeps the FIRST definition of a csect name and deletes the
        later one."""
        imported = {s.name: m for m in self.modules if m.external
                    for s in m.sections.values() if s.type == 'SD'}
        if not imported:
            return
        for mod in list(self.modules):
            if mod.external:
                continue
            deleted = False
            for esdId in [i for i, s in mod.sections.items()
                          if s.type == 'SD' and s.name in imported]:
                dup = mod.sections[esdId]
                winner = imported[dup.name]
                lds = [l for l in mod.lds if l.ldId == esdId]
                orphans = [l.name for l in lds
                           if l.name not in winner.sectionsByName]
                if orphans:
                    self.warnings.append(
                        f"duplicate csect {dup.name} from "
                        f"{Path(mod.filename).name} KEPT: the MAP import "
                        f"{winner.name} does not carry {', '.join(orphans)}")
                    continue
                for ld in lds:
                    mod.lds.remove(ld)
                    mod.ldsByEsdId.pop(ld.esdId, None)
                    if mod.sectionsByName.get(ld.name) is ld:
                        del mod.sectionsByName[ld.name]
                del mod.sections[esdId]
                if mod.sectionsByName.get(dup.name) is dup:
                    del mod.sectionsByName[dup.name]
                mod.relocations = [r for r in mod.relocations
                                   if r.posId != esdId]
                deleted = True
                log.info(f"duplicate csect {dup.name} from "
                         f"{Path(mod.filename).name} deleted: already "
                         f"defined by MAP import {winner.name}")
            if deleted and not mod.sections and not mod.lds:
                self.modules.remove(mod)
                log.info(f"module {Path(mod.filename).name} left empty by "
                         f"duplicate-csect deletion; dropped from the link")

    @staticmethod
    def _carriedZconNext(lib):
        """The Z1 pool cursor a MAP-carded parent phase carries forward.
        Prefer the lib's serialized linkState (the exact one-job LE state);
        a legacy lib written before the LSTA record falls back to a
        reconstruction from its CESD (pool end + the overlay's closing 2-hw
        checksum slot)."""
        nxt = (lib.linkState or {}).get('zconPoolNext')
        if nxt is not None:
            return int(nxt)
        from ap101Utils.objModule import EsdType as _ET
        ends = [e.address + (e.length or 4) for e in lib.cesd
                if e.type == _ET.SD and e.address is not None
                and e.address < int(ZCON_END)
                and any(e.name.strip().startswith(p) for p, z
                        in _ZONE_BY_PREFIX if z == 'ZCON')]
        return max(ends) + 4 if ends else 0

    def loadExternalSyms(self, path):
        """
        Load a JSON containing csect locations and symbol offsets,
        which we can use to perform relocations without loading the
        actual object modules.

        The json should be a dictionary mapping CSECT names to a struct
        describing the CSECT and its internal symbols:

         "FCMTRCLG": {
            "start": 117148,
            "end": 117547,
            "type": "NONHAL",
            "contents": {
                "FCMBEGTL": 0,
                "FCMENDTL": 400
            }
        },

        Addresses and offsets are in halfwords; both decimal and hex
        numbers are accepted.

        We don't use it at the moment, but known CSECT types are:

            BCE             | IOP code
            MSC             | IOP code
            DATA
            EXCLUSIVE
            FUNCTION
            HAL_LIBRARY_CODE
            HAL_LIBRARY_DATA
            HAL_LIBRARY_ZCON
            NONHAL
            PATCH
            PDE
            PROCEDURE
            PROGRAM
            STACK
            ZCON
        """
        with open(path) as f:
            csectTable = json.load(f)
        self.csectTable = csectTable

        module = self._getOrCreateSyntheticModule("<external-syms>", "<ext-syms>")
        module.external = True
        added = False

        ldToParent = {}
        for csectName, csectEntry in csectTable.items():
            if isinstance(csectEntry, dict) and 'contents' in csectEntry:
                for ldName in csectEntry['contents']:
                    ldToParent[ldName] = csectName

        loadedCsects = set()

        for symName in list(self.undefinedSymbols):
            entry = csectTable.get(symName)
            if entry is None or 'start' not in entry:
                parentName = ldToParent.get(symName)
                if parentName and parentName not in loadedCsects:
                    entry = csectTable[parentName]
                    symName = parentName
                else:
                    continue
            if symName in loadedCsects:
                continue
            loadedCsects.add(symName)

            startHW = entry['start']
            endHW = entry.get('end', startHW)
            baseAddr = Addr.from_hw(startHW)
            lengthBytes = AddrDisp.from_hw(endHW - startHW + 1)

            section = self._addSyntheticSection(
                module, symName, lengthBytes, baseAddress=baseAddr)
            added = True
            log.info(f"External sym '{symName}' @ {baseAddr}  len={lengthBytes}")

            # "linkInfo": "placement": the entry supplies an address and no
            # linkage.  A CSECT table carries a section the configuration
            # does not load when a ZCON in this configuration points at a
            # module loaded in another; the ZCON needs the address the code
            # has there.  The section is placed and its contents are
            # defined, so the symbol table the AP-101S emulators read is
            # complete.  The contents resolve no relocation: no module in
            # this configuration supplied them, and the original link left
            # those sites as assembled.  In OI340600's GNC9, FIOPDSPG's
            # `#LBR TFCMPFD1` and `#LBR TFCMPFD2` name two fields of
            # #DDPLLIG, whose overlay DPLLIGHT is absent from that
            # configuration's memory map, and the flight image holds 0000 at
            # both sites.
            #
            # The section name resolves; the ZCON case names the section.  A
            # HAL object references a compool by section name with the field
            # offset in its text, so the mark reaches assembler externals
            # that name a field directly.  A field the runtime library or
            # any linked module defines is resolved before the table is
            # read and is never marked.
            contents = entry.get('contents')
            if contents:
                if entry.get('linkInfo') == 'placement':
                    self.placementOnlySymbols.update(contents)
                for ldName, ldVal in contents.items():
                    if isinstance(ldVal, dict):
                        offsetHW = ldVal.get('offset', 0)
                    else:
                        offsetHW = ldVal
                    offsetAddr = AddrDisp.from_hw(offsetHW)
                    ld = Section(ldName, module.nextEsdId(), 'LD',
                                 address=offsetAddr,
                                 module=module,
                                 baseAddress=baseAddr + offsetAddr,
                                 ldId=section.esdId)
                    module.addSection(ld)

        # Pre-assign addresses for locally-defined sections found in the JSON
        for module in self.modules:
            if module.external:
                continue
            for section in module.sections.values():
                if section.type != 'SD' or section.baseAddress is not None:
                    continue
                entry = csectTable.get(section.name)
                if entry is not None and 'start' in entry:
                    section.baseAddress = Addr.from_hw(entry['start'])
                    log.info(f"Placed '{section.name}' @ {section.baseAddress} (from external-syms)")

        if added:
            self._rebuildSymbolTable()
        return added

    def link(self):

        if self.nocall:
            log.info("NOCALLER (NCAL): unresolved externals are tolerated")

        # LE CHANGE cards rename symbols in their INCLUDEd modules; must run
        # before any symbol table / placement sees the old names.
        if self.args.concard:
            self.applyConcardChanges()

        # Sections a LATER phase segment places stay out of this link
        # entirely (their refs become cross-phase ERs).
        self.dropDeferredSections()

        # Process -D defined symbols first
        log.info("Processing defined symbols...")
        self.processDefinedSymbols()

        # MAP <phase>,<member...> cards resolve against earlier-phase .lib
        # load modules: import the mapped overlay regions' symbols (address-
        # only) and reserve their intervals before anything is placed.
        if getattr(self.args, 'map_lib', None):
            log.info("Loading MAP phase libraries...")
            self.loadMapLibs()
            self.dropMapDuplicateCsects()

        log.info("Building global symbol table...")
        self._rebuildSymbolTable()
        
        log.info("Resolving external references...")
        allResolved = self.resolveExternals(searchLibraries=bool(self.args.library_path))

        # Load external symbol table — needed both for resolving remaining
        # undefined symbols AND for placing loaded sections (including library
        # modules) at their correct addresses in the memory map.
        if self.args.external_syms:
            log.info("Loading external symbol table...")
            if self.loadExternalSyms(self.args.external_syms):
                allResolved = self.resolveExternals(searchLibraries=False)

        # Generate the @-stack CSECTs: named by CON80 STACK cards (the only
        # trigger under SDL, where objects carry no @ ERs) and by undefined
        # @xxx ERs (NOSDL); sized by the call-chain algorithm.
        hasKnownSizes = any(m.stackSizes for m in self.modules)
        if self.stackCsectNames() and (hasKnownSizes or self.args.generate_stacks):
            log.info("Generating stack sections...")
            self.generateStackSections()
            allResolved = self.resolveExternals(searchLibraries=False)

        # Z1-pool stubs for referenced ZCON targets whose owning module is
        # not in this link (see generateZconStubs) -- only meaningful when a
        # CON80 deck drives placement (the Z1 pool).
        if self.args.concard and self.generateZconStubs():
            allResolved = self.resolveExternals(searchLibraries=False)

        if not allResolved:
            # The HAL/S-FC compiler can emit references to COMPOOLs that are
            # not in the link when the referencing code can never execute in
            # the linked configuration (issue #22).  Make missing compools
            # warnings unless --strict-compools is passed.
            strict = []
            lenient = []
            nocalled = []
            for sym in sorted(self.undefinedSymbols):
                if sym.startswith('#P') and not self.args.strict_compools:
                    lenient.append(sym)
                elif self.nocall:
                    nocalled.append(sym)
                else:
                    strict.append(sym)

            # Tolerated undefineds are cross-phase references (the real OFTMP
            # linkedit resolved them across overlay segments; ours defers to
            # the phaseresolve pass), so a per-symbol warning wall is noise
            # by default -- -Wunresolved-phases restores it.
            perSym = log.warning if getattr(
                self.args, 'warn_unresolved_phases', False) else log.info
            for sym in lenient:
                perSym(
                    f"Undefined COMPOOL: {sym}, referenced by "
                    f"{self._formatUndefRefs(self.undefinedSymbols[sym])}")
            for sym in nocalled:
                perSym(
                    f"Undefined symbol (left unresolved, NOCALLER): {sym}, "
                    f"referenced by "
                    f"{self._formatUndefRefs(self.undefinedSymbols[sym])}")
            if (lenient or nocalled) and perSym is log.info:
                log.info(f"{len(lenient) + len(nocalled)} undefined "
                         f"symbol(s) left for cross-phase resolution "
                         f"(-Wunresolved-phases lists them)")
            for sym in strict:
                self.errors.append(
                    f"Undefined symbol: {sym}, referenced by "
                    f"{self._formatUndefRefs(self.undefinedSymbols[sym])}")

            if strict and not self.args.force:
                return False
            elif strict:
                log.warning(f"{len(strict)} undefined symbol(s), continuing due to -f")
        
        #
        # Place Sections
        #
        if self.args.concard:
            log.info("Assigning addresses from CON80 layout...")
            self.assignAddressesFromConcard()
        elif self.args.load_config:
            log.info("Loading link config...")
            baseConfig = self.generateConfig(includeAddresses=False)
            overlayConfig = LinkConfig.load(self.args.load_config)
            try:
                mergedConfig = baseConfig.merge(overlayConfig)
            except ValueError as e:
                error(str(e))
            configErrors = mergedConfig.validate()
            if configErrors:
                for e in configErrors:
                    self.errors.append(f"Config: {e}")
                if not self.args.force:
                    return False
                else:
                    log.warning(f"{len(configErrors)} config error(s), continuing due to -f")
            log.info("Assigning addresses from config...")
            self.assignAddressesFromConfig(mergedConfig)
        else:
            log.info("Assigning addresses...")
            self.assignAddresses()

        if self.args.save_config:
            log.info("Saving link config...")
            config = self.generateConfig()
            config.save(self.args.save_config)
            log.info(f"Wrote link config to {self.args.save_config}")

        #
        # Apply Relocations
        #
        log.info("Applying relocations...")
        success = self.applyRelocations()
        self.patchStackPDEs()

        self.determineEntryPoint()

        return success or self.args.force
    
    def saveImage(self, outputPath):
        if self.image is None:
            error("No image to save")
            return
        
        with open(outputPath, 'wb') as f:
            f.write(self.image)

        log.info(f"Wrote {len(self.image)} bytes to {outputPath}")

    def saveLib(self, outputPath):
        """Write the link result as an AP-101 loadable module (.lib) -- the
        load-module boundary the real SDL flow stored in a library for the
        MMU build (MAFGEN's LM=...).  Carries the CESD (resolved symbols),
        per-extent text, the retained RLD, per-halfword store-protection,
        CON80 overlay regions, and phase metadata.  See ap101Utils.libModule
        for the record format."""

        from ap101Utils import libModule

        if self.image is None:
            error("No image to write to .lib")
            return

        from .repro import created_stamp
        lib = libModule.LibModule(
            name=Path(outputPath).stem[:8].upper(),
            linkerId=f"LNK101 {_version_string()}"[:16],
            created=created_stamp())

        # -- CESD: placed SDs (address order), then LDs, then unresolved ERs.
        placedSDs = sorted(self._placedSDs(), key=lambda s: int(s.baseAddress))
        ids = {}
        for s in placedSDs:
            name = s.name.strip()
            if name in ids:
                continue
            ids[name] = len(lib.cesd) + 1
            lib.cesd.append(libModule.EsdEntry.sd(
                ids[name], name, int(s.baseAddress), int(s.length)))
        for m in self.modules:
            if m.external:
                continue
            for ld in m.lds:
                name = ld.name.strip()
                if ld.baseAddress is None or name in ids:
                    continue
                owner = self._findLDOwner(ld, m)
                ownerId = ids.get(owner.name.strip(), 1) if owner else 1
                ids[name] = len(lib.cesd) + 1
                lib.cesd.append(libModule.EsdEntry.ld(
                    ids[name], name, int(ld.baseAddress), ownerId))
        for name in sorted(self.undefinedSymbols):
            if name not in ids:
                ids[name] = len(lib.cesd) + 1
                lib.cesd.append(libModule.EsdEntry.er(ids[name], name))

        # -- TEXT extents: contiguous runs of placed sections; text comes
        # from the relocated image.  A run ENDS at any gap, including the
        # 1-hw pad after an odd-length csect.
        base = self.imageBase
        run, runs, runEnd = [], [], 0
        for s in (s for s in placedSDs if s.length > 0
                  and s.name.strip() not in self.reservedCsects):
            if run and int(s.baseAddress) > runEnd:
                runs.append((run, runEnd))
                run, runEnd = [], 0
            run.append(s)
            runEnd = max(runEnd, int(s.baseAddress + s.length))
        if run:
            runs.append((run, runEnd))
        protRanges = self.storeProtectRangesHw()
        for run, end in runs:
            start = int(run[0].baseAddress)
            nBits = (end - start + 1) // 2
            startHw = start // 2
            bits = [False] * nBits
            for a, b in protRanges:
                for i in range(max(a, startHw) - startHw,
                               min(b, startHw + nBits) - startHw):
                    bits[i] = True
            lib.extents.append(libModule.Extent(
                start, bytes(self.image[start - base:end - base]), bits))

        # -- RLD: retained relocations, positions rebased to absolute bytes.
        for m in self.modules:
            if m.external:
                continue
            for r in m.relocations:
                pos = m.sections.get(r.posId)
                if pos is None or pos.baseAddress is None:
                    continue
                target = m.sections.get(r.relId) or m.externals.get(r.relId) \
                    or m.ldsByEsdId.get(r.relId)
                tname = ""
                if target is not None:
                    resolved = getattr(target, 'resolvedSection', None)
                    tname = (resolved.name if resolved is not None
                             else target.name).strip()
                lib.rlds.append(libModule.LibRld(
                    r.flags, int(pos.baseAddress) + int(r.address),
                    ids.get(tname, 0), ids.get(pos.name.strip(), 0)))

        # -- CON80 overlay regions and phase metadata.
        placement = self._concardPlacement
        if placement is not None:
            # One region record per CONTIGUOUS run of an overlay's csects
            # (tolerating fullword pads and the 2-hw inter-block checksums).
            # A min/max span per overlay is WRONG: the roll-in pool scatters
            # an overlay's members across the bank's free gaps, and a
            # min-max span covering the gaps makes a later phase's MAP of
            # the region reserve unrelated content, pushing its re-open
            # content away.  The MAP reader already accepts multiple
            # intervals per region name.
            extents = {}
            for s in placedSDs:
                ov = placement.overlay_of.get(s.name.strip())
                if not ov:
                    continue
                lo = int(s.baseAddress)
                extents.setdefault(ov, []).append((lo, lo + int(s.length)))
            recs = []
            for ov, spans in extents.items():
                spans.sort()
                run_lo, run_hi = spans[0]
                for lo, hi in spans[1:]:
                    if lo - run_hi <= 8:            # pad / checksum slack
                        run_hi = max(run_hi, hi)
                    else:
                        recs.append(libModule.Region(
                            ov, run_lo, run_hi, run_lo // int(SECTOR_SIZE)))
                        run_lo, run_hi = lo, hi
                recs.append(libModule.Region(
                    ov, run_lo, run_hi, run_lo // int(SECTOR_SIZE)))
            lib.regions = sorted(recs, key=lambda r: r.start)
        if self.args.concard:
            lib.phaseRoot = self.args.concard_root
        lib.phaseNumbers = self._deckPhaseNumbers()
        # -- LE carried state: the one-job allocation cursor at the end of
        # this phase's deck segment, cumulative over the MAP-carded parents'
        # carried state, so the next phase's link CONTINUES it exactly.
        ownPool = [int(s.baseAddress) + int(s.length) for s in placedSDs
                   if s.zone == 'ZCON' and int(s.baseAddress) < int(ZCON_END)]
        zconNext = self.phaseDefinedZconNext
        if ownPool:
            # A segment that allocates in Z1 AT ALL closes its contribution
            # with the overlay's 2-hw checksum slot -- even when it grew the
            # pool by nothing.
            zconNext = max(zconNext, max(ownPool)) + 4
        if zconNext:
            lib.linkState['zconPoolNext'] = zconNext

        lib.nocall = self.nocall
        lib.entryAddress = int(self.entryPoint) \
            if self.entryPoint is not None else None
        lib.entryName = self.args.entry or next(
            (m.entryName for m in self.modules if m.entryName), "") or ""

        lib.write(outputPath)
        nProt = sum(sum(x.protect) for x in lib.extents if x.protect)
        log.info(f"Wrote load module {outputPath}: {len(lib.cesd)} CESD, "
                 f"{len(lib.extents)} extent(s), {nProt} hw protected")
    
    @staticmethod
    def _formatUndefRefs(refs, use_basename=False):
        """Format undefined symbol references grouped by module.
        refs is a set of (filename, csect_or_None) tuples."""
        by_file = {}
        for filename, csect in refs:
            by_file.setdefault(filename, set())
            if csect:
                by_file[filename].add(csect)
        parts = []
        for filename in sorted(by_file):
            name = Path(filename).name if use_basename else filename
            csects = sorted(by_file[filename])
            if csects:
                parts.append(f"{name}({', '.join(csects)})")
            else:
                parts.append(name)
        return ', '.join(parts)

    def saveListing(self, outputPath):
        """Generate a listing file showing the linked memory layout."""
        
        lines = []
        lines.append(f"LNK101 {version} - AP-101 Linker")
        lines.append(f"Output: {outputPath}")
        lines.append("=" * 70)
        lines.append("")
        
        # Section map
        lines.append("SECTION MAP")
        lines.append("-" * 70)
        lines.append(f"{'Address':>8}  {'Length':>8}  {'Name':<16}  {'Module':<30}")
        lines.append("-" * 70)
        
        totalLength = AddrDisp(0)
        for module in self.modules:
            for section in module.sections.values():
                if section.type != 'SD' or section.baseAddress is None:
                    continue
                baseHw = section.baseAddress.hw
                lengthHw = section.length.hw
                lines.append(f"{baseHw:08X}  {lengthHw:8d}  {section.name:<16}  "
                           f"{Path(module.filename).name:<30}")
                totalLength += section.length
        
        lines.append("-" * 70)
        lines.append(f"Total: {totalLength} bytes ({totalLength.hw} halfwords)")
        lines.append("")
        
        # Symbol table
        lines.append("GLOBAL SYMBOLS (halfword addresses)")
        lines.append("-" * 70)
        
        col = 0
        symLine = ""
        for name in sorted(self.globalSymbols.keys()):
            section, module, addr = self.globalSymbols[name]
            if addr is not None:
                symLine += f"{name:<10} {addr.x}   "
                col += 1
                if col >= 4:
                    lines.append(symLine)
                    symLine = ""
                    col = 0
        
        if symLine:
            lines.append(symLine)
        
        lines.append("")
        
        # Undefined symbols
        if self.undefinedSymbols:
            lines.append("UNDEFINED SYMBOLS")
            lines.append("-" * 70)
            for sym in sorted(self.undefinedSymbols):
                lines.append(f"  {sym:<16}  referenced by: "
                             f"{self._formatUndefRefs(self.undefinedSymbols[sym], use_basename=True)}")
            lines.append("")
        
        # Entry point
        if self.entryPoint is not None:
            lines.append(f"Entry Point: {self.entryPoint.x}")
        
        lines.append("")
        
        # Warnings
        if self.warnings:
            lines.append("WARNINGS")
            lines.append("-" * 70)
            for w in self.warnings:
                lines.append(f"  {w}")
            lines.append("")
        
        # Hex dump
        if self.args.dump and self.image:
            lines.append("MEMORY DUMP (first 1024 bytes)")
            lines.append("-" * 70)
            dumpLen = min(len(self.image), 1024)
            for i in range(0, dumpLen, 16):
                chunk = self.image[i : i + 16]
                hexPart = ' '.join(f'{b:02X}' for b in chunk).ljust(48)
                asciiPart = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
                lines.append(f"{i:06X}: {hexPart} |{asciiPart}|")
            lines.append("")
        
        with open(outputPath, 'w') as f:
            f.write('\n'.join(lines))
        
        log.info(f"Wrote listing to {outputPath}")
    
    def _loadDebugSymbols(self):
        """Load the per-module `<obj>.asmg.json` debug sidecars emitted by
        asm101 and resolve their LOCAL labels to final linked addresses.
        """
        parsed = {} 
        
        def load(fn):
            if fn in parsed:
                return parsed[fn]
            p = Path(fn).with_name(Path(fn).stem + ".asmg.json")
            d = None
            if p.exists():
                try:
                    d = json.loads(p.read_text())
                except (OSError, ValueError) as e:
                    self.warnings.append(f"Could not read debug symbols {p}: {e}")
            parsed[fn] = d
            return d

        localSymbols = []
        kindByName = {}
        seen = set()
        for module in self.modules:
            if not module.filename:
                continue
            asmg = load(module.filename)
            if not asmg:
                continue
            modName = Path(module.filename).stem
            for sym in asmg.get("symbols", []):
                name = sym.get("name")
                kind = sym.get("kind")
                if name and kind:
                    kindByName.setdefault(name, kind)
                # Globals already come from the linker's own ESD-based table;
                # only fold in the locals (and only those with an address).
                if sym.get("scope") != "local":
                    continue
                sectName = sym.get("section")
                offset = sym.get("offset")
                if sectName is None or offset is None:
                    continue  # absolute EQU and the like -- nothing to place
                section = module.sectionsByName.get(sectName)
                if section is None or section.baseAddress is None:
                    continue
                addrHW = section.baseAddress.hw + offset  # base is hw + hw offset
                key = (modName, name, addrHW)
                if key in seen:
                    continue
                seen.add(key)
                localSymbols.append({
                    "name": name,
                    "address": addrHW,
                    "type": "local",
                    "kind": kind,
                    "scope": "local",
                    "section": sectName,
                    "module": modName,
                })
        return localSymbols, kindByName

    def saveJsonSymbols(self, outputPath):
        # gen .sym.json
        assert self.entryPoint is not None
        data = {
            "version": version,
            "imageSize": self.imageSize.hw,  # halfwords
            "entryPoint": self.entryPoint.hw,
            "sections": [],
            "symbols": [],
            "modules": [],
            "storeProtect": {
                "unit": "halfword",
                "ranges": self.storeProtectRangesHw(),
            },
        }
        
        # Collect SD CSECTs sorted by address
        for module in self.modules:
            modInfo = {
                "name": Path(module.filename).stem if module.filename else module.name,
                "filename": module.filename
            }
            data["modules"].append(modInfo)
            
            for section in module.sections.values():
                if section.type == 'SD' and section.baseAddress is not None:
                    ent = {
                        "name": section.name,
                        "address": section.baseAddress.hw,
                        "size": section.length.hw,
                        "module": modInfo["name"]
                    }
                    if section.origName is not None:
                        ent["origName"] = section.origName
                    data["sections"].append(ent)
        
        # Sort sections by address
        data["sections"].sort(key=lambda x: x["address"])

        localSymbols, kindByName = self._loadDebugSymbols()

        # Global symbols with resolved addresses
        for name in sorted(self.globalSymbols.keys()):
            section, module, byteAddr = self.globalSymbols[name]
            if byteAddr is not None:
                symType = "entry" if section.type == 'LD' else "section"
                # For LDs, resolve the parent SD name via ldId
                parentName = None
                if section.type == 'LD' and section.ldId is not None:
                    parent = module.sections.get(section.ldId)
                    if parent:
                        parentName = parent.name

                if symType == "section":
                    kind = "section"
                else:  # LD entry
                    kind = kindByName.get(name, "code")

                data["symbols"].append({
                    "name": name,
                    "address": byteAddr.hw,
                    "type": symType,
                    "kind": kind,
                    "scope": "global",
                    "section": parentName,
                    "module": Path(module.filename).stem if module.filename else module.name
                })

        # Fold in the local labels
        globalNames = {s["name"] for s in data["symbols"]}
        for sym in localSymbols:
            if sym["name"] not in globalNames:
                data["symbols"].append(sym)

        # Sort symbols by address
        data["symbols"].sort(key=lambda x: x["address"])

        # Build address map for resolving relocation targets to symbols.
        addrMap = AddressMap()
        addrMap.add_global_symbols(self.globalSymbols)
        addrMap.add_csect_table(self.csectTable)

        data["relocations"] = []
        for rec in sorted(self.appliedRelocations, key=lambda r: r["address"]):
            # For positive relocations, resolve the target value to a symbol.
            # For negative (sign bit set), the value is a displacement, not an address.
            if rec["flags"] & 0x80:
                sym = rec["targetName"]
            else:
                sym = addrMap.format(rec["target"]) or rec["targetName"]
            data["relocations"].append({
                "address": rec["address"],
                "target": rec["target"],
                "targetName": rec["targetName"],
                "flags": rec["flags"],
                "symbol": sym,
            })

        if self.unresolvedRelocations:
            data["unresolvedRelocations"] = sorted(
                self.unresolvedRelocations, key=lambda r: r["imageOffset"])

        with open(outputPath, 'w') as f:
            json.dump(data, f, indent=2)

        log.info(f"Wrote symbol table to {outputPath}")

    def saveExternalSyms(self, outputPath):
        """Save csect address table for use with --external-syms.

        Produces JSON mapping section names to halfword addresses::

            { "CSECT_NAME": { "start": <hw>, "end": <hw> }, ... }

        Includes all SD sections and LD (entry) labels.
        """
        data = {}

        for module in self.modules:
            for section in module.allSections():
                if section.baseAddress is None:
                    continue
                if section.type == 'SD':
                    startHW = section.baseAddress.hw
                    endHW = startHW + section.length.hw - 1
                    data[section.name] = {
                        "start": startHW,
                        "end": max(startHW, endHW),
                    }
                elif section.type == 'LD':
                    hw = section.baseAddress.hw
                    data[section.name] = {
                        "start": hw,
                        "end": hw,
                    }

        with open(outputPath, 'w') as f:
            json.dump(data, f, indent=2)

        log.info(f"Wrote external syms ({len(data)} entries) to {outputPath}")

    def printSectionTable(self):
        # Collect all sections with assigned addresses
        allSections = []
        for module in self.modules:
            for section in module.allSections():
                if section.baseAddress is not None:
                    allSections.append((section, module))
        
        allSections.sort(key=lambda x: x[0].baseAddress)
        
        if not allSections:
            print("\nNo sections with assigned addresses.")
            return
        
        print("\n" + "=" * 90)
        print("SECTION TABLE (sorted by address)")
        print("=" * 90)
        print(f"{'Address':>10}  {'HW Addr':>8}  {'Size':>8}  {'HW Size':>7}  {'Type':<4}  {'Name':<12}  {'Module'}")
        print("-" * 90)
        
        totalBytes = AddrDisp(0)
        prevEnd = self.imageBase
        
        for section, module in allSections:
            baseAddr = section.baseAddress
            hwAddr = baseAddr.hw
            length = section.length
            hwLength = length.hw
            stype = section.type
            name = section.name[:12] if section.name else "<unnamed>"
            modName = Path(module.filename).name if module.filename else module.name
            
            # Show gap if there's unused space between sections
            if baseAddr > prevEnd and stype == 'SD':
                gap = baseAddr - prevEnd
                print(f"{'':>10}  {'':>8}  {gap:>8}  {gap.hw:>7}  {'gap':<4}  {'<unused>':<12}  ")
            
            if stype == 'SD':
                print(f"0x{baseAddr:08X}  {hwAddr:>8X}  {length:>8}  {hwLength:>7}  {stype:<4}  {name:<12}  {modName}")
                totalBytes += length
                prevEnd = baseAddr + length
            else:
                # LD (label) - show as entry point within a section
                print(f"0x{baseAddr:08X}  {hwAddr:>8X}  {'':>8}  {'':>7}  {stype:<4}  {name:<12}  {modName}")
        
        print("-" * 90)
        print(f"{'Total:':<10}  {'':>8}  {totalBytes:>8}  {totalBytes.hw:>7}")
        
        if self.entryPoint is not None:
            print(f"\nEntry Point: {self.entryPoint.x}")
        
        print("=" * 90)
    
    def printSummary(self):
        print(f"\nLink Summary:")
        print(f"  Modules:     {len(self.modules)}")
        print(f"  Symbols:     {len(self.globalSymbols)}")
        print(f"  Image size:  {self.imageSize} bytes ({self.imageSize.hw} halfwords)")
        
        if self.entryPoint is not None:
            print(f"  Entry point: {self.entryPoint.x}")
        
        if self.undefinedSymbols:
            print(f"  Undefined:   {len(self.undefinedSymbols)}")
        
        if self.warnings:
            print(f"  Warnings:    {len(self.warnings)}")
        
        if self.errors:
            print(f"  Errors:      {len(self.errors)}")
