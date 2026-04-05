#!/usr/bin/env python3
#
# Dump IBM AP-101 object files
#

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .addr import Addr, AddrDisp
from .repro import ReproTracker, version_string
from .addrcon import rld_flag_type_name
from .readObject101S import readObject101S, bytearrayToAscii, bytearrayToInteger

def _version_callback(value: bool):
    if value:
        print(f"ibmobjdump {version_string()}")
        raise typer.Exit()


app = typer.Typer(
    help="Dump IBM AP-101S object files (.obj) in human-readable form.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)

_SYM_TYPES = ["SD", "LD", "ER", "PC", "CM", "XD/PR", "WX",
              "SD(Q)", "PC(Q)", "CM(Q)"]


def _rld_type(flags):
    name = rld_flag_type_name(flags)
    sign = "-" if flags & 0x80 else "+"
    return f"{name}({sign})"


def _print_sym_table(symbols):
    """Print the decoded SYM symbol table."""
    print(f"{'=' * 60}")
    print(f"SYM table: {len(symbols)} symbols")
    print(f"{'=' * 60}")
    for sym in symbols:
        stype = sym.get("symbolType", "?")
        name = sym.get("name", "")
        offset = sym.get("offsetInCSECT", 0)

        parts = [f"  {stype:12s}"]
        if name:
            parts.append(f"{name:8s}")
        else:
            parts.append(f"{'':8s}")
        parts.append(f"offset={offset:04X}")

        if stype == "DATA":
            dt = sym.get("dataType", "?")
            parts.append(f"type={dt}")
            if "length" in sym:
                parts.append(f"len={sym['length']}")
            if "multiplicity" in sym:
                parts.append(f"mult={sym['multiplicity']}")
            if "scale" in sym:
                parts.append(f"scale={sym['scale']}")
        if sym.get("cluster"):
            parts.append("CLUSTER")
        print("  ".join(parts))

        if "error" in sym:
            print(f"    *** {sym['error']}")
        for w in sym.get("warnings", []):
            print(f"    *** {w}")
    print(f"{'=' * 60}")


def dump_obj(filename, hex_dump=False, show_sym=False):
    obj, symbols = readObject101S(str(filename))

    if obj[-1]["errors"]:
        for err in obj[-1]["errors"]:
            print(f"  {err}")

    esd_names = {}  # esdId -> name
    sym_table_printed = False

    for cardNum in range(obj["numLines"]):
        line = obj[cardNum]
        typ = line["type"]

        # Print SYM table after the last SYM card
        if show_sym and not sym_table_printed and typ != "SYM" and symbols:
            _print_sym_table(symbols)
            sym_table_printed = True

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
                # Flags
                flags = []
                if sym.get("remote"):
                    flags.append("REMOTE")
                if sym.get("RMODE24"):
                    flags.append("RMODE24")
                elif sym.get("RMODE31ANY"):
                    flags.append("RMODE31")
                elif sym.get("RMODE64"):
                    flags.append("RMODE64")
                if sym.get("AMODE24"):
                    flags.append("AMODE24")
                elif sym.get("AMODE31"):
                    flags.append("AMODE31")
                elif sym.get("AMODEANY"):
                    flags.append("AMODEANY")
                elif sym.get("AMODE64"):
                    flags.append("AMODE64")
                if sym.get("RSECT"):
                    flags.append("RSECT")
                if flags:
                    parts.append(",".join(flags))
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
                    # Dump as halfwords (pairs of bytes)
                    hwords = []
                    for h in range(0, len(chunk), 2):
                        if h + 1 < len(chunk):
                            hwords.append(f"{chunk[h]:02X}{chunk[h+1]:02X}")
                        else:
                            hwords.append(f"{chunk[h]:02X}")
                    hw_addr = Addr(line.get("relativeAddress", 0) + off)
                    print(f"       {hw_addr.x}: {' '.join(hwords)}")

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
            print(f"SYM  {size} bytes")

        elif typ == "END":
            entry = line.get("entryAddress")
            esdId = line.get("esdid")
            length = line.get("length")
            parts = ["END"]
            if entry is not None:
                parts.append(f"entry={Addr(entry).x}")
            entryName = line.get("entryName", "").strip()
            if entryName:
                parts.append(f"entryName={entryName}")
            if esdId is not None:
                name = esd_names.get(esdId, f"#{esdId}")
                parts.append(f"esdid={esdId}({name})")
            if length is not None:
                parts.append(f"len={length}")
            translator = line.get("translator", "").strip()
            processor = line.get("processor", "").strip()
            if translator:
                parts.append(f"translator={translator}")
            if processor:
                parts.append(f"processor={processor}")
            print("  ".join(parts))

        else:
            print(f"???  type={typ}")

        for err in line.get("errors", []):
            print(f"  *** {err}")


def extract_txt(filename, out_dir):
    """Extract TXT data for each CSECT to separate .bin files in out_dir."""
    obj, _symbols = readObject101S(str(filename))

    # Collect ESD names and section lengths
    esd_names = {}
    esd_lengths = {}
    for cardNum in range(obj["numLines"]):
        line = obj[cardNum]
        if line["type"] == "ESD":
            firstEsdId = line.get("esdid", 1)
            for i, symKey in enumerate(["symbol1", "symbol2", "symbol3"]):
                sym = line.get(symKey)
                if sym is None:
                    continue
                esdId = firstEsdId + i
                esd_names[esdId] = sym.get("name", "").strip()
                if "length" in sym and sym.get("type") in ("SD", "PC"):
                    esd_lengths[esdId] = sym["length"]

    # Accumulate TXT data per CSECT
    sections = {}  # esdId -> bytearray
    for cardNum in range(obj["numLines"]):
        line = obj[cardNum]
        if line["type"] != "TXT":
            continue
        esdId = line.get("esdid", 1)
        relAddr = line.get("relativeAddress", 0)
        size = line.get("size", 0)
        data = line.get("data", ())
        if size == 0:
            continue

        if esdId not in sections:
            length = esd_lengths.get(esdId, 0)
            sections[esdId] = bytearray(max(length, relAddr + size))
        buf = sections[esdId]
        # Extend if needed (TXT cards may arrive out of order)
        if relAddr + size > len(buf):
            buf.extend(b'\x00' * (relAddr + size - len(buf)))
        buf[relAddr:relAddr + size] = data[:size]

    # Write files
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for esdId in sorted(sections):
        name = esd_names.get(esdId, f"ESD{esdId}")
        out_path = out_dir / f"{name}.bin"
        out_path.write_bytes(sections[esdId])
        print(f"  {name:8s}  {len(sections[esdId])} bytes -> {out_path}")


@app.command()
def main(
    obj_files: Annotated[list[Path], typer.Argument(help="Object file(s) to dump",
        exists=True)],
    hex_dump: Annotated[bool, typer.Option("--hex", "-x",
        help="Include hex dump of TXT record data")] = False,
    show_sym: Annotated[bool, typer.Option("--show-sym-table", "-s",
        help="Show decoded SYM symbol table")] = False,
    extract: Annotated[Optional[Path], typer.Option("--extract-txt",
        help="Extract each CSECT's TXT data to a .bin file in this directory"
    )] = None,
    repro: Annotated[bool, typer.Option("--repro/--no-repro",
        help="Print repro info (git version, file MD5s)")] = True,
    check_repro: Annotated[Optional[Path], typer.Option("--check-repro",
        help="Compare current run against a saved .repro.json",
        exists=True)] = None,
    version: Annotated[bool, typer.Option("--version", help="Show version",
        callback=_version_callback, is_eager=True)] = False,
):
    """Dump IBM AP-101 object files."""
    tracker = ReproTracker("ibmobjdump")
    for obj_file in obj_files:
        tracker.track(obj_file, role="input")

    for i, obj_file in enumerate(obj_files):
        if len(obj_files) > 1:
            if i > 0:
                print()
            print(f"=== {obj_file} ===")
        if extract is not None:
            extract_txt(obj_file, extract)
        else:
            dump_obj(obj_file, hex_dump=hex_dump, show_sym=show_sym)

    if repro:
        tracker.print_summary()
    if check_repro:
        tracker.print_check(check_repro)


if __name__ == "__main__":
    app()
