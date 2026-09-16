# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

A Claude Code / Pi **skill** (`envbuild`) plus the Python that does its
deterministic work. The skill's brain is Markdown; its hands are `src/`.

- `SKILL.md` — frontmatter + the operating procedure the agent follows.
- `specs/*.md` — the contracts the agent reads on demand: the failure taxonomy,
  the typed actions, base selection, and the two system prompts
  (`synthesis.md`, `repair.md`). These churn; that is why they are prose, not code.
- `src/*.py` — everything that must be deterministic. One CLI (`driver.py`).
- `harness/` — the Pi agent image for headless runs (mirrors `../biomodel-annotator`).

**There is no LLM client in `src/`.** The agent *is* the model: it calls the CLI,
reads JSON, and does the two judgement steps (synthesise a spec, pick a repair).
Do not add an API client — that would duplicate the agent inside the tool it drives.

The parent `../.claude/CLAUDE.md` describes a generic Windows/PowerShell/venv
workflow. It does not apply here: there is no venv at this level, dependencies
are resolved by `uv` from PEP 723 inline metadata, and everything runs on Linux
containers.

## Running things

```bash
uv run scripts/test_envbuild.py       # offline suite; no Docker, no network
uv run src/driver.py --help
uv run src/driver.py render --spec spec.json
```

Anything past `init` needs a cluster: the `envbuild` ServiceAccount from
`deploy/envbuild.yaml`, both PVCs mounted, and a registry the cluster can push
to. There is no docker daemon and no `kubectl` anywhere in the loop.

## Invariants to preserve when editing

These are load-bearing for downstream consumers. Breaking one silently produces a
corpus nobody can learn from — which is the actual product of Phase 0.

- **The agent never emits Dockerfile text.** It emits an `EnvSpec`. If you find
  yourself adding a string-template escape hatch, add a field instead.
- **`base_digest` is never empty in a rendered Dockerfile.** Any action that
  changes the base clears the digest and forces re-resolution. Failing to resolve
  is a job failure, not a reason to fall back to a tag.
- **Mounted mode bakes no `ENTRYPOINT` and copies no code.** The image is a
  dependency stack shared across models; the entrypoint lives in the model record.
- **`MISSING_DEPENDENCY` vs `IMPORT_PATH_ERROR` stays split, and stays
  rung-aware.** Same stderr, different meaning at L1 and L2. Conflating them makes
  the loop install a same-named PyPI package over the model's own module and ship
  a container that runs the wrong code. `evidence.local_modules` is the
  disambiguator; do not remove it.
- **`record.py` raises on an incomplete row.** Completeness is a correctness
  property, not logging. Do not soften it to a warning.
- **`normalize.py` runs even though nothing reads its output.** It is the seed of
  the memory layer; a signature first recorded in Phase 1 has no history.
- **`Builder` and `Verifier` stay separate protocols.** They are different pods
  with different isolation requirements.

- **Nothing may require `pods/exec`.** The agent is LLM-driven; `pods/exec` plus
  `pods/create` is arbitrary code execution in the namespace — create a pod
  mounting any Secret, exec in, read it. Bytes reach a pod on a mounted PVC and
  come back the same way. If a change needs to reach into a *running* container,
  it is the wrong change; `scripts/test_envbuild.py` asserts that neither pod
  manifest has grown an init container or a sidecar.

- **Verification pods stay tokenless and network-denied.**
  `automountServiceAccountToken: false` keeps a cluster credential out of model
  code, and the `envbuild.io/network: deny` label is the in-cluster form of
  `--network none`. Both are asserted offline; the second is only *true* if the
  cluster's CNI enforces NetworkPolicy, so prove that separately.

- **The models claim is mounted read-only, always.** A model that can rewrite the
  corpus source makes every later attempt row unreproducible.

## Cross-file consistency

When you change one of these, check the others:

| Change | Also update |
|---|---|
| a failure class | `src/classify.py` `ROUTING`, `specs/failure_taxonomy.md` |
| a typed action | `src/patch.py` `ACTIONS`, `specs/remediation_actions.md`, the coverage test |
| an attempt/verdict field | `src/record.py` required lists, `specs/record_schema.md` |
| the renderer's layer order | `scripts/test_envbuild.py` `renderer: fixed layer order` |
| the Kaniko version | `config.ini` `kaniko_image` |
| a Kubernetes API call | `src/k8s.py`, the `FakeClient` in `scripts/test_envbuild.py` |
| an RBAC verb the code needs | `deploy/envbuild.yaml` Role, and say why in the comment |
| a pod manifest field | `src/builder.py` or `src/verifier.py`, and the manifest assertions in the test suite |

Bumping the builder is a deliberate act: it introduces a discontinuity in error
signatures across the corpus. Do it between corpus runs, not during one.

**The builder is Kaniko, and only Kaniko.** There is no BuildKit path and no
backend switch. `--mount=type=cache`, heredoc `RUN` and `# syntax=` directives
are BuildKit-only and Kaniko mis-executes or ignores all three, so the renderer
must never emit them — `scripts/test_envbuild.py` asserts that.

**The Kubernetes surface is five calls**, in `src/k8s.py`: create pod, get pod,
get log, delete pod, and one access review. Adding a sixth is a real decision —
it usually means a new RBAC verb, and every verb the agent holds is one an
LLM-driven loop holds. There is no Kubernetes client library and no `kubectl`;
`urllib` and the stdlib `ssl` module cover all of it in under 300 lines.

## Adding a classifier rule

The rule table is the main *output* of the corpus run. When a real stderr falls
through to the LLM:

1. Add a `(name, pattern, handler)` row in `src/classify.py:_rules()`. Put more
   specific patterns *before* general ones — `build_mode` before `py_module` is
   the existing example of why.
2. Extract an argument if you can. `libxml/parser.h` → `libxml2-dev` via
   `HEADER_APT` turns an LLM round-trip into a free deterministic repair.
3. Add the stderr to the `classify: the rest of the table` case dict in
   `scripts/test_envbuild.py`.

## Style

Comments explain *why*, especially where a simple-looking choice is load-bearing
(digest pinning, the L1/L2 split, the two registry hostnames). Deliberate
shortcuts are marked `ponytail:` with the ceiling and the upgrade path named.
Keep both conventions.
