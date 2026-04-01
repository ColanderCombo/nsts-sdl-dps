#!/usr/bin/env python3
# Linker for AP-101 objects
#
# Ref:  http://bitsavers.informatik.uni-stuttgart.de/pdf/ibm/360/os/R01-08/C28-6538-3_Linkage_Editor_Oct66.pdf
#

program = "LNK101"
version = "0.1"

import sys
import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from collections import OrderedDict, defaultdict
from .readObject101S import readObject101S, bytearrayToAscii, bytearrayToInteger
from .addr import Addr, AddrDisp
from .linkconfig import LinkConfig


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


#=============================================================================
# Logging
#=============================================================================
import logging
log = logging.getLogger("LNK101")

def error(msg, fatal=True):
    log.error(msg)
    if fatal:
        sys.exit(1)

def hexDump(data, start=0, length=None):
    if length is None:
        length = len(data)
    lines = []
    for i in range(0, length, 16):
        chunk = data[start + i : start + i + 16]
        hexPart = ' '.join(f'{b:02X}' for b in chunk)
        lines.append(f'  {i:06X}: {hexPart}')
    return '\n'.join(lines)

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
    module: 'ObjectModule | None' = None
    baseAddress: 'Addr | None' = None            # assigned during linking
    data: bytearray = field(default_factory=bytearray)
    ldId: int | None = None                      # LD only: ESD ID of owning SD

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
    module: 'ObjectModule | None' = None
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
    module: 'ObjectModule | None' = None

    # Derived from flags
    sign: int = field(init=False)
    direction: int = field(init=False)
    length: int = field(init=False)
    continuation: int = field(init=False)

    def __post_init__(self):
        self.sign = (self.flags >> 7) & 1
        self.direction = (self.flags >> 1) & 1
        self.length = ((self.flags >> 2) & 0x03) + 1
        self.continuation = self.flags & 1


