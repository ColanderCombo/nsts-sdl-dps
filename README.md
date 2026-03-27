
** The initial import of this repository is still in progress **

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
    - Original ASM101S assembler (modified version in src/ASM101S)
  - nsts-sim-gpc
    - gpc-batch: AP-101 batch emulator
    - gpc-dbu: AP-101 debugger
    - gpc-gui: AP-101 debugger gui

Cloning
-------
This repository references virtualagc and nsts-sim-gpc as submodules.
Symlinks throughout the tree point into these submodules, so you need
both the submodules **and** working symlink support for things to work.

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

On a debian/unbuntu system you can install the required packages using:
```
sudo apt install git cmake python3-pip pipx nodejs npm
pipx install uv
pipx insurepath 
# open a new shell to make sure 'uv' is findable
```

Building
--------

```
./GENERATE_CMAKE.sh
cd build
make
make test
```

Contents
--------

Any external repositories we use live in the `ext/` (external) tree.
Since the bits we're interested in sometimes live pretty deep inside
these trees, symlinks elsewhere in the tree point into the `ext/` tree.

  - sdl/
    - cmake/: build system definition + bin wrappers
      - BuildHALSFC.cmake
      - BuildHALProgram.cmake
      - BuildRuntime.cmake
      - InstallWrappers.cmake
      - TestHalProgram.cmake
      - Uv{,Install}.cmake
    - code/: 'original' source code 
      - HAL.HALS.RUNMAC
      - PASS.REL32V0
      - TOOLS.COMPILER.CLIST
      - TOOLS.PASS.SIMASM
      - TOOLS.SYSTEM.CLIST
    - ext/: links to external repositories.
      - virtualagc
      - nsts-sim-gpc
    - src/: 'modern' source code
      -  `ASM101S`: adapted version of virtualagc/ASM101S
      - `LNK101`: AP-101 relocating linker
      - `XCOM-I`: XPL->C translator, for compiling HALSFC
    - test/: automatic testing files
      - baselines/
      - halTests/
      - regress/
    - vscode/
      - tatsu-ebnf-vscode/: plugin for doing syntax highlighting on tatsu parser grammars.
  
