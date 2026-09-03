"""Attempt and verdict emission.

Phase 0 has no memory, but it is writing the corpus that memory will index.
Every attempt row with a missing signature or an untyped action is a row nobody
can learn from later, so completeness is enforced here as a correctness
property, not treated as logging: a row missing a required key raises.

Files: outputs/attempts.jsonl (one row per attempt, appended regardless of
outcome) and outputs/verdicts.jsonl (one row per job, every exit path).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Mirrors specs/record_schema.md. Keep the two in sync.
ATTEMPT_REQUIRED = (
    "model_id", "job_id", "attempt",
    "install_mode", "base_image", "base_digest", "builder_version", "envspec_hash",
    "ladder_reached", "failed_step_index", "failed_step_kind",
    "error_signature", "error_raw", "failure_class", "classified_by",
    "action_taken", "next_ladder",
    "duration_s", "image_bytes", "cache_hits", "tokens",
)
VERDICT_REQUIRED = (
    "model_id", "job_id", "status", "ladder_reached", "attempts",
    "image_digest", "code_revision", "envspec_hash", "failure_class",
    "routes_to", "duration_s", "ended_at", "reason",
)

VALID_STATUS = ("verified", "failed", "escalated", "budget_exhausted", "error")


class RecordError(ValueError):
    """A row that would corrupt the dataset."""


def _check(row: dict, required, kind: str) -> None:
    missing = [k for k in required if k not in row]
    if missing:
        raise RecordError(f"{kind} row missing required keys: {missing}")


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())      # a crashed job must not lose its last row


def append_attempt(outputs_dir: str | Path, row: dict) -> dict:
    row = {"ts": time.time(), **row}
    _check(row, ATTEMPT_REQUIRED, "attempt")
    _append(Path(outputs_dir) / "attempts.jsonl", row)
    return row


def write_verdict(outputs_dir: str | Path, row: dict) -> dict:
    row = {"ended_at": time.time(), **row}
    if row.get("status") not in VALID_STATUS:
        raise RecordError(f"status must be one of {VALID_STATUS}, got {row.get('status')!r}")
    _check(row, VERDICT_REQUIRED, "verdict")
    _append(Path(outputs_dir) / "verdicts.jsonl", row)
    return row


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
