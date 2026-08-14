#!/usr/bin/env python3
"""Resumable serial Qwen3-Coder-Plus × CORAL evaluation over all local tasks."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.utils.paper_discovery import discover_valid_papers  # noqa: E402
from test_time_self_evolution.coral.coral_cli_wrapper import (  # noqa: E402
    OPENCODE_MODEL,
    OPENCODE_PROVIDER_PACKAGE,
    build_opencode_settings,
)


DEFAULT_RUN_ID = "qwen3-coder-plus-coral-all180-a2-24g-20260806"
EXPECTED_CASE_COUNT = 180
MODEL_ID = "qwen3-coder-plus"
OPENCODE_MODEL_ID = f"frontier/{MODEL_ID}"
MEMORY_LIMIT = "24G"
MEMORY_RESERVE = "32G"
MEMORY_LIMIT_BYTES = 24 * 1024**3
CASE_ADMISSION_GIB = 48
CASE_ADMISSION_BYTES = CASE_ADMISSION_GIB * 1024**3
GLOBAL_MEMORY_FLOOR_BYTES = 32 * 1024**3
DISK_FLOOR_BYTES = 50 * 1024**3
TEST_INSTANCES = ["large_2", "large_3", "large_4", "large_5"]
SOURCE_FINGERPRINT_FILES = (
    "one_shot_eval.py",
    "paper_meta_info.json",
    "scripts/run_qwen3_coral_all.py",
    "scripts/run_qwen3_coral_all.sh",
    "scripts/utils/instance_paths.py",
    "scripts/utils/paper_discovery.py",
    "external/coral/coral/agent/runtime.py",
    "external/coral/coral/agent/builtin/opencode.py",
    "scripts/utils/exec_backends.py",
    "test_time_self_evolution/run_eval_modes.py",
    "test_time_self_evolution/eval_modes.py",
    "test_time_self_evolution/coral/runner.py",
    "test_time_self_evolution/coral/coral_cli_wrapper.py",
    "test_time_self_evolution/openevolve/evaluator.py",
    "test_time_self_evolution/scoring/building_blocks.py",
    "tools/opencode/package.json",
    "tools/opencode/package-lock.json",
)

FIXED_CONFIG: dict[str, Any] = {
    "model": MODEL_ID,
    "endpoint_env": "OPENAI_BASE_URL (fallback OPENAI_API_BASE)",
    "api_key_env": "OPENAI_API_KEY",
    "framework": "coral",
    "attempts": 2,
    "max_seconds": "auto",
    "attempts_budget_multiplier": 1.3,
    "agent_runtime": "opencode",
    "agent_model": OPENCODE_MODEL_ID,
    "agent_count": 1,
    "max_turns": 20,
    "heartbeat": {"pivot_plateau_every": 5, "reflect_every": 0, "consolidate_every": 0},
    "tiny": {"instances": ["tiny"], "time_limit_seconds": 300, "gap_threshold": 0.10},
    "dev": {"selection": "median_tau_g", "time_limit_seconds": 3600, "workers": 1},
    "test": {"instances": TEST_INSTANCES, "time_limit_seconds": 3600, "workers": 1},
    "paper_workers": 1,
    "candidate": {
        "exec_mode": "bubblewrap",
        "cpus": 1,
        "memory": MEMORY_LIMIT,
        "memory_reserve": MEMORY_RESERVE,
        "network": "unshared when supported; credentials always cleared",
    },
    "global_guard": {
        "case_admission_mem_available_gib": CASE_ADMISSION_GIB,
        "memory_floor_gib": 32,
        "disk_floor_gib": 50,
    },
    "resource_limit_note": (
        "No usable cgroup/Docker is assumed. 24G is an inherited per-process "
        "RLIMIT_AS ceiling, not an aggregate process-tree cgroup limit."
    ),
    "opencode": {
        "provider": "frontier",
        "provider_package": OPENCODE_PROVIDER_PACKAGE,
        "model": OPENCODE_MODEL_ID,
        "xdg_state_scope": "batch_run",
        "live_provider_prewarm": True,
        "transport_headers": {"Connection": "close"},
        "diagnostic_logging": "print internal errors only",
        "agent_pwd": "forced to the agent worktree before spawn",
        "session_id_fields": ["session_id", "sessionId", "sessionID"],
        "workspace_model_resolution": "required before every agent spawn",
        "provider_cache": "run-local links to repository-pinned dependencies",
        "workspace_runtime": "repository-local opencode plugin and node_modules",
        "max_consecutive_dead_restarts": 3,
        "restart_backoff_seconds": [5, 10, 20],
    },
}


STOP_REQUESTED = False
ACTIVE_PROCESS: subprocess.Popen | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def load_trusted_dotenv() -> None:
    """Load the trusted shell-format .env without printing any values."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    script = 'set -a; source "$1"; env -0'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(env_path)],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    for item in result.stdout.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        os.environ[key.decode(errors="surrogateescape")] = value.decode(errors="surrogateescape")


