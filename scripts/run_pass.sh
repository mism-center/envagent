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
  job="$(date +%Y%m%d-%H%M%S)-$slug"
  echo "$(date -Is) START $n envbuild-$job" >> "$OUT/pass.log"
  sed -e "s|__JOB__|$job|g" -e "s|__MODEL_ID__|bench:$n|g" \
      -e "s|__MODEL_REPO__|/models/bench-$n/1.0|g" -e "s|__ANNOTATION__||g" \
      -e "s|__MODELS_PVC__|${MODELS_PVC:-envbuild-bench-models}|g" -e "s|__AGENT_IMAGE__|$IMG|g" \
      -e "s|__RUN_ID__|$RUN_ID|g" -e "s|__NO_MEMORY__|${NO_MEMORY:-0}|g" "$REPO/deploy/agent-job.yaml" \
    | kubectl -n "$NS" apply -f - >> "$OUT/pass.log" 2>&1 || { echo "$(date -Is) APPLY_FAILED $n" >> "$OUT/pass.log"; exit 1; }
  t0=$(date +%s); state=timeout
  while [ $(( $(date +%s) - t0 )) -lt "$CAP" ]; do
    sleep 30
    s=$(kubectl -n "$NS" get job "envbuild-$job" -o jsonpath='{.status.succeeded}/{.status.failed}' 2>/dev/null) || continue
    case "$s" in 1/*) state=complete; break;; */1) state=failed; break;; esac
  done
  kubectl -n "$NS" logs "job/envbuild-$job" --tail=-1 > "$OUT/$n.log" 2>&1
  [ "$state" = timeout ] && kubectl -n "$NS" delete job "envbuild-$job" --wait=false >> "$OUT/pass.log" 2>&1
  echo "$(date -Is) END $n $state $(( $(date +%s) - t0 ))s" >> "$OUT/pass.log"
  echo "$job $state" > "$OUT/$n.done"
done
echo "$(date -Is) PASS_DONE" >> "$OUT/pass.log"
