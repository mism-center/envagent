---
name: envbuild
description: Turn a registered computational model (a git repo plus an approved annotation YAML) into a verified execution environment — a digest-pinned dependency image pushed to a registry, a verification level (L0–L3), and a complete record of every attempt made to get there. Runs a search-with-verification loop: synthesize a structured EnvSpec, build it with BuildKit, climb the verification ladder, classify the failure, apply exactly one typed repair, repeat until verified or out of budget. Use whenever a user asks to build, containerize, dockerize, reproduce, or verify the execution environment for a model or repo — including "make this repo runnable", "build an image for this model", "does this model actually run", "why won't this install", or "reverify this after the code changed". Trusted-corpus repos only.
---

# envbuild

Building an environment is **search with verification**, so the loop is the
product; the Dockerfile is just the current candidate.

You do two things a model is actually needed for — synthesize the first
`EnvSpec`, and pick a repair when the rule table has nothing to say. Everything
else (scanning, rendering, building, ladder-climbing, rule classification,
budgets, records, teardown) is deterministic code in `src/`, driven through one
CLI. Do not reimplement any of it in ad-hoc bash.

## Scope — read this before anything else

**Trusted repos only.** Phase 0 executes model code in verification containers
with no meaningful sandbox. It is for a hand-picked corpus (vivarium-chemotaxis,
MBMM, ~20 others). **Refuse unknown or public sources**: if the user points you
at a repo they did not vouch for, say so and stop. This constraint is not a
formality — it is the reason no isolation layer was built yet.

Out of scope, on purpose: memory across jobs, L4 reference-trace comparison, the
MISM queue, gVisor, ephemeral namespaces, image scanning, in-cluster execution.
Other people own those; this skill leaves the seams clean for them.

## What it produces

1. A dependency image, pushed to the registry and addressed **by digest**.
2. A verification level: L0 (builds) → L1 (deps import) → L2 (code mounts and
   resolves) → L3 (a short run writes outputs). L4 is always null in Phase 0.
3. `outputs/attempts.jsonl` and `outputs/verdicts.jsonl` — one row per attempt,
   one per job, on every exit path.

**The image is a dependency stack, not a model.** Model code is mounted at run
time, not baked in, so many models can share one image. The consequence: the
approval unit is the pair *(image digest, code revision)*.

## Locating the CLI

`src/driver.py` ships **inside this skill's directory**, which is not your
working directory. Your harness tells you where that is (the skill's `location=`
attribute, or a "References are relative to `<dir>`" line). Use that `<dir>`:

```bash
SKILL_DIR=<the directory containing this SKILL.md>
envbuild() { uv run "$SKILL_DIR/src/driver.py" "$@"; }
```

`uv run` resolves the one dependency (PyYAML) inline via PEP 723 — no install
step. If `uv` is unavailable, use `python3` with PyYAML present. Do not `find`
the filesystem for the script; the path is already known.

## Modes

| Mode | Trigger | What you do |
|---|---|---|
| `init` | "set this up", the first half of a run | scan, pick a base, draft a spec |
| `run` | the default — "build an image for this model" | the full loop to a verdict |
| `inspect` | "what happened on job X" | `envbuild inspect`, then read the rows |
| `replay` | "show me the Dockerfile for attempt 2" | `envbuild render`, read `Dockerfile.aN` |

`reverify` is a fifth path, for "the code changed but the deps did not" — it
replays L2–L3 against the existing image and skips the expensive part entirely.

---

## The run workflow

### Step 0 — Preconditions

Confirm the repo is corpus-trusted. Then check the stack is up:

```bash
docker compose -f "$SKILL_DIR/compose.yaml" ps
```

`buildkitd` and `registry` must both be running, and `buildctl` must be on
`PATH`. If you are driving from the **host** (a Claude Code session rather than
the Pi container), both need one-time setup:

```bash
docker run --rm --entrypoint cat moby/buildkit:v0.24.0 /usr/bin/buildctl > ~/.local/bin/buildctl
chmod +x ~/.local/bin/buildctl
docker compose -f "$SKILL_DIR/compose.yaml" -f "$SKILL_DIR/compose.host.yaml" \
  up -d buildkitd registry
export ENVBUILD_BUILDKIT_HOST=tcp://127.0.0.1:1234
```

The base compose file leaves buildkitd unpublished on purpose; `compose.host.yaml`
binds it to loopback only. If any of this is missing, say so — do not try to build
without it.

### Step 1 — `init`

```bash
envbuild init --repo /workspace/repo \
              --annotation /workspace/repo/metadata-package \
              --model-id mism:model/1a2b3c
```

