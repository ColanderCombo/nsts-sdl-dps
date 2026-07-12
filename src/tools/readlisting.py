#!/usr/bin/env python3
'''
License:    This program is declared by its author, Ronald Burkey, to be the 
            U.S. Public Domain, and may be freely used, modifified, or 
            distributed for any purpose whatever.
Filename:   readlisting.py
Purpose:    Reads a legacy AP-101S assembly-listing report and extracts the
            object code it contains, by control section.  Run standalone it
            prints a hex dump of each section; `readListing()` is also importable
            as a library.  (Originally the data source for the assembler's
            now-removed --compare cross-check.)
Contact:    info@sandroid.org
Refer to:   https://www.ibiblio.org/apollo/ASM101S.html
History:    2024-10-13 RSB  Began.
            2026-06-22 DAS  Moved from asm101s to tools as a standalone program.
'''

import os
import sys
from typing import Annotated

os.environ["TYPER_USE_RICH"] = "0"  # disable fancy formatting
import typer

# Upon error, returns `None`.  Upon success, returns a dictionary whose keys
# are CSECT name, and whose values are themselves dictionaries representing the 
# control sections.  The dictionaries are structured as follows:
#    "pos"        Current halfword address.
#    "memory"     List of memory values, starting from byte address 0.  The
#                 values are integers in the range 0-255, or `None` at locations
#                 not used so far.
  
# While the numerical values in such lists are generally
# integers in the range 0-255, the value `None` may also appear for locations
# which are uninitialized.
chunkSize = 4096
def readListing(filename):
    
    sects = {}
    
    try:
        f = open(filename, "rt")
        source = f.readlines()
        f.close()
    except:
        return None
    
    sect = None
    for line in source:
        if len(line) > 20 and line[20] == " ":
            front = line[:21]
        else:
            front = line[:30]
        back = line[36:36+71]
        col1 = back[:1]
        if col1 == "*":
            continue
        
        # Get the hex values related to the object code.
        frontFields = front.split()
        if len(frontFields) == 0 or len(frontFields[0]) != 5:
            continue
        try:
            address = int(frontFields[0], 16)
            address *= 2  # To get a byte address.
        except:
            continue
        values = []
        error = False
        for field in frontFields[1:]:
            if 0 != (len(field) & 1):
                error = True
                break
            for i in range(0, len(field), 2):
                try:
                    values.append(int(field[i:i+2], 16))
                except:
                    error = True
                    break
            if error:
                break
        if error:
            continue
        
        # Is this a line that affects the what control section we're in?
        backFields = back.split()
        if len(backFields) == 0:
            continue
        if len(backFields) > 0 and col1 == " ":
            name = None
            operation = backFields[0]
        elif len(backFields) > 1 and col1 != " ":
            name = backFields[0]
            operation = backFields[1]
        if operation == "DSECT":
            sect = name
            continue
        elif operation == "CSECT" or (sect == None and len(values) > 0):
            if operation != "CSECT":
                name = ""
            if name not in sects:
                sects[name] = {
                    "pos": 0,
                    "memory": [None]*chunkSize
                    }
            sect = name
            #address *= 2
            #sects[sect]["pos"] = address
            #sects[sect]["memory"] = [None]*chunkSize
            continue
        
        # Now actually add the stuff to memory.
        for value in values:
            while address >= len(sects[sect]["memory"]):
                sects[sect]["memory"] = sects[sect]["memory"] + ([None]*chunkSize)
            if sects[sect]["memory"][address] not in [None, value]:
                print(f"Memory overwritten: {sect},{address:05X} "
                      f"{sects[sect]['memory'][address]:02X}->{value:02X}")
            sects[sect]["memory"][address] = value
            address += 1
    
    for sect in sects:
        memory = sects[sect]["memory"]
        for n in range(len(memory)-1, -1, -1):
            if memory[n] == None:
                memory.pop()
            else:
                break
    return sects


app = typer.Typer(
    help="Extract object code from a legacy AP-101S assembly listing.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@app.command()
def main(
    listing: Annotated[str, typer.Argument(
        help="legacy AP-101S assembly-listing report")],
) -> None:
    """Extract object code from a listing and hex-dump it by CSECT
       (one 16-byte row per line; '..' marks an unfilled byte)."""
    sects = readListing(listing)
    if sects is None:
        print(f"readlisting: could not read {listing!r}", file=sys.stderr)
        raise typer.Exit(1)

    for name, sect in sects.items():
        memory = sect["memory"]
        label = name if name else "(unnamed)"
        print(f"=== CSECT {label} ({len(memory)} byte(s)) ===")
        for base in range(0, len(memory), 16):
            row = memory[base:base + 16]
            hexes = " ".join(".." if b is None else f"{b:02X}" for b in row)
            print(f"  {base:06X}: {hexes}")


if __name__ == "__main__":
    app()

