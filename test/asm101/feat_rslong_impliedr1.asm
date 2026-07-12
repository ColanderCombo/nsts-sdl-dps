* Exercises the RS LONG forms (extended AM=0 and indexed AM=1) of the IMPLIED-R1
* SRS ops -- ZH/TD/TH/SHW.  These bake R1 into the descriptor's fixed opcode bits
* (no x field) and carry a d/b layout, so their RS long forms are sentinel
* promotions of the single SRS descriptor (d=0x3c extended / 0x3d indexed), exactly
* like A/L/ST -- NO separate /X descriptor is needed.  Two latent bugs lived here
* until 2026-06-22: (1) has_rs_descriptor required an x field, so ZH/SHW RS-long
* CRASHED (generateRS0/RS1 ValueError); (2) TD/TH were missing from model101's
* implied-R1 list, so they emitted ZERO for EVERY form (short and long) -- a bug the
* feat_srsshort golden had captured.  Byte values below are FLIGHT-CONFIRMED against
* PASS MAFGEN (../pfs/PFS/mafgen/DASS_G16.ASC) where cited, else descriptor-derived.
RSIMPR1  CSECT
* --- extended (AM=0): large direct displacement forces base-3 IC-relative ---
         ZH    X'D5FA'                 FLIGHT: A1F3 D5FA (CGV3RTL)
         TH    X'C037'                 FLIGHT: A3F3 C037 (CGTCUPL)
         SHW   X'D5FA'                 desc:   A2F3 D5FA
* --- extended (AM=0): explicit base 0-3 ---
         TD    X'0048'(1)              FLIGHT: A0F1 0048 (0GEHRTL)
         ZH    5000(2)                 desc:   A1F2 1388
         SHW   5000(2)                 desc:   A2F2 1388
* --- indexed (AM=1): explicit index register ---
         TD    X'0017'(7,1)            FLIGHT: A0F5 E017 (CGNIMEA)
         ZH    8(7,2)                  desc:   A1F6 E008
         TH    8(7,2)                  desc:   A3F6 E008
         SHW   8(7,2)                  desc:   A2F6 E008
         END
