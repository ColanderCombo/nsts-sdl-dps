# asm101 ↔ AS037F1 behavioral comparison

Comparing our asm101 (`src/asm101/`, Python) against the IBM OS/360
F-Level Assembler source **AS037F1** (`../pfs/AS037F1/`, S/360 assembly, the
lineage reference for ASM101S). Goal: find bugs / behavioral differences /
missing features that make our assembler more correct.

## Oracle hierarchy (confidence order — applied to every candidate)

1. Our behavior contradicts the AP-101 oracle/spec (IBM-75-A97-001, RUNLST,
   BILDNEW5, MLIB80 corpus) **AND** AS037F1 corroborates the correct behavior. *Highest.*
2. Machine-independent assembler semantics; clear AS037F1 rule; we differ; no
   AP-101 oracle contradicts.
3. AS037F1 has a feature/limit we lack but **nothing real reaches it** — note & defer.
4. Target difference (S/360 vs AP-101 opcode/register/encoding) — **drop at source.**

Reading-level findings are hypotheses, not bugs. Each survivor must be validated
against the oracle (minimal input → run our assembler → confirm divergence)
before any code change. **No code edits in this pass.**

---

## PILOT — expression evaluator (IEUF8V ↔ expressions.py `evalArithmeticExpression`)

Note: `expressions.py` is shared by BOTH the macro/conditional-assembly SETA
arithmetic AND machine-operand expressions (it takes `symtab`+`star`). IEUF8V
is the phase-8 machine-expression evaluator. AS037F1 evaluates SETA in the macro
generator (IEUF2/F3), separately from IEUF8V.

### Confirmed CORRECT (rejected as findings — calibration)
- **Operator precedence**: grammar nests `term = factor {(*|/) factor}` inside
  `arith = term {(+|-) term}` (asm.lark `arith`/`term`/`factor` rules), so `*`/`/`
  bind tighter than `+`/`-`; the flat chained-loop (expressions.py:607-647) only ever
  sees same-level operators. Matches S/360. **Not a bug.**
- **Left-to-right within a precedence level**: matches.
- **Integer division truncates toward zero; ÷0 → 0** (expressions.py:638-643).
  Matches S/360 `D` semantics + manual.

### Category 3 — limits AS037F1 enforces, we don't (nothing real reaches them yet)
- **16-term limit per expression**: IEUF8V COMPT (line 393, `COMP1=A(RLIST+30)`,
  err MNYERR=27 "EXP CONTAINS MORE THAN 16 TERMS"). We have no term cap. *Defer.*
- **Parenthesis nesting limit = 5 levels**: IEUF8V LPAR (line 472,
  `CLI PCNTR,X'04'; BH FATLER2`, err PARERR). Our parser recurses unbounded.
  (⚠️ MEMORY CORRECTION: campaign notes say "≤4 levels"; the real AS037F1 limit
  is **5** — PCNTR may reach 5 before a 6th LPAR errors.) *Defer.*
- **32-bit arithmetic overflow detection**: IEUF8V uses S/360 `M`/`D` (32-bit),
  err ARITHER=56 on overflow (line 604 `BO ERR13`). We use Python arbitrary-
  precision ints — no overflow detection, no 32-bit wrap on intermediates.
  Potential correctness edge if an expression legitimately exceeds 32 bits;
  low reach in flight code. *Defer, but flag for the value-masking review.*

### Category 2/latent — meatiest area: RELOCATABILITY classification
- IEUF8V classifies every expression as **absolute / simply relocatable (CC=1) /
  complexly relocatable (CC=2)**, tracks per-term relocation sign bytes, and
  enforces:
  - **mult/div of a relocatable term is illegal** (IEUF8V MORE line 621, err
    RELERR=25) — line 610 "CHECK THAT NO RELOCATABLE";
  - paired-relocatable-term cancellation (A−B of two reloc terms → absolute).
- Our `evalArithmeticExpression` returns a bare int and tracks **no
  relocatability at all**; relocation/RLD is handled separately in model101.py
  by symbol section. → Does our separate path reject `RELOC*2`, `RELOC/2`,
  `RELOC1*RELOC2`? Does it correctly fold `A−B` (same section) to absolute and
  `A+B` (two reloc) to a complex-reloc error where required? **Highest latent-bug
  probability. Hand to the symbol/USING/location fan-out subagent with oracle.**

---

## FAN-OUT FINDINGS — triaged (3 subagents + my own verification runs)

