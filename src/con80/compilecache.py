#!/usr/bin/env python3
"""Content-addressed cache of HAL/S compilation results, shared across phases.

A key covers everything the compiler reads:

  * the source text as compiled (the mirror, so a build-layer CARDTYPE
    rewrite is part of the key)
  * the compile parms
  * every unit reachable through `D INCLUDE TEMPLATE`, by name and by source
    content -- not by the library members those units produce, whose bytes
    carry a compile timestamp and so would never match across phases
  * the include library (D INCLUDE macros), by content
  * the compiler binaries, via the halsc wrapper

One input is deliberately left out: whether a PROGRAM dependency's SDF is the
real one or the empty-body stub `hal()` seeds to break template cycles.  Which
one a phase has genuinely varies, but a program template is its declaration
line -- identical either way -- and the SDF difference is symbol-table content
that does not reach codegen.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

SCHEMA = "v2"          # bump whenever the keying changes

# Two compiles of the same source differ only in the date/time the compiler
# stamps into them: `\x02D<yyddd>` / `\x02T<hhmmsst>` in SYM records, the
# translator-identification date in END-card columns 47-51, and in a load
# module lnk101's timestamp. We normalize these out:
_D = rb"[\xf0-\xf9]"
_STAMP = re.compile(rb"\x02(?:\xc4" + _D * 5 + rb"|\xe3" + _D * 7 + rb")")
_LINK_STAMP = re.compile(
    _D * 4 + rb"\x60" + _D * 2 + rb"\x60" + _D * 2 + rb"\xe3"
    + _D * 2 + rb"\x7a" + _D * 2 + rb"\x7a" + _D * 2)
_END = b"\x02\xc5\xd5\xc4"          # 0x02 'END'


def stamp_normalize(data: bytes) -> bytes:
    data = _STAMP.sub(
        lambda m: b"\x02" + m.group()[1:2] + b"?" * (len(m.group()) - 2), data)
    data = _LINK_STAMP.sub(lambda m: b"?" * len(m.group()), data)
    out = bytearray(data)
    for off in range(0, len(out) - 79, 80):
        if out[off:off + 4] == _END:
            out[off + 47:off + 52] = b"?????"
    return bytes(out)


def _copy_members(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for m in src.iterdir():
        if m.is_file():
            shutil.copyfile(m, dest / m.name)


class CompileCache:
    """Cache rooted at `root`; entries are `<root>/<key>/`.

    `tree` is halorder's whole-source-tree index ({descored name: (path,
    full, kind)}) and `text_of` maps a source path to the bytes that will
    actually be compiled.
    """

    def __init__(self, root: Path, tree: dict, text_of, extra: bytes = b""):
        self.root = Path(root)
        self.tree = tree
        self.text_of = text_of
        self.extra = extra              # INCLIB + compiler fingerprints
        self._src: dict[Path, str] = {}
        self._clo: dict[str, frozenset] = {}
        self._depcache: dict[str, tuple] = {}
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # -- keying ----------------------------------------------------------
    def _source_hash(self, path: Path) -> str:
        h = self._src.get(path)
        if h is None:
            try:
                data = self.text_of(path)
            except OSError:
                data = b""
            h = hashlib.sha256(data).hexdigest()
            self._src[path] = h
        return h

    def _deps(self, name: str) -> tuple:
        d = self._depcache.get(name)
        if d is None:
            from ap101Utils import halorder
            ent = self.tree.get(name)
            d = () if ent is None else tuple(halorder.parse_hal(ent[0])[1])
            self._depcache[name] = d
        return d

    def _closure(self, name: str) -> frozenset:
        """Every unit reachable from `name` through D INCLUDE TEMPLATE."""
        got = self._clo.get(name)
        if got is not None:
            return got
        seen: set[str] = set()
        stack = [name]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            cached = self._clo.get(n)
            if cached is not None:      # a settled closure short-circuits
                seen |= cached
                continue
            stack.extend(self._deps(n))
        out = frozenset(seen)
        self._clo[name] = out
        return out

    def key(self, srcpath: Path, name: str | None, parm: str) -> str | None:
        """Cache key for compiling `srcpath` (unit `name`) with `parm`."""
        if not name:
            return None
        h = hashlib.sha256()
        h.update(SCHEMA.encode())
        h.update(b"\0" + parm.encode())
        h.update(b"\0" + self.extra)
        h.update(b"\0" + self._source_hash(srcpath).encode())
        for n in sorted(self._closure(name)):
            ent = self.tree.get(n)
            h.update(b"\0" + n.encode() + b"=")
            # not in the tree = external (a RUN/ZCON entry point, a macro):
            # nothing of ours to hash, but the NAME still participates
            h.update(b"?" if ent is None
                     else self._source_hash(ent[0]).encode())
        return h.hexdigest()

    # -- store / fetch ---------------------------------------------------
    def fetch(self, key: str, obj: Path, templib: Path, sdflib: Path) -> bool:
        """Place a cached result. True when the entry was complete."""
        ent = self.root / key
        if not (ent / ".ok").exists():
            return False
        try:
            obj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ent / "obj", obj)
            _copy_members(ent / "tmpl", templib)
            _copy_members(ent / "sdf", sdflib)
        except OSError:
            return False
        self.hits += 1
        return True

    def missed(self) -> None:
        """One compile that no key satisfied."""
        self.misses += 1

    def store(self, key: str, obj: Path, tmpl_out: Path, sdf_out: Path) -> None:
        """Record a completed compile.  `tmpl_out`/`sdf_out` are the compile's
        private output directories."""
        ent = self.root / key
        if (ent / ".ok").exists():
            return
        tmp = self.root / f".tmp.{key}.{os.getpid()}"
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            (tmp / "tmpl").mkdir(parents=True, exist_ok=True)
            (tmp / "sdf").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(obj, tmp / "obj")
            _copy_members(tmpl_out, tmp / "tmpl")
            _copy_members(sdf_out, tmp / "sdf")
            (tmp / ".ok").write_bytes(b"")
            # rename is the publish: a racing builder either wins or finds the
            # entry already there, and both produced the same bytes.
            if not ent.exists():
                tmp.rename(ent)
                self.stores += 1
        except OSError:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# Files at or below this go into the fingerprint by content; larger ones by
# name+size+mtime, which is far cheaper and good enough for a multi-MB binary
# that only changes when it is rebuilt.
_FINGERPRINT_BY_CONTENT = 1 << 20


def fingerprint(paths) -> bytes:
    """Identity of a set of files -- used for the compiler binaries, so
    rebuilding the compiler invalidates the cache."""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(p.name.encode() + b":")
        if st.st_size <= _FINGERPRINT_BY_CONTENT:
            try:
                h.update(hashlib.sha256(p.read_bytes()).digest())
                continue
            except OSError:
                pass
        h.update(f"{st.st_size}:{int(st.st_mtime)}\0".encode())
    return h.hexdigest().encode()


def dir_content_fingerprint(d: Path) -> bytes:
    """Identity of a directory by member name and content -- used for INCLIB,
    which con80build rewrites in place per phase, so mtimes are unusable."""
    h = hashlib.sha256()
    d = Path(d)
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file():
                h.update(p.name.encode() + b"\0")
                h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest().encode()
