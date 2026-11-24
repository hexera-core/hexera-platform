import json,glob,collections,sys
d=sys.argv[1]
same=diff=0; loc=collections.defaultdict(lambda: collections.Counter())
FIELDS=("name","status","error_kind","message","min_sicn","elements","cause","algorithms")
rows=[]
for b in sorted(glob.glob(f"{d}/before-*.json"), key=lambda p:int(p.split('-')[-1][:-5])):
    i=b.split('-')[-1][:-5]; a=f"{d}/after-{i}.json"
    try: B,A=json.load(open(b)),json.load(open(a))
    except FileNotFoundError: continue
    if B.get('error') or A.get('error'): rows.append((i,'ERROR',B.get('error'),A.get('error'))); continue
    mism=[]
    if (B['clean'],B['valid'])!=(A['clean'],A['valid']): mism.append(('clean/valid',(B['clean'],B['valid']),(A['clean'],A['valid'])))
    for mb,ma in zip(B['members'],A['members']):
        for f in FIELDS:
            if mb[f]!=ma[f]: mism.append((mb['name'],f,mb[f],ma[f]))
        for k,m in (('before',mb),('after',ma)):
            loc[(mb['name'],k)]['faces']+=m['located_faces']; loc[(mb['name'],k)]['live']+=m['located_live']
    if len(B['members'])!=len(A['members']): mism.append(('member count',len(B['members']),len(A['members'])))
    if mism: diff+=1
    else: same+=1
    rows.append((i, 'EQUAL' if not mism else mism, B['sha256'][:10], [(m['name'],m['status'],m['located_live'],m['located_faces']) for m in B['members']], [(m['name'],m['located_live'],m['located_faces']) for m in A['members']]))
for r in rows: print(r)
print(f"\nfiles field-by-field equal: {same}; differing: {diff}")
print("located faces naming a live workspace face (live/located):")
for name in ("gmsh_prod","gmsh_default","gmsh_alt","netgen_occ"):
    b,a=loc[(name,'before')],loc[(name,'after')]
    print(f"  {name:12s} before {b['live']}/{b['faces']}   after {a['live']}/{a['faces']}")