### ⭐ RANK 1 — ✅ FIXED 2026-06-19 (Phase 0a), validated against BILDNEW5

**STATUS: FIXED.** `model101.py` A-type DC branch now mirrors the Y branch
(eval → unhash → `appendReloc(..., type='A')` → `packBE(...,4)`), and the `'h'`
dup loop is replaced by the shared slice-replicate. **`type='A'` → objectWriter
flags `0x1C` (4-byte fullword adcon)**; Y stays `0x00` (halfword).
- Verified on a minimal deck: `A(local)`/`A(MAIN)`/`A(EXTRN)` all emit the
  evaluated value + a 4-byte RLD (external `A(EXT1)` now relocates vs its ESDID).
- **Validated against BILDNEW5 (GPCIPL flight code): it contained 26
  `DC A(symbol)` adcons (in COPY'd code, `…AC` seq) that the old branch emitted
  as ZERO with no relocation — silently unlinkable. After the fix: each resolves
  to the referenced symbol's address (spot-checked `A(MSCHISAM)=00000189` @ sym
  addr 0x189; `A(DEUMODE)=00003DBE` @ 0x3DBE), ALL symbol addresses unchanged (no
  shift), RLD entries 1283 → 1309 (= exactly +26).**
- ⚠️ **BILDNEW5 RE-BASELINED.** The "byte-identical" gate was passing on the
  buggy zero-adcon output; the new `.obj` is the correct reference going forward.
  The only delta vs pre-fix is the 26 adcon values (00000000 → real address) +
  26 new 4-byte RLDs (+3 RLD cards from reflow). No other change.
- Regression fixture `feat_dc_a_adcon.asm` + 6 checks (200 feature total).

**(original finding, for the record:)**
**A1. `DC A(symbol)` / `DC A(expr)` is broken: no value, NO RLD, wrong length.**
Found INDEPENDENTLY by both the DC/DS and symbol/USING subagents; I re-verified.
- AS037F1: IEUF8D/IEUF8V evaluate the A-con expression, classify relocatability,
  and emit a 4-byte value + an RLD for any relocatable A-con (IEUF8V header
  lines 1080-1120; A-con reloc path IEUF8D:715-725). An `A(EXTRN)` MUST yield an
  RLD or the module can't link.
- OURS: `model101.py:1936-1958` — the A branch handles ONLY the self-defining
  hex form `DC A'hex'` (`'h'` key); for the normal `A(symbol)`/`A(expr)` form it
  falls through to `toMemory(duplicationFactor*4)` (line 1958). It never calls
  `evalArithmeticExpression`, `unhash`, or `relocations.append` — unlike the
  ADJACENT, CORRECT Y branch (`model101.py:1959-2008`).
- My repro (`/tmp/acon.asm`, MAIN CSECT with `A(TGT)`/`A(MAIN)`/`A(EXT1)`):
  exit 0, **no RLD card emitted** (the `A(EXT1)` external ref silently vanishes),
  the three A-cons show a **blank object-code column** (vs `Y(TGT)`→`0008`), and
  the location counter advances only ~2 bytes each instead of a fullword 4 —
  so all downstream addresses are corrupted too.
