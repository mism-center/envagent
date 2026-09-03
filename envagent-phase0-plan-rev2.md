# `/envbuild` — Phase 0 Implementation Plan (rev 2)

An agent that turns a registered model into a verified execution environment, and tells you honestly when it can't.

---

## 0. Scope

**In.** One model at a time. Input is an approved annotation YAML plus a source repo. Output is a dependency image pushed to a registry, a verification level, and a record of every attempt made to get there.

**Out.** Memory, L4 reference-trace comparison, the MISM queue, gVisor, ephemeral namespaces, image scanning, remote/in-cluster execution. Other people own those. This plan's job is to get the loop right and leave clean seams.

**Corpus.** Trusted repos only — vivarium-chemotaxis, MBMM, and ~20 hand-picked others. Phase 0 executes untrusted code with no meaningful sandbox. It must not become the thing that accepts public submissions.

**The one-sentence thesis.** Building an environment is search with verification, so the loop is the product; the Dockerfile is just the current candidate.

---

## 1. What changed in rev 2

Three decisions from the design discussion reshape the plan:

1. **Standalone buildkitd + a local registry**, not the embedded builder. Registry cache export needs it, structured progress is native to it, and it's the topology the infra team will deploy.
2. **Model code is mounted at run time, not baked into the image.** The image is a dependency stack; many models can share one.
3. **Everything is digest-pinned** — base images, the builder itself. "Verified" has to mean something six months from now.

Consequence of (2) worth stating up front: the approval unit is no longer a single digest. It is the pair *(image digest, code revision)*. A code change invalidates verification but not the build — which gives you a re-verification path that skips the expensive part entirely.

---

## 2. Local topology

Four containers in one compose file. Everything the agent does goes through a network endpoint, never a host path.

```
┌─ agent ──────────────┐   gRPC    ┌─ buildkitd ─────────┐
│ pi-agent skill       │──────────▶│ moby/buildkit:v0.2x │
│ buildctl, docker CLI │           │ (pinned)            │
└──────┬───────────────┘           └──────────┬──────────┘
       │                                      │ push
       │ /var/run/docker.sock                 ▼
       │ (verification runs only)      ┌─ registry:2 ─┐
       ▼                               │ local        │
┌─ dockerd (host) ─────┐    pull       └──────┬───────┘
│ runs verify containers│◀────────────────────┘
└───────────────────────┘
        │ mounts
        ▼
   named volume: code-<job_id>
```

**Why the local registry.** buildkitd's output doesn't land in dockerd's image store, so verification can't `docker run` it directly. A registry in the stack gives you digest-addressed handoff, a cache backend that behaves like Docker Hub, and no pull-rate exposure during development. Swapping in Hub later is a config change, not a code change.

**Why the named volume.** dockerd resolves mount paths on the *host*, not inside the agent container. The agent populates a daemon-managed named volume via `put_archive`, and the verification container mounts it. No host paths anywhere — and it's the same shape as the PVC the infra team will use in-cluster.

**Pin the builder.** `moby/buildkit:latest` is a build agent that changes underneath you without a commit. You are building a dataset whose entire value is that attempt records are comparable across runs; if buildkitd's error text or vertex semantics shift mid-corpus, your error signatures split and you won't know why. Pin it, record the version in every attempt, bump deliberately.

---

## 3. `EnvSpec` — the central object

**The agent produces a structured spec, never Dockerfile text.**

This is the decision everything else depends on. If the Dockerfile is a string, a repair means regex surgery or a full rewrite — and a rewrite has no reusable unit, which kills the memory story before it starts. As a structured object, every typed action is a trivial mutation and rendering is deterministic code with unit tests.

```python
@dataclass(frozen=True)
class MountContract:
    code_path:   str = "/model"      # where source is mounted at run time
    input_path:  str = "/inputs"
    output_path: str = "/outputs"
    workdir:     str = "/model"
    extra_path:  list[str] = ()      # PYTHONPATH / R_LIBS additions

@dataclass
class EnvSpec:
    schema_version:  str
    install_mode:    Literal["mounted", "installed"]
    base_image:      str             # "python:3.11-slim"
    base_digest:     str             # "sha256:…" — resolved once, recorded
    apt_packages:    list[str]
    pkg_manager:     Literal["pip", "conda", "mamba", "renv", "pkg"]
    pkg_specs:       list[str]       # ["numpy<2", "tellurium==2.2.10"]
    pre_install:     list[str]
    post_install:    list[str]
    env_vars:        dict[str, str]
    mount:           MountContract
    entrypoint:      list[str]       # recorded, NOT baked in mounted mode
    builder_version: str
```

