import json,glob,statistics as st
rows=[]
allper=[]
for p in sorted(glob.glob('vm/[0-9][0-9]_*.json')):
    r=json.load(open(p)); tr=(r.get('trace') or {}).get('events') or []
    if not tr: continue
    exps=[e for e in tr if e['kind']=='expand']; stop=[e for e in tr if e['kind']=='stop'][-1]
    marks=[e['t'] for e in exps]+[stop['t']]
    per=[]
    for i,e in enumerate(exps):
        c=[x for x in tr if x['kind']=='candidate' and x['state']==e['state']]
        ran=[x for x in c if x['outcome']!='skipped_budget']
        judged=[x for x in c if x['outcome'] in ('evaluated','duplicate')]
        dur=marks[i+1]-e['t']
        per.append((round(dur),len(ran),len(judged)))
        if judged: allper.append(dur/len(judged))
    print(r['index'], r.get('label'), 'search', round(stop['t']), 'start', round(exps[0]['t']) if exps else None, stop['reason'], 'ops', stop['spent']['ops'], 'exp(dur,ran,judged)=',per)
print('s per judged candidate: median %.0f p75 %.0f max %.0f n=%d'%(st.median(allper), sorted(allper)[int(.75*len(allper))], max(allper), len(allper)))
