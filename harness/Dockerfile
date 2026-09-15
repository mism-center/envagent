# Dockerized envbuild: Pi harness + skill + the tools the loop shells out to.
# Build from the REPO ROOT so SKILL.md/specs/src are in the build context:
#   docker build -t envbuild-pi -f harness/Dockerfile .
FROM node:24-bookworm-slim

# Runtime tools Pi's built-in tools expect, plus tar (code staging) and git
# (code_revision -- half of the approval unit).
RUN apt-get update \
  && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep curl tar \
  && rm -rf /var/lib/apt/lists/*

# uv: required at runtime -- the skill runs `uv run src/driver.py`, which resolves
# PyYAML and a managed Python via PEP 723 inline deps. Static binary, pinned.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

# docker CLI + buildx plugin. Used ONLY for verification runs and digest
# resolution against the host daemon -- the agent never builds through the socket.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-buildx \
     /usr/local/libexec/docker/cli-plugins/docker-buildx

# buildctl, from the SAME buildkit version the daemon runs. Both are pinned for
# the same reason: attempt records are only comparable across runs if the build
# agent does not change underneath the corpus. Keep in step with
# config.ini [builder] buildkit_version and compose.yaml.
COPY --from=moby/buildkit:v0.24.0 /usr/bin/buildctl /usr/local/bin/buildctl

# kubectl. Only the "kaniko" builder backend uses it (K8sBuilder shells out to
# it -- a Pod is the build connection, its logs are the progress stream), but
# it is baked in unconditionally so one image serves both backends. Pinned for
# the same reason buildctl is: the build agent must not change under the corpus.
ARG KUBECTL_VERSION=v1.33.1
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
  && chmod 0755 /usr/local/bin/kubectl \
  && kubectl version --client=true --output=yaml >/dev/null

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
# second one. Access to /var/run/docker.sock comes from compose's `group_add`,
# not from running as root.
RUN mkdir -p /workspace/outputs \
  && chown -R node:node /workspace /opt/pi /opt/skills
USER node
ENV HOME=/home/node

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