What this buys:

| | |
|---|---|
| **Trivial repairs** | `ADD_APT_PKG` appends to a list. `PIN_PKG` rewrites one element. No parsing. |
| **Deterministic layers** | The renderer decides layer order, not the LLM. Cache hits become predictable across models sharing a base. |
| **Free failure attribution** | The renderer knows which Dockerfile line came from which field. A failing step index maps straight back to `apt_packages` vs `pkg_specs`. |
| **Stable hashing** | Hash the spec, not the text. Whitespace stops looking like a new attempt. |
| **Narrow blast radius** | The LLM never emits raw shell into layers you control. |

### `install_mode` — the escape hatch

Most models are `mounted`: a pure dependency image, code arrives at run time. But some repos genuinely entangle installation and source — `pip install -e .`, compiled extensions, anything with a build step. Forcing those into the mounted model produces weird failures in the tail.

So make it a declared field. Base selection guesses it from evidence (`ext_modules` in `setup.py`, a `Makefile`, `.pyx` files, `src/` with C sources), and `SWITCH_INSTALL_MODE` is a legitimate typed action when the guess is wrong.

### Rendering

Fixed layer order, chosen for cache reuse:

1. `FROM {base_image}@{base_digest}`
2. `ENV` — static vars, sorted
3. apt — sorted and deduped, single `RUN` with `--mount=type=cache`
4. package manager bootstrap
5. `pkg_specs` — declared order preserved, deduped, with a cache mount
6. `pre_install` / `post_install`
7. `mkdir -p` the mount points, `WORKDIR`
8. *(installed mode only)* `COPY` + install step

**No `ENTRYPOINT` in mounted mode.** The image is a stack, not a model. The entrypoint belongs to the model record and is supplied at `docker run` time. This is what lets one image serve many models.

Use `# syntax=docker/dockerfile:1` and heredocs — you will be reading generated Dockerfiles constantly during the corpus run, and readability is worth the line.

---

## 4. Base image selection and digest pinning

A small rule table, not a classifier. A table you can read beats a model you can't debug at this stage.

```
python evidence     → python:3.11-slim
R / DESCRIPTION     → rocker/r-ver:4.4.1
julia               → julia:1.10
conda env file      → mambaorg/micromamba:1.5
unknown             → ubuntu:24.04
```

Then **resolve the tag to a digest once, at synthesis time, and store both.** `EnvSpec.base_image` must never be a bare tag in the rendered Dockerfile. Otherwise a model that verified in March silently stops verifying in June, and your "verified" status is a lie — the same digest-binding argument as the approval gate, pushed one layer down.

---

## 5. The verification ladder, with mounting

Mounting improves the ladder, because it splits image correctness from code correctness cleanly.

| Rung | Test | What a failure means |
|---|---|---|
| **L0** | Image builds and pushes | Build problem |
| **L1** | Interpreter starts; declared deps import — **image alone, no code mounted** | Image problem |
| **L2** | Code mounts; the model artifact parses/loads | Mount or code problem |
| **L3** | A short run completes and writes to `output_path` | Runtime problem |
| **L4** | Output matches a reference trace | *Schema field exists; always null in Phase 0* |

The L1/L2 boundary is the useful new thing. An L1 failure is unambiguously the agent's fault and the agent should repair it. An L2 failure is a mount contract issue or a genuine code problem, and those need different actions.

It also gives you the cheap re-verification path: when only the code changes, replay L2 and L3 against the existing image digest. No rebuild, no L0, no L1.

Run L1–L3 with **network disabled**. A model that only "runs" with live network access has not been verified.

---

## 6. Failure taxonomy

Lives in `specs/failure_taxonomy.md`, not in code — it will churn constantly for the first few hundred repos.

