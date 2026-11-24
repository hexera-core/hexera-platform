import json,glob,collections,sys
B='/Users/kitts/archductor/sdd-worktrees/lane-d/devtools/validation/corpora/v4/provisional_v2/'
vs={}
for split in ('PROVISIONAL_dev_v2.json','PROVISIONAL_clean_v2.json'):
    for f in json.load(open(B+split))['files']: vs[f['sha256']]=f.get('verdict_summary') or {}
recs=[json.load(open(p)) for p in sorted(glob.glob(sys.argv[1]+'/*.json'), key=lambda p:int(p.split('/')[-1][:-5]))]
mem=collections.Counter(); fil=collections.Counter(); rows=[]; missing=[]
for r in recs:
    if r.get('error') or 'every_root' not in r or 'first_root' not in r: missing.append((r['i'],r['sha256'][:10],r.get('error'))); continue
    a,b=r['first_root'],r['every_root']
    v=vs[r['sha256']]
    others=[m for m in v.get('panel',[]) if m['name']!='netgen_occ']
    others_pass=all(m['status']=='pass' for m in others); valid=bool(v.get('valid'))
    clean_first= valid and others_pass and a['status']=='pass'
    clean_every= valid and others_pass and b['status']=='pass'
    mchange = a['status']!=b['status'] or a['error_kind']!=b['error_kind']
    mem[(r['split'], f"{a['status']}->{b['status']}")]+=1
    fil[(r['split'], f"clean {clean_first}->{clean_every}")]+=1
    rows.append((r['split'], r['sha256'][:10], r['name'][-40:], f"{a['status']}/{a['error_kind']} sicn={a['min_sicn'] and round(a['min_sicn'],4)} el={a['elements']} solids={a['solids']}",
                 f"{b['status']}/{b['error_kind']} sicn={b['min_sicn'] and round(b['min_sicn'],4)} el={b['elements']} solids={b['solids']}",
                 'valid' if valid else 'INVALID', 'others pass' if others_pass else 'others fail', f"clean {clean_first}->{clean_every}", 'MEMBER CHANGE' if mchange else ''))
print('measured',len(rows),'missing',len(missing), missing)
print('member (first->every):'); [print(' ',v,k) for k,v in sorted(mem.items())]
print('file clean (first->every):'); [print(' ',v,k) for k,v in sorted(fil.items())]
print('\nCLEAN CONTROLS:'); [print(' ',x) for x in rows if x[0]=='clean_v2']
print('\nDEV member changes:'); [print(' ',x) for x in rows if x[0]=='dev_v2' and x[-1]]
