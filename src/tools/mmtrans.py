#!/usr/bin/env python3
"""mmtrans -- the MMU transaction tables: decode what is in the build, and
say which halfword the load-build cards actually determine.

Csects `MMLDRTBL` and `MMUPUR` carry the tables the GPC's mass-memory
loader walks: which tape address a load starts at, how many blocks it is,
and where in main store it goes.  The delivered source says of them:

    THE TABLE CONTENT IS DEFINED HERE FOR DOCUMENTATION AND USE BY THE
    USER.  THE ACTUAL DATA CONTENT IS INSERTED HERE BY THE MMU BUILD
    SYSTEM DURING A TAPE BUILD.

What it assembles to is one past tape build's output.  This module
recovers the encoding and checks it against the cards that determine it,
per table and per field.

Formats (from the prolog, confirmed field by field against the SYS5
ALLOC/SYSTEM cards -- see `verify`):

  transaction table    [count]  then per transaction
                       header 3 HW  + entries 2 HW each + 0000 terminator
     header  +0  MM address, 0 0 F F F T T T S S S B B B B B
             +1  total blocks-1 << 3 | 3 DSR bits
             +2  protect-data-table GPC address (8008 = none)
     entry   +0  blocks-1 (5b) | last-block words-1 (9b) | 2 addr MSBs
             +1  GPC start address
  message table        one SVC-number halfword per transaction, then 0000
                       bit0 1 = do not load; bits 1-3 reserved

A table whose first halfword is a transaction COUNT (`FMADEU11`,
`BSL1LDR*`) is distinguished from one that is a bare run of transactions
(`VMATFLD*`) by the count halfword being a small integer that predicts
the table's own length; `decode` checks both readings and takes the one
that consumes the table exactly.

Usage:
  python -m tools.mmtrans --tables            # decode and print
  python -m tools.mmtrans --verify            # join to the cards
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ap101Utils.mmbstamp import mm16
from tools.mmubuild import MMUDeck, build as build_deck

HW_PER_BLOCK = 512
NO_PROTECT = 0x8008
FINDING = "!!"        # prefixes a note the card join could not account for

# The tables, in the order the two csects lay them out.  `kind` names what
# the halfwords mean.
TRANSACTION = "transaction"
MESSAGE = "message"
POINTER = "pointer"
PURGE = "purge"

TABLES = [
    ("MAPTABLE", POINTER),
    ("MSGTABLE", POINTER),
    ("FMADEU11", TRANSACTION),
    ("BSL1LDR1", TRANSACTION),
    ("BSL1LDR2", TRANSACTION),
    ("BSL1MTB1", MESSAGE),
    ("BSL1MTB2", MESSAGE),
    ("FMADEUMT", MESSAGE),
    ("VMATFLD1", TRANSACTION),
    ("VMATFLM1", MESSAGE),
    ("GMAIMUC1", MESSAGE),
    ("VMATFLD2", TRANSACTION),
    ("VMATFLM2", MESSAGE),
    ("VMATFLD3", TRANSACTION),
    ("VMATFLM3", MESSAGE),
    ("MMPURTBL", PURGE),
    ("MMUPURMT", MESSAGE),
]


# ------------------------------------------------------------------ tape

def fmtCard(addr: int) -> str:
    """mm16 -> the five-digit FTSBB an ALLOC card writes."""
    f, t, s, b = (addr >> 11) & 7, (addr >> 8) & 7, (addr >> 5) & 7, addr & 31
    return f"{f}{t}{s}{b:02d}"


# --------------------------------------------------------------- decode

@dataclass
class Entry:
    blocks: int              # blocks-1 + 1
    lastWords: int           # last-block word count-1 + 1
    addrMsb: int             # 2 bits that extend the GPC address
    gpc: int

    @property
    def halfwords(self) -> int:
        return (self.blocks - 1) * HW_PER_BLOCK + self.lastWords


@dataclass
class Transaction:
    mm: int                  # tape address (mm16)
    blocks: int              # header blocks-1 + 1
    dsr: int
    protect: int
    entries: list[Entry] = field(default_factory=list)

    @property
    def halfwords(self) -> int:
        return sum(e.halfwords for e in self.entries)


@dataclass
class Table:
    name: str
    kind: str
    start: int               # halfword address in the phase image
    words: list[int]
    count: int | None = None          # leading transaction count, if any
    transactions: list[Transaction] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)


def _readTransactions(w: list[int], i: int,
                      limit: int | None = None) -> tuple[list[Transaction], int]:
    """Consume transactions from w[i:].

    A zero MM address is a legitimate transaction and not a terminator:
    `MMPURTBL`'s first purge covers tape address 00000.  The run is
    bounded by `limit` when a count halfword gave one, and stops on a zero
    header only when it did not.
    """
    out: list[Transaction] = []
    while i + 3 <= len(w) and (limit is None or len(out) < limit):
        mm, blk, prot = w[i], w[i + 1], w[i + 2]
        if limit is None and mm == 0:     # padding, not a header
            break
        t = Transaction(mm=mm, blocks=(blk >> 3) + 1, dsr=blk & 7, protect=prot)
        i += 3
        while i + 2 <= len(w) and w[i] != 0:
            e = w[i]
            t.entries.append(Entry(blocks=((e >> 11) & 31) + 1,
                                   lastWords=((e >> 2) & 0x1FF) + 1,
                                   addrMsb=e & 3, gpc=w[i + 1]))
            i += 2
        if i < len(w) and w[i] == 0:      # end-of-transaction flag
            i += 1
        out.append(t)
    return out, i


def decode(name: str, kind: str, start: int, words: list[int]) -> Table:
    t = Table(name=name, kind=kind, start=start, words=words)
    if kind in (TRANSACTION, PURGE):
        # A leading halfword is a transaction count only if reading exactly
        # that many consumes the table -- up to the fullword pad the next
        # `DS 0F` label forces.  Otherwise the table is a bare run.
        n = words[0] if words else 0
        if 0 < n <= (len(words) - 1) // 4:
            got, end = _readTransactions(words, 1, limit=n)
            if len(got) == n and len(words) - end <= 1:
                t.count, t.transactions = n, got
                return t
        t.transactions, _ = _readTransactions(words, 0)
    elif kind == MESSAGE:
        for v in words:
            if v == 0:
                break
            t.messages.append(v)
    return t


# ---------------------------------------------------------------- input

def loadTables(mmuRoot: Path, phase: int = 10) -> list[Table]:
    """Decode every table out of the built phase image."""
    d = mmuRoot / f"PHASE{phase:02d}"
    sym = json.loads((d / f"PHASE{phase:02d}.sym.json").read_text())
    image = (d / f"PHASE{phase:02d}.fcm").read_bytes() if (
        d / f"PHASE{phase:02d}.fcm").exists() else None
    if image is None:                      # composed SSW carries PHASE10 too
        image = (mmuRoot / "SSW" / "SSW.fcm").read_bytes()
    at = {s["name"]: s["address"] for s in sym["symbols"]
          if s.get("section") in ("MMLDRTBL", "MMUPUR")}
    ends = {s["name"]: s["address"] + s["size"] for s in sym["sections"]
            if s["name"] in ("MMLDRTBL", "MMUPUR")}
    order = [(n, k, at[n]) for n, k in TABLES if n in at]
    order.sort(key=lambda r: r[2])
    out = []
    for i, (name, kind, addr) in enumerate(order):
        if i + 1 < len(order) and order[i + 1][2] > addr:
            end = order[i + 1][2]
        else:
            end = max(e for e in ends.values() if e > addr)
        words = [int.from_bytes(image[a * 2:a * 2 + 2], "big")
                 for a in range(addr, end)]
        out.append(decode(name, kind, addr, words))
    return out



# ------------------------------------------------------------- generate

# One file/track pair -- 8 subfiles of 32 blocks -- is the unit the purge
# sweep erases and what "end of file" means to the transport.
BLOCKS_PER_REGION = 256
REGIONS = 64                       # 8 files x 8 tracks, the whole tape
PURGE_SLOTS = 75                   # the delivered table's capacity
SPARE = 0xFFFF


def encodeHeader(mm: int, blocks: int, dsr: int = 0,
                 protect: int = NO_PROTECT) -> list[int]:
    return [mm, ((blocks - 1) << 3) | dsr, protect]


def encodeEntry(blocks: int, lastWords: int, gpc: int,
                addrMsb: int = 0) -> list[int]:
    return [((blocks - 1) << 11) | ((lastWords - 1) << 2) | addrMsb, gpc]


def purgeTable(regions: int = REGIONS, slots: int = PURGE_SLOTS) -> list[int]:
    """The MMU purge/overwrite table, derived from tape geometry alone.

    Every entry is one file/track pair, ascending, `mm16` stepping by 0x100;
    the table is padded out to its capacity with all-ones slots and then to
    a fullword.  Nothing here comes from the load-build cards -- the sweep
    covers the whole tape whatever is on it -- which is why this one is
    fully derivable and is the closed-loop check on the encoding.
    """
    w = [slots]
    for i in range(regions):
        w += encodeHeader(i * 0x100, BLOCKS_PER_REGION)
        w += encodeEntry(1, BLOCKS_PER_REGION, 0x0000, addrMsb=1)
        w.append(0)                                    # end of transaction
    for _ in range(slots - regions):
        w += [SPARE] * 5 + [0]
    if len(w) % 2:
        w.append(0)                                    # DS 0F pad
    return w


PURGE_LIVE_MSG = 0x009C            # "purge data" message
PURGE_SPARE_MSG = 0x809C           # bit0 set = do not load this transaction


def purgeMessages(regions: int = REGIONS,
                  slots: int = PURGE_SLOTS) -> list[int]:
    """The purge table's message table: one halfword per slot, the live
    regions carrying the purge message and the spare slots the same message
    with bit 0 -- "do not load this transaction" -- set.  Same 64/11 split
    as `purgeTable`, which is what ties the two together."""
    return [PURGE_LIVE_MSG] * regions + [PURGE_SPARE_MSG] * (slots - regions)


# The DEU load: three transactions, in this order, each a whole named load.
# `copy` picks the tape copy; the ground build writes NUMCOPY of each with
# identical content, so the choice cannot change what is loaded.
DEU_LOADS = [("DCP", "FMADEU1{n}"), ("DST", "FMADEU2{n}"),
             ("CRTFMT", "DMACDFT{n}")]


def deuTable(allocs: list[Alloc], hwds: dict[str, int], gpc: list[int],
             copy: int = 1) -> tuple[list[int], list[str]]:
    """The DEU DCP/DST/critical-format transaction table, from the cards.

    Derived: the tape address (ALLOC ADDR), the block count (ALLOC BLKS)
    and the load length (SYSTEM HWDS, split into whole blocks plus a
    last-block remainder).  NOT derived, and passed in by the caller: the
    GPC main-store destination of each load, which no load-build card
    carries: it belongs to the loader.
    """
    byLabel = {a.label: a for a in allocs}
    w, notes = [len(DEU_LOADS)], []
    for (what, pat), dest in zip(DEU_LOADS, gpc):
        label = pat.format(n=copy)
        a = byLabel.get(label)
        if a is None:                       # DST has only two copies
            a = byLabel[pat.format(n=1)]
            notes.append(f"{what}: no copy {copy} ({label}); used {a.label}")
        n = hwds[a.label]
        last = n - (a.blocks - 1) * HW_PER_BLOCK
        if not 1 <= last <= HW_PER_BLOCK:
            raise ValueError(f"{a.label}: HWDS {n:X} does not fit "
                             f"{a.blocks} blocks")
        w += encodeHeader(a.addr, a.blocks)
        w += encodeEntry(a.blocks, last, dest)
        w.append(0)
        notes.append(f"{what}: {a.label} {fmtCard(a.addr)} "
                     f"{a.blocks} blocks, {n:X} hw -> gpc {dest:04X}")
    return w, notes            # no pad: the next `DS 0F` label owns that


# --------------------------------------------------------------- verify

@dataclass
class Alloc:
    label: str
    addr: int
    blocks: int
    sysid: str
    member: str


def allocations(con80: Path) -> list[Alloc]:
    b = build_deck(MMUDeck(con80))
    out = []
    for st in b.allocations():
        if "ADDR" not in st.params:
            continue
        out.append(Alloc(label=st.label, addr=mm16(st.params["ADDR"]),
                         blocks=int(st.params.get("BLKS", 1)),
                         sysid=st.params.get("SYSID", ""), member=st.source))
    return out


def systemHwds(con80: Path) -> dict[str, int]:
    b = build_deck(MMUDeck(con80))
    out = {}
    for st in b.systems():
        if st.label and "HWDS" in st.params:
            out[st.label] = int(st.params["HWDS"], 16)
    return out


def copiesOf(a: Alloc, allocs: list[Alloc],
             hwds: dict[str, int]) -> list[Alloc]:
    """The other tape copies of one LOAD.  The ground build gives a load
    NUMCOPY copies under sibling labels (FMADEU11/12/13); they carry
    identical content, so which one a transaction names cannot change what
    gets loaded -- provided every copy is actually written.

    The stem alone is too loose: it makes SMASM21 and SMASM41 copies of
    each other, and all 34 SMARDPnn roll-ins one family.  A copy is the
    same length and a named load (a SYSTEM card), and both conditions
    gate it.
    """
    if a.label not in hwds:
        return [a]
    stem = a.label.rstrip("0123456789")
    return sorted((x for x in allocs
                   if x.label.rstrip("0123456789") == stem
                   and x.blocks == a.blocks and x.label in hwds),
                  key=lambda x: x.label)


def verify(tables: list[Table], allocs: list[Alloc],
           hwds: dict[str, int], out=sys.stdout) -> dict:
    """For every transaction, find the allocation that owns its tape address
    and report what the cards do and do not determine.

    Two joins.  A transaction that starts exactly on an ALLOC is that whole
    load, so its block count equals BLKS and its length equals the SYSTEM
    card's HWDS.  A transaction that starts inside one is a piece of it --
    the TFL formats each read one block out of an 85-block area -- and
    there the only thing the cards
    determine is that the piece fits.
    """
    byAddr: dict[int, list[Alloc]] = {}
    for a in allocs:
        byAddr.setdefault(a.addr, []).append(a)
    covering = sorted(allocs, key=lambda a: a.addr)

    def owner(mm: int) -> tuple[Alloc | None, int]:
        if mm in byAddr:
            return byAddr[mm][0], 0
        for a in covering:
            if a.addr <= mm < a.addr + a.blocks:
                return a, mm - a.addr
        return None, 0

    stats = {"transactions": 0, "exact": 0, "inside": 0, "unowned": 0,
             "blocks_ok": 0, "hwds_ok": 0, "overrun": 0, "findings": [],
             "purge_regions": 0, "purge_spare": 0}
    for t in tables:
        if t.kind == PURGE:
            live = [x for x in t.transactions if x.mm != 0xFFFF]
            spare = len(t.transactions) - len(live)
            step = {live[i + 1].mm - live[i].mm for i in range(len(live) - 1)}
            print(f"\n{t.name}  @{t.start:05X}  {len(t.words)} hw"
                  f"  count={t.count}  {len(live)} regions + {spare} FFFF "
                  f"spare", file=out)
            print(f"  a tape-region sweep, not a load: "
                  f"{fmtCard(live[0].mm)}..{fmtCard(live[-1].mm)}, "
                  f"{live[0].blocks} blocks each"
                  f"{', step ' + str(sorted(step)) if len(step) > 1 else ''}"
                  f" -- one file/track pair per entry, so the load-allocation "
                  f"join says nothing about it", file=out)
            stats["purge_regions"] = len(live)
            stats["purge_spare"] = spare
            continue
        if t.kind != TRANSACTION:
            continue
        print(f"\n{t.name}  @{t.start:05X}  {len(t.words)} hw"
              f"{'' if t.count is None else f'  count={t.count}'}"
              f"  {len(t.transactions)} transactions", file=out)
        for n, tr in enumerate(t.transactions, 1):
            stats["transactions"] += 1
            a, off = owner(tr.mm)
            note = []
            if a is None:
                stats["unowned"] += 1
                where = "no ALLOC covers it"
            elif off == 0:
                stats["exact"] += 1
                where = f"{a.label} {a.sysid} ({a.member})"
                # An allocation with a SYSTEM card is a single load whose
                # whole extent one transaction reads.  Starting on the base
                # of a plain data area (the TFL formats do) says nothing
                # about BLKS: that read is one block of eighty-five.
                want = hwds.get(a.label)
                if want is None:
                    where += f", one piece of its {a.blocks} blocks"
                else:
                    if a.blocks == tr.blocks:
                        stats["blocks_ok"] += 1
                    else:
                        note.append(f"{FINDING} blocks {tr.blocks} vs "
                                    f"BLKS {a.blocks}")
                    if tr.halfwords == want:
                        stats["hwds_ok"] += 1
                    else:
                        note.append(f"{FINDING} length {tr.halfwords:X} vs "
                                    f"HWDS {want:X}")
                sibs = copiesOf(a, allocs, hwds)
                if len(sibs) > 1:
                    note.append("copy " + "/".join(
                        f"{'>' if x.label == a.label else ''}{x.label}"
                        for x in sibs))
            else:
                stats["inside"] += 1
                where = f"inside {a.label} +{off} of {a.blocks}"
                if off + tr.blocks > a.blocks:
                    stats["overrun"] += 1
                    note.append(f"{FINDING} runs {off + tr.blocks - a.blocks} "
                                f"blocks past the allocation")
            print(f"  #{n} {fmtCard(tr.mm)} ({tr.mm:04X}) blocks={tr.blocks}"
                  f" len={tr.halfwords} gpc="
                  f"{'/'.join(f'{e.gpc:04X}' for e in tr.entries)}"
                  f"  {where}", file=out)
            if note:
                print(f"      {'; '.join(note)}", file=out)
                for x in note:
                    if x.startswith(FINDING):
                        stats["findings"].append((t.name, n, x))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mmtrans", description=__doc__)
    ap.add_argument("--con80", type=Path,
                    default=Path("code/OI340700/CON80"))
    ap.add_argument("--mmu", type=Path, default=Path("build/mmu"))
    ap.add_argument("--tables", action="store_true", help="decode and print")
    ap.add_argument("--verify", action="store_true", help="join to the cards")
    ap.add_argument("--generate", action="store_true",
                    help="regenerate the derivable tables and diff them "
                         "against what the build assembled")
    a = ap.parse_args(argv)
    tables = loadTables(a.mmu)
    if a.tables or not a.verify:
        for t in tables:
            print(f"{t.name:9} {t.kind:11} @{t.start:05X} {len(t.words):3} hw "
                  f"{' '.join(f'{w:04X}' for w in t.words[:12])}"
                  f"{' ...' if len(t.words) > 12 else ''}")
    if a.verify:
        st = verify(tables, allocations(a.con80), systemHwds(a.con80))
        print(f"\n{st['transactions']} transactions: {st['exact']} start on "
              f"an ALLOC ({st['blocks_ok']} agree with BLKS, {st['hwds_ok']} "
              f"with HWDS), {st['inside']} address a piece of one "
              f"({st['overrun']} overrun), {st['unowned']} unowned")
        print(f"purge sweep: {st['purge_regions']} tape regions "
              f"+ {st['purge_spare']} spare slots")
        for f in st["findings"]:
            print(f"  {f[2]}  ({f[0]} #{f[1]})")
    if a.generate:
        built = {t.name: t for t in tables}
        allocs, hwds = allocations(a.con80), systemHwds(a.con80)
        rc = 0

        def diff(name: str, want: list[int], how: str,
                 notes: list[str] | None = None) -> int:
            # Compare the table proper.  Anything the built csect carries
            # beyond it is the fullword pad the next `DS 0F` forces, and in
            # the image that is IPL fill (C9FB), not table content.
            got = built[name].words[:len(want)]
            bad = [(i, g, w) for i, (g, w) in enumerate(zip(got, want))
                   if g != w]
            for n in notes or []:
                print(f"  {n}")
            if len(got) == len(want) and not bad:
                print(f"{name}: {len(want)}/{len(want)} halfwords "
                      f"reproduced {how}")
                return 0
            print(f"{name}: {len(bad)} of {len(want)} halfwords differ "
                  f"({how})")
            for i, g, w in bad[:12]:
                print(f"  +{i:04d} built {g:04X} generated {w:04X}")
            return 1

        rc |= diff("MMPURTBL", purgeTable(), "from tape geometry alone")
        rc |= diff("MMUPURMT", purgeMessages(), "from the same 64/11 split")
        # The GPC destinations belong to the loader, and are carried from
        # what the build assembled.
        dest = [t.entries[0].gpc for t in built["FMADEU11"].transactions]
        for copy in (1, 2, 3):
            print(f"\nFMADEU11 as tape copy {copy}:")
            w, notes = deuTable(allocs, hwds, dest, copy=copy)
            diff("FMADEU11", w, f"from the SYS5 cards, copy {copy}", notes)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
