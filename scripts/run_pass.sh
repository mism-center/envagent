#!/usr/bin/env bash
# Run envbuild Jobs for a list of benchmark models, one after another, and keep
# each Job's log. This is the hold-out protocol's runner: sequential, no fixes
# between models, a cap per Job, resumable (a model with a .done marker is
# skipped). The caller exports KUBECONFIG; this script never reads or prints it.
#
#   KUBECONFIG=... scripts/run_pass.sh <run-id> <agent-image@digest> [model ...]
#
# No model names -> every model in bench/holdout_manifest.yaml, in order.
# Env: OUT (log dir, default ~/.cache/envbuild/<run-id> -- not /tmp, which a
#      reboot empties mid-pass), MODELS_PVC (default envbuild-bench-models),
#      CAP (seconds per Job, default 7200), NO_MEMORY=1 (search trial: the
#      Job ignores remembered specs), NS (namespace, default `default`).
set -uo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <run-id> <agent-image@digest> [model ...]" >&2; exit 2; }
: "${KUBECONFIG:?export KUBECONFIG first}"
RUN_ID=$1; IMG=$2; shift 2
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT=${OUT:-$HOME/.cache/envbuild/$RUN_ID}; NS=${NS:-default}; CAP=${CAP:-7200}
mkdir -p "$OUT"
if [ $# -gt 0 ]; then MODELS="$*"; else
  MODELS=$(python3 -c "
import yaml
for m in yaml.safe_load(open('$REPO/bench/holdout_manifest.yaml'))['holdout']['models']:
    print(m['id'].split(':',1)[1])"); fi
for n in $MODELS; do
  [ -f "$OUT/$n.done" ] && continue
  slug=$(printf '%s' "$n" | tr '[:upper:]_' '[:lower:]-' | cut -c1-24); slug=${slug%-}
  # The run id's hash is in the name: two passes launched in the same second
  # (fresh-1, both arms) otherwise get one Job name and the second is refused.
  job="$(date +%Y%m%d-%H%M%S)-$(printf '%s' "$RUN_ID" | sha256sum | cut -c1-4)-$slug"
  echo "$(date -Is) START $n envbuild-$job" >> "$OUT/pass.log"
  sed -e "s|__JOB__|$job|g" -e "s|__MODEL_ID__|bench:$n|g" \
      -e "s|__MODEL_REPO__|/models/bench-$n/1.0|g" -e "s|__ANNOTATION__||g" \
      -e "s|__MODELS_PVC__|${MODELS_PVC:-envbuild-bench-models}|g" -e "s|__AGENT_IMAGE__|$IMG|g" \
      -e "s|__RUN_ID__|$RUN_ID|g" -e "s|__NO_MEMORY__|${NO_MEMORY:-0}|g" -e "s|__CAP__|$CAP|g" \
      "$REPO/deploy/agent-job.yaml" > "$OUT/.job.yaml"
  # A launch that cannot reach the API is retried: a dropped network on the
  # launching machine is not a reason to end the pass (aks-holdout-7).
  applied=0
  for _ in 1 2 3 4 5 6; do
    kubectl -n "$NS" apply -f "$OUT/.job.yaml" >> "$OUT/pass.log" 2>&1 && { applied=1; break; }
    sleep 60
  done
  [ $applied = 1 ] || { echo "$(date -Is) APPLY_FAILED $n" >> "$OUT/pass.log"; exit 1; }
  # The Job's activeDeadlineSeconds enforces CAP in the cluster. This loop only
  # watches, and only the cluster's answer ends it: an unreachable API (sleep,
  # VPN, DNS) is waited out, never read as a timeout.
  t0=$(date +%s); state=""
  while [ -z "$state" ]; do
    sleep 30
    s=$(kubectl -n "$NS" get job "envbuild-$job" -o jsonpath='{.status.succeeded}/{.status.failed}/{.status.conditions[?(@.type=="Failed")].reason}' 2>&1)
    case "$s" in
      *NotFound*) state=lost ;;               # past ttlSecondsAfterFinished while we were away
      1/*) state=complete ;;
      */1/DeadlineExceeded) state=timeout ;;
      */1/*) state=failed ;;
    esac
  done
  kubectl -n "$NS" logs "job/envbuild-$job" --tail=-1 > "$OUT/$n.log" 2>&1
  echo "$(date -Is) END $n $state $(( $(date +%s) - t0 ))s" >> "$OUT/pass.log"
  # `lost` means nobody saw how it ended: the verdict on the work claim is the
  # record, and the model is NOT marked done, so a re-run picks it up.
  [ "$state" = lost ] || echo "$job $state" > "$OUT/$n.done"
done
echo "$(date -Is) PASS_DONE" >> "$OUT/pass.log"
