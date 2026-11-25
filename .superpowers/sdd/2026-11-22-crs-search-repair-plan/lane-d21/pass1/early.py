import json,glob,collections,sys
rows=[json.load(open(p)) for p in glob.glob(sys.argv[1]+'/*.row.json')]
def origin(w):
    why=w.get('why') or ''
    if w['op']=='run_stage': return 'stage'
    if w['op']=='defeature' and 'on the part' in why: return 'wholepart'
    return 'grid' if why.startswith(('oracle_grid:','oracle_face_grid:')) else 'det'
st=collections.Counter(r['status'] for r in rows); per=collections.Counter(); ex=[]
for r in rows:
    if r['status'] in ('staged_clean','clean_at_input'): per['staged/input clean']+=1; continue
    w=r.get('winners') or []; cw=[x for x in w if x['full']['clean']]
    if not cw: per['passes only' if w else 'nothing touched']+=1; continue
    o={origin(x) for x in cw}
    lab='det had it' if 'det' in o else ('stage' if 'stage' in o else ('whole-part defeature (OFF in arms)' if 'wholepart' in o else 'grid only'))
    lab+=' gamed-only' if all(x.get('gaming') for x in cw) else ''
    per[lab]+=1; ex.append((r['sha256'][:10],lab,sorted({x['op'] for x in cw})))
print(len(rows),'rows',dict(st)); print(dict(per)); [print(' ',e) for e in ex]
