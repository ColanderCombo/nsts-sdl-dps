#!/usr/bin/env python3
#
# Differential test: the descriptor-driven CPU encoder vs REAL HAL/S-FC object
# code from the flight-software archive.
#
# Each vector is a real instruction taken from the flight-software archive -- the
# BFS COMPILED listings (pfs/PFS/BFS.SRC as received/COMPILED) and the PFS MAFGEN
# disassembly (mafgen/*.ASC) -- object code emitted by the original HAL/S-FC
# toolchain, NOT by asm101.  We assert that encoding the instruction's
# fields through the auto-extracted GPC descriptors (instrdefs: CPU.encode for
# the 16-bit forms, rs_hw1 + the rs_ai second halfword for the RS long forms)
# reproduces the archived bytes exactly.  This pins the descriptor scheme to an
# independent ground truth and would catch a descriptor or sentinel-scheme error
# that the byte-golden test (which only re-checks asm101 against itself) cannot.
#
# This validates ENCODING given the operand fields; it deliberately does not go
# through the assembler's form-selection (SRS-vs-RS, condensation), which needs
# the surrounding USING/address context the bare fields don't carry.

import sys

from asm101.instrdefs import CPU, rs_hw1
from asm101 import iop_instr as iop


def enc16(mnem, fields):
  """A 16-bit form (RR/SRS-short/RI-hw1/SI-hw1) -> 4 hex chars."""
  return int.from_bytes(CPU.encode(mnem, fields), "big")


def enc_iop(mnem, fields):
  """An IOP (MSC/BCE) descriptor form -> int (2- or 4-byte word)."""
  return int.from_bytes(iop.IOP.encode(mnem, fields), "big")


def enc_rs(mnem, r1, b2, x2, d2, indexed):
  """An RS long form -> 8 hex chars: descriptor first halfword (sentinel
  scheme) + the second halfword (16-bit displacement, or index<<13 | disp)."""
  hw1 = int.from_bytes(rs_hw1(mnem, r1, b2, indexed), "big")
  hw2 = ((x2 << 13) | d2) if indexed else d2
  return (hw1 << 16) | hw2


