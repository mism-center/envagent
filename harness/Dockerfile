# Dockerized envbuild: Pi harness + skill + the tools the loop shells out to.
# Build from the REPO ROOT so SKILL.md/specs/src are in the build context:
#   docker build -t envbuild-pi -f harness/Dockerfile .
FROM node:24-bookworm-slim

# Runtime tools Pi's built-in tools expect, plus git (code_revision -- half of
# the approval unit). ca-certificates is load-bearing rather than incidental:
# every call to the Kubernetes API and to the registry is HTTPS from Python.
RUN apt-get update \
  && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep curl \
  && rm -rf /var/lib/apt/lists/*

# uv: required at runtime -- the skill runs `uv run src/driver.py`, which resolves
# PyYAML and a managed Python via PEP 723 inline deps. Static binary, pinned.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

# No docker, no buildx, no kubectl -- on purpose, and each for its own reason.
# Builds are Kaniko pods and verification runs are pods, both created over the
# Kubernetes REST API (src/k8s.py); base-image digests and image sizes are
# registry GETs (src/registry.py). There is no daemon to talk to and no binary
# worth shipping. An agent that cannot shell out to a cluster CLI is also one
# fewer way for an LLM-driven loop to do something nobody asked for.

# Install pi coding agent globally (still as root).
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent

# Pi reads global config (settings, system-prompt bridge) from this dir.
ENV PI_CODING_AGENT_DIR=/opt/pi

# The skill itself, copied verbatim. Same allowlist as dev-install.sh:
# SKILL.md + specs/*.md + src/*.py + config.ini + scripts/.
COPY SKILL.md        /opt/skills/envbuild/SKILL.md
COPY config.ini      /opt/skills/envbuild/config.ini
COPY specs/          /opt/skills/envbuild/specs/
COPY src/            /opt/skills/envbuild/src/
COPY scripts/        /opt/skills/envbuild/scripts/
COPY fixtures/       /opt/skills/envbuild/fixtures/

COPY harness/extensions/ /opt/pi/extensions/
COPY harness/pi/settings.json     /opt/pi/settings.json
COPY harness/pi/APPEND_SYSTEM.md  /opt/pi/APPEND_SYSTEM.md
COPY harness/entrypoint.sh        /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# uv writable state (managed Python + wheel cache) baked at BUILD time into an
# exec-safe, world-writable dir. At runtime /tmp may be a noexec tmpfs, where
# uv's fallback of downloading CPython fails to exec it (EACCES). /opt is
# exec-safe, and 0777 makes it usable under any --user.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_CACHE_DIR=/opt/uv/cache
RUN mkdir -p /opt/uv/python /opt/uv/cache \
  && uv python install 3.12 \
  && uv run /opt/skills/envbuild/src/driver.py --help >/dev/null 2>&1 || true \
  && chmod -R 0777 /opt/uv

# Non-root. Reuses node:24's own uid-1000 `node` user rather than adding a
# second one. Nothing here needs a privileged socket or a supplementary group
# any more: the only thing this process talks to is the API server, as the
# ServiceAccount the pod was given.
RUN mkdir -p /workspace/outputs \
  && chown -R node:node /workspace /opt/pi /opt/skills
USER node
ENV HOME=/home/node

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
