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
from ap101Utils import g3dat, mmbstamp                       # noqa: E402
from ap101Utils.fcmImage import FcmImage                      # noqa: E402

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
    """mmbstamp unit checks: MMU address decode, MM packing (phase-shaped
    load-block length fixtures -- SOT placement, track-skip accounting,
    multi-track flag), load-block derivation from a synthetic
    .lib (pool cursor + re-supply drop, checksum-slot merge, 16K merge cap,
    bank split, protection split, HIMEM drop), and the stamp() overlay."""
    # -- ALLOC ADDR=FTSBB decode -------------------------------------------
    check('mm16_g9r', mmbstamp.mm16('56000') == 0x2E00)
    check('mm16_mfb', mmbstamp.mm16('33600') == 0x1BC0)
    check('mm16_ipl3', mmbstamp.mm16('03530') == 0x03BE)
    try:
        mmbstamp.mm16('38000')          # subfile 8 out of range
        check('mm16_range', False, 'no error for subfile 8')
    except mmbstamp.StampError:
        check('mm16_range', True)

    # -- MM packing: a 28-block phase allocated at 0x2C00 ------------------
    # SOT falls on LB20 alone; NUM_CONT = 404 data + 10 skipped = 414, and
    # the run crosses a track, so the multi-track flag is set
    ph4 = [626, 94, 82, 3214, 6234, 7088, 16024, 1876, 16382, 16138, 2306,
           3056, 2698, 2278, 16382, 16352, 478, 766, 9662, 16378, 1766,
           4246, 12142, 14170, 8090, 16350, 4598, 1682]
    lbs = [mmbstamp.LoadBlock(0, ln, False) for ln in ph4]
    ncont, crossed = mmbstamp.pack_mm(lbs, 0x2C00)
    check('pack_ph4_ncont', (ncont, crossed) == (414, True),
          f'{ncont},{crossed}')
    check('pack_ph4_sot', [i for i, lb in enumerate(lbs) if lb.sot] == [19],
          str([i for i, lb in enumerate(lbs) if lb.sot]))
    # a 29-block phase at 0x2905: SOT on LB23, NUM_CONT 292
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

    # -- the fullword pad after an odd-length csect is not load-module text
    # (the linker leaves a hole), but it is inside one checksum group, so
    # the derivation must coalesce it before the 16384-hw cap is applied
    libp = LibModule(name='PHASE95')
    libp.extents = [Extent(2 * 0x1000, b'\xC1' * 2 * 0x0B),   # 11 hw: odd
                    Extent(2 * 0x100C, b'\xC2' * 2 * 0x10)]   # after the pad
    lbsp = mmbstamp.derive_load_blocks(libp, 0, lambda hw: False, False)
    check('lb_odd_pad', [(lb.start, lb.length) for lb in lbsp]
          == [(0x1000, 0x1E)],          # 0x1000..0x101C incl. the checksum
          str([(hex(lb.start), hex(lb.length)) for lb in lbsp]))

    # -- FillRule: the LE's pad past an auto-placed group tail ---------------
    lib3 = LibModule(name='PHASE97')
    lib3.extents = [
        Extent(2 * 0x1000, b'\xA1' * 2 * 0x10),   # tail LIBDATA: auto-placed
        Extent(2 * 0x1800, b'\xA2' * 2 * 0x10),   # pinned patch csect
        Extent(2 * 0x2000, b'\xA3' * 2 * 0x10),   # tail INSERTED: no pad
        Extent(2 * 0x5000, b'\xA4' * 2 * 0x10),   # pinned, > 2048 hw away
    ]
    fill = mmbstamp.FillRule(
        sections=[(0x1000, 0x1010, 'LIBDATA'), (0x1800, 0x1810, '$Y097001'),
                  (0x2000, 0x2010, 'INSERTED'), (0x5000, 0x5010, '$Y097002')],
        deck_placed=frozenset({'INSERTED'}),
        origins=frozenset({0x1800, 0x5000}))
    lbs3 = mmbstamp.derive_load_blocks(lib3, 0, lambda hw: False, False,
                                       fill=fill)
    check('lb_fill', [(lb.start, lb.length) for lb in lbs3] == [
        (0x1000, 0x812),      # pad reaches the pinned csect: one group
        (0x2000, 0x12),       # deck-INSERTed tail: no pad
        (0x5000, 0x12),
    ], str([(hex(lb.start), hex(lb.length)) for lb in lbs3]))
    # move the patch csect past pad range: the run before it now pads 2048 hw
    # and closes on its own checksum, and the 0x1000 run loses its pad (the
    # next content of ours is the INSERTed csect, which pins nothing)
    lib3.extents[1] = Extent(2 * 0x2800, b'\xA2' * 2 * 0x10)
    fill.sections[1] = (0x2800, 0x2810, '$Y097001')
    fill.origins = frozenset({0x2800, 0x5000})
    lbs4 = mmbstamp.derive_load_blocks(lib3, 0, lambda hw: False, False,
                                       fill=fill)
    check('lb_fill_pad', [(lb.start, lb.length) for lb in lbs4] == [
        (0x1000, 0x12), (0x2000, 0x12),
        (0x2800, mmbstamp.FILL_PAD_HW + 0x12), (0x5000, 0x12),
    ], str([(hex(lb.start), hex(lb.length)) for lb in lbs4]))

    # -- the merge cap counts the pad the merged block will then carry.
    # A group ending 250 hw below the cap cannot absorb a next unit whose
    # tail pads 2048 hw: the LB would run to 0x5202, well past 16384.
    libc = LibModule(name='PHASE93')
    libc.extents = [Extent(2 * 0x1000, b'\xE1' * 2 * 0x3F00),  # 16128 hw
                    Extent(2 * 0x4F02, b'\xE2' * 2 * 0x10),    # auto-placed
                    Extent(2 * 0x5800, b'\xE3' * 2 * 0x10)]    # pinned
    fillc = mmbstamp.FillRule(
        sections=[(0x1000, 0x4F00, 'BULK'), (0x4F02, 0x4F12, 'LIBDATA'),
                  (0x5800, 0x5810, '$Y093001')],
        deck_placed=frozenset({'BULK'}), origins=frozenset({0x5800}))
    lbsc = mmbstamp.derive_load_blocks(libc, 0, lambda hw: False, False,
                                       fill=fillc)
    check('lb_cap_counts_pad', [(lb.start, lb.length) for lb in lbsc] == [
        (0x1000, 0x3F02),                     # closes on its own checksum
        (0x4F02, 0x10 + mmbstamp.FILL_PAD_HW + 2),
        (0x5800, 0x12),
    ], str([(hex(lb.start), hex(lb.length)) for lb in lbsc]))

    # -- a bank's last block backs up over free memory in 1024-hw steps
    # until the remainder fits one 2048-hw load block
    libt = LibModule(name='PHASE94')
    libt.extents = [Extent(2 * 0x1000, b'\xD1' * 2 * 0x10),      # content
                    Extent(2 * 0x7FEA, b'\xD2' * 2 * 0x14)]      # bank tail
    fillt = mmbstamp.FillRule(
        sections=[(0x1000, 0x1010, 'CONTENT'), (0x7FEA, 0x7FFE, '$Y094001')],
        deck_placed=frozenset({'CONTENT'}), origins=frozenset({0x7FEA}))
    lbst = mmbstamp.derive_load_blocks(libt, 0, lambda hw: False, False,
                                       fill=fillt)
    # avail = 0x8000 - 0x1012 = 28654; k minimal with the remainder <= 2048
    # is ceil((28654 - 2048) / 1024) = 26, so the block backs up to
    # 0x1012 + 26*1024 = 0x7812 and runs 2030 hw to the bank end
    tail = lbst[-1]
    check('lb_bank_tail_step',
          (tail.start, tail.length) == (0x7812, 0x8000 - 0x7812)
          and tail.length <= mmbstamp.FILL_PAD_HW
          and (tail.start - 0x1012) % mmbstamp.BANK_TAIL_STEP == 0,
          f'{hex(tail.start)} len {tail.length}')

    # -- a MAP-carded parent's longer csect at the same start keeps the
    # region's tail allocated, so the bank tail backs up over less of it.
    # Same geometry as above, but a parent phase filled the region at
    # 0x1000 out to 0x1810 where this phase's own csect stops at 0x1010.
    libm = LibModule(name='PHASE95')
    libm.extents = [Extent(2 * 0x1000, b'\xD1' * 2 * 0x10),
                    Extent(2 * 0x7FEA, b'\xD2' * 2 * 0x14)]
    fillm = mmbstamp.FillRule(
        sections=[(0x1000, 0x1010, 'CONTENT'), (0x7FEA, 0x7FFE, '$Y095001')],
        deck_placed=frozenset({'CONTENT'}), origins=frozenset({0x7FEA}),
        mapped={0x1000: 0x1810})
    lbsm = mmbstamp.derive_load_blocks(libm, 0, lambda hw: False, False,
                                       fill=fillm)
    # floor 0x1812 instead of 0x1012; avail = 0x8000 - 0x1812 = 26606, so
    # k = ceil((26606 - 2048) / 1024) = 24 and the tail starts 0x1812 + 24*1024
    tailm = lbsm[-1]
    check('lb_bank_tail_map_extent',
          (tailm.start, tailm.length) == (0x7812, 0x8000 - 0x7812)
          and (tailm.start - 0x1812) % mmbstamp.BANK_TAIL_STEP == 0,
          f'{hex(tailm.start)} len {tailm.length}')
    # and the mapped extent must not be visible where the phase does not
    # itself occupy the start: an unrelated address is left alone
    check('lb_map_extent_scoped',
          fillm.occupied_below(0x7FEA, 0) == 0x1812
          and mmbstamp.FillRule(
              sections=fillm.sections, deck_placed=fillm.deck_placed,
              origins=fillm.origins).occupied_below(0x7FEA, 0) == 0x1012)

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


