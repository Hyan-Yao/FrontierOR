"""Compatibility wrapper around CORAL's CLI.

Only patches the generated Codex ``config.toml`` (the pinned CORAL revision
writes ``[tools].web_search = "disabled"``, which the locally-installed Codex
CLI rejects). The instruction template (``CORAL.md`` / ``AGENTS.md``) is kept
upstream so single/multi-agent auto-switching works and the full collaborative
workflow is intact. Project-specific guardrails (e.g. forbid reading
``gurobi_solution/``) are injected via ``task.tips`` in ``task.yaml``, which
CORAL renders as a ``## Tips`` section appended to the original template.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path


OPENCODE_PROVIDER_PACKAGE = "@ai-sdk/openai-compatible@3.0.16"
OPENCODE_MODEL = "qwen3-coder-plus"
OPENCODE_VERSION = "1.18.5"
ROOT_DIR = Path(__file__).resolve().parents[2]
LOCAL_OPENCODE_NODE_MODULES = ROOT_DIR / "tools" / "opencode" / "node_modules"
MAX_CONSECUTIVE_DEAD_RESTARTS = 3
RESTART_BACKOFF_BASE_SECONDS = 5


def restart_backoff_seconds(consecutive_failures: int) -> int:
    return min(30, RESTART_BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)))


def build_opencode_settings(coral_dir: Path, *, research: bool = False) -> dict:
    """Build a secret-free OpenCode config for the Frontier endpoint."""
    private_pattern = str(coral_dir.resolve() / "private") + "/**"
    public_pattern = str(coral_dir.resolve() / "public") + "/**"
    return {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["frontier"],
        # OpenCode's CLI has no --max-turns flag. Its built-in primary agent is
        # `build`, so `steps` is the supported hard ceiling for agentic turns.
        "agent": {"build": {"steps": 20}},
        "permission": {
            "*": "allow",
            "external_directory": {
                "*": "deny",
                public_pattern: "allow",
            },
            "read": {private_pattern: "deny"},
            "bash": {private_pattern: "deny"},
            "edit": {private_pattern: "deny"},
            "write": {private_pattern: "deny"},
            "question": "deny",
            "doom_loop": "allow",
            "webfetch": "allow" if research else "deny",
            "websearch": "allow" if research else "deny",
        },
        "provider": {
            "frontier": {
                "npm": OPENCODE_PROVIDER_PACKAGE,
                "name": "Frontier OpenAI-compatible",
                "options": {
                    "apiKey": "{env:OPENAI_API_KEY}",
                    "baseURL": "{env:OPENAI_BASE_URL}",
                    # The configured endpoint returns intermittent server
                    # errors when Bun reuses a streaming HTTP connection.
                    # A fresh connection per request is stable for long
                    # OpenCode tool-call loops.
                    "headers": {"Connection": "close"},
                },
                "models": {
                    OPENCODE_MODEL: {
                        "name": OPENCODE_MODEL,
                    }
                },
            }
        },
    }


def seed_opencode_workspace_runtime(opencode_dir: Path) -> Path:
    """Make workspace OpenCode/plugin dependencies deterministic and local."""
    plugin = LOCAL_OPENCODE_NODE_MODULES / "@opencode-ai" / "plugin"
    try:
        plugin_metadata = json.loads(
            (plugin / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("repository-local OpenCode plugin is unavailable") from exc
    if plugin_metadata.get("version") != OPENCODE_VERSION:
        raise RuntimeError("repository-local OpenCode plugin version is not pinned to the CLI")

    opencode_dir.mkdir(parents=True, exist_ok=True)
    package = {
        "dependencies": {"@opencode-ai/plugin": OPENCODE_VERSION}
    }
    (opencode_dir / "package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )
    stale_lock = opencode_dir / "package-lock.json"
    if stale_lock.exists() or stale_lock.is_symlink():
        stale_lock.unlink()

    node_modules = opencode_dir / "node_modules"
    expected_target = LOCAL_OPENCODE_NODE_MODULES.resolve()
    if node_modules.is_symlink() and node_modules.resolve() == expected_target:
        return node_modules
    if node_modules.is_symlink() or node_modules.is_file():
        node_modules.unlink()
    elif node_modules.exists():
        shutil.rmtree(node_modules)
    node_modules.symlink_to(expected_target, target_is_directory=True)
    return node_modules


def _patch_opencode_settings():
    from coral.agent import manager as manager_module
    from coral.workspace import worktree as worktree_module
    import coral.workspace as workspace_module

    def setup_opencode_settings(
        worktree_path: Path,
        coral_dir: Path,
        *,
        research: bool = True,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
    ) -> None:
        del gateway_url, gateway_api_key
        opencode_dir = worktree_path / ".opencode"
        opencode_dir.mkdir(exist_ok=True)
        settings = build_opencode_settings(coral_dir, research=research)
        (opencode_dir / "opencode.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
        seed_opencode_workspace_runtime(opencode_dir)

    worktree_module.setup_opencode_settings = setup_opencode_settings
    workspace_module.setup_opencode_settings = setup_opencode_settings
    manager_module.setup_opencode_settings = setup_opencode_settings


def _patch_agent_restart_policy():
    """Bound crash-only restarts while preserving normal eval-driven resumes."""
    from coral.agent import manager as manager_module

    original_restart = manager_module.AgentManager._restart_agent
    logger = logging.getLogger("coral.agent.manager")

    def bounded_restart(self, idx, prompt=None, prompt_source=None):
        handle = self.handles[idx]
        agent_id = handle.agent_id
        eval_count = self._get_eval_count()
        state = getattr(self, "_frontier_dead_restart_state", {})
        previous_eval_count, failures = state.get(agent_id, (eval_count, 0))
        if eval_count != previous_eval_count:
            failures = 0
        failures += 1
        state[agent_id] = (eval_count, failures)
        self._frontier_dead_restart_state = state

        if failures > MAX_CONSECUTIVE_DEAD_RESTARTS:
            message = (
                f"Agent {agent_id} exited {failures} consecutive times without "
                f"a finalized attempt; refusing an infinite restart loop"
            )
            logger.error(message)
            self.stop_all()
            raise RuntimeError(message)

        delay = restart_backoff_seconds(failures)
        logger.warning(
            "Backing off %ss before crash restart %s/%s for %s",
            delay,
            failures,
            MAX_CONSECUTIVE_DEAD_RESTARTS,
            agent_id,
        )
        if self._stop_event.wait(timeout=delay):
            raise RuntimeError(f"Agent restart for {agent_id} cancelled during shutdown")
        return original_restart(
            self,
            idx,
            prompt=prompt,
            prompt_source=prompt_source,
        )

    manager_module.AgentManager._restart_agent = bounded_restart


def _patch_codex_settings():
    from coral.agent import manager as manager_module
    from coral.workspace import worktree as worktree_module
    import coral.workspace as workspace_module

    def setup_codex_settings(
        worktree_path: Path,
        coral_dir: Path,
        *,
        research: bool = True,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
    ) -> None:
        del coral_dir, research, gateway_api_key
        codex_dir = worktree_path / ".codex"
        codex_dir.mkdir(exist_ok=True)
        lines = [
            'model = "gpt-5.4"',
            'approval_policy = "never"',
            'sandbox_mode = "workspace-write"',
            'personality = "pragmatic"',
        ]
        if gateway_url:
            lines += [
                'model_provider = "litellm"',
                "",
                "[model_providers.litellm]",
                'name = "LiteLLM Proxy"',
                f'base_url = "{gateway_url}/v1"',
                'wire_api = "responses"',
                'env_key = "OPENAI_API_KEY"',
            ]
        (codex_dir / "config.toml").write_text("\n".join(lines) + "\n")

    worktree_module.setup_codex_settings = setup_codex_settings
    workspace_module.setup_codex_settings = setup_codex_settings
    manager_module.setup_codex_settings = setup_codex_settings


def main():
    _patch_codex_settings()
    _patch_opencode_settings()
    _patch_agent_restart_policy()
    from coral.cli import main as coral_main

    coral_main()


if __name__ == "__main__":
    main()
