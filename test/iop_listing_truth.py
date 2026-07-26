"""Listing-correct MSC/BCE descriptor reference -- the IOP coffee<->listing
drift guard's ground truth.

These descriptor strings are derived from the real RELIST/AP101S object code in
the flight-software listings
    .../pfs/PFS/BFS.SRC as received/COMPILED/{MSC,BCE}
(NOT from the GPC simulator's coffee).  test_asm101_instr_defs compares the
auto-extracted instr_defs.json (which the production asm101.iop_instr now sources
its descriptors from) against this independent reference, so that a coffee
descriptor drifting away from flight-listing truth is caught here.  Field-letter
conventions and per-instruction provenance are documented in asm101.iop_instr.

This is a TEST-ONLY artifact: the assembler encodes from the JSON, never from
this file.  Keep it hand-maintained against the listings, not the coffee.
"""

MSC_DESC = {
    # --- Accumulator / memory (short, PC-relative; "(1)" sets index bit i) ---
    "@L":    "0100iddddddddddd",   # load ACC
    "@A":    "0101iddddddddddd",   # add to ACC
    "@N":    "0110iddddddddddd",   # AND to ACC
    "@X":    "0111iddddddddddd",   # exclusive-OR to ACC
    "@ST":   "1000iddddddddddd",   # store ACC
    "@TSZ":  "1001iddddddddddd",   # tally and skip on zero
    "@REC":  "1010iddddddddddd",   # return from external call

    # --- Branches (short, PC-relative).  @BC/@BXC take an explicit condition;
    #     the mnemonic aliases below bake the condition into ccc. ---
    "@BC":   "00100cccdddddddd",   # branch on accumulator
    "@BXC":  "00101cccdddddddd",   # branch on index

    # --- Repeat (short).  bit4 = index flag; bits5-7 select the repeat type. ---
    "@RAW":  "1101i000dddddddd",   # repeat until all waiting
    "@RNW":  "1101i001dddddddd",   # repeat until any waiting
    "@RAI":  "1101i100dddddddd",   # repeat until all indicators   (tentative)
    "@RNI":  "1101i101dddddddd",   # repeat until any indicator

    # --- Direct register operations (short, OP=1110 I=0, OPX in bits 5-7) ---
    "@LAR":  "11100000dddddddd",   # load ACC with status register
    "@SFD":  "1110000100000000",   # set fail discretes
    "@RFD":  "1110001000000000",   # reset fail discretes
    "@LMS":  "1110001100000000",   # load ACC with MSC status
    "@SIO":  "1110010000000000",   # start I/O
    "@XAX":  "1110010100000000",   # exchange ACC and X
    "@SEC":  "1110011000000000",   # sample for external call   (tentative)
    "@RBI":  "11100111bbbbb000",   # reset BCE indicator (b = BCE number)

    # --- Register immediate (short, OP=1110 I=1, OPX in bits 5-7) ---
    "@NIX":  "11101000iiiiiiii",   # normalize and increment X
    "@TAX":  "11101001iiiiiiii",   # tally ACC to X
    "@TXI":  "11101010iiiiiiii",   # tally X
    "@LXI":  "11101011iiiiiiii",   # load X immediate
    "@TXA":  "11101100iiiiiiii",   # tally X to ACC
    "@TI":   "11101101iiiiiiii",   # tally immediate to ACC
    "@SAI":  "11101110iiiiiiii",   # subtract ACC from immediate
    "@LI":   "11101111iiiiiiii",   # load ACC immediate

    # --- Special (short) ---
    "@WAT":  "0000100000000000",   # wait (0x0800)
    "@DLY":  "1100iddddddddddd",   # delay
    "@INT":  "0011Illlllllllll",   # interrupt CPU (I=OR-with-X mode bit)
    "@STP":  "0001000000000000",   # self test               (tentative)

    # --- Long format (32-bit, absolute addressing; "(1)" sets index bit i) ---
    "@LF":    "1111i100000000aaaaaaaaaaaaaaaaaa",   # load ACC fullword
    "@LH":    "1111i100000010aaaaaaaaaaaaaaaaaa",   # load ACC halfword
    "@STF":   "1111i101000000aaaaaaaaaaaaaaaaaa",   # store ACC fullword
    "@STH":   "1111i101000010aaaaaaaaaaaaaaaaaa",   # store ACC halfword
    "@BU":    "1111i000_____0aaaaaaaaaaaaaaaaaa",   # branch unconditional
    "@BU@":   "1111i000_____1aaaaaaaaaaaaaaaaaa",   # branch unconditional indirect
    "@CALL":  "1111i001ttttt0aaaaaaaaaaaaaaaaaa",   # subroutine call (t = delta)
    "@CALL@": "1111i001ttttt1aaaaaaaaaaaaaaaaaa",   # subroutine call indirect
    "@LBB":   "1111i010bbbbb0aaaaaaaaaaaaaaaaaa",   # load BCE base register
    "@LBB@":  "1111i010bbbbb1aaaaaaaaaaaaaaaaaa",   # load BCE base register indirect
    "@LBP":   "1111i011bbbbb0aaaaaaaaaaaaaaaaaa",   # load BCE program counter
    "@LBP@":  "1111i011bbbbb1aaaaaaaaaaaaaaaaaa",   # load BCE program counter indirect
    "@CI":    "1111i110_____0aaaaaaaaaaaaaaaaaa",   # compare immediate
    "@C":     "1111i110_____1aaaaaaaaaaaaaaaaaa",   # compare (memory)
    "@TMI":   "1111i111_____0aaaaaaaaaaaaaaaaaa",   # test under mask immediate
    "@TM":    "1111i111_____1aaaaaaaaaaaaaaaaaa",   # test under mask (memory)
}

