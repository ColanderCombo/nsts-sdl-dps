#!/usr/bin/env python3
"""Compare our PASS2 listing (pass2.rpt) vs real MAFGEN DASS listing for one CSECT.

Emits an aligned opcode-level diff with statement markers.
"""
import re, sys, difflib

def norm_num(tok):
    # X'hhhh' or decimal or hex string -> canonical hex (no leading zeros)
    m = re.fullmatch(r"X'([0-9A-Fa-f]+)'", tok)
    if m: return format(int(m.group(1),16),'X')
    if re.fullmatch(r'\d+', tok): return format(int(tok),'X')
    return tok

def norm_operands(ops):
    if ops is None: return ''
    ops = ops.strip()
    # tokenize numbers, X'..', registers, punctuation
    out=[]
    for tok in re.findall(r"X'[0-9A-Fa-f]+'|[A-Za-z_#@$][A-Za-z0-9_#@$]*|\d+|.", ops):
        out.append(norm_num(tok))
    return ''.join(out)

def parse_ours(path, csect):
    """pass2.rpt: LOC CODE EFFAD LABEL INSN OPERANDS"""
    items=[]; incs=False; stmt=None
    for line in open(path, errors='replace'):
        line=line.rstrip('\n')
        m=re.match(r'^([0-9A-F]{5,7})\s+(.*)$', line)
        if not m:
            continue
        rest=m.group(2)
        cm=re.search(r'^\s*([#@$A-Z0-9_]+)\s+CSECT', rest)
        if cm:
            incs = (cm.group(1)==csect); continue
        sm=re.search(r'(ST#\d+)\s+EQU', rest)
        if sm:
            stmt=sm.group(1); continue
        if not incs: continue
        # fixed columns: INSN at col 45, operands col 52-72
        insn=line[45:52].strip(); ops=line[52:72].strip()
        if not insn or not re.fullmatch(r'[A-Z][A-Z0-9]*', insn): continue
        if not re.match(r'^[0-9A-F]{4}', rest): continue  # must have code words
        loc=int(m.group(1)[:5],16)
        if insn=='DC':
            items.append((loc,stmt,'DC',ops and norm_operands(ops) or ''))
        elif insn=='EQU':
            continue
        else:
            items.append((loc,stmt,insn,norm_operands(ops)))
    return items

def parse_real(path, csect):
    """DASS listing lines: RANGE CSECT+OFF CODEWORDS [EFF] INSN OPERANDS"""
    items=[]; stmt=None
    pat=re.compile(r'^\s*([0-9A-F]{6})(?:-[0-9A-F]{6})?\s+'+re.escape(csect)+r'\+([0-9A-F]{4})\s+(.*)$')
    for line in open(path, errors='replace'):
        line=line.rstrip('\n')
        m=pat.match(line)
        if not m:
            continue
        rest=m.group(3); off=int(m.group(2),16)
        sm=re.search(r'(ST#\d+)\s+EQU', rest)
        if sm:
            stmt=sm.group(1); continue
        # code words: groups of 4 hex separated by 2+ spaces, then optional 6-hex effaddr, then mnemonic
        im=re.match(r'^([0-9A-F]{4})(?:\s{1,2}([0-9A-F]{4}))?\s+(?:([0-9A-F]{6})\s+)?([A-Z]+[A-Z0-9]*)\s+(\S*)', rest)
        if not im: continue
        insn=im.group(4); ops=im.group(5)
        if insn=='EQU': continue
        items.append((off,stmt,insn,norm_operands(ops)))
    return items

def main():
    ours_path, real_path, csect = sys.argv[1], sys.argv[2], sys.argv[3]
    ours=parse_ours(ours_path, csect)
    real=parse_real(real_path, csect)
    print(f'ours: {len(ours)} insns, real: {len(real)} insns')
    a=[f'{i[2]} {i[3]}' for i in ours]
    b=[f'{i[2]} {i[3]}' for i in real]
    sm=difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag,i1,i2,j1,j2 in sm.get_opcodes():
        if tag=='equal': continue
        # skip same-length replaces where the mnemonic sequences match (addr/reloc noise)
        if tag=='replace' and (i2-i1)==(j2-j1) and all(ours[i1+k][2]==real[j1+k][2] for k in range(i2-i1)):
            continue
        print(f'--- {tag}: ours[{i1}:{i2}] (loc {ours[i1][0]:#06x}{"" if i1==i2 else ""} stmt {ours[i1][1] if i1<len(ours) else "?"}) '
              f'vs real[{j1}:{j2}] (off {real[j1][0]:#06x} stmt {real[j1][1] if j1<len(real) else "?"})')
        for k in range(i1,min(i2,i1+12)):
            print(f'  OURS {ours[k][0]:05X} {ours[k][1]}: {a[k]}')
        if i2-i1>12: print(f'  ... {i2-i1-12} more')
        for k in range(j1,min(j2,j1+12)):
            print(f'  REAL {real[k][0]:05X} {real[k][1]}: {b[k]}')
        if j2-j1>12: print(f'  ... {j2-j1-12} more')

if __name__=='__main__':
    main()
