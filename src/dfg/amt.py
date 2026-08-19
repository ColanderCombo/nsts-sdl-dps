#!/usr/bin/env python3
#
# AMT (Application Moding Table) generation — the DFG's *other* mode.
#
# The CDAP04..CDAP16 members of SSSRC are not display decks: they are DFG
# AMT-mode input decks (`PMF=(PHASE=nn,MFBID=mm),` + `AMTG/AMTP/AMTS=(...)`
# command groups, terminated by `END`).  The real DFG 30.40 translated such a
# deck into a HAL/S COMPOOL (`CDA_Pnn_AMT: COMPOOL RIGID;` ... `CLOSE;`) that
# HAL/S-FC compiled to object member CDAPnn (csect #PCDAPnn), INSERTed by the
# CON80 phase decks.  The UI/FCOS reads the tables to mode OPS/SPEC displays:
# which OPS live in this memory configuration, each OPS's modes and legal
# SPECs, the control-segment program for each, and their scheduling
# priorities (the PRIO_* symbols come from the INCL80 ZPRIOTIM include).
#
# Generation rules, derived line-by-line from the OI301700.listing oracles
# (pfs/code/OI301700.listing/SSSRC/CDAP04..CDAP16, banner `DFG VERSION:
# 30.40`):
#
#   * Banner + verbatim echo of every input card as `C` comment cards.
#   * Six DFB/DMMD `D INCLUDE TEMPLATE ... REMOTE NOLIST` compools, named
#     from PMF: CDB021 (fixed), CDD<mm>2 (MFB), CDF<nn>3/CDG<nn>4/CDH<nn>5
#     and DMMD CDR<nn>D (phase) — confirmed across MFB 03/09/14, phases
#     04..16.
#   * `D INCLUDE TEMPLATE <name> NOLIST` for each unique OPGM, then each
#     unique non-CC SPGM, then (AMTS CC=YES only) each unique CC SPGM.
#   * CDAV_BNAME: NAME ARRAY(2) BIT(16) REMOTE pointers to the six
#     <buffer>_HDR arrays.
#   * CDAV_MF(3): one (NOPS, COPY_FOPS) row per major function — row 1 = PL
#     (AMTP), row 2 = GNC (AMTG), row 3 = SM (AMTS); the deck's row carries
#     (number of OPS groups, 1), the others (0, 0).  Oracle quirk: both SM
#     decks (CDAP15/16, one OPS group each) emit NOPS = -1 — reproduced for
#     AMTS as observed.
#   * CDAV_OPS(6): per OPS group (OPSID, NMODES, MODXXFR_ST, NSPEC, SPEC_ST,
#     XFR, PRIOR).  MODXXFR_ST/SPEC_ST are running 1-based indices into
#     CDAV_MODE_VAL_ARR / CDAV_SPEC; XFR is HEX'0001' in every oracle;
#     PRIOR is PRIO_ + first 3 chars of the OPGM name (a ZPRIOTIM REPLACE).
#   * CDAV_OPNAME(6): NAME(<opgm>) per OPS group.
#   * CDAV_SPEC(12): (id, blocks, PRIO_<spgm[:3]>) per non-CC SPEC in deck
#     order (duplicates across OPS kept).  CDAV_SPNAME(12): NAME(<spgm>).
#   * CDAV_MODE_VAL_ARR(34) BIT(32): HEX'<block-count>' per mode, OPS order.
#   * CC=YES SPECs (AMTS): CDAV_NUMBER_OF_CC_SPECS = count;
#     CDAV_CC_SPNAME(<n unique CC programs>) = (PRIO_<pgm[:3]>, NAME(pgm));
#     CDAV_CC_SPEC(18) = (id, blocks, BIT(16) mask with bit <OPSID from
#     left, 1-based> set) per CC SPEC.
#   * Every partially-filled INITIAL list ends with `, *`.
#
# Table capacities (6/12/34/3/18) are fixed in every oracle regardless of
# content.  No SDF/type resolution is needed: AMT generation is purely
# syntactic.
#
import re

from ap101Utils import cards

_DFG_VERSION = "30.40"