BCE_DESC = {
    # --- BCE register instructions ---
    "#LTOI":  "10110ddddddddddd",   # load time-out register immediate
    "#LTO":   "10111ddddddddddd",   # load time-out register
    "#RIB":   "11100___________",   # reset indicator bit
    "#SIB":   "11101___________",   # set indicator bit
    "#SSC":   "0100mddddddddddd",   # store status and clear (m = index mode)
    "#SST":   "0101mddddddddddd",   # store status
    "#LBR":   "11110010000000aaaaaaaaaaaaaaaaaa",   # load base register
    "#LBR@":  "11111010000000aaaaaaaaaaaaaaaaaa",   # load base register indexed

    # --- BCE branching ---
    "#BU":    "11110000000000aaaaaaaaaaaaaaaaaa",   # branch unconditional
    "#BU@":   "11111000000000aaaaaaaaaaaaaaaaaa",   # branch unconditional indexed
    "#WIX":   "00100ddddddddddd",   # wait for index

    # --- BCE transmission ---
    "#CMDI":  "11110110uuuuuiiiiiiiiiiiiiiiiiii",   # transmit command immediate
    "#CMD":   "11111110000000aaaaaaaaaaaaaaaaaa",   # transmit command
    "#TDS":   "100cccccdddddddd",   # transmit data short (c=count, d=disp)
    "#TDLI":  "11110100000000cccccccccccccccccc",   # transmit data long immediate
    "#TDL":   "11111100000000aaaaaaaaaaaaaaaaaa",   # transmit data long
    "#MOUT":  "11110101ddddddddcccccccccccccccc",   # message out (d=disp, c=count)
    "#MOUTC": "________uuuuummmmmmmmmmmmmmmmmmm",   # message-out command word
    "#MOUT@": "11111101000000aaaaaaaaaaaaaaaaaa",   # message out indexed

    # --- BCE receive data ---
    "#RDS":   "011cccccdddddddd",   # receive data short
    "#RDLI":  "11110011000000cccccccccccccccccc",   # receive data long immediate
    # receive data long: the 18-bit operand is a main-store ADDRESS (the
    # count lives in memory at addr + 2*BCE#), so it is an 'a' field like
    # #TDL/#MIN@ -- the flight image relocates it (FIODEUPG '#RDL FIOWCE'
    # links to FB00 8C94); it was first transcribed 'c' by analogy to #RDLI.
    "#RDL":   "11111011000000aaaaaaaaaaaaaaaaaa",
    "#MIN":   "11110001ddddddddcccccccccccccccc",   # message in (d=disp, c=count)
    "#MINC":  "________uuuuuccccccccccccccccccc",   # message-in command word
    "#MIN@":  "11111001000000aaaaaaaaaaaaaaaaaa",   # message in indexed

    # --- BCE special ---
    "#DLYI":  "11000iiiiiiiiiii",   # delay immediate (literal count)
    "#DLY":   "11001ddddddddddd",   # delay from memory (d = PC-relative addr of the
                                    # delay word; HW adds 2*BCE# at runtime).  Opcode
                                    # 11001 disambiguates from #DLYI's 11000.  Flight-
                                    # pinned: FCMMGBOV `#DLY FCMGNDLC+1` -> C800 (d=0,
                                    # DASS_G16).
    "#WAT":   "0000100000000000",   # wait (same encoding as MSC @WAT: 0x0800)
    "#STP":   "0001___________f",   # self test (f = flag)
}

# Simulator-descriptor discrepancies found against the BFS MSC/BCE listings.
# Flight listings are ground truth; the GPC coffee is secondary and historically
# had these wrong.  When the ext/sim harness auto-extracts descriptors, this is
# the overlay applied so the generated definitions stay listing-correct.  Each
# entry: the simulator's descriptor vs the corrected one and the listing evidence.
# All seven MSC entries are now reconciled (the coffee matches), so they are
# historical record.  Remaining coffee worklist is BCE-only:
#   * #WAT -- coffee '00001___________' vs listing '0000100000000000'; encodes
#             identically (0x0800), only the decode mask differs.
SIM_BUGS = {
    "@RBI": ("1110011100000000", "11100111bbbbb000",
             "@RBI 14 -> E770 (BCE 14 << 3 in bits 8-12); sim has no field"),
    "@LAR": ("11100000rrdddddd", "11100000dddddddd",
             "@LAR 3 -> E003 (operand in DATA byte, not bits 8-9)"),
    "@RAW": ("11010001dddddddd", "1101i000dddddddd",
             "@RAW 0(1) -> D800 (OPX=000, index bit set)"),
    "@RNW": ("11010001dddddddd", "1101i001dddddddd",
             "@RNW 1 -> D101 (OPX=001)"),
    "@RNI": ("11011000dddddddd", "1101i101dddddddd",
             "@RNI 153 -> D599, @RNI 0(1) -> DD00 (OPX=101, index bit)"),
    "@DLY": ("1011iddddddddddd", "1100iddddddddddd",
             "@DLY 680 -> C2A8 (opcode 1100, not 1011)"),
    "@WAT": ("1011000000000000", "0000100000000000",
             "@WAT 0 -> 0800 (opcode 00001), matching #WAT"),
    # #DLY's old sim bug (11000..., identical to #DLYI) is resolved; the coffee
    # now carries the correct 11001..., so it is no longer an overlay entry.
}
