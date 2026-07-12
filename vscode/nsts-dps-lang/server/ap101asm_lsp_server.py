#!/usr/bin/env python3
"""
AP-101 Assembler Language Server (browse-only)

Provides code *navigation* — not assembly — for AP-101 / IBM-style assembler
source in the Space Shuttle PFS tree: document outline, go-to-definition,
find-references, hover, and COPY/macro member navigation.
"""

import json
import sys
import re
import hashlib
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Dict, List, Optional, Any, Tuple, Set


# --------------------------------------------------------------------------- #
# Assembler card parsing
# --------------------------------------------------------------------------- #

# Identifier / ordinary-symbol charset. HLASM symbols are letters, digits and
# @ # $  _ . Sequence symbols (.X) and variable symbols (&X)
# are deliberately NOT ordinary symbols and are excluded as definitions.
_IDENT = r'[A-Za-z@#$_][A-Za-z0-9@#$_]*'
_RE_IDENT = re.compile(_IDENT)

# Cards may carry a col 73-80 trailer: 6-digit sequence + 2-char revision 
# (e.g. "000100AA"); col 72 is the continuation flag. Content is 1-71.
_RE_SEQ_TRAILER = re.compile(r'[0-9]{6}[0-9A-Za-z]{2}\s*$')

# Operation-field classification → definition kind. Anything producing a
# name-field label that the linker can see is tagged global (see GLOBAL_KINDS).
_SECTION_OPS = {'CSECT': 'csect', 'RSECT': 'csect', 'START': 'csect',
                'COM': 'csect', 'DSECT': 'dsect'}
_PROGRAM_OPS = {'PROGRAM'} # macro
_EQU_OPS = {'EQU'}
_STORAGE_OPS = {'DC', 'DS', 'DXD', 'CXD'}

# Pure listing-control directives: a name field here is a deck-id or ignored,
_NON_DEF_OPS = {'TITLE', 'SPACE', 'EJECT', 'PRINT'}

# Kinds visible to the linker / other assemblies.
GLOBAL_KINDS = {'csect', 'dsect', 'program', 'macro'}

# LSP SymbolKind numbers (subset).
_LSP_KIND = {
    'program': 2,    # Module
    'csect': 5,      # Class
    'dsect': 5,      # Class
    'macro': 12,     # Function
    'constant': 14,  # Constant
    'variable': 8,   # Field
    'label': 20,     # Key
}


def _is_comment_card(line: str) -> bool:
    """True for a full-card comment: '*' in column 1, or a '.* ' macro comment."""
    if not line:
        return False
    if line[0] == '*':
        return True
    if line[0] == '.' and len(line) > 1 and line[1] == '*':
        return True
    return False


def _card_content(raw: str) -> str:
    line = raw.rstrip('\r\n')
    if len(line) >= 73 and _RE_SEQ_TRAILER.search(line):
        return line[:71]
    return line


def _mask_quotes(text: str) -> str:
    out = list(text)
    in_str = False
    for i, ch in enumerate(text):
        if ch == "'":
            out[i] = ' '
            in_str = not in_str
        elif in_str:
            out[i] = ' '
    return ''.join(out)


def _split_fields(content: str) -> Dict[str, Any]:
    name = operation = operand = ''
    name_col = op_col = operand_col = -1

    # Name field: column 0 up to first blank (blank col 0 ⇒ no name).
    if content and not content[0].isspace():
        m = _RE_IDENT.match(content) or re.match(r'\S+', content)
        name = m.group(0)
        name_col = 0
        i = m.end()
    else:
        i = 0

    # Operation field.
    while i < len(content) and content[i].isspace():
        i += 1
    if i < len(content):
        m = re.match(r'\S+', content[i:])
        operation = m.group(0)
        op_col = i
        i = m.end() + i

    # Operand field: up to the first unquoted blank.
    while i < len(content) and content[i].isspace():
        i += 1
    if i < len(content):
        operand_col = i
        rest = content[i:]
        masked = _mask_quotes(rest)
        end = masked.find(' ')
        operand = rest if end < 0 else rest[:end]

    return {
        'name': name, 'name_col': name_col,
        'operation': operation, 'op_col': op_col,
        'operand': operand, 'operand_col': operand_col,
    }