# (description, computed value, expected) from BFS.SRC/COMPILED.
def vectors():
  v = []
  # --- RS extended (AM=0): hw1 (sentinel d=0x3c) + 16-bit displacement ---
  for mnem, r1, b2, d2, want, prov in [
      ("L",  5, 1, 308, 0x1DF10134, "10D05  L  R5,308(R1)"),
      ("L",  7, 0, 212, 0x1FF000D4, "16E78  L  R7,212(R0)"),
      ("LA", 2, 1, 265, 0xEAF10109, "10DBF  LA R2,265(R1)"),
      ("LH", 5, 1,  72, 0x9DF10048, "10D16  LH R5,72(R1)"),
      ("ST", 7, 0, 212, 0x37F000D4, "16E6C  ST R7,212(R0)"),
      # IAL uses the 0x3e extended sentinel (not 0x3c) -- MAFGEN DASS_G16:
      ("IAL", 0, 3, 0x00DC, 0xE0FB00DC, "MAFGEN #CGV7RTL IAL R0,X'00DC'"),
      ("IAL", 3, 3, 0x0000, 0xE3FB0000, "MAFGEN MM6DN   IAL R3,0"),
      ("IAL", 2, 3, 0x0001, 0xE2FB0001, "MAFGEN #CGV7RTL IAL R2,1"),
  ]:
    v.append((f"ext {prov}", enc_rs(mnem, r1, b2, 0, d2, False), want))
  # --- RS no-R1 system ops (AM=0): take an ADDRESS operand, with R1 a FIXED part
  # of the opcode (LM=4, STM=0, LDM=0, SVC=1, LPS=5).  Confirms the descriptors
  # bake the flight R1 (the old variable-R1 path emitted zero for LM/STM and
  # collided SVC with STM).  All from PASS MAFGEN (mafgen/DASS_G16.ASC). ---
  for mnem, d2, want, prov in [
      ("LM",  0x9566, 0xCCFB9566, "FCMASYNC+0080 LM  X'9566'"),
      ("STM", 0x968E, 0xC8FB968E, "FCMASYNC+0065 STM X'968E'"),
      ("LDM", 0x0520, 0x68FB0520, "$0GEHRTL+0009 LDM X'0520'"),
      ("SVC", 0x9DEA, 0xC9FB9DEA, "#0ROUND+0020  SVC X'9DEA'"),
      ("LPS", 0x0078, 0xCDFB0078, "FIOERRLA+0023 LPS X'0078'"),
  ]:
    v.append((f"sys {prov}", enc_rs(mnem, 0, 3, 0, d2, False), want))
  # --- RS indexed (AM=1): hw1 (sentinel d=0x3d) + index<<13 | displacement ---
  for mnem, r1, b2, x2, d2, want, prov in [
      ("LH", 7, 2, 4, 712, 0x9FF682C8, "3F338  LH R7,712(R4,R2)"),
      ("LA", 7, 2, 7,   8, 0xEFF6E008, "3F372  LA R7,8(R7,R2)"),
      ("LH", 2, 2, 6,   0, 0x9AF6C000, "10DC3  LH R2,0(R6,R2)"),
      ("LE", 2, 0, 7,  92, 0x7AF4E05C, "16F11  LE F2,92(R7,R0)"),
      ("ME", 0, 0, 7,  74, 0x60F4E04A, "16F0F  ME F0,74(R7,R0)"),
  ]:
    v.append((f"idx {prov}", enc_rs(mnem, r1, b2, x2, d2, True), want))
  # --- RR (2-byte, two register fields x=R1, y=R2) ---
  for mnem, r1, r2, want, prov in [
      ("PC", 4, 4, 0xDCEC, "STARTUP PC G4,G4 (the unification-caught bug)"),
      ("PC", 7, 4, 0xDFEC, "STARTUP PC G7,G4"),
      ("PC", 1, 1, 0xD9E9, "STARTUP PC G1,G1"),
      ("NR", 5, 6, 0x25E6, "3F279  NR R5,R6"),
      ("CR", 5, 6, 0x15E6, "3F27C  CR R5,R6"),
      ("STXAR", 3, 3, 0xA3EB, "MAFGEN DASS_G16 FIOSVCP STXAR R3,R3"),
  ]:
    v.append((f"rr  {prov}", enc16(mnem, {"x": r1, "y": r2}), want))
  # --- SRS short (2-byte, x/d/b; d is the ENCODED 6-bit displacement) ---
  for mnem, r1, d, b2, want, prov in [
      ("STH", 1, 5, 0, 0xB914, "3F264  STH R1,5(R0)"),
      ("LH",  5, 4, 3, 0x9D13, "3F273  LH R5,4(R3)"),
      ("N",   6, 2, 1, 0x2609, "3F278  N R6,4(R1)"),
  ]:
    v.append((f"srs {prov}", enc16(mnem, {"x": r1, "d": d, "b": b2}), want))
  # --- SRS branch forward form (BVCF): x=mask, d=PC-relative disp, form fixed
  # at 01 in the descriptor.  Confirms BVCF is form-01 (the legacy direct path
  # mis-encoded it as form-11 = BCTB). ---
  for mnem, mask, d, want, prov in [
      ("BVCF", 6, 38, 0xDE99, "BFS_OI_BUILD.ASC:2106 BVCF 6,*+39 (LBL#19)"),
  ]:
    v.append((f"srsb {prov}", enc16(mnem, {"x": mask, "d": d}), want))
  # --- RI (4-byte: descriptor first halfword, y=register; + immediate) ---
  for mnem, r2, imm, want, prov in [
      ("AHI", 5, 0x00A0, 0xB0E500A0, "3F36D  AHI R5,160"),
      ("AHI", 7, 0x0001, 0xB0E70001, "16F22  AHI R7,1"),
      ("CHI", 5, 0x0004, 0xB5E50004, "17001  CHI R5,4"),
  ]:
    got = (enc16(mnem, {"y": r2}) << 16) | imm
    v.append((f"ri  {prov}", got, want))
  # --- SI (4-byte: descriptor first halfword, d/b; + immediate) ---
  for mnem, d, b2, imm, want, prov in [
      ("SB",   3, 3, 0x0001, 0xB20F0001, "3F342  SB ..,X'27' (d=3,b=3)"),
      ("MSTH", 29, 0, 0x0001, 0xB0740001, "16F5D  MSTH ..,X'1D' (d=29,b=0)"),
  ]:
    got = (enc16(mnem, {"d": d, "b": b2}) << 16) | imm
    v.append((f"si  {prov}", got, want))
  # --- IOP BCE delay family (PFS MAFGEN DASS_G16, FCMMGBOV skeleton words; the
  # disassembler flags them ILLEGAL OP CODE but still shows the assembled word).
  # These pin the descriptor opcode + field placement to flight bytes; #DLY's
  # PC-relative 'd' computation is pinned separately in test_asm101_features
  # (FCMGNDLC #DLY FCMGNDLC+1 -> C800, d=0). ---
  for mnem, fields, want, prov in [
      ("#DLY",  {"d": 0},  0xC800, "FCMMGBOV+0307 FCMGNDLC #DLY (d=0)"),
      ("#DLYI", {"i": 0},  0xC000, "FCMMGBOV+0304 FCMGRDLZ #DLYI 0"),
      ("#DLYI", {"i": 12}, 0xC00C, "FCMMGBOV+0306 FCMGTDLL #DLYI 12 (FIODELAY)"),
  ]:
    v.append((f"iop {prov}", enc_iop(mnem, fields), want))
  # --- IOP BCE short memory-reference ops: PC-relative 'd' (the raw field value,
  # already = target-(IC+1) in the archive) and the 'm' index bit.  These pin the
  # descriptor bit-placement to flight bytes; the assembler's PC-relative 'd' + 'm'
  # extraction is pinned separately in test_asm101_features. ---
  for mnem, fields, want, prov in [
      ("#WIX", {"d": 0x534},        0x2534, "BFS DKWIX #WIX (d=-0x2CC)"),
      ("#SST", {"m": 1, "d": 0x7FB}, 0x5FFB, "FIOMMUPG #SST FIOSSBCE(1) (d=-5)"),
      ("#SSC", {"m": 1, "d": 0x02C}, 0x482C, "BILDNEW5 #SSC KFSTAT(1) (d=0x2C)"),
      ("#SSC", {"m": 0, "d": 0x05E}, 0x405E, "FCMBOOT #SSC FCMBREAA+.. (d=0x5E)"),
      ("#LTO", {"d": 0},            0xB800, "BILDNEW5 #LTO TIMOUT-36 (d=0)"),
  ]:
    v.append((f"iop {prov}", enc_iop(mnem, fields), want))
  return v


def main() -> int:
  failures = []
  for desc, got, want in vectors():
    if got != want:
      failures.append(f"{desc}: encoded {got:#06x} != archive {want:#06x}")
  if failures:
    print(f"FAILED {len(failures)} compiled-vector check(s):", file=sys.stderr)
    for f in failures:
      print(f"  - {f}", file=sys.stderr)
    return 1
  print(f"OK: all {len(vectors())} descriptor encodings match real BFS "
        f"COMPILED object code")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
