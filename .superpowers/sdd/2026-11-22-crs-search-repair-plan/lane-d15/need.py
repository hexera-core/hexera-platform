import json,glob
print("i label | exp1 rows: own / defeature (site,part) | own past k=12 now | s/candidate run (exp1) | start s | wall to run own+defeature in exp1")
for p in sorted(glob.glob('vm/[0-9][0-9]_*.json')):
    r=json.load(open(p)); tr=(r.get('trace') or {}).get('events') or []; pr=r.get('proposes') or []
    if not pr: continue
    rows=pr[0]['rows']; own=[x for x in rows if x['op']!='defeature']; ds=[x for x in rows if x['op']=='defeature' and x['source']=='site']; dp=[x for x in rows if x['op']=='defeature' and x['source']!='site']
    # under the OLD order, own rows cut by k
    own_cut=sum(1 for x in own if x['rank']>=12)
    exps=[e for e in tr if e['kind']=='expand']; stop=[e for e in tr if e['kind']=='stop'][-1]
    marks=[e['t'] for e in exps]+[stop['t']]
    ran=[x for x in tr if x['kind']=='candidate' and x['state']==exps[0]['state'] and x['outcome']!='skipped_budget']
    spc=(marks[1]-marks[0])/max(1,len(ran))
    n=len(own)+len(ds)+len(dp)
    print(r['index'], r.get('label'), '|', len(own), '/', len(ds), len(dp), '|', own_cut, '|', round(spc), '|', round(exps[0]['t']), '|', round(exps[0]['t']+n*spc), 'rows', n)
