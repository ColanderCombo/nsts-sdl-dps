#!/usr/bin/env python3
#
# Compute csect memory placement from a CON80 layout program.
# 
#   * addr OVERLAY name -> begin a region named <name> 
#                          at absolute halfword <addr>
#         e.g.: 00000 OVERLAY PSA -> 0
#               001A8 OVERLAY Z1 -> 424
#                     OVERLAY NOADDR -> keeps LC, placed after last section
# 
#   *      BANK n -> IF it changes the current bank, 
#                    default the counter to n * 0x8000 
#   * addr INSERT csect` -> place csect AND associated data csects of
#                           its owning module 
#                               - In module order
#                               - Fullword aligned, advancing the counter.  
#                               
#                           No csect is placed twice; subsequent INSERTS
#                           after the first are ignored.
#                           <addr> field will force the csect to that location.
#   * SET / CLEAR -> toggle the per-halfword write-protection bit for
#                           subsequently placed csects 
#   * STACK csect -> stack allocation for a module's auto storage; s
#   * PHASE list -> begins a phase: a separately MMU-stored segment overlaid
#                   into memory at runtime.  
#
# The csect type code carries the underlying placement constraint:
#   CODE ($0 etc.) in sector >= 2 
#   DATA (#D #0 #P #E #L #X) in sectors 0-1, 
#   ZCON (#Z #Q) in the first 2K of sector 0.
#
from __future__ import annotations

from dataclasses import dataclass, field

BANK_SIZE_HW = 0x8000 # == 32K HW's, 64KB


def align_word(hw: int) -> int:
    return (hw + 1) & ~1


@dataclass
class Placement:
    addresses: dict[str, int] = field(default_factory=dict)   # csect -> start (hw)
    bank_of: dict[str, int] = field(default_factory=dict)
    overlay_of: dict[str, str] = field(default_factory=dict)
    phase_of: dict[str, str] = field(default_factory=dict)     # csect -> phase list
    protected: dict[str, bool] = field(default_factory=dict)   # csect -> write-protected
    unknown_inserts: list[str] = field(default_factory=list)   # INSERTs w/o a module
    log: list[str] = field(default_factory=list)


class LayoutEngine:
    """Replays a layout_program into a Placement"""

    def __init__(self, module_csects: dict[str, list[tuple[str, int]]],
                 *, align: bool = True):
        self.module_csects = module_csects
        self.align = align

    def run(self, program) -> Placement:
        out = Placement()
        lc = 0
        cur_bank: int | None = None
        cur_overlay: str | None = None
        cur_phase: str | None = None
        protected = True            # code is write-protected unless CLEARed

        for op in program:
            verb = op.verb
            if verb == "PHASE":
                cur_phase = op.operand

            elif verb == "BANK":
                try:
                    n = int(op.operand)
                except ValueError:
                    out.log.append(f"BANK with non-numeric operand {op.operand!r}")
                    continue
                if n != cur_bank:
                    cur_bank = n
                    lc = n * BANK_SIZE_HW

            elif verb == "OVERLAY":
                cur_overlay = op.operand or cur_overlay
                if op.origin is not None:          # absolute load address
                    lc = op.origin

            elif verb == "INSERT":
                if op.origin is not None:          # location field pins the address
                    lc = op.origin
                sizes = self.module_csects.get(op.operand)
                size = dict(sizes).get(op.operand) if sizes else None
                if size is None:
                    out.unknown_inserts.append(op.operand)
                    continue
                # Place ONLY the named csect: its assembled data lives within its
                # own SD, and a module's other SD csects are placed by their own
                # INSERTs
                name = op.operand
                if name not in out.addresses:
                    if self.align:
                        lc = align_word(lc)
                    out.addresses[name] = lc
                    out.bank_of[name] = cur_bank
                    out.overlay_of[name] = cur_overlay
                    out.phase_of[name] = cur_phase
                    out.protected[name] = protected
                    lc += size

            elif verb == "SET":
                protected = True       # write-protect subsequent csects

            elif verb == "CLEAR":
                protected = False      # CLR overlays hold UNPROTECTED things

            elif verb == "STACK":
                # Stack allocation for a module's auto storage; sizing TBD.
                out.log.append(f"STACK {op.operand} (unhandled)")

        return out
