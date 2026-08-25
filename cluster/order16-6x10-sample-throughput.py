import subprocess,re,json,statistics
from pathlib import Path
root=Path("/mnt/weka/hrant/interval-search/results/order16-6x10")
out=subprocess.check_output(["sacct","-j","228788","--format=JobID,State,Elapsed","-P","-S","2026-08-20"],text=True)
el={}
for line in out.splitlines():
 p=line.strip().split("|")
 if len(p)<3 or p[0].endswith(".batch") or "_" not in p[0] or not p[0].split("_")[1].isdigit(): continue
 tid=int(p[0].split("_")[1])
 if p[1]=="COMPLETED":
  h,m,s=map(int,p[2].split(":")); el[tid]=h*3600+m*60+s
files=list(root.glob("chunk-*.jsonl"))
avg_line=6422193954/291917907
chunk_rows=570153
completed=sorted(tid for tid in el if (root/f"chunk-{tid}.jsonl").exists())
samples=[]
for tid in completed[::max(1,len(completed)//12)][:12]:
 f=root/f"chunk-{tid}.jsonl"
 rows=sum(1 for _ in f.open(errors="replace"))
 start=tid*chunk_rows; expected=min(chunk_rows,291917907-start)
 elapsed=el[tid]; size=f.stat().st_size
 samples.append(dict(task=tid,elapsed_h=round(elapsed/3600,3),rows=rows,size_gib=round(size/2**30,3),bytes_per_row=round(size/max(rows,1),2),rows_per_h=round(rows/(elapsed/3600),1),expected_rows=expected,start=start,skip_gib=round(start*avg_line/2**30,3),read_gib_h=round((start*avg_line+size)/(elapsed/3600)/2**30,1)))
summary=dict(sample_count=len(samples),completed_jobs=len(el),files_present=len(files),avg_elapsed_h=round(statistics.mean(x["elapsed_h"] for x in samples),3),max_elapsed_h=max(x["elapsed_h"] for x in samples),mean_bytes_per_row=round(statistics.mean(x["bytes_per_row"] for x in samples),2),mean_rows_per_h=round(statistics.mean(x["rows_per_h"] for x in samples),1),median_rows_per_h=round(statistics.median(x["rows_per_h"] for x in samples),1),samples=samples)
print(json.dumps(summary,sort_keys=True))
