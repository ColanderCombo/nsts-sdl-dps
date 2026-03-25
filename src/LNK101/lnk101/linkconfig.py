
import copy
import logging
import yaml

from .addr import Addr

log = logging.getLogger("LNK101S")


class LinkConfig:
    CONFIG_VERSION = 1

    # Fields that hold byte addresses and should be hex-formatted in YAML
    _HEX_FIELDS = frozenset({'address'})

    def __init__(self):
        self.version = self.CONFIG_VERSION
        self.imageBase = Addr(0)
        self.entryPoint = None
        self.sections = []
        self.definedSymbols = []
        self.generatedStacks = []
        self.modules = []

    @staticmethod
    def _intsToHex(entries, fields=_HEX_FIELDS):
        result = []
        for e in entries:
            d = dict(e)
            for f in fields:
                if f in d and d[f] is not None:
                    d[f] = f"0x{d[f]:06X}"
            result.append(d)
        return result

    @staticmethod
    def _hexToInts(entries, fields=_HEX_FIELDS):
        result = []
        for e in entries:
            d = dict(e)
            for f in fields:
                if f in d and isinstance(d[f], str):
                    d[f] = int(d[f], 0)
            result.append(d)
        return result

    def toDict(self) -> dict:
        d = {
            'version': self.version,
            'imageBase': f"0x{self.imageBase:06X}",
        }
        if self.entryPoint is not None:
            d['entryPoint'] = self.entryPoint
        d['modules'] = list(self.modules)
        d['definedSymbols'] = self._intsToHex(self.definedSymbols)
        d['generatedStacks'] = list(self.generatedStacks)
        d['sections'] = self._intsToHex(self.sections)
        return d

    @classmethod
    def fromDict(cls, d) -> 'LinkConfig':
        config = cls()
        config.version = d.get('version', cls.CONFIG_VERSION)
        ib = d.get('imageBase', 0)
        config.imageBase = Addr(int(ib, 0) if isinstance(ib, str) else int(ib))
        config.entryPoint = d.get('entryPoint')
        config.modules = d.get('modules', [])
        config.definedSymbols = cls._hexToInts(d.get('definedSymbols', []))
        config.generatedStacks = d.get('generatedStacks', [])
        config.sections = cls._hexToInts(d.get('sections', []))
        return config

    def save(self, outputPath):
        data = self.toDict()
        with open(outputPath, 'w') as f:
            f.write("# LNK101S link configuration\n")
            f.write("# All addresses are in bytes (hex). Halfword address = byte address / 2.\n")
            f.write("# Sections must be 4-byte aligned. Edit addresses to control placement.\n")
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    @classmethod
    def load(cls, inputPath) -> 'LinkConfig':
        with open(inputPath, 'r') as f:
            data = yaml.safe_load(f)
        return cls.fromDict(data)

    def validate(self):
        errors = []

        sdByName = {}
        sdAddressed = []
        for s in self.sections:
            if s.get('type') == 'SD':
                sdByName[s['name']] = s
                if s.get('address') is not None:
                    sdAddressed.append(s)

        for s in sdAddressed:
            if s['address'] % 4 != 0:
                errors.append(
                    f"Section '{s['name']}' from '{s['module']}' at 0x{s['address']:06X} "
                    f"is not 4-byte aligned")

        sortedSd = sorted(sdAddressed, key=lambda s: s['address'])
        for i in range(len(sortedSd) - 1):
            cur, nxt = sortedSd[i], sortedSd[i + 1]
            curEnd = cur['address'] + cur.get('length', 0)
            if curEnd > nxt['address']:
                errors.append(
                    f"Section '{cur['name']}' (0x{cur['address']:06X}..0x{curEnd:06X}) "
                    f"overlaps with '{nxt['name']}' (0x{nxt['address']:06X})")

        for s in sdAddressed:
            if s['address'] < self.imageBase:
                errors.append(
                    f"Section '{s['name']}' at 0x{s['address']:06X} is below "
                    f"imageBase 0x{self.imageBase:06X}")

        for s in self.sections:
            if s.get('type') != 'LD':
                continue
            ownerName = s.get('ldOwner')
            if ownerName is None:
                errors.append(f"LD section '{s['name']}' has no ldOwner")
                continue
            if ownerName not in sdByName:
                errors.append(
                    f"LD section '{s['name']}' references unknown owner '{ownerName}'")
                continue
            owner = sdByName[ownerName]
            offset = s.get('ldOffset', 0)
            ownerLen = owner.get('length', 0)
            if ownerLen > 0 and offset >= ownerLen:
                errors.append(
                    f"LD section '{s['name']}' offset {offset} >= owner "
                    f"'{ownerName}' length {ownerLen}")

        if self.entryPoint is not None:
            ep = self.entryPoint
            if isinstance(ep, str):
                allNames = {s['name'] for s in self.sections}
                if ep not in allNames:
                    errors.append(f"Entry point symbol '{ep}' not found in sections")
            elif isinstance(ep, int):
                if sdAddressed and not any(
                        s['address'] <= ep < s['address'] + s.get('length', 0)
                        for s in sdAddressed):
                    errors.append(
                        f"Entry point address 0x{ep:06X} is not within any section")

        return errors

    def merge(self, overlay):
        """Merge an overlay config atop this (base) config.
        Overlay values take priority. Returns a new merged LinkConfig.
        Raises ValueError if overlay has sections not in base or type/length mismatch.
        """
        merged = copy.deepcopy(self)

        if overlay.imageBase is not None:
            merged.imageBase = overlay.imageBase
        if overlay.entryPoint is not None:
            merged.entryPoint = overlay.entryPoint
        if overlay.definedSymbols:
            merged.definedSymbols = copy.deepcopy(overlay.definedSymbols)
        if overlay.generatedStacks:
            merged.generatedStacks = copy.deepcopy(overlay.generatedStacks)

        baseIdx = {(s['name'], s['module']): i
                   for i, s in enumerate(merged.sections)}
        coveredKeys = set()

        for ovSec in overlay.sections:
            key = (ovSec['name'], ovSec['module'])
            if key not in baseIdx:
                raise ValueError(
                    f"Config section '{ovSec['name']}' from module "
                    f"'{ovSec['module']}' not found in loaded modules")

            baseSec = merged.sections[baseIdx[key]]
            coveredKeys.add(key)

            ovType = ovSec.get('type')
            if ovType is not None and ovType != baseSec.get('type'):
                raise ValueError(
                    f"Config type mismatch for '{ovSec['name']}' from "
                    f"'{ovSec['module']}': config has '{ovType}', "
                    f"modules have '{baseSec.get('type')}'")

            if baseSec.get('type') == 'SD':
                ovLen = ovSec.get('length')
                if ovLen is not None and ovLen != baseSec.get('length'):
                    raise ValueError(
                        f"Config length mismatch for '{ovSec['name']}' from "
                        f"'{ovSec['module']}': config has {ovLen}, "
                        f"modules have {baseSec.get('length')}")
                if 'address' in ovSec:
                    baseSec['address'] = ovSec['address']
            elif baseSec.get('type') == 'LD':
                if 'ldOffset' in ovSec:
                    baseSec['ldOffset'] = ovSec['ldOffset']
                if 'ldOwner' in ovSec:
                    baseSec['ldOwner'] = ovSec['ldOwner']

        for s in merged.sections:
            key = (s['name'], s['module'])
            if key not in coveredKeys:
                log.info(f"Section '{s['name']}' from module '{s['module']}' not in config, "
                         f"will be auto-placed")
                if s.get('type') == 'SD':
                    s['address'] = None

        return merged
