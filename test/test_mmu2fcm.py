#!/usr/bin/env python3
"""Regression tests for tools.mmu2fcm: the GPC load emulation that
composes phase .lib modules into a memory-configuration .fcm.

Synthetic two-phase fixture exercising: IPL background fill, extent
placement, later-phase overlay precedence, overlay-aware sym.json union
(real text beats imported markers; relocations at overlaid sites dropped),
and the collision report (byte-differing only; identical re-supply and
same-name re-supply are benign)."""
import json
import logging
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'src'))

from ap101Utils.libModule import Extent, LibModule           # noqa: E402
from tools.mmu2fcm import (FILL_SPLIT_HW, MEM_HW, compose,     # noqa: E402
                                reportOverlays, unionSym, main)
from ap101Utils import mmbstamp                              # noqa: E402

_failures, _passes = [], 0


def check(name, cond, detail=''):
    global _passes
    if cond:
        _passes += 1
    else:
        _failures.append(f'{name}: {detail}')


def makePhase(root: Path, n: int, extents, sym):
    lib = LibModule(name=f'PHASE{n:02d}')
    lib.extents = [Extent(address=a, data=bytes(d)) for a, d in extents]
    lib.crossResolved = True
    lib.xresCount = 1
    lib.write(root / f'PHASE{n:02d}.lib')
    d = root / f'PHASE{n:02d}'
    d.mkdir()
    base = dict(version='test', imageSize=0, entryPoint=0, sections=[],
                symbols=[], modules=[], relocations=[],
                unresolvedRelocations=[], repro={})
    base.update(sym)
    json.dump(base, open(d / f'PHASE{n:02d}.sym.json', 'w'))


