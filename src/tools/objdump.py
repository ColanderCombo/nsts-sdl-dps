#!/usr/bin/env python3
#
# Dump IBM AP-101 object files
#

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from ap101Utils.addr import Addr, AddrDisp
from lnk101.repro import ReproTracker, version_string
from ap101Utils.addrcon import AddrCon
from ap101Utils import objModule
from ap101Utils.objModule import (EsdType, EsdRecord, TxtRecord, RldRecord,
                                  SymRecord, EndRecord, ControlRecord, RldEntry)

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


def _rld_type(flags):
    con = AddrCon(flags)
    return f"{con.kind}({'-' if con.is_negative else '+'})"


def _print_sym_table(symbols):
    """Print the decoded SYM symbol table."""
    print(f"{'=' * 60}")
    print(f"SYM table: {len(symbols)} symbols")
    print(f"{'=' * 60}")
    for sym in symbols:
        stype = sym.symbolType
        name = sym.name or ""
        offset = sym.offsetInCSECT

        parts = [f"  {stype:12s}"]
        parts.append(f"{name:8s}")
        parts.append(f"offset={offset:04X}")

        if stype == "DATA":
            parts.append(f"type={sym.dataLetter}")
            if sym.length is not None:
                parts.append(f"len={sym.length}")
            if sym.multiplicity is not None:
                parts.append(f"mult={sym.multiplicity}")
            if sym.scale is not None:
                parts.append(f"scale={sym.scale}")
        if sym.cluster:
            parts.append("CLUSTER")
        print("  ".join(parts))
    print(f"{'=' * 60}")


def dump_obj(filename, hex_dump=False, show_sym=False):
    of = objModule.ObjectFile(str(filename))

    for err in (e for m in of.modules for e in m.errors):
        print(f"  {err}")

    symbols = [s for m in of.modules for s in m.symbols]
    esd_names = {}  # esdId -> name
    sym_table_printed = False

    for rec in of.records:
        typ = rec.cardType

        # Print SYM table after the last SYM card
        if show_sym and not sym_table_printed and typ != "SYM" and symbols:
            _print_sym_table(symbols)
            sym_table_printed = True

        if isinstance(rec, ControlRecord):
            print(f"CTL  {rec.text.rstrip()}")

        elif isinstance(rec, EsdRecord):
            for e in rec.esdEntries:
                name = e.name.strip()
                esd_names[e.esdId] = name

                parts = [f"ESD  [{e.esdId:3d}] {e.typeName:2s} {name:8s}"]
                if e.address is not None:
                    parts.append(f"addr={Addr(e.address).x}")
                if e.length is not None:
                    parts.append(f"len={AddrDisp(e.length).hw} hw")
                if e.ldid is not None:
                    parts.append(f"ldid={e.ldid}")
                if e.remote:
                    parts.append("REMOTE")
                print("  ".join(parts))

        elif isinstance(rec, TxtRecord):
            tr = rec.textRecord
            size = len(tr.data)
            name = esd_names.get(tr.esdId, f"#{tr.esdId}")
            print(f"TXT  [{tr.esdId:3d}] {name:8s}  addr={Addr(tr.address).x}  {size} bytes")
            if hex_dump and size > 0:
                for off in range(0, size, 16):
                    chunk = tr.data[off:min(off + 16, size)]
                    # Dump as halfwords (pairs of bytes)
                    hwords = []
                    for h in range(0, len(chunk), 2):
                        if h + 1 < len(chunk):
                            hwords.append(f"{chunk[h]:02X}{chunk[h+1]:02X}")
                        else:
                            hwords.append(f"{chunk[h]:02X}")
                    print(f"       {Addr(tr.address + off).x}: {' '.join(hwords)}")

        elif isinstance(rec, RldRecord):
            for r in RldEntry.expand([rec.payload], []):
                rname = esd_names.get(r.relId, f"#{r.relId}")
                pname = esd_names.get(r.posId, f"#{r.posId}")
                rtype = _rld_type(r.flags)
                print(f"RLD  {rtype:12s}  {rname:8s} -> {pname:8s}"
                      f"  addr={Addr(r.address).x}  flags={r.flags:02X}")

        elif isinstance(rec, SymRecord):
            print(f"SYM  {rec.numBytesData} bytes")

        elif isinstance(rec, EndRecord):
            end = rec.endInfo
            parts = ["END"]
            if end.entryAddress is not None:
                parts.append(f"entry={Addr(end.entryAddress).x}")
            if end.entryName and end.entryName.strip():
                parts.append(f"entryName={end.entryName.strip()}")
            if end.esdId is not None:
                name = esd_names.get(end.esdId, f"#{end.esdId}")
                parts.append(f"esdid={end.esdId}({name})")
            if end.length is not None:
                parts.append(f"len={end.length}")
            if end.translator and end.translator.strip():
                parts.append(f"translator={end.translator.strip()}")
            if end.processor and end.processor.strip():
                parts.append(f"processor={end.processor.strip()}")
            print("  ".join(parts))

        else:
            print(f"???  type={typ}")


def extract_txt(filename, out_dir):
    """Extract TXT data for each CSECT to separate .bin files in out_dir."""
    of = objModule.ObjectFile(str(filename))

    # Collect ESD names and section lengths
    esd_names = {}
    esd_lengths = {}
    for m in of.modules:
        for e in m.esdEntries:
            esd_names[e.esdId] = e.name.strip()
            if e.length is not None and e.type in (EsdType.SD, EsdType.PC):
                esd_lengths[e.esdId] = e.length

    # Accumulate TXT data per CSECT.  Grow-mode scatter (a dump tool must not
    # trust the ESD length), padded up to the declared length afterwards.
    records = {}  # esdId -> [TextRecord]
    for m in of.modules:
        for tr in m.textRecords:
            if tr.data:
                records.setdefault(tr.esdId, []).append(tr)
    sections = {}  # esdId -> bytearray
    for esdId, trs in records.items():
        buf = objModule.scatterText(trs)
        length = esd_lengths.get(esdId, 0)
        if len(buf) < length:
            buf.extend(bytes(length - len(buf)))
        sections[esdId] = buf

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
