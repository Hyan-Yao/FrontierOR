from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "utils"))

import exec_backends


class MemoryParsingTests(unittest.TestCase):
    def test_common_units(self):
        self.assertEqual(exec_backends.parse_memory_bytes("128M"), 128 * 1024**2)
        self.assertEqual(exec_backends.parse_memory_bytes("1.5GiB"), int(1.5 * 1024**3))
        self.assertEqual(exec_backends.parse_memory_bytes(4096), 4096)

    def test_invalid_or_zero(self):
        for value in ("", "-1G", "lots", 0, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exec_backends.parse_memory_bytes(value)

    def test_admission_denies_when_reserve_exceeds_available_memory(self):
        with self.assertRaisesRegex(RuntimeError, "memory admission denied"):
            with exec_backends._memory_admission(1, 1024**6):
                self.fail("impossible reservation should not be admitted")


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("prlimit"), "requires bwrap + prlimit")
class BubblewrapMemoryTests(unittest.TestCase):
    def _run(self, source: str, memory: str, *, under_workspace: bool = False):
        parent = ROOT / "tests" if under_workspace else None
        with tempfile.TemporaryDirectory(prefix="frontieror_memtest_", dir=parent) as tmp:
            root = Path(tmp)
            code = root / "code.py"
            instance = root / "instance.json"
            output = root / "output"
            output.mkdir()
            solution = output / "solution.json"
            code.write_text(source, encoding="utf-8")
            instance.write_text("{}\n", encoding="utf-8")
            result = exec_backends.run_bubblewrap(
                str(code), str(instance), str(solution), 5,
                cfg={"cpus": 1, "memory": memory, "memory_reserve": "0"},
            )
            payload = json.loads(solution.read_text()) if solution.exists() else None
            return result, payload

    def test_small_program_runs_below_limit(self):
        source = """\
import argparse, json
p = argparse.ArgumentParser()
p.add_argument('--instance_path')
p.add_argument('--solution_path')
p.add_argument('--time_limit')
p.add_argument('--log_path', default=None)
a = p.parse_args()
with open(a.solution_path, 'w') as f:
    json.dump({'ok': True}, f)
"""
        (success, output, _), payload = self._run(source, "256M")
        self.assertTrue(success, output)
        self.assertEqual(payload, {"ok": True})

    def test_workspace_home_is_hidden_but_candidate_mount_still_works(self):
        source = """\
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument('--instance_path')
p.add_argument('--solution_path')
p.add_argument('--time_limit')
p.add_argument('--log_path', default=None)
a = p.parse_args()
with open(a.solution_path, 'w') as f:
    json.dump({
        'user_entries': os.listdir('/home/hyao') if os.path.isdir('/home/hyao') else [],
        'env_visible': os.path.exists('/home/hyao/src/FrontierOR/.env'),
    }, f)
"""
        (success, output, _), payload = self._run(source, "256M", under_workspace=True)
        self.assertTrue(success, output)
        interpreter_root = Path((ROOT / ".venv" / "bin" / "python").resolve()).parents[1]
        allowed_home_entries = (
            [interpreter_root.name] if interpreter_root.parent == Path("/home/hyao") else []
        )
        self.assertEqual(
            payload,
            {"user_entries": allowed_home_entries, "env_visible": False},
        )

    def test_large_allocation_is_stopped(self):
        source = """\
import argparse
p = argparse.ArgumentParser()
p.add_argument('--instance_path')
p.add_argument('--solution_path')
p.add_argument('--time_limit')
p.add_argument('--log_path', default=None)
p.parse_args()
blob = bytearray(256 * 1024 * 1024)
raise RuntimeError(f'allocation unexpectedly succeeded: {len(blob)}')
"""
        (success, output, _), payload = self._run(source, "128M")
        self.assertFalse(success)
        self.assertIsNone(payload)
        self.assertIn("memory limit", output.lower())


if __name__ == "__main__":
    unittest.main()
