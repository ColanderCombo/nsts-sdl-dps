"""Link DEUCFLM.

deucflm consumes the OBJECT MODULES a previous halsc step compiled from the
CRTFMT compools.  
"""
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..", "pfs", "code", "OI340600")


def make_obj(csect, words, path):
    from ap101Utils import objModule
    img = struct.pack(">%dH" % len(words), *(w & 0xFFFF for w in words))
    mod = objModule.Module.from_assembly(
        sections=[(csect, 0, 0, img)], externs=[], entries=[])
    path.write_bytes(mod.to_bytes())


def body_words(member):
    from dfg.deck import find_deck
    from dfg.encode import encode
    name = member[2:]                            # '#PXG3011' -> 'XG3011'
    if name == "XD0000":
        src = open(find_deck(name, ROOT)).read()
        return [int(t, 16) for t in re.findall(r"HEX'([0-9A-F]{1,4})'", src)]
    return encode(name).image()


def deuloc_mismatches(slots, params):
    """Each member's C-display counterpart must carry DEULOC = the member's
    CFIT slot address."""
    from dfg.deck import encodable_directives, find_deck
    origin = params["ORIGIN"]
    out = []
    from dfg.deucflm import SPARE
    for i, member in enumerate(slots):
        if member == SPARE:
            continue
        disp = "C" + member[3:]                  # '#PXG3011' -> 'CG3011'
        path = find_deck(disp, ROOT)
        if path is None:
            continue
        deuloc = next((int(re.sub(r"\D", "", v) or 0)
                       for k, v in encodable_directives(path)
                       if k == "DEULOC" and v), None)
        if deuloc is not None and deuloc != origin + i:
            out.append("%s: DEULOC %d != CFIT slot address %d (%s at slot %d)"
                       % (disp, deuloc, origin + i, member, i))
    return out


def main():
    if not os.path.isdir(ROOT):
        print("SKIP: %s not found (out-of-tree flight sources)" % ROOT)
        return 0
    os.environ["DFG_DECK_ROOT"] = ROOT
    from dfg import deucflm

    slots, params = deucflm.parse_cfsysin(
        os.path.join(ROOT, "CON80", "CFSYSIN"))
    problems = []
    if params.get("CRTFMTLM") != "DEUCFLM":
        problems.append("CRTFMTLM parsed as %r" % params.get("CRTFMTLM"))
    # 20 CRTFMTCU entries (16 members + 4 SPARE), padded with spares to the
    # 32 CFIT slots.
    if len(slots) != 32 or slots.count(deucflm.SPARE) != 16:
        problems.append("CRTFMTCU slots: %d with %d SPARE"
                        % (len(slots), slots.count(deucflm.SPARE)))

    members = list(dict.fromkeys(slots))
    with tempfile.TemporaryDirectory() as objdir:
        objdir = Path(objdir)
        for m in members:
            make_obj(m, body_words(m), objdir / (m[2:] + ".obj"))

        # ld behavior: a missing member object fails the link.
        try:
            deucflm.member_words("#PXG9999", [objdir])
            problems.append("missing XG9999.obj did not raise")
        except FileNotFoundError:
            pass

        bodies = {m: deucflm.member_words(m, [objdir]) for m in members}
    image = deucflm.build(slots, params, bodies)

    origin = params["ORIGIN"]
    cfitsize = params["CFITSIZE"]
    # AIGDEU fills CFBSIZE+1 words (0x100..0xF48): PAD fill + checksum word.
    if len(image) != params["CFBSIZE"] + 1:
        problems.append("image %d != CFBSIZE+1 = %d"
                        % (len(image), params["CFBSIZE"] + 1))
    if image[-1] != sum(image[:-1]) & 0xFFFF:
        problems.append("last word %04X != additive checksum %04X"
                        % (image[-1], sum(image[:-1]) & 0xFFFF))
    if image[-2] != params["PAD"]:
        problems.append("word before checksum %04X != PAD %04X"
                        % (image[-2], params["PAD"]))

    starts = set()
    for i, name in enumerate(slots):
        w = image[i]
        if w >> 12 != 1:
            problems.append("slot %d (%s): %04X is not a branch FCW"
                            % (i, name, w))
            continue
        target = w & 0x0FFF
        starts.add(target)
        if not (origin + cfitsize <= target < origin + len(image)):
            problems.append("slot %d (%s): target %04X outside image"
                            % (i, name, target))
    for i, name in enumerate(slots):
        if name == deucflm.SPARE:
            continue
        # body tail: ...111E 0000 0000 (next body start or image end bounds it)
        target = image[i] & 0x0FFF
        end = min((s for s in starts if s > target),
                  default=origin + len(image))
        tail = image[end - origin - 3:end - origin]
        if tail != [0x111E, 0, 0]:
            problems.append("%s body tail %s != [111E, 0, 0]"
                            % (name, ["%04X" % w for w in tail]))

    problems += ["deuloc: " + m for m in deuloc_mismatches(slots, params)]

    if problems:
        for p in problems:
            print("  " + p)
        return 1
    print("ALL PASS: DEUCFLM %d halfwords, %d slots, %d bodies"
          % (len(image), cfitsize, len(starts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
