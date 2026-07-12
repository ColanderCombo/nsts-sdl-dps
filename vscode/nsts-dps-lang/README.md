# NSTS DPS Languages

VS Code language support for the Space Shuttle **Data Processing System (DPS)** —
the subsystem comprising the AP-101 General Purpose Computers, their connected
hardware, and all of the flight and support software (including the Primary
Flight Software, PFS).

## Languages

| Language                  | ID         |
| ------------------------- | ---------- |
| HAL/S                     | `hals`     |
| Display Format Generator  | `dfg`      |
| AP-101S Assembler         | `ap101asm` |
| Link-editor control cards | `concard`  |
| MMU build cards           | `mmubuild` |

## Revision coloring

The punched-card formats (HAL/S, DFG, AP-101S, CONCARD, MMU) carry an 8-column
trailer — a 6-digit sequence number plus a 2-letter revision code. The extension
colors each line's trailer by revision (git-blame style) so edits from the same CR
stand out. Implemented once in `src/revisionDecorations.ts` and shared by all five.

## EBCDIC Decorations

HAL/S source code is nominally written in EBCDIC and uses two non-ASCII 
characters throughout; 
  - The cent symbol `¢` is used as an escape character and is translated
    to ASCII as a backtick.
  - The not symbol `¬` is used for logical 'not' and is translated to a 
    tilde '~' (exclamation '!' is also accepted by the compiler)
    
This extension uses vscode decorations to display `¢` and `¬` characters when
it sees backtick and `~` 

## Language servers

- **HAL/S** — a Python LSP server bundled under `server/`. Configure the
  interpreter with `hals.pythonPath`; override the server with `hals.serverPath`.
- **AP-101S** — an optional external HLASM server (OCaml). Point `ap101asm.serverPath`
  at the binary to enable diagnostics/hover; syntax highlighting works without it.

## Building

```sh
npm install
npm run compile      # tsc -> out/
```

## Licensing

The extension is licensed Apache-2.0.  The vendored HAL/S language server
from [Zaneham/hals-lsp](https://github.com/Zaneham/hals-lsp) is Apache-2.0.
See `LICENSE` and `THIRD-PARTY.md`.
