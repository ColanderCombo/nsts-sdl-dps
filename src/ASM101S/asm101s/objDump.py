#!/usr/bin/env python3
'''
License:    This program is declared by its author to be the U.S. Public Domain,
            and may be freely used, modified, or distributed for any purpose.
Filename:   objDump.py
Purpose:    dump IBM object module files in human-readable format.
Reference:  http://bitsavers.informatik.uni-stuttgart.de/pdf/ibm/360/os/R01-08/C28-6538-3_Linkage_Editor_Oct66.pdf
'''

import sys
import argparse
from pathlib import Path
from readObject101S import readObject101S

def be(data):
    '''Convert bytes to big-endian integer'''
    return int.from_bytes(data, 'big')

def divider(title=None):
    line = "=" * 70
    if title:
        print(f"{line}\n{title}\n{line}")
    else:
        print(line)

def hexDump(data, length, indent=''):
    lines = []
    for i in range(0, length, 16):
        chunk = data[i : i + 16]
        hex_part = ' '.join(f'{b:02X}' for b in chunk).ljust(48)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk).ljust(16)
        lines.append(f'{indent}{i:04X}: {hex_part} |{ascii_part}|')
    return '\n'.join(lines)

def dumpObjFile(filename, showHex=False, showData=False, showHeader=True):
    obj, symbols = readObject101S(filename)
    
    for err in obj[-1]["errors"]:
        print(err, file=sys.stderr)
    
    if showHeader:
        divider("IBM Object Module Dump")
        print(f"File:    {filename}")
        print(f"Cards:   {obj['numLines']}")
        divider()
        print()
    
    sections, externals, entries, entryPoint = {}, [], [], None
    
    for cardNum in range(obj["numLines"]):
        line = obj[cardNum]
        typ = line["type"]
        
        for err in line["errors"]:
            print(f"WARNING: {err}")
        
        print(f"Card {cardNum:3d}: {typ:<3s}  ident={line['ident'].strip()}")
        
        if showHex:
            print(hexDump(line["lineData"], len(line["lineData"]), '          '))
        
        if typ == 'HDR':
            print(f"          Text: \"{line.get('text', '')}\"")
        
        elif typ == 'ESD':
            firstEsdId = line.get("esdid", 1)
            for i, symKey in enumerate(["symbol1", "symbol2", "symbol3"]):
                if (sym := line.get(symKey)) is None:
                    continue
                symType = sym.get("type", "??")
                name = sym.get("name", "").strip()
                addr = sym.get("address", 0)
                length = sym.get("length", 0)
                esdId = firstEsdId + i
                
                print(f"          ESD#{esdId:<3d} {symType:<2s}  {name:<8s}  addr={addr:06X}  len={length:6d}")
                
                match symType:
                    case 'SD': sections[esdId] = {'name': name, 'length': length, 'addr': addr}
                    case 'LD': entries.append({'esdId': esdId, 'name': name, 'addr': addr})
                    case 'ER' | 'WX': externals.append({'esdId': esdId, 'name': name})
        
        elif typ == 'TXT':
            esdid, addr, size = line.get("esdid", 0), line.get("relativeAddress", 0), line.get("size", 0)
            print(f"          ESD#{esdid:<3d}  addr={addr:06X}  len={size} bytes")
            if showData and "data" in line:
                print(hexDump(bytes(line["data"]), size, '          '))
        
        elif typ == 'RLD':
            size = line.get("size", 0)
            print(f"          {size // 8} relocation entries:")
            for j in range(0, size, 8):
                rec = line["lineData"][16+j : 16+j+8]
                relId, posId, flags, addr = be(rec[:2]), be(rec[2:4]), rec[4], be(rec[5:])
                print(f"            R={relId:<3d} P={posId:<3d}  addr={addr:06X}  flags={flags:02X}")
        
        elif typ == 'END':
            if (addr := line.get("entryAddress")) is not None:
                esdId = line.get("esdid", 0)
                print(f"          Entry: ESD#{esdId} + {addr:06X}")
                entryPoint = (esdId, addr)
            if (t := line.get("translator")):
                print(f"          Translator: \"{t.strip()}\"")
            if (p := line.get("processor")):
                print(f"          Processor: \"{p.strip()}\"")
        
        elif typ == 'SYM':
            size = line.get("size", 0)
            print(f"          {size} bytes of symbol data")
            if showData:
                print(hexDump(bytes(line["lineData"][16:16+size]), size, '          '))
        
        print()
    
    if symbols:
        divider("SYM RECORD SYMBOLS")
        for sym in symbols:
            parts = [f"  {sym.get('symbolType', '?'):<11s} offset={sym.get('offsetInCSECT', 0):06X}"]
            if (n := sym.get("name")): parts.append(f'name="{n}"')
            if (t := sym.get("dataType")): parts.append(f'type={t}')
            if (l := sym.get("length")): parts.append(f'len={l}')
            print(' '.join(parts))
        print()
    
    divider("SUMMARY")
    
    if sections:
        print("\nSections (SD):")
        for esdId in sorted(sections):
            s = sections[esdId]
            print(f"  ESD#{esdId:<3d} {s['name']:<8s}  {s['length']:6d} bytes")
        print("  " + "-" * 30)
        print(f"  Total:            {sum(s['length'] for s in sections.values()):6d} bytes")
    
    if entries:
        print("\nEntry Points (LD):")
        for e in entries:
            print(f"  ESD#{e['esdId']:<3d} {e['name']:<8s}  addr={e['addr']:06X}")
    
    if externals:
        print("\nExternal References (ER):")
        for e in externals:
            print(f"  ESD#{e['esdId']:<3d} {e['name']:<8s}")
    
    if entryPoint:
        print(f"\nModule Entry Point: ESD#{entryPoint[0]} + 0x{entryPoint[1]:06X}")
    
    print()

def main():
    parser = argparse.ArgumentParser(
        description='Dump IBM object module files in human-readable format.',
        epilog='Reference: http://bitsavers.informatik.uni-stuttgart.de/pdf/ibm/360/os/R01-08/C28-6538-3_Linkage_Editor_Oct66.pdf')
    parser.add_argument('filename', help='Object file to dump')
    parser.add_argument('--hex', action='store_true', help='Show full hex dump of each card')
    parser.add_argument('--data', action='store_true', help='Show hex dump of TXT/SYM data')
    parser.add_argument('--no-header', action='store_true', help='Skip file header information')
    args = parser.parse_args()
    
    if not Path(args.filename).exists():
        parser.error(f"File not found: {args.filename}")
    
    dumpObjFile(args.filename, showHex=args.hex, showData=args.data, showHeader=not args.no_header)

if __name__ == '__main__':
    main()
