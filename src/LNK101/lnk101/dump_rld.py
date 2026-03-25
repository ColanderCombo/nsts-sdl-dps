#!/usr/bin/env python3

import sys
from .readObject101S import readObject101S

def main():
    if len(sys.argv) < 2:
        print("Usage: dump_rld.py <objfile>")
        sys.exit(1)
    
    obj, syms = readObject101S(sys.argv[1])
    
    print("=== ESD ENTRIES ===")
    for i in range(obj['numLines']):
        line = obj[i]
        if line['type'] == 'ESD':
            esdId = line.get('esdid', 1)
            for j, sk in enumerate(['symbol1', 'symbol2', 'symbol3']):
                if sym := line.get(sk):
                    print(f"  {esdId+j}: {sym.get('type','')} {sym.get('name','').strip()} addr={sym.get('address',0):04X} len={sym.get('length',0)}")
    
    print("\n=== RLD ENTRIES ===")
    for i in range(obj['numLines']):
        line = obj[i]
        if line['type'] == 'RLD':
            size = line.get('size', 0)
            data = line['lineData']
            j = 0
            while j < size:
                rec = data[16+j:16+j+8]
                relId = (rec[0]<<8)|rec[1]
                posId = (rec[2]<<8)|rec[3]
                flags = rec[4]
                addr = (rec[5]<<16)|(rec[6]<<8)|rec[7]
                print(f"  R={relId:2d} P={posId:2d} flags={flags:02X} addr={addr:04X} (byte {addr*2:04X})")
                j += 8

if __name__ == '__main__':
    main()
