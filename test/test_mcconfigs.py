#!/usr/bin/env python3
"""Regression tests for ap101Utils.mcconfigs: the deck-driven MC registry.

Covers: parsing the real CON80 deck and matching the pinned EXPECTED
registry below; the interface (load_configs, CONFIG_NAMES,
McConfig.describe, listConfigs, IPL_PHASES); and fail-loud drift
detection on a mutated deck copy."""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'src'))

from ap101Utils.concard import ConcardDeck                    # noqa: E402
from ap101Utils.mcconfigs import (CONFIG_NAMES, IPL_PHASES,   # noqa: E402
                                  McConfigError, listConfigs,
                                  load_configs)

_failures, _passes = [], 0


def check(name, cond, detail=''):
    global _passes
    if cond:
        _passes += 1
    else:
        _failures.append(f'{name}: {detail}')


DECK = ROOT / 'code' / 'OI340600' / 'CON80'

# The pinned registry: what the deck + the CZ2V_GRT_PHASES rows must
# produce.  A parse that differs is deck drift -- investigate before
# updating this table.
EXPECTED = {
    "SSW": (10, 2, 13, 3),
    "G16": (10, 2, 13, 3, 4),
    "G2":  (10, 2, 13, 3, 5),
    "G3":  (10, 2, 13, 3, 6),
    "G8":  (10, 2, 13, 3, 7),
    "G9":  (10, 2, 13, 3, 8, 18),
    "P9":  (10, 2, 13, 3, 9, 12),
    "S2":  (10, 2, 13, 3, 14, 15),
    "S4":  (10, 2, 13, 3, 14, 16),
}

# ---------------------------------------------------------------- real deck
cfgs = load_configs(ConcardDeck(DECK))
check('registry matches EXPECTED',
      {n: c.phases for n, c in cfgs.items()} == EXPECTED,
      f'{ {n: c.phases for n, c in cfgs.items()} }')
check('SSW is the MMLOAD IPL set', cfgs['SSW'].phases == IPL_PHASES,
      f"{cfgs['SSW'].phases}")
check('CONFIG_NAMES covers the registry',
      set(CONFIG_NAMES) == set(cfgs), f'{CONFIG_NAMES}')
check('MC numbers round-trip',
      {c.mc for c in cfgs.values() if c.mc} == {1, 2, 3, 4, 5, 6, 8, 9})
check('describe() renders', all('phases' in c.describe()
                                and c.name in c.describe()
                                for c in cfgs.values()))
check('listConfigs lists every config',
      all(n in listConfigs(cfgs) for n in EXPECTED))

# ------------------------------------------------------------- failure modes
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / 'CON80'
    tmp.mkdir()
    for m in ('MMLOAD', 'MMUSYS1'):
        shutil.copy(DECK / m, tmp / m)
    check('deck copy parses clean',
          load_configs(ConcardDeck(tmp))['G9'].phases ==
          (10, 2, 13, 3, 8, 18))

    # drift 1: move MC=1 from PH=4 to PH=5 (MC= card off the GRT-row tail)
    text = (DECK / 'MMUSYS1').read_text()
    (tmp / 'MMUSYS1').write_text(text.replace('PH=4,MC=1', 'PH=4')
                                     .replace('PH=5,PHTAB=YES,MC=2',
                                              'PH=5,PHTAB=YES,MC=1'))
    try:
        load_configs(ConcardDeck(tmp))
        check('MC reassignment drift raises', False, 'no exception')
    except McConfigError as e:
        check('MC reassignment drift raises', 'drift' in str(e), str(e))

    # drift 2: remove a GRT-row phase's PHASE card entirely
    (tmp / 'MMUSYS1').write_text(
        '\n'.join(l for l in text.splitlines() if 'PH=8;' not in l) + '\n')
    try:
        load_configs(ConcardDeck(tmp))
        check('missing PHASE card raises', False, 'no exception')
    except McConfigError as e:
        check('missing PHASE card raises',
              'no MMUSYS1 PHASE card' in str(e), str(e))

    # drift 3: IPL set changed
    (tmp / 'MMUSYS1').write_text(text)
    ipl_text = (DECK / 'MMLOAD').read_text()
    (tmp / 'MMLOAD').write_text(
        ipl_text.replace('PH=(10,2,13,3)', 'PH=(10,2,13)'))
    try:
        load_configs(ConcardDeck(tmp))
        check('IPL drift raises', False, 'no exception')
    except McConfigError as e:
        check('IPL drift raises', 'IPL set' in str(e), str(e))

    # missing member
    (tmp / 'MMLOAD').unlink()
    try:
        load_configs(ConcardDeck(tmp))
        check('missing MMLOAD raises', False, 'no exception')
    except McConfigError as e:
        check('missing MMLOAD raises', 'MMLOAD' in str(e), str(e))

# ------------------------------------------------------------------- report
print(f'{_passes} passed, {len(_failures)} failed')
for f in _failures:
    print(f'FAIL {f}')
sys.exit(1 if _failures else 0)
