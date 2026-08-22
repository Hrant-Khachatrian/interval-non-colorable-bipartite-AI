#!/usr/bin/env python3
import subprocess
from interval_edge_coloring import Graph,from_graph6,rank_potential_solve,weighted_hub_statistics

p=subprocess.run(["/usr/bin/nauty-genbg","-q","-g","-c","-l","5","5","14:14"],text=True,capture_output=True)
checked=0;neg=[]
for number,line in enumerate(p.stdout.splitlines()):
    n,edges=from_graph6(line)
    # genbg -c does not guarantee that the requested side sizes appear in
    # input order. The generated connected graph has a unique bipartition,
    # so derive it instead of assuming the first five labels are one side.
    names=[f"V{i}" for i in range(n)]
    g=Graph(names,[(names[i],names[j]) for i,j in edges])
    deg=g.degrees
    checked+=1
    r=rank_potential_solve(g,5,8)
    print("check",number,g.n,g.m,max(deg.values()),min(deg.values()),r.status,r.span,flush=True)
    if r.status=="non-colorable":
        neg.append((g,r))
for i,(g,r) in enumerate(neg):
    g.save(f"results/graphs/record-search-hit-{i}.json")
print("checked",checked,"negative",len(neg))