@dataclass
class ObjectModule:
    filename: str
    name: str = ''
    sections: dict = field(default_factory=dict)          # esdId -> Section
    sectionsByName: dict = field(default_factory=dict)     # name -> Section
    externals: dict = field(default_factory=dict)          # esdId -> External
    externalsByName: dict = field(default_factory=dict)    # name -> External
    relocations: list = field(default_factory=list)
    entryPoint: tuple | None = None                        # (esdId, byteOffset)
    entryName: str | None = None
    stackSizes: dict = field(default_factory=dict)         # csectName -> halfword size
    external: bool = False                                 # True = address-only, not in image

    def __post_init__(self):
        if not self.name:
            self.name = Path(self.filename).stem

    def addSection(self, section):
        self.sections[section.esdId] = section
        self.sectionsByName[section.name] = section

    def addExternal(self, ext):
        self.externals[ext.esdId] = ext
        self.externalsByName[ext.name] = ext

    def synthesizeMissingExternals(self):
        #  Synthesize missing ER entries for RLD targets not in the ESD table.
        #       PROGRAMs get @0P stack ER's, but TASKS don't seem to get them
        #       for TASKS, generate a @nX from the $nX symbol:
        #
        for reloc in self.relocations:
            if reloc.relId in self.sections or reloc.relId in self.externals:
                continue
            posName = None
            if reloc.posId in self.sections:
                posName = self.sections[reloc.posId].name
            if posName and posName.startswith('$'):
                inferredName = f'@{posName[1:]}'
            else:
                inferredName = f'@ESD{reloc.relId}'

            self.addExternal(External(inferredName, reloc.relId, False, self))
            log.debug(f"Synthesized missing ER: ESD#{reloc.relId} = '{inferredName}' "
                      f"(inferred from posId={reloc.posId})")

    @classmethod
    def load(cls, filename, quiet=False):
        """Parse an object file into ObjectModule(s).
        Returns (modules, errors, warnings).
        """
        obj, symbols = readObject101S(filename)

        errors = [f"{filename}: {err}" for err in obj[-1]["errors"] if "Error" in err]
        warnings = []
        if not quiet:
            for cardNum in range(obj["numLines"]):
                for err in obj[cardNum]["errors"]:
                    warnings.append(f"{filename}: {err}")

        modules = []
        module = None
        moduleNum = 0

        for cardNum in range(obj["numLines"]):
            line = obj[cardNum]
            typ = line["type"]

            if typ == "HDR":
                if module is not None and module.sections:
                    modules.append(module)
                moduleNum += 1
                module = cls(filename, name=f"{Path(filename).stem}#{moduleNum}")
                continue

            if typ == "ESD":
                if module is None:
                    moduleNum += 1
                    module = cls(filename,
                                 name=f"{Path(filename).stem}#{moduleNum}" if moduleNum > 1
                                      else Path(filename).stem)

                firstEsdId = line.get("esdid", 1)
                for i, symKey in enumerate(["symbol1", "symbol2", "symbol3"]):
                    if (sym := line.get(symKey)) is None:
                        continue

                    symType = sym.get("type", "")
                    name = sym.get("name", "").strip()
                    addr = AddrDisp(sym.get("address", 0))
                    length = AddrDisp(sym.get("length", 0))
                    esdId = firstEsdId + i

                    if symType == "SD":
                        module.addSection(Section(name, esdId, 'SD', addr, length,
                                                  module, data=bytearray(length)))
                    elif symType == "LD":
                        module.addSection(Section(name, esdId, 'LD', addr,
                                                  module=module, ldId=sym.get("ldid", 0)))
                    elif symType in ("ER", "WX"):
                        module.addExternal(External(name, esdId, symType == "WX", module))

            elif typ == "TXT":
                if module is None:
                    continue
                esdId = line.get("esdid", 1)
                relAddr = line.get("relativeAddress", 0)
                size = line.get("size", 0)
                data = line.get("data", ())
                sect = module.sections.get(esdId)
                if sect and sect.type == 'SD':
                    for i, b in enumerate(data[:size]):
                        if relAddr + i < len(sect.data):
                            sect.data[relAddr + i] = b

            elif typ == "RLD":
                if module is None:
                    continue
                size = line.get("size", 0)
                lineData = line["lineData"]
                j = 0
                prevRelId = prevPosId = 0
                while j < size:
                    rec = lineData[16+j : 16+j+8]
                    if j > 0 and module.relocations and module.relocations[-1].continuation:
                        flags = rec[0]
                        addr = AddrDisp(bytearrayToInteger(rec[1:4]))
                        log.debug(f"RLD card#{cardNum} @{16+j}: SHORT R={prevRelId} P={prevPosId} "
                                  f"flags=0x{flags:02X} addr=0x{addr:06X}")
                        module.relocations.append(
                            Relocation(prevRelId, prevPosId, flags, addr, module))
                        j += 4
                        continue

                    relId = bytearrayToInteger(rec[:2])
                    posId = bytearrayToInteger(rec[2:4])
                    flags = rec[4]
                    addr = AddrDisp(bytearrayToInteger(rec[5:8]))
                    log.debug(f"RLD card#{cardNum} @{16+j}: FULL R={relId} P={posId} "
                              f"flags=0x{flags:02X} addr=0x{addr:06X} (raw: {rec[:8].hex()})")
                    module.relocations.append(
                        Relocation(relId, posId, flags, addr, module))
                    prevRelId, prevPosId = relId, posId
                    j += 8

            elif typ == "END":
                if module is None:
                    continue
                esdId = line.get("esdid", 0)
                if (addr := line.get("entryAddress")) is not None:
                    module.entryPoint = (esdId, AddrDisp(addr))
                elif esdId > 0 and line.get("idrType") == '2':
                    module.entryPoint = (esdId, AddrDisp(0))
                if (name := line.get("entryName")):
                    module.entryName = name.strip()

                if module.sections:
                    module.synthesizeMissingExternals()
                    modules.append(module)
                module = None

        if module is not None and module.sections:
            module.synthesizeMissingExternals()
            modules.append(module)

        # Extract per-CSECT stack sizes from SYM STACKEND entries
        if modules and symbols:
            csectName = None
            for sym in symbols:
                name = sym.get('name')
                if name is None:
                    continue
                if sym.get('symbolType') == 'CONTROL':
                    csectName = name
                elif name == 'STACKEND' and csectName is not None:
                    stackHW = sym.get('offsetInCSECT', 0)
                    if stackHW > 0:
                        for mod in modules:
                            if csectName in mod.sectionsByName:
                                mod.stackSizes[csectName] = stackHW
                                break

        return modules, errors, warnings



