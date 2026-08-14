#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 RUN_ID PAPER_ID [MODEL_ID]" >&2
  exit 2
fi

run_id=$1
paper_id=$2
model_id=${3:-gpt-5.6-sol}
if [[ ! "$run_id" =~ ^[A-Za-z0-9._-]+$ ]] || [[ ! "$paper_id" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID and PAPER_ID may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi
if [[ ! "$model_id" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "MODEL_ID may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
workspace_root=/tmp/frontieror_codex_harness
case_dir=$workspace_root/$run_id/$paper_id
run_dir=$repo_root/codex_harness/runs/$run_id
submission_dir=$run_dir/submissions/$paper_id/$model_id
trace_dir=$run_dir/traces/$paper_id
runtime_codex_home=$(mktemp -d "/tmp/frontieror_codex_home.${run_id}.${paper_id}.XXXXXX")
trap 'rm -rf -- "$runtime_codex_home"' EXIT

auth_mode=${CODEX_HARNESS_AUTH_MODE:-auto}
if [[ "$auth_mode" == auto ]]; then
  if [[ "$model_id" != gpt-5.6-sol && -n "${OPENAI_API_KEY:-}" ]]; then
    auth_mode=api_key
  else
    auth_mode=chatgpt
  fi
fi
case "$auth_mode" in
  api_key)
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "CODEX_HARNESS_AUTH_MODE=api_key requires OPENAI_API_KEY" >&2
      exit 1
    fi
    printf '%s' "$OPENAI_API_KEY" | CODEX_HOME="$runtime_codex_home" \
      codex login --with-api-key >/dev/null
    ;;
  chatgpt)
    if [[ ! -f "$HOME/.codex/auth.json" ]]; then
      echo "Codex auth file not found: $HOME/.codex/auth.json" >&2
      exit 1
    fi
    install -m 600 "$HOME/.codex/auth.json" "$runtime_codex_home/auth.json"
    if [[ -f "$HOME/.codex/installation_id" ]]; then
      install -m 600 "$HOME/.codex/installation_id" "$runtime_codex_home/installation_id"
    fi
    ;;
  *)
    echo "CODEX_HARNESS_AUTH_MODE must be auto, api_key, or chatgpt" >&2
    exit 2
    ;;
esac
for cache_file in cloud-config-bundle-cache.json cloud-requirements-cache.json models_cache.json; do
  if [[ -f "$HOME/.codex/$cache_file" ]]; then
    install -m 600 "$HOME/.codex/$cache_file" "$runtime_codex_home/$cache_file"
  fi
done

mkdir -p "$trace_dir"
"$repo_root/.venv/bin/python" \
  "$repo_root/scripts/codex_harness/prepare_case.py" \
  "$paper_id" --run-id "$run_id" --workspace-root "$workspace_root" \
  --model "$model_id"

bwrap \
  --die-with-parent \
  --new-session \
  --ro-bind / / \
  --dev /dev \
  --proc /proc \
  --tmpfs /tmp \
  --bind "$runtime_codex_home" "$runtime_codex_home" \
  --dir /tmp/codex-install \
  --dir /tmp/frontieror-venv \
  --ro-bind "$repo_root/.venv" /tmp/frontieror-venv \
  --setenv CODEX_HOME "$runtime_codex_home" \
  --setenv CODEX_INSTALL_DIR /tmp/codex-install \
  --setenv VIRTUAL_ENV /tmp/frontieror-venv \
  --setenv PATH "/tmp/frontieror-venv/bin:$PATH" \
  --dir /tmp/frontieror_codex_harness \
  --dir "/tmp/frontieror_codex_harness/$run_id" \
  --bind "$case_dir" "$case_dir" \
  --tmpfs "$repo_root" \
  codex exec \
  --ignore-user-config \
  --ignore-rules \
  --skip-git-repo-check \
  --ephemeral \
  --model "$model_id" \
  --config 'model_reasoning_effort="xhigh"' \
  --config 'features.multi_agent=false' \
  --config 'web_search="disabled"' \
  --config 'approval_policy="never"' \
  --sandbox workspace-write \
  --cd "$case_dir" \
  --json \
  --output-last-message "$case_dir/final_message.md" \
  - < "$case_dir/TASK.md" | tee "$trace_dir/events.jsonl"

if [[ ! -s "$case_dir/code.py" ]]; then
  echo "Codex run completed without a non-empty code.py: $case_dir" >&2
  exit 1
fi

"$repo_root/.venv/bin/python" -m py_compile "$case_dir/code.py"
mkdir -p "$submission_dir"
cp "$case_dir/code.py" "$submission_dir/code.py"
cp "$case_dir/manifest.json" "$submission_dir/manifest.json"
cp "$case_dir/final_message.md" "$trace_dir/final_message.md"

echo "Submission: $submission_dir/code.py"
echo "Trace:      $trace_dir/events.jsonl"
