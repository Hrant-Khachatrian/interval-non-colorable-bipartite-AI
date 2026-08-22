#!/usr/bin/env python3
"""Random search for hub-incidence designs satisfying the weighted obstruction."""
import argparse,random
def evaluate(k, sets):
    rows=[[] for _ in range(k)]
    for s in sets:
        if not 2<=len(s)<=10: return None
        for v in s: rows[v].append(s)
    if any(not r for r in rows): return None
    deg=[1+len(r) for r in rows]
    if max(deg)>10 or max(map(len,sets))>10:return None
    dist=[deg[i]-1 for i in range(k)]
    changed=True
    while changed:
        changed=False
        for s in sets:
            ws=[deg[v]-1 for v in s]
            base=min(dist[v] - ws[i] for i, v in enumerate(s))
            for i,v in enumerate(s):
                nd=base+ws[i]
                if nd<dist[v]:dist[v]=nd;changed=True
    diameter=max(dist[a] for a in range(k) for b in range(k) if a!=b)
    return k-1-diameter,diameter,dist,deg,[len(s) for s in sets]
p=argparse.ArgumentParser();p.add_argument("--trials",type=int,default=1000000);a=p.parse_args()
rng=random.Random(20260823);best=-99
for k in [7,8,9,10]:
  for b in range(3,25):
   for t in range(a.trials):
    sets=[tuple(sorted(rng.sample(range(k),rng.choice([2,3,3,4,4,5])))) for _ in range(b)]
    out=evaluate(k,sets)
    if out and out[0]>best:
     best=out[0];print("BEST",k,b,out,sets,flush=True)
    if out and out[0]>0:
     print("HIT",k,b,out,sets);raise SystemExit
print("DONE",best)
