#!/usr/bin/env bash
# Fire-and-forget envbuild run.
#
# MODEL_REPO : repo to build an environment for (default /workspace/repo)
# ANNOTATION : optional metadata-package/ dir or annotation YAML
# MODEL_ID   : optional model identifier recorded in every row
# PROMPT     : optional extra instruction appended to the task
#
# Credentials, from the host env only -- never baked into an image or a script:
#   Azure OpenAI : AZURE_OPENAI_BASE_URL + AZURE_OPENAI_API_KEY
#   Anthropic    : ANTHROPIC_API_KEY
# The provider is inferred from whichever key is present; AI_PROVIDER and
# AI_MODEL override the defaults.
#
# Records land under ENVBUILD_OUTPUTS (bind-mounted to the host). The full JSON
# event trace goes to stdout (= docker logs).
#
# Retry loop: a provider can drop the SSE stream mid-turn, which pi reports as a
# terminal error. The session is saved (no --no-session), so on nonzero exit we
# resume with --continue and finish the job rather than losing a 90%-done run --
# and an unfinished job is not just wasted work, it is a missing verdict.
set -euo pipefail

REPO="${MODEL_REPO:-/workspace/repo}"
ATTEMPTS="${MAX_ATTEMPTS:-3}"

# Resolve the provider from the credential that is actually present. Failing here
# with a named variable beats pi failing several minutes into a build with a
# stream error that looks like a model problem.
provider="${AI_PROVIDER:-}"
model="${AI_MODEL:-}"
if [ -n "${AZURE_OPENAI_API_KEY:-}" ]; then
  : "${AZURE_OPENAI_BASE_URL:?AZURE_OPENAI_API_KEY is set but AZURE_OPENAI_BASE_URL is not}"
  provider="${provider:-azure-openai-responses}"
  model="${model:-gpt-5.6-luna}"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  model="${model:-anthropic/claude-opus-4-5}"
else
  echo "envbuild: no model credential in the environment." >&2
  echo "  Azure    : export AZURE_OPENAI_BASE_URL and AZURE_OPENAI_API_KEY" >&2
  echo "  Anthropic: export ANTHROPIC_API_KEY" >&2
  echo "Export it on the host and pass it through; do not hardcode it." >&2
  exit 78                     # EX_CONFIG -- a setup fault, not a job failure
fi

PROVIDER_ARGS=()
[ -n "$provider" ] && PROVIDER_ARGS=(--provider "$provider")
echo "envbuild: provider=${provider:-<pi default>} model=${model}" >&2

run() {
  pi --approve --stream=all \
    ${model:+--model "$model"} \
    "${PROVIDER_ARGS[@]}" \
    --append-system-prompt "$(cat /opt/pi/APPEND_SYSTEM.md)" "$@"
}

TASK="/skill:envbuild run the full loop for the model at ${REPO}"
[ -n "${ANNOTATION:-}" ] && TASK="${TASK}, annotation ${ANNOTATION}"
[ -n "${MODEL_ID:-}" ]   && TASK="${TASK}, model-id ${MODEL_ID}"
[ -n "${PROMPT:-}" ]     && TASK="${TASK}. ${PROMPT}"

# Attempt 1: the whole loop.
if run -p "${TASK}"; then
  exit 0
fi

# Attempts 2..N: resume the saved session and finish whatever is left. The
# reminder is deliberate -- the one thing that must not be skipped is the verdict.
for i in $(seq 2 "${ATTEMPTS}"); do
  echo "pi exited nonzero -- resume attempt ${i}/${ATTEMPTS} via --continue" >&2
  if run -c -p "Continue. Finish the envbuild loop and close the job with \
\`envbuild verdict\` -- every exit path must write a verdict, then report."; then
    exit 0
  fi
done

exit 1
