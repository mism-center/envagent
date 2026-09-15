# envbuild — Docker harness (Pi)

Runs the `envbuild` skill as a one-shot, headless **pod** using the
[Pi](https://pi.dev) coding agent instead of Claude Code, mirroring the
`biomodel-annotator` harness in this org.

## What's here

| Path | Role |
|---|---|
| `Dockerfile` | node:24 + Pi + skill + `uv` + `git`. No docker, no kubectl. **Build from repo root.** |
| `pi/settings.json` | Registers the skill. No MCP, no extensions. |
| `pi/APPEND_SYSTEM.md` | Skill dir, uv rules, endpoint env vars, the verdict obligation, the trusted-corpus constraint. |
| `entrypoint.sh` | Launches the run; resumes with `--continue` if the stream drops. |

The skill files (`SKILL.md`, `specs/`, `src/`, `config.ini`) are **unchanged** —
copied into the image at build time.

Unlike the biomodel-annotator harness there are **no Pi extensions**: that one
needed an extension to replace an MCP server (OLS). envbuild has no MCP
dependency — every tool it uses is a shell command — and `pi --mode json
--stream=all` already emits the full event trace on stdout.

## Build

```bash
# from the repo root (NOT from harness/)
docker build -t envbuild-pi -f harness/Dockerfile .
```

## Run

The agent runs **in the cluster**, as a Job. It needs two PVCs (the model
artifacts read-only, envbuild's own scratch read-write) and the `envbuild`
ServiceAccount. `../run.sh` renders `deploy/agent-job.yaml` and applies it:

```bash
../run.sh mbmm/1.0 mism:model/mbmm      # <model_id>/<version> on irods-pvc
kubectl -n default logs -f job/envbuild-<job>
```

One-time cluster setup is in `deploy/envbuild.yaml` plus two Secrets — see the
header of `run.sh`.

## Credentials

Read from the `envbuild-llm` Secret, mounted into the Job as environment
variables — never baked into the image or a tracked file. `entrypoint.sh` infers
the provider from whichever key is present and fails with exit 78 (`EX_CONFIG`)
naming the missing variable if none is, rather than dying several minutes into a
build with a stream error that looks like a model fault.

| Variables | Provider | Default model |
|---|---|---|
| `AZURE_OPENAI_BASE_URL` + `AZURE_OPENAI_API_KEY` | `azure-openai-responses` | `gpt-5.6-luna` |
| `ANTHROPIC_API_KEY` | Pi default resolution | `anthropic/claude-opus-4-5` |

`AI_PROVIDER` and `AI_MODEL` override both. Other passthroughs: `MODEL_REPO`
(`/models/<model_id>/<version>`), `ANNOTATION`, `MODEL_ID`, `PROMPT`, `MISM_GUID`,
`MAX_ATTEMPTS`.

stdout = full JSON event trace; `attempts.jsonl` and `verdicts.jsonl` land under
`ENVBUILD_OUTPUTS` on the work claim, which outlives the pod.

## Why the registry is not cluster-local

The build pod pushes and the verification pod pulls, and they are different pods
with no shared image store — so an image has to go somewhere both can reach
(Docker Hub by default, `ENVBUILD_REGISTRY_PUSH`). Kaniko authenticates from the
`envbuild-registry-auth` Secret; verification pods use the same Secret as an
`imagePullSecret`; this process reads manifests from it over HTTPS to resolve
base digests and image sizes.

The image is always addressed **by digest** from the moment it is built, so what
gets verified is provably what got built.

## Egress

The agent pod talks outbound HTTPS to the model provider, the Kubernetes API and
the registry. Build pods reach whatever base images and package indexes a build
needs (`docker.io`, `pypi.org`, `deb.debian.org`, …).

**Verification pods reach nothing.** They carry `envbuild.io/network: deny`, which
the NetworkPolicy in `deploy/envbuild.yaml` selects — no egress, no ingress, no
DNS. That is the in-cluster form of `--network none`, and it only holds if the
cluster's CNI actually enforces NetworkPolicy. Prove that once, with an L3 that
tries to reach the network and fails, before trusting a `verified` verdict.

## Security posture

The agent authenticates as the `envbuild` ServiceAccount, which can create pods
and read their logs **in one namespace** and nothing else. No `pods/exec`, no
`secrets`, no docker socket. Verification pods get no ServiceAccount token, drop
all capabilities, cannot escalate privilege, and mount the model source
read-only.

What is still missing is a real sandbox *inside* the verification pod: model code
runs as an ordinary container process, so a container escape is a cluster
problem. Run against the trusted corpus only. gVisor and ephemeral namespaces
remain out of scope, and are why `Builder` and `Verifier` are separate
protocols.
