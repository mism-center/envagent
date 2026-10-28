"""Cross-job memory: what verified last time for this model, reused as the first spec.

The loop re-derives every repair per job: viva-tumor-tcell took the same four
attempts in five hold-out passes, and c302 built in pass 4 but not in pass 5
because the agent chose a different first repair. The records already hold the
answer; this module gives it back at `init`.

Rules, so memory cannot become a way to skip verification:

  * only a `verified` verdict is remembered (spec, command, L3 timeout, code
    revision, image digest, lockfile);
  * recall is a *draft*, not a result -- the ladder still climbs from L0, the
    image is rebuilt (or cache-hit) and every rung runs;
  * recall is recorded (`spec_source: memory`, the remembered job id) on the
    init output and the attempt rows, so a pass report can separate
    "verified from memory" from "verified by search";
  * a recalled spec that then fails is simply a failed first attempt: the agent
    patches from there, or `envbuild spec` resets to the evidence draft which
    `init` keeps alongside (`draft_from_evidence`).

Keyed by model_id. The code revision is stored and compared: a changed revision
still recalls (the dependency stack rarely changes with the code) but says so.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

SCHEMA = "envbuild-memory/1"


def _path(outputs_dir: str | Path, model_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id)
    return Path(outputs_dir) / "memory" / f"{safe}.json"


def remember(outputs_dir: str | Path, state: dict, verdict_row: dict) -> Path | None:
    """Store the verified configuration. Only for `verified`; returns the path."""
    if verdict_row.get("status") != "verified":
        return None
    best = state.get("best") or {}
    if not best.get("spec"):
        return None
    entry = {
        "schema": SCHEMA,
        "model_id": state["model_id"],
        "job_id": state["job_id"],
        "run_id": (verdict_row.get("run") or {}).get("run_id"),
        "stored_at": time.time(),
        "code_revision": state.get("code_revision"),
        "spec": best["spec"],
        "command": state.get("command"),
        "entrypoint_source": state.get("entrypoint_source"),
        "l3_timeout_s": best["spec"].get("l3_timeout_s"),
        "image_digest": best.get("image_digest"),
        "lockfile_sha256": best.get("lockfile_sha256"),
        "charged_attempts": verdict_row.get("charged_attempts"),
    }
    p = _path(outputs_dir, state["model_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entry, indent=1, sort_keys=True), encoding="utf-8")
    return p


def recall(outputs_dir: str | Path, model_id: str) -> dict | None:
    p = _path(outputs_dir, model_id)
    if not p.exists():
        return None
    try:
        entry = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if entry.get("schema") != SCHEMA or not isinstance(entry.get("spec"), dict):
        return None
    return entry


def forget(outputs_dir: str | Path, model_id: str) -> bool:
    p = _path(outputs_dir, model_id)
    if p.exists():
        p.unlink()
        return True
    return False
