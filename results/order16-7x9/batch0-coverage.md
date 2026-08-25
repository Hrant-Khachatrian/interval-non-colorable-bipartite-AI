# Order-16 7+9 Batch 0 Terminal Audit

Updated: `2026-08-25T12:47:07.193132Z`

- Chunk `0` is excluded because its dedicated auditor owns it.
- Scheduler snapshot: terminal completed `8`, running `82`, pending `910`.
- Prior accepted chunks retained without re-audit: `[1, 2]`.
- Newly audited terminal candidates: `[4, 6, 7, 24, 30, 32]`.
- Accepted chunks: `[1, 2, 4, 6, 7, 24, 30, 32]`; accepted rows: `3519896`.
- Rejected terminal chunks from this increment: `[]`.

Per-chunk status:
- Chunk `1`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `2`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `4`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `6`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `7`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `24`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `30`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `32`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.

Evidence: `batch0-terminal-audit.json`.
