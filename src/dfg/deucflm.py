#!/usr/bin/env python3
#
# Link the DEUCFLM critical-format load module image from CON80/CFSYSIN.
#
# DEUCFLM holds the critical display backgrounds resident in DEU memory.
# MMUSYS5H stages it onto the MMU with `LOADMOD,MEMBER=DEUCFLM`.  The
# image is CFBSIZE+1 halfwords, laid out from ORIGIN (0x0100):
#
#   offset from ORIGIN      contents
#   ----------------------  --------------------------------------------
#   0 .. CFITSIZE-3         CFIT slots: one BRANCH FCW per background
#   CFITSIZE-2, CFITSIZE-1  the exit stub -- see `exit_stub`
#   CFITSIZE ..             the bodies, in CRTFMTCU first-use order
#   .. CFBSIZE-1            PAD fill
#   CFBSIZE                 checksum word
#
# A display deck's DEULOC= is its background's slot address, ORIGIN+slot.
# SPARE slots, and slots past the CRTFMTCU list, branch to the shared
# #PXD0000 "NO CFMT BKGD" body.
#
# The DEU IPL loads CFBSIZE+1 halfwords, waits 70 ms for "DCP CHECKSUM
# PROCESSING", then reads the DEU BITE "CRITICAL FORMAT CHECKSUM ERROR"
# bit: the DEU sums all but the last word and compares it to the last.
# The routine is undocumented and no built DEUCFLM is available to check
# against; this uses SUM(data) % 0xFFFF.
#
import re
import struct
from pathlib import Path
from typing import List, Optional

import typer

from ap101Utils.csectImage import csect_words
from . import fcw

SPARE = "#PXD0000"    # the "NO CFMT BKGD" body linked into SPARE slots
EXIT_WORDS = 2        # CFIT halfwords the exit stub takes, at the table's end


def parse_cfsysin(path):
    slots = []
    params = {
        "ORIGIN":   0x0100,     # image start (DEU address of the CFIT)
        "CFITSIZE": 0x0020,     # CFIT slots
        "CFBSIZE":  0x0E48,     # image halfwords before the checksum word
        "PAD":      0x111E,     # fill word (bodies-end .. CFBSIZE), and the
                                # terminator every body ends on
        "CFITDBA":  0x19EE,     # where the exit stub sends a finished body
        "CRTFMTLM": "DEUCFLM",  # load module name
    }
    for line in open(path, errors="replace").read().splitlines():
        code = line[:72].strip().rstrip(";")
        for m in re.finditer(r"(\w+)=(\(([^)]*)\)|\w+)", code):
            key, val, group = m.groups()
            if key == "CRTFMTCU":
                slots += [SPARE if t.strip() == "SPARE" else t.strip()
                          for t in group.split(",")]
            elif key == "CRTFMTLM":
                params[key] = val
            else:
                params[key] = int(val, 16)
    slots += [SPARE] * (params["CFITSIZE"] - EXIT_WORDS - len(slots))
    return slots, params


def member_words(csect, libdirs):
    stem = csect[2:] if csect.startswith("#P") else csect
    for d in libdirs:
        p = Path(d, stem + ".obj")
        if p.is_file():
            return csect_words(p, name=csect)
    raise FileNotFoundError(
        "cannot find %s.obj (csect %s) on %s"
        % (stem, csect, ":".join(str(d) for d in libdirs)))


def exit_stub(params):
    """The last two CFIT halfwords: where a background body goes when it is
    done.  Every body ends on the branch word 0x111E, "EXIT FROM CRITICAL
    FMT AREA TO DYNAMICS" in the #PXD0000 compool's commentary, and PAD is
    that same word.  0x111E addresses the halfword two before the end of a
    CFIT that starts at 0x0100 and is 0x20 long.

    A branch word carries twelve address bits and reaches within the 4K it
    is running in; the backgrounds are in the low 4K and the display in the
    high one.  The pair is therefore a zero-count SUBLIST carrying CFITDBA's
    4K sector, then the branch word for the twelve bits below it -- the
    sector-qualified jump the display list uses to get here.

    CFITDBA (0x19EE) is the display header, where the dynamic part of every
    display begins; a display with no external background reaches it by
    falling through its static section into the same address."""
    dba = params["CFITDBA"]
    return [0x2000 | ((dba >> 12) << 8), 0x1000 | (dba & 0x0FFF)]


def build(slots, params, bodies):
    """Lay out the image from ORIGIN: the CFIT, the bodies (CRTFMTCU
    first-use order), PAD fill to CFBSIZE and the trailing checksum word.
    `bodies` maps each unique slot csect to its body words."""
    origin = params["ORIGIN"]
    cfitsize = params["CFITSIZE"]
    if len(slots) > cfitsize - EXIT_WORDS:
        raise ValueError("CRTFMTCU lists %d slots > CFITSIZE %d less the "
                         "%d-halfword exit stub"
                         % (len(slots), cfitsize, EXIT_WORDS))
    # PAD is the terminator every body ends on, and is the branch to the
    # stub: that ties PAD, ORIGIN and CFITSIZE together.
    exit_addr = origin + cfitsize - EXIT_WORDS
    if params["PAD"] != int(fcw.Branch(exit_addr)):
        raise ValueError("PAD %04X is not the branch %04X to the exit stub "
                         "at %04X"
                         % (params["PAD"], int(fcw.Branch(exit_addr)),
                            exit_addr))

    addr = origin + cfitsize
    body_addr = {}
    body_words = []
    for m in dict.fromkeys(slots):
        body_addr[m] = addr
        body_words += bodies[m]
        addr += len(bodies[m])

    # A short CRTFMTCU leaves unnamed slots, which take PAD: a display
    # whose DEULOC= names one draws no background.
    table = ([int(fcw.Branch(body_addr[m])) for m in slots]
             + [params["PAD"]] * (cfitsize - EXIT_WORDS - len(slots))
             + exit_stub(params))
    image = table + body_words

    cfbsize = params["CFBSIZE"]
    if len(image) > cfbsize:
        raise ValueError("image %d halfwords exceeds CFBSIZE %d"
                         % (len(image), cfbsize))
    image += [params["PAD"]] * (cfbsize - len(image))
    image.append(sum(image) & 0xFFFF)
    return image


app = typer.Typer(add_completion=False, rich_markup_mode=None,
                  pretty_exceptions_enable=False)


@app.command()
def main(
    cfsysin: Path = typer.Argument(..., exists=True, dir_okay=False,
        help="CFSYSIN member (CRTFMTCU list + layout params)."),
    libdir: List[Path] = typer.Option(..., "-L", "--libdir",
        help="Directory searched for the members' <name>.obj object modules."),
    output: Optional[Path] = typer.Option(None, "--output", "-o",
        help="Output file (default: <CRTFMTLM>.bin, e.g. DEUCFLM.bin)."),
) -> None:
    """Link the DEUCFLM critical-format image from CFSYSIN + object modules."""
    slots, params = parse_cfsysin(cfsysin)
    try:
        bodies = {m: member_words(m, libdir) for m in dict.fromkeys(slots)}
        image = build(slots, params, bodies)
    except (ValueError, KeyError, FileNotFoundError) as e:
        typer.echo("error: %s" % e, err=True)
        raise typer.Exit(1)
    out = output or Path(params["CRTFMTLM"] + ".bin")
    out.write_bytes(struct.pack(">%dH" % len(image), *image))


if __name__ == "__main__":
    app()
