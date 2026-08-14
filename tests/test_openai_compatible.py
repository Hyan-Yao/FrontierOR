from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external" / "coral"))

from coral.agent.builtin.opencode import (
    _extract_opencode_session_id,
    prepare_agent_env,
    verify_model_available,
)
from coral.agent.runtime import _extract_session_id as _extract_generic_session_id

import one_shot_eval
from test_time_self_evolution import run_eval_modes
from test_time_self_evolution.coral import runner as coral_runner
from test_time_self_evolution.coral.coral_cli_wrapper import (
    MAX_CONSECUTIVE_DEAD_RESTARTS,
    build_opencode_settings,
    restart_backoff_seconds,
    seed_opencode_workspace_runtime,
)


class FakeResponse:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }


class OpenAICompatibleTests(unittest.TestCase):
    def test_self_evolve_uses_native_qwen_model_id(self):
        config = {
            "LLM_API_PROVIDER": "openai_compatible",
            "models": ["qwen/qwen3-coder-plus"],
        }
        self.assertEqual(
            run_eval_modes.resolve_model_id(config, "qwen3-coder-plus"),
            "qwen3-coder-plus",
        )

    @mock.patch("one_shot_eval.requests.post", return_value=FakeResponse())
    def test_seed_request_uses_custom_base_and_native_qwen_model(self, post):
        config = {
            "LLM_API_PROVIDER": "openai_compatible",
            "LLM_API_KEY": "test-secret",
            "LLM_API_BASE": "https://example.invalid/v1/",
        }
        content, usage = one_shot_eval.call_openrouter(
            [{"role": "user", "content": "hello"}],
            config,
            "qwen/qwen3-coder-plus",
        )
        self.assertEqual(content, "ok")
        self.assertEqual(usage["completion_tokens"], 2)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://example.invalid/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "qwen3-coder-plus")

    def test_coral_env_uses_repo_local_binaries_and_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "opencode-runtime"
            env = coral_runner.prepare_coral_env(
                {"PATH": "/usr/bin"},
                {"LLM_API_KEY": "secret", "LLM_API_BASE": "https://example.invalid/v1/"},
                opencode_state_root=str(state_root),
            )
        self.assertEqual(env["OPENAI_API_KEY"], "secret")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://example.invalid/v1")
        self.assertTrue(env["PATH"].startswith(coral_runner.LOCAL_OPENCODE_BIN))
        self.assertEqual(env["XDG_CACHE_HOME"], str(state_root / "cache"))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(state_root / "config"))
        self.assertEqual(env["XDG_DATA_HOME"], str(state_root / "data"))
        self.assertEqual(env["XDG_STATE_HOME"], str(state_root / "state"))

    def test_run_local_provider_cache_is_seeded_from_pinned_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CACHE_HOME": str(Path(tmp) / "cache")}
            destination = coral_runner.seed_opencode_provider_cache(env)
            for relative in coral_runner.OPENCODE_PROVIDER_CACHE_DEPS:
                dependency = destination / "node_modules" / relative
                self.assertTrue(dependency.is_symlink())
                self.assertTrue((dependency / "package.json").is_file())

            # An incomplete cache is replaced atomically on the next check.
            broken = destination / "node_modules" / "zod"
            broken.unlink()
            self.assertFalse(broken.exists())
            repaired = coral_runner.seed_opencode_provider_cache(env)
            self.assertEqual(repaired, destination)
            self.assertTrue(broken.is_symlink())

    @mock.patch("test_time_self_evolution.coral.runner.subprocess.run")
    def test_provider_prewarm_is_redacted_and_reused(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"type":"text","text":"secret https://example.invalid/v1 OK"}\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = coral_runner.CoralTask(
                task_name="test",
                task_dir=str(root / "task"),
                config_path=str(root / "task.yaml"),
                run_dir=str(root / "run"),
                coral_dir=str(root / "run" / ".coral"),
                repo_dir=str(root / "run" / "repo"),
                log_path=str(root / "coral.log"),
            )
            env = coral_runner.prepare_coral_env(
                {"PATH": "/usr/bin"},
                {"LLM_API_KEY": "secret", "LLM_API_BASE": "https://example.invalid/v1"},
                opencode_state_root=str(root / "opencode-runtime"),
            )
            first = coral_runner.prewarm_opencode_provider(
                task, env, "frontier/qwen3-coder-plus"
            )
            second = coral_runner.prewarm_opencode_provider(
                task, env, "frontier/qwen3-coder-plus"
            )
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            log = (root / "opencode-runtime" / "provider-prewarm.log").read_text()
            self.assertNotIn("secret", log)
            self.assertNotIn("https://example.invalid/v1", log)
            self.assertIn("<OPENAI_API_KEY>", log)

    def test_dead_agent_restart_policy_is_bounded_and_backed_off(self):
        self.assertEqual(MAX_CONSECUTIVE_DEAD_RESTARTS, 3)
        self.assertEqual([restart_backoff_seconds(i) for i in (1, 2, 3)], [5, 10, 20])

    def test_opencode_config_uses_frontier_provider_without_plaintext_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_opencode_settings(Path(tmp) / ".coral", research=False)
        provider = settings["provider"]["frontier"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible@3.0.16")
        self.assertIn("qwen3-coder-plus", provider["models"])
        self.assertEqual(provider["options"]["apiKey"], "{env:OPENAI_API_KEY}")
        self.assertEqual(provider["options"]["baseURL"], "{env:OPENAI_BASE_URL}")
        self.assertEqual(provider["options"]["headers"], {"Connection": "close"})
        self.assertEqual(settings["agent"]["build"]["steps"], 20)
        self.assertEqual(
            list(settings["permission"]["external_directory"]),
            ["*", str((Path(tmp) / ".coral" / "public").resolve()) + "/**"],
        )
        self.assertNotIn("test-secret", str(settings))

    def test_workspace_runtime_uses_pinned_local_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            opencode_dir = Path(tmp) / ".opencode"
            node_modules = seed_opencode_workspace_runtime(opencode_dir)
            self.assertTrue(node_modules.is_symlink())
            self.assertTrue(
                (node_modules / "@opencode-ai" / "plugin" / "package.json").is_file()
            )
            package = json.loads((opencode_dir / "package.json").read_text())
            self.assertEqual(
                package["dependencies"]["@opencode-ai/plugin"], "1.18.5"
            )

    def test_opencode_runtime_model_has_frontier_provider(self):
        self.assertEqual(
            coral_runner._coral_model_for_runtime("qwen3-coder-plus", "opencode", None),
            "frontier/qwen3-coder-plus",
        )

    @mock.patch("coral.agent.builtin.opencode.subprocess.run")
    def test_agent_workspace_resolves_custom_model_before_spawn(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="frontier/qwen3-coder-plus\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            verify_model_available(
                "frontier/qwen3-coder-plus", Path(tmp), {"PATH": "/usr/bin"}
            )
        run.assert_called_once_with(
            ["opencode", "models", "frontier"],
            cwd=mock.ANY,
            env={"PATH": "/usr/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )

    @mock.patch(
        "coral.agent.builtin.opencode._clean_env",
        return_value={"PWD": "/stale/repository/root", "PATH": "/usr/bin"},
    )
    def test_agent_environment_replaces_stale_pwd(self, _clean_env):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "agent-1"
            worktree.mkdir()
            env = prepare_agent_env(worktree)
        self.assertEqual(env["PWD"], str(worktree.resolve()))
        self.assertEqual(env["VIRTUAL_ENV"], str(worktree / ".venv"))
        self.assertTrue(env["PATH"].startswith(str(worktree / ".venv" / "bin")))

    def test_opencode_session_extractor_accepts_uppercase_id_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agent.log"
            log.write_text('{"type":"step_start","sessionID":"ses_test"}\n')
            self.assertEqual(_extract_opencode_session_id(log), "ses_test")

    def test_generic_interrupt_extractor_accepts_opencode_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agent.log"
            log.write_text(
                '{"type":"step_start","sessionID":"ses_fallback"}\n'
                '{"type":"result","sessionID":"ses_result"}\n'
            )
            self.assertEqual(_extract_generic_session_id(log), "ses_result")

    @mock.patch("coral.agent.builtin.opencode.subprocess.run")
    def test_agent_workspace_rejects_missing_custom_model(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="frontier/another-model\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                verify_model_available(
                    "frontier/qwen3-coder-plus", Path(tmp), {"PATH": "/usr/bin"}
                )


if __name__ == "__main__":
    unittest.main()
