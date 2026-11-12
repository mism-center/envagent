#!/usr/bin/env bash
# Load the benchmark sources onto the `envbuild-bench-models` claim.
#
#   KUBECONFIG=... scripts/load_bench_models.sh [old-claim]
#
# One self-expiring pod: clones every hold-out repo at its manifest sha into
# /dst/bench-<name>/1.0, and copies from the old claim (default irods-pvc,
# mounted read-only) what GitHub cannot supply: checkouts whose clone fails
# (viva-yalla's repo is gone), metadata-package/ dirs, and the tuning corpus.
# Each checkout gets /dst/_integrity/bench-<name>.sha256 for the pre-launch gate.
# Idempotent: a checkout already at its sha is left alone.
set -euo pipefail
OLD="${1:-irods-pvc}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIST=$(python3 -c "
import yaml
for m in yaml.safe_load(open('$REPO/bench/holdout_manifest.yaml'))['holdout']['models']:
    print(m['id'].split(':',1)[1], m['repo'], m['sha'])")
kubectl -n default delete pod envbuild-loader --ignore-not-found --wait=true
kubectl -n default apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata: {name: envbuild-loader, labels: {app: envbuild}}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 3600
  automountServiceAccountToken: false
  containers:
  - name: l
    image: alpine/git:2.45.2
    command: ["/bin/sh", "-c"]
    args:
    - |
      set -u; mkdir -p /dst/_integrity; fail=0
      git config --global --add safe.directory '*'
      seal() {  # \$1 = name
        (cd /dst/bench-\$1/1.0 && find . -type f -not -path './.git/*' -not -path './metadata-package/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > /dst/_integrity/bench-\$1.sha256
      }
      while read -r n url sha; do
        [ -n "\$n" ] || continue
        d=/dst/bench-\$n/1.0
        if [ "\$(git -C \$d rev-parse HEAD 2>/dev/null)" = "\$sha" ]; then echo "KEEP \$n"; continue; fi
        rm -rf /dst/bench-\$n; mkdir -p \$d
        if (cd \$d && git init -q && git config core.fileMode false && git remote add origin \$url && git fetch -q --depth 1 origin \$sha && git checkout -q FETCH_HEAD); then
          echo "CLONED \$n"
        else
          rm -rf /dst/bench-\$n; mkdir -p \$d
          if [ "\$(git -C /src/bench-\$n/1.0 rev-parse HEAD 2>/dev/null)" = "\$sha" ]; then
            cp -a /src/bench-\$n/1.0/. \$d/ && echo "COPIED \$n (clone failed; old claim is at the sha)"
          else echo "FAILED \$n"; fail=1; continue; fi
        fi
        [ -d /src/bench-\$n/1.0/metadata-package ] && cp -a /src/bench-\$n/1.0/metadata-package \$d/ && echo "ANNOTATION \$n"
      done <<'LIST'
$(printf '%s\n' "$LIST" | sed 's/^/      /')
      LIST
      # ponytail: the tuning corpus has no pinned shas, so it is copied as found
      for s in /src/bench-*; do
        b=\$(basename \$s); [ -e /dst/\$b ] && continue
        cp -a \$s /dst/ && echo "CORPUS \$b"
      done
      # busybox tools only (no xargs -d): NUL-separated. Sealed once, never refreshed --
      # a later mismatch is the signal, so the gate must not be able to re-seal.
      for d in /dst/bench-*; do
        b=\$(basename \$d); [ -s /dst/_integrity/\$b.sha256 ] || { seal \${b#bench-}; echo "SEALED \$b \$(wc -l < /dst/_integrity/\$b.sha256)"; }
      done
      ls /dst; echo "LOADER_DONE fail=\$fail"; exit \$fail
    volumeMounts:
    - {name: dst, mountPath: /dst}
    - {name: src, mountPath: /src, readOnly: true}
    resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {cpu: "1", memory: 1Gi}}
  volumes:
  - {name: dst, persistentVolumeClaim: {claimName: envbuild-bench-models}}
  - {name: src, persistentVolumeClaim: {claimName: $OLD, readOnly: true}}
YAML
echo "follow: kubectl -n default logs -f envbuild-loader"
