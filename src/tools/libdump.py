#!/usr/bin/env python3
#
# Dump AP-101 ".lib" load modules (ap101Utils.libModule)
#
# Prints the identity records (IDR, PHAS, ENTRY, XRES, LSTA), then CESD,
# TEXT with its protect map, RLD, REGN and a summary.  The CESD ids in the
# RLDs are resolved to names.  Addresses are halfwords; --bytes prints the
# byte addresses the container stores.
#

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from ap101Utils.addr import Addr, AddrDisp
from ap101Utils.addrcon import AddrCon
from ap101Utils.libModule import LibModule
from ap101Utils.objModule import EsdType
from lnk101.repro import ReproTracker, version_string


def _version_callback(value: bool):
    if value:
        print(f"ibmlibdump {version_string()}")
        raise typer.Exit()


app = typer.Typer(
    help="Dump AP-101 load modules (.lib).",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


def _rld_type(flags):
    con = AddrCon(flags)
    return f"{con.kind}({'-' if con.is_negative else '+'})"


class _Fmt:
    """Address formatting: halfword (default) or byte addresses."""

    def __init__(self, byte_addr=False):
        self.byte_addr = byte_addr
        self.unit = "byte" if byte_addr else "hw"

    def addr(self, value):
        return f"{value:06X}" if self.byte_addr else Addr(value).x

    def length(self, nbytes):
        return f"{nbytes} bytes" if self.byte_addr \
            else f"{AddrDisp(nbytes).hw} hw"


def _print_header(lib, fmt):
    """IDR / PHAS / ENTRY / XRES / LSTA -- the module's identity records."""
    parts = [f"IDR  {lib.name or '(unnamed)':8s}"]
    if lib.linkerId:
        parts.append(f"linker={lib.linkerId}")
    if lib.created:
        parts.append(f"created={lib.created}")
    print("  ".join(parts))

    if lib.phaseRoot or lib.phaseNumbers or lib.nocall:
        nums = ",".join(str(p) for p in lib.phaseNumbers)
        parts = [f"PHAS root={lib.phaseRoot or '-'}", f"phases=[{nums}]"]
        if lib.nocall:
            parts.append("NOCALLER")
        print("  ".join(parts))

    if lib.entryAddress is not None or lib.entryName:
        ea = fmt.addr(lib.entryAddress) if lib.entryAddress is not None \
            else "-" * 6
        print(f"ENTRY  {lib.entryName or '-':8s}  addr={ea}")

    if lib.crossResolved:
        print(f"XRES  cross-phase resolved  {lib.xresCount} site(s) patched")

    if lib.linkState:
        st = "  ".join(f"{k}=0x{v:X}" if isinstance(v, int) else f"{k}={v}"
                       for k, v in sorted(lib.linkState.items()))
        print(f"LSTA  {st}")


def _print_cesd(lib, fmt):
    for e in lib.cesd:
        name = e.name.strip()
        parts = [f"CESD [{e.esdId:4d}] {e.typeName:5s} {name:8s}"]
        if e.address is not None:
            parts.append(f"addr={fmt.addr(e.address)}")
        if e.type in (EsdType.SD, EsdType.PC, EsdType.CM) \
                and e.length is not None:
            parts.append(f"len={fmt.length(e.length)}")
        if e.type == EsdType.LD and e.ldid is not None:
            owner = lib.cesdEntry(e.ldid)
            oname = owner.name.strip() if owner else f"#{e.ldid}"
            parts.append(f"sd={e.ldid}({oname})")
        if e.remote:
            parts.append("REMOTE")
        print("  ".join(parts))


def _print_extent(x, fmt, hex_dump):
    nprot = sum(x.protect) if x.protect else 0
    print(f"TEXT  {fmt.addr(x.address)}..{fmt.addr(x.address + len(x.data))}"
          f"  {fmt.length(len(x.data))}  protected {nprot}/{x.hwLength} hw")
    if not hex_dump or not x.data:
        return
    data = x.data
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hwords = []
        for h in range(0, len(chunk), 2):
            if h + 1 < len(chunk):
                text = f"{chunk[h]:02X}{chunk[h+1]:02X}"
            else:
                text = f"{chunk[h]:02X}  "
            i = (off + h) // 2
            mark = "*" if x.protect and i < len(x.protect) and x.protect[i] \
                else " "
            hwords.append(text + mark)
        print(f"       {fmt.addr(x.address + off)}: {' '.join(hwords)}")


def _print_rlds(lib, fmt):
    for r in lib.rlds:
        rel = lib.cesdEntry(r.relId)
        pos = lib.cesdEntry(r.posId)
        rname = rel.name.strip() if rel else ("(byname)" if r.relId == 0
                                              else f"#{r.relId}")
        pname = pos.name.strip() if pos else f"#{r.posId}"
        print(f"RLD  {_rld_type(r.flags):12s}  {rname:8s} -> {pname:8s}"
              f"  addr={fmt.addr(r.position)}  flags={r.flags:02X}")


def _print_regions(lib, fmt):
    for r in lib.regions:
        print(f"REGN  {r.name:8s}  {fmt.addr(r.start)}..{fmt.addr(r.end)}"
              f"  bank {r.bank}")


def _print_summary(lib, fmt):
    counts = {}
    for e in lib.cesd:
        counts[e.typeName] = counts.get(e.typeName, 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"       CESD: {len(lib.cesd)} entries"
          f"{f' ({breakdown})' if breakdown else ''}")
    totalHw = sum(x.hwLength for x in lib.extents)
    totalProt = sum(sum(x.protect) if x.protect else 0 for x in lib.extents)
    print(f"       TEXT: {totalHw} hw in {len(lib.extents)} extent(s),"
          f" {totalProt} hw protected")
    print(f"       RLD: {len(lib.rlds)} entries")
    if lib.regions:
        print(f"       REGN: {len(lib.regions)} region(s)")


def dump_lib(filename, hex_dump=False, byte_addr=False, summary=False,
             show_cesd=True, show_rld=True):
    lib = LibModule.read(str(filename))
    fmt = _Fmt(byte_addr)

    _print_header(lib, fmt)
    if summary:
        _print_summary(lib, fmt)
        _print_regions(lib, fmt)
        print("EOM")
        return

    if show_cesd:
        _print_cesd(lib, fmt)
    else:
        print(f"CESD  {len(lib.cesd)} entries (suppressed)")

    for x in lib.extents:
        _print_extent(x, fmt, hex_dump)

    if show_rld:
        _print_rlds(lib, fmt)
    else:
        print(f"RLD   {len(lib.rlds)} entries (suppressed)")

    _print_regions(lib, fmt)
    _print_summary(lib, fmt)
    print("EOM")


def extract_text(filename, out_dir, byte_addr=False):
    """Extract each TEXT extent's data to a separate .bin file in out_dir."""
    lib = LibModule.read(str(filename))
    fmt = _Fmt(byte_addr)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = lib.name.strip() or Path(filename).stem
    for x in lib.extents:
        out_path = out_dir / f"{stem}_{Addr(x.address).x}.bin"
        out_path.write_bytes(bytes(x.data))
        print(f"  {fmt.addr(x.address)}  {len(x.data)} bytes -> {out_path}")


@app.command()
def main(
    lib_files: Annotated[list[Path], typer.Argument(help="Load module(s) to "
        "dump", exists=True)],
    hex_dump: Annotated[bool, typer.Option("--hex", "-x",
        help="Include hex dump of TEXT extent data ('*' = store-protected "
             "halfword)")] = False,
    summary: Annotated[bool, typer.Option("--summary", "-s",
        help="Print only the per-module summary (no CESD/TEXT/RLD "
             "listing)")] = False,
    no_cesd: Annotated[bool, typer.Option("--no-cesd",
        help="Suppress the per-entry CESD listing")] = False,
    no_rld: Annotated[bool, typer.Option("--no-rld",
        help="Suppress the per-entry RLD listing")] = False,
    byte_addr: Annotated[bool, typer.Option("--bytes", "-b",
        help="Print byte addresses (default: halfword addresses)")] = False,
    extract: Annotated[Optional[Path], typer.Option("--extract-text",
        help="Extract each TEXT extent's data to a .bin file in this "
             "directory")] = None,
    repro: Annotated[bool, typer.Option("--repro/--no-repro",
        help="Print repro info (git version, file MD5s)")] = True,
    check_repro: Annotated[Optional[Path], typer.Option("--check-repro",
        help="Compare current run against a saved .repro.json",
        exists=True)] = None,
    version: Annotated[bool, typer.Option("--version", help="Show version",
        callback=_version_callback, is_eager=True)] = False,
):
    """Dump AP-101 .lib load modules."""
    tracker = ReproTracker("ibmlibdump")
    for lib_file in lib_files:
        tracker.track(lib_file, role="input")

    for i, lib_file in enumerate(lib_files):
        if len(lib_files) > 1:
            if i > 0:
                print()
            print(f"=== {lib_file} ===")
        try:
            if extract is not None:
                extract_text(lib_file, extract, byte_addr=byte_addr)
            else:
                dump_lib(lib_file, hex_dump=hex_dump, byte_addr=byte_addr,
                         summary=summary, show_cesd=not no_cesd,
                         show_rld=not no_rld)
        except ValueError as e:
            print(f"  {e}", file=sys.stderr)
            raise typer.Exit(1)

    if repro:
        tracker.print_summary()
    if check_repro:
        tracker.print_check(check_repro)


if __name__ == "__main__":
    app()
