import json,glob,collections,sys
rows=[]
for f in glob.glob('results/snap-v1-dev/shard-*/rows.jsonl')+glob.glob('results/snap-v1-clean/shard-*/rows.jsonl'):
  for l in open(f):
    r=json.loads(l)
    if r['arm']=='input_only' and not r.get('inconclusive'): rows.append(r)
print('inputs',len(rows))
M=['gmsh_prod','gmsh_default','gmsh_alt','netgen_occ']
fails=collections.Counter(); combos=collections.Counter(); data=[]
for r in rows:
  t=r.get('triage') or {}
  pf={m['name'] for m in (t.get('panel_failing') or [])}
  valid=bool(r.get('in_valid'))
  faithful=r.get('in_faithful_ok') is not False
  full_pass=bool(r.get('in_clean'))
  assert full_pass == (valid and faithful and not pf), (r['name'], full_pass, valid, faithful, pf)
  data.append((pf, valid and faithful, full_pass))
  for m in pf: fails[m]+=1
  combos[tuple(sorted(pf))]+=1
print('member fail counts',dict(fails)); print('combos',dict(combos))
def agree(cm):
  c=collections.Counter()
  for pf,ok,fullp in data:
    cp = ok and not (pf & set(cm))
    c[('cheap_pass' if cp else 'cheap_fail')+'_full_'+('pass' if fullp else 'fail')]+=1
  return dict(sorted(c.items()))
for cm in (['gmsh_default'],['gmsh_prod'],['gmsh_default','gmsh_prod'],['gmsh_default','netgen_occ'],['gmsh_default','gmsh_alt'],['gmsh_prod','netgen_occ']):
  print(cm, agree(cm))
