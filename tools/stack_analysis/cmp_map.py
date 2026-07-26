#!/usr/bin/env python3
"""Compare our PHASE02 memory map against the real DASS_SSW section map.

Usage: python tools/stack_analysis/cmp_map.py [sym.json] [--details N]
"""
import json, re, sys

DASS = '../pfs/PFS/mafgen/DASS_SSW_(PostIPL).ASC'
sym_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') \
    else 'build/phase02.out/PHASE02.sym.json'

# real map: name -> start hw (first section-header occurrence)
real = {}
hdr = re.compile(r'^\s*([0-9A-F]{6})-[0-9A-F]{6}\s+([$#@A-Z0-9]{1,8})\s+\*\*\*\*')
for line in open(DASS, errors='replace'):
    m = hdr.match(line)
    if m and m.group(2) not in real:
        real[m.group(2)] = int(m.group(1), 16)

sym = json.load(open(sym_path))
ours = {s['name']: s['address'] for s in sym['sections']}

common = sorted(set(real) & set(ours))
exact = [n for n in common if real[n] == ours[n]]
print(f'sections: ours={len(ours)} real={len(real)} common={len(common)}')
print(f'EXACT ADDRESS MATCHES: {len(exact)}/{len(common)}')

# bucket mismatches by delta to spot systematic shifts
from collections import Counter
deltas = Counter(ours[n] - real[n] for n in common)
print('top address deltas (ours-real, hw):')
for d, c in deltas.most_common(12):
    ex = [n for n in common if ours[n] - real[n] == d][:4]
    print(f'  {d:+8x}: {c:4} sections  e.g. {ex}')

if '--details' in sys.argv:
    n = int(sys.argv[sys.argv.index('--details') + 1])
    print('\nfirst mismatches by real address:')
    shown = 0
    for name in sorted(common, key=lambda n: real[n]):
        if real[name] != ours[name]:
            print(f'  {name:10} real={real[name]:#8x} ours={ours[name]:#8x} '
                  f'diff={ours[name]-real[name]:+#x}')
            shown += 1
            if shown >= n:
                break