# fixed table capacities (every 30.40 oracle)
_CAP_OPS = 6
_CAP_SPEC = 12
_CAP_MODE = 34
_CAP_CC = 18
_MF_ROWS = 3
# AMT command letter -> CDAV_MF row (1-based): PL / GNC / SM
_MF_ROW = {"P": 1, "G": 2, "S": 3}

_CMD_RE = re.compile(r"^\s{0,4}(PMF|AMT[GPS])\s*=\s*\(")


class AmtError(Exception):
    pass


def is_amt_deck(path):
    """True when the file's first non-comment code card is a PMF=/AMTx=(
    command — the DFG AMT-mode input form."""
    try:
        with open(path, errors="replace") as f:
            for raw in f:
                t = cards.code_text(raw)
                s = t.strip()
                if not s or s[0] == "*":
                    continue
                return bool(_CMD_RE.match(t))
    except OSError:
        return False
    return False


class Op:
    def __init__(self, opsid):
        self.opsid = opsid
        self.modes = []          # per-mode block counts
        self.opgm = None
        self.specs = []          # (id, blocks, spgm, cc)


class AmtDeck:
    def __init__(self):
        self.phase = None
        self.mfb = None
        self.mf = None           # 'G' | 'P' | 'S'
        self.ops = []
        self.echo = []           # every input card, verbatim (comments too)

    @property
    def name(self):
        return "CDA_P%02d_AMT" % self.phase

    @property
    def buffers(self):
        """The six DFB/DMMD compools (NBUF1..NBUF5 + NMMDX)."""
        return ["CDB021",
                "CDD%02d2" % self.mfb,
                "CDF%02d3" % self.phase,
                "CDG%02d4" % self.phase,
                "CDH%02d5" % self.phase,
                "CDR%02dD" % self.phase]

    def _uniq(self, names):
        out, seen = [], set()
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    @property
    def opgms(self):
        return self._uniq(o.opgm for o in self.ops)

    @property
    def spgms(self):
        return self._uniq(s[2] for o in self.ops for s in o.specs if not s[3])

    @property
    def cc_spgms(self):
        return self._uniq(s[2] for o in self.ops for s in o.specs if s[3])

    def cc_specs(self):
        """(id, blocks, spgm, opsid) per CC=YES SPEC, deck order."""
        return [(s[0], s[1], s[2], o.opsid)
                for o in self.ops for s in o.specs if s[3]]


def _dim(n):
    # HAL/S: A one copy structure is -STRUCTURE. n > 1 == -STRUCTURE(n)
    return "" if n == 1 else "(%d)" % n


def _prio(pgm):
    """Scheduling-priority symbol for a control-segment program: PRIO_ + the
    program name's first 3 characters (a ZPRIOTIM REPLACE macro)."""
    return "PRIO_" + pgm[:3]


def parse(path):
    """Parse an AMT deck file into an AmtDeck."""
    deck = AmtDeck()
    code = []
    for raw in open(path, errors="replace"):
        t = cards.code_text(raw)
        if not t.strip():
            continue
        deck.echo.append(t)
        if t.strip()[0] != "*":
            code.append(t.strip())
    blob = "".join(code)

    m = re.search(r"PMF=\(([^)]*)\)", blob)
    if not m:
        raise AmtError("no PMF=(PHASE=,MFBID=) command")
    for k, v in re.findall(r"(\w+)=(\d+)", m.group(1)):
        if k == "PHASE":
            deck.phase = int(v)
        elif k == "MFBID":
            deck.mfb = int(v)
    if deck.phase is None or deck.mfb is None:
        raise AmtError("PMF lacks PHASE=/MFBID=")

    for cmd, body in re.findall(r"AMT([GPS])=\(([^)]*)\)", blob):
        if deck.mf is None:
            deck.mf = cmd
        elif deck.mf != cmd:
            raise AmtError("mixed AMT%s/AMT%s commands" % (deck.mf, cmd))
        _parse_group(deck, body)
    if not deck.ops:
        raise AmtError("no AMT OPS group")
    if "END" not in blob:
        raise AmtError("no END card")
    return deck


