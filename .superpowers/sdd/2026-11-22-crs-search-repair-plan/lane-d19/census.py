import json,glob,collections,sys
recs=[json.load(open(p)) for p in sorted(glob.glob(sys.argv[1]+'/[0-9][0-9][0-9]_*.json'))]
print('files',len(recs),'errors',sum(1 for r in recs if r.get('error')))
SOLID={'SOLID','COMPSOLID'}
def classify(rt):
    ch=rt.get('children') or []
    s=[i for i,c in enumerate(ch) if c in SOLID]; n=[i for i,c in enumerate(ch) if c not in SOLID]
    if not s: return 'no_solid_root'
    if not n: return 'solids_only'
    return 'nonsolid_first' if n[0]<s[0] else 'nonsolid_among'
t=collections.Counter(); rows=[]
for r in recs:
    if r.get('error'): rows.append((r['index'],r['sha256'][:10],r['split'],'ERROR',r['error'][:80])); continue
    o,e,ng=r['original'],r['engine_form'],r['netgen_sees']
    co,ce=classify(o),classify(e)
    ng_solids=ng.get('solids')
    step_solids=sum(1 for c in (e.get('children') or []) if c in SOLID)
    hides= ng_solids==0 and step_solids>0
    key=(r['split'],ce,'netgen_sees_0' if ng_solids==0 else 'netgen_sees_solids', r['netgen_label'])
    t[key]+=1
    if ce!='solids_only' or hides or (r['netgen_label'] or '').endswith('step_unreadable'):
        rows.append((r['index'],r['sha256'][:10],r['split'],'orig:'+co,'ef:'+ce,str(e.get('children'))[:60],'ng:'+str(ng.get('type'))+'/'+str(ng_solids),'label:'+str(r['netgen_label']),'HIDES' if hides else ''))
for k,v in sorted(t.items()): print(v,k)
print()
for row in rows: print(row)
