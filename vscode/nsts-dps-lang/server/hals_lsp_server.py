#!/usr/bin/env python3
"""

This file is a vendored copy of:
    https://github.com/Zaneham/hals-lsp/blob/main/hals_lsp_server.py



HAL/S Language Server Protocol (LSP) Implementation

Provides IDE features for HAL/S (High-order Assembly Language/Shuttle),
the real-time aerospace programming language used for NASA Space Shuttle
flight software.

Based on the NASA HAL/S Language Specification (1974-1978)
"""

import json
import sys
import re
import hashlib
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Dict, List, Optional, Any, Tuple, Set

from hals_semantic_parser import HALSParser, SymbolKind, Symbol


class HALSLanguageServer:
    """LSP server for HAL/S"""

    # Precompiled regex patterns for performance
    _RE_IDENTIFIER = re.compile(r'\b([A-Z_][A-Z0-9_]*)\b', re.IGNORECASE)
    _RE_IDENTIFIER_HASH = re.compile(r'[A-Za-z_][A-Za-z0-9_#]*')
    _RE_INCLUDE = re.compile(r'\bINCLUDE\b', re.IGNORECASE)
    _RE_INCLUDE_TEMPLATE = re.compile(r'\bINCLUDE\s+(?:TEMPLATE\s+)?([A-Z][A-Z0-9_#]*)\b', re.IGNORECASE)
    _RE_INCLUDE_DFG = re.compile(r'\bINCLUDE\s*=\s*([A-Z][A-Z0-9_#]*)\b', re.IGNORECASE)
    _RE_DECLARE = re.compile(r'\bDECLARE\b', re.IGNORECASE)
    _RE_NON_ALNUM = re.compile(r'[^A-Z0-9]')
    _RE_OI_DIR = re.compile(r'OI\d{6,7}', re.IGNORECASE)

    def __init__(self):
        self.parser = HALSParser()
        self.documents: Dict[str, str] = {}
        self.running = True
        self.definition_enable_workspace_index = True
        self.definition_search_subdirs = ['SSSRC', 'APPLSRC', 'INCL80']
        self._workspace_index_cache: Dict[str, Dict[str, Any]] = {}
        self._include_closure_cache: Dict[str, Dict[str, Any]] = {}

    def start(self):
        """Start the language server"""
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
        """Read a JSON-RPC message from stdin.

        Read from the binary buffer: Content-Length is a *byte* count, so reading
        the text stream (which counts code points) would stop short on any
        multibyte UTF-8 in the body and desync every subsequent message.
        """
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
        """Send a JSON-RPC message to stdout (byte-accurate Content-Length)."""
        content = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
        sys.stdout.buffer.write(header + content)
        sys.stdout.buffer.flush()

    def _send_notification(self, method: str, params: Dict):
        """Send a notification"""
        self._send_message({
            'jsonrpc': '2.0',
            'method': method,
            'params': params
        })

    def _log(self, message: str):
        """Log a message to the client"""
        self._send_notification('window/logMessage', {
            'type': 3,  # Info
            'message': f"[HAL/S] {message}"
        })

    def _handle_message(self, message: Dict) -> Optional[Dict]:
        """Handle incoming JSON-RPC message"""
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
            'textDocument/didSave': self._handle_did_save,
            'textDocument/didClose': self._handle_did_close,
            'textDocument/completion': self._handle_completion,
            'textDocument/hover': self._handle_hover,
            'textDocument/definition': self._handle_definition,
            'textDocument/references': self._handle_references,
            'textDocument/documentSymbol': self._handle_document_symbol,
            'textDocument/semanticTokens/full': self._handle_semantic_tokens_full,
        }

        handler = handlers.get(method)
        if handler:
            result = handler(params)
            if msg_id is not None:
                return {
                    'jsonrpc': '2.0',
                    'id': msg_id,
                    'result': result
                }
        elif msg_id is not None:
            return {
                'jsonrpc': '2.0',
                'id': msg_id,
                'error': {
                    'code': -32601,
                    'message': f"Method not found: {method}"
                }
            }
        return None

    def _handle_initialize(self, params: Dict) -> Dict:
        """Handle initialize request"""
        init_options = params.get('initializationOptions', {}) or {}
        self.definition_enable_workspace_index = bool(
            init_options.get('definitionEnableWorkspaceIndex', True)
        )

        subdirs = init_options.get('definitionSearchSubdirs', ['SSSRC', 'APPLSRC', 'INCL80'])
        if isinstance(subdirs, list) and subdirs:
            cleaned = [str(s).strip() for s in subdirs if str(s).strip()]
            if cleaned:
                self.definition_search_subdirs = cleaned

        return {
            'capabilities': {
                'textDocumentSync': {
                    'openClose': True,
                    'change': 1,  # Full sync
                    'save': {'includeText': True}
                },
                'completionProvider': {
                    'triggerCharacters': ['.', ':', ' '],
                    'resolveProvider': False
                },
                'hoverProvider': True,
                'definitionProvider': True,
                'referencesProvider': True,
                'documentSymbolProvider': True,
                'semanticTokensProvider': {
                    'legend': {
                        'tokenTypes': ['macro', 'variable'],
                        'tokenModifiers': ['declaration']
                    },
                    'full': True
                },
            },
            'serverInfo': {
                'name': 'HAL/S Language Server',
                'version': '1.0.0'
            }
        }

    def _uri_to_path(self, uri: str) -> Optional[Path]:
        """Convert file:// URI to local Path."""
        try:
            parsed = urlparse(uri)
            if parsed.scheme != 'file':
                return None
            return Path(unquote(parsed.path))
        except Exception:
            return None

    def _path_to_uri(self, path: Path) -> str:
        """Convert local Path to file:// URI without resolving symlinks.

        Keeping the original workspace-visible path (for example /Users/don/OneDrive/...)
        makes VS Code peek/definition headers more readable than canonicalized
        CloudStorage targets.
        """
        return path.absolute().as_uri()

    def _find_oi_root(self, file_path: Path) -> Optional[Path]:
        """Find nearest OI* root directory for a file path."""
        for parent in [file_path.parent] + list(file_path.parents):
            if self._RE_OI_DIR.fullmatch(parent.name.upper()):
                return parent
        return None

    def _normalize_member_name(self, text: str) -> str:
        return self._RE_NON_ALNUM.sub('', text.upper())

    def _is_comment_card_line(self, line: str) -> bool:
        """Return True for HAL/S column-1 C comment cards (C, C , C*, C@)."""
        if not line:
            return False
        if line[0].upper() != 'C':
            return False
        if len(line) == 1:
            return True
        return line[1] in (' ', '*', '@')

    def _extract_include_members_from_text(self, text: str) -> List[str]:
        members: List[str] = []
        for raw_line in text.split('\n'):
            if not raw_line:
                continue
            if self._is_comment_card_line(raw_line):
                continue
            if raw_line.startswith('*'):
                continue

            line = raw_line[1:] if raw_line and raw_line[0].isalpha() else raw_line
            match = self._RE_INCLUDE_TEMPLATE.search(line)
            if match:
                members.append(match.group(1).upper())
                continue

            dfg_match = self._RE_INCLUDE_DFG.search(line)
            if dfg_match:
                members.append(dfg_match.group(1).upper())
        return members

    def _extract_heuristic_symbol_defs(self, text: str, file_path: Path) -> List[Dict[str, Any]]:
        """Extract likely variable definitions from common HAL/S declaration forms."""
        defs: List[Dict[str, Any]] = []
        lines = text.split('\n')

        type_word_set = {
            'INTEGER', 'SCALAR', 'VECTOR', 'MATRIX', 'BOOLEAN', 'CHARACTER',
            'BIT', 'EVENT', 'STRUCTURE', 'ARRAY', 'CONSTANT', 'INITIAL'
        }
        type_words = r'(?:INTEGER|SCALAR|VECTOR|MATRIX|BOOLEAN|CHARACTER|BIT|EVENT|STRUCTURE|ARRAY|CONSTANT|INITIAL)'
        decl_name_re = re.compile(rf'\b([A-Z_][A-Z0-9_]*)\b(?=\s+{type_words})', re.IGNORECASE)
        struct_field_re = re.compile(r'^\s*\d+\s+([A-Z_][A-Z0-9_]*)\b', re.IGNORECASE)
        typed_list_re = re.compile(rf'\bDECLARE\s+{type_words}\b([^;]*);', re.IGNORECASE)
        ident_re = re.compile(r'\b([A-Z_][A-Z0-9_]*)\b', re.IGNORECASE)

        in_declare_stmt = False
        for line_no, raw_line in enumerate(lines):
            if not raw_line:
                continue
            if self._is_comment_card_line(raw_line):
                continue

            # Stripping the column-0 card character shifts every match left by one;
            # add the offset back so stored columns match the editor document.
            col_offset = 1 if (raw_line and raw_line[0].isalpha()) else 0
            line = raw_line[col_offset:]

            field_match = struct_field_re.search(line)
            if field_match:
                field_name = field_match.group(1).upper()
                if field_name in self.parser.KEYWORDS or field_name in type_word_set:
                    continue
                defs.append({
                    'path': file_path,
                    'line': line_no,
                    'column': col_offset + field_match.start(1),
                    'name': field_name,
                    'kind': 'variable',
                })

            if re.search(r'\bDECLARE\b', line, re.IGNORECASE):
                in_declare_stmt = True

            # DECLARE <TYPE> name1, name2; form.
            typed_match = typed_list_re.search(line)
            if typed_match:
                trailing = typed_match.group(1)
                for ident in ident_re.finditer(trailing):
                    ident_name = ident.group(1).upper()
                    if ident_name in self.parser.KEYWORDS or ident_name in type_word_set:
                        continue
                    defs.append({
                        'path': file_path,
                        'line': line_no,
                        'column': col_offset + typed_match.start(1) + ident.start(1),
                        'name': ident_name,
                        'kind': 'variable',
                    })

            if in_declare_stmt:
                for match in decl_name_re.finditer(line):
                    ident_name = match.group(1).upper()
                    if ident_name in self.parser.KEYWORDS or ident_name in type_word_set:
                        continue
                    defs.append({
                        'path': file_path,
                        'line': line_no,
                        'column': col_offset + match.start(1),
                        'name': ident_name,
                        'kind': 'variable',
                    })

            if in_declare_stmt and ';' in line:
                in_declare_stmt = False

        return defs

    def _best_include_target(self, candidates: List[Path], oi_root: Path) -> Optional[Path]:
        if not candidates:
            return None

        priorities = {name.upper(): idx for idx, name in enumerate(self.definition_search_subdirs)}

        def rank(path: Path) -> Tuple[int, int, str]:
            parent_name = path.parent.name.upper()
            dir_rank = priorities.get(parent_name, len(priorities) + 10)
            rel_depth = len(path.relative_to(oi_root).parts) if oi_root in path.parents else 999
            return (dir_rank, rel_depth, str(path))

        return sorted(candidates, key=rank)[0]

    def _build_workspace_index(self, oi_root: Path) -> Dict[str, Any]:
        """Build per-OI index for include member lookup and cross-file symbols."""
        include_by_norm: Dict[str, List[Path]] = {}
        include_by_short: Dict[str, List[Path]] = {}
        include_members_by_path: Dict[str, List[str]] = {}
        symbol_defs: Dict[str, List[Dict[str, Any]]] = {}
        symbol_refs: Dict[str, List[Dict[str, Any]]] = {}
        
        # Use sets for O(1) duplicate detection instead of O(n) list scans
        seen_defs: Dict[str, Set[tuple]] = {}  # name -> set of (path, line, col)
        seen_refs: Dict[str, Set[tuple]] = {}  # name -> set of (path, line, col, end_col)

        parser = HALSParser()
        max_file_bytes = 2 * 1024 * 1024

        def add_symbol_def(entry: Dict[str, Any]) -> None:
            name_key = str(entry['name']).upper()
            dedup_key = (str(entry['path']), entry['line'], entry['column'])
            
            if name_key not in seen_defs:
                seen_defs[name_key] = set()
            if dedup_key in seen_defs[name_key]:
                return
            seen_defs[name_key].add(dedup_key)
            symbol_defs.setdefault(name_key, []).append(entry)

        def add_symbol_ref(entry: Dict[str, Any]) -> None:
            name_key = str(entry['name']).upper()
            dedup_key = (str(entry['path']), entry['line'], entry['column'], entry.get('end_column', 0))
            
            if name_key not in seen_refs:
                seen_refs[name_key] = set()
            if dedup_key in seen_refs[name_key]:
                return
            seen_refs[name_key].add(dedup_key)
            symbol_refs.setdefault(name_key, []).append(entry)

        for subdir in self.definition_search_subdirs:
            root = oi_root / subdir
            if not root.exists() or not root.is_dir():
                continue

            for file_path in root.rglob('*'):
                if not file_path.is_file():
                    continue
                if file_path.name.startswith('.'):
                    continue

                try:
                    size = file_path.stat().st_size
                    if size > max_file_bytes:
                        continue
                except Exception:
                    continue

                stem_norm = self._normalize_member_name(file_path.name)
                if stem_norm:
                    include_by_norm.setdefault(stem_norm, []).append(file_path)
                    include_by_short.setdefault(stem_norm[:6], []).append(file_path)

                try:
                    text = file_path.read_text(encoding='utf-8', errors='replace')
                except Exception:
                    continue

                include_members_by_path[str(file_path.resolve())] = self._extract_include_members_from_text(text)

                parser.parse(text)
                for symbol in parser.get_symbols().values():
                    add_symbol_def({
                        'path': file_path,
                        'line': symbol.line,
                        'column': symbol.column,
                        'name': symbol.name,
                        'kind': symbol.kind.value,
                        'data_type': symbol.data_type,
                        'documentation': symbol.documentation,
                        'parameters': list(symbol.parameters),
                        'replacement_text': symbol.replacement_text,
                    })

                for ref in parser.get_references():
                    add_symbol_ref({
                        'path': file_path,
                        'line': ref.line,
                        'column': ref.column,
                        'end_column': ref.end_column,
                        'name': ref.name,
                    })

                for extra_def in self._extract_heuristic_symbol_defs(text, file_path):
                    add_symbol_def(extra_def)

        return {
            'include_by_norm': include_by_norm,
            'include_by_short': include_by_short,
            'include_members_by_path': include_members_by_path,
            'symbol_defs': symbol_defs,
            'symbol_refs': symbol_refs,
        }

    def _workspace_index_cache_file(self, oi_root: Path) -> Path:
        """Return disk cache file path for an OI index."""
        cache_root = Path.home() / '.cache' / 'hals-lsp'
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_format_version = 'v5-card-heuristics'
        key_text = f"{cache_format_version}|{str(oi_root.resolve())}|{'|'.join(self.definition_search_subdirs)}"
        key = hashlib.sha1(key_text.encode('utf-8')).hexdigest()[:16]
        return cache_root / f'index-{key}.json'

    def _serialize_workspace_index(self, index: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            'include_by_norm': {},
            'include_by_short': {},
            'include_members_by_path': index.get('include_members_by_path', {}),
            'symbol_defs': {},
            'symbol_refs': {},
        }

        for key, paths in index.get('include_by_norm', {}).items():
            out['include_by_norm'][key] = [str(p) for p in paths]
        for key, paths in index.get('include_by_short', {}).items():
            out['include_by_short'][key] = [str(p) for p in paths]
        for key, defs in index.get('symbol_defs', {}).items():
            out['symbol_defs'][key] = [{
                'path': str(d['path']),
                'line': d['line'],
                'column': d['column'],
                'name': d['name'],
                'kind': d.get('kind', ''),
                'data_type': d.get('data_type', ''),
                'documentation': d.get('documentation', ''),
                'parameters': d.get('parameters', []),
                'replacement_text': d.get('replacement_text', ''),
            } for d in defs]

        for key, refs in index.get('symbol_refs', {}).items():
            out['symbol_refs'][key] = [{
                'path': str(r['path']),
                'line': r['line'],
                'column': r['column'],
                'end_column': r.get('end_column', r['column'] + len(key)),
                'name': r.get('name', key),
            } for r in refs]

        return out

    def _deserialize_workspace_index(self, data: Dict[str, Any]) -> Dict[str, Any]:
        include_by_norm: Dict[str, List[Path]] = {}
        include_by_short: Dict[str, List[Path]] = {}
        include_members_by_path: Dict[str, List[str]] = {}
        symbol_defs: Dict[str, List[Dict[str, Any]]] = {}
        symbol_refs: Dict[str, List[Dict[str, Any]]] = {}

        for key, paths in (data.get('include_by_norm', {}) or {}).items():
            include_by_norm[key] = [Path(p) for p in paths]
        for key, paths in (data.get('include_by_short', {}) or {}).items():
            include_by_short[key] = [Path(p) for p in paths]
        include_members_by_path = data.get('include_members_by_path', {}) or {}

        for key, defs in (data.get('symbol_defs', {}) or {}).items():
            symbol_defs[key] = [{
                'path': Path(d['path']),
                'line': d['line'],
                'column': d['column'],
                'name': d['name'],
                'kind': d.get('kind', ''),
                'data_type': d.get('data_type', ''),
                'documentation': d.get('documentation', ''),
                'parameters': d.get('parameters', []),
                'replacement_text': d.get('replacement_text', ''),
            } for d in defs]

        for key, refs in (data.get('symbol_refs', {}) or {}).items():
            symbol_refs[key] = [{
                'path': Path(r['path']),
                'line': r['line'],
                'column': r['column'],
                'end_column': r.get('end_column', r['column'] + len(key)),
                'name': r.get('name', key),
            } for r in refs]

        return {
            'include_by_norm': include_by_norm,
            'include_by_short': include_by_short,
            'include_members_by_path': include_members_by_path,
            'symbol_defs': symbol_defs,
            'symbol_refs': symbol_refs,
        }

    def _load_workspace_index_from_disk(self, oi_root: Path) -> Optional[Dict[str, Any]]:
        cache_file = self._workspace_index_cache_file(oi_root)
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            return self._deserialize_workspace_index(data)
        except Exception:
            return None

    def _save_workspace_index_to_disk(self, oi_root: Path, index: Dict[str, Any]) -> None:
        cache_file = self._workspace_index_cache_file(oi_root)
        payload = self._serialize_workspace_index(index)
        try:
            cache_file.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
        except Exception:
            return

    def _get_workspace_index(self, oi_root: Path) -> Dict[str, Any]:
        key = str(oi_root.resolve())
        if key not in self._workspace_index_cache:
            loaded = self._load_workspace_index_from_disk(oi_root)
            if loaded is not None:
                self._workspace_index_cache[key] = loaded
            else:
                built = self._build_workspace_index(oi_root)
                self._workspace_index_cache[key] = built
                self._save_workspace_index_to_disk(oi_root, built)
        return self._workspace_index_cache[key]

    def _drop_oi_disk_cache(self, oi_root: Path) -> None:
        """Delete the on-disk OI index snapshot (the in-memory copy is kept)."""
        cache_file = self._workspace_index_cache_file(oi_root)
        try:
            if cache_file.exists():
                cache_file.unlink()
        except OSError:
            pass

    def _drop_include_closures_for_oi(self, oi_key: str) -> None:
        """Forget cached include closures belonging to one OI root."""
        stale = [u for u, c in self._include_closure_cache.items() if c.get('oi_key') == oi_key]
        for u in stale:
            self._include_closure_cache.pop(u, None)

    def _reindex_file_in_index(self, index: Dict[str, Any], file_path: Path, text: str) -> None:
        """Incrementally refresh one file's entries in an already-built OI index.

        Replaces just this file's symbol defs/refs and include-member list from the
        given (in-memory) text, leaving every other file untouched. This is what
        keeps a keystroke from triggering a full rglob+reparse of the whole OI tree.
        File-existence maps (include_by_norm/short) are unaffected by edits.
        """
        path_str = str(file_path)

        # Drop this file's existing defs/refs.
        for table in ('symbol_defs', 'symbol_refs'):
            store = index.get(table, {})
            for name_key in list(store.keys()):
                kept = [e for e in store[name_key] if str(e.get('path')) != path_str]
                if kept:
                    store[name_key] = kept
                else:
                    del store[name_key]

        defs = index.setdefault('symbol_defs', {})
        refs = index.setdefault('symbol_refs', {})
        seen_defs: Set[tuple] = set()
        seen_refs: Set[tuple] = set()

        def add_def(entry: Dict[str, Any]) -> None:
            dedup = (str(entry['path']), entry['line'], entry['column'])
            if dedup in seen_defs:
                return
            seen_defs.add(dedup)
            defs.setdefault(str(entry['name']).upper(), []).append(entry)

        parser = HALSParser()
        parser.parse(text)
        for symbol in parser.get_symbols().values():
            add_def({
                'path': file_path,
                'line': symbol.line,
                'column': symbol.column,
                'name': symbol.name,
                'kind': symbol.kind.value,
                'data_type': symbol.data_type,
                'documentation': symbol.documentation,
                'parameters': list(symbol.parameters),
                'replacement_text': symbol.replacement_text,
            })
        for extra in self._extract_heuristic_symbol_defs(text, file_path):
            add_def(extra)
        for ref in parser.get_references():
            dedup = (path_str, ref.line, ref.column, ref.end_column)
            if dedup in seen_refs:
                continue
            seen_refs.add(dedup)
            refs.setdefault(ref.name.upper(), []).append({
                'path': file_path,
                'line': ref.line,
                'column': ref.column,
                'end_column': ref.end_column,
                'name': ref.name,
            })

        index.setdefault('include_members_by_path', {})[str(file_path.resolve())] = \
            self._extract_include_members_from_text(text)

    def _invalidate_document_caches(self, uri: str) -> None:
        self._include_closure_cache.pop(uri, None)

    def _path_affects_workspace_index(self, path: Path, oi_root: Path) -> bool:
        """True when path is inside an indexed OI search subdir."""
        try:
            rel_parts = [p.upper() for p in path.resolve().relative_to(oi_root.resolve()).parts]
        except Exception:
            return False
        search = {d.upper() for d in self.definition_search_subdirs}
        return any(part in search for part in rel_parts)

    def _resolve_include_member_path(self, index: Dict[str, Any], oi_root: Path, member: str) -> Optional[Path]:
        member_norm = self._normalize_member_name(member)
        if not member_norm:
            return None

        candidates: List[Path] = []
        candidates.extend(index['include_by_norm'].get(member_norm, []))
        candidates.extend(index['include_by_short'].get(member_norm[:6], []))

        seen = set()
        uniq_candidates = []
        for p in candidates:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            uniq_candidates.append(p)

        return self._best_include_target(uniq_candidates, oi_root)

    def _collect_include_closure(self, uri: str, oi_root: Path, source_text: str) -> Set[Path]:
        """Collect transitive INCLUDE member files starting from current source text."""
        oi_key = str(oi_root.resolve())
        text_hash = hash(source_text)
        cached = self._include_closure_cache.get(uri)
        if cached and cached.get('oi_key') == oi_key and cached.get('text_hash') == text_hash:
            return {Path(p) for p in cached.get('paths', [])}

        index = self._get_workspace_index(oi_root)
        pending: List[str] = self._extract_include_members_from_text(source_text)
        visited_members: Set[str] = set()
        visited_paths: Set[str] = set()
        include_paths: Set[Path] = set()
        max_files = 400

        while pending and len(include_paths) < max_files:
            member = pending.pop(0).upper()
            if member in visited_members:
                continue
            visited_members.add(member)

            member_path = self._resolve_include_member_path(index, oi_root, member)
            if not member_path:
                continue

            member_key = str(member_path.resolve())
            if member_key in visited_paths:
                continue

            visited_paths.add(member_key)
            include_paths.add(member_path)

            pending.extend(index.get('include_members_by_path', {}).get(member_key, []))

        self._include_closure_cache[uri] = {
            'oi_key': oi_key,
            'text_hash': text_hash,
            'paths': [str(p.resolve()) for p in include_paths],
        }

        return include_paths

    def _mask_semantic_scan_text(self, text: str) -> str:
        """Mask comments/strings while preserving line/column layout."""
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

    def _get_workspace_semantic_tokens(self, uri: str) -> List[Tuple[int, int, int, int, int]]:
        """Semantic tokens for symbols originating from included workspace members."""
        if uri not in self.documents:
            return []
        if not self.definition_enable_workspace_index:
            return []

        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return []
        oi_root = self._find_oi_root(doc_path)
        if not oi_root:
            return []

        source_text = self.documents[uri]
        include_closure = self._collect_include_closure(uri, oi_root, source_text)
        if not include_closure:
            return []

        include_path_keys = {str(p.resolve()) for p in include_closure}
        index = self._get_workspace_index(oi_root)

        external_macros: Set[str] = set()
        external_vars: Set[str] = set()

        local_symbols = set(self.parser.get_symbols().keys())

        for name, defs in index['symbol_defs'].items():
            if name in self.parser.KEYWORDS:
                continue
            matching_defs = [d for d in defs if str(d['path'].resolve()) in include_path_keys]
            if not matching_defs:
                continue
            if name in local_symbols:
                continue

            kinds = {str(d.get('kind', '')).lower() for d in matching_defs}
            if 'replace' in kinds:
                external_macros.add(name)
            if kinds.intersection({'variable', 'constant', 'structure', 'parameter'}):
                external_vars.add(name)

        if not external_macros and not external_vars:
            return []

        macro_type_index = 0
        variable_type_index = 1
        tokens: List[Tuple[int, int, int, int, int]] = []

        masked_text = self._mask_semantic_scan_text(source_text)
        masked_lines = masked_text.split('\n')
        raw_lines = source_text.split('\n')

        for line_no, line_text in enumerate(masked_lines):
            if not line_text:
                continue

            raw_line = raw_lines[line_no] if line_no < len(raw_lines) else ''
            if raw_line and self._is_comment_card_line(raw_line):
                continue

            if self._RE_INCLUDE.search(line_text):
                continue

            for match in self._RE_IDENTIFIER.finditer(line_text):
                name = match.group(1).upper()
                start = match.start(1)

                if name in self.parser.KEYWORDS:
                    continue

                if start > 0 and line_text[start - 1] == '%':
                    continue

                if name in external_macros:
                    tokens.append((line_no, start, len(match.group(1)), macro_type_index, 0))
                    continue

                if name in external_vars:
                    tokens.append((line_no, start, len(match.group(1)), variable_type_index, 0))

        return tokens

    def _token_at_position(self, line_text: str, char: int) -> Optional[Dict[str, Any]]:
        """Get token under cursor for HAL/S-like identifiers."""
        if char < 0:
            return None

        for match in self._RE_IDENTIFIER_HASH.finditer(line_text):
            if match.start() <= char <= match.end():
                return {
                    'text': match.group(0),
                    'start': match.start(),
                    'end': match.end(),
                }
        return None

    def _resolve_include_definition(self, uri: str, line: int, char: int) -> Optional[Dict[str, Any]]:
        """Resolve INCLUDE member token to source file location."""
        if uri not in self.documents:
            return None

        lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(lines):
            return None

        line_text = lines[line]
        if not re.search(r'\bINCLUDE\b', line_text, re.IGNORECASE):
            return None

        token = self._token_at_position(line_text, char)
        if not token:
            return None

        token_text = token['text'].upper()
        if token_text in {'INCLUDE', 'TEMPLATE', 'D', 'F'}:
            return None

        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return None

        oi_root = self._find_oi_root(doc_path)
        if not oi_root or not self.definition_enable_workspace_index:
            return None

        index = self._get_workspace_index(oi_root)
        target = self._resolve_include_member_path(index, oi_root, token_text)
        if not target:
            return None

        return {
            'uri': self._path_to_uri(target),
            'range': {
                'start': {'line': 0, 'character': 0},
                'end': {'line': 0, 'character': 1}
            }
        }

    def _resolve_workspace_symbol_definition(self, uri: str, line: int, char: int) -> Optional[Dict[str, Any]]:
        """Resolve symbol definitions using the OI workspace index."""
        if uri not in self.documents:
            return None

        if not self.definition_enable_workspace_index:
            return None

        lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(lines):
            return None

        token = self._token_at_position(lines[line], char)
        if not token:
            return None

        symbol = token['text'].upper()
        if symbol in self.parser.KEYWORDS:
            return None

        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return None

        oi_root = self._find_oi_root(doc_path)
        if not oi_root:
            return None

        index = self._get_workspace_index(oi_root)
        defs = index['symbol_defs'].get(symbol, [])
        if not defs:
            return None

        include_closure = self._collect_include_closure(uri, oi_root, self.documents[uri])
        include_path_keys = {str(p.resolve()) for p in include_closure}

        # Choose by precedence: a definition reachable through the include closure
        # wins; otherwise any definition in another file; otherwise the first.
        # (The include-closure pass runs last so it is not overwritten.)
        chosen = defs[0]
        for d in defs:
            if str(d['path'].resolve()) != str(doc_path.resolve()):
                chosen = d
                break
        for d in defs:
            if str(d['path'].resolve()) in include_path_keys:
                chosen = d
                break

        return {
            'uri': self._path_to_uri(chosen['path']),
            'range': {
                'start': {'line': chosen['line'], 'character': chosen['column']},
                'end': {
                    'line': chosen['line'],
                    'character': chosen['column'] + len(chosen['name'])
                }
            }
        }

    def _resolve_workspace_references(
        self,
        uri: str,
        line: int,
        char: int,
        include_declarations: bool,
    ) -> List[Dict[str, Any]]:
        """Resolve references for symbol at position across indexed OI workspace files."""
        if uri not in self.documents or not self.definition_enable_workspace_index:
            return []

        lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(lines):
            return []

        token = self._token_at_position(lines[line], char)
        if not token:
            return []

        symbol = token['text'].upper()
        if symbol in self.parser.KEYWORDS:
            return []

        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return []
        oi_root = self._find_oi_root(doc_path)
        if not oi_root:
            return []

        index = self._get_workspace_index(oi_root)
        refs = index.get('symbol_refs', {}).get(symbol, [])
        defs = index.get('symbol_defs', {}).get(symbol, []) if include_declarations else []

        out: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int, int, int]] = set()

        def add_loc(path: Path, start_line: int, start_col: int, end_col: int) -> None:
            loc_uri = self._path_to_uri(path)
            key = (loc_uri, start_line, start_col, end_col)
            if key in seen:
                return
            seen.add(key)
            out.append({
                'uri': loc_uri,
                'range': {
                    'start': {'line': start_line, 'character': start_col},
                    'end': {'line': start_line, 'character': end_col},
                },
            })

        for r in refs:
            r_path = r.get('path')
            if not isinstance(r_path, Path):
                r_path = Path(str(r_path))
            start_col = int(r.get('column', 0))
            end_col = int(r.get('end_column', start_col + len(symbol)))
            add_loc(r_path, int(r.get('line', 0)), start_col, end_col)

        for d in defs:
            d_path = d.get('path')
            if not isinstance(d_path, Path):
                d_path = Path(str(d_path))
            start_col = int(d.get('column', 0))
            end_col = start_col + len(str(d.get('name', symbol)))
            add_loc(d_path, int(d.get('line', 0)), start_col, end_col)

        return out

    def _resolve_workspace_hover(self, uri: str, line: int, char: int) -> Optional[Dict[str, Any]]:
        """Resolve hover content using indexed symbols (including include-sourced ones)."""
        if uri not in self.documents or not self.definition_enable_workspace_index:
            return None

        lines = self.documents[uri].split('\n')
        if line < 0 or line >= len(lines):
            return None

        token = self._token_at_position(lines[line], char)
        if not token:
            return None

        symbol = token['text'].upper()
        if symbol in self.parser.KEYWORDS:
            return None

        doc_path = self._uri_to_path(uri)
        if not doc_path:
            return None
        oi_root = self._find_oi_root(doc_path)
        if not oi_root:
            return None

        index = self._get_workspace_index(oi_root)
        defs = index['symbol_defs'].get(symbol, [])
        if not defs:
            return None

        include_closure = self._collect_include_closure(uri, oi_root, self.documents[uri])
        include_path_keys = {str(p.resolve()) for p in include_closure}

        chosen = defs[0]
        for d in defs:
            if str(d['path'].resolve()) in include_path_keys:
                chosen = d
                break

        kind = str(chosen.get('kind', '')).lower()
        data_type = chosen.get('data_type') or (kind.upper() if kind else 'SYMBOL')
        name = chosen.get('name', symbol)

        if kind == 'replace':
            params = chosen.get('parameters', []) or []
            replacement = chosen.get('replacement_text', '') or ''
            args = self.parser._extract_call_arguments_from_lines(lines, line, token['end'])

            temp_sym = Symbol(
                name=name,
                kind=SymbolKind.REPLACE,
                data_type='REPLACE',
                line=chosen.get('line', 0),
                column=chosen.get('column', 0),
                documentation=chosen.get('documentation', ''),
                parameters=params,
                replacement_text=replacement,
            )

            # Temporarily seed parser symbol table with include-visible REPLACE
            # symbols so nested expansion matches compiler-like recursive scans.
            injected: Dict[str, Symbol] = {}
            for def_name, def_list in index.get('symbol_defs', {}).items():
                if def_name in self.parser.symbols:
                    continue
                for d in def_list:
                    d_path = d.get('path')
                    if not isinstance(d_path, Path):
                        d_path = Path(str(d_path))
                    if str(d_path.resolve()) not in include_path_keys:
                        continue
                    if str(d.get('kind', '')).lower() != 'replace':
                        continue
                    injected[def_name] = Symbol(
                        name=d.get('name', def_name),
                        kind=SymbolKind.REPLACE,
                        data_type='REPLACE',
                        line=d.get('line', 0),
                        column=d.get('column', 0),
                        documentation=d.get('documentation', ''),
                        parameters=d.get('parameters', []) or [],
                        replacement_text=d.get('replacement_text', '') or '',
                    )
                    break

            old_symbols = self.parser.symbols
            try:
                self.parser.symbols = dict(old_symbols)
                self.parser.symbols.update(injected)
                expansion, trace = self.parser.evaluate_replace_macro_with_trace(temp_sym, args)
            finally:
                self.parser.symbols = old_symbols

            hover_lines = [f"**{name}**: REPLACE"]
            if params:
                hover_lines.append(f"\nParameters: `{', '.join(params)}`")
            if expansion:
                display_expansion = self.parser.format_expansion_for_hover(expansion)
                hover_lines.append("\nExpands to:")
                hover_lines.append(f"```hals\n{display_expansion}\n```")
                trace_md = self.parser.format_trace_for_hover(trace)
                if trace_md:
                    hover_lines.append(f"\n{trace_md}")
            elif chosen.get('documentation'):
                hover_lines.append(f"\n{chosen.get('documentation')}")

            return {'contents': '\n'.join(hover_lines)}

        doc = chosen.get('documentation', '')
        if doc:
            return {'contents': f"**{name}**: {data_type}\n\n{doc}"}

        return {'contents': f"**{name}**: {data_type}"}

    def _handle_initialized(self, params: Dict) -> None:
        """Handle initialized notification"""
        self._log("HAL/S Language Server ready - NASA Space Shuttle flight software language")
        return None

    def _handle_shutdown(self, params: Dict) -> None:
        """Handle shutdown request"""
        return None

    def _handle_exit(self, params: Dict) -> None:
        """Handle exit notification"""
        self.running = False
        return None

    def _handle_did_open(self, params: Dict) -> None:
        """Handle textDocument/didOpen"""
        doc = params.get('textDocument', {})
        uri = doc.get('uri', '')
        text = doc.get('text', '')
        self.documents[uri] = text
        self._invalidate_document_caches(uri)
        self._parse_and_publish(uri, text)
        return None

    def _handle_did_change(self, params: Dict) -> None:
        """Handle textDocument/didChange"""
        doc = params.get('textDocument', {})
        uri = doc.get('uri', '')
        changes = params.get('contentChanges', [])
        if changes:
            text = changes[0].get('text', '')
            self.documents[uri] = text
            doc_path = self._uri_to_path(uri)
            if doc_path:
                oi_root = self._find_oi_root(doc_path)
                if oi_root and self._path_affects_workspace_index(doc_path, oi_root):
                    key = str(oi_root.resolve())
                    index = self._workspace_index_cache.get(key)
                    if index is not None:
                        # Incrementally refresh just this file rather than dropping
                        # and rebuilding the entire OI index on every keystroke.
                        self._reindex_file_in_index(index, doc_path, text)
                        self._drop_include_closures_for_oi(key)
                    # If the index isn't loaded yet, leave it: it will build on the
                    # next request that needs it.
            self._invalidate_document_caches(uri)
            self._parse_and_publish(uri, text)
        return None

    def _handle_did_save(self, params: Dict) -> None:
        """Handle textDocument/didSave.

        The in-memory index is already current from didChange; drop the on-disk
        snapshot so a later cold start rebuilds from the saved files instead of
        loading a pre-edit copy.
        """
        doc = params.get('textDocument', {})
        uri = doc.get('uri', '')
        text = params.get('text')
        if text is not None:
            self.documents[uri] = text
        doc_path = self._uri_to_path(uri)
        if doc_path:
            oi_root = self._find_oi_root(doc_path)
            if oi_root and self._path_affects_workspace_index(doc_path, oi_root):
                self._drop_oi_disk_cache(oi_root)
        return None

    def _handle_did_close(self, params: Dict) -> None:
        """Handle textDocument/didClose"""
        doc = params.get('textDocument', {})
        uri = doc.get('uri', '')
        if uri in self.documents:
            del self.documents[uri]
        self._invalidate_document_caches(uri)
        return None

    def _parse_and_publish(self, uri: str, text: str):
        """Parse document and publish diagnostics"""
        self.parser.parse(text)
        diagnostics = []
        for diag in self.parser.get_diagnostics():
            diagnostics.append({
                'range': {
                    'start': {'line': diag.line, 'character': diag.column},
                    'end': {'line': diag.line, 'character': diag.end_column}
                },
                'message': diag.message,
                'severity': 1 if diag.severity == 'error' else 2
            })
        self._send_notification('textDocument/publishDiagnostics', {
            'uri': uri,
            'diagnostics': diagnostics
        })

    def _handle_completion(self, params: Dict) -> List[Dict]:
        """Handle textDocument/completion"""
        uri = params.get('textDocument', {}).get('uri', '')
        position = params.get('position', {})
        line = position.get('line', 0)
        char = position.get('character', 0)

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        completions = self.parser.get_completions(line, char)
        result = []

        kind_map = {
            'keyword': 14,      # Keyword
            'program': 2,       # Module
            'procedure': 3,     # Function
            'function': 3,      # Function
            'task': 2,          # Module
            'compool': 2,       # Module
            'variable': 6,      # Variable
            'constant': 21,     # Constant
            'structure': 22,    # Struct
            'label': 20,        # Reference
            'replace': 15,      # Snippet
            'parameter': 6,     # Variable
        }

        for c in completions:
            result.append({
                'label': c['label'],
                'kind': kind_map.get(c['kind'], 6),
                'detail': c['detail'],
                'documentation': c.get('documentation', '')
            })

        return result

    def _handle_hover(self, params: Dict) -> Optional[Dict]:
        """Handle textDocument/hover"""
        uri = params.get('textDocument', {}).get('uri', '')
        position = params.get('position', {})
        line = position.get('line', 0)
        char = position.get('character', 0)

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        hover = self.parser.get_hover(line, char)
        if not hover:
            hover = self._resolve_workspace_hover(uri, line, char)

        if hover:
            return {
                'contents': {
                    'kind': 'markdown',
                    'value': hover['contents']
                }
            }
        return None

    def _handle_definition(self, params: Dict) -> Optional[Dict]:
        """Handle textDocument/definition"""
        uri = params.get('textDocument', {}).get('uri', '')
        position = params.get('position', {})
        line = position.get('line', 0)
        char = position.get('character', 0)

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        include_definition = self._resolve_include_definition(uri, line, char)
        if include_definition:
            return include_definition

        definition = self.parser.get_definition(line, char)
        if definition:
            return {
                'uri': uri,
                'range': {
                    'start': {'line': definition['line'], 'character': definition['column']},
                    'end': {'line': definition['line'], 'character': definition['column'] + len(definition['name'])}
                }
            }

        workspace_definition = self._resolve_workspace_symbol_definition(uri, line, char)
        if workspace_definition:
            return workspace_definition

        return None

    def _handle_references(self, params: Dict) -> List[Dict]:
        """Handle textDocument/references"""
        uri = params.get('textDocument', {}).get('uri', '')
        position = params.get('position', {})
        context = params.get('context', {})
        line = position.get('line', 0)
        char = position.get('character', 0)
        include_declarations = bool(context.get('includeDeclaration', False))

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        refs = self.parser.get_references_at(line, char)
        result = []
        seen: Set[Tuple[str, int, int, int]] = set()

        def add_result(loc_uri: str, start_line: int, start_col: int, end_col: int) -> None:
            key = (loc_uri, start_line, start_col, end_col)
            if key in seen:
                return
            seen.add(key)
            result.append({
                'uri': loc_uri,
                'range': {
                    'start': {'line': start_line, 'character': start_col},
                    'end': {'line': start_line, 'character': end_col}
                }
            })

        for ref in refs:
            add_result(uri, ref['line'], ref['column'], ref['end_column'])

        workspace_refs = self._resolve_workspace_references(uri, line, char, include_declarations)
        for wr in workspace_refs:
            start = wr['range']['start']
            end = wr['range']['end']
            add_result(wr['uri'], start['line'], start['character'], end['character'])

        return result

    def _handle_document_symbol(self, params: Dict) -> List[Dict]:
        """Handle textDocument/documentSymbol"""
        uri = params.get('textDocument', {}).get('uri', '')

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        symbols = self.parser.get_document_symbols()
        result = []

        kind_map = {
            'program': 2,       # Module
            'procedure': 12,    # Function
            'function': 12,     # Function
            'task': 2,          # Module
            'compool': 2,       # Module
            'variable': 13,     # Variable
            'constant': 14,     # Constant
            'structure': 23,    # Struct
            'label': 20,        # Key
            'replace': 15,      # Macro
            'parameter': 13,    # Variable
        }

        for sym in symbols:
            result.append({
                'name': sym['name'],
                'kind': kind_map.get(sym['kind'], 13),
                'range': {
                    'start': {'line': sym['line'], 'character': sym['column']},
                    'end': {'line': sym['line'], 'character': sym['column'] + len(sym['name'])}
                },
                'selectionRange': {
                    'start': {'line': sym['line'], 'character': sym['column']},
                    'end': {'line': sym['line'], 'character': sym['column'] + len(sym['name'])}
                },
                'detail': sym.get('detail', '')
            })

        return result

    def _handle_semantic_tokens_full(self, params: Dict) -> Dict:
        """Handle textDocument/semanticTokens/full."""
        uri = params.get('textDocument', {}).get('uri', '')

        if uri in self.documents:
            self.parser.parse(self.documents[uri])

        tokens = self.parser.get_semantic_tokens()
        tokens.extend(self._get_workspace_semantic_tokens(uri))
        tokens = sorted(set(tokens), key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
        data: List[int] = []

        prev_line = 0
        prev_start = 0

        for line, start, length, token_type, token_modifiers in tokens:
            delta_line = line - prev_line
            if delta_line == 0:
                delta_start = start - prev_start
            else:
                delta_start = start

            data.extend([
                delta_line,
                delta_start,
                length,
                token_type,
                token_modifiers,
            ])

            prev_line = line
            prev_start = start

        return {'data': data}


def main():
    """Start the HAL/S language server"""
    server = HALSLanguageServer()
    server.start()


if __name__ == '__main__':
    main()
