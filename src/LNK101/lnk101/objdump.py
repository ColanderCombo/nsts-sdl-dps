#!/usr/bin/env python3
#
# Dump IBM AP-101 object files
#

import sys
from pathlib import Path
from typing import Annotated

import typer

from .addr import Addr, AddrDisp
from .readObject101S import readObject101S, bytearrayToAscii, bytearrayToInteger

app = typer.Typer(
    help="Dump IBM AP-101S object files (.obj) in human-readable form.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)

_SYM_TYPES = ["SD", "LD", "ER", "PC", "CM", "XD/PR", "WX",
              "SD(Q)", "PC(Q)", "CM(Q)"]

_RLD_TYPE_NAMES = {
    0x00: "YCON",
    0x04: "ZCON/code",
    0x10: "ACON",
    0x20: "BSR-only",
    0x40: "DSR-only",
    0x50: "ZCON/data",
}


def _rld_type(flags):
    ft = flags & 0x7F
    typ = (flags >> 4) & 0x07
    name = _RLD_TYPE_NAMES.get(ft)
    if name is None:
        name = f"type={ft:02X}"
    sign = "-" if flags & 0x80 else "+"
    return f"{name}({sign})"


def dump_obj(filename, hex_dump=False):
    obj, symbols = readObject101S(str(filename))

    if obj[-1]["errors"]:
        for err in obj[-1]["errors"]:
            print(f"  {err}")

    esd_names = {}  # esdId -> name

    for cardNum in range(obj["numLines"]):
        line = obj[cardNum]
        typ = line["type"]

        if typ == "HDR":
            text = line.get("text", "").rstrip()
            print(f"HDR  {text}")

        elif typ == "ESD":
            firstEsdId = line.get("esdid", 1)
            for i, symKey in enumerate(["symbol1", "symbol2", "symbol3"]):
                sym = line.get(symKey)
                if sym is None:
                    continue
                esdId = firstEsdId + i
                name = sym.get("name", "").strip()
                stype = sym.get("type", "??")
                esd_names[esdId] = name

                parts = [f"ESD  [{esdId:3d}] {stype:2s} {name:8s}"]
                if "address" in sym:
                    addr = Addr(sym["address"])
                    parts.append(f"addr={addr.x}")
                if "length" in sym:
                    length = AddrDisp(sym["length"])
                    parts.append(f"len={length.hw} hw")
                if "ldid" in sym:
                    parts.append(f"ldid={sym['ldid']}")
                print("  ".join(parts))

        elif typ == "TXT":
            esdId = line.get("esdid", 1)
            relAddr = Addr(line.get("relativeAddress", 0))
            size = line.get("size", 0)
            name = esd_names.get(esdId, f"#{esdId}")
            print(f"TXT  [{esdId:3d}] {name:8s}  addr={relAddr.x}  {size} bytes")
            if hex_dump and size > 0:
                data = line.get("data", ())
                for off in range(0, size, 16):
                    chunk = data[off:min(off + 16, size)]
                    hexstr = " ".join(f"{b:02X}" for b in chunk)
                    hw_addr = Addr(line.get("relativeAddress", 0) + off)
                    print(f"       {hw_addr.x}: {hexstr}")

        elif typ == "RLD":
            size = line.get("size", 0)
            lineData = line["lineData"]
            j = 0
            prevRelId = prevPosId = 0
            while j < size:
                rec = lineData[16 + j : 16 + j + 8]
                if j > 0 and (lineData[16 + j - 4] & 1 if j >= 4 else False):
                    # Continuation: short form
                    flags = rec[0]
                    addr = Addr(bytearrayToInteger(rec[1:4]))
                    relId, posId = prevRelId, prevPosId
                    j += 4
                else:
                    relId = bytearrayToInteger(rec[:2])
                    posId = bytearrayToInteger(rec[2:4])
                    flags = rec[4]
                    addr = Addr(bytearrayToInteger(rec[5:8]))
                    prevRelId, prevPosId = relId, posId
                    j += 8

                rname = esd_names.get(relId, f"#{relId}")
                pname = esd_names.get(posId, f"#{posId}")
                rtype = _rld_type(flags)
                print(f"RLD  {rtype:12s}  {rname:8s} -> {pname:8s}"
                      f"  addr={addr.x}  flags={flags:02X}")

        elif typ == "SYM":
            size = line.get("size", 0)
            print(f"SYM  {size} bytes of packed symbol data")

        elif typ == "END":
            entry = line.get("entryAddress")
            esdId = line.get("esdid")
            length = line.get("length")
            parts = ["END"]
            if entry is not None:
                parts.append(f"entry={Addr(entry).x}")
            if esdId is not None:
                name = esd_names.get(esdId, f"#{esdId}")
                parts.append(f"esdid={esdId}({name})")
            if length is not None:
                parts.append(f"len={length}")
            print("  ".join(parts))

        else:
            print(f"???  type={typ}")

        for err in line.get("errors", []):
            print(f"  *** {err}")


@app.command()
def main(
    obj_files: Annotated[list[Path], typer.Argument(help="Object file(s) to dump",
        exists=True)],
    hex_dump: Annotated[bool, typer.Option("--hex", "-x",
        help="Include hex dump of TXT record data")] = False,
):
    """Dump IBM AP-101 object files."""
    for i, obj_file in enumerate(obj_files):
        if len(obj_files) > 1:
            if i > 0:
                print()
            print(f"=== {obj_file} ===")
        dump_obj(obj_file, hex_dump=hex_dump)


if __name__ == "__main__":
    app()
