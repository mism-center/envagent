#!/usr/bin/env bash
# Fire one envbuild run as a Kubernetes Job.
#
#   ./run.sh mbmm/1.0 mism:model/mbmm metadata-package
#
# The first argument is the model's path ON THE ARTIFACTS CLAIM, which is laid
# out <model_id>/<version>/<files>. It is mounted read-only at /models, so
# `mbmm/1.0` becomes /models/mbmm/1.0 inside every pod.
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
#   kubectl -n default create secret generic envbuild-registry-auth \
#       --from-file=.dockerconfigjson=$HOME/.docker/config.json \
#       --type=kubernetes.io/dockerconfigjson
#   kubectl -n default create secret generic envbuild-llm \
#       --from-literal=AZURE_OPENAI_API_KEY=... \
#       --from-literal=AZURE_OPENAI_BASE_URL=...
#
# Credentials live in those Secrets, never in this file: a key pasted into a
# tracked script is a key you have to rotate, and it stays in git history after
# you delete the line.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NS="${ENVBUILD_NAMESPACE:-default}"
MODELS_PVC="${ENVBUILD_MODELS_PVC:-irods-pvc}"
MODELS_MOUNT="${ENVBUILD_MODELS_MOUNT:-/models}"
# The tag that actually matches this branch. :develop is built by CI from
# `develop` and predates the in-cluster rewrite -- running it would look
# correct and execute the old code. Point this back at :develop once this
# branch merges.
AGENT_IMAGE="${ENVBUILD_AGENT_IMAGE:-mismplatform/pi-envagent:k8s-test}"

SUBPATH="${1:-}"
if [ -z "$SUBPATH" ]; then
  echo "usage: $0 <model_id>/<version> [model-id] [annotation-subpath]" >&2
  echo "   e.g. $0 mbmm/1.0 mism:model/mbmm metadata-package" >&2
  exit 64
fi
SUBPATH="${SUBPATH#/}"                       # accept a leading slash either way
SUBPATH="${SUBPATH#"${MODELS_MOUNT#/}/"}"    # ...and a full /models/... path
MODEL_REPO="$MODELS_MOUNT/$SUBPATH"

MODEL_ID="${2:-local:$(echo "$SUBPATH" | tr / :)}"
ANNOTATION_SUB="${3:-metadata-package}"
ANNOTATION="$MODEL_REPO/$ANNOTATION_SUB"

# Job names are DNS labels: lowercase alphanumerics and dashes, and they must
# END on an alphanumeric. A model id that is a UUID truncates onto a dash about
# one cut in five, and the API server then rejects the name and every label
# carrying it -- five errors for one trailing character. Trim after the cut.
#
# Only the first 8 characters of the id are kept: the rest of a UUID is noise,
# and dropping it leaves room for the version, which is the part that actually
# tells two runs apart while you are watching `kubectl get pods`.
sanitise() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | tr -s '-'
}
ID_PART="${SUBPATH%%/*}"
VER_PART="${SUBPATH#*/}"
[ "$VER_PART" = "$SUBPATH" ] && VER_PART=""      # no slash means no version
SLUG="$(sanitise "$(printf '%.8s%s%s' "$ID_PART" "${VER_PART:+-}" "$VER_PART")")"
SLUG="${SLUG#-}"; SLUG="${SLUG%-}"
SLUG="$(printf '%s' "$SLUG" | cut -c1-28)"; SLUG="${SLUG%-}"
JOB="$(date +%Y%m%d-%H%M%S)-${SLUG:-job}"

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
    -e "s|__AGENT_IMAGE__|$AGENT_IMAGE|g" \
    deploy/agent-job.yaml | kubectl -n "$NS" apply -f -

echo "job:     envbuild-$JOB"
echo "model:   $MODEL_REPO  (claim $MODELS_PVC, read-only)"
echo "image:   $AGENT_IMAGE"
echo "logs:    kubectl -n $NS logs -f job/envbuild-$JOB"
echo "pods:    kubectl -n $NS get pods -l job-id=$JOB -w"
echo "records: /work/records on the envbuild-work claim"