def runMmbstamp():
    """mmbstamp unit checks: MMU address decode, MM packing (flight PHASE04/
    PHASE05 load-block lengths as fixtures -- SOT placement, track-skip
    accounting, multi-track flag), load-block derivation from a synthetic
    .lib (pool cursor + re-supply drop, checksum-slot merge, 16K merge cap,
    bank split, protection split, HIMEM drop), and the stamp() overlay."""
    # -- ALLOC ADDR=FTSBB decode (verified sites from MMUDAT1/3) ------------
    check('mm16_g9r', mmbstamp.mm16('56000') == 0x2E00)
    check('mm16_mfb', mmbstamp.mm16('33600') == 0x1BC0)
    check('mm16_ipl3', mmbstamp.mm16('03530') == 0x03BE)
    try:
        mmbstamp.mm16('38000')          # subfile 8 out of range
        check('mm16_range', False, 'no error for subfile 8')
    except mmbstamp.StampError:
        check('mm16_range', True)

    # -- MM packing: flight PHASE04 LB lengths from ALLOC 0x2C00 ------------
    # (SSW DASS: SOT on LB20 only; NUM_CONT = 404 data + 10 skipped = 414
    # with the multi-track flag)
    ph4 = [626, 94, 82, 3214, 6234, 7088, 16024, 1876, 16382, 16138, 2306,
           3056, 2698, 2278, 16382, 16352, 478, 766, 9662, 16378, 1766,
           4246, 12142, 14170, 8090, 16350, 4598, 1682]
    lbs = [mmbstamp.LoadBlock(0, ln, False) for ln in ph4]
    ncont, crossed = mmbstamp.pack_mm(lbs, 0x2C00)
    check('pack_ph4_ncont', (ncont, crossed) == (414, True),
          f'{ncont},{crossed}')
    check('pack_ph4_sot', [i for i, lb in enumerate(lbs) if lb.sot] == [19],
          str([i for i, lb in enumerate(lbs) if lb.sot]))
    # flight PHASE05 from ALLOC+PHTAB 0x2905: SOT on LB23, NUM_CONT 292
    ph5 = [418, 94, 82, 242, 1662, 1304, 6234, 7676, 15576, 1736, 1020,
           16378, 14866, 2156, 3194, 2724, 2278, 16368, 16366, 504, 772,
           10790, 9538, 1334, 336, 1882, 2620, 2262, 1262]
    lbs = [mmbstamp.LoadBlock(0, ln, False) for ln in ph5]
    ncont, crossed = mmbstamp.pack_mm(lbs, 0x2905)
    check('pack_ph5', (ncont, crossed) == (292, True), f'{ncont},{crossed}')
    check('pack_ph5_sot', [i for i, lb in enumerate(lbs) if lb.sot] == [22],
          str([i for i, lb in enumerate(lbs) if lb.sot]))

    # -- load-block derivation from a synthetic phase ------------------------
    lib = LibModule(name='PHASE99')
    lib.linkState = {'zconPoolNext': 2 * 0x120}   # pool cursor at hw 0x120
    lib.extents = [
        Extent(2 * 0x0E0, b'\x11\x11' * 2),   # re-supply below parent cursor
        Extent(2 * 0x100, b'\x22\x22' * 16),  # own pool cells
        Extent(2 * 0x200, b'\x33\x33' * 14),  # unit [0x200,0x210)
        Extent(2 * 0x210, b'\x44\x44' * 16),  # contiguous: merges
        Extent(2 * 0x300, b'\x55\x55' * 10),  # P0 unit [0x300,0x30C)
        Extent(2 * 0x30C, b'\x66\x66' * 4),   # P1: protection split
        Extent(2 * 0x7FF0, b'\x77\x77' * 32), # crosses bank 0 -> 1
        Extent(2 * 0x40000, b'\x88\x88' * 8), # HIMEM: dropped
    ]
    prot = {0x30C: True, 0x30D: True, 0x30E: True, 0x30F: True}
    look = lambda hw: prot.get(hw, False)     # noqa: E731
    lbs = mmbstamp.derive_load_blocks(lib, parent_pool=0x100, look=look,
                                      himem=False)
    got = [(lb.start, lb.length, lb.protected) for lb in lbs]
    check('lb_derive', got == [
        (0x100, 0x20, True),        # pool: [own start, zconPoolNext), prot
        (0x200, 0x22, False),       # two checksum groups merged
        (0x300, 0xC, False),        # split at protection change
        (0x30C, 0x6, True),
        (0x7FF0, 0x10, False),      # capped at the bank boundary
        (0x8000, 0x12, False),      # remainder in bank 1
    ], str([(hex(a), hex(b), c) for a, b, c in got]))
    check('lb_words', lbs[5].words() == (0x8000, 0x0610, 0x12),
          str(lbs[5].words()))
    # merge cap: two adjacent 8200-hw groups stay separate (> 16384 merged)
    lib2 = LibModule(name='PHASE98')
    lib2.extents = [Extent(2 * 0x1000, b'\x9A' * 2 * 8198),
                    Extent(2 * 0x3008, b'\x9B' * 2 * 8198)]
    lbs2 = mmbstamp.derive_load_blocks(lib2, 0, lambda hw: False, False)
    check('lb_cap', [(lb.start, lb.length) for lb in lbs2]
          == [(0x1000, 8200), (0x3008, 8200)],
          str([(hex(lb.start), lb.length) for lb in lbs2]))

    # -- stamp(): overlay at the composed csect addresses --------------------
    tables = mmbstamp.PhaseTables(
        gpt=list(range(mmbstamp.GPT_SIZE)),
        cpha=list(range(mmbstamp.CPHA_SIZE)))
    img = bytes(2 * 0x3000)
    sym = {'sections': [
        {'name': '#PFCMGPT', 'address': 0x1000, 'size': mmbstamp.GPT_SIZE},
        {'name': '#PCDCPHA', 'address': 0x2000, 'size': mmbstamp.CPHA_SIZE}]}
    out = mmbstamp.stamp(img, sym, tables)
    check('stamp_gpt',
          out[2 * 0x1000:2 * 0x1000 + 6] == b'\x00\x00\x00\x01\x00\x02')
    check('stamp_cpha',
          out[2 * 0x2000:2 * 0x2000 + 4] == b'\x00\x00\x00\x01')
    check('stamp_len', len(out) == len(img))
    try:
        mmbstamp.stamp(img, {'sections': []}, tables)
        check('stamp_missing', False, 'no error for missing csect')
    except mmbstamp.StampError:
        check('stamp_missing', True)


