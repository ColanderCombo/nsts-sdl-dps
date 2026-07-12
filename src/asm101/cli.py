#!/usr/bin/env python3
# 
# asm101 - IBM AP-101 Assembler
# 

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional
import os

os.environ["TYPER_USE_RICH"] = "0" # Disable fancy formatting
import typer

from .assemble import Assemble, AssemblyError


class March(str, Enum):
  ap101b = "ap101b"
  ap101s = "ap101s"

app = typer.Typer(
  name="asm101",
  help="Assemble for the IBM AP-101 with Shuttle instruction set",
)


def validate_source_file(path: Path) -> Path:
  if not path.exists():
    raise typer.BadParameter(f"Source file not found: {path}")
  return path


def validate_object_file(path: Optional[Path]) -> Optional[Path]:
  return path


@app.command()
def assemble(
  source_files: Annotated[ list[Path], typer.Argument(
    help="Source files to assemble",
    exists=True, dir_okay=False, resolve_path=True),
  ],
  object_file: Annotated[ Optional[Path], typer.Option("--object", "-o",
    help="Output object-code file name. Defaults to BASENAME.obj in current directory.",
    dir_okay=False, resolve_path=True),
  ] = None,
  lst_file: Annotated[ Optional[Path], typer.Option("--listing", "-l",
    help="Output listing file name.",
    dir_okay=False, resolve_path=True),
  ] = None,
  library: Annotated[ Optional[list[Path]], typer.Option("--library", "-L",
      help="Path to a macro library. Can be specified multiple times.",
      exists=True, file_okay=False, resolve_path=True),
  ] = None,
  sysparm: Annotated[ str, typer.Option("--sysparm", "-s",
      help="SETs global &SYSPARM (BFS or PASS)"),
  ] = "PASS",
  march: Annotated[ March, typer.Option("-march", "--march",
      help="ap101b or ap101s"),
  ] = March.ap101s,
  tolerable: Annotated[ int, typer.Option("--tolerable",
      help="Maximum tolerable error severity. asm101 errors are severity 255.",
      min=0, max=255),
  ] = 1,
  debug_info: Annotated[ bool, typer.Option("--debug-info/--no-debug-info",
      help="Emit a BASENAME.asmg.json debug-symbol sidecar."),
  ] = True,
  quiet: Annotated[ bool, typer.Option("--quiet", "-q",
      help="Suppress output unless there are errors."),
  ] = False,
  verbose: Annotated[ bool, typer.Option("--verbose", "-v",
      help="Print progress messages during assembly."),
  ] = False,
) -> None:
  """
  Assemble AP-101 assembly language source files.

  Examples:
    asm101 source.asm
    asm101 --library=macros/ --object=output.obj source.asm
    asm101 -L macros/ -o output.obj file1.asm file2.asm
  """

  for src in source_files:
    validate_source_file(src)

  object_file = validate_object_file(object_file)

  if object_file is None:
    object_file = Path(source_files[-1].stem + ".obj")

  try:
    assembler = Assemble(
      source_files=source_files,
      object_file=object_file,
      libraries=library,
      sysparm=sysparm,
      tolerable_severity=tolerable,
      verbose=verbose,
      debug_info=debug_info,
    )

    assembler.assemble()

    if lst_file is not None:
      assembler.writeListing(lst_file)

  except AssemblyError as e:
    typer.echo(str(e), err=True)
    raise typer.Exit(code=1)


def main():
  app()


if __name__ == "__main__":
  main()
