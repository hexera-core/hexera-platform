import re,glob,sys,collections
for d,lab in zip(sys.argv[1::2],sys.argv[2::2]):
    per=collections.defaultdict(list)
    for p in glob.glob(d+'/out/*dev_*.log'):
        for line in open(p,errors='replace'):
            m=re.search(r'PHASE (\S+) (search_\w+) t=[\d.]+ end (\S+) ([\d.]+)s',line)
            if m: per[m.group(3)].append(float(m.group(4)))
    print('==',lab)
    for k in ('input_verdict','staged_healer','confirm_staged','search','output_verdict'):
        v=per.get(k,[])
        if v: print(f"   {k:15s} n={len(v):3d} total={sum(v):7.0f} mean={sum(v)/len(v):5.0f} max={max(v):6.0f}")
