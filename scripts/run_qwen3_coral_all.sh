#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="${QWEN_CORAL_RUN_ID:-qwen3-coder-plus-coral-all180-a2-24g-20260806}"
RUN_DIR="${ROOT_DIR}/eval/coral_batches/${RUN_ID}"
PYTHON="${ROOT_DIR}/.venv/bin/python"
RUNNER="${ROOT_DIR}/scripts/run_qwen3_coral_all.py"
ACTION="${1:-start}"
shift || true

mkdir -p "${RUN_DIR}"
umask 077

load_env() {
  if [ -f "${ROOT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
  fi
  export PATH="${ROOT_DIR}/tools/opencode/node_modules/.bin:${ROOT_DIR}/.venv/bin:${PATH}"
}

run_process_groups() {
  local own_pgid
  own_pgid="$(ps -o pgid= -p "$$" | tr -dc '0-9')"
  ps -eo pid=,pgid=,args= | awk \
    -v needle="${RUN_ID}" -v own_pid="$$" -v own_pgid="${own_pgid}" \
    'index($0, needle) && $1 != own_pid && $2 != own_pgid {print $2}' | sort -u

  local pid_file agent_pid agent_cwd agent_pgid
  for pid_file in "${ROOT_DIR}/eval/coral/${RUN_ID}"/*/*/coral_run/.coral/public/agent.pids; do
    [ -f "${pid_file}" ] || continue
    while read -r agent_pid; do
      [[ "${agent_pid}" =~ ^[0-9]+$ ]] || continue
      [ -d "/proc/${agent_pid}" ] || continue
      agent_cwd="$(readlink -f "/proc/${agent_pid}/cwd" 2>/dev/null || true)"
      case "${agent_cwd}" in
        "${ROOT_DIR}/eval/coral/${RUN_ID}/"*) ;;
        *) continue ;;
      esac
      agent_pgid="$(ps -o pgid= -p "${agent_pid}" | tr -dc '0-9')"
      [ -n "${agent_pgid}" ] && [ "${agent_pgid}" != "${own_pgid}" ] && echo "${agent_pgid}"
    done < "${pid_file}"
  done
}

signal_run_processes() {
  local signal_name="$1" pgid
  while read -r pgid; do
    [[ "${pgid}" =~ ^[0-9]+$ ]] || continue
    kill "-${signal_name}" -- "-${pgid}" 2>/dev/null || true
  done < <(run_process_groups | sort -u)
}

case "${ACTION}" in
  install)
    exec npm ci --prefix "${ROOT_DIR}/tools/opencode"
    ;;
  foreground)
    load_env
    exec "${PYTHON}" -u "${RUNNER}" --run-id "${RUN_ID}" "$@"
    ;;
  start)
    if [ -s "${RUN_DIR}/launcher.pid" ]; then
      old_pid="$(tr -dc '0-9' < "${RUN_DIR}/launcher.pid")"
      if [ -n "${old_pid}" ] && kill -0 "${old_pid}" 2>/dev/null; then
        echo "Batch already running (PID ${old_pid})"
        exit 0
      fi
    fi
    load_env
    nohup setsid "${PYTHON}" -u "${RUNNER}" --run-id "${RUN_ID}" "$@" \
      >> "${RUN_DIR}/run.log" 2>&1 < /dev/null &
    batch_pid=$!
    echo "${batch_pid}" > "${RUN_DIR}/launcher.pid"
    echo "Started ${RUN_ID} (PID ${batch_pid}); log: ${RUN_DIR}/run.log"
    ;;
  status)
    if [ -s "${RUN_DIR}/launcher.pid" ]; then
      batch_pid="$(tr -dc '0-9' < "${RUN_DIR}/launcher.pid")"
      if [ -n "${batch_pid}" ] && kill -0 "${batch_pid}" 2>/dev/null; then
        echo "running PID ${batch_pid}"
      else
        echo "not running (stale launcher PID ${batch_pid:-unknown})"
      fi
    else
      echo "not running"
    fi
    if [ -f "${RUN_DIR}/progress.json" ]; then
      "${PYTHON}" -m json.tool "${RUN_DIR}/progress.json"
    fi
    ;;
  stop)
    batch_pid=""
    if [ -s "${RUN_DIR}/launcher.pid" ]; then
      batch_pid="$(tr -dc '0-9' < "${RUN_DIR}/launcher.pid")"
    fi
    signal_run_processes TERM
    sleep 3
    if [ -n "$(run_process_groups)" ]; then
      signal_run_processes KILL
      echo "Stopped ${RUN_ID}; force-killed residual run process groups"
    else
      echo "Stopped ${RUN_ID}; no residual run process groups"
    fi
    ;;
  *)
    echo "Usage: $0 {install|foreground|start|status|stop} [runner args...]" >&2
    exit 2
    ;;
esac
