# tools - misc toolchain tools

Smaller CLI utilities that 

| module                 | entry point           | purpose                                             |
|------------------------|-----------------------|-----------------------------------------------------|
| `fcmcmp`               | `fcmcmp` / `-m`       | compare two FCM images halfword-by-halfword         |
| `objdump`              | `ibmobjdump` / `-m`   | dump an IBM object module (ESD/TXT/RLD)             |
| `rldanalyze`           | `-m tools.rldanalyze` | RLD relocation analysis                             |
| `extract_fcmcmp_diffs` | `-m`                  | pull diffs out of an fcmcmp JSON report             |
| `dfginput`             | `-m tools.dfginput`   | recover DFG source from a DFG output COMPOOL member |
| `mmu2fcm`              | `-m tools.mmu2fcm`    | compose phase `.lib`s into a configuration `.fcm`   |
| `mmu2mmv`              | `-m tools.mmu2mmv`    | lay the built phases onto a mass memory tape volume |
| `mmubuild`             | `-m tools.mmubuild`   | MMU load-build cards: parser, expansion tree, `DIRECTRY` tables |

## mmu2mmv — the mass memory tape

`mmu2mmv` joins the MMU load-build cards to the linked phase modules under
`build/mmu`, and writes a `.mmv` volume for the simulator's mass memory
unit (`ext/sim/mmu`, `MMU.sh run --volume`).

```
python3 -m tools.mmu2mmv --report                      # the tape map
python3 -m tools.mmu2mmv --out build/mmu/mmu.mmv       # write it
```