def parse_source(text: str) -> Dict[str, Any]:
    defs: List[Dict[str, Any]] = []
    refs: List[Dict[str, Any]] = []
    copies: List[Dict[str, Any]] = []
    entry_names: Set[str] = set()

    awaiting_proto = False   # next statement after MACRO is the prototype
    raw_lines = text.split('\n')

    for line_no, raw in enumerate(raw_lines):
        if _is_comment_card(raw):
            continue
        content = _card_content(raw)
        if not content.strip():
            continue

        f = _split_fields(content)
        name, operation = f['name'], f['operation'].upper()
        op_raw = f['operation']

        # Macro prototype: the statement after MACRO names the macro in its
        # operation field (name field, if any, is a symbolic parameter).
        if awaiting_proto:
            awaiting_proto = False
            if op_raw:
                defs.append({
                    'name': op_raw, 'line': line_no, 'column': f['op_col'],
                    'kind': 'macro', 'operation': 'MACRO',
                    'detail': content.strip(), 'is_global': True,
                })
            continue

        if operation == 'MACRO' and not name:
            awaiting_proto = True
            continue

        # Name-field definition (skip sequence '.X' and variable '&X' symbols,
        # and names on listing directives, which define nothing).
        if name and name[0] not in '.&' and operation not in _NON_DEF_OPS:
            if operation in _SECTION_OPS:
                kind = _SECTION_OPS[operation]
            elif operation in _PROGRAM_OPS:
                kind = 'program'
            elif operation in _EQU_OPS:
                kind = 'constant'
            elif operation in _STORAGE_OPS:
                kind = 'variable'
            else:
                kind = 'label'
            defs.append({
                'name': name, 'line': line_no, 'column': f['name_col'],
                'kind': kind, 'operation': op_raw,
                'detail': content.strip(),
                'is_global': kind in GLOBAL_KINDS,
            })

        # COPY member reference (its own kind of cross-file navigation).
        if operation == 'COPY' and f['operand']:
            member = _RE_IDENT.match(f['operand'])
            if member:
                copies.append({
                    'name': member.group(0), 'line': line_no,
                    'column': f['operand_col'],
                    'end_column': f['operand_col'] + member.end(),
                })

        # ENTRY exports make the named local defs globally visible.
        if operation == 'ENTRY' and f['operand']:
            for m in _RE_IDENT.finditer(_mask_quotes(f['operand'])):
                entry_names.add(m.group(0).upper())

        # Operation-field symbol use (macro call sites resolve through this);
        # skip the MEND/MACRO bracketing keywords.
        if op_raw and op_raw[0] not in '.&' and operation not in ('MACRO', 'MEND'):
            refs.append({
                'name': op_raw, 'line': line_no, 'column': f['op_col'],
                'end_column': f['op_col'] + len(op_raw),
            })

        # Operand-field symbol uses (EXTRN targets, address operands, …).
        if f['operand']:
            base = f['operand_col']
            for m in _RE_IDENT.finditer(_mask_quotes(f['operand'])):
                refs.append({
                    'name': m.group(0), 'line': line_no, 'column': base + m.start(),
                    'end_column': base + m.end(),
                })

    # Promote ENTRY-exported local defs to global.
    if entry_names:
        for d in defs:
            if d['name'].upper() in entry_names:
                d['is_global'] = True

    return {'defs': defs, 'refs': refs, 'copies': copies}


# --------------------------------------------------------------------------- #
# Language server
# --------------------------------------------------------------------------- #

