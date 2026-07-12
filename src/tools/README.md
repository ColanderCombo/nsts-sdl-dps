# tools - misc toolchain tools

Smaller CLI utilities that 

| module                 | entry point           | purpose                                             |
|------------------------|-----------------------|-----------------------------------------------------|
| `fcmcmp`               | `fcmcmp` / `-m`       | compare two FCM images halfword-by-halfword         |
| `objdump`              | `ibmobjdump` / `-m`   | dump an IBM object module (ESD/TXT/RLD)             |
| `rldanalyze`           | `-m tools.rldanalyze` | RLD relocation analysis                             |
| `extract_fcmcmp_diffs` | `-m`                  | pull diffs out of an fcmcmp JSON report             |
| `dfginput`             | `-m tools.dfginput`   | recover DFG source from a DFG output COMPOOL member |