def runG3dat():
    """g3dat unit checks: upper-memory module placement, the >>4 descriptor
    re-encoding, the destination-sector split, the entry cap, and the
    stamp() overlay."""
    # -- placement: DAT/RTV/STR stacked down from the top of the sector ------
    at = g3dat.place_modules({'FCMG3DAT': 161, 'FCMG3RTV': 152,
                              'FCMG3STR': 143})
    check('g3_place', (at['FCMG3DAT'], at['FCMG3RTV'], at['FCMG3STR'])
          == (0xFF5F, 0xFEC7, 0xFE38),
          ' '.join(f'{at[n]:04X}' for n in g3dat.MODULES))
    try:
        g3dat.place_modules({n: 0x4000 for n in g3dat.MODULES})
        check('g3_place_fit', False, 'no error when the modules overflow')
    except g3dat.G3DatError:
        check('g3_place_fit', True)

    # -- descriptor re-encoding: the DAT flag halfword is the phase
    # table descriptor's >> 4 ----------------------------------------------
    lb = mmbstamp.LoadBlock(4 * mmbstamp.BANK_HW + 0x22, 8, True)
    lb.sot = True
    check('g3_flags_gpt', lb.words()[1] == 0xC640, f'{lb.words()[1]:04X}')
    ents, _ = g3dat.split_for_destination([lb], 15 * g3dat.SECTOR_HW + 0x7E38)
    check('g3_flags_dat', ents == [(0x8022, 0x0C64, 8)], str(ents))

    # -- destination split: a phase-shaped run of LB lengths, archive top
    # FE38 -----------------------------------------------------------------
    ph6 = [542, 94, 82, 1906, 1306, 6234, 4958, 16052, 2058, 1920, 16380,
           16140, 2156, 3210, 2706, 2278, 16382, 16352, 490, 754, 9204,
           16382, 2220, 3738, 12634, 7220, 1830, 7534, 1682]
    lbs = [mmbstamp.LoadBlock((i % 8) * mmbstamp.BANK_HW, ln, False)
           for i, ln in enumerate(ph6)]
    ents, bottom = g3dat.split_for_destination(
        lbs, 15 * g3dat.SECTOR_HW + 0x7E38)
    lens = [e[2] for e in ents]
    check('g3_split_count', len(ents) == 34, str(len(ents)))
    check('g3_split_total', sum(lens) == sum(ph6) == 174444, str(sum(lens)))
    check('g3_split_bottom', bottom == 0x0554CC, f'{bottom:#07x}')
    # the five LBs that straddle a 32K destination boundary, in order
    check('g3_split_pieces',
          [(lens[i], lens[i + 1]) for i in (8, 12, 19, 24, 29)]
          == [(1138, 920), (13548, 2592), (3444, 12908), (9412, 6970),
              (7206, 14)],
          str([(lens[i], lens[i + 1]) for i in (8, 12, 19, 24, 29)]))
    # a split piece keeps its parent's flags and continues at addr+take
    a0, f0, l0 = ents[8]
    a1, f1, _ = ents[9]
    check('g3_split_carry', (f1 == f0 and a1 == a0 + l0),
          f'{a0:04X}+{l0}→{a1:04X} {f0:04X}/{f1:04X}')
    # a destination that starts sector-aligned gets a whole sector of room,
    # not zero (the fixture above only ever arrives at a boundary)
    one = [mmbstamp.LoadBlock(mmbstamp.BANK_HW, 0x8001, False)]
    ents, _ = g3dat.split_for_destination(one, 15 * g3dat.SECTOR_HW)
    check('g3_split_aligned', [e[2] for e in ents] == [0x8000, 1],
          str([hex(e[2]) for e in ents]))
    # running the archive off the bottom of memory is an error, not a hang
    try:
        g3dat.split_for_destination(
            [mmbstamp.LoadBlock(0, 16000, False) for _ in range(40)],
            15 * g3dat.SECTOR_HW + 0x7E38)
        check('g3_split_underrun', False, 'no error for an oversized archive')
    except g3dat.G3DatError:
        check('g3_split_underrun', True)

    # -- stamp(): overlay at the composed csect address ---------------------
    dat = g3dat.G3Dat(words=list(range(g3dat.G3DAT_SIZE)))
    img = bytes(2 * 0x3000)
    sym = {'sections': [{'name': g3dat.G3DAT_CSECT, 'address': 0x1000,
                         'size': g3dat.G3DAT_SIZE}]}
    out = g3dat.stamp(img, sym, dat)
    check('g3_stamp', out[2 * 0x1000:2 * 0x1000 + 6]
          == b'\x00\x00\x00\x01\x00\x02')
    check('g3_stamp_len', len(out) == len(img))
    try:
        g3dat.stamp(img, {'sections': []}, dat)
        check('g3_stamp_missing', False, 'no error for missing csect')
    except g3dat.G3DatError:
        check('g3_stamp_missing', True)
    try:
        g3dat.stamp(img, {'sections': [
            {'name': g3dat.G3DAT_CSECT, 'address': 0x1000, 'size': 4}]}, dat)
        check('g3_stamp_short', False, 'no error for an undersized csect')
    except g3dat.G3DatError:
        check('g3_stamp_short', True)