class AP101AsmLanguageServer:
    """LSP server for AP-101 assembler."""

    _RE_OI_DIR = re.compile(r'OI\d{6,7}', re.IGNORECASE)
    _RE_NON_ALNUM = re.compile(r'[^A-Z0-9]')

    def __init__(self):
        self.documents: Dict[str, str] = {}
        self.running = True
        self.enable_workspace_index = True
        self.search_subdirs = ['SSSRC', 'MLIB80', 'APPLSRC', 'INCL80']
        self._index_cache: Dict[str, Dict[str, Any]] = {}

    # -- JSON-RPC framing (generic; mirrors hals_lsp_server) ----------------- #

    def start(self):
        while self.running:
            try:
                message = self._read_message()
                if message:
                    response = self._handle_message(message)
                    if response:
                        self._send_message(response)
            except Exception as e:
                self._log(f"Error: {e}")

    def _read_message(self) -> Optional[Dict]:
        stdin = sys.stdin.buffer
        headers = {}
        while True:
            line = stdin.readline()
            if not line:
                self.running = False
                return None
            line = line.strip()
            if not line:
                break
            if b':' in line:
                key, value = line.split(b':', 1)
                headers[key.strip().decode('ascii')] = value.strip().decode('ascii')
        if 'Content-Length' not in headers:
            return None
        length = int(headers['Content-Length'])
        content = stdin.read(length).decode('utf-8')
        return json.loads(content)

    def _send_message(self, message: Dict):
        content = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
        sys.stdout.buffer.write(header + content)
        sys.stdout.buffer.flush()

    def _send_notification(self, method: str, params: Dict):
        self._send_message({'jsonrpc': '2.0', 'method': method, 'params': params})

    def _log(self, message: str):
        self._send_notification('window/logMessage',
                                {'type': 3, 'message': f"[AP-101] {message}"})

    def _handle_message(self, message: Dict) -> Optional[Dict]:
        method = message.get('method', '')
        msg_id = message.get('id')
        params = message.get('params', {})

        handlers = {
            'initialize': self._handle_initialize,
            'initialized': self._handle_initialized,
            'shutdown': self._handle_shutdown,
            'exit': self._handle_exit,
            'textDocument/didOpen': self._handle_did_open,
            'textDocument/didChange': self._handle_did_change,
            'textDocument/didClose': self._handle_did_close,
            'textDocument/definition': self._handle_definition,
            'textDocument/references': self._handle_references,
            'textDocument/documentSymbol': self._handle_document_symbol,
            'textDocument/hover': self._handle_hover,
        }
        handler = handlers.get(method)
        if handler:
            result = handler(params)
            if msg_id is not None:
                return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}
        elif msg_id is not None:
            return {'jsonrpc': '2.0', 'id': msg_id,
                    'error': {'code': -32601, 'message': f"Method not found: {method}"}}
        return None

    def _handle_initialize(self, params: Dict) -> Dict:
        opts = params.get('initializationOptions', {}) or {}
        self.enable_workspace_index = bool(opts.get('definitionEnableWorkspaceIndex', True))
        subdirs = opts.get('definitionSearchSubdirs')
        if isinstance(subdirs, list):
            cleaned = [str(s).strip() for s in subdirs if str(s).strip()]
            if cleaned:
                self.search_subdirs = cleaned
        return {
            'capabilities': {
                'textDocumentSync': {'openClose': True, 'change': 1},
                'definitionProvider': True,
                'referencesProvider': True,
                'documentSymbolProvider': True,
                'hoverProvider': True,
            },
            'serverInfo': {'name': 'AP-101 Assembler Language Server', 'version': '0.1.0'},
        }

    def _handle_initialized(self, params: Dict) -> None:
        self._log("AP-101 browse server ready")
        return None

    def _handle_shutdown(self, params: Dict) -> None:
        return None

    def _handle_exit(self, params: Dict) -> None:
        self.running = False
        return None

    # -- Document store ------------------------------------------------------ #

    def _handle_did_open(self, params: Dict) -> None:
        doc = params.get('textDocument', {})
        self.documents[doc.get('uri', '')] = doc.get('text', '')
        return None

    def _handle_did_change(self, params: Dict) -> None:
        doc = params.get('textDocument', {})
        uri = doc.get('uri', '')
        changes = params.get('contentChanges', [])
        if changes:
            self.documents[uri] = changes[0].get('text', '')
        return None

    def _handle_did_close(self, params: Dict) -> None:
        self.documents.pop(params.get('textDocument', {}).get('uri', ''), None)
        return None

    # -- Path / URI helpers -------------------------------------------------- #

    def _uri_to_path(self, uri: str) -> Optional[Path]:
        try:
            parsed = urlparse(uri)
            if parsed.scheme != 'file':
                return None
            return Path(unquote(parsed.path))
        except Exception:
            return None

    def _path_to_uri(self, path: Path) -> str:
        return path.absolute().as_uri()

    def _find_oi_root(self, file_path: Path) -> Optional[Path]:
        for parent in [file_path.parent] + list(file_path.parents):
            if self._RE_OI_DIR.fullmatch(parent.name.upper()):
                return parent
        return None

    def _normalize_member_name(self, text: str) -> str:
        return self._RE_NON_ALNUM.sub('', text.upper())

    def _token_at(self, line_text: str, char: int) -> Optional[Dict[str, Any]]:
        """Ordinary-symbol token under the cursor."""
        if char < 0:
            return None
        for m in _RE_IDENT.finditer(line_text):
            if m.start() <= char <= m.end():
                return {'text': m.group(0), 'start': m.start(), 'end': m.end()}
        return None

    def _parsed_doc(self, uri: str) -> Optional[Dict[str, Any]]:
        text = self.documents.get(uri)
        if text is None:
            return None
        return parse_source(text)

    # -- Workspace index (global symbols only — the linker's-eye view) ------- #

    def _build_index(self, oi_root: Path) -> Dict[str, Any]:
        global_defs: Dict[str, List[Dict[str, Any]]] = {}
        refs: Dict[str, List[Dict[str, Any]]] = {}
        include_by_norm: Dict[str, List[Path]] = {}
        max_file_bytes = 2 * 1024 * 1024

        for subdir in self.search_subdirs:
            root = oi_root / subdir
            if not root.is_dir():
                continue
            for fp in root.rglob('*'):
                if not fp.is_file() or fp.name.startswith('.'):
                    continue
                try:
                    if fp.stat().st_size > max_file_bytes:
                        continue
                    text = fp.read_text(encoding='utf-8', errors='replace')
                except Exception:
                    continue

                norm = self._normalize_member_name(fp.name)
                if norm:
                    include_by_norm.setdefault(norm, []).append(fp)

                parsed = parse_source(text)
                for d in parsed['defs']:
                    if not d['is_global']:
                        continue
                    global_defs.setdefault(d['name'].upper(), []).append({
                        'path': fp, 'line': d['line'], 'column': d['column'],
                        'name': d['name'], 'kind': d['kind'], 'detail': d['detail'],
                    })
                for r in parsed['refs']:
                    refs.setdefault(r['name'].upper(), []).append({
                        'path': fp, 'line': r['line'], 'column': r['column'],
                        'end_column': r['end_column'], 'name': r['name'],
                    })
        return {'global_defs': global_defs, 'refs': refs,
                'include_by_norm': include_by_norm}

    def _index_cache_file(self, oi_root: Path) -> Path:
        cache_root = Path.home() / '.cache' / 'ap101asm-lsp'
        cache_root.mkdir(parents=True, exist_ok=True)
        key_text = f"v1|{oi_root.resolve()}|{'|'.join(self.search_subdirs)}"
        key = hashlib.sha1(key_text.encode('utf-8')).hexdigest()[:16]
        return cache_root / f'index-{key}.json'

    def _serialize_index(self, index: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {'global_defs': {}, 'refs': {}, 'include_by_norm': {}}
        for k, defs in index['global_defs'].items():
            out['global_defs'][k] = [{**d, 'path': str(d['path'])} for d in defs]
        for k, rs in index['refs'].items():
            out['refs'][k] = [{**r, 'path': str(r['path'])} for r in rs]
        for k, paths in index['include_by_norm'].items():
            out['include_by_norm'][k] = [str(p) for p in paths]
        return out

    def _deserialize_index(self, data: Dict[str, Any]) -> Dict[str, Any]:
        gd: Dict[str, List[Dict[str, Any]]] = {}
        rf: Dict[str, List[Dict[str, Any]]] = {}
        inc: Dict[str, List[Path]] = {}
        for k, defs in (data.get('global_defs') or {}).items():
            gd[k] = [{**d, 'path': Path(d['path'])} for d in defs]
        for k, rs in (data.get('refs') or {}).items():
            rf[k] = [{**r, 'path': Path(r['path'])} for r in rs]
        for k, paths in (data.get('include_by_norm') or {}).items():
            inc[k] = [Path(p) for p in paths]
        return {'global_defs': gd, 'refs': rf, 'include_by_norm': inc}

    def _get_index(self, oi_root: Path) -> Dict[str, Any]:
        key = str(oi_root.resolve())
        if key not in self._index_cache:
            cache_file = self._index_cache_file(oi_root)
            loaded = None
            if cache_file.exists():
                try:
                    loaded = self._deserialize_index(
                        json.loads(cache_file.read_text(encoding='utf-8')))
                except Exception:
                    loaded = None
            if loaded is None:
                loaded = self._build_index(oi_root)
                try:
                    cache_file.write_text(
                        json.dumps(self._serialize_index(loaded), separators=(',', ':')),
                        encoding='utf-8')
                except Exception:
                    pass
            self._index_cache[key] = loaded
        return self._index_cache[key]

    def _index_for_uri(self, uri: str) -> Optional[Tuple[Dict[str, Any], Path]]:
        if not self.enable_workspace_index:
            return None
        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return None
        oi_root = self._find_oi_root(doc_path)
        if not oi_root:
            return None
        return self._get_index(oi_root), oi_root

    # -- Request handlers ---------------------------------------------------- #

    def _handle_document_symbol(self, params: Dict) -> List[Dict]:
        """Outline of the open buffer — works with no workspace index."""
        uri = params.get('textDocument', {}).get('uri', '')
        parsed = self._parsed_doc(uri)
        if not parsed:
            return []
        result = []
        for d in parsed['defs']:
            rng = {
                'start': {'line': d['line'], 'character': d['column']},
                'end': {'line': d['line'], 'character': d['column'] + len(d['name'])},
            }
            result.append({
                'name': d['name'],
                'kind': _LSP_KIND.get(d['kind'], 13),
                'detail': d['operation'].upper(),
                'range': rng, 'selectionRange': rng,
            })
        return result

    def _handle_definition(self, params: Dict) -> Optional[Dict]:
        uri = params.get('textDocument', {}).get('uri', '')
        pos = params.get('position', {})
        line, char = pos.get('line', 0), pos.get('character', 0)
        parsed = self._parsed_doc(uri)
        if not parsed:
            return None
        doc_lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(doc_lines):
            return None
        token = self._token_at(doc_lines[line], char)
        if not token:
            return None
        name_u = token['text'].upper()

        # 1. COPY member under the cursor → resolve the member file.
        for c in parsed['copies']:
            if c['line'] == line and c['column'] <= char <= c['end_column']:
                target = self._resolve_member(uri, c['name'])
                if target:
                    return self._loc(target, 0, 0, 1)

        # 2. In-file definition (local labels, EQU, DC/DS) — no index needed.
        local = [d for d in parsed['defs'] if d['name'].upper() == name_u]
        if local:
            d = local[0]
            return self._loc(self._uri_to_path(uri), d['line'], d['column'],
                             d['column'] + len(d['name']), uri=uri)

        # 3. Cross-file fallback: global symbols only.
        idx = self._index_for_uri(uri)
        if idx:
            defs = idx[0]['global_defs'].get(name_u, [])
            if defs:
                d = defs[0]
                return self._loc(d['path'], d['line'], d['column'],
                                 d['column'] + len(d['name']))
        return None

    def _handle_references(self, params: Dict) -> List[Dict]:
        uri = params.get('textDocument', {}).get('uri', '')
        pos = params.get('position', {})
        line, char = pos.get('line', 0), pos.get('character', 0)
        parsed = self._parsed_doc(uri)
        if not parsed:
            return []
        doc_lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(doc_lines):
            return []
        token = self._token_at(doc_lines[line], char)
        if not token:
            return []
        name_u = token['text'].upper()

        out: List[Dict] = []
        seen: Set[Tuple[str, int, int, int]] = set()

        def add(path: Path, ln: int, col: int, end: int, uri_override: Optional[str] = None):
            loc_uri = uri_override or self._path_to_uri(path)
            key = (loc_uri, ln, col, end)
            if key in seen:
                return
            seen.add(key)
            out.append({'uri': loc_uri, 'range': {
                'start': {'line': ln, 'character': col},
                'end': {'line': ln, 'character': end}}})

        # In-file occurrences (defs + refs) always.
        doc_path = self._uri_to_path(uri)
        for d in parsed['defs']:
            if d['name'].upper() == name_u:
                add(doc_path, d['line'], d['column'],
                    d['column'] + len(d['name']), uri)
        for r in parsed['refs']:
            if r['name'].upper() == name_u:
                add(doc_path, r['line'], r['column'], r['end_column'], uri)

        # Cross-file only for globally-visible symbols, to avoid flooding
        # find-refs with same-named local labels from unrelated assemblies.
        idx = self._index_for_uri(uri)
        if idx and name_u in idx[0]['global_defs']:
            index = idx[0]
            for d in index['global_defs'].get(name_u, []):
                add(d['path'], d['line'], d['column'], d['column'] + len(d['name']))
            for r in index['refs'].get(name_u, []):
                add(r['path'], r['line'], r['column'], r['end_column'])
        return out

    def _handle_hover(self, params: Dict) -> Optional[Dict]:
        uri = params.get('textDocument', {}).get('uri', '')
        pos = params.get('position', {})
        line, char = pos.get('line', 0), pos.get('character', 0)
        parsed = self._parsed_doc(uri)
        if not parsed:
            return None
        doc_lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(doc_lines):
            return None
        token = self._token_at(doc_lines[line], char)
        if not token:
            return None
        name_u = token['text'].upper()

        chosen = None
        for d in parsed['defs']:
            if d['name'].upper() == name_u:
                chosen = d
                break
        scope = 'local'
        if not chosen:
            idx = self._index_for_uri(uri)
            if idx:
                defs = idx[0]['global_defs'].get(name_u, [])
                if defs:
                    chosen = defs[0]
                    scope = 'global'
        if not chosen:
            return None

        kind = chosen['kind'].upper()
        md = f"**{chosen['name']}** — {kind} ({scope})\n\n```ap101asm\n{chosen['detail']}\n```"
        return {'contents': {'kind': 'markdown', 'value': md}}

    # -- Shared resolution helpers ------------------------------------------ #

    def _resolve_member(self, uri: str, member: str) -> Optional[Path]:
        idx = self._index_for_uri(uri)
        if not idx:
            return None
        candidates = idx[0]['include_by_norm'].get(self._normalize_member_name(member), [])
        return candidates[0] if candidates else None

    def _loc(self, path: Optional[Path], line: int, col: int, end_col: int,
             uri: Optional[str] = None) -> Optional[Dict]:
        if path is None and uri is None:
            return None
        return {
            'uri': uri or self._path_to_uri(path),
            'range': {'start': {'line': line, 'character': col},
                      'end': {'line': line, 'character': end_col}},
        }


def main():
    AP101AsmLanguageServer().start()


if __name__ == '__main__':
    main()
