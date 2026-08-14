#!/usr/bin/env bash
# Clone & pin upstream CORAL.
# Source: https://github.com/Human-Agent-Society/CORAL
# Pinned to HEAD on 2026-04-27 (no upstream release tags).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET_DIR="${ROOT_DIR}/external/coral"
OPENCODE_DIR="${ROOT_DIR}/tools/opencode"
REPO_URL="https://github.com/Human-Agent-Society/CORAL.git"
REF="${CORAL_REF:-61bc7619a05e4e0e36e3556f89756f095f36b8db}"

if [ ! -d "${TARGET_DIR}/.git" ]; then
  mkdir -p "$(dirname "${TARGET_DIR}")"
  git clone "${REPO_URL}" "${TARGET_DIR}"
fi

git -C "${TARGET_DIR}" fetch --tags
git -C "${TARGET_DIR}" checkout "${REF}"

# Install coral globally so the `coral` CLI is on PATH inside agent bash
# subshells. Without this the agent's `coral eval` / `coral log` etc. fail
# with `coral: command not found` (the upstream `setup_worktree_env` hook
# only creates a per-worktree .venv when `workspace.setup` is non-empty,
# which we deliberately leave empty since we don't have per-task pyproject).
if [ -f "${TARGET_DIR}/pyproject.toml" ]; then
  python -m pip install -e "${TARGET_DIR}"
fi

# Install the exact OpenCode + OpenAI-compatible SDK versions from the
# repository lockfile. `--prefix` keeps everything under this checkout.
if [ -f "${OPENCODE_DIR}/package-lock.json" ]; then
  npm ci --prefix "${OPENCODE_DIR}"
fi

echo "CORAL ready at ${TARGET_DIR} (${REF})"
command -v coral >/dev/null 2>&1 \
  && echo "coral CLI: $(command -v coral) ($(coral --version 2>&1))" \
  || echo "WARNING: coral CLI not on PATH after install — check pip install above"
if [ -x "${OPENCODE_DIR}/node_modules/.bin/opencode" ]; then
  echo "opencode CLI: ${OPENCODE_DIR}/node_modules/.bin/opencode ($("${OPENCODE_DIR}/node_modules/.bin/opencode" --version))"
else
  echo "WARNING: repository-local opencode CLI is missing"
fi
