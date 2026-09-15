#!/usr/bin/env bash
# Fire one envbuild run as a Kubernetes Job.
#
#   ./run.sh /models/mbmm mism:model/mbmm metadata-package
#
# This is a TEST SCRIPT. It uses your own kubectl to create the Job -- the agent
# never sees your credential; inside the cluster it authenticates as the
# `envbuild` ServiceAccount, which can create pods and read their logs and
# nothing else. Eventually the execution platform fires this from model
# discovery; keeping it at pod level is what makes that a small change.
#
# One-time cluster setup, before the first run:
#
#   kubectl apply -f deploy/envbuild.yaml
#   kubectl -n envbuild create secret generic envbuild-registry-auth \
#       --from-file=.dockerconfigjson=$HOME/.docker/config.json \
#       --type=kubernetes.io/dockerconfigjson
#   kubectl -n envbuild create secret generic envbuild-llm \
#       --from-literal=AZURE_OPENAI_API_KEY=... \
#       --from-literal=AZURE_OPENAI_BASE_URL=...
#
# Credentials live in those Secrets, never in this file: a key pasted into a
# tracked script is a key you have to rotate, and it stays in git history after
# you delete the line.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NS="${ENVBUILD_NAMESPACE:-envbuild}"
MODELS_PVC="${ENVBUILD_MODELS_PVC:-}"

MODEL_REPO="${1:-}"
if [ -z "$MODEL_REPO" ]; then
  echo "usage: $0 <model-path-under-/models> [model-id] [annotation-subpath]" >&2
  echo "   e.g. $0 /models/mbmm mism:model/mbmm metadata-package" >&2
  exit 64
fi
if [ -z "$MODELS_PVC" ]; then
  echo "$0: set ENVBUILD_MODELS_PVC to the claim the model artifacts live on." >&2
  exit 78
fi

MODEL_ID="${2:-local:$(basename "$MODEL_REPO")}"
ANNOTATION_SUB="${3:-metadata-package}"
ANNOTATION="$MODEL_REPO/$ANNOTATION_SUB"

# Job names are DNS labels: lowercase alphanumerics and dashes, <=63 chars.
JOB="$(date +%Y%m%d-%H%M%S)-$(basename "$MODEL_REPO" | tr '[:upper:]_.' '[:lower:]--' \
        | tr -cd 'a-z0-9-' | cut -c1-20)"

for s in envbuild-registry-auth envbuild-llm; do
  kubectl -n "$NS" get secret "$s" >/dev/null 2>&1 || {
    echo "$0: secret $s missing in namespace $NS -- see the header of this script." >&2
    exit 78
  }
done

sed -e "s|__JOB__|$JOB|g" \
    -e "s|__MODEL_ID__|$MODEL_ID|g" \
    -e "s|__MODEL_REPO__|$MODEL_REPO|g" \
    -e "s|__ANNOTATION__|$ANNOTATION|g" \
    -e "s|__MODELS_PVC__|$MODELS_PVC|g" \
    deploy/agent-job.yaml | kubectl -n "$NS" apply -f -

echo "job: envbuild-$JOB"
echo "logs: kubectl -n $NS logs -f job/envbuild-$JOB"
echo "records land on the work claim under /work/records"
