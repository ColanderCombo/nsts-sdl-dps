import sys, glob
sys.path.insert(0,'src')
from ap101Utils.objModule import ObjectFile, SymRefType
# clean DASS: lines like ' 001414-0014AF  @0AIESIP **** 009C(  156)  STACK ...'
import re
dass={}
for line in open(sys.argv[1]):
    m=re.search(r'(@0[A-Z0-9]{6})\s+\*\*\*\*\s+([0-9A-F]+)\(\s*(\d+)\)\s+STACK', line)
    if m: dass[m.group(1)]=int(m.group(3))  # decimal bytes
rows=[]
for fn in sorted(glob.glob('build/phase02.out/obj/*.obj')):
    try: of=ObjectFile(fn)
    except: continue
    ends={}
    for mo in of.modules:
        cur=None
        for s in mo.symbols:
            if s.name is None: continue
            if s.symType==SymRefType.CONTROL: cur=s.name
            elif s.name=='STACKEND' and cur is not None: ends[cur]=(s.offsetInCSECT or 0)
    prog=[c for c in ends if c.startswith('$0')]
    if not prog: continue
    at='@0'+prog[0][2:]
    if at not in dass: continue
    rows.append((at,dass[at],ends))
rows.sort()
print(f"{'stack':10} {'DASSb':>5} {'$0':>4} {'sum':>4} {'max':>4}  blockvals(bytes)")
tot_dass=0
for at,d,ends in rows:
    vals=list(ends.values()); s0=ends.get('$0'+at[2:],0)
    tot_dass+=d
    print(f"{at:10} {d:5} {s0:4} {sum(vals):4} {max(vals):4}  {sorted(vals,reverse=True)}")
print(f"n={len(rows)} total DASS bytes={tot_dass} = {tot_dass//2} hw (expect 1948)")
