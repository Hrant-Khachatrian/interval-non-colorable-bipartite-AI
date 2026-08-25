# Order-16 7+9 Batch 0 Terminal Audit

Updated: `2026-08-25T13:02:41.334801Z`

- Chunk `0` is excluded because its dedicated auditor owns it.
- Scheduler snapshot: terminal completed `17`, running `88`, pending `895`.
- Prior accepted chunks retained without re-audit: `[1, 2, 4, 6, 7, 24, 30, 31, 32]`.
- Newly audited terminal candidates: `[3, 5, 23, 26, 29, 34, 35]`.
- Accepted chunks: `[1, 2, 4, 6, 7, 24, 30, 31, 32, 3, 5, 23, 26, 29, 34, 35]`; accepted rows: `7039792`.
- Rejected terminal chunks from this increment: `[]`.

Per-chunk status:
- Chunk `1`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `2`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `4`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `6`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `7`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `24`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `30`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `31`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `32`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `3`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `5`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `23`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `26`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `29`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `34`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.
- Chunk `35`: **ACCEPTED**; rows `439987/439987`, missing `0`, duplicate indices `0`, malformed/schema `0/0`, timeout `0`, negative `0`, duplicate hashes `0`.

Evidence: `batch0-terminal-audit.json`.
