"""mcconfigs -- the GPC memory-configuration registry for mmu2fcm.

A memory configuration (MC) is the ordered list of phase load modules the
loader places into GPC memory: the IPL set first, then the MC's application
phases at OPS transition.  Later phases overlay earlier ones, so the order
matters as much as the membership.

Sources, cross-checked against each other at load (any mismatch raises
McConfigError -- deck drift is a finding, not something to silently
follow):

  * CON80/MMLOAD  `IPL,PH=(10,2,13,3)`  -- the IPL set and its load order.
  * CON80/MMUSYS1 `PHASE,PH=n[,MC=m]`   -- the phase census; MC= tags each
    MC's final overlay phase (only that phase -- the deck does not carry
    the rest of an MC's list).

load_configs(deck) takes a constructed concard.ConcardDeck and returns
the validated registry as a plain {name: McConfig} dict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Expected value of the CON80/MMLOAD `IPL,PH=(...)` card.  SSW (= OPS000,
# the post-IPL state) is exactly this set.
IPL_PHASES = (10, 2, 13, 3)

# MC number -> (config name, CZ2V_GRT_PHASES row, note).  Rows are the
# flight table (SSSRC/CZ2COMMO) in ARCGPC load order: MFB column first,
# then PGM columns.  The deck's MC= card must sit on the last element of
# its row.
_MCS: dict[int, tuple[str, tuple[int, ...], str]] = {
    1: ("G16", (3, 4),     "GNC OPS1/6"),
    2: ("G2",  (3, 5),     "GNC OPS2"),
    3: ("G3",  (3, 6),     "GNC OPS3"),
    4: ("S2",  (14, 15),   "SM OPS2"),
    5: ("S4",  (14, 16),   "SM OPS4"),
    6: ("P9",  (9, 12),    "PL OPS9"),
    8: ("G8",  (3, 7),     "GNC OPS8"),
    9: ("G9",  (3, 8, 18), "GNC OPS9"),
}

# Every config name, IPL set first, then by MC number.
CONFIG_NAMES = ("SSW",) + tuple(_MCS[mc][0] for mc in sorted(_MCS))


class McConfigError(RuntimeError):
    """CON80 deck missing a member, unparsable, or inconsistent with _MCS."""


@dataclass(frozen=True)
class McConfig:
    """One memory configuration: name and ordered phase load list."""
    name: str
    phases: tuple[int, ...]
    mc: int | None = None       # flight MC number; None for the IPL set
    note: str = ""
    row: tuple[int, ...] = ()   # GRT row (MFB + PGMs); () for the IPL set

    @property
    def mcf_phases(self) -> frozenset[int]:
        if self.row:
            return frozenset({2, 13} | set(self.row))
        return frozenset(self.phases[1:])

    def describe(self) -> str:
        mc = f"MC{self.mc}" if self.mc is not None else "IPL"
        ph = ",".join(str(p) for p in self.phases)
        return f"{self.name:<4s} {mc:<4s} phases {ph:<16s} {self.note}"


#
# Deck parsing
#

_IPL_RE = re.compile(r"PH=\((\d+(?:,\d+)*)\)")
_PH_RE = re.compile(r"\bPH=(\d+)")
_MC_RE = re.compile(r"\bMC=(\d+)")


def _parse_ipl_phases(deck) -> tuple[int, ...]:
    """IPL set from the CON80/MMLOAD `IPL,PH=(...)` card, in card order."""
    for d in deck.read("MMLOAD"):
        if d.is_directive and d.verb == "IPL":
            m = _IPL_RE.search(d.operand)
            if not m:
                raise McConfigError(
                    f"MMLOAD IPL card has no PH=(...) list: {d.raw!r}")
            return tuple(int(t) for t in m.group(1).split(","))
    raise McConfigError(f"no IPL card found in {deck.path}/MMLOAD")


def _parse_phase_cards(deck) -> tuple[set[int], dict[int, int]]:
    """From CON80/MMUSYS1 PHASE cards: (all PH numbers, {mc: PH of the
    card carrying MC=mc})."""
    phases: set[int] = set()
    mc_phase: dict[int, int] = {}
    for d in deck.read("MMUSYS1"):
        if not (d.is_directive and d.verb == "PHASE"):
            continue
        mph = _PH_RE.search(d.operand)
        if not mph:
            raise McConfigError(
                f"MMUSYS1 PHASE card without PH=: {d.raw!r}")
        ph = int(mph.group(1))
        phases.add(ph)
        mmc = _MC_RE.search(d.operand)
        if mmc:
            mc = int(mmc.group(1))
            if mc in mc_phase:
                raise McConfigError(
                    f"MMUSYS1 assigns MC={mc} twice (PH={mc_phase[mc]} "
                    f"and PH={ph})")
            mc_phase[mc] = ph
    if not phases:
        raise McConfigError(f"no PHASE cards found in {deck.path}/MMUSYS1")
    return phases, mc_phase


def load_configs(deck) -> dict[str, McConfig]:
    """Validate `deck` (a concard.ConcardDeck) and return the registry.

    Raises McConfigError on missing members, unparsable cards, or any
    deck-vs-_MCS inconsistency.
    """
    for member in ("MMLOAD", "MMUSYS1"):
        if not deck.has(member):
            raise McConfigError(
                f"CON80 deck {deck.path} has no {member} member")

    ipl = _parse_ipl_phases(deck)
    deck_phases, mc_phase = _parse_phase_cards(deck)

    problems: list[str] = []
    if ipl != IPL_PHASES:
        problems.append(f"MMLOAD IPL set {ipl} != expected {IPL_PHASES}")
    if set(mc_phase) != set(_MCS):
        problems.append(f"MMUSYS1 MC= set {sorted(mc_phase)} != expected "
                        f"{sorted(_MCS)}")
    for p in ipl:
        if p not in deck_phases:
            problems.append(f"IPL phase {p} has no MMUSYS1 PHASE card")
    for mc, (name, row, _) in _MCS.items():
        if mc in mc_phase and mc_phase[mc] != row[-1]:
            problems.append(
                f"MC{mc}: MMUSYS1 MC= card is on PH={mc_phase[mc]}, but "
                f"GRT row {row} ends in {row[-1]}")
        for p in row:
            if p not in deck_phases:
                problems.append(
                    f"MC{mc}: GRT phase {p} has no MMUSYS1 PHASE card")
    if problems:
        raise McConfigError(
            "CON80 deck drift vs derived registry (deck %s):\n  %s"
            % (deck.path, "\n  ".join(problems)))

    configs = {"SSW": McConfig("SSW", ipl, note="OPS000 post-IPL state")}
    for mc, (name, row, note) in sorted(_MCS.items()):
        # An MFB phase may repeat one the IPL set already loads (the GNC
        # rows re-list 3); composition is idempotent, so list it once.
        configs[name] = McConfig(
            name, ipl + tuple(p for p in row if p not in ipl),
            mc=mc, note=note, row=row)
    return configs


def listConfigs(configs: dict[str, McConfig]) -> str:
    """Human-readable registry dump (for --list-configs)."""
    return "\n".join(c.describe() for c in configs.values())
