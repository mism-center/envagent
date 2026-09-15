# envagent — `envbuild`

An agent that turns a registered model into a **verified execution environment**,
and tells you honestly when it can't.

Given a source repo and (ideally) an approved annotation YAML, `envbuild`
synthesises a structured build spec, builds it with Kaniko, climbs a
verification ladder, classifies whatever broke, applies exactly one typed repair,
and repeats — writing a complete record of every attempt along the way.

> **Phase 0 is for trusted repos only.** Verification containers execute model
> code with no meaningful sandbox. Do not point this at public submissions.

Implements [`envagent-phase0-plan-rev2.md`](envagent-phase0-plan-rev2.md).

---

## The one-sentence thesis

Building an environment is **search with verification**, so the loop is the
product; the Dockerfile is just the current candidate.

Three consequences shape everything:

1. **The agent emits an `EnvSpec`, never Dockerfile text.** Every typed repair is
   a one-field mutation, rendering is deterministic code with unit tests, and a
   failing build step maps back to the field that caused it.
2. **Model code is mounted at run time, not baked in.** The image is a dependency
   stack; many models can share one. The approval unit is therefore the pair
   *(image digest, code revision)* — a code change invalidates verification but
   not the build, which makes re-verification cheap.
3. **Everything is digest-pinned** — base images and the builder itself.
   "Verified" has to still mean something six months from now.

## The verification ladder

| Rung | Test | A failure means |
|---|---|---|
| **L0** | image builds and pushes | build problem |
| **L1** | declared deps import — **image alone, no code mounted** | image problem, unambiguously ours |
| **L2** | code mounts; the entry point's imports resolve | mount or code problem |
| **L3** | a short run completes and writes to `output_path` | runtime problem |
| **L4** | output matches a reference trace | *schema field only; always null in Phase 0* |

L1–L3 run with **network disabled**. A model that only "runs" with live network
access has not been verified.

## Layout

```
SKILL.md              the skill: modes init | run | inspect | replay
config.ini            endpoints, budgets, pinned versions
deploy/               namespace, ServiceAccount, Role, PVC, NetworkPolicy, Job
specs/                the prose contracts the model reads
  failure_taxonomy.md   classes, routing, the MISSING_DEPENDENCY/IMPORT_PATH_ERROR split
  remediation_actions.md the twelve typed actions and their arguments
  base_selection.md     the base-image rule table and digest pinning
  synthesis.md          system prompt: evidence -> EnvSpec
  repair.md             system prompt: one failure -> one typed action
  record_schema.md      the attempt/verdict JSON contract
src/
  envspec.py            dataclass, renderer, hash
  evidence.py           repo scan, annotation reader, context tar
  baseselect.py         evidence -> base + digest + install_mode
  patch.py              apply one typed action to an EnvSpec
  classify.py           rule table -> class + action (LLM is the fallback)
  normalize.py          stderr -> stable error signature
  k8s.py                the whole Kubernetes surface: create, get, log, delete
  registry.py           tag -> digest, digest -> bytes, over plain HTTPS
  builder.py            <- seam: Builder protocol + K8sBuilder (Kaniko pod)
  verifier.py           <- seam: Verifier protocol + K8sVerifier (one pod per rung)
  ladder.py             L0-L3
  record.py             attempt + verdict emission, with completeness enforced
  driver.py             the CLI: loop, budgets, teardown
harness/              Pi agent image (headless runs), mirrors ../biomodel-annotator
fixtures/             one tiny repo per failure class
outputs/              attempts.jsonl, verdicts.jsonl, per-job state
```

## Quick start

### Offline (no Docker) — the whole rule layer

```bash
uv run scripts/test_envbuild.py       # renderer, every action, rules, goldens
uv run src/driver.py render --spec my-spec.json
```

### In the cluster

Everything runs as pods: the agent, the Kaniko build, and each verification rung.

```bash
kubectl apply -f deploy/envbuild.yaml
kubectl -n envbuild create secret generic envbuild-registry-auth \
    --from-file=.dockerconfigjson=$HOME/.docker/config.json \
    --type=kubernetes.io/dockerconfigjson

export ENVBUILD_MODELS_PVC=<claim the model artifacts live on>
./run.sh /models/mbmm mism:model/mbmm
```

Or drive the CLI directly from inside the agent pod:

