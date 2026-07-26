import glob,re,os,collections
files=sorted(glob.glob('../pfs/PFS/mafgen/DASS_*.ASC'))
# stack line:  ADDR-ADDR  @Nxxxxxx **** HEX(  DEC)  STACK  | comment
pat=re.compile(r'(@[0-9A-Z][A-Z0-9]{6})\s+\*\*\*\*\s+([0-9A-F]+)\(\s*(\d+)\)\s+STACK')
per=collections.defaultdict(dict)   # file -> {name: hw}
allnames=collections.Counter()
prefixes=collections.Counter()
for f in files:
    seen={}
    for line in open(f, errors='replace'):
        m=pat.search(line)
        if m:
            nm,hx,dec=m.group(1),m.group(2),int(m.group(3))
            seen[nm]=dec; allnames[nm]+=1; prefixes[nm[:2]]+=1
    per[os.path.basename(f)]=seen
print("=== per-config stack counts / total halfwords ===")
for f,d in per.items():
    print(f"  {f:28} {len(d):4} stacks  {sum(d.values()):6} hw")
print("=== @-prefix distribution (task-stack detection) ===")
for p,c in sorted(prefixes.items()): print(f"  {p}: {c}")
print(f"=== unique stack CSECT names across ALL configs: {len(allnames)} ===")
# any non-@0 (task) stacks?
tasks=[n for n in allnames if n[1]!='0']
print(f"non-@0 (task) stacks: {len(tasks)} -> {sorted(set(tasks))[:20]}")
