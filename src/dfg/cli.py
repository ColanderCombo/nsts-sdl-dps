#!/usr/bin/env python3
#
# `DFG` — the Display Format Generator
#
# Reimplementation of the preprocessor that translates a DFG display deck
# (HEADER=/STAT/XC=/CHAR=/VARY/VPARM=/...) into the `#P<name>` HAL/S COMPOOL
# holding everything needed to display and process a UI page on the DEU
# (or MEDS/IDP): the page's background format, its cyclic updates from
# variables in other COMPOOLs, and the keyboard table for ITEM+value+EXEC
# input parsing/validation.
#
# The COMPOOL is consumed by the FCOS "CRT Interface" functions
# (IBM-77-SS-3576 "ALT System Software Design Specification"):
#
#   DMC_NEW_DISPLAY     3.3.1.1  display call-up; sends the static FCWs
#   DCI#CYC             3.3.1.2  per-cycle update loop over the DEUs
#   DCI#FMT             3.3.1.3  steps the DDT, generating FCWs to send
#   keyboard interface  3.1.1    consumes the KVT
#
# #P<name> COMPOOL layout: header, Keyboard/Value Table (KVT), static FCWs,
# Dynamic Data Table (DDT).  A dynamic field pointing at a live variable is
# emitted as a typed `NAME(var)` initializer (a PADR) the compiler resolves;
# every other word is a HEX'...' literal.
#
# Modules:
#   deck      — read a display deck into ordered directives
#   compool   — resolve a compool variable's type from its binary TEMPLATE
#   fcw       — Function Control Word primitives (coordinates, glyphs, vectors)
#   kvt       — the keyboard/value table (item entries + limit tables)
#   static    — the static (background) FCW section
#   ddt       — DDT sequencing + flow/rate resolution; the directive→opcode
#               table is `ops`
#   emit      — render the encoded compool as HAL/S source
#   deucflm   — link the DEUCFLM critical-format load module
#
import sys
from pathlib import Path
from typing import Optional

import typer

from . import compool
from .encode import encode, Error
from .emit import to_hal, n_untyped


def generate(
    name: str = typer.Argument(..., help="Display name (e.g. CG3011) or deck path."),
    output: Optional[Path] = typer.Option(None, "--output", "-o",
                                          help="Write output here instead of stdout."),
    templib: Optional[Path] = typer.Option(None, "--templib",
                                           help="Compiler TEMPLIB directory."),
    deck_root: Optional[Path] = typer.Option(None, "--deck-root",
                                             help="OI root holding SSSRC/APPLSRC."),
) -> None:
    """Translate display NAME into HAL/S COMPOOL source."""
    if templib:
        compool.set_templib(str(templib))
    if deck_root:
        import os
        os.environ["DFG_DECK_ROOT"] = str(deck_root)
    try:
        enc = encode(name)
        text = to_hal(enc)
    except FileNotFoundError as e:
        typer.echo("error: %s" % e, err=True)
        raise typer.Exit(2)
    except Error as e:
        typer.echo("error: cannot generate %s — %s" % (name, e), err=True)
        raise typer.Exit(1)
    u = n_untyped(enc)
    if u:
        typer.echo("warning: %d PADR pointer(s) fell back to NAME BIT(16) "
                   "(missing template)" % u, err=True)
    if output is None:
        sys.stdout.write(text)
    else:
        Path(output).write_text(text)


def main():
    typer.run(generate)


if __name__ == "__main__":
    main()