| Class | Detected at | Typical repair | Routes to |
|---|---|---|---|
| `MISSING_SYSTEM_LIB` | L0 | `ADD_APT_PKG` | agent retry |
| `DEP_RESOLUTION_CONFLICT` | L0 | `UNPIN_PKG`, `SWITCH_INSTALLER` | agent retry |
| `COMPILE_ERROR` | L0 | `PIN_PKG`, `CHANGE_INTERPRETER_VERSION`, `ADD_APT_PKG` | agent retry |
| `MISSING_DEPENDENCY` | L1/L2 | `ADD_PKG` | agent retry |
| `IMPORT_PATH_ERROR` | L2 | `FIX_MOUNT_CONTRACT`, `SET_ENV_VAR` | agent retry |
| `ABI_MISMATCH` | L1 | `PIN_PKG`, `CHANGE_BASE_IMAGE` | agent retry |
| `MOUNT_CONTRACT_ERROR` | L2/L3 | `FIX_MOUNT_CONTRACT` | agent retry |
| `BUILD_MODE_MISMATCH` | L2 | `SWITCH_INSTALL_MODE` | agent retry |
| `ENTRYPOINT_UNKNOWN` | L2 | — | back to submitter |
| `MISSING_DATA_FILE` | L3 | — | back to submitter |
| `LICENSE_REQUIRED` | L0/L3 | — | back to submitter |
| `UNSUPPORTED_TOOLCHAIN` | L0 | — | dead-letter |
| `TIMEOUT` / `RUNTIME_ERROR` | L3 | `SET_ENV_VAR`, `ESCALATE` | dead-letter |

Splitting the old `MISSING_MODULE` into `MISSING_DEPENDENCY` and `IMPORT_PATH_ERROR` matters specifically because of mounting. Conflating them sends the agent installing packages to fix a mount bug — it will "succeed" by shadowing the real problem, and you'll ship a container that runs the wrong code.

**Action enum:** `ADD_APT_PKG`, `ADD_PKG`, `PIN_PKG`, `UNPIN_PKG`, `CHANGE_INTERPRETER_VERSION`, `CHANGE_BASE_IMAGE`, `SWITCH_INSTALLER`, `SWITCH_INSTALL_MODE`, `SET_ENV_VAR`, `ADD_PRE_INSTALL_CMD`, `FIX_MOUNT_CONTRACT`, `ESCALATE`.

---

## 7. Classification is mostly rules

Do not send every stderr to the LLM. Most build errors are regex-matchable, and a rule table is faster, free, deterministic, and testable.

```
r"fatal error: (\S+\.h): No such file"           → MISSING_SYSTEM_LIB
r"ResolutionImpossible|conflict is caused by"    → DEP_RESOLUTION_CONFLICT
r"error: command '.*(gcc|g\+\+)' failed"         → COMPILE_ERROR
r"ModuleNotFoundError: No module named '(\S+)'"  → MISSING_DEPENDENCY | IMPORT_PATH_ERROR
r"undefined symbol:|GLIBCXX_\d"                  → ABI_MISMATCH
r"there is no package called '(\S+)'"            → MISSING_DEPENDENCY   # R
r"Permission denied: '/outputs"                  → MOUNT_CONTRACT_ERROR
```

Two refinements worth building in from the start:

**Rules extract arguments too.** `libxml/parser.h` → `libxml2-dev` via a small header→package map makes the repair fully deterministic. A meaningful share of Phase 0 repairs should never reach the LLM.

