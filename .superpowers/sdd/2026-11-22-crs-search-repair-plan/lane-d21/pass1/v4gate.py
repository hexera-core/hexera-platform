"""Pass-2 gate: from v4's rows, the dev files whose SEARCH arms (search_det, search_ai) still fail.
A file is solved if any search arm's conclusive row is out_clean. Files with no conclusive search row stay IN."""
import json,glob,sys,collections
rows=[json.loads(l) for p in glob.glob(sys.argv[1]+'/**/rows*.jsonl',recursive=True) for l in open(p) if l.strip()]
solved=set(); seen=collections.defaultdict(set)
for r in rows:
    if r.get('arm') in ('search_det','search_ai','staged'):
        seen[r['sha256']].add(r['arm'])
        if not r.get('inconclusive') and r.get('out_clean'):
            solved.add(r['sha256'])
json.dump(sorted(solved),open(sys.argv[2],'w'))
print('v4 rows',len(rows),'files with a search/staged row',len(seen),'solved by an arm',len(solved))
