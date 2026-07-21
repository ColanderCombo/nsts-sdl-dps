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
#   compool   — resolve a compool variable's type from its PASS3 SDF
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


def _sdf_hint(name):
    """' [no SDF member for INCLUDE(s): ...]' when the deck INCLUDEs
    compool(s) absent from the SDF library, else ''.  Kept on the refusal's
    own line: a missing SDF is the usual root cause of a w0 / rate-count
    refusal, and the build harness surfaces only the last stderr line."""
    try:
        from .deck import encodable_directives
        from .resolve import missing_sdf_includes
        missing = missing_sdf_includes(encodable_directives(name))
    except Exception:
        return ""
    if not missing:
        return ""
    return " [no SDF member for INCLUDE(s): %s]" % ", ".join(missing)


def generate(
    name: str = typer.Argument(..., help="Display name (e.g. CG3011) or deck path."),
    output: Optional[Path] = typer.Option(None, "--output", "-o",
                                          help="Write output here instead of stdout."),
    sdflib: Optional[Path] = typer.Option(None, "--sdflib",
                                          help="PASS3 SDF library directory "
                                          "(compool type resolution) "
                                          "[default: $DFG_SDFLIB or the "
                                          "OI340600 build's gen/SDFLIB]."),
    deck_root: Optional[Path] = typer.Option(None, "--deck-root",
                                             help="OI root holding SSSRC/APPLSRC."),
    amt: bool = typer.Option(False, "--amt",
                             help="Treat NAME as an AMT-mode deck (PMF=/AMTx= "
                             "cards -> CDA_Pnn_AMT moding-table compool). "
                             "Auto-detected from the deck when omitted."),
) -> None:
    """Translate display NAME into HAL/S COMPOOL source."""
    if sdflib:
        compool.set_sdflib(str(sdflib))
    if deck_root:
        import os
        os.environ["DFG_DECK_ROOT"] = str(deck_root)

    # AMT mode: the CDAPnn decks are moding-table inputs, not display decks.
    # Purely syntactic (no SDF), so it branches before display encoding.
    from .deck import resolve_deck
    from . import amt as amtmod
    path = resolve_deck(name)
    if path is not None and (amt or amtmod.is_amt_deck(path)):
        try:
            text = amtmod.generate(path)
        except amtmod.AmtError as e:
            typer.echo("error: cannot generate AMT %s — %s" % (name, e),
                       err=True)
            raise typer.Exit(1)
        if output is None:
            sys.stdout.write(text)
        else:
            Path(output).write_text(text)
        return

    try:
        enc = encode(name)
        text = to_hal(enc)
    except FileNotFoundError as e:
        typer.echo("error: %s" % e, err=True)
        raise typer.Exit(2)
    except Error as e:
        typer.echo("error: cannot generate %s — %s%s"
                   % (name, e, _sdf_hint(name)), err=True)
        raise typer.Exit(1)
    u = n_untyped(enc)
    if u:
        # A BIT(16) PADR against a typed referent is PASS2-fatal
        # (DI108/DI109) — refusing here beats emitting broken HAL.
        typer.echo("error: cannot generate %s — %d PADR pointer(s) have no "
                   "SDF-resolvable referent type%s"
                   % (name, u, _sdf_hint(name)), err=True)
        raise typer.Exit(1)
    if output is None:
        sys.stdout.write(text)
    else:
        Path(output).write_text(text)


def main():
    typer.run(generate)


if __name__ == "__main__":
    main()
