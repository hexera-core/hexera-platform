import json,glob,collections,re,sys,os
def run(d):
    out=d+'/out'
    rows={}
    for p in glob.glob(out+'/**/rows.jsonl',recursive=True):
        if 'merged' not in p: continue
        for l in open(p):
            if l.strip():
                r=json.loads(l)
                if r['arm'] in ('search_det','search_ai'): rows[(r['sha256'],r['arm'])]=r
    exc={}
    for p in glob.glob(out+'/**/EXCLUDED.json',recursive=True):
        if 'merged' not in p: continue
        d_=json.load(open(p))
        for e in (d_ if isinstance(d_,list) else d_.get('excluded',[])):
            if e.get('arm') in ('search_det','search_ai'): exc[(e['sha256'],e['arm'])]=e
    # phase of death: the last 'PHASE ... start X' without an end, from unit logs
    died={}
    for p in glob.glob(out+'/*.log'):
        cur={}
        for line in open(p,errors='replace'):
            m=re.search(r'PHASE (\S+) (\S+) t=([\d.]+) (start|end) (\S+)',line)
            if m:
                sha12,arm_,t,se,ph=m.groups(); tag=sha12+' '+arm_
                cur[tag]=f"{ph} (t={t})"   # the timeout unwinds through the phase's finally, so its END is logged
        for tag,ph in cur.items(): died[tag]=ph
    return rows,exc,died
def vcpuh(d):
    log=open(glob.glob(d+'/launch.log')[0]).read() if glob.glob(d+'/launch.log') else ''
    m=re.search(r'([\d.]+) vCPU-h',log); return m.group(1) if m else '?'
for d,lab in zip(sys.argv[1::2],sys.argv[2::2]):
    rows,exc,died=run(d)
    print(f"== {lab}: finished search rows {len(rows)}, file_timeouts {sum(1 for e in exc.values() if 'file_timeout' in e.get('reason',''))} (det {sum(1 for k,e in exc.items() if k[1]=='search_det' and 'file_timeout' in e['reason'])}, ai {sum(1 for k,e in exc.items() if k[1]=='search_ai' and 'file_timeout' in e['reason'])}), other excluded {sum(1 for e in exc.values() if 'file_timeout' not in e.get('reason',''))}; clean det {sum(1 for k,r in rows.items() if k[1]=='search_det' and r.get('out_clean'))} ai {sum(1 for k,r in rows.items() if k[1]=='search_ai' and r.get('out_clean'))}; plan vCPU-h {vcpuh(d)}")
    tot=collections.defaultdict(list)
    for r in rows.values():
        for k,v in (((r.get('triage') or {}).get('phases') or {}).get('seconds') or {}).items(): tot[k].append(v)
    for k in ('input_verdict','staged_healer','confirm_staged','search','output_verdict'):
        v=tot.get(k,[])
        if v: print(f"   {k:15s} n={len(v):3d} total={sum(v):7.0f} mean={sum(v)/len(v):5.0f} max={max(v):5.0f}")
    to=[k for k,e in exc.items() if 'file_timeout' in e.get('reason','')]
    for sha,arm in sorted(to):
        ph=[v for t,v in died.items() if t==sha[:12]+' '+arm]
        print(f"   TIMEOUT {sha[:10]} {arm}: died in {ph[0] if ph else '?'}")