- Reached by real flight code: BTBCEGEN adcon tables (`DC A(FIOBABBR)`…),
  `FAZ2DATA:34 MSCADDR DC A(MSCINTF)`, MISCELLANEOUS/*.bal.
- FIX: give the A branch the Y branch's eval/unhash/RLD logic (4-byte field,
  RLD type 'A'). Invisible to the 194-test suite because the 6 tested decks are
  *macro* decks, not data decks.

**A1b (rider). A-type `'h'` dup factor → infinite loop.** `model101.py:1950`
`while duplicationFactor > 1:` never decrements → `DC nA'hex'` (n>1) spins until
dcBuffer overruns. The Y branch's comment (lines 2000-2005) says it FIXED exactly
this loop; the A branch kept the broken version. FIX: build the dup via slice
(`dcBuffer[:len]*duplicationFactor`) like Y.

### RANK 2 — ✅ FIXED 2026-06-19 (Phase 0b), validated against BILDNEW5

**STATUS: FIXED.** `model101.py` B branch now packs the bits MSB-first
(implicit length rounds up to a halfword, matching the X branch's AP-101 rule;
explicit `BLn` honored exactly; dup replicated).
- **BILDNEW5/GPCIPL had 27 bare `B'...'` constants hitting the stub — including
  `JOBTABLE` (the job-scheduling table: 27 entries of 32-bit minor-cycle masks).
  The stub emitted ZERO bytes AND did not advance the LC, so JOBTABLE collapsed
  to zero width and every address past 0x2040 was wrong.** After the fix:
  `JOBTABLE`→`C4707FE0`, `SCHEDWRD B'0001110000100000'`→`1C20`, etc. (decodes
  verified). Section length 0x3E23→0x3E57 (+52 bytes).
- **Validated (advisor's 3 checks):** (1) each B-con decodes to its declared
  bits; (2) the deck still converges **rc=0, 0 errors** — the +52-byte shift
  broke no branch displacement; (3) **0 upstream collateral** — all 1298 symbols
  below the first B-con unchanged; symbols above shift monotonically by ≤0x34.
- Regression fixture `feat_dc_b.asm` + 6 checks (206 feature total).
- ⚠️ **BILDNEW5 RE-BASELINED again** (now +A-cons +B-cons). The new `.obj` is the
  reference going forward.

**METHODOLOGY NOTE (important, not alarming):** the "BILDNEW5 byte-identical"
gate is a *regression* gate — "did my change alter output vs the baseline." It
structurally CANNOT find pre-existing latent bugs, only changes; so prior
neutrality-gated fixes remain validly neutral relative to their baseline. The
gate did its job. Discovering the baseline itself carried latent bugs (zero-width
A/B constants) is the AS037F1 comparison delivering its intended value, not a
failure of method. For a re-baselined-correct deck, validate by oracle (each
constant decodes correctly; clean convergence; no upstream collateral), NOT by
byte-equality to the old (buggy) output.

**(original finding, for the record:)**
**A2. `DC B'...'` is a no-op stub: emits no bytes, does not advance the LC.**
- AS037F1: IEUF8D packs B MSB-first into ceil(bits/8) bytes, left-pad zero
  (header IEUF8D:204-205). OURS: `model101.py:1861-1864` is `commonProcessing(1)`
  then bare `pass`. (The bit-length path `packBitLengthDC` handles B, but only
  with an explicit `L.n`; a plain `DC B'...'` never reaches it.)
- My repro: `W DC B'10100101'` emits nothing and lands at the SAME address
  (00000) as the next `V DC X'1234'` — silent data loss + address corruption.
- Reached: FAZ2EXEC / REALEXEC each have 25 B-type DCs (32-bit schedule masks,
  e.g. `JOBTABLE DC B'00000100...'`).
- FIX: pack like the X branch (MSB-first, `max(ceil(len/8), Lmod)` bytes, honor dup).

### RANK 2 — confirmed real, latent (no current flight oracle exercises it)

**R1. ✅ ADDRESSED 2026-06-19 (Phase 1a).** A `classify(value)` helper
(model101.py, reading the section-hash high bits via `unhash`) plus a
complex-relocatability diagnostic at the Y and A adcon sites. The hash already
performs IEUF8V paired-term reduction (A−B same-section → absolute, verified),
so `A*2`/`A+B` land as `classify==complex` and are now flagged ("Complex
relocatable expression not supported in …-type constant") instead of silently
emitting a garbage value. Measured false-positive-free: 0 complex across
BILDNEW5 (108K unhash calls) + all 5 MLIB80 decks; BILDNEW5 byte-identical.
Fixture `feat_reloc_complex.asm` pins both directions. Scope note: catches
complex only at relocation-consuming (`unhash`) sites. Y/A *value-too-large*
(D1 below) deferred — needs its own magnitude measurement + exact IEUF8D bound.

**(original finding:)**
**R1. mult/div of a relocatable term accepted silently (no RELERR).** AS037F1
IEUF8V `MORE` (~line 621, err RELERR=25, "CHECK THAT NO RELOCATABLE") rejects
`reloc*k` / `reloc/k`. OURS computes a bare int (expressions.py:632-643), and the
separate RLD-inference path never flags it. Repro: `Y(A*2)`→0004, `Y(A/2)`→0001,
exit 0, no error, no RLD. (Symmetric `A−B` same-section fold to absolute is
CORRECT — verified.) FIX: detect a relocatable operand feeding `*`/`/` and emit RELERR.

**M1. `N'` of an omitted / past-end operand returns 1, should be 0.** AS037F1
NFETCH (IEUF3:1957) keeps count 0 for an omitted/past-end param. OURS
(expressions.py:564-588) resolves the subscript to `""` first, which isn't a
list, so N' returns 1. Latent: corpus macros guard with `'…' EQ ''` before N'.
FIX: return 0 when the resolved operand/element is null.

**M2. Surplus / undefined keyword macro arg → KeyError crash, not MNOTE.**
AS037F1 emits MIERR ("UNUSED OR MULTIPLY-DEFINED … PARAMETER", IEUF3 CHKEND).
OURS (`assemble.py:984-986`) indexes `"_"+key` which is absent for an undeclared
keyword → uncaught KeyError. FIX: emit a recoverable MNOTE and skip.

### RANK 3 — real but nothing reaches it; note & defer

- **P1. 16-term limit** per expression (IEUF8V COMPT, MNYERR=27) — unenforced.
- **P2. 5-level paren-nesting limit** (IEUF8V LPAR `PCNTR>4`, PARERR) — unenforced.
  (⚠️ memory says "≤4"; real limit is 5.)
- **P3. 32-bit arithmetic-overflow detection** (IEUF8V ARITHER=56) — we use Python
  bignum, no 32-bit wrap/overflow on intermediates.
- **D1. Y-con value-too-large silently truncated** (no diagnostic). AS037F1
  TOOBIGER=17 when |Y| > 0x7FFF (A-cons explicitly never flagged). model101.py:1996.
- **D2. DC-path vs literal-path encoding inconsistency** (discriminating-test item,
  not a verdict): `DC F'1.5'` (×2³¹) vs `=F'1.5'` (round→2); `L` = bytes (DC) vs
  halfwords (literal); scale modifier `Sn` applied by literal path, ignored AND
  unparsed by DC path (`DC F'2'S3` fails to parse). Pick a canonical path for AP-101.
- **M3. `&SYSNDX` rendered as bare int, off-by-one start.** AS037F1: 4-digit
  zero-padded, first invocation = 1 (`LABEL0001`); OURS: starts at 0, no padding
  (`LABEL0`). No real `&SYSNDX` use in the corpus.
- **M4. `K'` of an unsubscripted sublist** errors "Not a string" instead of the
  source-text length (IEUF3 POSK3). No corpus case applies K' to a known sublist.
- **F1. Non-default `ICTL` silently ignored** (in the `ignore` set; columns
  hard-coded in cardreader). No flight code uses non-default ICTL.

### NON-FINDINGS — correctly rejected (calibration; do not re-investigate)

- **E-type DP→SP TRUNCATION (not rounding) is CORRECT for AP-101** — matches the
  load-bearing memory [[halfc_compile_time_arithmetic]] / [[feedback_xxdtoc_faithfulness]]
  (STE truncates, never rounds). AS037F1 rounds (S/360 convention) → that's the
  rank-4 target difference; our truncation is right. (Subagent correctly caught this trap.)
- **USING base-register tie-break** (`findB2D2` keeps higher register on disp tie)
  matches IEUF8M DCCOMP (`BH DCMP03`). Correct.
- **Cross-section USING / ESD-id match** — correct by construction (28-bit section
  hash cancels only within one section).
- **Same-section `A−B` folds to absolute, no spurious RLD** — verified correct.
- **ACTR default = 4096** (`ACTRV EQU 4096`, IEUF2A:3070) — ours matches the lineage.
- **&SYSLIST past-end → null string** — matches AS037F1 POSNULL.
- **SETC 255-char cap / arbitrary substring length** — deliberate Assembler-H
  (not F-level) semantics; real flight macros (FAZ2MAC TITL) require it. Correct
  for our AP-101 H-level target; AS037F1's F-level 8-char cap is rank-4.
- **Continuation `lineContinues`** — documented adaptation to malformed hand-keyed
  decks, not an AS037F1-conformance bug.

---

## Recommended action order
1. **A1 `DC A(symbol)` + A1b dup loop** (rank 1, unlinkable output, real flight data) — clone the Y branch.
2. **A2 `DC B'...'`** (rank 2, real flight data, silent data loss) — pack like X.
3. R1 / M1 / M2 (rank 2, latent or crash-on-bad-input) — cheap diagnostics.
4. Rank-3 items as opportunistic hardening; D2 needs an AP-101 canonical-path decision first.
Each fix MUST hold the gates: 194 features + fastparse golden + BILDNEW5 byte-identical,
plus a new data-deck fixture (the macro decks don't exercise A/B-type DC).
