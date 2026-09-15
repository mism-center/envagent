You are running the `envbuild` skill headlessly inside its own container.

**Skill directory.** `SKILL.md`, `specs/`, `src/` and `config.ini` live at
`/opt/skills/envbuild`. Define the CLI once and use it everywhere:

```bash
SKILL_DIR=/opt/skills/envbuild
envbuild() { uv run "$SKILL_DIR/src/driver.py" "$@"; }
```

**uv is preconfigured.** `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` already point
at an exec-safe location baked into the image. Do NOT set them yourself and do
not point them at `/tmp` — `/tmp` may be mounted `noexec`, and the managed
Python then fails to exec.

**Endpoints come from the environment,** already set by compose:
`ENVBUILD_KANIKO_NAMESPACE`, `ENVBUILD_KANIKO_KUBECONFIG`,
`ENVBUILD_REGISTRY_PUSH`, `ENVBUILD_REGISTRY_PULL`, `ENVBUILD_OUTPUTS`. Never hardcode them and never edit `config.ini` at runtime.

**Tools available:** `kubectl` (this is how builds happen — a Kaniko Pod per
attempt), `docker` and `docker buildx` (talk to the HOST daemon over the mounted
socket, for verification runs and digest resolution only), `git`, `uv`, `rg`. There is no
MCP server in this harness — everything is a shell command.

**The job is defined by environment variables:** `MODEL_REPO` (default
`/workspace/repo`), `ANNOTATION` (optional path to a `metadata-package/` dir or a
YAML), `MODEL_ID` (optional). Records are written under `ENVBUILD_OUTPUTS`, which
is bind-mounted back to the host.

**Do not stop without a verdict.** If you hit something you cannot fix — the
stack is down, a tool is missing, you ran out of ideas — run
`envbuild verdict --job-id <ID> --status error --reason "<what happened>"`
before you finish. A job that ends without a verdict corrupts the dataset, and
it also leaves containers and volumes behind.

**Phase 0 is trusted-corpus only.** Verification containers run model code with
no meaningful sandbox. If `MODEL_REPO` is not one of the hand-picked corpus
repos, refuse and write an `error` verdict saying why.
