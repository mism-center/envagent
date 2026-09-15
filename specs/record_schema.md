# Record schema

Two append-only JSONL files under `outputs/`. `src/record.py` enforces the
required keys and *raises* on a row that would be incomplete: Phase 0 has no
memory, but it is writing the corpus that memory will index later, and a row with
a missing signature or an untyped action is a row nobody can learn from.
Completeness is a correctness property, not logging.

This file plus the two protocols in `src/builder.py` / `src/verifier.py` and the
annotation subset in `src/evidence.py:read_annotation` are the whole contract
surface for the infra team.

## `outputs/attempts.jsonl` — one row per attempt, appended regardless of outcome

```json
{
  "model_id": "mism:model/1a2b3c",
  "job_id": "job-2026-08-27-0041",
  "attempt": 2,

  "install_mode": "mounted",
  "base_image": "python:3.11-slim",
  "base_digest": "sha256:4c9e…",
  "builder_version": "v0.24.0",
  "envspec_hash": "sha256:9f21…",

  "ladder_reached": "L0",
  "failed_rung": "L0",
  "failed_step_index": 3,
  "failed_step_kind": "apt",
  "failed_step_field": "apt_packages",
  "error_signature": "sha256:c0de…",
  "error_raw": "fatal error: libxml/parser.h: No such file…",
  "failure_class": "MISSING_SYSTEM_LIB",
  "classified_by": "rule",

  "action_taken": {"type": "ADD_APT_PKG", "arg": "libxml2-dev", "why": "…"},
  "next_ladder": "L0",

  "duration_s": 184,
  "image_bytes": 1284310528,
  "image_digest": null,
  "code_revision": "9c1f…",
  "outputs_written": [],
  "cache_hits": 0,
  "vertices_total": 0,
  "tokens": 0
}
```

Notes on the load-bearing fields:

- **`error_signature`** — sha256 over normalised salient stderr lines (absolute
  paths, hashes, line numbers, temp dirs, versions and timestamps stripped; see
  `src/normalize.py`). *Nothing in Phase 0 reads it.* Write it anyway — a
  signature that only starts being recorded in Phase 1 has no history to learn
  from, and the normaliser is thirty lines with golden tests.
- **`classified_by`** — `"rule" | "llm"`. This is how you measure whether the
  rule table is winning. Its distribution over the corpus run decides how much of
  Phase 0.5 is just "write more rules".
- **`failed_step_kind` / `failed_step_field`** — free failure attribution: the
  renderer knows which Dockerfile line came from which EnvSpec field.
- **`action_taken`** — backfilled by the `patch` that responds to this row, so
  the failure and its repair sit together. `null` means the job ended here.
- **`cache_hits` / `vertices_total`** — both always `0`: Kaniko builds run with
  `--cache=false` (Phase 0 buys correctness, not speed). The fields stay in the
  row because a row missing them is not comparable with one that has them; when
  a caching builder lands, read them as a fraction (`0/0` total hit, `0/9` total
  miss) with `duration_s` corroborating.
- **`tokens`** — always 0 from the driver: the agent process does the model work,
  not this CLI. Fill it from the harness if you want per-job token accounting.

## `outputs/verdicts.jsonl` — one row per job, on every exit path

```json
{
  "model_id": "mism:model/1a2b3c",
  "job_id": "job-2026-08-27-0041",
  "status": "verified",
  "ladder_reached": "L3",
  "attempts": 3,
  "image_digest": "sha256:8ab1…",
  "image_ref": "registry:5000/envbuild/job-…@sha256:8ab1…",
  "code_revision": "9c1f…",
  "envspec_hash": "sha256:9f21…",
  "failure_class": null,
  "routes_to": "retry",
  "duration_s": 512.4,
  "ended_at": 1756312800.0,
  "reason": "",
  "l4": null,
  "forbidden": [["MISSING_SYSTEM_LIB", "ADD_APT_PKG", "libxml2-dev"]]
}
```

`status` ∈ `verified | failed | escalated | budget_exhausted | error`.

**The approval unit is the pair `(image_digest, code_revision)`, not a single
digest.** Model code is mounted at run time, so a code change invalidates
verification but not the build — which is exactly what makes `envbuild reverify`
cheap: replay L2–L3 against the existing image, skip L0 and L1 entirely.

`l4` is always `null` in Phase 0. The field exists so the schema does not change
when reference-trace comparison lands.