def run():
    logging.disable(logging.WARNING)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # phase 1: csect A hw 0x80..0x83, csect C hw 0xA0..0xA1, and an
        # imported marker for B (text lives in phase 2)
        makePhase(root, 1,
                  [(0x100, b'\x11\x11' * 4), (0x140, b'\xAB\xCD' * 2)],
                  dict(sections=[
                          dict(name='A', address=0x80, size=4, module='A'),
                          dict(name='C', address=0xA0, size=2, module='C'),
                          dict(name='B', address=0x90, size=2, module='PH2>')],
                       relocations=[dict(address=0x80, flags=0),
                                    dict(address=0x82, flags=0)],
                       unresolvedRelocations=[
                          dict(imageOffsetHW=0x81, length=2),
                          dict(imageOffsetHW=0x83, length=2)],
                       entryPoint=0x123))
        # phase 2: clobbers A's tail (different bytes), re-supplies C's range
        # under another name with IDENTICAL bytes, and carries B's real text
        makePhase(root, 2,
                  [(0x104, b'\x22\x22' * 2),          # hw 0x82..0x83: differs
                   (0x140, b'\xAB\xCD' * 2),          # hw 0xA0..0xA1: same
                   (0x120, b'\x33\x33' * 2)],         # hw 0x90..0x91: B
                  dict(sections=[
                          dict(name='B', address=0x90, size=2, module='B'),
                          dict(name='CC', address=0xA0, size=2, module='CC')],
                       relocations=[dict(address=0x90, flags=0)]))

        image, owner, info, _ops0 = compose(root, [1, 2])
        check('image_size', len(image) == 2 * MEM_HW, f'{len(image)}')
        check('fill_low', image[0:2] == b'\xC9\xFB', image[0:2].hex())
        check('fill_high',
              image[2 * FILL_SPLIT_HW:2 * FILL_SPLIT_HW + 2] == b'\xC6\xC6',
              image[2 * FILL_SPLIT_HW:2 * FILL_SPLIT_HW + 2].hex())
        check('fill_top', image[-2:] == b'\xC6\xC6', image[-2:].hex())
        check('placement', image[0x100:0x104] == b'\x11\x11\x11\x11',
              image[0x100:0x104].hex())
        check('overlay_wins', image[0x104:0x108] == b'\x22\x22\x22\x22',
              image[0x104:0x108].hex())
        check('owner', (owner[0x80], owner[0x82], owner[0x90], owner[0x7F])
              == (1, 2, 2, 0), f'{owner[0x80]},{owner[0x82]},{owner[0x90]}')

        damaged = reportOverlays(image, owner, info)
        check('collisions', damaged == 1, f'{damaged} != 1 (A only; the '
              'identical-byte C/CC re-supply must not count)')

        sym = unionSym('T', owner, info)
        secs = {s['name']: s for s in sym['sections']}
        check('union_A', secs['A']['module'] == 'A')
        check('union_B_real', secs['B']['module'] == 'B',
              f"imported marker won: {secs['B']}")
        # C's text was fully re-supplied (identical bytes) under CC by the
        # later phase: flight maps list only the overlay winner, so C is
        # shadow-dropped and CC listed
        check('union_shadow', 'C' not in secs and 'CC' in secs,
              f"{sorted(secs)}")
        check('union_count', len(sym['sections']) == 3, f"{sorted(secs)}")
        check('reloc_kept',
              [r['address'] for r in sym['relocations']] == [0x80, 0x90],
              str(sym['relocations']))
        check('unres_kept',
              [r['imageOffsetHW'] for r in sym['unresolvedRelocations']]
              == [0x81], str(sym['unresolvedRelocations']))
        check('entry', sym['entryPoint'] == 0x123, str(sym['entryPoint']))

        # end-to-end CLI: files land in the cmp_bytes phase-dir shape
        out = root / 'T'
        rc = main(['--mmu', str(root), '--config', 'T', '--phases', '1,2',
                   '--con80', str(root), '--out', str(out)])
        check('cli_rc', rc == 0, str(rc))
        check('cli_fcm', (out / 'T.fcm').exists())
        j = json.load(open(out / 'T.sym.json'))
        check('cli_sym', len(j['sections']) == 3 and j['imageSize'] == MEM_HW)
        check('cli_bytes', (out / 'T.fcm').read_bytes() == image,
              'CLI image differs from compose()')

    runMmbstamp()

    for f in _failures:
        print('FAIL', f)
    print(f'{"OK: all" if not _failures else "FAILED:"} {_passes} mmu2fcm '
          f'checks' + (f'; {len(_failures)} failed' if _failures else ' passed'))
    return 1 if _failures else 0


if __name__ == '__main__':
    sys.exit(run())
