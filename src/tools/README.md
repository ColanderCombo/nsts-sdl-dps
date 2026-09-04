# tools - misc toolchain tools

Smaller CLI utilities.

| module                 | entry point           | purpose                                             |
|------------------------|-----------------------|-----------------------------------------------------|
| `fcmcmp`               | `fcmcmp` / `-m`       | compare two FCM images halfword-by-halfword         |
| `objdump`              | `ibmobjdump` / `-m`   | dump an IBM object module (ESD/TXT/RLD)             |
| `libdump`              | `ibmlibdump` / `-m`   | dump a `.lib` load module (CESD/TEXT/PROT/RLD/REGN) |
| `rldanalyze`           | `-m tools.rldanalyze` | RLD relocation analysis                             |
| `extract_fcmcmp_diffs` | `-m`                  | pull diffs out of an fcmcmp JSON report             |
| `dfginput`             | `-m tools.dfginput`   | recover DFG source from a DFG output COMPOOL member |
| `mmu2fcm`              | `-m tools.mmu2fcm`    | compose phase `.lib`s into a configuration `.fcm`   |
| `mmu2mmv`              | `-m tools.mmu2mmv`    | lay the built phases onto a mass memory tape volume |
| `mmubuild`             | `-m tools.mmubuild`   | MMU load-build cards: parser, expansion tree, `DIRECTRY` tables |
| `asmimage`             | `asmimage` / `-m`     | assemble one AP-101 program and locate it at a fixed address |

## mmu2mmv - convert mmu tree into a single volume file

`mmu2mmv` joins the MMU load-build cards to the linked phase modules under
`build/mmu`, and writes a `.mmv` volume for the simulator's mass memory
unit (`ext/sim/mmu`, `MMU.sh run --volume`).

```
python3 -m tools.mmu2mmv --report                      # the tape map
python3 -m tools.mmu2mmv --out build/mmu/mmu.mmv       # write it
```

## mmu2fcm - generate fcm image from mmu tree

`mmu2fcm` builds a GPC memory image from the MMU phase tree by duplicating
the logic GPCIPL/PFS uses when loading on the live system:

```
python3 -m tools.mmu2fcm --con80 code/OI340700/CON80 --mmu build/OI340700 \
        --boot --out build/OI340700/BOOT
ext/sim/GPC.sh run --ipl build/OI340700/BOOT/BOOT.fcm
```

## asmimage

`asmimage` runs asm101 over one source and lnk101 over the object with the
csect pinned at `--origin` and outputs a json representation of the binary:

```
asmimage ext/sim/gpc/asm/fakeipl.asm --origin 0x3FF80 \
         --out ext/sim/gpc/gen/fakeipl.json \
         --listing ext/sim/gpc/gen/fakeipl.lst
```
