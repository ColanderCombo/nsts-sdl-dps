"""Link-order pins (linkorder.json).

Placement orders the linkage editor used but that are not derivable from
the link inputs are supplied as a JSON pins file: `lnk101 --link-order`,
`con80build --link-order` (default `<gen tree>/linkorder.json`).  Without
one, orderings fall back to deterministic defaults and no pinned autocall
placement applies.

Top-level keys (all optional):

  zconPool     [name...]   Z1 ZCON-pool ordering
  orphanFlush  [name...]   cross-module orphan program-flush order
  poolBlocks   [{after, waves}]   pool blocks packed after an existing
               pool member: `waves` lists module stems per wave; a wave
               contributes its modules' #Z thunks plus their #Q ERs
               (EBCDIC-sorted), minus #Q names the base pool defines
  mc           {name: {...}}   per-memory-configuration sections; a
               section applies when the deck INSERTs its `anchor` csect
               (first match in file order wins)

Per-mc keys:

  anchor        name       deck INSERT that selects this section
  streams       {bank: [name...]}   per-bank autocall placement streams
                ("+2" pins a 2-hw checksum slot); when present, stream
                placement is used instead of the wave placement
  pool          [name...]  Z1-pool order for the job's autocalled ZCONs
  poolAfter     name       pool member that block packs after
  preblock      [name...]  autocall pre-block csects
  preblockAfter name       block the pre-block rides after
  wave1Order    [stem...]  bank-0 tail module order
  compoolOrder  [name...]  #P compool wave order
  codeOrder     [stem...]  code-run module order
  codeBank      int        bank of the code run (default 2)
"""

import json
from pathlib import Path


class McPins:
    """One per-memory-configuration pins section."""

    def __init__(self, name: str = "", d: dict | None = None):
        d = d or {}
        self.name = name
        self.anchor: str | None = d.get("anchor")
        self.streams: dict[int, list[str]] = {
            int(b): list(v) for b, v in d.get("streams", {}).items()}
        self.pool: list[str] = list(d.get("pool", []))
        self.poolAfter: str | None = d.get("poolAfter")
        self.preblock: list[str] = list(d.get("preblock", []))
        self.preblockAfter: str | None = d.get("preblockAfter")
        self.wave1Order: list[str] = list(d.get("wave1Order", []))
        self.compoolOrder: list[str] = list(d.get("compoolOrder", []))
        self.codeOrder: list[str] = list(d.get("codeOrder", []))
        self.codeBank: int = int(d.get("codeBank", 2))


class LinkOrder:
    def __init__(self, data: dict | None = None):
        d = data or {}
        self.zconPool: list[str] = list(d.get("zconPool", []))
        self.orphanFlush: list[str] = list(d.get("orphanFlush", []))
        self.poolBlocks: list[tuple[str | None, list[list[str]]]] = [
            (b.get("after"), [list(w) for w in b.get("waves", [])])
            for b in d.get("poolBlocks", [])]
        self.mc: dict[str, McPins] = {
            name: McPins(name, sub) for name, sub in d.get("mc", {}).items()}
        self._ordinal = {n: i for i, n in enumerate(self.zconPool)}

    @classmethod
    def load(cls, path) -> "LinkOrder":
        """Read a pins file.  Raises OSError/ValueError on an unreadable or
        malformed file -- an explicitly given pins file must never be
        silently ignored."""
        d = json.loads(Path(path).read_text())
        if not isinstance(d, dict):
            raise ValueError(f"{path}: expected a top-level JSON object")
        return cls(d)

    # -- Z1 ZCON pool ------------------------------------------------------

    def zcon_sort_key(self, name: str, override: dict | None = None) -> tuple:
        """Sort key for a ZCON csect in the Z1 pool.

        An `override` entry (see pool_block_override) wins; otherwise known
        ZCONs sort by their pool position and unknown ones after all known,
        alphabetically, so placement stays deterministic."""
        n = name.strip()
        if override:
            k = override.get(n)
            if k is not None:
                return k
        o = self._ordinal.get(n)
        if o is not None:
            return (0, o, 0, n)
        return (1, len(self.zconPool), 0, n)

    # -- pool blocks -------------------------------------------------------

    def wave_modules(self) -> set:
        """Every module stem named by a poolBlocks wave."""
        return {m for _, waves in self.poolBlocks for w in waves for m in w}

    def pool_block_override(self, module_q_ers, base_defined_q) -> dict:
        """{name: sort key} pinning each pool block contiguously right
        after its `after` pool member: per wave, the modules' #Z thunks in
        wave order, then their new #Q ERs EBCDIC-sorted (cp037: letters
        sort before digits).

        module_q_ers: {module stem: iterable of #Q ER names from its ESD}
        for the wave modules present in the link."""
        override = {}
        for after, waves in self.poolBlocks:
            anchor = self._ordinal.get(after) if after else None
            if anchor is None:
                continue
            seq, defined = [], set(base_defined_q)
            for wave in waves:
                seq += ["#Z" + m for m in wave]
                new = {q for m in wave
                       for q in module_q_ers.get(m, ())} - defined
                seq += sorted(new, key=lambda n: n.encode("cp037"))
                defined |= new
            override.update({n: (0, anchor, i + 1, n)
                             for i, n in enumerate(seq)})
        return override

    # -- per-mc sections ---------------------------------------------------

    def mc_for(self, names) -> "McPins | None":
        """The first mc section whose anchor csect is in `names` (a deck's
        INSERT operands, or the link's defined SDs)."""
        for mc in self.mc.values():
            if mc.anchor and mc.anchor in names:
                return mc
        return None

    def pool_override(self, mc: McPins) -> dict:
        """{name: sort key} pinning an mc section's pool order right after
        its poolAfter pool member."""
        base = self._ordinal.get(mc.poolAfter, 0) if mc.poolAfter else 0
        return {n: (0, base, 1000 + i, n) for i, n in enumerate(mc.pool)}
