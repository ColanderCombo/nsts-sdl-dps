Toolchain & testing setup for HAL/S on the AP-101 computer
----------------------------------------------------------

This repository provides a setup for developing and testing the tools
required for writing HAL/S code targeting IBM 4Pi model AP-101 computers,
specifically the Space Shuttle Flight Computers.

Most of the source code referenced in this tree comes from other
repositories:

  - from the virtualagc project:
    - HALSFC compiler source
    - HAL/S runtime library assembler source
    - HAL/S example/test/benchmark programs
    - XCOM-I XPL->C translator package
    - Original ASM101S assembler (heavily modified version in src/asm101)
  - nsts-sim-gpc
    - `gpc run`: AP-101 batch emulator
    - `gpc debug`: AP-101 debugger (REPL)
    - `gpc gui`: AP-101 debugger GUI
  - Halmat: HALMAT intermediate-code tooling

Cloning
-------
This repository references virtualagc, nsts-sim-gpc, and Halmat as
submodules.  Symlinks throughout the tree point into these submodules, so
you need both the submodules **and** working symlink support for things
to work.

```
git clone --recurse-submodules https://github.com/ColanderCombo/nsts-sdl-dps
```

If you already cloned without `--recurse-submodules`:

```
git submodule update --init --recursive
```

### Windows users

