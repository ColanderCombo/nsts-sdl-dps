# dfg — Display Format Generator

Translates a Shuttle DEU **display deck** (`HEADER=`/`STAT`/`XC=`/`CHAR=`/`VARY`/
`VPARM=(…)` directives) into a HAL/S COMPOOL.

## Usage

```
dfg CG3011              # HAL/S source to stdout
dfg CG3011 -o out.hal   # ... to a file
dfg XG0500              # critical-format deck -> CFT_<n> FCW-array compool
deucflm CFSYSIN -L objdir   # link DEUCFLM from the members' .obj (see below)
```

## The DFG Language

A deck is a stream of comma/newline-separated directives: a bare `KEY`,
`KEY=value`, or `KEY=(args…)` — parentheses protect commas, and a group value
may span cards.  A directive may be preceded by a `LABEL:` branch target
(referenced by `TEST`/`BR`).

```
HEADER=1670S,
STAT,
XC=1,YC=8, CHAR=(A STRING), CARRTN,
VARY,
RATE=3,
RTC=(CDC_COMPOOL.CDC_MEMBER,12,RTC_23_TEXT,3,N,16),
VPARM=(NAME=CDC_COMPOOL.CDC_MEMBER2,ATTR=H,FMT=4.0,CONV=C,ZEROES=NO,SIGN=N),
END
```

### Comments

- `*` in column 1 — the whole card is a comment (banners, change history).
- `*/` or `*@` anywhere else — the rest of the card is a comment (`*@` marks
  change-tracking notes).  The card boundary ends the comment; the trailing
  `,` most comments carry is house style, not syntax (verified corpus-wide).
- Deck-editing directives are parsed but ignored: `ADD`, `CHANGE`, `DELETE`,
  `CR`, `PCR`, `RESEQ`, `RENUM`, `RESEQUENCED`.

### Directives

Deck structure and KVT:

- `HEADER=nnnnF`
  - display number + major-function letter (`C` command, `P` PL, `G` GNC,
    `S` SM, `V` vehicle utility)
  - the first names the display; later ones are page aliases
- `CRTFMT=nnnnF`
  - leads a critical-format background deck (the `X*` members): the output
    is a bare `CFT_<n>` FCW array (no DFT header/KVT/DDT, terminated
    `111E, 0000, 0000`) instead of a display compool
  - these are the shared backgrounds DEULOC displays draw over, packed
    into `DEUCFLM` (see below)
- `INCLUDE=name`
  - referenced compool (emitted as `D INCLUDE TEMPLATE`)
- `STAT`
  - begin the static (background) section
- `VARY`
  - begin the dynamic (DDT) section
- `END`, `STOP`
  - end of deck
- `PAD=n`, `PAD=(n,…)`
  - reserve `n` spare halfwords at the end of the compool
- `DEULOC=n`
  - no inline background: the shared background sits at DEU memory address
    `n`; the static section is just a Branch FCW to `addr`
  - displays with the same DEULOC draw over the same background
- `ITEM=(nn,cls,fmt,lo,hi)`
  - KVT item `nn`
  - cls: `UE` keyboard entry, `D` display item, `X`/`UX` execute (no fmt)
  - fmt: `I<d>` integer, `H<d>` hex, `S<m.n>` scaled
  - `lo,hi`: legal input range
- `KEYS=(X)`
  - KVT header keyboard-entry-class flag

Drawing (static section; also legal in the DDT, where each becomes inline
IMMED FCWs):

- `XC=n`, `XC=nA`
  - beam to character column `n` (`nA` = absolute screen X)
- `YC=n`, `YC=nA`
  - beam to character row `n` (`nA` = absolute screen Y)
- `CHAR=(text)`
  - draw text, two glyphs per FCW (`\(`/`\)` escape parens)
- `CARRTN`
  - carriage-return glyph
- `SPCHAR=n`
  - special character `n` (CHAR-ROM glyph)
- `VCORD=(x1,y1,x2,y2)`
  - vector, character-cell coordinates
- `VCORDA=(x1,y1,x2,y2)`
  - vector, absolute screen coordinates
- `AXIS=X|Y`
  - direction of character spacing
- `LINE=n`, `LINE=nA`
  - move down `n` rows (`nA` = screen units)
- `SPACE=n`
  - set character advance to `n` columns
- `REPT=(count,skip[,fill])`
  - draw the `fill` glyph (default blank; number = SPCHAR) `count` times
  - `skip` is 0 in every known deck and is not modeled
- `VDISP=n`
  - value-display FCW (in static, fills the preamble slot)

Attributes and modes (DDT, emitted as IMMED FCWs):

- `INT=ON|D` / `OFF|N`
  - intensity attribute on / off
- `SIZE=S|L`
  - small / large characters (pitch pair + mode FCW)
- `BLINK=E|D|OFF`
  - enable / disable blink
- `DASH=E|ON` / `D|OFF`
  - enable / disable dashed lines
- `ALTCHAR=n`
  - glyph `n` from the alternate character set
- `COLOR=n|DEU`
  - select color `n`, or restore the DEU default (MEDS)
