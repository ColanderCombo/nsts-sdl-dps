"""
This file is vendored copy of: 
    https://github.com/Zaneham/hals-lsp/blob/main/hals_semantic_parser.py
    
HAL/S Semantic Parser
Based on NASA HAL/S Language Specification and Programming in HAL/S (1978)

HAL/S (High-order Assembly Language/Shuttle) is a real-time aerospace
programming language developed by Intermetrics for NASA. It powered
approximately 85% of the Space Shuttle flight software.

Reference: NASA Technical Reports - HAL/S Language Specification (1974-1978)
"""

import re
from typing import List, Dict, Tuple, Optional, Set, Any
from dataclasses import dataclass, field
from enum import Enum


class SymbolKind(Enum):
    """Symbol kinds for HAL/S"""
    PROGRAM = "program"
    PROCEDURE = "procedure"
    FUNCTION = "function"
    TASK = "task"
    COMPOOL = "compool"
    VARIABLE = "variable"
    CONSTANT = "constant"
    STRUCTURE = "structure"
    LABEL = "label"
    PARAMETER = "parameter"
    REPLACE = "replace"


@dataclass
class Symbol:
    """A symbol in the HAL/S program"""
    name: str
    kind: SymbolKind
    data_type: str
    line: int
    column: int
    end_line: int = 0
    end_column: int = 0
    scope: str = "global"
    documentation: str = ""
    parameters: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    replacement_text: str = ""


@dataclass
class Reference:
    """A reference to a symbol"""
    name: str
    line: int
    column: int
    end_column: int = 0
    context: str = ""


@dataclass
class Diagnostic:
    """A diagnostic message"""
    line: int
    column: int
    end_column: int
    message: str
    severity: str = "error"


@dataclass
class Token:
    """A lexical token with an absolute (0-based) position in the source buffer."""
    kind: str   # 'ident' | 'kw' | 'percent' | 'number' | 'colon' | 'semi' | 'comma' | 'lparen' | 'rparen'
    value: str
    line: int
    col: int
    end_col: int


