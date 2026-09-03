# envbuild — Docker harness (Pi)

Runs the `envbuild` skill as a one-shot, headless container using the
[Pi](https://pi.dev) coding agent instead of Claude Code, mirroring the
`biomodel-annotator` harness in this org.

## What's here

| Path | Role |
|---|---|
| `Dockerfile` | node:24 + Pi + skill + `uv` + `docker`/`buildx` + `buildctl`. **Build from repo root.** |
| `buildkitd.toml` | Tells buildkitd the local registry is plain HTTP. Without it push and cache export fail on TLS. |
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

The agent needs the buildkitd and registry services, so use compose rather than
a bare `docker run`:

Easiest is the launcher at the repo root, which brings the stack up for you:

```bash
export AZURE_OPENAI_BASE_URL="https://<resource>.cognitiveservices.azure.com/openai/v1/"
export AZURE_OPENAI_API_KEY="..."             # from your secret store
../run.sh ../MBMM mism:model/mbmm
```

Or by hand:

```bash
export DOCKER_GID=$(getent group docker | cut -d: -f3)
docker compose up -d buildkitd registry
docker compose run --rm \
  -e MODEL_ID="mism:model/1a2b3c" \
  -e ANNOTATION=/workspace/repo/metadata-package \
  -v /path/to/model:/workspace/repo \
  agent
```

## Credentials

Read from the **host environment only** — never baked into the image, compose
file, or a tracked script. `entrypoint.sh` infers the provider from whichever key
is present and fails with exit 78 (`EX_CONFIG`) naming the missing variable if
none is, rather than dying several minutes into a build with a stream error that
looks like a model fault.

| Variables | Provider | Default model |
|---|---|---|
| `AZURE_OPENAI_BASE_URL` + `AZURE_OPENAI_API_KEY` | `azure-openai-responses` | `gpt-5.6-luna` |
| `ANTHROPIC_API_KEY` | Pi default resolution | `anthropic/claude-opus-4-5` |

`AI_PROVIDER` and `AI_MODEL` override both. Other passthroughs: `MODEL_REPO`
(default `/workspace/repo`), `ANNOTATION`, `MODEL_ID`, `PROMPT`, `MISM_GUID`,
`MAX_ATTEMPTS`.

stdout = full JSON event trace; `outputs/attempts.jsonl` and
`outputs/verdicts.jsonl` land in the mounted `./outputs`.

## Why the two registry hostnames

buildkitd pushes to `registry:5000` (compose network). The **host** daemon pulls
the same digest from `localhost:5000` (published port). Docker trusts localhost
registries without an `--insecure-registry` daemon flag, so this needs no host
reconfiguration. Same image, same digest, different route.

## Egress

Outbound HTTPS to the model provider, plus whatever base images and package
indexes a build needs (`docker.io`, `pypi.org`, `deb.debian.org`, …). Verification
containers run with `--network none` — a model that only "runs" with live network
access has not been verified.

## Security posture

Phase 0 has **no sandbox** on verification. The agent container mounts the host
docker socket, which is root-equivalent on the host. Run it against the trusted
corpus only, on a machine you are willing to treat as expendable. Hardening
(gVisor, ephemeral namespaces, in-cluster execution) is deliberately out of scope
and is why `Builder` and `Verifier` are separate protocols.
