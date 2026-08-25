# order16 6+10 audit note

Scheduler evidence queried at: `2026-08-25T06:57:38Z`; live-tail figures retain the preceding bounded pass and are explicitly not completion counts.

## Scheduler

- Array total: **512**
- Slurm terminal COMPLETED: **363**
- RUNNING: **149**
- PENDING: **0**
- TIMEOUT / FAILED / CANCELLED: **0 / 0 / 0**

## Completed-only rigorous audit

- Terminal COMPLETED chunks: **363**
- Terminal chunks actually audited: **24**
- Validated terminal rows: **13,683,672** of **291,917,907** overall (**4.68751%**)
- Audited terminal result: **13,683,672 colorable**
- Invalid terminal chunks in audited scope: **0**
- Malformed JSON / duplicate indices / duplicate hashes / holes in this audited terminal scope: **0 / 0 / 0 / 0**
- Exact timeout rows in audited terminal scope: **0**

### Remaining terminal-completion reconciliation

- Deferred existing-file audit backlog: **191**
- Slurm-terminal IDs with no output file yet (not treated as row-complete): **148**
- Check: 24 audited + 0 invalid + 191 deferred + 148 absent-file IDs = **363**.

## Live append-stable telemetry (not completion)

This section is explicitly provisional and does **not** contribute to completion.

- Present non-terminal chunk files scanned: **149**
- Append-stable files accepted: **149**; unstable/excluded: **0**
- Per-file tail cap: **8,388,608 bytes**; parsed tail bytes: **1,213,160,998**
- Clean validated tail rows: **4,939,723**
- Unique indices: **4,939,723**; duplicate index rows: **0**
- Unique hashes: **4,939,723**; duplicate hash rows: **0**
- Malformed JSON: **0**
- Unresolved timeout rows: **0** (timeouts are never counted as negatives)
- Candidate negatives requiring independent confirmation: **0**
- Observed tail index span: **70,602,460** through **200,704,196**

## Rerun decision

No exact timeout rows were found in either scoped section, so no one-hour rerun was queued.