def effective_base_url() -> str:
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
    if not base:
        raise RuntimeError("OPENAI_BASE_URL (or legacy OPENAI_API_BASE) is not set")
    return base.rstrip("/")


def batch_paths(run_id: str) -> dict[str, Path]:
    root = ROOT / "eval" / "coral_batches" / run_id
    return {
        "root": root,
        "manifest": root / "manifest.json",
        "config": root / "config.json",
        "fingerprint": root / "config.sha256",
        "state": root / "state.json",
        "progress": root / "progress.json",
        "preflight": root / "preflight.json",
        "pid": root / "batch.pid",
        "lock": root / "batch.lock",
        "logs": root / "case_logs",
        "commands": root / "case_commands",
        "restarts": root / "restarts",
    }


def freeze_manifest(run_id: str, paths: dict[str, Path]) -> tuple[list[str], dict[str, Any]]:
    source_hashes = {
        name: sha256_file(ROOT / name)
        for name in SOURCE_FINGERPRINT_FILES
    }
    config_doc = {
        "schema_version": 1,
        "run_id": run_id,
        "config": FIXED_CONFIG,
        "source_sha256": source_hashes,
    }
    fingerprint = hashlib.sha256(canonical_json(config_doc)).hexdigest()
    existing_manifest = read_json(paths["manifest"])
    existing_config = read_json(paths["config"])
    existing_fingerprint = None
    try:
        existing_fingerprint = paths["fingerprint"].read_text(encoding="utf-8").strip()
    except OSError:
        pass

    if existing_manifest is not None:
        papers = existing_manifest.get("papers") or []
        if len(papers) != EXPECTED_CASE_COUNT or papers != sorted(set(papers)):
            raise RuntimeError("Frozen manifest is corrupt or no longer contains exactly 180 sorted cases")
        if existing_config != config_doc or existing_fingerprint != fingerprint:
            raise RuntimeError(
                "Frozen run configuration/source fingerprint differs from the current checkout; "
                "use a new run ID instead of mutating this run"
            )
        return papers, config_doc

    papers = discover_valid_papers(ROOT / "frontier-or")
    if len(papers) != EXPECTED_CASE_COUNT:
        raise RuntimeError(
            f"Expected exactly {EXPECTED_CASE_COUNT} valid paper tasks, discovered {len(papers)}"
        )
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": utc_now(),
        "case_count": len(papers),
        "papers": papers,
    }
    atomic_json(paths["manifest"], manifest)
    atomic_json(paths["config"], config_doc)
    paths["fingerprint"].write_text(fingerprint + "\n", encoding="utf-8")
    return papers, config_doc


def initial_state(run_id: str, papers: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "pid": None,
        "pgid": None,
        "cases": {
            paper: {
                "status": "pending",
                "runs": 0,
                "resume_count": 0,
                "started_at": None,
                "ended_at": None,
                "duration_seconds": 0.0,
                "error": None,
                "wait_reason": None,
            }
            for paper in papers
        },
    }