```bash
uv run src/driver.py init --repo ./fixtures/import_path_error \
    --annotation ./fixtures/import_path_error/annotation.yaml \
    --model-id fixture:import_path_error --job-id demo
uv run src/driver.py attempt --job-id demo          # exit 1 -> read the classification
uv run src/driver.py patch   --job-id demo --action FIX_MOUNT_CONTRACT \
                             --arg extra_path=/model/src --why "src/ layout"
uv run src/driver.py attempt --job-id demo          # exit 0 -> L3
uv run src/driver.py verdict --job-id demo --status verified
```

Two claims carry everything, and the split is deliberate:

| Claim | Mounted | Holds |
|---|---|---|
| model artifacts | **read-only**, everywhere | model source — nothing envbuild runs can modify it |
| `envbuild-work` | read-write | rendered Dockerfiles, Kaniko's digest file, each job's `inputs/` and `outputs/` |

Bytes move on those claims, never through the API. That is what removed the
`kubectl cp`/`exec` staging dance — and with it the need for `pods/exec`.

### As a headless Pi agent

Mirrors the `biomodel-annotator` harness in this org — see
[`harness/README.md`](harness/README.md).

```bash
export AZURE_OPENAI_BASE_URL="https://<resource>.cognitiveservices.azure.com/openai/v1/"
export AZURE_OPENAI_API_KEY="..."      # host env only -- never in a tracked file
./run.sh ../MBMM mism:model/mbmm
```

`ANTHROPIC_API_KEY` works instead; the provider is inferred from whichever key is
present, and a missing credential fails fast with exit 78 naming the variable.
See [`harness/README.md`](harness/README.md) for the full passthrough list.

It consumes a `biomodel-annotator` `metadata-package/` directory directly:
`execution.entry_points`, `execution.dependencies`, `execution.system_dependencies`
and `io.outputs` are exactly the annotation subset the loop reads.

### As a Claude Code skill

```bash
./dev-install.sh                # -> ~/.claude/skills/envbuild
```

Then ask: *"build an environment for the model at `<path>`"*.

## The CLI

| Command | What it does |
|---|---|
| `init` | scan the repo, pick and pin a base, draft an `EnvSpec` |
| `spec` | install the agent's authored `EnvSpec` |
| `attempt` | build, push, climb the ladder, classify, record. Exit 0 = verified |
| `patch` | apply exactly one typed action; prints the rendered Dockerfile diff |
| `reverify` | replay L2–L3 against an existing image (code changed, deps did not) |
| `verdict` | close the job — **every exit path writes one** — and tear down |
| `inspect` | job state plus its attempt rows |
| `render` | print the Dockerfile for a spec. No Docker needed |

## Loop invariants

These separate a loop that converges from one that thrashes. The driver enforces
all five; the agent cannot opt out.

- **Retain best-so-far.** A repair that lowers the rung is rolled back and the
  regressing action forbidden. One bad `CHANGE_BASE_IMAGE` cannot discard four
  attempts of progress.
- **Forbid repeated `(failure_class, action, arg)` triples**, per job. Episodic
  memory that dies with the job — the largest single reducer of wasted attempts.
- **One action per attempt.** A second `patch` is refused and the discard logged.
- **Every exit path writes a verdict.** A job that ends without one is the single
  failure mode that corrupts the dataset.
- **Teardown in `finally`.** Containers, volumes, images, build cache.

Budgets: 5 attempts · 20 min wall clock · 10 min per build · 3 min per verify ·
image size ceiling · context tar ceiling. All in `config.ini`.

## Seams for the infra team

`Builder` and `Verifier` are separate `Protocol`s on purpose — build and verify
are different pods with different isolation requirements, and separating them now
is what makes gVisor-on-execution a drop-in later rather than a rewrite. Phase 0
ships `K8sBuilder` (a Kaniko pod per attempt) and `K8sVerifier` (a pod per rung,
tokenless and network-denied); the infra team's own versions swap in without
`driver.py` changing.

The whole contract surface is five things: those two protocols,
[`specs/record_schema.md`](specs/record_schema.md) (attempt + verdict JSON), and
the annotation subset read by `evidence.read_annotation`.

## What milestone 9 is for

The failure-class histogram from ~20 real repos decides Phase 0.5, and it should
be the only thing that does. Dense `DEP_RESOLUTION_CONFLICT` on older repos →
date-bounded dependency resolution. Dense `MISSING_SYSTEM_LIB` on a few solvers →
curated stacks. Dense `classified_by: "llm"` → the rule table is under-built,
which is the cheapest available win. Do not decide any of this in advance; the
point of Phase 0 is to *generate* the evidence for that decision.

## License

See [`LICENSE`](LICENSE).
