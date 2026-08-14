from __future__ import annotations

import unittest
from collections import namedtuple
from unittest import mock

from scripts import run_qwen3_coral_all as batch


class QwenCoralBatchTests(unittest.TestCase):
    def test_fixed_command_contains_all_resource_and_serial_flags(self):
        command = batch.case_command(batch.DEFAULT_RUN_ID, "adulyasak2015", resume=False)
        text = " ".join(command)
        for expected in (
            "--coral-attempts 2",
            "--coral-agent-runtime opencode",
            "--coral-agent-count 1",
            "--coral-max-turns 20",
            "--dev-set median",
            "--test-set large_2 large_3 large_4 large_5 --fixed-test-set",
            "--paper-workers 1",
            "--dev-instance-workers 1",
            "--test-instance-workers 1",
            "--exec-mode bubblewrap",
            "--cpus 1",
            "--memory 24G",
            "--memory-reserve 32G",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("qwen/qwen3-coder-plus", text)

    def test_resume_flag_only_when_requested(self):
        fresh = batch.case_command(batch.DEFAULT_RUN_ID, "adulyasak2015", resume=False)
        resumed = batch.case_command(batch.DEFAULT_RUN_ID, "adulyasak2015", resume=True)
        self.assertNotIn("--resume", fresh)
        self.assertIn("--resume", resumed)

    @mock.patch.object(batch, "mem_available_bytes", return_value=47 * 1024**3)
    @mock.patch.object(batch.shutil, "disk_usage")
    def test_case_admission_requires_48_gib(self, disk_usage, _memory):
        disk_usage.return_value = namedtuple("usage", "total used free")(100, 0, 60 * 1024**3)
        self.assertIn("candidate admission", batch.resource_wait_reason())
        self.assertEqual(batch.FIXED_CONFIG["global_guard"]["case_admission_mem_available_gib"], 48)

    @mock.patch.object(batch, "mem_available_bytes", return_value=100 * 1024**3)
    @mock.patch.object(batch.shutil, "disk_usage")
    def test_disk_guard_requires_50_gib(self, disk_usage, _memory):
        disk_usage.return_value = namedtuple("usage", "total used free")(100, 0, 49 * 1024**3)
        self.assertIn("disk floor", batch.resource_wait_reason())

    def test_fingerprint_config_contains_no_secret_values(self):
        serialized = batch.canonical_json(batch.FIXED_CONFIG).decode()
        self.assertIn("OPENAI_API_KEY", serialized)
        self.assertNotIn("sk-", serialized)
        self.assertIn("24G is an inherited per-process", serialized)
        self.assertIn("not an aggregate process-tree cgroup limit", serialized)
        self.assertEqual(batch.FIXED_CONFIG["opencode"]["xdg_state_scope"], "batch_run")
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["restart_backoff_seconds"],
            [5, 10, 20],
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["diagnostic_logging"],
            "print internal errors only",
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["agent_pwd"],
            "forced to the agent worktree before spawn",
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["session_id_fields"],
            ["session_id", "sessionId", "sessionID"],
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["workspace_model_resolution"],
            "required before every agent spawn",
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["provider_cache"],
            "run-local links to repository-pinned dependencies",
        )
        self.assertEqual(
            batch.FIXED_CONFIG["opencode"]["workspace_runtime"],
            "repository-local opencode plugin and node_modules",
        )


if __name__ == "__main__":
    unittest.main()