- `ANGLE=0|90|180|270`
  - character rotation (0 restores); `ANGLE=var` is the remote form (CASE 14)
- `TRANSOFF`
  - zero both translate registers (reset `TRANSXR`/`TRANSYR`)

Dynamic data (DDT structures, one `DCI#FMT` CASE each):

- `RATE=1..6` (CASE 21)
  - rate-group header: 2 / 1 / 0.5 / 0.25 / 0.125 Hz, 6 = on demand
- `VPARM=(NAME=var,ATTR=c,FMT=d.d,CONV=c,SIGN=c,ZEROES=YES|NO)` (CASE 5)
  - display a value; omitted attrs inherit from the previous VPARM
- `HEX=(start,nbits)` (CASE 24)
  - next VPARM shows bits `start..start+nbits-1` as hex
- `CHARR=(var,count)` (CASE 10)
  - `count` characters from a remote variable
- `RTC=(cond,bit,charvar,n[,f,maxlen])` (CASE 3)
  - remote text check: show half of the packed string pair per test bit
- `BLT=(cond1,bit1,cond2,bit2[,len1,len2])` (CASE 1)
  - bi-level test: char per two condition bits
- `SBC=var` (CASE 13)
  - status-byte check: status character (M/up/down arrow)
- `TEST=(var,bit[,width][,skip|label])` (CASE 18)
  - conditional: skip the next `skip` structures (or to `label`) when the
    test fails
- `BR=n|label` (CASE 22)
  - unconditional branch over `n` structures / to `label`
- `IF(var,bit[,width])=ON|OFF` … `ELSE` … `ENDIF` (CASE 18+22)
  - structured conditional (TEST + branches, resolved to the block ends)
- `MDT=(xtab,tab2)` (CASE 17)
  - dual-table plot (MDISC); `Z` in the 2nd name selects the Z axis
- `DMDUPD=(n,…)`, `DMDUPD=(table,K[,ngroups,…])` (CASE 23)
  - demand-update group list (numeric form = default table)
- `XCR=var`, `YCR=var` (CASE 8, 9)
  - remote X / Y coordinate
- `TRANSXR=var`, `TRANSYR=var` (CASE 6, 7)
  - remote X / Y translate
- `VCORDR=(x1,y1,x2,y2)` (CASE 4)
  - remote vector — four variable pointers
- `CIRCR=var` (CASE 16)
  - remote circle
- `ANGLER=var` (CASE 14)
  - remote character rotation
- `COLORR=var` (CASE 26)
  - remote color (MEDS)
- `LSITE=var` (CASE 25)
  - landing-site table FCWs
- `FCWSR=(var[,I])` (CASE 2)
  - pointer to a pre-built FCW string (array element)
- `BLINKR=(var,bit)` (CASE 11)
  - blink per a remote condition bit


## Critical formats and DEUCFLM

A deck starting with `CRTFMT=` (the `X*` members) is a **critical format**: 
a shared static background resident in DEU memory, which `DEULOC=` displays 
draw over.  It compiles to a bare `CFT_<n>` FCW array — no DFT header, KVT or 
DDT — ending with the `111E` exit-to-dynamics branch and two zeros.

`deucflm` links them into the DEU critical-format load module
(`MMUSYS5H: LOADMOD,MEMBER=DEUCFLM`), laid out by `CON80/CFSYSIN`.
`con80build --critfmt` runs both steps for a whole tree:

```
build/bin/con80build --root code/OI340700 --out build/OI340700 --critfmt
build/bin/deucflm code/OI340700/CON80/CFSYSIN \
    -L build/OI340700/obj/critfmt -o DEUCFLM.bin
```

Output is `<CRTFMTLM>.bin`, big-endian halfwords, which `mmu2mmv --loadmod`
puts on the tape.  `deucflm.py`'s header gives the image layout, and
`test_deucflm.py` checks every display deck's `DEULOC=` against the table.

## How it reads

Two instruction sets are in play, and the code types them separately: an
**FCW** (`fcw.FCW`) is a DEU-interpreted drawing word, while a **DDT
structure** (`model.DDTOp`) is a GPC-interpreted opcode (one DCI#FMT CASE
each) that decides every display cycle which FCWs to send to the DEU.  The
static section is a pure FCW stream drawn once; inside the DDT, FCWs appear
only as IMMED payload the GPC copies through.

| module        | layer |
|---------------|-------|
| `deck.py`     | read a deck into ordered directives |
| `compool.py`  | resolve a compool variable's TYPE from its TEMPLATE |
| `fcw.py`      | the `FCW` word type (fields + constructors per DEU op) and deck-value translators |
| `kvt.py`      | the keyboard/value table (items + limit tables) |
| `ops.py`      | the `DDTOp` classes + the LANGUAGE table (every directive's parse rule, one place) |
| `static.py`   | the static (background) FCW section |
| `ddt.py`      | DDT sequencing + flow/rate resolution over the ops |
| `encode.py`   | assemble the sections; layout arithmetic |
| `emit.py`     | render as HAL/S source |
| `model.py`    | `Segment` emission units, `Padr` pointer words |
| `deucflm.py`  | link the members' compiled objects + CFIT into DEUCFLM (per CFSYSIN) |
