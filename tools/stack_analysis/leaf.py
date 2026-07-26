import sys,glob,re
sys.path.insert(0,'src')
from ap101Utils.objModule import ObjectFile, SymRefType, EsdType
dass={}
for line in open(sys.argv[1]):
    m=re.search(r'(@0[A-Z0-9]{6})\s+\*\*\*\*\s+([0-9A-F]+)\(\s*(\d+)\)\s+STACK',line)
    if m: dass[m.group(1)]=int(m.group(3))
for fn in sorted(glob.glob('build/phase02.out/obj/*.obj')):
    of=ObjectFile(fn)
    ends={}; ers=set()
    for mo in of.modules:
        cur=None
        for s in mo.symbols:
            if s.name is None: continue
            if s.symType==SymRefType.CONTROL: cur=s.name
            elif s.name=='STACKEND' and cur is not None: ends[cur]=s.offsetInCSECT or 0
        for e in mo.esdEntries:
            if e.type==EsdType.ER: ers.add(e.name.strip())
    prog=[c for c in ends if c.startswith('$0')]
    if not prog: continue
    at='@0'+prog[0][2:]
    if at not in dass: continue
    nblocks=len(ends); qcalls=[e for e in ers if e.startswith('#Q')]
    # candidate leaf: 1 block AND no #Q runtime calls
    if nblocks==1 and not qcalls:
        se=ends[prog[0]]
        print(f"LEAF {at}: STACKEND={se}  DASS={dass[at]}hw  ratio={dass[at]/se if se else 0:.2f}  ERs#Q={qcalls}")
