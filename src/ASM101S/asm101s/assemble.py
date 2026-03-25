

#!/usr/bin/env python3
"""
Assembler for the IBM AP-101S computer.
"""

import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from .expressions import (
    svGlobals, svGlobalLocals, definedNormalSymbols, error, getErrorCount,
    parserASM, evalBooleanExpression, svReplace, svDeclare, svSet, unroll
)
from .fieldParser import joinOperand
from .readListing import readListing
from .objectWriter import writeObjectModule
from .model101 import (
    generateObjectCode, instructionsWithoutOperands,
    symtab, sects, entries, extrns, literalPools
)

PROGRAM = "ASM101S"
VERSION = "0.01"


class AssemblyError(Exception):
    """Raised when assembly fails due to errors in the source code."""
    
    def __init__(self, message: str, error_count: int = 0, max_severity: int = 0):
        super().__init__(message)
        self.error_count = error_count
        self.max_severity = max_severity


class Assemble:
    """
    Assembler for IBM AP-101S assembly language.
    
    This class reads assembly source files, expands macros, and generates
    object code in IBM object module format.
    
    Example usage:
        assembler = Assemble(
            source_files=[Path("source.asm")],
            object_file=Path("output.obj"),
            libraries=[Path("macros/")],
            sysparm="PASS"
        )
        assembler.assemble()
        assembler.writeListing(Path("output.lst"))
    """
    
    # All pseudo-ops ("assembler instructions"). See Appendix E of the 
    # "IBM System/360 Operating System Assembler Language" manual.
    # Gives the minimum and maximum number of comma-delimited operands 
    # in the operand field. -1 for maximum means "no limit".
    PSEUDO_OPS = {
        "ACTR": [1, 1],
        "AGO": [1, 1],
        "AIF": [1, 1],
        "ANOP": [0, 0],
        "CCW": [4, 4],
        "CNOP": [2, 2],
        "COM": [0, 0],
        "COPY": [1, 1],
        "CSECT": [0, 0],
        "CXD": [0, 0],
        "DC": [1, -1],
        "DROP": [1, 16],
        "DS": [1, -1],
        "DSECT": [0, 0],
        "DXD": [1, -1],
        "EJECT": [0, 0],
        "END": [0, 1],
        "ENTRY": [1, -1],
        "EQU": [1, 1],
        "EXTRN": [1, -1],
        "GBLA": [1, -1],
        "GBLB": [1, -1],
        "GBLC": [1, -1],
        "ICTL": [1, 3],
        "ISEQ": [2, 2],
        "LCLA": [1, -1],
        "LCLB": [1, -1],
        "LCLC": [1, -1],
        "LTORG": [0, 0],
        "MACRO": [0, 0],
        "MEND": [0, 0],
        "MEXIT": [0, 0],
        "MNOTE": [2, 2],
        "ORG": [0, 1],
        "PRINT": [1, 3],
        "PUNCH": [1, 1],
        "REPRO": [0, 0],
        "SETA": [1, 1],
        "SETB": [1, 1],
        "SETC": [1, 1],
        "SPACE": [0, 1],
        "SPOFF": [0, 0],
        "SPON": [0, 0],
        "START": [0, 1],
        "TITLE": [1, 1],
        "USING": [2, 17]
    }
    
    # Characters valid in symbol names
    LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ$#@"
    DIGITS = "0123456789"
    SPECIAL_CHARACTERS = "+-,=*()'/& "
    
    def __init__(
        self,
        source_files: list[Path],
        object_file: Path,
        libraries: Optional[list[Path]] = None,
        sysparm: str = "PASS",
        tolerable_severity: int = 1,
        verbose: bool = False,
        comparison_file: Optional[Path] = None
    ):
        """
        Initialize the assembler.
        
        Args:
            source_files: List of assembly source files to assemble.
            object_file: Path for the output object file.
            libraries: List of macro library directories.
            sysparm: Value for &SYSPARM global symbol (typically "PASS" or "BFS").
            tolerable_severity: Maximum tolerable error severity (default 1).
            verbose: If True, print progress messages.
            comparison_file: Optional path to an assembly listing file to compare
                generated code against.
        """
        self.source_files = source_files
        self.object_file = object_file
        self.libraries = libraries or []
        self.sysparm = sysparm
        self.tolerable_severity = tolerable_severity
        self.verbose = verbose
        self.comparison_file = comparison_file
        self.current_date = datetime.today().strftime('%m/%d/%y')
        
        # Assembly state
        self.source = []  # All source lines with properties
        self.library_dirs = []  # Paths to macro library directories
        self.macros = {}  # Macro definitions
        self.sequence_global_locals = {}  # Sequence symbols for global-local scope
        self.metadata = {}  # Assembly metadata (TITLE, etc.)
        self.sysndx = -1  # Macro invocation counter
        self.end_libraries = 0  # First line after macro library definitions
        
        # Comparison data
        self.comparison_sects = None
        if comparison_file is not None:
            self.comparison_sects = readListing(str(comparison_file))
            if self.comparison_sects is None:
                raise AssemblyError(f"Could not load comparison file {comparison_file}")
        
        # Initialize global symbolic variables
        svGlobals["_passCount"] = -1
        svGlobals["&SYSPARM"] = self.sysparm
    
    def _log(self, message: str) -> None:
        """Print a message if verbose mode is enabled."""
        if self.verbose:
            print(message, file=sys.stderr)
    
    def _is_symbol(self, name: str, in_macro_definition: bool = False) -> bool:
        """
        Check if a name is a valid symbol.
        
        Args:
            name: The name to check.
            in_macro_definition: If True, allows . and & prefixes.
            
        Returns:
            True if the name is a valid symbol, False otherwise.
        """
        good_name = True
        if in_macro_definition and name[0] in [".", "&"]:
            new_name = name[1:]
            max_len = 7
        else:
            new_name = name
            max_len = 8
        if len(new_name) > max_len or new_name[0] not in self.LETTERS:
            good_name = False
        else:
            for n in new_name[1:]:
                if n not in self.LETTERS and n not in self.DIGITS:
                    good_name = False
                    break
        return good_name
    
    def _is_symbol_expression(self, name: str, in_macro_definition: bool = False) -> bool:
        """
        Check if a name is a valid symbol expression (concatenation of strings).
        
        Args:
            name: The name to check.
            in_macro_definition: If True, allows . and & prefixes.
            
        Returns:
            True if valid, False otherwise.
        """
        if name[-1] != ")":
            return self._is_symbol(name, in_macro_definition)
        fields = name[:-1].split("(")
        if len(fields) != 2:
            error({}, "Incorrect symbol expression")
            return False
        return (self._is_symbol(fields[0], in_macro_definition) and
                self._is_symbol(fields[1], in_macro_definition))
    
    def _parse_line(self, lines: list, line_number: int, 
                    in_macro_definition: bool, in_macro_proto: bool) -> int:
        """
        Parse an input card into name, operation, and operand fields.
        
        This does not try to determine validity (except to the extent necessary
        for parsing) nor to evaluate any expressions. It takes into account
        continuation cards, macro definitions (without expanding them), and
        the alternate continuation format.
        
        Args:
            lines: The list of source lines.
            line_number: Index of the current line.
            in_macro_definition: Whether we're inside a macro definition.
            in_macro_proto: Whether we're on the macro prototype line.
            
        Returns:
            The number of continuation lines processed.
        """
        alternate = in_macro_proto
        skipped = 0
        properties = self.source[-1]
        properties["operand"] = None
        
        if (properties["empty"] or properties["fullComment"] or
                properties["dotComment"]):
            return 0
        
        text = properties["text"]
        
        # Parse all fields prior to the operand
        j = 0
        while j < len(text) and text[j] != " ":  # Scan past the label
            j += 1
        name = text[:j]
        properties["name"] = name
        
        while j < len(text) and text[j] == " ":  # Scan up to operation
            j += 1
        k = j
        while j < len(text) and text[j] != " ":  # Scan past the operation
            j += 1
        operation = text[k:j]
        properties["operation"] = operation
        
        if operation in self.macros:
            alternate = True
        if operation == "MACRO":
            if in_macro_definition:
                error(properties, "Nested MACRO definitions")
            return 0
        if operation == "MEND":
            if not in_macro_definition:
                error(properties, "MEND without preceding MACRO")
            return 0
        if in_macro_definition and not in_macro_proto:
            return 0
        
        while j < len(text) and text[j] == " ":  # Scan up to operand/comment
            j += 1
        
        # Determine the full operand field
        operand = ""
        if in_macro_proto:
            in_macro_proto = False
            success, field, skipped = joinOperand(lines, line_number, j, proto=True)
            if success:
                operand = field
            else:
                error(properties, "Cannot parse macro-prototype cards")
        elif operation in self.macros:
            success, field, skipped = joinOperand(lines, line_number, j, invoke=True)
            if success:
                operand = field
            else:
                error(properties, "Cannot parse macro-invocation operands")
        elif operation in instructionsWithoutOperands:
            pass
        elif operation in self.PSEUDO_OPS and self.PSEUDO_OPS[operation][0] == 0:
            pass
        else:
            # Operation has operands with standard continuation format
            success, field, skipped = joinOperand(lines, line_number, j)
            if success:
                operand = field
            else:
                error(properties, "Cannot parse macro-invocation operands")
        properties["operand"] = operand
        
        return skipped
    
    def _eval_macro_argument(self, properties: dict, suboperand):
        """
        Evaluate a suboperand in a macro invocation.
        
        Returns:
            A tuple (key, value) where key is the formal parameter name
            (like "&A") for non-positional parameters, or None for positional.
            The value is a tuple of replacement strings.
        """
        # Positional parameter: bare, unquoted string
        if isinstance(suboperand, str):
            return None, suboperand
        # Non-positional parameter: bare, unquoted string
        elif (isinstance(suboperand, (list, tuple)) and len(suboperand) == 3 and
              suboperand[1] == "=" and isinstance(suboperand[2], str)):
            return ("&" + suboperand[0]), suboperand[2]
        # Non-positional parameter: list
        elif (isinstance(suboperand, (list, tuple)) and len(suboperand) == 5 and
              suboperand[1] == "=" and suboperand[2] == "(" and
              isinstance(suboperand[3], tuple) and suboperand[4] == ")"):
            parm_name = "&" + suboperand[0]
            replacement_list = []
            if len(suboperand[3]) > 0:
                replacement_list.append(suboperand[3][0])
                if len(suboperand[3]) > 1:
                    for e in suboperand[3][1]:
                        replacement_list.append(e[1])
                return parm_name, tuple(replacement_list)
        # Positional parameter: quoted string
        elif (isinstance(suboperand, tuple) and len(suboperand) == 4 and
              suboperand[0] == "'" and suboperand[3] == "'" and
              suboperand[2] == [] and isinstance(suboperand[1], str)):
            return None, ("'" + suboperand[1] + "'")
        # Positional parameter: list like (1,2,A)
        elif (isinstance(suboperand, tuple) and len(suboperand) == 3 and
              suboperand[0] == '(' and suboperand[2] == ')' and
              isinstance(suboperand[1], tuple)):
            replacement_list = []
            if len(suboperand[1]) > 0:
                replacement_list.append(suboperand[1][0])
                if len(suboperand[1]) > 1:
                    for e in suboperand[1][1]:
                        replacement_list.append(e[1])
                return None, tuple(replacement_list)
        else:
            # Try joining as concatenation of strings
            try:
                s = "".join(suboperand)
                return None, s
            except:
                pass
            error(properties,
                  f"Implementation error in replacement argument {suboperand}")
            return None, None
    
    def _read_source_file(self, from_where: str, sv_locals: dict, sequence: dict,
                          copy: bool = False, printable: bool = True, 
                          depth: int = 0) -> None:
        """
        Recursively read source code lines, expanding COPY and macros.
        
        Args:
            from_where: Either a filename or a macro name.
            sv_locals: Local symbolic variables for macro expansion.
            sequence: Dictionary of sequence symbols encountered.
            copy: True if reading as target of COPY pseudo-op.
            printable: True if file should be listed in output.
            depth: Depth of macro expansion (0 = not in macro).
        """
        line_number = -1
        first_index_of_file = len(self.source)
        in_macro_proto = False
        in_macro_definition = False
        continuation = False
        name = ""
        operation = ""
        prototype_index = -1
        continue_prototype = False
        line_correspondence = []
        
        if from_where in self.macros:
            # Load the macro definition
            filename = None
            macroname = from_where
            macro_where = self.macros[macroname]
            this_source = []
            sequence = {}
            prototype_index = macro_where[3] - macro_where[2]
            
            for i in range(macro_where[2], macro_where[4] + 1):
                if i == macro_where[2]:
                    continue
                if i == macro_where[3]:
                    if self.source[i]["continues"]:
                        continue_prototype = True
                    continue
                if i == macro_where[4]:
                    continue
                if continue_prototype:
                    if not self.source[i]["continues"]:
                        continue_prototype = False
                    continue
                
                if self.source[i]["continues"]:
                    suffix = "X"
                else:
                    suffix = " "
                this_source.append(self.source[i]["text"] + suffix)
                line_correspondence.append(i - macro_where[5])
        else:
            try:
                with open(from_where, "rt") as f:
                    this_source = f.readlines()
                for i in range(len(this_source)):
                    line_correspondence.append(i)
                filename = from_where
                macroname = None
            except FileNotFoundError:
                raise AssemblyError(f"Source file '{from_where}' does not exist")
        
        skip_count = 0
        line_number = -1
        skip_to_seq = None
        
        while line_number + 1 < len(this_source):
            line_number += 1
            line = this_source[line_number]
            
            # When skipping to a sequence symbol (forward AGO/AIF target), check
            # if this line defines it. The sequence symbol must be at the start
            # of the line, followed by whitespace before the operation field.
            # 
            # Bug fix: The original check required exactly one space after the
            # sequence symbol (skip_to_seq + " "), but macro source lines are
            # padded to 80 columns with variable whitespace between fields.
            # For example, ".DUPLOOP AIF ..." has 1 space but ".DUP     ANOP"
            # has 5 spaces. We now check that the character immediately after
            # the sequence symbol is a space (or that the line ends there).
            if skip_to_seq is not None:
                seq_len = len(skip_to_seq)
                if not (line.startswith(skip_to_seq) and 
                        (len(line) == seq_len or line[seq_len] == ' ')):
                    continue
            skip_to_seq = None
            
            line = f"{line.rstrip()[:80]:<80}"
            text = line[:71]
            properties = {
                "section": None,
                "pos1": None,
                "length": None,
                "alignment": 2,
                "text": text,
                "name": "",
                "operation": "",
                "operand": "",
                "file": filename,
                "macro": macroname,
                "lineNumber": line_number + 1,
                "continues": (line[71] != " "),
                "identification": line[72:],
                "empty": (text.strip() == ""),
                "fullComment": line.startswith("*"),
                "dotComment": line.startswith(".*"),
                "endComment": "",
                "errors": [],
                "inMacroDefinition": in_macro_definition,
                "copy": copy,
                "printable": printable,
                "depth": depth,
                "n": len(self.source)
            }
            self.source.append(properties)
            
            if skip_count > 0:
                skip_count -= 1
                properties["skip"] = True
                continue
            if (properties["empty"] or properties["fullComment"] or
                    properties["dotComment"]):
                continue
            if len(self.source) > 1 and self.source[-2]["continues"]:
                continue
            
            skip_count = self._parse_line(this_source, line_number,
                                         in_macro_definition, in_macro_proto)
            name = properties["name"]
            if name[:1] == ".":
                sequence[name] = (from_where, line_number)
            operation = properties["operation"]
            operand = properties["operand"]
            
            if operation == "MACRO":
                in_macro_proto = True
                in_macro_definition = True
                macro_start = len(self.source) - 1
                properties["inMacroDefinition"] = True
            elif in_macro_proto:
                in_macro_proto = False
                # Determine macro name and parameter counts
                positional = 0
                nonpositional = 0
                for sub in operand:
                    if "=" in sub:
                        nonpositional += 1
                    else:
                        positional += 1
                macro_name = operation
                self.macros[macro_name] = [
                    positional, positional + nonpositional,
                    macro_start, len(self.source) - 1
                ]
            elif operation == "MEND":
                self.macros[macro_name].append(len(self.source) - 1)
                self.macros[macro_name].append(first_index_of_file)
                in_macro_definition = False
                continue
            elif operation == "COPY":
                found = False
                for library in self.library_dirs:
                    if line[0] == " ":
                        fname = line.split()[1]
                    else:
                        fname = line.split()[2]
                    fcopy = os.path.join(library, fname + ".asm")
                    if os.path.exists(fcopy) and os.path.isfile(fcopy):
                        found = True
                        self._read_source_file(fcopy, sv_locals, sequence,
                                              copy=True, printable=printable,
                                              depth=depth)
                        break
                if not found:
                    raise AssemblyError(f"File {fname}.asm for COPY not found")
                continue
            
            if in_macro_definition:
                continue
            
            # Handle macro-language related pseudo-ops
            if operation == "MEXIT":
                break
            if operation in {"GBLA", "GBLB", "GBLC", "LCLA", "LCLB", "LCLC"}:
                svDeclare(operation, operand, sv_locals, properties)
                continue
            if operation in {"SETA", "SETB", "SETC"}:
                svSet(operation, name, operand, sv_locals, properties)
                continue
            if operation == "AGO":
                target = operand.rstrip()
                if target in sequence:
                    if from_where != sequence[target][0]:
                        error(properties, "Target out of this macro")
                        continue
                    line_number = sequence[target][1] - 1
                else:
                    skip_to_seq = target
                continue
            if operation == "AIF":
                operand = operand.rstrip()
                ast = parserASM(operand, "aifAll")
                if (isinstance(ast, tuple) and len(ast) == 4 and
                        ast[0] == '(' and ast[2] == ')'):
                    target = ast[3]
                    expression = ast[1]
                    pass_fail = evalBooleanExpression(expression, sv_locals, properties)
                    if pass_fail is None:
                        error(properties, f"Cannot evaluate {expression}")
                        continue
                    if not pass_fail:
                        continue
                    # Conditional test passed - go to sequence symbol
                    if target in sequence:
                        if from_where != sequence[target][0]:
                            error(properties, "Target out of this macro")
                            continue
                        line_number = sequence[target][1] - 1
                    else:
                        skip_to_seq = target
                    continue
                else:
                    error(properties, f"Unrecognized AIF operand: {operand}")
                continue
            if operation == "ANOP":
                continue
            if operation == "MNOTE":
                ast = parserASM(operand, "mnote")
                if ast is None:
                    error(properties, f"Cannot parse MNOTE: {operand}")
                else:
                    msg = unroll(ast["msg"])[1]
                    msg = svReplace(properties, msg, sv_locals)
                    if "com" in ast:
                        pass
                    elif "sev" in ast:
                        error(properties, msg, severity=int(ast["sev"][0]))
                    else:
                        error(properties, msg, severity=1)
                    properties["fullComment"] = True
                    properties["text"] = msg
                    properties["name"] = ""
                    properties["operation"] = ""
                    properties["operand"] = ""
                    properties["mnote"] = True
                continue
            
            # Symbolic-variable replacement
            if "&" in line:
                properties["rawName"] = name
                properties["rawOperation"] = operation
                properties["rawOperand"] = operand
                name = svReplace(properties, name, sv_locals)
                operation = svReplace(properties, operation, sv_locals)
                operand = svReplace(properties, operand, sv_locals)
                properties["name"] = name
                properties["operation"] = operation
                properties["operand"] = operand
            
            if (name != "" and name[:1] not in [".", "&"] and
                    operation not in ["TITLE", "CSECT", "DSECT"] and
                    operation not in self.macros):
                if name not in definedNormalSymbols:
                    definedNormalSymbols[name] = {
                        "label": True,
                        "fromWhere": from_where,
                        "lineNumber": line_number,
                        "fromLine": line_correspondence[line_number]
                    }
                else:
                    error(properties, f"Already defined: {name}")
            elif operation == "EXTRN":
                symbols = operand.split(",")
                for symbol in symbols:
                    if symbol not in definedNormalSymbols:
                        definedNormalSymbols[symbol] = {
                            "label": True,
                            "fromWhere": from_where,
                            "lineNumber": line_number,
                            "fromLine": line_correspondence[line_number]
                        }
            
            if operation in self.macros:
                self.sysndx += 1
                macrostats = self.macros[operation]
                # Get formal parameters from macro prototype
                formals = self.source[macrostats[3]]["operand"]
                pformals = parserASM(formals, "operandPrototype")
                if operand.strip() == "":
                    poperands = []
                else:
                    poperands = parserASM(operand, "operandInvocation")
                if isinstance(poperands, dict) and "pi" in poperands:
                    poperands = poperands["pi"]
                else:
                    poperands = []
                if isinstance(pformals, dict) and "pi" in pformals:
                    pformals = pformals["pi"]
                else:
                    pformals = []
                
                # Build dictionary of formal parameter replacements
                new_locals = {
                    "parent": [from_where, line_number,
                              line_correspondence[line_number], sv_locals]
                }
                fname = self.source[macrostats[3]]["name"]
                if fname != "":
                    new_locals[fname] = name
                
                # Fill in default values
                syslist0 = name
                syslist = []
                for i in range(len(pformals) - 1, -1, -1):
                    pformal = pformals[i]
                    if isinstance(pformal, str):
                        new_locals[pformal] = ''
                        new_locals["_" + pformal] = {"omitted": True}
                    elif isinstance(pformal, (list, tuple)):
                        if (len(pformal) != 3 or pformal[1] != "=" or
                                pformal[0][:1] != "&" or
                                not isinstance(pformal[2], str)):
                            error(properties,
                                  f"Unrecognized format for formal parameter {pformal}")
                            continue
                        new_locals[pformal[0]] = pformal[2]
                        new_locals["_" + pformal[0]] = {"omitted": True}
                        del pformals[i]
                    else:
                        error(properties,
                              f"Implementation error in formal parameter {pformal}")
                        continue
                
                # Apply actual parameter replacements
                i = 0
                for suboperand in poperands:
                    key, value = self._eval_macro_argument(properties, suboperand)
                    if key is None:
                        syslist.append(value)
                        if i >= len(pformals):
                            continue
                        new_locals[pformals[i]] = value
                        new_locals["_" + pformals[i]]["omitted"] = False
                        i += 1
                    else:
                        new_locals[key] = value
                        new_locals["_" + key]["omitted"] = False
                
                new_locals["&SYSLIST"] = syslist
                new_locals["&SYSLIST0"] = syslist0
                new_locals["&SYSNDX"] = self.sysndx
                
                self._read_source_file(operation, new_locals, sequence,
                                       copy=copy, printable=printable,
                                       depth=depth + 1)
            continue
    
    def _read_macro_library(self, dir_path: Path) -> None:
        """
        Read an entire macro library directory.
        
        Args:
            dir_path: Path to the macro library directory.
        """
        macrofiles_path = dir_path / "MACROFILES.txt"
        try:
            with open(macrofiles_path, "rt") as f:
                macro_files = set()
                for line in f:
                    line = line.strip()
                    if line == "" or line[0] == ";":
                        continue
                    macro_files.add(line)
        except FileNotFoundError:
            raise AssemblyError(f"Cannot open {macrofiles_path}")
        
        self.library_dirs.append(str(dir_path))
        
        for file in os.listdir(dir_path):
            if file not in macro_files:
                continue
            path = dir_path / file
            self._read_source_file(str(path), svGlobalLocals,
                                  self.sequence_global_locals,
                                  copy=False, printable=False, depth=0)
    
    def assemble(self) -> None:
        """
        Perform the assembly process.
        
        This reads all source files, expands macros, generates object code,
        and writes the object file.
        
        Raises:
            AssemblyError: If there are intolerable errors in the source.
        """
        self._log(f"Assembling {len(self.source_files)} source file(s)...")
        
        # Read macro libraries
        for library_path in self.libraries:
            self._read_macro_library(library_path)
        self.end_libraries = len(self.source)
        
        # Read source files
        source_file_names = []
        for source_file in self.source_files:
            if not str(source_file).lower().endswith(".asm"):
                raise AssemblyError(
                    f"Source file '{source_file}' must end with .asm")
            source_file_names.append(source_file.stem)
            self._read_source_file(str(source_file), svGlobalLocals,
                                  self.sequence_global_locals,
                                  copy=False, printable=True, depth=0)
        
        # Generate object code
        self._log("Generating object code...")
        self.metadata = generateObjectCode(self.source, self.macros)
        
        # Check for errors
        error_count, max_severity = getErrorCount()
        if len(self.source) > 0 and self.source[-1]["inMacroDefinition"]:
            error_count += 1
            max_severity = 255
        
        if max_severity > self.tolerable_severity:
            # Build detailed error report
            error_lines = []
            error_lines.append(
                f"Assembly aborted due to intolerable errors. "
                f"{error_count} total error(s) detected.")
            error_lines.append(
                "Fix any intolerable errors marked below and retry.")
            error_lines.append("")
            
            last_error = False
            intolerables = 0
            for i in range(len(self.source)):
                line = self.source[i]
                depth_star = "+" if line["depth"] > 0 else ' '
                if len(line["errors"]) == 0:
                    error_lines.append(
                        f"{i:5d}: {depth_star}   {line['text']}")
                    last_error = False
                else:
                    if not last_error:
                        error_lines.append("=" * 53)
                    any_intolerable = False
                    for msg in line["errors"]:
                        fields = msg.split(")")[0].split()
                        if int(fields[-1]) > self.tolerable_severity:
                            any_intolerable = True
                        error_lines.append(msg)
                    if any_intolerable:
                        intolerables += 1
                    error_lines.append(
                        f"{i:5d}: {depth_star}   {line['text']}")
                    error_lines.append("=" * 53)
                    last_error = True
            
            if len(self.source) > 0 and self.source[-1]["inMacroDefinition"]:
                error_lines.append("No closing MEND for MACRO")
            
            error_lines.append(
                "Assembly aborted. Fix the errors or use higher tolerable_severity.")
            error_lines.append(
                f"{','.join(source_file_names)}: {intolerables} intolerable line(s) "
                f"detected, {self.tolerable_severity} < severity < {1 + max_severity}.")
            
            raise AssemblyError("\n".join(error_lines), error_count, max_severity)
        
        # Write object file
        self._log(f"Writing object file: {self.object_file}")
        writeObjectModule(str(self.object_file), self.metadata,
                         symtab, sects, entries, extrns)
        self._log(f"Assembly complete: {self.object_file}")
    
    def writeListing(self, listing_file: Path) -> None:
        """
        Write an assembly listing to a file.
        
        Args:
            listing_file: Path for the output listing file.
        """
        self._log(f"Writing listing to {listing_file}")
        
        # Configuration
        lines_per_page = 80
        page_separator = "\f" + ('-' * 120)
        
        # "Instructions" in the macro language by default aren't printed in the
        # assembly report.
        macro_language_instructions = {
            "GBLA", "GBLB", "GBLC", "LCLA", "LCLB",
            "LCLC", "SETA", "SETB", "SETC", "AIF",
            "AGO", "ANOP", "SPACE", "MEXIT", "MNOTE"
        }
        
        with open(listing_file, "w") as f:
            # State for listing generation
            in_copy = False
            member_name = ""
            rvl = 0  # Don't know how to get these, so just 0/0/0 for now
            concat = 0
            nest = 0
            printed_line_number = 0
            page_number = 0
            lines_this_page = 1000  # Force new page on first output
            mismatch_count = 0  # Count of bytes that differ from comparison file
            
            # Build IDs for symbols
            id_counter = 0
            ids = {}
            
            # ==== External Symbol Dictionary ====
            title = "EXTERNAL SYMBOL DICTIONARY".center(100)
            subtitle = f"{'SYMBOL   TYPE  ID  ADDR  LENGTH  LD ID':<95}{PROGRAM} {VERSION:>15} {self.current_date}"
            
            for symbol in symtab:
                if symbol.startswith("_"):
                    continue
                entry = symtab[symbol]
                ld_id = "    "
                if entry["type"] == "CSECT" and not entry.get("dsect", False):
                    module_type = "SD"
                    id_counter += 1
                    ids[symbol] = id_counter
                    pid = f"{id_counter:04d}"
                elif "entry" in entry:
                    module_type = "LD"
                    pid = "    "
                    ld_id = f"{ids.get(entry['section'], 0):04X}"
                elif entry["type"] == "EXTERNAL":
                    module_type = "ER"
                    id_counter += 1
                    ids[symbol] = id_counter
                    pid = f"{id_counter:04d}"
                else:
                    continue
                    
                address = "      "
                if "address" in entry:
                    addr_val = entry["address"]
                    if "preliminaryOffset" in entry:
                        addr_val += entry["preliminaryOffset"]
                    address = f"{addr_val:06X}"
                    
                length = "      "
                if symbol in sects:
                    length = f"{(sects[symbol]['used'] + 1) // 2:06X}"
                    
                if lines_this_page >= lines_per_page:
                    page_number += 1
                    if page_number > 1:
                        f.write(page_separator + "\n")
                    f.write(f"         {title:<100}  PAGE {page_number:4d}\n")
                    f.write(subtitle + "\n")
                    lines_this_page = 0
                    
                f.write(f"{symbol:<10}{module_type:<3}{pid:<5}{address:<7}{length:<7}{ld_id}".rstrip() + "\n")
                lines_this_page += 1
            
            # ==== Source Listing ====
            title = ""
            subtitle = ""
            literal_pool_number = 0
            continuation = False
            
            for i in range(self.end_libraries, len(self.source)):
                properties = self.source[i]
                skip = False
                
                if properties.get("empty", False):
                    continue
                    
                if continuation:
                    continuation = properties.get("continues", False)
                    lines_this_page += 1
                    continue
                continuation = properties.get("continues", False)
                
                operation = properties.get("operation", "")
                
                if operation == "SPACE":
                    space = 1  # Actually depends on the operand
                    printed_line_number += space
                    properties["printedLineNumber"] = printed_line_number
                    lines_this_page += space
                elif operation == "TITLE":
                    title = properties.get("operand", "").rstrip()[1:-1]
                    subtitle = f"{'  LOC  OBJECT CODE   ADR1 ADR2      SOURCE STATEMENT':<95}{PROGRAM} {VERSION:>15} {self.current_date}"
                    printed_line_number += 1
                    properties["printedLineNumber"] = printed_line_number
                    lines_this_page = 1000
                    skip = True
                    
                if lines_this_page >= lines_per_page:
                    page_number += 1
                    if page_number > 1:
                        f.write(page_separator + "\n")
                    f.write(f"         {title:<100}  PAGE {page_number:4d}\n")
                    f.write(subtitle + "\n")
                    lines_this_page = 0
                    if skip:
                        continue
                        
                if properties.get("depth", 0) > 0:
                    depth_star = "+"
                else:
                    depth_star = ' '
                    
                if operation in macro_language_instructions:
                    continue
                if properties.get("fullComment", False) and properties.get("text", "").startswith("*/"):
                    continue  # "Modern" comment
                    
                if properties.get("copy", False):
                    if not in_copy:
                        member_name = Path(properties.get("file", "")).stem
                        if properties.get("printable", False):
                            lines_this_page += 1
                            f.write(f"         START OF COPY MEMBER {member_name:<8} RVL {rvl:02d} CONCATENATION NO. {concat:03d}  NEST {nest:03d}\n")
                        in_copy = True
                else:
                    if in_copy:
                        if properties.get("printable", False):
                            lines_this_page += 1
                            f.write(f"           END OF COPY MEMBER {member_name:<8} RVL {rvl:02d} CONCATENATION NO. {concat:03d}  NEST {nest:03d}\n")
                        in_copy = False
                        
                if properties.get("printable", False):
                    address = None
                    section = None
                    comparison_memory = None
                    prefix = ""
                    
                    prop_section = properties.get("section")
                    if prop_section and prop_section in sects and "offset" in sects[prop_section]:
                        offset = sects[prop_section]["offset"]
                    else:
                        offset = 0
                        
                    if operation == "EQU":
                        name = properties.get("name", "")
                        if name in symtab:
                            prefix = f"{symtab[name]['value'] & 0xFFFFFFF:07X}"
                    elif operation == "USING":
                        prefix = f"{properties.get('using', 0):07X}"
                    elif operation == "LTORG":
                        pass
                    elif "pos1" in properties and properties["pos1"] is not None:
                        address = properties["pos1"]
                        section = properties.get("section")
                        # Get comparison memory for this section
                        if self.comparison_sects is not None and section in self.comparison_sects:
                            comparison_memory = self.comparison_sects[section]["memory"]
                        paddress = address // 2
                        if section is not None and section in sects and "offset" in sects[section]:
                            paddress += offset
                        prefix = f"{paddress:05X}"
                        
                    if "assembled" in properties:
                        for j in range(min(8, len(properties["assembled"]))):
                            b = properties["assembled"][j]
                            # Compare against comparison listing if available
                            if comparison_memory is not None and address is not None:
                                oaddress = address + offset * 2
                                if oaddress < len(comparison_memory):
                                    if b != comparison_memory[oaddress]:
                                        mismatch_count += 1
                                    # Mark as compared by setting to None
                                    comparison_memory[oaddress] = None
                                address += 1
                            if j == 0 or ((j & 1) == 0 and operation != "DC"):
                                prefix += " "
                            prefix += f"{b:02X}"
                            
                    if "adr1" in properties:
                        prefix = f"{prefix:<21}{properties['adr1']:04X}"
                    if "adr2" in properties:
                        prefix = f"{prefix:<26}{properties['adr2']:04X}"
                        
                    # For whatever reason, a macro-invocation line is printed only under
                    # some circumstances, and is omitted in others.
                    if operation in self.macros and not properties.get("inMacroDefinition", False):
                        macro_where = self.macros[operation]
                        if macro_where[2] > self.end_libraries:
                            continue
                    if operation in self.macros and properties.get("depth", 0) > 0:
                        continue
                        
                    if properties.get("depth", 0) == 0:
                        identification = properties.get("identification", "")[:8]
                    else:
                        identification = f"{properties.get('depth', 0):02d}-"
                        suffix = ""
                        if properties.get("macro") is not None:
                            suffix = properties["macro"]
                        identification = identification + suffix[:5]
                        
                    if properties.get("dotComment", False):
                        pass
                    elif properties.get("fullComment", False) or properties.get("inMacroDefinition", False):
                        printed_line_number += 1
                        properties["printedLineNumber"] = printed_line_number
                        lines_this_page += 1
                        text = properties.get("text", "").rstrip()
                        if (identification.strip() == "" or
                            (properties.get("fullComment", False) and depth_star != " " and
                             "mnote" not in properties)):
                            f.write(f"{prefix:<30}{printed_line_number:5d}{depth_star}{text}\n")
                        else:
                            f.write(f"{prefix:<30}{printed_line_number:5d}{depth_star}{text:<71} {identification}\n")
                    elif operation == "":
                        continue
                    else:
                        name = properties.get("name", "")
                        if name.startswith("."):
                            name = ""
                        printed_line_number += 1
                        properties["printedLineNumber"] = printed_line_number
                        lines_this_page += 1
                        operand_str = str(properties.get("operand", "")).rstrip()
                        mid = f"{prefix:<30}{printed_line_number:5d}{depth_star}{name:<8} {operation:<5} {operand_str}"
                        identification = properties.get("identification", "")[:8] if properties.get("depth", 0) == 0 else identification
                        f.write(f"{mid:<108}{identification}\n")
                        
                    if operation == "LTORG" and literal_pool_number < len(literalPools):
                        pool = literalPools[literal_pool_number]
                        reordered = {}
                        # literal pool structure varies, handle carefully
                        if len(pool) > 3:
                            for k in range(len(pool) - 4):  # Adjust indexing
                                idx = k + 4  # Skip first 4 entries (metadata)
                                if idx < len(pool) and isinstance(pool[idx], dict):
                                    pool_entry = pool[idx]
                                    if "offset" in pool_entry:
                                        entry_offset = pool[1] + pool_entry.get("offset", 0)
                                        reordered[entry_offset // 2] = pool_entry
                        for k in sorted(reordered):
                            prefix = f"{k:05X} "
                            attributes = reordered[k]
                            if "assembled" in attributes:
                                bytes_data = attributes["assembled"]
                                for m in range(attributes.get("L", 0)):
                                    if m < len(bytes_data):
                                        prefix += f"{bytes_data[m]:02X}"
                            prefix = f"{prefix[:30]:<50}"
                            f.write(prefix + " " + str(attributes.get("operand", "")) + "\n")
                        literal_pool_number += 1
            
            # ==== Cross Reference ====
            lines_this_page = 1000
            for symbol in sorted(symtab, key=self._sort_order):
                sym_props = symtab[symbol]
                if symbol.startswith("_") or symbol.startswith("."):
                    continue
                if lines_this_page >= lines_per_page:
                    page_number += 1
                    f.write(page_separator + "\n")
                    f.write(f"{'':<45}{'CROSS REFERENCE':<66}PAGE {page_number:4d}\n")
                    f.write(f"{'SYMBOL    LEN    VALUE   DEFN   REFERENCES':<95}{PROGRAM} {VERSION:>15} {self.current_date}\n")
                    lines_this_page = 0
                    
                if sym_props["type"] in ["EQU", "CSECT", "EXTERNAL"]:
                    length = 1
                elif "properties" in sym_props and "scratch" in sym_props["properties"]:
                    scratch = sym_props["properties"]["scratch"]
                    if scratch.get("length", 0) < 2:
                        length = 1
                    else:
                        length = scratch["length"] // 2
                else:
                    length = 2
                    
                value = sym_props.get("value", 0)
                defn = "     "
                if "properties" in sym_props and "printedLineNumber" in sym_props["properties"]:
                    defn = f"{sym_props['properties']['printedLineNumber']:5d}"
                    
                section_name = sym_props.get("section")
                if section_name and section_name in sects and "offset" in sects[section_name]:
                    value += sects[section_name]["offset"]
                    
                if sym_props["type"] in ["INSTRUCTION", "DATA"]:
                    line = f"{symbol:<8} {length:5d}   {value & 0xFFFFFF:06X} {defn}"
                else:
                    line = f"{symbol:<8} {length:5d} {value & 0xFFFFFFFF:08X} {defn}"
                    
                num_refs = 0
                if "references" in sym_props and len(sym_props["references"]) > 0:
                    line += " "
                    for n in sorted(sym_props["references"]):
                        if n < len(self.source) and "printedLineNumber" in self.source[n]:
                            if num_refs == 15:
                                f.write(line + "\n")
                                lines_this_page += 1
                                line = " " * 30
                                num_refs = 0
                            line += f" {self.source[n]['printedLineNumber']:5d}"
                            num_refs += 1
                f.write(line + "\n")
                lines_this_page += 1
            
            # ==== Comparison Summary ====
            comparison_output = []
            if self.comparison_sects is not None:
                comparison_output.append(f"Generated code was compared to file {os.path.basename(str(self.comparison_file))}")
                missing_count = 0
                for sect in self.comparison_sects:
                    header_shown = False
                    amemory = sects[sect]["memory"] if sect in sects else []
                    memory = self.comparison_sects[sect]["memory"]
                    for addr in range(len(memory)):
                        if memory[addr] is None:
                            continue
                        if 0 == (addr & 1):
                            c = "H"
                            if addr < len(amemory) and memory[addr] == amemory[addr]:
                                continue
                        else:
                            c = "L"
                            if addr < len(amemory) and memory[addr] == amemory[addr]:
                                continue
                        if not header_shown:
                            comparison_output.append(f'Missing object code from section "{sect}":')
                            header_shown = True
                        line = f"\t{addr // 2:05X}({c}): {memory[addr]:02X}"
                        comparison_output.append(line)
                        missing_count += 1
                source_names = ",".join(sf.stem for sf in self.source_files)
                summary = f"{source_names}: {mismatch_count} bytes mismatched and {missing_count} bytes missing in generated code"
                comparison_output.append(summary)
        
        self._log(f"Listing written: {listing_file}")
        
        # Return comparison results
        if self.comparison_sects is not None:
            return {
                "mismatch_count": mismatch_count,
                "missing_count": missing_count,
                "output": comparison_output
            }
        return None
    
    def _sort_order(self, s: str) -> str:
        """
        A peculiar collation for sorting the symbol table on the printout.
        It's not EBCDIC, nor ASCII. The alphanumeric ordering seems normal,
        but the other "letters" (#, @, $) follow the alphanumerics.
        """
        converted = ""
        for c in s:
            if c == "$":
                converted += 'a'
            elif c == "#":
                converted += 'b'
            else:
                converted += c
        return converted