def runTapeText():
    """mmbstamp.stack_holes / tape_text: an auto-generated @-stack is
    allocation, not content.

    The linker reserves the space and supplies no tape text, so the MMB
    staging buffer's fill is what the load block carries there and every
    block sum has to use it -- ours emits a zero-filled text extent
    instead.  All names synthetic."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # one 8-hw csect of real text, then an 8-hw generated stack, then
        # 4 more halfwords of real text, all in one extent
        makePhase(root, 42, [(2 * 0x100, b'\x01\x02' * 20)], dict(sections=[
            {'name': '#DAAAAAAA', 'address': 0x100, 'size': 8,
             'module': 'AAAAAAAA'},
            {'name': '@0AAAAAAA', 'address': 0x108, 'size': 8,
             'module': '<generated-stacks>'},
            {'name': '#EAAAAAAA', 'address': 0x110, 'size': 4,
             'module': 'AAAAAAAA'},
        ]))
        sym = json.load(open(root / 'PHASE42' / 'PHASE42.sym.json'))
        check('stack_holes', mmbstamp.stack_holes(sym) == [(0x108, 0x110)],
              str(mmbstamp.stack_holes(sym)))
        t = mmbstamp.tape_text(root, 42)
        check('tape_text_len', len(t) == 12, str(len(t)))
        check('tape_text_hole',
              all(h not in t for h in range(0x108, 0x110)), 'stack has text')
        check('tape_text_keeps',
              t.get(0x100) == 0x0102 and t.get(0x110) == 0x0102, str(t))
        # the point of the hole: a sum over the block reads FILL there
        s = sum(t.get(h, mmbstamp.FILL) for h in range(0x100, 0x114)) & 0xFFFF
        check('tape_text_sum', s == (12 * 0x0102 + 8 * mmbstamp.FILL) & 0xFFFF,
              hex(s))


def runChecksumSlots():
    """fcmImage._checksum_slots: survival in memory (the `owner` test) and
    appearance in the listing (MCF membership) are separate questions.

    A memory configuration is defined by the MCF as a phase subset.  Every
    phase the loader places is really in memory, checksum slots included,
    but a phase outside the subset is not part of the config's load module,
    so its blocks' slots must not reach the map -- the same verdict
    mmu2fcm.unionSym records as `residue` on that phase's csects.  Synthetic
    load orders and addresses throughout."""
    img = FcmImage()
    img.mem_hw = 0x400
    owner = bytearray(img.mem_hw)          # nobody wrote anything

    # load orders 1..3; the MCF lists 1 and 3 but not 2
    listed = {1, 3}
    lb = [(1, 0x100), (2, 0x120), (3, 0x140)]
    got = img._checksum_slots([], owner, lb, listed)
    check('cks_mcf_keeps', 0x100 in got and 0x140 in got, str(got))
    check('cks_mcf_drops', 0x120 not in got, str(got))

    # a slot two phases produce is listed as long as one is an MCF phase
    got = img._checksum_slots([], owner, [(2, 0x200), (3, 0x200)], listed)
    check('cks_mcf_shared', got == [0x200], str(got))
    got = img._checksum_slots([], owner, [(2, 0x200)], listed)
    check('cks_mcf_unshared', got == [], str(got))

    # listed_ord=None -> historic behaviour, every order contributes
    got = img._checksum_slots([], owner, lb, None)
    check('cks_mcf_none', got == [0x100, 0x120, 0x140], str(got))

    # the owner test is unchanged and still independent: a later phase's
    # text over an MCF phase's slot suppresses it
    owner2 = bytearray(img.mem_hw)
    owner2[0x100:0x102] = b'\x02\x02'      # order 2 wrote over order 1's slot
    got = img._checksum_slots([], owner2, [(1, 0x100)], listed)
    check('cks_owner_still', got == [], str(got))

    # the block-per-extent half honours the same rule (content end 0x181
    # rounds up to the fullword-aligned slot at 0x182)
    got = img._checksum_slots([(2, 0x181)], owner, [], listed)
    check('cks_extent_mcf', got == [], str(got))
    got = img._checksum_slots([(3, 0x181)], owner, [], listed)
    check('cks_extent_keeps', got == [0x182], str(got))


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
        # later phase; only the overlay winner is listed, so C is
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
    runG3dat()
    runTapeText()
    runChecksumSlots()

    for f in _failures:
        print('FAIL', f)
    print(f'{"OK: all" if not _failures else "FAILED:"} {_passes} mmu2fcm '
          f'checks' + (f'; {len(_failures)} failed' if _failures else ' passed'))
    return 1 if _failures else 0


if __name__ == '__main__':
    sys.exit(run())