def save_state(paths: dict[str, Path], state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(paths["state"], state)
    update_progress(paths, state)


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def result_rows(run_id: str) -> list[dict[str, str]]:
    csv_path = ROOT / "eval" / "eval_test_results_coral.csv"
    if not csv_path.is_file():
        return []
    try:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            return [row for row in csv.DictReader(handle) if row.get("run_id") == run_id]
    except OSError:
        return []


def update_progress(paths: dict[str, Path], state: dict[str, Any]) -> None:
    counts = Counter(case.get("status", "unknown") for case in state["cases"].values())
    rows = result_rows(state["run_id"])
    feasible = [row for row in rows if parse_bool(row.get("feasible"))]
    quality = []
    qte_known = []
    qte_pass = []
    failure_reasons: Counter[str] = Counter()
    for row in rows:
        try:
            gap = float(row["gap"])
        except (KeyError, TypeError, ValueError):
            gap = None
        quality_ok = parse_bool(row.get("feasible")) and gap is not None and gap <= 0.01
        quality.append(quality_ok)
        try:
            delta_time = float(row["delta_time"])
        except (KeyError, TypeError, ValueError):
            delta_time = None
        if delta_time is not None:
            qte_known.append(row)
            qte_pass.append(quality_ok and delta_time <= 0)
        if row.get("fail_reason"):
            failure_reasons[row["fail_reason"]] += 1
    for case in state["cases"].values():
        if case.get("status") == "failed" and case.get("error"):
            failure_reasons[str(case["error"]).split(":", 1)[0][:120]] += 1
    total_runtime = sum(float(case.get("duration_seconds") or 0) for case in state["cases"].values())
    progress = {
        "run_id": state["run_id"],
        "status": state.get("status"),
        "updated_at": utc_now(),
        "cases": dict(sorted(counts.items())),
        "test_rows": len(rows),
        "feasibility_rate": len(feasible) / len(rows) if rows else None,
        "solution_quality_1pct": sum(quality) / len(rows) if rows else None,
        "qte_known_cases": len(qte_known),
        "qte_rate_known_cases": sum(qte_pass) / len(qte_pass) if qte_pass else None,
        "case_runtime_seconds": round(total_runtime, 2),
        "failure_reasons": dict(failure_reasons.most_common()),
        "resource_limit_note": FIXED_CONFIG["resource_limit_note"],
    }
    atomic_json(paths["progress"], progress)


def mem_available_bytes() -> int | None:
    try:
        with Path("/proc/meminfo").open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def resource_wait_reason() -> str | None:
    available = mem_available_bytes()
    if available is None:
        return "cannot read /proc/meminfo"
    if available < GLOBAL_MEMORY_FLOOR_BYTES:
        return f"global memory floor: MemAvailable={available / 1024**3:.1f}GiB < 32GiB"
    disk = shutil.disk_usage(ROOT).free
    if disk < DISK_FLOOR_BYTES:
        return f"global disk floor: free={disk / 1024**3:.1f}GiB < 50GiB"
    if available < CASE_ADMISSION_BYTES:
        return (
            f"candidate admission: MemAvailable={available / 1024**3:.1f}GiB "
            f"< {CASE_ADMISSION_GIB}GiB"
        )
    return None


def wait_for_resources(paths: dict[str, Path], state: dict[str, Any], paper: str, poll: int) -> bool:
    case = state["cases"][paper]
    while not STOP_REQUESTED:
        reason = resource_wait_reason()
        if reason is None:
            if case.get("status") == "waiting":
                case["status"] = "pending"
                case["wait_reason"] = None
                save_state(paths, state)
            return True
        if case.get("wait_reason") != reason or case.get("status") != "waiting":
            print(f"[guard] {paper}: waiting; {reason}", flush=True)
            case["status"] = "waiting"
            case["wait_reason"] = reason
            state["status"] = "waiting_resources"
            save_state(paths, state)
        time.sleep(max(1, min(poll, 60)))
    return False


def opencode_binary() -> Path:
    return ROOT / "tools" / "opencode" / "node_modules" / ".bin" / "opencode"


def run_preflight(paths: dict[str, Path], *, skip_endpoint_smoke: bool) -> dict[str, Any]:
    existing = read_json(paths["preflight"])
    if (
        existing
        and existing.get("status") == "passed"
        and (skip_endpoint_smoke or existing.get("endpoint_smoke") != "skipped")
    ):
        return existing
    checks: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "running",
        "secrets_persisted": False,
    }
    required_commands = ["bwrap", "prlimit", "git"]
    missing = [name for name in required_commands if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing required commands: {missing}")
    if not opencode_binary().is_file():
        raise RuntimeError(
            "repository-local OpenCode is not installed; run "
            "npm ci --prefix tools/opencode"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    base_url = effective_base_url()

    version = subprocess.run(
        [str(opencode_binary()), "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    checks["opencode_version"] = version

    config_dir = paths["root"] / "opencode_preflight"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "opencode.json"
    settings = build_opencode_settings(config_dir / ".coral", research=False)
    config_text = json.dumps(settings, indent=2) + "\n"
    api_key = os.environ["OPENAI_API_KEY"]
    if api_key in config_text:
        raise RuntimeError("generated OpenCode config contains the plaintext API key")
    config_path.write_text(config_text, encoding="utf-8")
    checks["opencode_config"] = str(config_path.relative_to(ROOT))
    checks["opencode_config_secret_refs"] = ["{env:OPENAI_API_KEY}", "{env:OPENAI_BASE_URL}"]

    model_env = os.environ.copy()
    model_env["OPENCODE_CONFIG"] = str(config_path)
    model_env["OPENAI_BASE_URL"] = base_url
    model_env["HOME"] = str(config_dir / "home")
    model_env["XDG_DATA_HOME"] = str(config_dir / "xdg_data")
    model_env["XDG_CACHE_HOME"] = str(config_dir / "xdg_cache")
    models = subprocess.run(
        [str(opencode_binary()), "models", "frontier"],
        cwd=config_dir,
        env=model_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    model_output = (models.stdout + "\n" + models.stderr).strip()
    if models.returncode != 0 or OPENCODE_MODEL_ID not in model_output:
        raise RuntimeError(
            f"OpenCode did not resolve {OPENCODE_MODEL_ID} (exit={models.returncode}): "
            f"{model_output[-800:]}"
        )
    checks["opencode_model_resolved"] = OPENCODE_MODEL_ID

    if skip_endpoint_smoke:
        checks["endpoint_smoke"] = "skipped"
    else:
        started = time.monotonic()
        payload = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "Call the echo_probe tool with value coral-smoke."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "echo_probe",
                    "description": "Return a probe value.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "echo_probe"}},
            "max_tokens": 64,
        }
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls or tool_calls[0].get("function", {}).get("name") != "echo_probe":
            raise RuntimeError("endpoint response did not contain the required tool call format")
        checks["endpoint_smoke"] = {
            "request_model": MODEL_ID,
            "tool_calling": True,
            "latency_seconds": round(time.monotonic() - started, 3),
            "response_model": data.get("model"),
        }

    checks["status"] = "passed"
    checks["finished_at"] = utc_now()
    atomic_json(paths["preflight"], checks)
    return checks


def case_base_dir(run_id: str, paper: str) -> Path:
    return ROOT / "eval" / "coral" / run_id / paper / MODEL_ID


def valid_coral_checkpoint(run_id: str, paper: str) -> bool:
    base = case_base_dir(run_id, paper)
    coral_dir = base / "coral_run" / ".coral"
    public = coral_dir / "public"
    agents = base / "coral_run" / "agents"
    if not (
        (coral_dir / "config.yaml").is_file()
        and (public / ".git").exists()
        and any(agents.glob("agent-*"))
        and (base / "coral_task" / "seed" / "code.py").is_file()
    ):
        return False
    probe = subprocess.run(
        ["git", "-C", str(public), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return probe.returncode == 0


def archive_invalid_partial(paths: dict[str, Path], run_id: str, paper: str) -> str | None:
    base = case_base_dir(run_id, paper)
    if not base.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = paths["restarts"] / paper / stamp
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(base), str(destination))
    return str(destination)


def case_command(run_id: str, paper: str, *, resume: bool) -> list[str]:
    command = [
        str(ROOT / ".venv" / "bin" / "python"), "-u",
        str(ROOT / "test_time_self_evolution" / "run_eval_modes.py"),
        "--modes", "self_evolve",
        "--framework", "coral",
        "--paper-id", paper,
        "--primary-model", MODEL_ID,
        "--secondary-model", MODEL_ID,
        "--coral-attempts", "2",
        "--coral-max-seconds", "auto",
        "--coral-attempts-budget-multiplier", "1.3",
        "--coral-agent-runtime", "opencode",
        "--coral-agent-count", "1",
        "--coral-agent-model", OPENCODE_MODEL_ID,
        "--coral-max-turns", "20",
        "--coral-heartbeat-reflect-every", "0",
        "--coral-heartbeat-pivot-every", "5",
        "--coral-heartbeat-consolidate-every", "0",
        "--stage1-instances", "tiny",
        "--dev-set", "median",
        "--test-set", *TEST_INSTANCES,
        "--fixed-test-set",
        "--stage1-time-limit", "300",
        "--stage2-time-limit", "3600",
        "--test-time-limit", "3600",
        "--stage1-gap-threshold", "0.10",
        "--stage2-time-policy", "gurobi_time",
        "--test-time-policy", "uniform",
        "--paper-workers", "1",
        "--dev-instance-workers", "1",
        "--test-instance-workers", "1",
        "--exec-mode", "bubblewrap",
        "--cpus", "1",
        "--memory", MEMORY_LIMIT,
        "--memory-reserve", MEMORY_RESERVE,
        "--run-id", run_id,
    ]
    if resume:
        command.append("--resume")
    return command


def _limit_case_process() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _signal_handler(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"[batch] signal {signum} received; stopping current case safely", flush=True)
    proc = ACTIVE_PROCESS
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()


def finalized_attempts(base: Path) -> list[dict[str, Any]]:
    attempts_dir = base / "coral_run" / ".coral" / "public" / "attempts"
    attempts = []
    for path in sorted(attempts_dir.glob("*.json")):
        payload = read_json(path)
        if isinstance(payload, dict) and payload.get("status") != "pending":
            attempts.append(payload)
    return attempts


def validate_case(run_id: str, paper: str, command: list[str]) -> dict[str, Any]:
    base = case_base_dir(run_id, paper)
    attempts = finalized_attempts(base)
    failures = []
    provider_marker = base.parents[1] / ".opencode_runtime" / "provider-ready.json"
    marker_payload = read_json(provider_marker)
    if not (
        isinstance(marker_payload, dict)
        and marker_payload.get("status") == "passed"
        and marker_payload.get("model") == OPENCODE_MODEL_ID
        and marker_payload.get("provider_package") == OPENCODE_PROVIDER_PACKAGE
    ):
        failures.append("run-local OpenCode provider prewarm marker missing or invalid")
    if len(attempts) < 2:
        failures.append(f"expected >=2 finalized attempts, found {len(attempts)}")
    if not (base / "selected_code.py").is_file():
        failures.append("selected_code.py missing")
    if not valid_coral_checkpoint(run_id, paper):
        failures.append("valid CORAL checkpoint missing")
    task_path = base / "coral_task" / "task.yaml"
    task = {}
    try:
        task = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        failures.append("task.yaml missing or invalid")
    grader_args = ((task.get("grader") or {}).get("args") or {})
    agents = task.get("agents") or {}
    expected_task = {
        "exec_mode": "bubblewrap",
        "memory": MEMORY_LIMIT,
        "memory_reserve": MEMORY_RESERVE,
        "runtime": "opencode",
        "model": OPENCODE_MODEL_ID,
        "count": 1,
        "max_turns": 20,
    }
    actual_task = {
        "exec_mode": grader_args.get("exec_mode"),
        "memory": (grader_args.get("exec_cfg") or {}).get("memory"),
        "memory_reserve": (grader_args.get("exec_cfg") or {}).get("memory_reserve"),
        "runtime": agents.get("runtime"),
        "model": agents.get("model"),
        "count": agents.get("count"),
        "max_turns": agents.get("max_turns"),
    }
    if actual_task != expected_task:
        failures.append(f"task resource/runtime config mismatch: {actual_task}")
    command_text = " ".join(command)
    for token in ("--exec-mode bubblewrap", "--memory 24G", "--memory-reserve 32G"):
        if token not in command_text:
            failures.append(f"case command missing {token!r}")
    rows = [
        row for row in result_rows(run_id)
        if row.get("paper_id") == paper and row.get("model") == MODEL_ID
    ]
    row_instances = {row.get("instance") for row in rows}
    missing_rows = sorted(set(TEST_INSTANCES) - row_instances)
    if missing_rows:
        failures.append(f"test CSV rows missing: {missing_rows}")
    return {
        "passed": not failures,
        "checked_at": utc_now(),
        "finalized_attempts": len(attempts),
        "test_csv_rows": len(rows),
        "selected_code": str(base / "selected_code.py"),
        "checkpoint": str(base / "coral_run" / ".coral" / "public" / ".git"),
        "opencode_provider_marker": str(provider_marker),
        "actual_task": actual_task,
        "failures": failures,
    }


def run_case(
    paths: dict[str, Path],
    state: dict[str, Any],
    run_id: str,
    paper: str,
    *,
    resume: bool,
) -> bool:
    global ACTIVE_PROCESS
    case = state["cases"][paper]
    command = case_command(run_id, paper, resume=resume)
    command_record = {
        "paper_id": paper,
        "created_at": utc_now(),
        "resume": resume,
        "argv": command,
        "rlimit_as_bytes_per_process": MEMORY_LIMIT_BYTES,
        "environment_references": ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
    }
    atomic_json(paths["commands"] / f"{paper}.json", command_record)
    log_path = paths["logs"] / f"{paper}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    case["status"] = "running"
    case["runs"] = int(case.get("runs") or 0) + 1
    if resume:
        case["resume_count"] = int(case.get("resume_count") or 0) + 1
    case["started_at"] = utc_now()
    case["ended_at"] = None
    case["error"] = None
    case["wait_reason"] = None
    case["log"] = str(log_path)
    case["command"] = str(paths["commands"] / f"{paper}.json")
    state["status"] = "running"
    save_state(paths, state)
    print(f"[case] {paper}: starting ({'resume' if resume else 'fresh'})", flush=True)

    env = os.environ.copy()
    local_bins = [
        str(ROOT / "tools" / "opencode" / "node_modules" / ".bin"),
        str(ROOT / ".venv" / "bin"),
    ]
    env["PATH"] = os.pathsep.join(local_bins + [env.get("PATH", "")])
    env["PYTHONUNBUFFERED"] = "1"
    env["EFFICIENT_OR_DISABLE_ONESHOT_SEED_REUSE"] = "1"
    env["OPENAI_BASE_URL"] = effective_base_url()
    started = time.monotonic()
    log_mode = "a" if resume else "w"
    with log_path.open(log_mode, encoding="utf-8") as log:
        if resume:
            log.write(f"\n===== BATCH RESUME {utc_now()} =====\n")
            log.flush()
        ACTIVE_PROCESS = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            preexec_fn=_limit_case_process,
        )
        return_code = ACTIVE_PROCESS.wait()
    ACTIVE_PROCESS = None
    elapsed = time.monotonic() - started
    case["duration_seconds"] = round(float(case.get("duration_seconds") or 0) + elapsed, 2)

    if STOP_REQUESTED:
        case["status"] = "running"
        case["error"] = "interrupted; eligible for native CORAL resume on next start"
        state["status"] = "stopped"
        save_state(paths, state)
        return False

    validation = validate_case(run_id, paper, command)
    atomic_json(paths["commands"] / f"{paper}.validation.json", validation)
    case["ended_at"] = utc_now()
    case["validation"] = str(paths["commands"] / f"{paper}.validation.json")
    if return_code == 0 and validation["passed"]:
        case["status"] = "completed"
        case["error"] = None
        print(
            f"[case] {paper}: completed; attempts={validation['finalized_attempts']} "
            f"test_rows={validation['test_csv_rows']} elapsed={elapsed:.1f}s",
            flush=True,
        )
        save_state(paths, state)
        return True
    case["status"] = "failed"
    details = "; ".join(validation["failures"])
    case["error"] = f"exit={return_code}: {details or 'case process failed'}"
    print(f"[case] {paper}: FAILED: {case['error']}", flush=True)
    save_state(paths, state)
    return False


def acquire_batch_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("another batch controller already holds this run's lock") from exc
    return handle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--dry-run", action="store_true", help="Freeze/validate config without network calls or cases")
    parser.add_argument("--preflight-only", action="store_true", help="Run live endpoint/OpenCode checks without a case")
    parser.add_argument("--first-case-only", action="store_true", help="Stop after the first end-to-end case")
    parser.add_argument("--retry-failed", action="store_true", help="Retry cases already marked failed")
    parser.add_argument("--skip-endpoint-smoke", action="store_true", help="Skip the live tool-calling probe")
    parser.add_argument("--resource-poll-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_trusted_dotenv()
    paths = batch_paths(args.run_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    lock = acquire_batch_lock(paths["lock"])
    try:
        papers, _config = freeze_manifest(args.run_id, paths)
        state = read_json(paths["state"])
        if not isinstance(state, dict):
            state = initial_state(args.run_id, papers)
        if list(state.get("cases", {})) != papers:
            raise RuntimeError("state.json case order differs from the frozen manifest")
        state["pid"] = os.getpid()
        state["pgid"] = os.getpgid(0)
        paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
        save_state(paths, state)
        print(
            f"[batch] run={args.run_id} cases={len(papers)} pid={os.getpid()} "
            f"pgid={os.getpgid(0)}",
            flush=True,
        )
        if args.dry_run:
            print(f"[batch] dry-run complete; manifest={paths['manifest']}", flush=True)
            return 0

        try:
            run_preflight(paths, skip_endpoint_smoke=args.skip_endpoint_smoke)
        except Exception as exc:
            atomic_json(paths["preflight"], {
                "status": "failed",
                "finished_at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
                "secrets_persisted": False,
            })
            state["status"] = "preflight_failed"
            save_state(paths, state)
            raise
        print(f"[preflight] passed; details={paths['preflight']}", flush=True)
        if args.preflight_only:
            state["status"] = "preflight_passed"
            save_state(paths, state)
            return 0

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        first_paper = papers[0]
        first_status = state["cases"][first_paper].get("status")
        if first_status == "failed" and not args.retry_failed:
            state["status"] = "first_case_failed"
            save_state(paths, state)
            print("[batch] first end-to-end case is failed; use --retry-failed to retry it", flush=True)
            return 2
        if first_status == "completed" and args.first_case_only:
            state["status"] = "first_case_complete"
            save_state(paths, state)
            return 0
        for paper in papers:
            if STOP_REQUESTED:
                break
            case = state["cases"][paper]
            status = case.get("status")
            if status == "completed":
                continue
            if status == "failed" and not args.retry_failed:
                continue
            resume = status == "running" and valid_coral_checkpoint(args.run_id, paper)
            if status == "running" and not resume:
                archived = archive_invalid_partial(paths, args.run_id, paper)
                if archived:
                    print(f"[resume] {paper}: invalid checkpoint archived at {archived}", flush=True)
                case["status"] = "pending"
                case["error"] = "interrupted without valid checkpoint; restarted fresh"
                save_state(paths, state)
            if status == "failed" and args.retry_failed:
                resume = valid_coral_checkpoint(args.run_id, paper)
                if not resume:
                    archived = archive_invalid_partial(paths, args.run_id, paper)
                    if archived:
                        print(f"[retry] {paper}: invalid checkpoint archived at {archived}", flush=True)
            if not wait_for_resources(paths, state, paper, args.resource_poll_seconds):
                break
            passed = run_case(paths, state, args.run_id, paper, resume=resume)
            if STOP_REQUESTED:
                break
            if paper == first_paper and not passed:
                state["status"] = "first_case_failed"
                save_state(paths, state)
                print("[batch] first end-to-end case failed; remaining cases were not started", flush=True)
                return 2
            if args.first_case_only:
                state["status"] = "first_case_complete" if passed else "first_case_failed"
                save_state(paths, state)
                return 0 if passed else 2

        if STOP_REQUESTED:
            state["status"] = "stopped"
            save_state(paths, state)
            return 130
        statuses = Counter(item.get("status") for item in state["cases"].values())
        state["status"] = "complete" if statuses.get("pending", 0) == 0 and statuses.get("running", 0) == 0 else "partial"
        state["finished_at"] = utc_now()
        save_state(paths, state)
        print(f"[batch] finished: {dict(statuses)}; summary={paths['progress']}", flush=True)
        return 0 if statuses.get("failed", 0) == 0 else 1
    finally:
        try:
            paths["pid"].unlink()
        except FileNotFoundError:
            pass
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
