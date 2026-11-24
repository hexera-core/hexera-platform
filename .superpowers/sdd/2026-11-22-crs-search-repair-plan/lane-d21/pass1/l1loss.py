import json,glob,collections,sys
tot=collections.Counter(); lost=collections.Counter(); files=collections.defaultdict(set)
for p in glob.glob(sys.argv[1]+'/*.jsonl'):
    for line in open(p):
        try: c=json.loads(line)
        except ValueError: continue
        if 'valid_after' not in c: continue
        op=c['op']
        why=c.get('why') or ''
        if op=='defeature': op+=':'+(c['params'].get('source') or '?')+(':part' if 'on the part' in why else '')
        if op=='conformalise': op+=':'+str(c['params'].get('mode','')).split(':')[0]
        if op=='run_stage': op+=':'+c['params'].get('stage','')
        if why.startswith(('oracle_grid:','oracle_face_grid:')): op+=' [grid]'
        tot[op]+=1
        if c.get('lost_layer1'): lost[op]+=1; files[op].add(p.split('/')[-1][:10])
print(f"{'op':42s} judged  lost_L1  share  files")
for op,n in sorted(tot.items(), key=lambda kv:-lost[kv[0]]):
    if lost[op] or n>=20: print(f"{op:42s} {n:6d} {lost[op]:8d} {lost[op]/n:6.0%} {len(files[op]):5d}")
print('total judged',sum(tot.values()),'lost',sum(lost.values()))