Git on Windows does not create symlinks by default.  You must either
enable [Developer Mode](https://learn.microsoft.com/en-us/windows/apps/get-started/enable-your-device-for-development)
or run your shell as Administrator, and then clone with:

```
git clone -c core.symlinks=true --recurse-submodules https://github.com/ColanderCombo/nsts-sdl-dps
```

If you've already cloned without symlinks, the easiest fix is to
re-clone with the flag above.  Alternatively:

```
git config core.symlinks true
git checkout -- .
```

Prerequisites
-------------

  - cmake >= 3.28
  - python3 (3.13 used here)
  - [uv](https://github.com/astral-sh/uv)
  - nodejs (v18.19.1 tested) & npm

On a debian/ubuntu system you can install the required packages using:
```
sudo apt install git cmake python3-pip pipx nodejs npm
pipx install uv
pipx ensurepath
# open a new shell to make sure 'uv' is findable
```

Building
--------

```
./GENERATE_CMAKE.sh     # cmake -S . -B build (Release)
cd build
make world
make check
```

Build targets:

  - `make` — just the Python venv and the asm101/lnk101 tool wrappers
    (fast)
  - `make world` — everything: the HALSFC compiler, the runtime library,
    the HAL/S test programs, and the simulator
  - `make check` — build all test dependencies, then run ctest
  - `make halsfc` / `make runtime` / `make gpc-sim` — individual pieces
  - `make gpc-pkg` — the simulator plus its electron-builder package

Tests can also be run directly with `ctest --test-dir build` (add `-R
<pattern>` for a subset, `-j8` to parallelize).  Built tools land in
`build/bin/` (`halsc`, `asm101`, `lnk101`, `con80build`, `mmu2fcm`,
`mafgen`, `gpc`, `fcmcmp`, ...).

Contents
--------

Any external repositories we use live in the `ext/` (external) tree.
Since the bits we're interested in sometimes live pretty deep inside
these trees, symlinks elsewhere in the tree point into the `ext/` tree.

  - sdl/
    - cmake/: build system definition + bin wrapper templates
    - code/: source trees, mostly symlinks into `ext/virtualagc`
      - HAL.HALS.RUNMAC: runtime library macro source
      - PASS.REL32V0: HALSFC compiler source
      - TOOLS.{COMPILER,SYSTEM}.CLIST, TOOLS.PASS.SIMASM
      - BENCH: benchmark HAL/S programs
    - ext/: external repositories
      - virtualagc
      - sim (nsts-sim-gpc)
      - halmat
    - src/: 'modern' source code
      - `asm101`: AP-101 assembler (adapted from virtualagc/ASM101S)
      - `lnk101`: AP-101 relocating linker + cross-phase resolution
      - `ap101Utils`: shared modules (object model, address/ZCON
        handling, CON80 deck parsing, link-order pins, ...)
      - `con80`: con80build, the CON80-deck-driven tree builder
      - `dfg`: Display Format Generator
      - `mafgen`: DASS-style listing regeneration
      - `tools`: object-file and listing utilities (fcmcmp,
        ibmobjdump, mmu2fcm, mmubuild, delist, ...)
      - `XCOM-I`: link to virtualagc/XCOM-I
    - test/: automatic testing (ctest); baselines/, halTests/, unit/,
      regress/, fcmcmp/, fpasm/, asm101/, in5/, srcTest/
    - tools/: analysis and data-recovery scripts (floating point,
      stack analysis, listings)
    - vscode/
      - nsts-dps-lang/: VS Code extension for:
          HAL/S, DFG, AP-101S assembler, CONCARD, MMUBUILD

Building a source tree with con80build
--------------------------------------

`con80build` builds a complete software image from a conventionally 
laid out source tree, driven by the tree's own CON80 linkedit control 
deck.  A tree looks like:

```
<tree>/
  CON80/     linkedit control decks (the build's structure lives here)
  SSSRC/     assembler + HAL/S source members
  APPLSRC/   application source members (searched after SSSRC)
  MLIB80/    asm101 macro / COPY library
  INCL80/    HAL inclusion library
```

and may have a sibling `gen/<tree>/` overlay for generated or
preprocessed members — members there override same-named members in the
tree, so the source tree itself is never modified.  If the overlay
contains a `linkorder.json` pins file, its link orderings are picked 
up automatically.

To build one link target — resolve the deck, assemble the ASM members,
compile the HAL members in template order, build the display decks, and
link the result:

```
build/bin/con80build --root code/OI340600 --out build/OI340600 \
    --assemble --hal --display --link SSW
```

This produces `build/OI340600/SSW.fcm` (plus `SSW.lib`, a `sym.json`
symbol sidecar, and per-member objects under `obj/`).  The `.fcm` runs
in the simulator (`build/bin/gpc run`/`debug`).

For a deck whose master member defines multiple MMU phases:

```
build/bin/con80build --root code/OI340600 --out build/OI340600 --phase 4
build/bin/con80build --root code/OI340600 --out build/OI340600 --build-all
```

`--phase N` builds one phase (its load module lands at
`<out>/PHASE0N.lib`, with MAP dependencies resolved from the earlier
phases' modules); `--build-all` builds every phase in master-deck order
and then cross-resolves the load modules (`--resolve-phases` runs that
step alone).  `build/bin/mmu2fcm` then composes phase load modules into
a single memory image.

Useful extras: `--plan` resolves and classifies the worklist without
building anything, `--emit-cmake FILE` writes a standalone CMake recipe
for the same worklist, and `-e` halts at the first failing member.

### Inspecting the MMU build deck: mmubuild

A tree's CON80 directory also carries the mass-memory build deck, rooted
at the `MMXMEM` member.  `mmubuild` parses and reports it:

```
build/bin/mmubuild code/OI340600/CON80              # member expansion tree
build/bin/mmubuild code/OI340600/CON80 --alloc      # ALLOCs grouped by SYSID
build/bin/mmubuild code/OI340600/CON80 --loadmods   # LOADMOD -> linkedit members
build/bin/mmubuild code/OI340600/CON80 --orphans    # members not reached from the root
```

`--statements` dumps the parsed statements (optionally `--member NAME`
for one member), and `--dot`/`--json` emit the tree for other tools.

### Composing a memory configuration: mmu2fcm

After `con80build --build-all` has produced the per-phase load modules,
`mmu2fcm` composes them into a single memory image the way they'd be
loaded into the GPC's at runtime, duplicating the IPL + application
loading process. Phases loaded in the configuration's order,
later phases overlaying earlier ones:

```
build/bin/mmu2fcm --mmu build/OI340600 --con80 code/OI340600/CON80 \
    --config SSW --system-id 29.01A.01B --iload-id 29.1.0.00.0B
```

The memory-configuration registry (names like SSW, G9, S2, ... and their
phase load orders) comes from the CON80 deck itself; `--list-configs`
prints it, and `--phases 10,2,13` overrides the preset order.  The
composed image lands under `<mmu>/<config>/` and runs in the simulator
like any other `.fcm`.  `--system-id` and `--iload-id` stamp the
EBCDIC-coded System and I-Load Identifier words a loader writes at low
core (halfwords 0x1C and 0x20) — they are load-time values, not part of
any load module, so omitting them leaves the words zero.
`--stamp-phase-tables` additionally generates and stamps the
mass-memory phase tables into the image.

### Disassembling/Listing a build image: mafgen

MAFGEN was a period program that printed a build's memory map and
annotated disassembly "DASS" listings.  Our `mafgen` attempts to 
duplicate this functionality to generate a .ASC that can be used
to browse the contents of the image or to compare images

```
build/bin/mafgen SSW --mmu build/OI340600 -o build/mafgen/SSW.ASC
```

Three sections come out (`--sections index,extent,dump`): the
alphabetical csect→address index, the address-ordered csect extent list,
and the memory dump — AP-101 disassembly for code csects, typed `DC`
rows for HAL and assembler data, with names resolved from the phase SDF
libraries and the assembler's `.asmg.json` sidecars.

Our mafgen doesn't yet output *every* section output by the original
MAFGEN, and doesn't currently output page headers/footers.  If you
wanted to ensure two .ASC files are diff'able, you can use `--strip FILE` 
to normalize FILE to be in our expected format.

Given a second FILE.ASC in normal format,  `--score FILE` generates 
and then reports the exact-line match rate per line class 
(index / header / code / data / other).

### Generating display compools: dfg

Display decks compile through generated HAL/S: `dfg` translates a
display deck into its COMPOOL source, resolving referenced compool
types against the SDF library a `con80build --hal` run leaves at
`<out>/gen/SDFLIB`:

```
build/bin/dfg CG3011 --deck-root code/OI340600 \
    --sdflib build/OI340600/gen/SDFLIB -o CG3011.hal
```

An AMT-mode deck (PMF=/AMTx= cards) instead produces the moding-table
compool; the mode is auto-detected, or forced with `--amt`.
`con80build --display` drives all of this automatically for every
display deck the CON80 deck pulls in — the standalone command is for
inspecting or iterating on a single display.

### I-loads: extraction, UPF decks, and patching

I-loads are per-mission initialization values applied after the
main build.
`tools/iload/` covers the round trip:

`upfgen` harvests the I-load population (the PSFIN catalog's `D ILD;`
V-cards name it) and their values from a tree's reference listings,
maps each cell to its phase / load block / offset through the built
phase load modules, and emits a Universal Patch Format (UPF) card deck
as both an ASCII deck and an EBCDIC 20-cards-per-block tape image:

```
build/venv/bin/python tools/iload/upfgen.py \
    --pfs <root holding the tree and its reference listings> \
    --mmu build/OI340600 --out build/iload --configs G16
```

`upfapply` is the patch step: it applies a UPF deck to a composed
memory image.  Patches address by phase / load block / offset — 
the tape's own addressing, where a load block is a TEXT extent
of the phase's `.lib` — so application is correct even when the build's
absolute layout differs from the deck's; each card's AP101 ADDR is
cross-checked against the resolved address and mismatches are reported
as layout drift, not applied blindly:

```
build/venv/bin/python tools/iload/upfapply.py --upf build/iload/G16.upf \
    --mmu build/OI340600 --fcm-dir build/OI340600/G16 --name G16 \
    --phases 10,2,13,3,4 --out build/OI340600/G16_ILOAD
```

### Regenerating CDQANNUN: cdqanngen

A tree's `SSSRC/CDQANNUN` member carries large generated data blocks —
the FMPT (fault message parameter table) — produced from SM
Preprocessor (SMPP) input catalogs.  `cdqanngen` (run as `python -m
tools.cdqanngen` from the build venv) round-trips them: `extract` pulls
a definitions JSON out of a source member, `generate` rebuilds the
member from definitions plus a template, and `diff` compares two
definitions JSONs.  The SMPP input side is covered too: `mkcatalog`
derives SMPP input catalogs from a definitions JSON, `fmpt` rebuilds
the member's FMPT blocks from catalogs plus a template, and `validate`
reports catalog coverage against the definitions.