def _parse_group(deck, body):
    toks = [t.strip() for t in body.split(",") if t.strip()]
    i = 0
    spec = None
    while i < len(toks):
        t = toks[i]
        k, eq, v = t.partition("=")
        if not eq:
            raise AmtError("stray token %r in AMT group" % t)
        if k == "OPS":
            deck.ops.append(Op(int(v)))
            spec = None
        elif k == "MODES":
            if not deck.ops:
                raise AmtError("MODES= before OPS=")
            n = int(v)
            deck.ops[-1].modes = [int(x) for x in toks[i + 1:i + 1 + n]]
            if len(deck.ops[-1].modes) != n:
                raise AmtError("MODES=%d with %d values"
                               % (n, len(deck.ops[-1].modes)))
            i += n
        elif k == "OPGM":
            deck.ops[-1].opgm = v
        elif k == "SPEC":
            if not deck.ops:
                raise AmtError("SPEC= before any OPS group")
            if i + 1 >= len(toks) or "=" in toks[i + 1]:
                raise AmtError("SPEC=%s lacks a block count" % v)
            spec = [int(v), int(toks[i + 1]), None, False]
            deck.ops[-1].specs.append(spec)
            i += 1
        elif k == "SPGM":
            if spec is None:
                raise AmtError("SPGM= outside a SPEC entry")
            spec[2] = v
        elif k == "CC":
            if spec is None:
                raise AmtError("CC= outside a SPEC entry")
            spec[3] = v == "YES"
        else:
            raise AmtError("unknown AMT operand %s=" % k)
        i += 1


def template_names(deck):
    """Every compool/program the generated source D INCLUDE TEMPLATEs, in
    include order — the build seeds its template closure from these."""
    return deck.buffers + deck.opgms + deck.spgms + deck.cc_spgms


# ---------------------------------------------------------------- emission

def _box(text):
    """One banner card: C + ' ****' + text in a 61-column field + '****'."""
    return "C ****%-61s****" % text


def _wrap(stmt, indent="   "):
    """One HAL statement onto <=72-column cards, oracle indentation."""
    line = indent + stmt
    if len(line) <= 72:
        return [line]
    out, cur = [], line
    while len(cur) > 72:
        cut = cur.rfind(" ", len(indent) + 1, 72)
        if cut <= 0:
            break
        out.append(cur[:cut])
        cur = indent + cur[cut + 1:]
    out.append(cur)
    return out


def _initial(vals, capacity):
    """INITIAL value list, `, *`-terminated when partially filled."""
    if len(vals) > capacity:
        raise AmtError("%d initializers exceed table capacity %d"
                       % (len(vals), capacity))
    star = [] if len(vals) == capacity else ["*"]
    return ", ".join(list(vals) + star)