class Linker: 
    def __init__(self, args):
        self.args = args
        self.modules = [] 
        self.globalSymbols = OrderedDict()  # name -> (section, module, byteAddress)
        self.undefinedSymbols = defaultdict(set)  # name -> set of (filename, csect_or_None)
        self.stackSizes = {}
        self.generatedStacks = []
        self.image = None
        self.imageBase = Addr(args.base_address)
        self.imageSize = AddrDisp(0)
        self.entryPoint = None
        self.errors = []
        self.warnings = []
        self.appliedRelocations = []  # populated by applyRelocations()
    
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

    def _imgReadHW(self, offset):
        """Read a big-endian halfword from the image."""
        return (self.image[offset] << 8) | self.image[offset + 1]

    def _imgWriteHW(self, offset, value):
        """Write a big-endian halfword to the image."""
        self.image[offset] = (value >> 8) & 0xFF
        self.image[offset + 1] = value & 0xFF

    def _imgPatchHW(self, offset, mask, bits):
        """Read-modify-write a halfword: clear mask bits, set new bits."""
        hw = self._imgReadHW(offset)
        hw = (hw & mask) | bits
        self._imgWriteHW(offset, hw)
        return hw

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
        modules, errors, warnings = ObjectModule.load(filename, quiet=self.args.quiet)
        self.errors.extend(errors)
        self.warnings.extend(warnings)
        self.modules.extend(modules)
        return modules or None

    def findModuleForSymbol(self, symbolName):
        for libPath in self.args.library_path:
            if not os.path.isdir(libPath):
                continue
            
            # ZCON thunks: #QSQRT is in #QSQRT.obj
            for ext in ['.obj', '.OBJ', '']:
                candidate = os.path.join(libPath, symbolName + ext)
                if os.path.exists(candidate):
                    if self._moduleDefinesSymbol(candidate, symbolName):
                        return candidate
            
            # XXX Also try without the # prefix (some systems)
            if symbolName.startswith('#'):
                baseName = symbolName[1:]
                for ext in ['.obj', '.OBJ', '']:
                    candidate = os.path.join(libPath, baseName + ext)
                    if os.path.exists(candidate):
                        if self._moduleDefinesSymbol(candidate, symbolName):
                            return candidate
            
            # Fall back to searching all .obj files in directory
            # (slower but handles arbitrary symbol-to-file mappings)
            for fname in os.listdir(libPath):
                if not fname.lower().endswith('.obj'):
                    continue
                candidate = os.path.join(libPath, fname)
                # Skip files we've already checked
                if fname == symbolName + '.obj' or fname == symbolName + '.OBJ':
                    continue
                if symbolName.startswith('#'):
                    baseName = symbolName[1:]
                    if fname == baseName + '.obj' or fname == baseName + '.OBJ':
                        continue
                
                if self._moduleDefinesSymbol(candidate, symbolName):
                    return candidate
        
        return None
    
    def _moduleDefinesSymbol(self, filename, symbolName):
        """Check if an object file defines the given symbol."""
        try:
            obj, _ = readObject101S(filename)
            for cardNum in range(obj["numLines"]):
                line = obj[cardNum]
                if line["type"] == "ESD":
                    for symKey in ["symbol1", "symbol2", "symbol3"]:
                        if sym := line.get(symKey):
                            if sym.get("name", "").strip() == symbolName:
                                if sym.get("type") in ("SD", "LD"):
                                    return True
        except Exception:
            pass
        return False
    
    def buildGlobalSymbolTable(self):
        """Build the global symbol table from all loaded modules."""
        for module in self.modules:
            for esdId, section in module.sections.items():
                name = section.name
                if not name or section.type not in ('SD', 'LD'):
                    continue
                if name in self.globalSymbols:
                    existing = self.globalSymbols[name]
                    self.warnings.append(
                        f"Symbol '{name}' defined in both {existing[1].filename} "
                        f"and {module.filename}")
                else:
                    self.globalSymbols[name] = (section, module, None)
                    detail = f"length=0x{section.length:X}" if section.type == 'SD' \
                             else f"offset=0x{section.address:X}"
                    log.debug(f"Symbol {section.type} '{name}' defined in {module.name} "
                          f"ESD#{esdId} {detail}")

        for module in self.modules:
            for esdId, ext in module.externals.items():
                if ext.name not in self.globalSymbols:
                    self._addUndefRef(ext.name, module, esdId)

    def _addUndefRef(self, symName, module, esdId):
        """Record that module references undefined symbol symName (ESD ID esdId).
        Uses RLD entries to identify which CSECT(s) contain the reference."""
        csects = {module.sections[r.posId].name.strip()
                  for r in module.relocations
                  if r.relId == esdId and r.posId in module.sections}
        if csects:
            for csect in csects:
                self.undefinedSymbols[symName].add((module.filename, csect))
        else:
            self.undefinedSymbols[symName].add((module.filename, None))

    def resolveExternals(self, searchLibraries=True):
        """
        Resolve external references against the global symbol table.
        Optionally search library paths for missing symbols.
        """
        
        newModulesLoaded = True
        iterations = 0
        maxIterations = 100  # Prevent infinite loops
        
        while newModulesLoaded and iterations < maxIterations:
            iterations += 1
            newModulesLoaded = False
            
            self._rebuildSymbolTable()
            
            # Check if we need to search for more modules
            if searchLibraries and self.undefinedSymbols:
                for symName in list(self.undefinedSymbols):
                    if symName in self.globalSymbols:
                        continue
                    
                    # Search for module defining this symbol
                    filename = self.findModuleForSymbol(symName)
                    if filename:
                        # Check we haven't already loaded this module
                        if not any(m.filename == filename for m in self.modules):
                            if self.loadModule(filename):
                                newModulesLoaded = True
                                log.info(f"Loaded {filename} for symbol '{symName}'")
        
        # Final pass: resolve all externals
        for module in self.modules:
            for esdId, ext in module.externals.items():
                if ext.name in self.globalSymbols:
                    section, defModule, _ = self.globalSymbols[ext.name]
                    ext.resolved = True
                    ext.resolvedSection = section
                else:
                    if not ext.weak:
                        self._addUndefRef(ext.name, module, esdId)
        
        return len(self.undefinedSymbols) == 0
    
    def _placeSections(self, sections, startAddr, label=""):
        """Place a list of sections sequentially starting at startAddr.
        Returns the address after the last section placed.
        All addresses are in bytes.
        """
        currentAddr = Addr(startAddr)
        for section in sections:
            currentAddr = currentAddr.align(4)

            section.baseAddress = currentAddr
            currentAddr = currentAddr + section.length

            cat = section.zone
            log.info(f"  {section.name:8s} @ 0x{section.baseAddress:06X} "
                     f"({section.length} bytes){' [' + label + ']' if label else ''}")
            log.debug(f"Section '{section.name}' ({cat}) @ {section.baseAddress.x} "
                  f"len=0x{section.length:X}(bytes) end=0x{currentAddr:06X}(byte)")
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
            for esdId, section in module.sections.items():
                if section.type != 'LD':
                    continue
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
        for name in self.globalSymbols:
            section, module, _ = self.globalSymbols[name]
            if section.baseAddress is not None:
                self.globalSymbols[name] = (section, module, section.baseAddress)
                log.debug(f"Global symbol '{name}' -> addr={section.baseAddress.x}")

    @staticmethod
    def _classifySections(sections):
        """Classify and sort sections into (zcon, data, code) groups."""
        groups = {'ZCON': [], 'DATA': [], 'CODE': []}
        for s in sections:
            groups[s.zone].append(s)
        for g in groups.values():
            g.sort(key=lambda s: s.name.strip())
        return groups['ZCON'], groups['DATA'], groups['CODE']

    def _computeImageSize(self):
        """Compute imageSize from the highest SD section end."""
        highestEnd = self.imageBase
        for module in self.modules:
            if module.external:
                continue
            for esdId, section in module.sections.items():
                if section.type == 'SD' and section.baseAddress is not None:
                    end = section.baseAddress + section.length
                    if end > highestEnd:
                        highestEnd = end
        self.imageSize = highestEnd - self.imageBase  # AddrDisp
        return self.imageSize

    def _highestEndInRange(self, lo, hi):
        """Find the highest section end address within [lo, hi)."""
        best = lo
        for module in self.modules:
            if module.external:
                continue
            for esdId, section in module.sections.items():
                if section.type == 'SD' and section.baseAddress is not None:
                    end = section.baseAddress + section.length
                    if lo <= section.baseAddress < hi and end > best:
                        best = end
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
            for esdId, section in module.sections.items():
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
            for esdId, section in module.sections.items():
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
            for esdId, section in module.sections.items():
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
            for esdId, section in module.sections.items():
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

        if self.args.compact or not unassigned:
            self._placeSections(unassigned,
                                self._highestEndInRange(0, float('inf')),
                                "auto-placed")
        else:
            zcon, data, code = self._classifySections(unassigned)
            if zcon:
                self._placeSections(zcon,
                    max(self._highestEndInRange(0, ZCON_END), PSA_END),
                    "auto-ZCON")
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
        for module in self.modules:
            if module.external:
                continue
            for esdId, section in module.sections.items():
                if section.type != 'SD' or section.baseAddress is None:
                    continue
                
                offset = section.baseAddress - self.imageBase
                if log.isEnabledFor(logging.DEBUG) and section.length > 0:
                    dataHex = section.data[:min(16, section.length)].hex()
                    log.debug(f"COPY {module.name}/{section.name}: offset=0x{offset:06X}(byte) len={section.length} data={dataHex}...")
                for i, b in enumerate(section.data):
                    if offset + i < len(self.image):
                        self.image[offset + i] = b
        
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
                
                # Find what the relocation references (R section/external)
                targetAddr = Addr(0)
                resolved = True

                if reloc.relId in module.sections:
                    targetSection = module.sections[reloc.relId]
                    if targetSection.baseAddress is not None:
                        targetAddr = targetSection.baseAddress - targetSection.address
                    else:
                        resolved = False

                elif reloc.relId in module.externals:
                    ext = module.externals[reloc.relId]
                    if ext.resolved and ext.resolvedSection and ext.resolvedSection.baseAddress is not None:
                        targetAddr = ext.resolvedSection.baseAddress
                    else:
                        resolved = False
                        if not ext.weak and not self.args.force:
                            self.errors.append(
                                f"{module.filename}: Unresolved external '{ext.name}' "
                                f"at offset 0x{reloc.address:06X}(byte)")
                            relocErrors += 1
                
                else:
                    self.warnings.append(
                        f"{module.filename}: Unknown relocation target ESD#{reloc.relId}")
                    resolved = False
                
                if not resolved and not self.args.force:
                    continue
                
                # Calculate the image offset for this relocation (in bytes)
                # RLD address field contains byte offsets relative to P section
                # All internal addresses are in bytes, so this is a direct calculation
                imageOffset = posSection.baseAddress - self.imageBase + reloc.address
                
                if imageOffset < 0 or imageOffset >= len(self.image):
                    self.warnings.append(
                        f"{module.filename}: Relocation address out of bounds: 0x{imageOffset:06X}")
                    continue
                
                targetName = "???"
                if reloc.relId in module.sections:
                    targetName = module.sections[reloc.relId].name
                elif reloc.relId in module.externals:
                    targetName = module.externals[reloc.relId].name

                log.debug(f"RELOC {module.name}: "
                      f"offset=0x{imageOffset:06X}(byte) flags=0x{reloc.flags:02X} "
                      f"target='{targetName}' addr={targetAddr.x}")

                # Sector-register-only relocations (0x40=DSR, 0x20=BSR):
                # Write ONLY sector bits into hw2; do NOT add address.
                # Standard relocations: apply value then patch ZCON sector
                # fixups for cross-sector addresses.
                flagType = reloc.flags & 0x7F
                hw2Off = imageOffset + 2
                sector = targetAddr.sector

                if flagType == 0x40:
                    if hw2Off + 1 < len(self.image):
                        hw2 = self._imgPatchHW(hw2Off, 0xFFF0, sector & 0xF)
                        log.debug(f"  -> ZCON DSR sector-only: DSR={sector} -> 0x{hw2:04X}")
                elif flagType == 0x20:
                    if hw2Off + 1 < len(self.image):
                        hw2 = self._imgPatchHW(hw2Off, 0xFF8F, (sector & 0x7) << 4)
                        log.debug(f"  -> ZCON BSR sector-only: BSR={sector} -> 0x{hw2:04X}")
                else:
                    self._applyRelocationValue(imageOffset, targetAddr, reloc)

                # ZCON code-address (0x04, 0x10): patch BSR in hw2 if CB bit is set
                if flagType in (0x04, 0x10) and sector > 0:
                    if hw2Off + 1 < len(self.image):
                        if self._imgReadHW(hw2Off) & 0x0200:  # CB bit
                            hw2 = self._imgPatchHW(hw2Off, 0xFF8F, (sector & 0x7) << 4)
                            log.debug(f"  -> ZCON BSR fixup: ptrBSR={sector} -> 0x{hw2:04X}")

                # ZCON data-address (0x50): patch DSR in hw2
                if flagType == 0x50 and sector > 0:
                    if hw2Off + 1 < len(self.image):
                        hw2 = self._imgPatchHW(hw2Off, 0xFFF0, sector & 0xF)
                        log.debug(f"  -> ZCON DSR fixup: ptrDSR={sector} -> 0x{hw2:04X}")

                # Record the relocation: read back the final value from the
                # image and decode to an absolute halfword address so we can
                # resolve it to a specific symbol (not just the CSECT).
                if flagType not in (0x40, 0x20):  # skip sector-only
                    length = self._RELOC_LEN.get(reloc.flags,
                                2 if reloc.flags in self._RELOC_LEN_2
                                else reloc.length)
                    finalVal = int.from_bytes(
                        self.image[imageOffset:imageOffset + length], 'big')
                    if length == 2:
                        # Decode sector encoding: 0x8000|offset → sector*0x8000+offset
                        if finalVal & 0x8000:
                            resolvedHW = sector * 0x8000 + (finalVal & 0x7FFF)
                        else:
                            resolvedHW = finalVal
                    else:
                        resolvedHW = finalVal  # 4-byte ACON: raw halfword addr
                    self.appliedRelocations.append((imageOffset.hw, resolvedHW))

        return relocErrors == 0
    
    # PFS Relocation length by flag value.
    # PFS compiler flag byte = (type << 4) | (LL << 2) | direction | continuation
    # Type 0 = YCON -> 2-byte halfword address
    # Type 1 = ZCON -> 2-byte halfword (w/optional separate fixup card, if required)
    # Type 2 = BSR-only sector fixup
    # Type 4 = DSR-only sector fixup
    # Type 5 = ZCON data addresss
    _RELOC_LEN = {0x1C: 4, 0x9C: 4}
    _RELOC_LEN_2 = frozenset([0x00, 0x04, 0x10, 0x50, 0x80, 0x90, 0xA0, 0xC0, 0xD0])

    def _applyRelocationValue(self, imageOffset, targetAddr, reloc):
        """Apply a standard (non-sector-only) relocation to the image.
        targetAddr is an Addr. Caller handles 0x40/0x20 sector-only types.

        AP-101 16-bit sector encoding: halfword addresses in sector 1+
        are encoded as 0x8000 | (hw & 0x7FFF); CPU uses BSR/DSR at runtime.
        """
        length = self._RELOC_LEN.get(reloc.flags,
                    2 if reloc.flags in self._RELOC_LEN_2
                    else reloc.length)

        # 2-byte relocations use sector-encoded halfword; 4-byte use raw halfword
        value = targetAddr.sector_encode() if length == 2 else targetAddr.hw
        if length == 2 and targetAddr.sector > 0:
            log.debug(f"  -> sector encode: 0x{value:04X}")

        existing = int.from_bytes(
            self.image[imageOffset:imageOffset + length], 'big')

        relocValue = -value if reloc.sign else value
        newValue = (existing + relocValue) if reloc.direction == 0 else (existing - relocValue)
        newValue &= (1 << (length * 8)) - 1

        log.debug(f"  -> existing=0x{existing:0{length*2}X} "
                  f"{'+'if reloc.direction==0 else '-'} 0x{relocValue:04X} "
                  f"= 0x{newValue:0{length*2}X}")

        self.image[imageOffset:imageOffset + length] = newValue.to_bytes(length, 'big')
    
    def determineEntryPoint(self):
        if self.args.entry:
            # passed as argument:
            if self.args.entry in self.globalSymbols:
                section, module, byteAddr = self.globalSymbols[self.args.entry]
                if byteAddr is not None:
                    # Convert byte address to halfword for simulator
                    self.entryPoint = byteAddr  # already Addr
                    return
            try:
                self.entryPoint = Addr.from_hw(int(self.args.entry, 0))
                return
            except ValueError:
                self.warnings.append(f"Entry point symbol '{self.args.entry}' not found")

        for module in self.modules:
            if module.entryPoint:
                esdId, byteOffset = module.entryPoint
                section = module.sections.get(esdId)
                if section and section.baseAddress is not None:
                    self.entryPoint = section.baseAddress + byteOffset
                    return

            if module.entryName:
                if module.entryName in self.globalSymbols:
                    section, _, byteAddr = self.globalSymbols[module.entryName]
                    if byteAddr is not None:
                        self.entryPoint = byteAddr  # already Addr
                        return

        self.entryPoint = self.imageBase
    
    def _getOrCreateSyntheticModule(self, filename, displayName):
        """Get or create a synthetic module for generated sections."""
        for m in self.modules:
            if m.filename == filename:
                return m
        module = ObjectModule(filename)
        module.name = displayName
        self.modules.append(module)
        return module

    def _addSyntheticSection(self, module, name, length, baseAddress=None):
        """Add an SD section to a synthetic module. Returns the new Section."""
        length = AddrDisp(length) if not isinstance(length, AddrDisp) else length
        nextEsdId = max((s.esdId for s in module.sections.values()), default=0) + 1
        section = Section(name, nextEsdId, 'SD', AddrDisp(0), length, module,
                          data=bytearray(length),
                          baseAddress=baseAddress if isinstance(baseAddress, Addr) else
                                      Addr(baseAddress) if baseAddress is not None else None)
        module.addSection(section)
        return section

    def _rebuildSymbolTable(self):
        """Clear and rebuild the global symbol table."""
        self.globalSymbols.clear()
        self.undefinedSymbols.clear()
        self.buildGlobalSymbolTable()

    def generateStackSections(self):
        #
        # Generate BSS sections for undefined @xxx (stack frame) symbols.
        # Sizes come from SYM STACKEND data
        knownStackSizes = {}
        for module in self.modules:
            knownStackSizes.update(module.stackSizes)

        fallbackHW = self.args.generate_stacks or 0
        added = False

        for symName in list(self.undefinedSymbols):
            if not symName.startswith('@'):
                continue

            dollarName = '$' + symName[1:]
            sizeHW = knownStackSizes.get(dollarName, fallbackHW)
            if sizeHW <= 0:
                continue

            sizeBytes = AddrDisp.from_hw(sizeHW)
            module = self._getOrCreateSyntheticModule("<generated-stacks>", "<stacks>")
            self._addSyntheticSection(module, symName, sizeBytes)
            added = True

            source = "SYM" if dollarName in knownStackSizes else "fallback"
            log.info(f"Generated stack section '{symName}' ({sizeHW} HW / {sizeBytes} bytes, {source})")

        if added:
            self._rebuildSymbolTable()

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
            self._addSyntheticSection(module, symName, 0, baseAddress=value)
            added = True
            log.info(f"Defined symbol '{symName}' = 0x{value:06X}")

        if added:
            self._rebuildSymbolTable()

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

        adddresses and offsets are in halfwords we accept both decimal and
        hex numbers.

        We don't it at the moment, but known CSECT types are:
        
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

            contents = entry.get('contents')
            if contents:
                for ldName, ldVal in contents.items():
                    if isinstance(ldVal, dict):
                        offsetHW = ldVal.get('offset', 0)
                    else:
                        offsetHW = ldVal
                    offsetAddr = AddrDisp.from_hw(offsetHW)
                    nextEsdId = max(
                        (s.esdId for s in module.sections.values()), default=0) + 1
                    ld = Section(ldName, nextEsdId, 'LD',
                                 address=offsetAddr,
                                 module=module,
                                 baseAddress=baseAddr + offsetAddr,
                                 ldId=section.esdId)
                    module.addSection(ld)

        # Pre-assign addresses for locally-defined sections found in the JSON
        for module in self.modules:
            if module.external:
                continue
            for esdId, section in module.sections.items():
                if section.type != 'SD' or section.baseAddress is not None:
                    continue
                entry = csectTable.get(section.name)
                if entry is not None and 'start' in entry:
                    section.baseAddress = Addr.from_hw(entry['start'])
                    log.info(f"Placed '{section.name}' @ {section.baseAddress} (from external-syms)")

        if added:
            self._rebuildSymbolTable()
            return True
        return False

    def link(self):
        
        # Process -D defined symbols first
        log.info("Processing defined symbols...")
        self.processDefinedSymbols()
        
        log.info("Building global symbol table...")
        self._rebuildSymbolTable()
        
        log.info("Resolving external references...")
        allResolved = self.resolveExternals(searchLibraries=bool(self.args.library_path))

        # Resolve remaining undefined symbols from external JSON csect table
        if not allResolved and self.args.external_syms:
            log.info("Loading external symbol table...")
            if self.loadExternalSyms(self.args.external_syms):
                allResolved = self.resolveExternals(searchLibraries=False)

        # Generate stack sections for undefined @xxx symbols.
        # Sizes come from SYM STACKEND data
        if self.undefinedSymbols:
            hasKnownSizes = any(m.stackSizes for m in self.modules)
            if hasKnownSizes or self.args.generate_stacks:
                log.info("Generating stack sections...")
                self.generateStackSections()
                allResolved = self.resolveExternals(searchLibraries=False)
        
        if not allResolved:
            for sym in sorted(self.undefinedSymbols):
                self.errors.append(
                    f"Undefined symbol: {sym}, referenced by "
                    f"{self._formatUndefRefs(self.undefinedSymbols[sym])}")
            
            if not self.args.force:
                return False
            else:
                log.warning(f"{len(self.undefinedSymbols)} undefined symbol(s), continuing due to -f")
        
        #
        # Place Sections
        #
        if self.args.load_config:
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

        self.determineEntryPoint()

        return success or self.args.force
    
    def saveImage(self, outputPath):
        if self.image is None:
            error("No image to save")
            return
        
        with open(outputPath, 'wb') as f:
            f.write(self.image)
        
        log.info(f"Wrote {len(self.image)} bytes to {outputPath}")
    
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
            for esdId, section in module.sections.items():
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
    
    def saveJsonSymbols(self, outputPath):
        # gen .sym.json, read by gpc simulator for debugging
        data = {
            "version": version,
            "imageSize": self.imageSize.hw,  # halfwords
            "entryPoint": self.entryPoint.hw,
            "sections": [],
            "symbols": [],
            "modules": []
        }
        
        # Collect SD CSECTs sorted by address
        for module in self.modules:
            modInfo = {
                "name": Path(module.filename).stem if module.filename else module.name,
                "filename": module.filename
            }
            data["modules"].append(modInfo)
            
            for esdId, section in module.sections.items():
                if section.type == 'SD' and section.baseAddress is not None:
                    data["sections"].append({
                        "name": section.name,
                        "address": section.baseAddress.hw,
                        "size": section.length.hw,
                        "module": modInfo["name"]
                    })
        
        # Sort sections by address
        data["sections"].sort(key=lambda x: x["address"])
        
        # Global symbols with resolved addresses
        for name in sorted(self.globalSymbols.keys()):
            section, module, byteAddr = self.globalSymbols[name]
            if byteAddr is not None:
                symType = "entry" if section.type == 'LD' else "section"
                data["symbols"].append({
                    "name": name,
                    "address": byteAddr.hw,
                    "type": symType,
                    "section": section.name if section.type == 'LD' else None,
                    "module": Path(module.filename).stem if module.filename else module.name
                })
        
        # Sort symbols by address
        data["symbols"].sort(key=lambda x: x["address"])

        # Applied relocations: resolve each target hw address to the most
        # specific symbol (LD label preferred over SD csect).
        # Build sorted list of (hwAddr, name) for bisect lookup.
        symAddrs = sorted(
            (byteAddr.hw, name)
            for name, (section, module, byteAddr) in self.globalSymbols.items()
            if byteAddr is not None
        )
        symHWs = [a for a, _ in symAddrs]

        import bisect
        def _resolveSymbol(targetHW):
            """Find the symbol whose address is closest to (and <=) targetHW.
            Returns 'NAME' or 'NAME+offset' if there's a displacement."""
            idx = bisect.bisect_right(symHWs, targetHW) - 1
            if idx >= 0:
                symAddr, symName = symAddrs[idx]
                disp = targetHW - symAddr
                if disp:
                    return f"{symName}+{disp:X}"
                return symName
            return None

        data["relocations"] = []
        for hwAddr, targetHW in sorted(self.appliedRelocations):
            sym = _resolveSymbol(targetHW)
            data["relocations"].append({
                "address": hwAddr,
                "target": targetHW,
                "symbol": sym
            })

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
            for esdId, section in module.sections.items():
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
            for esdId, section in module.sections.items():
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


if __name__ == '__main__':
    main()