**Disambiguate by rung.** The same `ModuleNotFoundError` means `MISSING_DEPENDENCY` at L1 (image lacks it) and probably `IMPORT_PATH_ERROR` at L2 (the module is the model's own, and the mount is wrong). Pass the rung into the classifier.

LLM classification is the fallback when nothing matches — and every fallback that fires is a candidate rule to add. Growing this table is the main *output* of the corpus run, not just a means to it.

---

## 8. Loop invariants

These separate a loop that converges from one that thrashes.

- **Retain best-so-far.** Track the highest rung reached and the spec that reached it. If a repair *lowers* the rung, roll back and require a different action. Without this, one bad `CHANGE_BASE_IMAGE` discards four attempts of progress.
- **Forbid repeated `(failure_class, action, arg)` triples.** A per-job forbidden set. This is episodic memory — it dies with the job, so it doesn't break the no-memory constraint, and it's the single largest reducer of wasted attempts.
- **One action per attempt.** If the LLM returns two, take the first and log the discard. Multi-action repairs make attribution impossible, and attribution is the whole point of the record.
- **Every exit path writes a verdict.** Success, timeout, crash, budget exhaustion. A job that ends without a record is the one failure mode that corrupts the dataset.
- **Teardown in `finally`.** Containers, images, named volumes, failed-attempt cache. On a laptop this fills the disk in an afternoon otherwise.

**Budgets:** 5 attempts · 20 min wall clock total · 10 min per build · 3 min per verify · image size ceiling · context tar size ceiling.

---

## 9. The record

One row per attempt, appended regardless of outcome.

```json
{
  "model_id": "mism:model/1a2b3c",
  "job_id": "job-2026-08-27-0041",
  "attempt": 2,

  "install_mode": "mounted",
  "base_image": "python:3.11-slim",
  "base_digest": "sha256:4c9e…",
  "builder_version": "buildkit-v0.24.0",
  "envspec_hash": "sha256:9f21…",

  "ladder_reached": "L0",
  "failed_step_index": 3,
  "failed_step_kind": "apt",
  "error_signature": "sha256:c0de…",
  "error_raw": "fatal error: libxml/parser.h: No such file…",
  "failure_class": "MISSING_SYSTEM_LIB",
  "classified_by": "rule",

  "action_taken": {"type": "ADD_APT_PKG", "arg": "libxml2-dev"},
  "next_ladder": "L3",

  "duration_s": 184,
  "image_bytes": 1284310528,
  "cache_hits": 7,
  "tokens": 0
}
```

The normalizer producing `error_signature` — strip absolute paths, hashes, line numbers, temp dirs, version strings, timestamps — is about thirty lines and is the load-bearing piece of the future memory layer. **Write it in Phase 0 even though nothing reads it.** Give it golden tests against real stderr blobs; it's small enough to deserve them and important enough to need them.

`classified_by: "rule" | "llm"` is how you measure whether the rule table is winning.

---

## 10. Layout

Following `/annotate` conventions — spec files are the source of truth, generated scripts in `scripts/`, records in `outputs/`.

```
envbuild/
  SKILL.md                    # modes: init | run | inspect | replay
  config.ini
  compose.yaml                # agent + buildkitd + registry
  specs/
    failure_taxonomy.md
    remediation_actions.md
    base_selection.md
    synthesis.md              # system prompt → EnvSpec
    repair.md                 # system prompt → one typed action
  src/
    envspec.py                # dataclass, renderer, hash
    evidence.py               # non-LLM repo scan → evidence.json
    baseselect.py             # evidence → base + digest + install_mode
    synth.py                  # LLM call 1
    classify.py               # rules → LLM fallback
    repair.py                 # LLM call 2
    patch.py                  # apply action to EnvSpec
    builder.py                # ← seam: Builder protocol
    verifier.py               # ← seam: Verifier protocol
    ladder.py                 # L0–L3
    normalize.py              # stderr → signature
    record.py                 # attempt + verdict emission
    driver.py                 # loop, budgets, teardown
  fixtures/                   # tiny repos, one per failure class
  outputs/
    attempts.jsonl
    verdicts.jsonl
```

---

## 11. Seams for the infra team

Two protocols, defined now so they can build in parallel. Note these are **separate** — build and verify are different pods with different isolation requirements, and separating them in Phase 0 is what makes gVisor-on-execution a drop-in later.

```python
class Builder(Protocol):
    def build(self, spec: EnvSpec, context_tar: bytes,
              timeout_s: int) -> BuildResult: ...
    def cleanup(self, job_id: str) -> None: ...

class Verifier(Protocol):
    def stage_code(self, job_id: str, code_tar: bytes) -> str: ...   # → volume ref
    def run(self, image_digest: str, code_ref: str, cmd: list[str],
            mount: MountContract, timeout_s: int,
            network: bool = False) -> RunResult: ...
    def fetch_outputs(self, job_id: str) -> bytes: ...
    def cleanup(self, job_id: str) -> None: ...

@dataclass
class BuildResult:
    ok: bool
    image_digest: str | None
    image_bytes: int | None
    failed_step_index: int | None    # → maps back to EnvSpec field
    failed_step_kind: str | None     # "apt" | "pkg" | "cmd" | "copy"
    stderr: str
    duration_s: float
    cache_hits: int
```

Phase 0 ships `LocalBuildKitBuilder` and `LocalDockerVerifier`. They ship the in-cluster versions. `driver.py` never changes.

**Insist on `failed_step_index`.** Without it the classifier sees a wall of text; with it, the repair prompt gets "the apt layer failed, here are the packages in that layer" and the action space collapses to something tractable. `buildctl --progress=rawjson` gives per-vertex events natively.

Also hand them now: the attempt and verdict JSON schemas, and the annotation-YAML subset the agent reads (entrypoint, language, declared deps, expected outputs, expected inputs). Those five contracts are everything they need.

---

## 12. Test with fixtures, not real repos

Handcraft one tiny repo per failure class, each triggering exactly one, deterministically, in under thirty seconds. A `setup.py` needing `libxml2-dev`. A `requirements.txt` with an unsatisfiable pin. A script importing something undeclared. A package with a C extension that only works in `installed` mode. A repo with no entrypoint. A script writing to a path outside `output_path`.

This is the only way to get a regression suite that runs in CI, and it's how you develop the loop without waiting on multi-minute real builds. **Real repos are for milestone 9, not for development.**

---

## 13. Milestones

1. **Compose skeleton.** Agent, pinned buildkitd, local registry. Hello-world: build → push → pull → run → teardown. Prove no host path leaks anywhere, prove reclamation is complete.
2. **`envspec.py` + renderer + `patch.py`.** No LLM, no Docker. Pure unit tests. Every typed action has a test asserting the rendered diff.
3. **`evidence.py` + `baseselect.py`.** Include the CI-workflow extractor — a passing `test.yml` is a *verified* install sequence on a known OS, the highest-value file in most repos. Digest resolution lands here.
4. **`builder.py`.** rawjson parsing, `failed_step_index` attribution proven against a deliberately broken spec.
5. **`normalize.py` + rule-based `classify.py`** with golden tests.
6. **`verifier.py` + `ladder.py`.** L0–L3 with named-volume code staging and network disabled.
7. **`synth.py` + `repair.py`.** The loop closes. One fixture, end to end.
8. **All fixtures green.** Every class either repaired or correctly routed.
9. **Corpus run.** ~20 real repos. Then read the histogram.

Milestones 2 and 3 need no Docker at all — they can proceed while compose is still being sorted.

---

## 14. What milestone 9 is actually for

The failure-class histogram decides Phase 0.5, and it should be the only thing that does:

- Dense `DEP_RESOLUTION_CONFLICT` on older repos → build date-bounded dependency resolution (resolve against the paper's date, not today's index).
- Dense `MISSING_SYSTEM_LIB` clustering on a few solvers → build curated stacks for them.
- Dense `classified_by: "llm"` → the rule table is under-built; that's the cheapest possible win.
- Dense `BUILD_MODE_MISMATCH` → the install-mode heuristic needs work.

Do not decide this in advance. The point of Phase 0 is to *generate* the evidence for that decision.

---

## 15. Known risks

| Risk | Mitigation |
|---|---|
| `--mount=type=cache` contention under concurrency | Sequential in Phase 0; note it now because the symptom is a flaky failure that will look like a model bug |
| Agent "fixes" an `IMPORT_PATH_ERROR` by installing a same-named package from PyPI | Rung-aware classification; on L2 import failures prefer mount repairs and require explicit justification for `ADD_PKG` |
| Existing Dockerfiles in repos anchor synthesis to rotten choices | Feed as evidence, never pass through; require a fresh `EnvSpec`. Revisit once you can measure it |
| Wrong entrypoint in the annotation burns the whole budget | Cheap pre-L2 existence check on the declared entrypoint; fail fast to `ENTRYPOINT_UNKNOWN` |
| Context tars carrying hundreds of MB of result data | Ignore-list plus a hard size ceiling at evidence time |
| Phase 0 drifting into accepting real submissions | Keep the trusted-corpus constraint written into `SKILL.md` and refuse unknown sources |

---

## 16. The two things that make or break this

**The seam quality.** `Builder` and `Verifier` are the difference between the infra team's work being a swap and being a rewrite. Get them right in milestone 1, before there's any pressure to cut corners.

**The record discipline.** Phase 0 has no memory, but it is writing the corpus that memory will index. Every attempt record with a missing signature or an untyped action is a row you can't learn from later. Treat record completeness as a correctness property, not as logging.
