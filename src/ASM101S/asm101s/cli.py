#!/usr/bin/env python3
"""
ASM101S - IBM AP-101S Assembler
"""

from pathlib import Path
from typing import Annotated, Optional
import sys
import os
import tempfile

os.environ["TYPER_USE_RICH"] = "0" # Disable fancy formatting
import typer

from .assemble import Assemble, AssemblyError

app = typer.Typer(
  name="asm101s",
  help="Assemble for the IBM AP-101 with Shuttle instruction set",
)


def validate_source_file(path: Path) -> Path:
  if path.suffix.lower() != ".asm":
    raise typer.BadParameter("Source-code filenames must end with .asm")
  if not path.exists():
    raise typer.BadParameter(f"Source file not found: {path}")
  return path


def validate_object_file(path: Optional[Path]) -> Optional[Path]:
  if path is not None and path.suffix.lower() != ".obj":
    raise typer.BadParameter("Object-code filenames must end in .obj")
  return path


@app.command()
def assemble(
  source_files: Annotated[ list[Path], typer.Argument(
    help="Source files to assemble (must end with .asm)",
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
      help="Sets the global SET symbol &SYSPARM. For Space Shuttle flight software, allowed choices are BFS and PASS."),
  ] = "PASS",
  tolerable: Annotated[ int, typer.Option("--tolerable",
      help="Maximum tolerable error severity. ASM101S errors are severity 255. MNOTE instruction errors have severity determined by source code (level 1 is typically info).",
      min=0, max=255),
  ] = 1,
  compare: Annotated[ Optional[Path], typer.Option("--compare", "-c",
      help="Assembly-listing file whose generated code is compared to the current assembly.",
      exists=True, dir_okay=False, resolve_path=True),
  ] = None,
  quiet: Annotated[ bool, typer.Option("--quiet", "-q",
      help="Suppress output unless there are errors."),
  ] = False,
  verbose: Annotated[ bool, typer.Option("--verbose", "-v",
      help="Print progress messages during assembly."),
  ] = False,
) -> None:
  """
  Assemble AP-101S assembly language source files.

  Examples:
    asm101s source.asm
    asm101s --library=macros/ --object=output.obj source.asm
    asm101s -L macros/ -o output.obj file1.asm file2.asm
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
      comparison_file=compare,
    )
    
    assembler.assemble()
    
    if compare is not None and lst_file is None:
      with tempfile.NamedTemporaryFile(mode='w', suffix='.lst', delete=False) as tmp:
        lst_file = Path(tmp.name)

    comparison_result = None
    if lst_file is not None:
      comparison_result = assembler.writeListing(lst_file)
    
    # Print comparison results to stdout
    if comparison_result is not None:
      has_errors = comparison_result["mismatch_count"] > 0 or comparison_result["missing_count"] > 0
      if not quiet or has_errors:
        for line in comparison_result["output"]:
          typer.echo(line)
      
  except AssemblyError as e:
    typer.echo(str(e), err=True)
    raise typer.Exit(code=1)


def main():
  app()


if __name__ == "__main__":
  main()
