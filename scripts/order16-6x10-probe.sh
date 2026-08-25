#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' '---shell---'
ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
command -v sacct
sacct -j 228788 --array -X -n | head -5
sacct -j 228788 --array -X --noheader --parsable2 -o JobIDRaw,State,Elapsed,ExitCode,Reason%40 | head -5
printf 'stdin_type='
[ -t 0 ] && echo tty || echo pipe

python3 - <<'PY'
import subprocess
p = subprocess.run(['sacct', '-j', '228788', '--array', '-X', '-n'], text=True, capture_output=True)
print('returncode=', p.returncode)
print('stdout_lines=', len(p.stdout.splitlines()))
print('stdout_head=', repr(p.stdout[:200]))
print('stderr=', repr(p.stderr[:500]))
PY
REMOTE

printf '%s\n' '---python-remote---'
printf '%s\n' 'import subprocess,json' 'p=subprocess.run(["sacct","-j","228788","--array","-X","-n"],text=True,capture_output=True)' 'print(json.dumps({"returncode":p.returncode,"lines":len(p.stdout.splitlines()),"head":p.stdout[:200],"stderr":p.stderr[:500]}))' | ssh -o BatchMode=yes hrant@cluster.ysu.am "bash -lc 'cd /mnt/weka/hrant/interval-search && python3 -'"
