#!/usr/bin/env python3
#
# Link the DEUCFLM critical-format load module image from CON80/CFSYSIN.
#
# DEUCFLM holds the critical display backgrounds resident in DEU memory.
# MMUSYS5H stages it onto the MMU with `LOADMOD,MEMBER=DEUCFLM`:
#
#   ORIGIN (0x0100)   CFIT -- the Critical Format Index Table: CFITSIZE
#                     one-halfword entries, each a BRANCH FCW to its
#                     background body.  SPARE slots, and slots past the
#                     CRTFMTCU list, branch to the shared #PXD0000
#                     "NO CFMT BKGD" body.  Each DEULOC= in a display deck
#                     names its background's slot address (0x100 + slot).
#
#   after the table   the bodies -- each member's csect image, packed in
#                     CRTFMTCU first-use order
#
# The image is CFBSIZE+1 halfwords: table + bodies, PAD-filled to CFBSIZE,
# plus a trailing checksum word.  AIGDEU (DEU IPL) fills exactly that and
# then waits 70 ms for "DCP CHECKSUM PROCESSING" and reads the DEU BITE
# "CRITICAL FORMAT CHECKSUM ERROR" bit: the DEU checksums all but the last
# word and compares it to the last word.
#
# We don't know the exact checksum routine and don't have a built copy
# of DEUCFLM, so we're assuming the checksum is just SUM(data) % 0xffff
#
import re
import struct
from pathlib import Path
from typing import List, Optional

import typer

from ap101Utils.csectImage import csect_words
from . import fcw

SPARE = "#PXD0000"    # the "NO CFMT BKGD" body linked into SPARE slots


def parse_cfsysin(path):
    slots = []
    params = {
        "ORIGIN":   0x0100,     # image start (DEU address of the CFIT)
        "CFITSIZE": 0x0020,     # CFIT slots
        "CFBSIZE":  0x0E48,     # image halfwords before the checksum word
        "PAD":      0x111E,     # fill word (bodies-end .. CFBSIZE)
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
    slots += [SPARE] * (params["CFITSIZE"] - len(slots))
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


def build(slots, params, bodies):
    """Lay out the image from ORIGIN: the CFIT, the bodies (CRTFMTCU
    first-use order), PAD fill to CFBSIZE and the trailing checksum word.
    `bodies` maps each unique slot csect to its body words."""
    origin = params["ORIGIN"]
    cfitsize = params["CFITSIZE"]
    if len(slots) > cfitsize:
        raise ValueError("CRTFMTCU lists %d slots > CFITSIZE %d"
                         % (len(slots), cfitsize))

    addr = origin + cfitsize
    body_addr = {}
    body_words = []
    for m in dict.fromkeys(slots):
        body_addr[m] = addr
        body_words += bodies[m]
        addr += len(bodies[m])

    table = [int(fcw.Branch(body_addr[m])) for m in slots]
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
