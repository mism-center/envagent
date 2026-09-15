#!/usr/bin/env bash
# Headless envbuild run, same ergonomics as scratch/pi-agent/run.sh.
#
#   export AZURE_OPENAI_BASE_URL="https://<resource>.cognitiveservices.azure.com/openai/v1/"
#   export AZURE_OPENAI_API_KEY="..."          # from your secret store, NOT from a file
#   ./run.sh ../MBMM mism:model/mbmm
#
# Anthropic instead: export ANTHROPIC_API_KEY and the provider is inferred.
#
# API KEY FROM HOST ENV -- never hardcoded here. A key pasted into a tracked
# script is a key you have to rotate, and it stays in git history after you
# delete the line.
#
# Unlike the biomodel-annotator runner this uses compose, not a bare `docker
# run`: the agent needs a kubeconfig, a registry credential file and the docker
# socket wired up together, and compose is where that wiring lives.
#
# On the hardening flags in the pi-agent runner (--cap-drop=ALL etc.): they are
# deliberately NOT copied here. This container mounts /var/run/docker.sock, which
# is root-equivalent on the host, so dropping capabilities inside the container
# buys approximately nothing. Phase 0 is trusted-corpus only for this reason; the
# isolation story is the infra team's Builder/Verifier split, not container flags.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_HOST="${1:-}"
if [ -z "$REPO_HOST" ]; then
  echo "usage: $0 <path-to-model-repo> [model-id] [annotation-subpath]" >&2
  echo "   e.g. $0 ../MBMM mism:model/mbmm metadata-package" >&2
  exit 64
fi
REPO_HOST="$(cd "$REPO_HOST" && pwd)"        # absolute; bind mounts need it

if [ -z "${AZURE_OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "$0: export AZURE_OPENAI_API_KEY (+ AZURE_OPENAI_BASE_URL) or ANTHROPIC_API_KEY first." >&2
  exit 78
fi

export MODEL_ID="${2:-local:$(basename "$REPO_HOST")}"
# Annotation path is resolved INSIDE the container, under the mount point.
ANNOTATION_SUB="${3:-metadata-package}"
if [ -e "$REPO_HOST/$ANNOTATION_SUB" ]; then
  export ANNOTATION="/workspace/repo/$ANNOTATION_SUB"
else
  echo "$0: no $ANNOTATION_SUB in $REPO_HOST -- running without an annotation." >&2
  echo "     Expect L1 at best: L2/L3 need a declared entry point." >&2
  export ANNOTATION=""
fi

# The agent runs as uid 1000 and must join whatever group owns the socket. Read
# it off the socket rather than looking up a group called "docker": rootless and
# Docker Desktop setups do not always have one, and guessing costs a whole build.
export DOCKER_GID="${DOCKER_GID:-$(stat -c %g /var/run/docker.sock 2>/dev/null || true)}"
if [ -z "$DOCKER_GID" ]; then
  echo "$0: cannot read the gid of /var/run/docker.sock -- is docker running?" >&2
  exit 78
fi

mkdir -p outputs
# No local build service to start: the build runs in-cluster (Kaniko) and pushes
# to a real registry. The only local daemon involved is the one that verifies.
docker compose run --rm \
  -v "$REPO_HOST:/workspace/repo:ro" \
  agent

echo
echo "records:"
tail -n 1 outputs/verdicts.jsonl 2>/dev/null || echo "  (no verdict written -- check the log above)"