class HALSParser:
    """
    Parser for HAL/S (High-order Assembly Language/Shuttle)

    Based on NASA HAL/S Language Specification
    """

    # HAL/S keywords
    KEYWORDS = {
        # Program units
        'PROGRAM', 'PROCEDURE', 'FUNCTION', 'TASK', 'COMPOOL', 'UPDATE',
        'CLOSE', 'RETURN',

        # Declarations
        'DECLARE', 'CONSTANT', 'INITIAL', 'STATIC', 'AUTOMATIC',
        'TEMPORARY', 'DENSE', 'ALIGNED', 'RIGID', 'REMOTE', 'ACCESS',
        'ASSIGN', 'NAME', 'LOCK', 'EXCLUSIVE', 'LATCHED', 'REPLACE',
        'STRUCTURE', 'ARRAY',

        # Data types
        'INTEGER', 'SCALAR', 'VECTOR', 'MATRIX', 'BOOLEAN', 'CHARACTER',
        'BIT', 'EVENT', 'SINGLE', 'DOUBLE',

        # Control flow
        'IF', 'THEN', 'ELSE', 'DO', 'END', 'FOR', 'TO', 'BY', 'WHILE',
        'UNTIL', 'REPEAT', 'EXIT', 'GO', 'GOTO', 'CASE',

        # Real-time
        'SCHEDULE', 'WAIT', 'SIGNAL', 'PRIORITY', 'TERMINATE', 'CANCEL',
        'SET', 'RESET', 'ON', 'OFF', 'ERROR', 'DEPENDENT', 'IGNORE',

        # I/O
        'READ', 'READALL', 'WRITE', 'FILE',

        # Operators as keywords
        'NOT', 'AND', 'OR', 'CAT', 'MOD', 'TRUE', 'FALSE',

        # Built-in functions
        'ABS', 'CEILING', 'FLOOR', 'TRUNCATE', 'ROUND', 'ODD', 'SIGN',
        'MAX', 'MIN', 'SUM', 'PROD', 'SHL', 'SHR', 'SIZE', 'LENGTH',
        'INDEX', 'MIDVAL', 'RANDOM', 'RANDOMG', 'DATE', 'RUNTIME',
        'CLOCKTIME', 'PRIO', 'NEXTIME',

        # Math functions
        'SIN', 'COS', 'TAN', 'ARCSIN', 'ARCCOS', 'ARCTAN', 'ARCTAN2',
        'SINH', 'COSH', 'TANH', 'EXP', 'LOG', 'SQRT',

        # Vector/Matrix operations
        'TRANSPOSE', 'TRACE', 'DET', 'INVERSE', 'IDENTITY',
        'UNIT', 'ABVAL', 'DOT', 'CROSS',
    }

    MAX_MACRO_NESTING = 8

    # Program-unit header keywords and the symbol kind each introduces.
    UNIT_KEYWORDS = {
        'PROGRAM': SymbolKind.PROGRAM,
        'PROCEDURE': SymbolKind.PROCEDURE,
        'FUNCTION': SymbolKind.FUNCTION,
        'TASK': SymbolKind.TASK,
        'COMPOOL': SymbolKind.COMPOOL,
        'UPDATE': SymbolKind.PROCEDURE,
    }

    # Data-type keywords usable as a FUNCTION return type or DECLARE type.
    TYPE_KEYWORDS = {
        'INTEGER', 'SCALAR', 'VECTOR', 'MATRIX', 'BOOLEAN', 'CHARACTER',
        'BIT', 'EVENT', 'FIXED', 'DOUBLE', 'SINGLE',
    }

    def __init__(self):
        self.symbols: Dict[str, Symbol] = {}
        self.references: List[Reference] = []
        self.diagnostics: List[Diagnostic] = []
        self.lines: List[str] = []
        self._raw_text: str = ""
        self._tokens: List[Token] = []
        self._line_offsets: List[int] = []  # Start offset of each line for fast position lookup

    def _build_line_offsets(self, text: str) -> List[int]:
        """Build a table of character offsets for each line start.
        
        Returns a list where index i contains the character offset where line i begins.
        This enables O(log n) position lookups via binary search instead of O(n) string scans.
        """
        offsets = [0]  # Line 0 starts at offset 0
        for i, ch in enumerate(text):
            if ch == '\n':
                offsets.append(i + 1)
        return offsets

    def parse(self, text: str) -> None:
        """Parse HAL/S source code.

        Tokenizes a coordinate-preserving, masked copy of the source so every token
        position maps 1:1 onto the editor buffer, then extracts symbols structurally
        from that token stream (rather than via offset-shifting regex passes).
        """
        self.symbols = {}
        self.references = []
        self.diagnostics = []
        self.lines = text.split('\n')
        self._raw_text = text
        self._line_offsets = self._build_line_offsets(text)

        # Blank non-code card regions (comment/directive/2D cards, the cols-73-80
        # trailer) and string/comment content, all without shifting coordinates.
        masked = self._mask_card_structure(text)
        masked = self._mask_strings_and_comments(masked)

        self._tokens = self._tokenize(masked)

        # Structural extraction. Order matters: units and declares claim a name
        # before the label pass, which never overwrites an existing symbol.
        self._parse_units()
        self._parse_declares()
        self._parse_replaces()
        self._parse_labels()
        self._parse_references()

    def _is_comment_card_line(self, line: str) -> bool:
        """Return True for HAL/S column-1 C comment cards (C, C , C*, C@)."""
        if not line:
            return False
        if line[0].upper() != 'C':
            return False
        if len(line) == 1:
            return True
        return line[1] in (' ', '*', '@')

    def _is_multiline_card(self, line: str, designator: str) -> bool:
        """Return True for explicit E/S/M multiline cards using a card separator."""
        if not line:
            return False
        if line[0].upper() != designator:
            return False
        if len(line) == 1:
            return True
        return line[1] in (' ', '\t')

    def _is_directive_card_line(self, line: str) -> bool:
        """Column-1 'D' compiler-directive card, e.g. 'D INCLUDE ...' / 'D TEMPLATE ...'."""
        if len(line) < 2 or line[0].upper() != 'D' or line[1] not in (' ', '\t'):
            return False
        return bool(re.match(r'D\s+(INCLUDE|TEMPLATE)\b', line, re.IGNORECASE))

    def _blank_card_trailer(self, line: str) -> str:
        """Blank the fixed cols 73-80 sequence(6)+revision(2) trailer if present.

        Coordinate-preserving: replaces the trailer with spaces, keeping length.
        """
        if len(line) >= 78 and re.match(r'[0-9]{6}', line[72:78]):
            return line[:72] + ' ' * (len(line) - 72)
        return line

    def _mask_card_structure(self, text: str) -> str:
        """Blank non-code card regions while preserving every line/column.

        Column 1 (index 0) is the HAL/S card designator. Comment cards (C + space/
        */@), directive cards (D INCLUDE/TEMPLATE) and exponent/subscript cards
        (E/S) carry no parseable code and are blanked entirely. Main-line cards (M)
        keep their content but lose the designator. The cols-73-80 trailer is
        blanked on every card. Newlines and lengths are left untouched so token
        offsets still map onto the original buffer.
        """
        out_lines: List[str] = []
        for line in text.split('\n'):
            if not line:
                out_lines.append(line)
                continue
            if (self._is_comment_card_line(line)
                    or self._is_directive_card_line(line)
                    or self._is_multiline_card(line, 'E')
                    or self._is_multiline_card(line, 'S')):
                masked = ' ' * len(line)
            elif self._is_multiline_card(line, 'M'):
                masked = ' ' + line[1:]
            else:
                masked = line
            out_lines.append(self._blank_card_trailer(masked))
        return '\n'.join(out_lines)

    def _offset(self, line: int, col: int) -> int:
        """Absolute character offset of (0-based line, column) in the raw source."""
        if line < 0 or not self._line_offsets:
            return 0
        if line >= len(self._line_offsets):
            return len(self._raw_text)
        return min(self._line_offsets[line] + col, len(self._raw_text))

    def _tokenize(self, text: str) -> List[Token]:
        """Lex the (coordinate-preserving) masked text into positioned tokens."""
        tokens: List[Token] = []
        punct = {':': 'colon', ';': 'semi', ',': 'comma', '(': 'lparen', ')': 'rparen'}

        for line_no, line in enumerate(text.split('\n')):
            col = 0
            n = len(line)
            while col < n:
                ch = line[col]
                if ch.isspace():
                    col += 1
                elif ch == '%' and col + 1 < n and (line[col + 1].isalpha() or line[col + 1] == '_'):
                    start = col
                    col += 1
                    while col < n and (line[col].isalnum() or line[col] == '_'):
                        col += 1
                    tokens.append(Token('percent', line[start:col], line_no, start, col))
                elif ch.isalpha() or ch == '_':
                    start = col
                    while col < n and (line[col].isalnum() or line[col] == '_'):
                        col += 1
                    value = line[start:col]
                    kind = 'kw' if value.upper() in self.KEYWORDS else 'ident'
                    tokens.append(Token(kind, value, line_no, start, col))
                elif ch.isdigit():
                    start = col
                    while col < n and (line[col].isdigit() or line[col] == '.'):
                        col += 1
                    tokens.append(Token('number', line[start:col], line_no, start, col))
                elif ch in punct:
                    tokens.append(Token(punct[ch], ch, line_no, col, col + 1))
                    col += 1
                else:
                    col += 1
        return tokens

    def _statement_tokens(self, start: int) -> Tuple[List[Token], int]:
        """Tokens of the statement beginning at index `start`, up to (not incl.) ';'.

        Returns (tokens, index_after_semicolon). Statements naturally span
        continuation cards because the token stream ignores line breaks.
        """
        toks = self._tokens
        n = len(toks)
        j = start
        body: List[Token] = []
        while j < n and toks[j].kind != 'semi':
            body.append(toks[j])
            j += 1
        return body, (j + 1 if j < n else j)

    def _paren_idents(self, toks: List[Token], start: int) -> List[str]:
        """Uppercased identifiers inside the first parenthesized group at/after start."""
        i = start
        while i < len(toks) and toks[i].kind != 'lparen':
            i += 1
        names: List[str] = []
        depth = 0
        while i < len(toks):
            t = toks[i]
            if t.kind == 'lparen':
                depth += 1
            elif t.kind == 'rparen':
                depth -= 1
                if depth == 0:
                    break
            elif t.kind == 'ident' and depth == 1:
                names.append(t.value.upper())
            i += 1
        return names

    def _parse_units(self) -> None:
        """Parse program-unit headers: NAME: [TYPE] PROGRAM|PROCEDURE|FUNCTION|...

        Handles the return type appearing before or after FUNCTION, e.g.
        `F: SCALAR FUNCTION(X);` and `F: FUNCTION(X) INTEGER;`.
        """
        toks = self._tokens
        n = len(toks)
        i = 0
        while i < n:
            if toks[i].kind in ('ident', 'kw') and i + 1 < n and toks[i + 1].kind == 'colon':
                name_tok = toks[i]
                header, after = self._statement_tokens(i + 2)
                unit_idx = next(
                    (k for k, t in enumerate(header)
                     if t.kind == 'kw' and t.value.upper() in self.UNIT_KEYWORDS),
                    None,
                )
                if unit_idx is not None:
                    unit_kw = header[unit_idx].value.upper()
                    kind = self.UNIT_KEYWORDS[unit_kw]
                    params = self._paren_idents(header, unit_idx + 1)
                    data_type, doc = self._unit_type_and_doc(unit_kw, header, params)
                    self.symbols[name_tok.value.upper()] = Symbol(
                        name=name_tok.value.upper(),
                        kind=kind,
                        data_type=data_type,
                        line=name_tok.line,
                        column=name_tok.col,
                        parameters=params,
                        documentation=doc,
                    )
                    i = after
                    continue
            i += 1

    def _unit_type_and_doc(self, unit_kw: str, header: List[Token], params: List[str]) -> Tuple[str, str]:
        """Build (data_type, documentation) for a program-unit symbol."""
        if unit_kw == 'FUNCTION':
            ret = next(
                (t.value.upper() for t in header
                 if t.kind == 'kw' and t.value.upper() in self.TYPE_KEYWORDS),
                'SCALAR',
            )
            return f"{ret} FUNCTION", f"Function returning {ret}"
        if unit_kw in ('PROCEDURE', 'UPDATE'):
            doc = f"Procedure with {len(params)} parameters" if params else "Procedure"
            return ("UPDATE BLOCK" if unit_kw == 'UPDATE' else "PROCEDURE"), doc
        docs = {
            'PROGRAM': "HAL/S Program unit",
            'TASK': "Real-time task (schedulable process)",
            'COMPOOL': "Communication pool (shared data)",
        }
        return unit_kw, docs.get(unit_kw, unit_kw)

    def _parse_declares(self) -> None:
        """Parse DECLARE statements, including factored and multi-name forms:

            DECLARE NAME TYPE;
            DECLARE (A, B, C) BIT(16);
            DECLARE START BIT(16), DO_PRINT BIT(1);
            DECLARE (TEMP1, TEMP2) FIXED, MESSAGE CHARACTER;
        """
        toks = self._tokens
        n = len(toks)
        i = 0
        while i < n:
            if toks[i].kind == 'kw' and toks[i].value.upper() == 'DECLARE':
                body, after = self._statement_tokens(i + 1)
                self._parse_declare_body(body)
                i = after
                continue
            i += 1

    def _parse_declare_body(self, body: List[Token]) -> None:
        """Split a DECLARE body into comma-separated items and record each name."""
        m = len(body)
        p = 0
        while p < m:
            names: List[Token] = []
            t = body[p]
            if t.kind == 'lparen':
                # Factored name group: ( A, B, C )
                depth = 0
                while p < m:
                    tk = body[p]
                    if tk.kind == 'lparen':
                        depth += 1
                    elif tk.kind == 'rparen':
                        depth -= 1
                        if depth == 0:
                            p += 1
                            break
                    elif tk.kind == 'ident' and depth == 1:
                        names.append(tk)
                    p += 1
            elif t.kind == 'ident':
                names = [t]
                p += 1
            else:
                p += 1
                continue

            # Attributes/type run until the next top-level comma (or end of item).
            type_toks: List[Token] = []
            depth = 0
            while p < m:
                tk = body[p]
                if tk.kind == 'lparen':
                    depth += 1
                elif tk.kind == 'rparen':
                    depth -= 1
                elif tk.kind == 'comma' and depth == 0:
                    p += 1
                    break
                type_toks.append(tk)
                p += 1

            data_type, kind = self._classify_declaration(type_toks)
            for name_tok in names:
                if name_tok.value.upper() in self.KEYWORDS:
                    continue
                self.symbols[name_tok.value.upper()] = Symbol(
                    name=name_tok.value.upper(),
                    kind=kind,
                    data_type=data_type,
                    line=name_tok.line,
                    column=name_tok.col,
                    documentation=self._declaration_doc(kind, data_type),
                )

    def _classify_declaration(self, type_toks: List[Token]) -> Tuple[str, SymbolKind]:
        """Derive (data_type string, SymbolKind) from a declaration's attribute tokens."""
        words = {t.value.upper() for t in type_toks if t.kind in ('kw', 'ident')}
        if 'CONSTANT' in words:
            kind = SymbolKind.CONSTANT
        elif 'STRUCTURE' in words:
            kind = SymbolKind.STRUCTURE
        else:
            kind = SymbolKind.VARIABLE

        data_type = self._reconstruct_type(type_toks)
        if not data_type:
            data_type = kind.value.upper()
        return data_type, kind

    def _reconstruct_type(self, type_toks: List[Token]) -> str:
        """Compact type string from the raw source span of the attribute tokens.

        Reads the original buffer (so dimensions like `ARRAY(24) BIT(16)` are kept
        verbatim) and trims any trailing INITIAL(...) value clutter.
        """
        if not type_toks:
            return ""
        first, last = type_toks[0], type_toks[-1]
        raw = self._raw_text[self._offset(first.line, first.col):self._offset(last.line, last.end_col)]
        # Drop any card sequence/revision trailers picked up from continuation cards.
        raw = re.sub(r'[0-9]{6}[A-Z]{2}\b', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        raw = re.split(r'\bINITIAL\b', raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        return raw

    def _declaration_doc(self, kind: SymbolKind, data_type: str) -> str:
        if kind == SymbolKind.CONSTANT:
            return "Constant"
        if kind == SymbolKind.STRUCTURE:
            return "Structure type"
        return f"{data_type} variable"

    def _parse_replaces(self) -> None:
        """Parse REPLACE macro definitions: REPLACE NAME[(params)] BY "template".

        The name and parameters come from the token stream (so the name position is
        editor-accurate), while the quoted template is read from the raw buffer
        starting after BY — the template is blanked in the masked/tokenized text.
        """
        toks = self._tokens
        n = len(toks)
        text = self._raw_text
        i = 0
        while i < n:
            if not (toks[i].kind == 'kw' and toks[i].value.upper() == 'REPLACE'
                    and i + 1 < n and toks[i + 1].kind == 'ident'):
                i += 1
                continue

            name_tok = toks[i + 1]
            j = i + 2
            params: List[str] = []
            if j < n and toks[j].kind == 'lparen':
                depth = 0
                while j < n:
                    tk = toks[j]
                    if tk.kind == 'lparen':
                        depth += 1
                    elif tk.kind == 'rparen':
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    elif tk.kind == 'ident' and depth == 1:
                        params.append(tk.value.upper())
                    j += 1

            if not (j < n and toks[j].kind == 'kw' and toks[j].value.upper() == 'BY'):
                i += 1
                continue

            # Read the template from raw text after BY, skipping whitespace and any
            # card trailers between BY and the opening quote.
            scan = self._offset(toks[j].line, toks[j].end_col)
            replacement = ""
            while scan < len(text):
                while scan < len(text) and text[scan].isspace():
                    scan += 1
                trailer = re.match(r'[0-9]{6}[A-Z]{2}\b', text[scan:])
                if trailer:
                    scan += trailer.end()
                    continue
                break
            if scan < len(text) and text[scan] == '"':
                replacement, _ = self._read_hal_quoted_string(text, scan)

            details = f"Macro with parameters: {', '.join(params)}" if params else "Macro replacement"
            if replacement:
                preview = replacement.replace('\n', ' ')
                if len(preview) > 100:
                    preview = preview[:97] + '...'
                details = f"{details}. Expands to: {preview}"

            self.symbols[name_tok.value.upper()] = Symbol(
                name=name_tok.value.upper(),
                kind=SymbolKind.REPLACE,
                data_type="REPLACE",
                line=name_tok.line,
                column=name_tok.col,
                parameters=params,
                documentation=details,
                replacement_text=replacement,
            )
            i = j + 1

    def _read_hal_quoted_string(self, text: str, quote_index: int) -> Tuple[str, int]:
        """Read a HAL/S double-quoted string handling doubled quote escapes."""
        if quote_index >= len(text) or text[quote_index] != '"':
            return ("", quote_index)

        out: List[str] = []
        i = quote_index + 1
        n = len(text)

        while i < n:
            ch = text[i]
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    out.append('"')
                    i += 2
                    continue
                return (''.join(out), i + 1)
            out.append(ch)
            i += 1

        return (''.join(out), i)

    def _clean_hover_arg_text(self, value: str) -> str:
        """Normalize argument text from card-form source lines for hover display/substitution."""
        # Remove card sequence/revision trailers that can appear in continued calls.
        value = re.sub(r'\b[0-9]{6}[A-Z]{2}\b', ' ', value)
        # Collapse whitespace/newlines introduced by line continuations.
        value = re.sub(r'\s+', ' ', value)
        return value.strip()

    def _extract_call_arguments_from_lines(
        self,
        lines: List[str],
        line_no: int,
        token_end: int,
    ) -> Optional[List[str]]:
        """Extract macro call arguments from NAME(...) possibly spanning multiple lines."""
        if line_no < 0 or line_no >= len(lines):
            return None

        line_text = lines[line_no]
        i = token_end
        n = len(line_text)
        while i < n and line_text[i].isspace():
            i += 1
        if i >= n or line_text[i] != '(':
            return None

        args: List[str] = []
        current: List[str] = []
        depth = 0
        max_lines = min(len(lines), line_no + 40)

        for ln in range(line_no, max_lines):
            text = lines[ln]
            start_col = i if ln == line_no else 0

            col = start_col
            while col < len(text):
                ch = text[col]
                if ch == '(':
                    if depth >= 1:
                        current.append(ch)
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        arg = self._clean_hover_arg_text(''.join(current))
                        if arg or args:
                            args.append(arg)
                        return args
                    current.append(ch)
                elif ch == ',' and depth == 1:
                    args.append(self._clean_hover_arg_text(''.join(current)))
                    current = []
                else:
                    if depth >= 1:
                        current.append(ch)
                col += 1

            if depth >= 1:
                current.append('\n')

        return None

    def evaluate_replace_macro_with_trace(self, sym: Symbol, args: Optional[List[str]]) -> Tuple[str, List[Dict[str, Any]]]:
        """Evaluate REPLACE macro and return expansion plus substitution trace."""
        trace: List[Dict[str, Any]] = []
        result = self._expand_replace_symbol(sym, args, depth=1, trace=trace)
        # Drop trailing card sequence/revision trailers to keep hover readable.
        cleaned_lines = [re.sub(r'\s+[0-9]{6}[A-Z]{2}\s*$', '', ln) for ln in result.split('\n')]
        return ('\n'.join(cleaned_lines), trace)

    def _evaluate_replace_macro(self, sym: Symbol, args: Optional[List[str]]) -> str:
        """Evaluate REPLACE macro using compiler-like recursive scanner semantics."""
        expansion, _ = self.evaluate_replace_macro_with_trace(sym, args)
        return expansion

    def _expand_replace_symbol(
        self,
        sym: Symbol,
        args: Optional[List[str]],
        depth: int,
        trace: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Expand a REPLACE symbol and recursively scan substituted text."""
        template = sym.replacement_text or ""
        if not template:
            return ""

        if depth > self.MAX_MACRO_NESTING:
            return template

        params: Dict[str, str] = {}
        if sym.parameters and args is not None:
            for idx, param in enumerate(sym.parameters):
                if idx >= len(args):
                    break
                params[param.upper()] = args[idx]

        return self._macro_scan_text(template, params, depth, trace)

    def _macro_scan_text(
        self,
        text: str,
        params: Dict[str, str],
        depth: int,
        trace: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Compiler-like macro scanner: left-to-right with restart at inner substitution."""
        if depth > self.MAX_MACRO_NESTING:
            return text

        token_re = re.compile(r'[A-Z_][A-Z0-9_]*', re.IGNORECASE)
        result = text
        i = 0

        while i < len(result):
            match = token_re.search(result, i)
            if not match:
                break

            token = match.group(0)
            token_u = token.upper()
            start = match.start()
            end = match.end()

            # Parameter substitution, including token-paste form `PARAM`.
            if token_u in params:
                repl = params[token_u]
                if trace is not None:
                    trace.append({
                        'depth': depth,
                        'kind': 'parameter',
                        'name': token_u,
                        'value': repl,
                    })
                if start > 0 and end < len(result) and result[start - 1] == '`' and result[end] == '`':
                    result = result[:start - 1] + repl + result[end + 1:]
                    i = max(0, start - 1)
                else:
                    result = result[:start] + repl + result[end:]
                    i = max(0, start)
                continue

            # Replace-name substitution (with or without arguments).
            macro_sym = self.symbols.get(token_u)
            if macro_sym and macro_sym.kind == SymbolKind.REPLACE:
                call_args: Optional[List[str]] = None
                call_end = end

                scan = end
                while scan < len(result) and result[scan].isspace():
                    scan += 1
                if scan < len(result) and result[scan] == '(':
                    parsed = self._extract_call_arguments_from_lines([result], 0, end)
                    if parsed is not None:
                        call_args = parsed
                        close_idx = self._find_matching_paren(result, scan)
                        if close_idx is not None:
                            call_end = close_idx + 1
                elif macro_sym.parameters:
                    # Macro expects parameters but no call args were supplied.
                    i = end
                    continue

                nested = self._expand_replace_symbol(macro_sym, call_args, depth + 1, trace)
                if trace is not None:
                    trace.append({
                        'depth': depth,
                        'kind': 'macro',
                        'name': token_u,
                        'value': nested,
                    })
                result = result[:start] + nested + result[call_end:]
                i = max(0, start)
                continue

            i = end

        return result

    def _find_matching_paren(self, text: str, open_index: int) -> Optional[int]:
        """Find matching closing parenthesis index for text[open_index] == '('."""
        if open_index < 0 or open_index >= len(text) or text[open_index] != '(':
            return None

        depth = 0
        for idx in range(open_index, len(text)):
            ch = text[idx]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return idx
        return None

    def format_expansion_for_hover(self, expansion: str) -> str:
        """Format macro expansion for HAL/S hover code blocks.

        The HAL/S grammar treats column 1 as the card designator; pad content
        into column 2 so the first token isn't consumed as a card-type token.
        """
        if not expansion:
            return expansion

        formatted: List[str] = []
        for line in expansion.split('\n'):
            if not line:
                formatted.append(line)
                continue
            if line[0].isspace():
                formatted.append(line)
            else:
                formatted.append(' ' + line)
        return '\n'.join(formatted)

    def _escape_html(self, text: str) -> str:
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def format_trace_for_hover(self, trace: List[Dict[str, Any]], max_items: int = 24) -> str:
        """Render substitution trace lines with underlined substituted values."""
        if not trace:
            return ""

        lines: List[str] = []
        shown = trace[:max_items]
        for item in shown:
            depth = item.get('depth', 0)
            kind = item.get('kind', 'substitution')
            name = str(item.get('name', ''))
            value = str(item.get('value', ''))
            value_single = re.sub(r'\s+', ' ', value).strip()
            if len(value_single) > 100:
                value_single = value_single[:97] + '...'
            value_html = self._escape_html(value_single)
            lines.append(f"- d{depth} {kind} `{name}` -> <u>{value_html}</u>")

        if len(trace) > max_items:
            lines.append(f"- ... and {len(trace) - max_items} more")

        body = '\n'.join(lines)
        return (
            f"<details><summary>Substitutions ({len(trace)})</summary>\n\n"
            f"{body}\n"
            f"</details>"
        )

    def _mask_strings_and_comments(self, text: str) -> str:
        """Mask strings and block comments while preserving coordinates."""
        chars = list(text)
        i = 0
        n = len(chars)

        in_block_comment = False
        in_single = False
        in_double = False

        while i < n:
            ch = chars[i]
            nxt = chars[i + 1] if i + 1 < n else ''

            if in_block_comment:
                if ch == '*' and nxt == '/':
                    chars[i] = ' '
                    chars[i + 1] = ' '
                    i += 2
                    in_block_comment = False
                else:
                    if ch != '\n':
                        chars[i] = ' '
                    i += 1
                continue

            if in_single:
                if ch == '\'' and nxt == '\'':
                    chars[i] = ' '
                    chars[i + 1] = ' '
                    i += 2
                    continue
                if ch == '\'':
                    chars[i] = ' '
                    i += 1
                    in_single = False
                    continue
                if ch != '\n':
                    chars[i] = ' '
                i += 1
                continue

            if in_double:
                if ch == '"' and nxt == '"':
                    chars[i] = ' '
                    chars[i + 1] = ' '
                    i += 2
                    continue
                if ch == '"':
                    chars[i] = ' '
                    i += 1
                    in_double = False
                    continue
                if ch != '\n':
                    chars[i] = ' '
                i += 1
                continue

            if ch == '/' and nxt == '*':
                chars[i] = ' '
                chars[i + 1] = ' '
                i += 2
                in_block_comment = True
                continue

            if ch == '\'':
                chars[i] = ' '
                i += 1
                in_single = True
                continue

            if ch == '"':
                chars[i] = ' '
                i += 1
                in_double = True
                continue

            i += 1

        return ''.join(chars)

    def get_semantic_tokens(self) -> List[Tuple[int, int, int, int, int]]:
        """Return semantic tokens as (line, start, length, tokenType, tokenModifiers)."""
        # Legend is defined by the language server as tokenTypes=['macro'] and
        # tokenModifiers=['declaration'].
        macro_type_index = 0
        declaration_modifier = 1 << 0

        tokens: List[Tuple[int, int, int, int, int]] = []
        replace_defs: Set[Tuple[int, int]] = set()
        replace_names: Set[str] = set()
        first_replace_def_pos: Dict[str, Tuple[int, int]] = {}

        if not self.lines:
            return sorted(tokens, key=lambda t: (t[0], t[1]))

        masked_text = self._mask_strings_and_comments('\n'.join(self.lines))
        masked_lines = masked_text.split('\n')

        for line_no, line_text in enumerate(masked_lines):
            if not line_text:
                continue

            # Column-1 C card lines are full-line comments in HAL/S source decks.
            original_line = self.lines[line_no] if line_no < len(self.lines) else ""
            if original_line and self._is_comment_card_line(original_line):
                continue

            # Declarations are found directly from raw lines to keep positions
            # aligned with the editor buffer (card type prefixes included).
            search_windows = [(line_text, 0)]
            if line_text and line_text[0].isalpha():
                search_windows.append((line_text[1:], 1))

            for window_text, offset in search_windows:
                for match in re.finditer(
                    r'\bREPLACE\s+([A-Z_][A-Z0-9_]*)(?:\s*\(([^)]*)\))?\s+BY\b',
                    window_text,
                    re.IGNORECASE,
                ):
                    decl_col = offset + match.start(1)
                    pos = (line_no, decl_col)
                    if pos in replace_defs:
                        continue
                    replace_defs.add(pos)
                    macro_name = match.group(1).upper()
                    replace_names.add(macro_name)
                    if macro_name not in first_replace_def_pos:
                        first_replace_def_pos[macro_name] = pos
                    tokens.append((
                        line_no,
                        decl_col,
                        len(match.group(1)),
                        macro_type_index,
                        declaration_modifier,
                    ))

            # %MACRO and %MACRO(...) forms should always render as macro calls.
            for match in re.finditer(r'%[A-Z_][A-Z0-9_]*', line_text, re.IGNORECASE):
                tokens.append((
                    line_no,
                    match.start(),
                    match.end() - match.start(),
                    macro_type_index,
                    0,
                ))

            if not replace_names:
                continue

            for match in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\b', line_text, re.IGNORECASE):
                name = match.group(1).upper()
                if name not in replace_names:
                    continue

                def_pos = first_replace_def_pos.get(name)
                if def_pos and (line_no, match.start(1)) <= def_pos:
                    continue

                start = match.start(1)
                pos = (line_no, start)

                # Already emitted as a declaration token.
                if pos in replace_defs:
                    continue

                # Skip NAME part of %NAME; it is covered by the %MACRO token.
                if start > 0 and line_text[start - 1] == '%':
                    continue

                tokens.append((
                    line_no,
                    start,
                    len(match.group(1)),
                    macro_type_index,
                    0,
                ))

        return sorted(tokens, key=lambda t: (t[0], t[1]))

    def _parse_labels(self) -> None:
        """Parse statement labels: NAME: at the start of a statement.

        A label colon appears at statement start (previous token is None, ';', or a
        chained label ':'); this excludes mid-expression colons such as the range in
        a subscript `A$(I:J)`. Names already claimed as a unit/declare are left
        untouched (the dict keys disambiguation by parse order).
        """
        toks = self._tokens
        n = len(toks)
        for idx in range(n):
            t = toks[idx]
            if t.kind != 'ident' or idx + 1 >= n or toks[idx + 1].kind != 'colon':
                continue

            prev = toks[idx - 1] if idx > 0 else None
            if prev is not None and prev.kind not in ('semi', 'colon'):
                continue

            # Ignore range syntax like DGM_J:1 (a number after the colon).
            nxt = toks[idx + 2] if idx + 2 < n else None
            if nxt is not None and nxt.kind == 'number':
                continue

            name = t.value.upper()
            if name in self.KEYWORDS or name in self.symbols:
                continue

            self.symbols[name] = Symbol(
                name=name,
                kind=SymbolKind.LABEL,
                data_type="LABEL",
                line=t.line,
                column=t.col,
                documentation="Statement label (GO TO target)",
            )

    def _parse_references(self) -> None:
        """Record every identifier token (non-keyword) as a reference."""
        for t in self._tokens:
            if t.kind != 'ident':
                continue
            name = t.value.upper()
            if name in self.KEYWORDS:
                continue
            self.references.append(Reference(
                name=name,
                line=t.line,
                column=t.col,
                end_column=t.end_col,
            ))

    def get_symbols(self) -> Dict[str, Symbol]:
        """Get all parsed symbols"""
        return self.symbols

    def get_references(self) -> List[Reference]:
        """Get all references"""
        return self.references

    def get_diagnostics(self) -> List[Diagnostic]:
        """Get all diagnostics"""
        return self.diagnostics

    def get_completions(self, line: int, column: int) -> List[Dict]:
        """Get completion items at position"""
        completions = []

        # Add keywords
        for kw in sorted(self.KEYWORDS):
            completions.append({
                'label': kw,
                'kind': 'keyword',
                'detail': 'HAL/S keyword',
                'documentation': f"HAL/S keyword: {kw}"
            })

        # Add symbols
        for name, sym in self.symbols.items():
            completions.append({
                'label': sym.name,
                'kind': sym.kind.value,
                'detail': sym.data_type,
                'documentation': sym.documentation
            })

        return completions

    def get_hover(self, line: int, column: int) -> Optional[Dict]:
        """Get hover information at position"""
        if line >= len(self.lines):
            return None

        line_text = self.lines[line]

        # Find the word at this position
        for match in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\b', line_text, re.IGNORECASE):
            if match.start() <= column <= match.end():
                word = match.group(1).upper()

                if word in self.KEYWORDS:
                    return {'contents': f"**{word}**\n\nHAL/S keyword"}

                if word in self.symbols:
                    sym = self.symbols[word]
                    if sym.kind == SymbolKind.REPLACE:
                        args = self._extract_call_arguments_from_lines(self.lines, line, match.end())
                        expansion, trace = self.evaluate_replace_macro_with_trace(sym, args)

                        lines = [f"**{sym.name}**: {sym.data_type}"]
                        if sym.parameters:
                            lines.append(f"\nParameters: `{', '.join(sym.parameters)}`")

                        if expansion:
                            display_expansion = self.format_expansion_for_hover(expansion)
                            lines.append("\nExpands to:")
                            lines.append(f"```hals\n{display_expansion}\n```")
                            trace_md = self.format_trace_for_hover(trace)
                            if trace_md:
                                lines.append(f"\n{trace_md}")
                        elif sym.documentation:
                            lines.append(f"\n{sym.documentation}")

                        return {'contents': '\n'.join(lines)}

                    return {'contents': f"**{sym.name}**: {sym.data_type}\n\n{sym.documentation}"}
                break

        return None

    def get_definition(self, line: int, column: int) -> Optional[Dict]:
        """Get definition location for symbol at position"""
        if line >= len(self.lines):
            return None

        line_text = self.lines[line]

        for match in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\b', line_text, re.IGNORECASE):
            if match.start() <= column <= match.end():
                word = match.group(1).upper()
                if word in self.symbols:
                    sym = self.symbols[word]
                    return {
                        'line': sym.line,
                        'column': sym.column,
                        'name': sym.name
                    }
                break

        return None

    def get_references_at(self, line: int, column: int) -> List[Dict]:
        """Get all references to symbol at position"""
        if line >= len(self.lines):
            return []

        line_text = self.lines[line]
        target_word = None

        for match in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\b', line_text, re.IGNORECASE):
            if match.start() <= column <= match.end():
                target_word = match.group(1).upper()
                break

        if not target_word:
            return []

        refs = []
        for ref in self.references:
            if ref.name == target_word:
                refs.append({
                    'line': ref.line,
                    'column': ref.column,
                    'end_column': ref.end_column
                })

        return refs

    def get_document_symbols(self) -> List[Dict]:
        """Get all document symbols for outline"""
        symbols = []
        for name, sym in self.symbols.items():
            symbols.append({
                'name': sym.name,
                'kind': sym.kind.value,
                'detail': sym.data_type,
                'line': sym.line,
                'column': sym.column
            })
        return sorted(symbols, key=lambda x: x['line'])


def main():
    """Test the parser"""
    test_code = '''
C HAL/S TEST PROGRAM FOR SPACE SHUTTLE FLIGHT SOFTWARE
C DEMONSTRATES BASIC LANGUAGE FEATURES

SHUTTLE_NAV: PROGRAM;
    /* Declare constants */
    DECLARE PI CONSTANT(3.14159265);
    DECLARE G CONSTANT(9.80665);
    DECLARE EARTH_RADIUS CONSTANT(6371.0);

    /* Declare variables */
    DECLARE ALTITUDE SCALAR;
    DECLARE VELOCITY VECTOR(3);
    DECLARE POSITION VECTOR(3);
    DECLARE ATTITUDE MATRIX(3,3);
    DECLARE MISSION_TIME SCALAR;
    DECLARE ENGINE_ON BOOLEAN;
    DECLARE ABORT_FLAG EVENT;

    /* Arrays */
    DECLARE SENSOR_DATA ARRAY(10) SCALAR;
    DECLARE THRUSTER_STATUS ARRAY(6) BOOLEAN;

    /* Read initial state */
    READ(5) ALTITUDE, VELOCITY, POSITION;

    /* Navigation loop */
NAV_LOOP:
    DO WHILE ENGINE_ON;
        MISSION_TIME = MISSION_TIME + 0.1;

        /* Update position */
        POSITION = POSITION + VELOCITY * 0.1;

        /* Check altitude */
        IF ALTITUDE < 100.0 THEN
            SIGNAL ABORT_FLAG;

        /* Continue navigation */
        GO TO NAV_LOOP;
    END;

    WRITE(6) POSITION, VELOCITY, MISSION_TIME;

CLOSE SHUTTLE_NAV;

/* Guidance function */
COMPUTE_THRUST: SCALAR FUNCTION(MASS, ACCEL);
    DECLARE MASS SCALAR;
    DECLARE ACCEL VECTOR(3);
    DECLARE THRUST SCALAR;

    THRUST = MASS * ABVAL(ACCEL);
    RETURN THRUST;
CLOSE COMPUTE_THRUST;

/* Real-time navigation task */
NAV_TASK: TASK;
    DECLARE STATE VECTOR(6);

    DO WHILE TRUE;
        WAIT FOR 0.1 SECONDS;
        /* Update navigation state */
        STATE = STATE;
    END;
CLOSE NAV_TASK;
'''

    parser = HALSParser()
    parser.parse(test_code)

    print("=" * 60)
    print("HAL/S SEMANTIC PARSER TEST")
    print("NASA Space Shuttle Flight Software Language")
    print("=" * 60)
    print()

    print("SYMBOLS FOUND:")
    print("-" * 40)
    for name, sym in sorted(parser.symbols.items()):
        print(f"  {sym.name:20} {sym.kind.value:12} {sym.data_type}")
    print()

    print("COMPLETIONS (first 15):")
    print("-" * 40)
    completions = parser.get_completions(0, 0)
    for c in completions[:15]:
        print(f"  {c['label']:20} {c['kind']:12} {c['detail']}")
    print()

    print("DOCUMENT SYMBOLS:")
    print("-" * 40)
    for sym in parser.get_document_symbols():
        print(f"  Line {sym['line']:3}: {sym['name']:20} ({sym['kind']})")


if __name__ == '__main__':
    main()