`--annotation` accepts a biomodel-annotator `metadata-package/` directory or a
single YAML. It is optional but changes the quality of everything downstream: it
supplies the entry point, the declared dependencies and the expected outputs.
Without it the loop can still reach L1, but L2/L3 need an entry point.

Read the printed `evidence_summary`, `base_choice`, `annotation`, and
`draft_spec`. Note `job_id` — every later command needs it.

### Step 2 — Synthesize the EnvSpec

**Read `specs/synthesis.md` now.** Write the spec as JSON and install it:

```bash
envbuild spec --job-id "$JOB" --set-spec /tmp/spec.json
```

You emit a structured object, never Dockerfile text. If the draft is already
right, install it unchanged and say so — do not churn it to look busy.

### Step 3 — Attempt

```bash
envbuild attempt --job-id "$JOB"
```

Exit 0 means verified through L3. Non-zero means read the result:

- `failed_rung` — where it broke (`L0` build, `L1` image, `L2` mount, `L3` run)
- `failed_step` — index, kind and **EnvSpec field** of the failing instruction
- `classification` — the rule table's verdict, with `classified_by`
- `suggested_action` — present when a rule resolved both class and argument
- `attempts_left`, `seconds_left`, `forbidden`

### Step 4 — Repair, exactly once

If `suggested_action` is present, apply it directly. Otherwise **read
`specs/repair.md` and `specs/failure_taxonomy.md`**, classify it yourself, and
pick one action from `specs/remediation_actions.md`:

```bash
envbuild patch --job-id "$JOB" --action ADD_APT_PKG --arg libxml2-dev \
               --classified-by llm --failure-class MISSING_SYSTEM_LIB \
               --why "the apt step could not find libxml/parser.h"
```

Then go back to Step 3. **One action per attempt** — the driver refuses a second
patch and logs the discard. The driver also enforces forbidden-triple rejection
and best-so-far rollback; when it rejects something, that is information, not an
obstacle to route around.

### Step 5 — Verdict, always

Every exit path writes a verdict. Do not end a job without one:

```bash
envbuild verdict --job-id "$JOB" --status verified
envbuild verdict --job-id "$JOB" --status failed   --reason "annotation declares no entry point"
envbuild verdict --job-id "$JOB" --status escalated --reason "MATLAB toolchain"
envbuild verdict --job-id "$JOB" --status error    --reason "buildkitd unreachable"
```

`verdict` also tears down: containers, named volumes, pulled images, build cache.
Budget exhaustion writes its own verdict and tears down for you. If anything
crashes mid-run, call `verdict --status error` before you stop.

### Step 6 — Report

Tell the user, in this order:

1. Verified level reached, and the image digest + code revision if any.
2. The attempt-by-attempt path: failure class → action → result. Short.
3. Anything routed back to them (a missing entry point, a licence, missing data).
4. **Any failure you classified because no rule matched** — the stderr pattern,
   the class, the action. That is a candidate rule for `src/classify.py`, and
   growing that table is the main output of the corpus run.

---

## Reference files

Read on demand, not up front:

| File | When |
|---|---|
| `specs/synthesis.md` | Step 2, always |
| `specs/repair.md` | Step 4, whenever a rule did not fully resolve the failure |
| `specs/failure_taxonomy.md` | Step 4, to assign a class |
| `specs/remediation_actions.md` | Step 4, to pick an action and its argument |
| `specs/base_selection.md` | when a base choice or `install_mode` looks wrong |
| `specs/record_schema.md` | when reading `outputs/*.jsonl`, or answering the infra team |

## Rules that are not negotiable

- **Trusted corpus only.** Refuse unknown sources.
- **Never write Dockerfile text.** Emit an `EnvSpec`; the renderer owns the layers.
- **Never bake `ENTRYPOINT`, `CMD`, or model code into a mounted-mode image.**
- **Never pass an existing Dockerfile through.** It is evidence, not a template.
- **Never use a bare tag as a base.** Digest or nothing.
- **One typed action per attempt**, with a `--why`.
- **Every job ends in a verdict.** A job that ends without one is the single
  failure mode that corrupts the dataset.
- **`ADD_PKG` on an L2 import failure requires justification.** If the missing
  module is in `evidence.local_modules`, the mount is wrong and installing a
  same-named PyPI package ships a container that runs the wrong code.

## Available scripts

- **`src/driver.py`** — the whole CLI: `init`, `spec`, `attempt`, `patch`,
  `reverify`, `verdict`, `inspect`, `render`. Run via `uv run`. `--help` on any
  subcommand.
- **`scripts/test_envbuild.py`** — the offline regression suite (renderer, every
  typed action, classifier rules, normaliser goldens). No Docker required. Run it
  after touching anything in `src/`.