def to_hal(deck):
    """Render the AMT deck as the DFG 30.40 HAL/S COMPOOL source."""
    L = []
    blank = ""
    for _ in range(4):
        L.append(_box(blank))
    L.append(_box(" " * 22 + "COMPOOL NAME: %s" % deck.name))
    L.append(_box(" " * 23 + "DFG VERSION:       %s" % _DFG_VERSION))
    for _ in range(2):
        L.append(_box(blank))
    L.append(_box(" " * 29 + "PHASE:    %02d" % deck.phase))
    L.append(_box(" " * 31 + "MFB:    %02d" % deck.mfb))
    for _ in range(5):
        L.append(_box(blank))
    L.append("C ****           DFG INPUT      ****")
    for card in deck.echo:
        L.append("C " + card)
    L += ["C", "C"]

    L += ["C  INCLUDE compiler directives used to reference the",
          "C  Display Format Buffers (DFBs) and the Display",
          "C  Format Mass Memory Directory (DMMD).",
          "C"]
    for b in deck.buffers:
        L.append("D  INCLUDE TEMPLATE %s REMOTE NOLIST" % b)

    L += ["C",
          "C  INCLUDE TEMPLATE compiler directive for each unique OPS Control",
          "C  Segment Program identified by an AMT command",
          "C"]
    for p in deck.opgms:
        L.append("D  INCLUDE TEMPLATE %s NOLIST" % p)

    L += ["C",
          "C   INCLUDE TEMPLATE compiler directive for each unique SPEC Control",
          "C  Segment Program identified by an AMT command"]
    for p in deck.spgms:
        L.append("D  INCLUDE TEMPLATE %s NOLIST" % p)

    if deck.cc_spgms:
        L += ["C",
              "C   INCLUDE TEMPLATE compiler directive for each unique CC SPEC Control",
              "C  Segment Program identified by an AMT command"]
        for p in deck.cc_spgms:
            L.append("D  INCLUDE TEMPLATE %s NOLIST" % p)

    L.append(" %s: COMPOOL RIGID;" % deck.name)
    L.append("D  INCLUDE ZPRIOTIM NOLIST")

    # CDAV_BNAME — NAME pointers to the DFB/DMMD header arrays.
    L.append("   STRUCTURE CDAV_BNAME:")
    for i in range(5):
        L.append("     1 CDAV_NBUF%d NAME ARRAY(2) BIT(16) REMOTE," % (i + 1))
    L.append("     1 CDAV_NMMDX NAME ARRAY(2) BIT(16) REMOTE;")
    L += _wrap("DECLARE CDAV_BNAME CDAV_BNAME-STRUCTURE INITIAL(%s);"
               % ", ".join("NAME(%s_HDR)" % b for b in deck.buffers))

    # CDAV_MF — one (NOPS, COPY_FOPS) row per major function.
    L += ["   STRUCTURE CDAV_MF:",
          "     1 CDAV_MF_NOPS INTEGER SINGLE,",
          "     1 CDAV_MF_COPY_FOPS INTEGER SINGLE;"]
    nops = -1 if deck.mf == "S" else len(deck.ops)
    row = _MF_ROW[deck.mf]
    mf_vals = []
    for r in range(1, _MF_ROWS + 1):
        mf_vals += [str(nops), "1"] if r == row else ["0", "0"]
    L += _wrap("DECLARE CDAV_MF CDAV_MF-STRUCTURE(%d) INITIAL(%s);"
               % (_MF_ROWS, ", ".join(mf_vals)))

    # CDAV_OPS — per-OPS moding info.
    L += ["   STRUCTURE CDAV_OPS:",
          "     1 CDAV_OP_OPSID INTEGER SINGLE,",
          "     1 CDAV_OP_NMODES INTEGER SINGLE,",
          "     1 CDAV_OP_MODXXFR_ST INTEGER SINGLE,",
          "     1 CDAV_OP_NSPEC INTEGER SINGLE,",
          "     1 CDAV_OP_SPEC_ST INTEGER SINGLE,",
          "     1 CDAV_OP_XFR BIT(16),",
          "     1 CDAV_OP_PRIOR INTEGER SINGLE;"]
    ops_vals = []
    mode_ix = spec_ix = 1
    for o in deck.ops:
        nspec = sum(1 for s in o.specs if not s[3])
        ops_vals += [str(o.opsid), str(len(o.modes)), str(mode_ix),
                     str(nspec), str(spec_ix), "HEX'0001'", _prio(o.opgm)]
        mode_ix += len(o.modes)
        spec_ix += nspec
    L.append("   DECLARE CDAV_OPS CDAV_OPS-STRUCTURE(%d) INITIAL(" % _CAP_OPS)
    L += ["C",
          "C  Information about each OPS, in the following order:",
          "C  OPS id, number of modes within OPS, Index into the",
          "C  CDAV_MODE_VAL_ARR, Number of SPECs available with an",
          "C  OPS, Index into CDAV_SPEC OPS to OPS transfer",
          "C  information, Scheduling priority",
          "C"]
    L += _wrap(_initial(ops_vals, _CAP_OPS * 7) + ");")

    # CDAV_OPNAME — the OPS control-segment programs.
    L += ["   STRUCTURE CDAV_OPNAME:",
          "     1 CDAV_OP_PROGNM NAME PROGRAM;",
          "   DECLARE CDAV_OPNAME CDAV_OPNAME-STRUCTURE(%d) INITIAL("
          % _CAP_OPS,
          "C",
          "C  CDAV_OPNAME structure, which contains the OPS ControlSegments"]
    L += _wrap(_initial(["NAME(%s)" % o.opgm for o in deck.ops],
                        _CAP_OPS) + ");")

    # CDAV_SPEC / CDAV_SPNAME — the non-CC SPECs, deck order, dups kept.
    L += ["C",
          "C  Information about each SPEC,in the following order:",
          "C  SPEC Id(unique display number),number of blocks within the "
          "SPEC and th",
          "C  scheduling priority of the SPEC's Control Segment",
          "   STRUCTURE CDAV_SPEC:",
          "     1 CDAV_SP_ID INTEGER SINGLE,",
          "     1 CDAV_SP_BLOCKS INTEGER SINGLE,",
          "     1 CDAV_SP_PRIOR INTEGER SINGLE;"]
    spec_vals, spname_vals = [], []
    for o in deck.ops:
        for sid, blocks, spgm, cc in o.specs:
            if cc:
                continue
            spec_vals += [str(sid), str(blocks), _prio(spgm)]
            spname_vals.append("NAME(%s)" % spgm)
    L += _wrap("DECLARE CDAV_SPEC CDAV_SPEC-STRUCTURE(%d) INITIAL(%s);"
               % (_CAP_SPEC, _initial(spec_vals, _CAP_SPEC * 3)))
    L += ["   STRUCTURE CDAV_SPNAME:",
          "     1 CDAV_SP_SPECNM NAME PROGRAM;",
          "   DECLARE CDAV_SPNAME CDAV_SPNAME-STRUCTURE(%d) INITIAL("
          % _CAP_SPEC,
          "C",
          "C  CDAV_SPNAME structure, which contains the SPEC Control Segments"]
    L += _wrap(_initial(spname_vals, _CAP_SPEC) + ");")

    # CDAV_MODE_VAL_ARR — per-mode block counts, OPS order.
    L += ["C",
          "C  Each element in the CDAV_MODE_VAL_ARR holds the number of",
          "C  blocks within an individual OPS major mode.",
          "C"]
    mode_vals = ["HEX'%08X'" % m for o in deck.ops for m in o.modes]
    L += _wrap("DECLARE CDAV_MODE_VAL_ARR ARRAY(%d) BIT(32) INITIAL(%s);"
               % (_CAP_MODE, _initial(mode_vals, _CAP_MODE)))

    # CC=YES SPECs (AMTS payload-command-filter displays).
    cc = deck.cc_specs()
    if cc:
        L += _wrap("DECLARE CDAV_NUMBER_OF_CC_SPECS INTEGER SINGLE "
                   "INITIAL(%d);" % len(cc))
        L += ["   STRUCTURE CDAV_CC_SPNAME:",
              "     1 CDAV_CC_SPEC_PRIORITY INTEGER SINGLE,",
              "     1 CDAV_CC_SPECNM NAME PROGRAM;"]
        prog_vals = []
        for p in deck.cc_spgms:
            prog_vals += [_prio(p), "NAME(%s)" % p]
        L += _wrap("DECLARE CDAV_CC_SPNAME CDAV_CC_SPNAME-STRUCTURE%s "
                   "INITIAL(%s);"
                   % (_dim(len(deck.cc_spgms)),
                      _initial(prog_vals, len(deck.cc_spgms) * 2)))
        L += ["   STRUCTURE CDAV_CC_SPEC:",
              "     1 CDAV_CC_SP_ID INTEGER SINGLE,",
              "     1 CDAV_CC_SP_BLOCKS INTEGER SINGLE,",
              "     1 CDAV_OPS_ASSOC_CC_SPEC BIT(16);"]
        cc_vals = []
        for sid, blocks, _spgm, opsid in cc:
            if not 1 <= opsid <= 16:
                raise AmtError("CC SPEC %d: OPS id %d not in 1..16"
                               % (sid, opsid))
            mask = "".join("1" if i == opsid else "0" for i in range(1, 17))
            cc_vals += [str(sid), str(blocks), "BIN'%s'" % mask]
        L += _wrap("DECLARE CDAV_CC_SPEC CDAV_CC_SPEC-STRUCTURE(%d) "
                   "INITIAL(%s);" % (_CAP_CC, _initial(cc_vals, _CAP_CC * 3)))

    L.append(" CLOSE %s;" % deck.name)
    return "\n".join(L) + "\n"


def generate(path):
    """AMT deck file -> generated HAL/S COMPOOL source text."""
    return to_hal(parse(path))
