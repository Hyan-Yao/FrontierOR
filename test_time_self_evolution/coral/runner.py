"""CORAL-specific orchestration for frontier-or self evolution.

CORAL optimizes by running coding agents over a seed repository.  This adapter
materializes a CORAL task whose hidden grader calls the benchmark evaluator,
starts a bounded CORAL run, extracts the best committed ``code.py``, and writes
the same CSV outputs as the other eval modes.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

import one_shot_eval as eval_core
from test_time_self_evolution import eval_modes


ROOT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
EXTERNAL_CORAL_DIR = os.path.join(ROOT_DIR, "external", "coral")
LOCAL_OPENCODE_BIN = os.path.join(
    ROOT_DIR, "tools", "opencode", "node_modules", ".bin"
)
LOCAL_OPENCODE_NODE_MODULES = Path(ROOT_DIR) / "tools" / "opencode" / "node_modules"
OPENCODE_PROVIDER_NAME = "@ai-sdk/openai-compatible"
OPENCODE_PROVIDER_VERSION = "3.0.16"
OPENCODE_PROVIDER_CACHE_DEPS = (
    Path("@ai-sdk/openai-compatible"),
    Path("@ai-sdk/provider"),
    Path("@ai-sdk/provider-utils"),
    Path("@standard-schema/spec"),
    Path("@workflow/serde"),
    Path("eventsource-parser"),
    Path("json-schema"),
    Path("zod"),
)


@dataclass
class CoralTask:
    task_name: str
    task_dir: str
    config_path: str
    run_dir: str
    coral_dir: str
    repo_dir: str
    log_path: str


def seed_opencode_provider_cache(env: Dict[str, str]) -> Path:
    """Seed the isolated OpenCode provider cache from pinned local packages.

    Bun can leave the first dynamic provider installation incomplete when the
    importing OpenCode process exits. The repository-local OpenCode install is
    already locked and complete, so link those exact packages into the run's
    private XDG cache before the live provider prewarm.
    """
    cache_home = env.get("XDG_CACHE_HOME")
    if not cache_home:
        raise RuntimeError("OpenCode cache seeding requires XDG_CACHE_HOME")

    source_metadata_path = (
        LOCAL_OPENCODE_NODE_MODULES / OPENCODE_PROVIDER_NAME / "package.json"
    )
    try:
        source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("repository-local OpenCode provider package is unavailable") from exc
    if (
        source_metadata.get("name") != OPENCODE_PROVIDER_NAME
        or source_metadata.get("version") != OPENCODE_PROVIDER_VERSION
    ):
        raise RuntimeError(
            "repository-local OpenCode provider version does not match the pinned config"
        )
    for relative in OPENCODE_PROVIDER_CACHE_DEPS:
        if not (LOCAL_OPENCODE_NODE_MODULES / relative / "package.json").is_file():
            raise RuntimeError(f"repository-local OpenCode dependency is missing: {relative}")

    destination = (
        Path(cache_home).resolve()
        / "opencode"
        / "packages"
        / f"{OPENCODE_PROVIDER_NAME}@{OPENCODE_PROVIDER_VERSION}"
    )
    expected_package = {
        "dependencies": {OPENCODE_PROVIDER_NAME: OPENCODE_PROVIDER_VERSION}
    }
    try:
        current_package = json.loads(
            (destination / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        current_package = None
    cache_complete = current_package == expected_package and all(
        (destination / "node_modules" / relative / "package.json").is_file()
        for relative in OPENCODE_PROVIDER_CACHE_DEPS
    )
    if cache_complete:
        return destination

    temporary = destination.with_name(f"{destination.name}.tmp-{os.getpid()}")
    if temporary.is_symlink() or temporary.is_file():
        temporary.unlink()
    elif temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True, mode=0o700)
    (temporary / "package.json").write_text(
        json.dumps(expected_package, indent=2) + "\n", encoding="utf-8"
    )
    for relative in OPENCODE_PROVIDER_CACHE_DEPS:
        link = temporary / "node_modules" / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(
            (LOCAL_OPENCODE_NODE_MODULES / relative).resolve(),
            target_is_directory=True,
        )

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    os.replace(temporary, destination)
    return destination


def prepare_coral_env(
    base_env: Optional[Dict[str, str]],
    config: Dict,
    *,
    opencode_state_root: Optional[str] = None,
) -> Dict[str, str]:
    """Prepare environment for CORAL without writing secrets into task files."""
    env = dict(base_env or os.environ)
    key = (
        config.get("LLM_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("OPENROUTER_API_KEY")
        or config.get("OPENROUTER_API_KEY")
    )
    if key:
        env["OPENAI_API_KEY"] = key
    base_url = (
        config.get("LLM_API_BASE")
        or env.get("OPENAI_BASE_URL")
        or env.get("OPENAI_API_BASE")
    )
    if base_url:
        env["OPENAI_BASE_URL"] = str(base_url).rstrip("/")
    if os.environ.get("GRB_LICENSE_FILE"):
        env["GRB_LICENSE_FILE"] = os.environ["GRB_LICENSE_FILE"]

    # CORAL's OpenCode runtime invokes `opencode` by name and its agents invoke
    # `coral eval` by name.  Keep both installations repository-local.
    path_parts = [LOCAL_OPENCODE_BIN, os.path.join(ROOT_DIR, ".venv", "bin")]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"

    # Never share OpenCode's mutable package/session cache with unrelated
    # host jobs. A partially installed provider in the global cache made the
    # CLI fail at provider initialization and CORAL restart it indefinitely.
    if opencode_state_root:
        state_root = Path(opencode_state_root).resolve()
        xdg_dirs = {
            "XDG_CACHE_HOME": state_root / "cache",
            "XDG_CONFIG_HOME": state_root / "config",
            "XDG_DATA_HOME": state_root / "data",
            "XDG_STATE_HOME": state_root / "state",
        }
        for key_name, directory in xdg_dirs.items():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            env[key_name] = str(directory)
        seed_opencode_provider_cache(env)

    pythonpath = [ROOT_DIR]
    if os.path.isdir(EXTERNAL_CORAL_DIR):
        pythonpath.insert(0, EXTERNAL_CORAL_DIR)
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env


def _redact_opencode_diagnostics(text: str, env: Dict[str, str]) -> str:
    redacted = text
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        value = env.get(key)
        if value:
            redacted = redacted.replace(value, f"<{key}>")
    return redacted


def prewarm_opencode_provider(
    task: CoralTask,
    env: Dict[str, str],
    model: str,
    *,
    timeout: int = 120,
) -> str:
    """Install and exercise the run-local OpenCode provider exactly once.

    ``opencode models`` only parses configuration and does not import the
    provider. A tiny real request catches incomplete npm caches before CORAL
    enters its agent restart loop. The success marker is scoped to the frozen
    run-local XDG directory and contains no credentials.
    """
    from test_time_self_evolution.coral.coral_cli_wrapper import (
        OPENCODE_PROVIDER_PACKAGE,
        build_opencode_settings,
        seed_opencode_workspace_runtime,
    )

    cache_home = env.get("XDG_CACHE_HOME")
    if not cache_home:
        raise RuntimeError("OpenCode prewarm requires a run-local XDG cache")
    state_root = Path(cache_home).resolve().parent
    marker = state_root / "provider-ready.json"
    expected = {
        "status": "passed",
        "model": model,
        "provider_package": OPENCODE_PROVIDER_PACKAGE,
        "provider_cache": "repository-local-pinned",
    }
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, dict) and all(existing.get(k) == v for k, v in expected.items()):
        return str(marker)

    workspace = state_root / "provider-prewarm"
    config_dir = workspace / ".opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = build_opencode_settings(Path(task.coral_dir), research=False)
    (config_dir / "opencode.json").write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
    )
    seed_opencode_workspace_runtime(config_dir)
    command = [
        os.path.join(LOCAL_OPENCODE_BIN, "opencode"),
        "run",
        "--model", model,
        "--format", "json",
        "--dir", str(workspace),
        "Reply with exactly OK and do not use tools.",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"OpenCode provider prewarm timed out after {timeout}s") from exc

    diagnostic = _redact_opencode_diagnostics(
        (completed.stdout or "") + (completed.stderr or ""), env
    )
    (state_root / "provider-prewarm.log").write_text(diagnostic, encoding="utf-8")
    if completed.returncode != 0:
        tail = diagnostic[-2000:].strip()
        raise RuntimeError(
            f"OpenCode provider prewarm failed with exit {completed.returncode}: {tail}"
        )
    marker.write_text(
        json.dumps({**expected, "finished_at": time.time()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(marker)


def _write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _try_reuse_oneshot_seed(paper_id: str, model_name: str, seed_dir: str) -> Optional[str]:
    """Try to copy ``eval/eval_papers/<paper>/<model_short>/code_attempt0.py``
    into ``<seed_dir>/code.py`` and return its path. Returns None if the
    one-shot artifact doesn't exist (caller should fall back to live LLM
    generation via ``eval_core.generate_candidate_code``).

    Saves 1 LLM call per paper when a one-shot run with the same model has
    already populated eval_papers/. Also makes CORAL/OpenEvolve start from
    the same seed (apples-to-apples comparison).
    """
    import shutil
    short = eval_core.get_model_short_name(model_name)
    src = eval_modes.latest_oneshot_attempt(paper_id, short)
    if src is None:
        return None
    os.makedirs(seed_dir, exist_ok=True)
    dst = os.path.join(seed_dir, "code.py")
    shutil.copyfile(src, dst)
    provenance = os.path.join(seed_dir, "_seed_source.txt")
    with open(provenance, "w", encoding="utf-8") as f:
        f.write(f"reused from one-shot v0: {src}\n")
    print(f"[reuse-oneshot:coral] {paper_id}/{short}: copied one-shot v0 → {dst}")
    return dst


def _seed_readme(paper_id: str, prompt: str) -> str:
    return f"""# Efficient-OR Task: {paper_id}

