
import sys
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Annotated

import typer

app = typer.Typer(
    help="AP-101 Link Editor",
    add_completion=False,
    no_args_is_help=True,
)


@dataclass
class LinkerOpts:
    # inputs / outputs
    input_files:      list[str]        = field(default_factory=list)
    output:           Optional[str]    = None
    map:              Optional[str]    = None
    json_symbols:     Optional[str]    = None
    library_path:     list[str]        = field(default_factory=list)
    library:          list[str]        = field(default_factory=list)
    # link configuration
    save_config:      Optional[str]    = None
    load_config:      Optional[str]    = None
    entry:            Optional[str]    = None
    base_address:     int              = 0
    generate_stacks:  int              = 0
    define:           list[str]        = field(default_factory=list)
    compact:          bool             = False
    # error handling
    force:            bool             = False
    no_undefined:     bool             = False
    allow_undefined:  bool             = False
    # diagnostics
    verbose:          bool             = False
    quiet:            bool             = False
    debug:            bool             = False
    dump:             bool             = False
    dump_unrelocated: Optional[str]    = None
    print_map:        bool             = False


@app.command()
def link(
    input_files: Annotated[list[str], typer.Argument(help="Input object files to link")] = [],

    # Output
    output: Annotated[Optional[str], typer.Option("-o", "--output", help="Output FCM file name")] = None,
    map: Annotated[Optional[str], typer.Option("-M", "--map", help="Generate a link map/listing file")] = None,
    json_symbols: Annotated[Optional[str], typer.Option("--json-symbols", help="Output symbol table as JSON for simulator")] = None,

    # Libraries
    library_path: Annotated[Optional[list[str]], typer.Option("-L", "--library-path", help="Add directory to library search path")] = None,
    library: Annotated[Optional[list[str]], typer.Option("-l", "--library", help="Search library NAME (NAME.obj in -L paths)")] = None,

    # Link configuration
    save_config: Annotated[Optional[str], typer.Option("--save-config", help="Save link configuration to .lnk file")] = None,
    load_config: Annotated[Optional[str], typer.Option("--load-config", help="Load link configuration from .lnk file")] = None,

    # Linking options
    entry: Annotated[Optional[str], typer.Option("-e", "--entry", help="Set entry point symbol or address")] = None,
    base_address: Annotated[str, typer.Option("-Ttext", "--base-address", help="Base address for text (hex/dec, default: 0)")] = "0",
    generate_stacks: Annotated[str, typer.Option("--generate-stacks", help="Auto-generate stack sections (size in halfwords, hex/dec)")] = "0",
    define: Annotated[Optional[list[str]], typer.Option("-D", "--define", help="Define symbol with value (e.g. -D @0SIMPLE=0x1000)")] = None,
    compact: Annotated[bool, typer.Option("--compact", help="layout all sections as adjacent (sector rules ignored)")] = False,

    # Error handling
    force: Annotated[bool, typer.Option("-f", "--force", help="Force output even with unresolved symbols")] = False,
    no_undefined: Annotated[bool, typer.Option("--no-undefined", help="Report unresolved symbols as errors (default)")] = False,
    allow_undefined: Annotated[bool, typer.Option("--allow-undefined", help="Allow unresolved symbols without -f")] = False,

    # Diagnostics
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Verbose output")] = False,
    quiet: Annotated[bool, typer.Option("-q", "--quiet", help="Suppress non-critical warnings")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Print detailed debug info")] = False,
    dump: Annotated[bool, typer.Option("--dump", help="Include hex dump in listing file")] = False,
    dump_unrelocated: Annotated[Optional[str], typer.Option("--dump-unrelocated", help="Dump FCM before relocations")] = None,
    print_map: Annotated[bool, typer.Option("--print-map", help="Print link map to stdout")] = False,

    version: Annotated[bool, typer.Option("--version", help="Show version")] = False,
):
    import logging
    from .linker import Linker, error, log as lnk_log, program as prog_name, version as prog_version

    if version:
        print(f"{prog_name} {prog_version}")
        raise typer.Exit()

    if not input_files and not library:
        typer.echo("Error: No input files specified", err=True)
        raise typer.Exit(1)

    # Configure logging level from CLI flags
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    elif quiet:
        level = logging.ERROR
    else:
        level = logging.WARNING
    logging.basicConfig(
        format="%(name)s: %(levelname)s: %(message)s" if level <= logging.WARNING
               else "%(message)s",
        level=level,
    )
    # INFO messages are linker progress — print without prefix
    if level <= logging.INFO:
        class InfoFilter(logging.Formatter):
            def format(self, record):
                if record.levelno == logging.INFO:
                    return record.getMessage()
                if record.levelno == logging.DEBUG:
                    return f"DEBUG: {record.getMessage()}"
                return f"LNK101S: {record.levelname.lower()}: {record.getMessage()}"
        handler = logging.getLogger().handlers[0]
        handler.setFormatter(InfoFilter())

    opts = LinkerOpts(
        input_files=input_files,
        output=output or (Path(input_files[0]).stem + '.fcm' if input_files else 'a.out.fcm'),
        map=map,
        json_symbols=json_symbols,
        library_path=library_path or [],
        library=library or [],
        save_config=save_config,
        load_config=load_config,
        entry=entry,
        base_address=int(base_address, 0),
        generate_stacks=int(generate_stacks, 0),
        define=define or [],
        compact=compact,
        force=force or allow_undefined,
        no_undefined=no_undefined,
        allow_undefined=allow_undefined,
        verbose=verbose,
        quiet=quiet,
        debug=debug,
        dump=dump,
        dump_unrelocated=dump_unrelocated,
        print_map=print_map,
    )

    if opts.load_config and not os.path.exists(opts.load_config):
        error(f"Config file not found: {opts.load_config}")

    linker = Linker(opts)
    linker.loadInputFiles()

    if linker.errors and not opts.force:
        linker.printErrors()
        raise typer.Exit(1)

    if opts.verbose:
        print(f"\n{prog_name} {prog_version}")
        print("=" * 60)

    success = linker.link()

    linker.printErrors()
    linker.printWarnings()

    if not success and not opts.force:
        raise typer.Exit(1)

    linker.saveImage(opts.output)

    if opts.map:
        linker.saveListing(opts.map)
    elif opts.verbose:
        linker.saveListing(opts.output + '.LIST')

    if opts.json_symbols:
        linker.saveJsonSymbols(opts.json_symbols)

    if opts.print_map or opts.verbose:
        linker.printSectionTable()
        linker.printSummary()

    if success:
        print(f"\nLinked {len(linker.modules)} module(s) -> {opts.output} "
              f"({linker.imageSize} bytes)")
    else:
        print(f"\nLinked with errors -> {opts.output}", file=sys.stderr)
        raise typer.Exit(1)


def main():
    app()
