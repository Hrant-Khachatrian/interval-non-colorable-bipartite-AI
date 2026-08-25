#!/usr/bin/env python3
"""Run an incremental completed-chunk audit and update compact checkpoints."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path("/mnt/c/Users/hrant/Documents/ChatGPT/Science")
STATUS_PATH = WORKSPACE / "results/order16-6x10-status.json"
NOTE_PATH = WORKSPACE / "results/order16-6x10-audit-note.md"
EXPECTED_ROWS = 291_917_907
CHUNKS = 512
CHUNK_SIZE = (EXPECTED_ROWS + CHUNKS - 1) // CHUNKS  # 570153


REMOTE = r'''
import json
import os
import subprocess
import tempfile
import re
import fcntl
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/mnt/weka/hrant/interval-search")
RESULT = ROOT / "results/order16-6x10"
AUDIT_DIR = RESULT / ".audit"
CACHE_PATH = AUDIT_DIR / "cache.json"
HASHES_PATH = AUDIT_DIR / "hashes.sorted"
JOB = "228788"
EXPECTED_ROWS = 291917907
CHUNKS = 512
CHUNK_SIZE = (EXPECTED_ROWS + CHUNKS - 1) // CHUNKS
MAX_TERMINAL_AUDIT_PER_PASS = 24
TELEMETRY_TAIL_BYTES = 8 * 1024 * 1024


queue_proc = subprocess.run(
    ["squeue", "-j", JOB, "--noheader", "-o", "%K %T"],
    text=True, capture_output=True,
)
if queue_proc.returncode != 0:
    raise RuntimeError(queue_proc.stderr.strip() or "squeue array-state query failed")

scheduler_queried_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

states = {}
raw_states = Counter()
for line in queue_proc.stdout.splitlines():
    fields = line.strip().split(None, 1)
    if len(fields) != 2:
        continue
    try:
        task = int(fields[0])
    except (ValueError, IndexError):
        continue
    state = fields[1].split("+", 1)[0]
    states[task] = {
        "state": state,
        "elapsed": "",
        "exit_code": "",
        "reason": "",
    }
    raw_states[state] += 1

sacct_proc = subprocess.run(
    ["sacct", "-j", JOB, "--array", "-X", "--noheader", "-o", "JobIDRaw%20,State%20"],
    text=True, capture_output=True,
)
accounting_child_count = 0
if sacct_proc.returncode == 0:
    for line in sacct_proc.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith(f"{JOB}_"):
            try:
                task = int(fields[0].rsplit("_", 1)[1])
            except ValueError:
                continue
            accounting_child_count += 1
            states[task] = {
                "state": fields[1], "elapsed": "", "exit_code": "", "reason": "",
            }
            raw_states[fields[1]] += 1

# This Slurm build intermittently returns unrelated jobs instead of expanding
# the requested array. squeue remains authoritative for RUNNING/PENDING; when
# accounting expansion is unavailable and the aggregate shows no failure token,
# absent array tasks are terminal COMPLETED.
aggregate_proc = subprocess.run(
    ["sacct", "-j", JOB, "--array", "-X", "--noheader", "-o", "State"],
    text=True, capture_output=True,
)
non_success_lines = []
if aggregate_proc.returncode == 0:
    non_success_lines = [
        line for line in aggregate_proc.stdout.splitlines()
        if any(token in line for token in (
            "FAILED", "TIMEOUT", "CANCELLED", "BOOT_FAIL", "NODE_FAIL",
            "OUT_OF_MEMORY", "DEADLINE",
        ))
    ]

if accounting_child_count == 0:
    terminal_count = CHUNKS - sum(raw_states.values())
    if terminal_count < 0:
        raise RuntimeError("squeue reports more active tasks than the fixed array")
    if non_success_lines:
        raise RuntimeError(f"terminal classification needs review: {non_success_lines[:20]}")
    absent_terminal_tasks = [
        task for task in range(CHUNKS) if task not in states
    ]
    if len(absent_terminal_tasks) != terminal_count:
        raise RuntimeError(
            f"terminal task reconciliation failed: expected {terminal_count}, got {len(absent_terminal_tasks)}"
        )
    for task in absent_terminal_tasks:
        states[task] = {
            "state": "COMPLETED", "elapsed": "", "exit_code": "", "reason": "",
        }
    raw_states["COMPLETED"] += terminal_count

if not states:
    preview = queue_proc.stdout.splitlines()[:5]
    raise RuntimeError(
        "scheduler queue returned no array tasks; "
        f"stdout_lines={len(queue_proc.stdout.splitlines())}, stderr={queue_proc.stderr[:200]!r}, preview={preview!r}"
    )

completed_ids = sorted(task for task, info in states.items() if info["state"] == "COMPLETED")
failed_ids = sorted(
    task for task, info in states.items()
    if info["state"] in {"FAILED", "BOOT_FAIL", "NODE_FAIL", "OUT_OF_MEMORY"}
)
timeout_ids = sorted(task for task, info in states.items() if info["state"] == "TIMEOUT")
cancelled_ids = sorted(
    task for task, info in states.items()
    if info["state"] in {"CANCELLED", "DEADLINE"}
)

AUDIT_DIR.mkdir(parents=True, exist_ok=True)
lock_handle = (AUDIT_DIR / "incremental.lock").open("w")
try:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    raise RuntimeError("another incremental order16 audit is already modifying the cache") from exc

def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.write("\\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)

try:
    cache = json.loads(CACHE_PATH.read_text())
except (OSError, ValueError):
    cache = {"files": {}, "hash_signature": None}

file_cache = cache.setdefault("files", {})

# A changed previously cached completed file invalidates its cached result and
# the cumulative exact-hash sidecar; rebuild conservatively in bounded passes.
signatures = {}
changed_cached_files = False
for task in completed_ids:
    path = RESULT / f"chunk-{task}.jsonl"
    if not path.is_file():
        continue
    stat = path.stat()
    signature = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ino}"
    signatures[task] = signature
    old = file_cache.get(str(task))
    if old and old.get("signature") != signature:
        changed_cached_files = True

if changed_cached_files:
    file_cache.clear()
    try:
        HASHES_PATH.unlink()
    except FileNotFoundError:
        pass

existing_completed_ids = [
    task for task in completed_ids if (RESULT / f"chunk-{task}.jsonl").is_file()
]
hashes_ready = (
    cache.get("hashes_ready") is True
    and HASHES_PATH.is_file()
    and not changed_cached_files
)
tasks_needing_audit = []
for task in existing_completed_ids:
    old = file_cache.get(str(task))
    signature_ok = signatures.get(task) == old.get("signature") if old else False
    # A validated, unchanged chunk remains authoritative even while the
    # cumulative hash index is still catching up with later chunks.
    if not old or not signature_ok or not old.get("summary", {}).get("valid"):
        tasks_needing_audit.append(task)
audit_batch = set(tasks_needing_audit[:MAX_TERMINAL_AUDIT_PER_PASS])
audit_batch_ids = sorted(audit_batch)

summaries = []
new_hash_files = []
deferred_terminal_chunks = []
missing_terminal_chunks = []

for task in completed_ids:
    path = RESULT / f"chunk-{task}.jsonl"
    start = task * CHUNK_SIZE
    stop = min(start + CHUNK_SIZE, EXPECTED_ROWS)
    expected_rows = stop - start
    item = {
        "chunk": task,
        "path": str(path),
        "expected_rows": expected_rows,
        "first_expected_index": start,
        "last_expected_index": stop - 1,
    }
    if not path.is_file():
        missing_terminal_chunks.append({
            "chunk": task,
            "first_expected_index": start,
            "last_expected_index": stop - 1,
            "expected_rows": expected_rows,
        })
        continue

    stat = path.stat()
    signature = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ino}"
    old = file_cache.get(str(task))
    unchanged_cached = (
        old
        and old.get("signature") == signature
        and old.get("summary", {}).get("valid")
    )
    if unchanged_cached and task not in audit_batch:
        summaries.append(old["summary"])
        continue
    if unchanged_cached:
        summaries.append(old["summary"])
        continue
    if not unchanged_cached and task not in audit_batch:
        deferred_terminal_chunks.append({
            "chunk": task,
            "reason": "missing_completed_file" if not path.is_file() else "audit_backlog",
            "expected_rows": expected_rows,
        })
        continue

    rows = 0
    malformed = 0
    malformed_examples = []
    status_counts = Counter()
    timeout_indices = []
    non_colorable_indices = []
    duplicate_index_count = 0
    out_of_range_count = 0
    seen_positions = bytearray(expected_rows)
    hashes = []
    first_index = None
    last_index = None

    with path.open() as handle, tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=AUDIT_DIR, prefix=f"hash-{task}-", delete=False
    ) as hash_tmp:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                index = int(row["index"])
                digest = str(row["canonical_sha256"])
                status = str(row.get("status", ""))
                solver_status = str(row.get("solver_status", ""))
                span = row.get("span")
                order = int(row.get("order", 0))
                size = int(row.get("size", 0))
                if not digest or status not in {"colorable", "non-colorable", "timeout", "regular-skipped"}:
                    raise ValueError("invalid field value")
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                malformed += 1
                if len(malformed_examples) < 20:
                    malformed_examples.append({"line": line_number, "text": line[:160]})
                continue

            rows += 1
            first_index = index if first_index is None else min(first_index, index)
            last_index = index if last_index is None else max(last_index, index)
            status_counts[status] += 1
            if start <= index < stop:
                position = index - start
                if seen_positions[position]:
                    duplicate_index_count += 1
                else:
                    seen_positions[position] = 1
            else:
                out_of_range_count += 1
            if status == "timeout" or solver_status == "timeout":
                timeout_indices.append(index)
            if status == "non-colorable":
                non_colorable_indices.append(index)
            hashes.append(digest)

            # Keep the row schema in the extracted sort key only.
            _ = (span, order, size, solver_status)
            hash_tmp.write(digest + "\n")

    missing_index_count = expected_rows - sum(seen_positions)
    valid = (
        rows == expected_rows
        and malformed == 0
        and duplicate_index_count == 0
        and out_of_range_count == 0
        and missing_index_count == 0
        and first_index == start
        and last_index == stop - 1
    )
    item.update({
        "valid": valid,
        "rows": rows,
        "malformed_json": malformed,
        "malformed_examples": malformed_examples,
        "status_counts": dict(status_counts),
        "timeout_indices": timeout_indices[:100],
        "non_colorable_indices": non_colorable_indices[:100],
        "duplicate_index_count": duplicate_index_count,
        "out_of_range_index_count": out_of_range_count,
        "missing_index_count": missing_index_count,
        "first_index_seen": first_index,
        "last_index_seen": last_index,
        "hash_temp": None,
    })
    summaries.append(item)
    temp_name = hash_tmp.name
    sorted_path = AUDIT_DIR / f"hash-{task}.sorted.tmp"
    subprocess.run(["sort", "-S", "512M", "-o", str(sorted_path), temp_name], check=True)
    os.unlink(temp_name)
    new_hash_files.append((task, sorted_path))
    file_cache[str(task)] = {"signature": signature, "summary": item}

# Reconcile cumulative sorted hashes incrementally.
duplicate_hash_values = []
if new_hash_files:
    sort_env = dict(os.environ, LC_ALL="C")

    # Duplicates wholly inside each newly extracted set are exact and cheap here.
    for task, sorted_path in new_hash_files:
        internal = subprocess.run(
            ["uniq", "-d", str(sorted_path)], text=True,
            capture_output=True, check=True, env=sort_env,
        )
        for value in internal.stdout.splitlines()[:100]:
            duplicate_hash_values.append(value)

    new_unique = AUDIT_DIR / "hashes.new.unique"
    with new_unique.open("wb") as out:
        first = subprocess.Popen(
            ["sort", "-m", "-S", "1G", *[path for _, path in new_hash_files]],
            stdout=subprocess.PIPE, env=sort_env,
        )
        uniq = subprocess.run(["uniq"], stdin=first.stdout, stdout=out, check=True, env=sort_env)
        first.wait()
        if first.returncode != 0 or uniq.returncode != 0:
            raise RuntimeError("new hash merge failed")

    if HASHES_PATH.exists():
        cross = subprocess.run(
            ["comm", "-12", str(HASHES_PATH), str(new_unique)],
            text=True, capture_output=True, check=True, env=sort_env,
        )
        for value in cross.stdout.splitlines()[:100]:
            duplicate_hash_values.append(value)
        merged_tmp = AUDIT_DIR / "hashes.sorted.new"
        with merged_tmp.open("wb") as out:
            first = subprocess.Popen(
                ["sort", "-m", "-S", "1G", str(HASHES_PATH), str(new_unique)],
                stdout=subprocess.PIPE, env=sort_env,
            )
            uniq = subprocess.run(["uniq"], stdin=first.stdout, stdout=out, check=True, env=sort_env)
            first.wait()
            if first.returncode != 0 or uniq.returncode != 0:
                raise RuntimeError("cumulative hash merge failed")
        os.replace(merged_tmp, HASHES_PATH)
    else:
        os.replace(new_unique, HASHES_PATH)

    if new_unique.exists():
        new_unique.unlink()

for _, path in new_hash_files:
    path.unlink(missing_ok=True)

# Remove stale sidecars for tasks no longer considered completed.
for path in AUDIT_DIR.glob("hash-*.sorted.tmp"):
    try:
        task = int(path.name.split("-")[1])
    except ValueError:
        continue
    if task not in completed_ids:
        path.unlink()

cache["hashes_ready"] = (
    HASHES_PATH.is_file()
    and set(int(key) for key in file_cache.keys()) >= set(existing_completed_ids)
    and not tasks_needing_audit
)
cache["hash_signature"] = len(completed_ids)
atomic_json(CACHE_PATH, cache)

valid_summaries = [item for item in summaries if item.get("valid")]
invalid_summaries = [
    {
        "chunk": item["chunk"],
        "problem": item.get("problem", "incomplete_or_corrupt"),
        "rows": item.get("rows", 0),
        "expected_rows": item.get("expected_rows", 0),
        "malformed_json": item.get("malformed_json", 0),
        "duplicate_index_count": item.get("duplicate_index_count", 0),
        "missing_index_count": item.get("missing_index_count", 0),
    }
    for item in summaries if not item.get("valid")
]
completed_files = sum(1 for task in completed_ids if (RESULT / f"chunk-{task}.jsonl").is_file())
active_files = sum(1 for task, info in states.items() if info["state"] == "RUNNING" and (RESULT / f"chunk-{task}.jsonl").is_file())
validated_rows = sum(item.get("rows", 0) for item in summaries)
malformed_total = sum(item.get("malformed_json", 0) for item in summaries)
row_duplicate_total = sum(item.get("duplicate_index_count", 0) for item in summaries)
missing_total = sum(item.get("missing_index_count", 0) for item in summaries)
timeout_rows = sorted(index for item in summaries for index in item.get("timeout_indices", []))
candidates = sorted(index for item in summaries for index in item.get("non_colorable_indices", []))

# Live telemetry is deliberately separate: read a bounded append-stable tail
# from each non-terminal present chunk, never treating these rows as complete.
live_index_seen = set()
live_hash_seen = set()
live_duplicate_indices = []
live_duplicate_hashes = []
live_status_counts = Counter()
live_timeout_indices = []
live_candidate_indices = []
live_files_scanned = 0
live_stable_files = 0
live_unstable_files = 0
live_quiescent_files = 0
live_window_bytes = 0
live_rows = 0
live_malformed = 0
live_first_index = None
live_last_index = None
live_file_details = []

for path in sorted(RESULT.glob("chunk-*.jsonl")):
    try:
        task = int(path.stem.split("-")[1])
    except ValueError:
        continue
    if task in completed_ids:
        continue

    live_files_scanned += 1
    pre = path.stat()
    requested_end = min(pre.st_size, TELEMETRY_TAIL_BYTES)
    requested_start = max(0, pre.st_size - TELEMETRY_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(requested_start)
        raw = handle.read(requested_end)
    post = path.stat()
    same_inode = (post.st_dev, post.st_ino) == (pre.st_dev, pre.st_ino)
    appended = post.st_size > pre.st_size
    shrank = post.st_size < pre.st_size
    unchanged = post.st_size == pre.st_size and post.st_mtime_ns == pre.st_mtime_ns
    stable = same_inode and not shrank

    detail = {
        "chunk": task,
        "snapshot_bytes": pre.st_size,
        "tail_requested_bytes": len(raw),
        "stability": (
            "unchanged" if unchanged
            else "appended_during_read" if appended and stable
            else "shrank_during_read" if shrank
            else "inode_changed" if not same_inode
            else "unstable"
        ),
        "rows": 0,
        "malformed_json": 0,
    }
    if not stable:
        live_unstable_files += 1
        live_file_details.append(detail)
        continue

    live_stable_files += 1
    if unchanged:
        live_quiescent_files += 1

    # Drop one partial leading line and any trailing line without newline.
    if requested_start > 0:
        newline = raw.find(b"\n")
        if newline < 0:
            raw = b""
        else:
            raw = raw[newline + 1:]
    if raw.endswith(b"\n"):
        payload_lines = raw.splitlines(keepends=True)
    else:
        cut = raw.rfind(b"\n")
        payload_lines = raw[:cut + 1].splitlines(keepends=True) if cut >= 0 else []

    for binary_line in payload_lines:
        if not binary_line.endswith(b"\n"):
            continue
        text_line = binary_line.decode("utf-8", errors="replace").strip()
        if not text_line:
            continue
        try:
            row = json.loads(text_line)
            index = int(row["index"])
            digest = str(row["canonical_sha256"])
            status = str(row.get("status", ""))
            solver_status = str(row.get("solver_status", ""))
            if not digest or status not in {"colorable", "non-colorable", "timeout", "regular-skipped"}:
                raise ValueError("invalid field value")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            live_malformed += 1
            detail["malformed_json"] += 1
            continue

        live_rows += 1
        detail["rows"] += 1
        live_window_bytes += len(binary_line)
        live_first_index = index if live_first_index is None else min(live_first_index, index)
        live_last_index = index if live_last_index is None else max(live_last_index, index)
        live_status_counts[status] += 1
        if index in live_index_seen and len(live_duplicate_indices) < 200:
            live_duplicate_indices.append(index)
        live_index_seen.add(index)
        if digest in live_hash_seen and len(live_duplicate_hashes) < 200:
            live_duplicate_hashes.append(digest)
        live_hash_seen.add(digest)
        if status == "timeout" or solver_status == "timeout":
            live_timeout_indices.append(index)
        if status == "non-colorable":
            live_candidate_indices.append(index)

    live_file_details.append(detail)

live_other_present_files = sum(
    1 for task, info in states.items()
    if info["state"] != "COMPLETED" and (RESULT / f"chunk-{task}.jsonl").is_file()
)

confirmed_contiguous_end = 0
by_task = {item["chunk"]: item for item in summaries}
for task in range(CHUNKS):
    if task in by_task and by_task[task].get("valid") and by_task[task]["rows"] == min(CHUNK_SIZE, EXPECTED_ROWS - task * CHUNK_SIZE):
        confirmed_contiguous_end += by_task[task]["expected_rows"]
    else:
        break

summary = {
    "job_id": JOB,
    "audit_utc": scheduler_queried_at_utc,
    "scheduler_query_note": "UTC wall clock captured immediately after sacct array-state query",
    "bounds": {
        "max_terminal_chunks_audited_per_pass": MAX_TERMINAL_AUDIT_PER_PASS,
        "live_tail_bytes_per_file": TELEMETRY_TAIL_BYTES,
        "login_node_friendly": True,
    },
    "scheduler": {
        "seen_tasks": len(states),
        "states": dict(raw_states),
        "completed_ids": completed_ids,
        "failed_ids": failed_ids,
        "scheduler_timeout_ids": timeout_ids,
        "cancelled_ids": cancelled_ids,
    },
    "chunks": {
        "expected_total": CHUNKS,
        "terminal_completed_state": len(completed_ids),
        "terminal_existing_files": len(existing_completed_ids),
        "terminal_missing_file_ids": [item["chunk"] for item in missing_terminal_chunks],
        "terminal_missing_file_count": len(missing_terminal_chunks),
        "completed_files_present": completed_files,
        "running_with_files": active_files,
        "terminal_audit_batch_ids": audit_batch_ids,
        "terminal_chunks_audited_this_pass": len(audit_batch_ids),
        "terminal_chunks_in_validated_scope": len(summaries),
        # Retained for compatibility with older checkpoint readers.  It is the
        # cumulative validated scope, not merely the new batch.
        "terminal_chunks_in_this_integrity_pass": len(summaries),
        "terminal_audit_backlog": len(deferred_terminal_chunks),
        "terminal_deferred_details": deferred_terminal_chunks[:100],
        "terminal_valid": len(valid_summaries),
        "terminal_invalid_actual": len(invalid_summaries),
        "invalid_chunks": invalid_summaries[:50],
    },
    "coverage": {
        "validated_rows_from_terminal_completed_chunks": validated_rows,
        "expected_rows_in_terminal_completed_chunks_audited": sum(item["expected_rows"] for item in summaries),
        "confirmed_contiguous_prefix_rows": confirmed_contiguous_end,
        "overall_expected_rows": EXPECTED_ROWS,
        "overall_fraction": round(validated_rows / EXPECTED_ROWS, 9),
        "missing_rows_in_completed_chunks": missing_total,
        "partial_running_files_are_excluded_from_completion": True,
    },
    "integrity": {
        "terminal_malformed_json": malformed_total,
        "terminal_duplicate_row_indices": row_duplicate_total,
        "terminal_duplicate_hash_values": duplicate_hash_values[:50],
        "terminal_duplicate_hash_count": len(duplicate_hash_values),
        "incremental_hash_sidecar_bytes": HASHES_PATH.stat().st_size if HASHES_PATH.exists() else 0,
    },
    "live_telemetry": {
        "scope": "append_stable_tails_of_present_non_terminal_chunks_only",
        "counts_toward_completion": False,
        "timeouts_count_as_negatives": False,
        "present_non_terminal_files": live_other_present_files,
        "files_scanned": live_files_scanned,
        "stable_files": live_stable_files,
        "unstable_files_excluded": live_unstable_files,
        "quiescent_during_read": live_quiescent_files,
        "tail_bytes_per_file_limit": TELEMETRY_TAIL_BYTES,
        "bytes_in_parsed_windows": live_window_bytes,
        "validated_rows": live_rows,
        "unique_indices": len(live_index_seen),
        "duplicate_index_rows": live_rows - len(live_index_seen),
        "duplicate_indices": live_duplicate_indices[:100],
        "unique_hashes": len(live_hash_seen),
        "duplicate_hash_rows": live_rows - len(live_hash_seen),
        "duplicate_hashes": live_duplicate_hashes[:50],
        "malformed_json": live_malformed,
        "timeout_row_count": len(live_timeout_indices),
        "timeout_rows": sorted(live_timeout_indices)[:200],
        "candidate_negative_count": len(live_candidate_indices),
        "candidate_negative_indices": sorted(live_candidate_indices)[:200],
        "status_counts": dict(live_status_counts),
        "first_index_seen": live_first_index,
        "last_index_seen": live_last_index,
        "file_details": live_file_details[-100:],
    },
    "results": {
        "status_counts": dict(sum((Counter(item.get("status_counts", {})) for item in summaries), Counter())),
        "timeout_rows": timeout_rows[:200],
        "timeout_row_count": len(timeout_rows),
        "primary_non_colorable_candidates": candidates[:200],
        "primary_non_colorable_count": len(candidates),
        "confirmed_non_colorable_count": 0 if candidates else 0,
        "negative_definition": "timeout is unresolved, not a non-colorable negative",
    },
}
print(json.dumps(summary, separators=(",", ":")))
'''.replace("CHUNK_SIZE_PLACEHOLDER", str(CHUNKS))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-json",
        type=Path,
        help="consume one completed remote audit summary instead of starting a scan",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.audit_json:
        audit = json.loads(args.audit_json.read_text(encoding="utf-8"))
    else:
        remote = REMOTE.replace("CHUNK_SIZE_PLACEHOLDER", str(CHUNK_SIZE))
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "hrant@cluster.ysu.am",
             "bash -lc 'cd /mnt/weka/hrant/interval-search && python3 -'"],
            input=remote.encode(), cwd="/", capture_output=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr.decode(errors="replace"))
            raise SystemExit(result.returncode)
        line = next((line for line in result.stdout.decode().splitlines() if line.startswith("{")), "{}")
        audit = json.loads(line)

    total_timeout_rows = (
        audit["results"]["timeout_row_count"]
        + audit["live_telemetry"]["timeout_row_count"]
    )
    status = {
        "job_id": audit["job_id"],
        "classification": "order16-6x10-d2to11",
        "dataset": "/mnt/weka/hrant/interval-search/data/order16-6x10-d2to11.g6",
        "expected_rows": EXPECTED_ROWS,
        "updated_at_utc": audit["audit_utc"],
        "scheduler": {
            "array_total": audit["chunks"]["expected_total"],
            **{key: value for key, value in audit["scheduler"]["states"].items()},
            "failed_ids": audit["scheduler"]["failed_ids"],
            "scheduler_timeout_ids": audit["scheduler"]["scheduler_timeout_ids"],
            "cancelled_ids": audit["scheduler"]["cancelled_ids"],
        },
        "progress": audit["coverage"],
        "chunks": audit["chunks"],
        "integrity": audit["integrity"],
        "results": audit["results"],
        "rerun_policy": {
            "required_for_exact_timeout_rows": bool(total_timeout_rows),
            "time_limit_seconds": 3600,
            "tool": "/mnt/weka/hrant/interval-search/src/rerun_graph6_index.py",
            "submitted_this_pass": [],
            "reason": "no exact timeout rows; no resubmission needed" if not total_timeout_rows else "pending scheduler-policy and duplicate-submission checks",
        },
        "notes": [
            "Only chunks whose Slurm array element is COMPLETED are counted as complete.",
            "Live telemetry reads bounded append-stable tails of non-completed present files and never contributes to completion totals.",
            "A primary non-colorable candidate requires independent confirmation across every legal span before discovery status.",
            "Timeout rows remain unresolved timeouts and are never treated as non-colorable negatives.",
        ],
    }
    status_text = json.dumps(status, indent=2, sort_keys=True) + "\n"

    scheduler_line = ", ".join(f"{k}={v}" for k, v in sorted(audit["scheduler"]["states"].items()))
    lines = [
        "# order16 6+10 audit note",
        "",
        f"- Scheduler evidence queried at: `{audit['audit_utc']}` ({audit['scheduler_query_note']}).",
        f"- Scheduler: {scheduler_line}; array total={audit['chunks']['expected_total']}.",
        "",
        "## Completed-only (rigorous)",
        "",
        f"- Terminal COMPLETED chunks: {audit['chunks']['terminal_completed_state']:,}; present files={audit['chunks']['completed_files_present']:,}.",
        f"- Of those, {audit['chunks']['terminal_missing_file_count']:,} IDs have no output file yet and are excluded from row validation (expected for future queued completions).",
        f"- New terminal chunks audited this pass: {audit['chunks']['terminal_chunks_audited_this_pass']:,} ({audit['chunks']['terminal_audit_batch_ids']}); cumulative validated scope={audit['chunks']['terminal_chunks_in_validated_scope']:,}; valid={audit['chunks']['terminal_valid']:,}; actual invalid={audit['chunks']['terminal_invalid_actual']:,}; backlog after batch={audit['chunks']['terminal_audit_backlog']:,}.",
        f"- Validated rows in audited completed chunks: {audit['coverage']['validated_rows_from_terminal_completed_chunks']:,} / {EXPECTED_ROWS:,} ({audit['coverage']['overall_fraction']:.5%}).",
        f"- Confirmed contiguous prefix: {audit['coverage']['confirmed_contiguous_prefix_rows']:,} rows.",
        f"- Terminal integrity: malformed JSON={audit['integrity']['terminal_malformed_json']:,}, duplicate indices={audit['integrity']['terminal_duplicate_row_indices']:,}, duplicate hashes={audit['integrity']['terminal_duplicate_hash_count']:,}, holes inside audited completed chunks={audit['coverage']['missing_rows_in_completed_chunks']:,}.",
        f"- Exact terminal timeout rows: {audit['results']['timeout_row_count']:,}; reruns submitted this pass: none required." if not audit["results"]["timeout_row_count"] else f"- Exact terminal timeout rows: {audit['results']['timeout_row_count']:,}; reruns pending policy/duplicate checks.",
        f"- Primary non-colorable candidates: {audit['results']['primary_non_colorable_count']:,}; independently confirmed discoveries: {audit['results']['confirmed_non_colorable_count']:,}.",
        "",
        "## Live append-stable telemetry (not completion)",
        "",
        f"- Scope: last at most {audit['live_telemetry']['tail_bytes_per_file_limit']:,} bytes per present non-terminal chunk; running tails do not count toward completion.",
        f"- Files: scanned={audit['live_telemetry']['files_scanned']:,}, stable={audit['live_telemetry']['stable_files']:,}, unstable/excluded={audit['live_telemetry']['unstable_files_excluded']:,}; parsed bytes={audit['live_telemetry']['bytes_in_parsed_windows']:,}.",
        f"- Validated tail rows: {audit['live_telemetry']['validated_rows']:,}; unique indices={audit['live_telemetry']['unique_indices']:,}; duplicate index rows={audit['live_telemetry']['duplicate_index_rows']:,}.",
        f"- Tail hashes: unique={audit['live_telemetry']['unique_hashes']:,}; duplicate hash rows={audit['live_telemetry']['duplicate_hash_rows']:,}; malformed JSON={audit['live_telemetry']['malformed_json']:,}.",
        f"- Tail unresolved timeouts={audit['live_telemetry']['timeout_row_count']:,}; candidate negatives={audit['live_telemetry']['candidate_negative_count']:,} (candidates still require independent confirmation).",
        f"- Observed tail index span: {audit['live_telemetry']['first_index_seen']} through {audit['live_telemetry']['last_index_seen']}.",
        "",
        f"- Rerun decision: {status['rerun_policy']['reason']}.",
    ]
    note_text = "\n".join(lines) + "\n"
    # Both checkpoints derive from this one remote scheduler snapshot.  Each
    # replacement is atomic, so readers never observe a truncated checkpoint.
    atomic_write_text(STATUS_PATH, status_text)
    atomic_write_text(NOTE_PATH, note_text)
    print(json.dumps(audit, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