{prompt}
"""


def _grader_code(root_dir: str) -> str:
    return f'''"""Hidden CORAL grader for frontier-or."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from coral.grader import TaskGrader


ROOT_DIR = {root_dir!r}
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class Grader(TaskGrader):
    def evaluate(self):
        args = self.args
        code_path = Path(self.codebase_path) / "code.py"
        if not code_path.exists():
            return self.fail("code.py not found")

        # Per-instance grader scratch lands in <base_dir>/coral_eval/.
        # Mirrors openevolve's `openevolve_eval/` — evaluator.evaluate_stage{{1,2}}
        # create stage1/ stage2/ subdirs.
        output_dir = Path(args["base_dir"]) / "coral_eval"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Sidecar metadata dir — keyed by commit_hash so runner.py can
        # reconstruct per-instance dev results from the best attempt later.
        # We use a sidecar instead of ScoreBundle.metadata because CORAL's
        # Attempt.to_dict() drops Score.metadata when serializing to JSON.
        sidecar_dir = output_dir / "attempt_metadata"
        sidecar_dir.mkdir(parents=True, exist_ok=True)

        try:
            commit_hash = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(self.codebase_path),
                text=True, timeout=5,
            ).strip()
        except Exception:
            commit_hash = None

        env = {{
            "EFFICIENT_OR_ROOT": ROOT_DIR,
            "EFFICIENT_OR_PAPER_ID": args["paper_id"],
            "EFFICIENT_OR_MODEL_NAME": args.get("model_name", "coral"),
            "EFFICIENT_OR_STAGE1_INSTANCES": ",".join(args["stage1_instances"]),
            "EFFICIENT_OR_STAGE1_TIME_LIMIT": str(args["stage1_time_limit"]),
            "EFFICIENT_OR_STAGE1_GAP_THRESHOLD": str(args["stage1_gap_threshold"]),
            "EFFICIENT_OR_STAGE2_INSTANCES": ",".join(args["stage2_instances"]),
            "EFFICIENT_OR_STAGE2_TIME_LIMIT": str(args["stage2_time_limit"]),
            "EFFICIENT_OR_STAGE2_TIME_POLICY": args.get("stage2_time_policy", "uniform"),
            "EFFICIENT_OR_STAGE2_TIME_BUFFER": str(args.get("stage2_time_buffer", 0)),
            "EFFICIENT_OR_STAGE2_SCORER": args.get("stage2_scorer", "staged_qte"),
            "EFFICIENT_OR_STAGE2_STAGE_BOUNDARY": str(args.get("stage2_stage_boundary", 0.01)),
            "EFFICIENT_OR_EXEC_MODE": args.get("exec_mode", "bare"),
            "EFFICIENT_OR_T_MAX": "" if args.get("t_max") is None else str(args["t_max"]),
            "EFFICIENT_OR_OUTPUT_DIR": str(output_dir),
        }}
        for key, value in (args.get("exec_cfg") or {{}}).items():
            env[f"EFFICIENT_OR_EXEC_{{key.upper()}}"] = str(value)

        old_env = {{key: os.environ.get(key) for key in env}}
        os.environ.update(env)
        try:
            from test_time_self_evolution.openevolve import evaluator

            # OpenEvolve's evaluator returns EvaluationResult (dataclass with
            # .metrics + .artifacts) when openevolve is importable, dict
            # otherwise. Normalize via getattr so .get() works on both shapes.
            def _metrics(r):
                return getattr(r, "metrics", r) or {{}}

            def _write_sidecar(payload):
                if commit_hash:
                    (sidecar_dir / f"{{commit_hash}}.json").write_text(
                        json.dumps(payload, default=str)
                    )

            stage1 = _metrics(evaluator.evaluate_stage1(str(code_path)))
            if float(stage1.get("combined_score", 0.0)) < 1.0:
                _write_sidecar({{
                    "stage1_combined_score": stage1.get("combined_score", 0.0),
                    "stage1": stage1,
                    "stage2_skipped": True,
                }})
                return self.bundle(
                    0.0,
                    "Stage1 gate failed",
                    feedback=f"Stage1 failed: combined_score="
                            f"{{stage1.get('combined_score', 0):.3f}}, "
                            f"worst_gap={{stage1.get('stage1_worst_gap', 'N/A')}}",
                )

            stage2 = _metrics(evaluator.evaluate_stage2(str(code_path)))
            score = float(stage2.get("combined_score", 0.0))
            metadata_full = dict(stage2)
            metadata_full["stage1_combined_score"] = stage1.get("combined_score", 0.0)
            _write_sidecar(metadata_full)
            return self.bundle(
                score,
                f"Stage2 score {{score:.6f}}",
                feedback=f"Stage2 score {{score:.6f}}",
            )
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
'''


def _write_gateway_config(path: str, model_alias: str, model_id: str):
    config = {
        "model_list": [
            {
                "model_name": model_alias,
                "litellm_params": {
                    "model": f"openrouter/{model_id}",
                    "api_key": "os.environ/OPENROUTER_API_KEY",
                    "api_base": "https://openrouter.ai/api/v1",
                },
            }
        ],
        "litellm_settings": {"drop_params": True},
    }
    _write_text(path, yaml.safe_dump(config, sort_keys=False))


def _coral_model_for_runtime(primary_model: str, agent_runtime: str, agent_model: Optional[str]) -> str:
    if agent_model:
        return agent_model
    if agent_runtime == "codex":
        return eval_core.get_model_short_name(primary_model)
    if agent_runtime == "opencode":
        return f"frontier/{eval_core.get_model_short_name(primary_model)}"
    return primary_model


def write_coral_task(
    *,
    base_dir: str,
    paper_id: str,
    prompt: str,
    model_name: str,
    primary_model: str,
    stage1_instances: List[str],
    stage2_instances: List[str],
    stage1_time_limit: int,
    stage2_time_limit: int,
    stage1_gap_threshold: float,
    exec_mode: str,
    exec_cfg: Dict,
    t_max,
    stage2_scorer: str,
    agent_runtime: str,
    agent_count: int,
    agent_model: Optional[str],
    max_turns: int,
    gateway_enabled: bool,
    openrouter_api_key: Optional[str] = None,
    stage2_stage_boundary: float = 0.01,
    stage2_time_policy: str = "uniform",
    stage2_time_buffer: int = 0,
    heartbeat_reflect_every: int = 0,
    heartbeat_pivot_every: int = 5,
    heartbeat_consolidate_every: int = 0,
) -> CoralTask:
    task_dir = os.path.join(base_dir, "coral_task")
    seed_dir = os.path.join(task_dir, "seed")
    eval_dir = os.path.join(task_dir, "eval")
    run_dir = os.path.join(base_dir, "coral_run")
    results_dir = os.path.join(base_dir, "coral_results")
    config_path = os.path.join(task_dir, "task.yaml")
    log_path = os.path.join(base_dir, "coral_start.log")

    os.makedirs(seed_dir, exist_ok=True)
    os.makedirs(eval_dir, exist_ok=True)
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)

    # seed/code.py is populated by the caller (run_self_evolve) — either by
    # reusing one-shot's code_attempt0.py or by live LLM generation. We don't
    # write a hardcoded stub here because the bench evaluates code.py as an
    # argparse script (subprocess), and any contract-violating stub would just
    # cost the agent 1 wasted attempt.
    _write_text(os.path.join(seed_dir, "README.md"), _seed_readme(paper_id, prompt))
    _write_text(os.path.join(eval_dir, "grader.py"), _grader_code(ROOT_DIR))

    coral_model = _coral_model_for_runtime(primary_model, agent_runtime, agent_model)
    task_name = f"efficient_or_{paper_id}_{model_name}"
    # heartbeat: each action only included when its `*_every` > 0.
    # - reflect (interval): pause every N evals, write a note. High cost.
    # - pivot (plateau): only fires after N non-improving evals. Low cost.
    # - consolidate (interval, global): merge cross-agent notes; needs >1 agent.
    heartbeat: List[Dict] = []
    if heartbeat_reflect_every > 0:
        heartbeat.append({
            "name": "reflect",
            "every": int(heartbeat_reflect_every),
            "trigger": "interval",
        })
    if heartbeat_pivot_every > 0:
        heartbeat.append({
            "name": "pivot",
            "every": int(heartbeat_pivot_every),
            "trigger": "plateau",
        })
    if heartbeat_consolidate_every > 0:
        heartbeat.append({
            "name": "consolidate",
            "every": int(heartbeat_consolidate_every),
            "trigger": "interval",
            "is_global": True,
        })

    agents = {
        "runtime": agent_runtime,
        "count": int(agent_count),
        "model": coral_model,
        "max_turns": int(max_turns),
        # research: keep off — agent web search would find the source paper
        # and leak benchmark answers. Reproducibility/cost also worse.
        "research": False,
        # Modern Codex exposes web search independently of CORAL's legacy
        # `research` flag. Pass an explicit runtime override so closed-book
        # benchmark runs cannot search for the source paper or solutions.
        "runtime_options": {"web_search": False},
        "heartbeat": heartbeat,
    }
    if gateway_enabled:
        gateway_config_path = os.path.join(task_dir, "litellm_config.yaml")
        _write_gateway_config(gateway_config_path, coral_model, primary_model)
        agents["gateway"] = {
            "enabled": True,
            "port": 4000,
            "config": gateway_config_path,
            "api_key": "",
        }

    task_config = {
        "task": {
            "name": task_name,
            "description": (
                "Optimize code.py to maximize the score on the optimization "
                "problem described in README.md."
            ),
            # Tips are auto-rendered as a ## Tips section in CORAL.md (upstream
            # template feature, see external/coral/coral/template/coral_md.py).
            # Keep this minimal — only project-specific guardrails that the
            # upstream CORAL.md template, the rendered seed/README.md, and the
            # reused seed/code.py do not already convey.
            "tips": (
                "**Forbidden (answer leakage)** — never read these; the grader "
                "uses them internally and reading them constitutes cheating that "
                "voids the run:\n"
                "- `frontier-or/<paper>/gurobi_solution/` (ground-truth solutions)\n"
                "- any file matching `large_solution_*.json`\n"
                "- `.coral/private/` (hidden grader code)"
            ),
        },
        "grader": {
            "timeout": int(stage1_time_limit + stage2_time_limit + 120),
            "direction": "maximize",
            "args": {
                "paper_id": paper_id,
                "model_name": model_name,
                "base_dir": base_dir,
                "stage1_instances": stage1_instances,
                "stage2_instances": stage2_instances,
                "stage1_time_limit": stage1_time_limit,
                "stage2_time_limit": stage2_time_limit,
                "stage1_gap_threshold": stage1_gap_threshold,
                "stage2_scorer": stage2_scorer,
                "stage2_stage_boundary": stage2_stage_boundary,
                "stage2_time_policy": stage2_time_policy,
                "stage2_time_buffer": stage2_time_buffer,
                "exec_mode": exec_mode,
                "exec_cfg": exec_cfg or {},
                "t_max": t_max,
            },
        },
        "agents": agents,
        "workspace": {
            "results_dir": results_dir,
            "repo_path": seed_dir,
            "run_dir": run_dir,
        },
        "run": {
            "session": "local",
            "verbose": False,
            "ui": False,
        },
    }
    _write_text(config_path, yaml.safe_dump(task_config, sort_keys=False))

    return CoralTask(
        task_name=task_name,
        task_dir=task_dir,
        config_path=config_path,
        run_dir=run_dir,
        coral_dir=os.path.join(run_dir, ".coral"),
        repo_dir=os.path.join(run_dir, "repo"),
        log_path=log_path,
    )


def _coral_cli() -> List[str]:
    return [sys.executable, "-m", "test_time_self_evolution.coral.coral_cli_wrapper"]


def read_attempts(coral_dir: str) -> List[Dict]:
    attempts_dir = os.path.join(coral_dir, "public", "attempts")
    if not os.path.isdir(attempts_dir):
        return []
    attempts = []
    for name in sorted(os.listdir(attempts_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(attempts_dir, name), encoding="utf-8") as f:
                attempts.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return attempts


def read_best_attempt(coral_dir: str) -> Optional[Dict]:
    scored = [a for a in read_attempts(coral_dir) if a.get("score") is not None]
    if not scored:
        return None
    return max(scored, key=lambda attempt: float(attempt.get("score") or 0.0))


def _sandboxed_agent_needs_controller_submit(task: CoralTask) -> Optional[str]:
    """Return an agent worktree after a sandbox-blocked `coral eval`.

    Codex workspace-write intentionally keeps linked-worktree Git metadata
    read-only. The agent may edit/test code.py but cannot create the commit
    CORAL's eval command requires. Only trigger the trusted-controller bridge
    after the agent explicitly attempted `coral eval` and the log records the
    precise read-only index.lock failure.
    """
    agent_dirs = sorted(Path(task.run_dir, "agents").glob("agent-*"))
    if not agent_dirs:
        return None
    logs_dir = Path(task.coral_dir, "public", "logs")
    logs = sorted(logs_dir.glob("agent-*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    try:
        log_text = logs[-1].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "coral eval" not in log_text or "index.lock': Read-only file system" not in log_text:
        return None
    worktree = agent_dirs[0]
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--", "code.py"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if status.returncode != 0 or not status.stdout.strip():
        return None
    return str(worktree)


def _stop_coral(task: CoralTask, env: Dict[str, str]):
    if not os.path.isdir(task.coral_dir):
        return
    agent_dirs = sorted(Path(task.run_dir, "agents").glob("agent-*"))
    stop_cwd = str(agent_dirs[0]) if agent_dirs else ROOT_DIR
    subprocess.run(
        _coral_cli() + ["stop"],
        cwd=stop_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_coral_until_done(task: CoralTask, env: Dict[str, str], attempts: int, max_seconds: int,
                         resume: bool = False):
    os.makedirs(os.path.dirname(task.log_path), exist_ok=True)
    if resume:
        # CORAL's native resume CLI continues a prior run by --task / --run
        # path. Requires the previous coral_dir / attempts to still exist.
        if not os.path.isdir(task.coral_dir):
            raise RuntimeError(
                f"Cannot resume CORAL at {task.coral_dir}: dir does not exist. "
                f"The prior run may not have started successfully."
            )
        agent_dirs = sorted(Path(task.run_dir, "agents").glob("agent-*"))
        if not agent_dirs:
            raise RuntimeError(f"Cannot resume CORAL: no agent worktree under {task.run_dir}")
        process_cwd = str(agent_dirs[0])
        cmd = _coral_cli() + [
            "resume",
            "--instruction",
            (
                "Continue from the latest grader feedback. Do not re-evaluate "
                "unchanged code: make a substantive improvement to code.py, "
                "verify it, then submit it with coral eval."
            ),
            "run.session=local",
        ]
        log_mode = "a"  # append to existing log
        print(f"[resume:coral] resuming task={task.task_name} run={os.path.basename(task.run_dir)}")
    else:
        cmd = _coral_cli() + ["start", "-c", task.config_path, "run.session=local"]
        log_mode = "w"
        process_cwd = ROOT_DIR
    with open(task.log_path, log_mode, encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=process_cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    deadline = time.monotonic() + max_seconds
    controller_submit_proc = None
    try:
        while time.monotonic() < deadline:
            finalized = [
                attempt for attempt in read_attempts(task.coral_dir)
                if attempt.get("status") != "pending"
            ]
            if len(finalized) >= attempts:
                return
            if controller_submit_proc is None:
                submit_cwd = _sandboxed_agent_needs_controller_submit(task)
                if submit_cwd:
                    print(
                        "[coral-controller] agent eval hit read-only Git metadata; "
                        f"submitting tested candidate from {submit_cwd}"
                    )
                    controller_submit_proc = subprocess.Popen(
                        _coral_cli() + [
                            "eval", "-m",
                            "controller submit sandboxed CORAL agent candidate",
                        ],
                        cwd=submit_cwd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            if proc.poll() is not None:
                return
            time.sleep(5)
    finally:
        if proc.poll() is None:
            try:
                _stop_coral(task, env)
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait(timeout=10)
        if controller_submit_proc is not None and controller_submit_proc.poll() is None:
            controller_submit_proc.terminate()
            try:
                controller_submit_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                controller_submit_proc.kill()


def extract_attempt_code(task: CoralTask, attempt: Dict, destination_dir: str) -> str:
    commit_hash = attempt["commit_hash"]
    os.makedirs(destination_dir, exist_ok=True)
    dest = os.path.join(destination_dir, f"coral_{commit_hash[:12]}_code.py")
    result = subprocess.run(
        ["git", "-C", task.repo_dir, "show", f"{commit_hash}:code.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    _write_text(dest, result.stdout)
    return dest


def _results_from_metadata(metadata: Dict, instances: List[str]) -> Dict[str, Dict]:
    """Reconstruct per-instance result dicts from grader sidecar metadata.

    The keys returned must match what ``eval_modes._build_self_evolve_row``
    reads. In addition to the basic feasibility/gap fields, we forward the
    staged_qte breakdown (``score``/``stage_id``/``quality_part``/...) so the
    dev/test CSVs are populated with the same level of detail as openevolve.
    """
    results: Dict[str, Dict] = {}
    for inst in instances:
        prefix = f"inst_{inst}"
        feasible_raw = metadata.get(f"{prefix}_feasible")
        feasible = True if feasible_raw == 1.0 else (False if feasible_raw == 0.0 else None)
        results[inst] = {
            "status": "pass" if feasible is True else ("fail" if feasible is False else "missing"),
            "fail_reason": None if feasible is True else "coral_metadata",
            "feasible": feasible,
            "gap": metadata.get(f"{prefix}_gap"),
            "llm_obj": metadata.get(f"{prefix}_obj"),
            "gurobi_obj": metadata.get(f"{prefix}_gurobi_obj"),
            "solve_time": metadata.get(f"{prefix}_time"),
            "aocc": metadata.get(f"{prefix}_aocc"),
            # staged_qte breakdown — read by _build_self_evolve_row to fill
            # score_staged / stage_id / quality_part / speed_part / signed_gap
            # / beat_amount columns. Without these the dev CSV has 6 blanks.
            "score": metadata.get(f"{prefix}_score"),
            "stage_id": metadata.get(f"{prefix}_stage_id"),
            "quality_part": metadata.get(f"{prefix}_quality_part"),
            "speed_part": metadata.get(f"{prefix}_speed_part"),
            "signed_gap": metadata.get(f"{prefix}_signed_gap"),
            "beat_amount": metadata.get(f"{prefix}_beat_amount"),
            "error": None,
            "retries": 0,
        }
    return results


def _failure_results(instances: List[str], error: str) -> Dict[str, Dict]:
    return {
        inst: {
            "status": "fail",
            "fail_reason": "coral_no_attempt",
            "feasible": None,
            "gap": None,
            "solve_time": None,
            "llm_obj": None,
            "gurobi_obj": None,
            "aocc": None,
            "error": error,
            "retries": 0,
        }
        for inst in instances
    }


def run_self_evolve(
    *,
    run_id: str,
    paper_id: str,
    primary_model: str,
    prompt: str,
    config: Dict,
    stage1_instances: List[str],
    stage2_instances: List[str],
    test_instances: List[str],
    stage1_time_limit: int,
    stage2_time_limit: int,
    test_time_limit: int,
    stage1_gap_threshold: float,
    exec_mode: str,
    exec_cfg: Dict,
    t_max,
    stage2_scorer: str = "staged_qte",
    stage2_stage_boundary: float = 0.01,
    attempts: int = 1,
    max_seconds: int = 900,
    agent_runtime: str = "codex",
    agent_count: int = 1,
    agent_model: Optional[str] = None,
    max_turns: int = 20,
    gateway_enabled: bool = False,
    stage2_time_policy: str = "uniform",
    stage2_time_buffer: int = 0,
    test_time_policy: str = "uniform",
    test_time_buffer: int = 0,
    test_instance_workers: int = 4,
    heartbeat_reflect_every: int = 0,
    heartbeat_pivot_every: int = 5,
    heartbeat_consolidate_every: int = 0,
    secondary_model: Optional[str] = None,
    resume: bool = False,
):
    del secondary_model
    primary_name = eval_core.get_model_short_name(primary_model)
    # CSV rows are disambiguated across frameworks by the `framework` column,
    # so the short name needs no framework prefix.
    model_name = primary_name
    base_dir = eval_modes.mode_run_dir(run_id, "coral", paper_id, model_name)
    selection_instance = stage1_instances[0] if stage1_instances else (
        (test_instances or stage2_instances or ["tiny"])[0]
    )
    final_instances = list(test_instances)
    reporting_instances = final_instances or list(stage2_instances)

    task = write_coral_task(
        base_dir=base_dir,
        paper_id=paper_id,
        prompt=prompt,
        model_name=model_name,
        primary_model=primary_model,
        stage1_instances=stage1_instances,
        stage2_instances=stage2_instances,
        stage1_time_limit=stage1_time_limit,
        stage2_time_limit=stage2_time_limit,
        stage1_gap_threshold=stage1_gap_threshold,
        exec_mode=exec_mode,
        exec_cfg=exec_cfg,
        t_max=t_max,
        stage2_scorer=stage2_scorer,
        stage2_stage_boundary=stage2_stage_boundary,
        agent_runtime=agent_runtime,
        agent_count=agent_count,
        agent_model=agent_model,
        max_turns=max_turns,
        gateway_enabled=gateway_enabled,
        openrouter_api_key=config.get("OPENROUTER_API_KEY"),
        stage2_time_policy=stage2_time_policy,
        stage2_time_buffer=stage2_time_buffer,
        heartbeat_reflect_every=heartbeat_reflect_every,
        heartbeat_pivot_every=heartbeat_pivot_every,
        heartbeat_consolidate_every=heartbeat_consolidate_every,
    )

    # Seed code.py priority: resume (keep existing) > one-shot reuse > live LLM generation.
    seed_dir = os.path.join(task.task_dir, "seed")
    seed_code_path = os.path.join(seed_dir, "code.py")
    seed_token_usage: Dict = {}
    if resume and os.path.exists(seed_code_path) and os.path.getsize(seed_code_path) > 0:
        print(f"[resume:coral] keeping existing seed at {seed_code_path}")
    elif (os.environ.get("EFFICIENT_OR_REUSE_SEED_IF_EXISTS") == "1"
          and os.path.exists(seed_code_path) and os.path.getsize(seed_code_path) > 0):
        print(f"[reuse-seed] using existing seed at {seed_code_path} (skip LLM generation)")
    else:
        reused = None
        if os.environ.get("EFFICIENT_OR_DISABLE_ONESHOT_SEED_REUSE") != "1":
            reused = _try_reuse_oneshot_seed(paper_id, primary_model, seed_dir)
        if reused is None:
            generated = eval_core.generate_candidate_code(
                prompt, config, primary_model, seed_dir,
                candidate_id="seed", temperature=0.4,
            )
            if generated["status"] != "ok":
                fallback_instances = final_instances or stage1_instances or [selection_instance]
                results = {
                    inst: {
                        "status": "fail",
                        "fail_reason": "generation_error",
                        "feasible": None,
                        "gap": None,
                        "solve_time": None,
                        "llm_obj": None,
                        "gurobi_obj": None,
                        "aocc": None,
                        "error": generated.get("error", "seed generation failed"),
                        "retries": 0,
                    }
                    for inst in fallback_instances
                }
                eval_modes.write_api_cost_row(
                    run_id, "self_evolve", paper_id, primary_model, model_name,
                    generated.get("usage", {}),
                    note=f"CORAL seed generation failed: {generated.get('error', '?')}",
                )
                return {"candidate_id": "coral_seed_fail", "results": results, "code_path": ""}
            seed_token_usage = generated.get("usage", {}) or {}

    # One XDG root per frozen batch run. Papers are serial in the all-180
    # controller, so they safely reuse the prewarmed provider while sessions
    # remain isolated from the user's global OpenCode state.
    run_root = Path(base_dir).parents[1]
    opencode_state_root = run_root / ".opencode_runtime"
    env = prepare_coral_env(
        os.environ,
        config,
        opencode_state_root=str(opencode_state_root),
    )
    if agent_runtime == "opencode":
        coral_model = _coral_model_for_runtime(primary_model, agent_runtime, agent_model)
        marker = prewarm_opencode_provider(task, env, coral_model)
        print(f"[opencode:coral] run-local provider prewarm passed: {marker}")
    run_coral_until_done(task, env, attempts=attempts, max_seconds=max_seconds, resume=resume)
    best = read_best_attempt(task.coral_dir)
    if not best:
        results = _failure_results(reporting_instances or stage1_instances, "No scored CORAL attempts")
        eval_modes.write_api_cost_row(
            run_id, "self_evolve", paper_id, primary_model, model_name,
            seed_token_usage,
            note=f"CORAL produced no scored attempts; seed cost only; see {task.log_path}",
        )
        return {"candidate_id": "coral_no_attempt", "results": results, "code_path": ""}

    candidate_id = f"coral_best:{best['commit_hash']}"
    extracted = extract_attempt_code(task, best, os.path.join(base_dir, "selected"))

    # Read per-instance metadata sidecar that the grader wrote during eval.
    # CORAL's Attempt JSON drops Score.metadata, so we keep our own keyed by
    # commit_hash under <base_dir>/coral_eval/attempt_metadata/<hash>.json.
    sidecar_path = os.path.join(
        base_dir, "coral_eval", "attempt_metadata", f"{best['commit_hash']}.json",
    )
    best_metadata: Dict = {}
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path, encoding="utf-8") as f:
                best_metadata = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[warn] failed to read sidecar {sidecar_path}: {e}")

    if final_instances:
        final_results = eval_modes.evaluate_best_on_test_set(
            paper_id, model_name, extracted, final_instances,
            test_time_limit, test_time_policy, test_time_buffer,
            os.path.join(base_dir, "final_eval"),
            exec_mode, exec_cfg, t_max,
            max_workers=test_instance_workers,
        )
    else:
        final_results = _results_from_metadata(best_metadata, reporting_instances)
    selected_code = eval_modes.copy_selected_code(extracted, base_dir)

    # CORAL attempts are independent (no parent → generation always 0).
    # iteration_found = 1-indexed position of best in sorted attempts list.
    all_attempts = read_attempts(task.coral_dir)
    iteration_found = None
    for idx, a in enumerate(all_attempts, 1):
        if a.get("commit_hash") == best.get("commit_hash"):
            iteration_found = idx
            break
    dev_results_for_csv = _results_from_metadata(
        best_metadata, list(stage2_instances),
    )
    test_results_for_csv = final_results if final_instances else {}
    eval_modes.write_self_evolve_results(
        paper_id=paper_id,
        model_name=model_name,
        framework="coral",
        dev_instances=list(stage2_instances),
        dev_results=dev_results_for_csv,
        dev_seed_results={},   # CORAL has no seed concept (each attempt is independent)
        test_instances=list(test_instances),
        test_results=test_results_for_csv,
        iteration_found=iteration_found,
        generation=0,
        run_id=run_id,
    )

    eval_modes.write_api_cost_row(
        run_id, "self_evolve", paper_id, primary_model, model_name,
        seed_token_usage,
        note=("seed cost only; CORAL agent usage is tracked in CORAL logs "
              f"under {task.coral_dir}"),
    )
    return {
        "candidate_id": candidate_id,
        "results": final_results,
        "code_path": selected_code,
    }
