# src/mafgen — MAFGEN (DASS listing) replacement

Reads a fcm, its .sym.json and SDF data to generate 
a MAFGEN "DASS" disassembly listing.

## Usage

mafgen currently expects the build artifacts to live in
a mmu build tree:

```sh
# generate (reads build/mmu/<CFG>/<CFG>.fcm + .sym.json + PHASE*/gen/SDFLIB)
mafgen G2 -o build/mafgen/G2.ASC

# normalize a real DASS for diffing; removes page headers & sections we don't
# yet replicate:
mafgen --strip DASS_G2.ASC -o g2.ref

# the fidelity metric: exact-line match rate per line class
mafgen G2 --score DASS_G2.ASC
```

Sections emitted (`--sections index,extent,dump`): the alphabetical
csect→address index (EBCDIC collation, 7 per row), the address-ordered csect
extent list (header lines), and the MEMORY MAP dump (headers + AP-101
disassembly for code csects, hex rows for data).
