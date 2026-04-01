#!/usr/bin/env python3
"""Dump ESD cards"""

import sys
from .readObject101S import readObject101S, bytearrayToAscii, symbolTypes

def dump_esd(filename):
    obj, syms = readObject101S(filename)
    for i in range(obj['numLines']):
        card = obj[i]
        if card['type'] != 'ESD':
            continue
        d = card['lineData']
        data = d[16:72]
        for idx, sym in enumerate(['symbol1', 'symbol2', 'symbol3']):
            if idx * 16 >= card['size']:
                continue
            chunk = data[idx*16:(idx+1)*16]
            name = bytearrayToAscii(chunk[:8])
            typ_byte = chunk[8]
            typ_str = symbolTypes[typ_byte] if typ_byte < len(symbolTypes) else f"?{typ_byte}"
            addr = chunk[9:12]
            byte12 = chunk[12]
            size_bytes = chunk[13:16]
            print(f'ESD card {i}: {sym}: name={name!r} type={typ_str}({typ_byte}) '
                  f'byte12={byte12:02X} addr={addr.hex()} size={size_bytes.hex()}')
        if card.get('errors'):
            for e in card['errors']:
                print(f'  -> {e}')

def main():
    if len(sys.argv) < 2:
        print(f"esddump <object-file>", file=sys.stderr)
        sys.exit(1)
    for filename in sys.argv[1:]:
        if len(sys.argv) > 2:
            print(f"\n=== {filename} ===")
        dump_esd(filename)

if __name__ == '__main__':
    main()
