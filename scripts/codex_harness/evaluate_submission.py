#!/usr/bin/env python3
"""Evaluate Codex-harness submissions with FrontierOR's local checkers."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "frontier-or"
RUNS_ROOT = REPO_ROOT / "codex_harness" / "runs"


def load_directions() -> dict[str, str]:
    for path in (REPO_ROOT / "paper_meta_info.json", DATA_ROOT / "paper_meta_info.json"):
        if path.is_file():
            rows = json.loads(path.read_text(encoding="utf-8"))
            return {
                row["paper_id"]: row["direction"]
                for row in rows
                if row.get("direction") in {"min", "max"}
            }
    raise FileNotFoundError("paper_meta_info.json not found")


def reference_runtime(paper_id: str, instance: str) -> float | None:
    suffix = "tiny" if instance == "tiny" else instance.removeprefix("large_")
    solution_name = "tiny_solution.json" if instance == "tiny" else f"large_solution_{suffix}.json"
    solution_path = DATA_ROOT / paper_id / "gurobi_solution" / solution_name
    try:
        solution = json.loads(solution_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        solution = {}

    for key in (
        "wall_time",
        "runtime",
        "elapsed_time_seconds",
        "solve_time",
        "elapsed_time",
    ):
        value = solution.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            return value

    log_name = "tiny_log.jsonl" if instance == "tiny" else f"large_log_{suffix}.jsonl"
    log_path = DATA_ROOT / paper_id / "gurobi_solution_log" / log_name
    times: list[float] = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            value = float(row.get("time"))
            if math.isfinite(value) and value >= 0:
                times.append(value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return max(times) if times else None


def install_bubblewrap_backend(core) -> bool:
    """Use the shared memory-limited Bubblewrap backend."""
    import exec_backends
    core.EXEC_BACKENDS["bubblewrap"] = exec_backends.run_bubblewrap
    return exec_backends.bubblewrap_network_isolation_available()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--paper-id", nargs="+", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--instances",
        nargs="+",
        default=["large_1", "large_2", "large_3", "large_4", "large_5"],
    )
    parser.add_argument("--time-limit", type=int, default=3600)
    parser.add_argument(
        "--exec-mode",
        choices=["bubblewrap", "bare", "systemd", "docker"],
        default="bubblewrap",
    )
    parser.add_argument("--cpus", type=int, default=1)
    parser.add_argument("--memory", default="16G")
    parser.add_argument(
        "--memory-reserve",
        default="16G",
        help="Host MemAvailable kept unreserved by Bubblewrap admission control.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))
    import one_shot_eval as core

    directions = load_directions()
    core._DIRECTIONS_CACHE = directions
    bubblewrap_network_isolated = install_bubblewrap_backend(core)

    run_dir = RUNS_ROOT / args.run_id
    evaluation_dir = run_dir / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    paper_summaries: list[dict[str, Any]] = []

    for paper_id in args.paper_id:
        if paper_id not in directions:
            parser.error(f"missing optimization direction for {paper_id}")
        submission_path = run_dir / "submissions" / paper_id / args.model / "code.py"
        if not submission_path.is_file():
            parser.error(f"submission not found: {submission_path}")

        # The repository's bare backend hides the real instance tree, but an
        # untrusted program could still climb from __file__ or solution_path.
        # Put both candidate code and candidate-writable outputs in /tmp so
        # neither argument reveals the benchmark checkout.
        with tempfile.TemporaryDirectory(prefix=f"frontieror_eval_{paper_id}_") as temp_name:
            temp_root = Path(temp_name)
            candidate_dir = temp_root / "candidate"
            output_dir = temp_root / "outputs"
            candidate_dir.mkdir()
            output_dir.mkdir()
            code_path = candidate_dir / "code.py"
            shutil.copy2(submission_path, code_path)
            shutil.copy2(REPO_ROOT / "scripts" / "utils" / "solution_logger.py", candidate_dir)

            tiny, _ = core.run_and_evaluate_instance(
                paper_id,
                args.model,
                "tiny",
                str(code_path),
                min(args.time_limit, 300),
                args.exec_mode,
                {
                    "cpus": args.cpus,
                    "memory": args.memory,
                    "memory_reserve": args.memory_reserve,
                },
                None,
                output_dir=str(output_dir),
            )
            tiny_gate = tiny.get("feasible") is True and (
                tiny.get("gap") is None or tiny["gap"] <= 0.10
            )

            instance_results: list[tuple[str, dict[str, Any]]] = []
            for instance in args.instances:
                if tiny_gate:
                    result, _ = core.run_and_evaluate_instance(
                        paper_id,
                        args.model,
                        instance,
                        str(code_path),
                        args.time_limit,
                        args.exec_mode,
                        {
                            "cpus": args.cpus,
                            "memory": args.memory,
                            "memory_reserve": args.memory_reserve,
                        },
                        None,
                        output_dir=str(output_dir),
                    )
                else:
                    result = {
                        "status": "skipped",
                        "fail_reason": "tiny_gate_failed",
                        "feasible": False,
                        "gap": None,
                        "llm_obj": None,
                        "gurobi_obj": None,
                        "solve_time": None,
                        "aocc": None,
                        "error": tiny.get("error"),
                    }
                instance_results.append((instance, result))

            final_output_dir = evaluation_dir / paper_id
            final_output_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(output_dir, final_output_dir, dirs_exist_ok=True)

        for instance, result in instance_results:
            baseline_time = reference_runtime(paper_id, instance)
            feasible = result.get("feasible") is True
            gap = result.get("gap")
            quality_pass = feasible and gap is not None and gap <= 0.01
            solve_time = result.get("solve_time")
            qte_pass = None
            if baseline_time is not None and solve_time is not None:
                qte_pass = quality_pass and float(solve_time) <= baseline_time

            rows.append(
                {
                    "paper_id": paper_id,
                    "instance": instance,
                    "tiny_gate": tiny_gate,
                    "executed": (
                        result.get("status") != "skipped"
                        and result.get("fail_reason") != "runtime_error"
                    ),
                    "feasible": feasible,
                    "quality_pass_1pct": quality_pass,
                    "qte_pass": qte_pass,
                    "gap": gap,
                    "llm_obj": result.get("llm_obj"),
                    "gurobi_obj": result.get("gurobi_obj"),
                    "solve_time": solve_time,
                    "gurobi_time": baseline_time,
                    "aocc": result.get("aocc"),
                    "status": result.get("status"),
                    "fail_reason": result.get("fail_reason"),
                    "error": (result.get("error") or "")[:2000],
                }
            )

        own = [row for row in rows if row["paper_id"] == paper_id]
        known_qte = [row for row in own if row["qte_pass"] is not None]
        paper_summaries.append(
            {
                "paper_id": paper_id,
                "tiny_gate": tiny_gate,
                "execution_rate": sum(bool(row["executed"]) for row in own) / len(own),
                "feasibility": sum(bool(row["feasible"]) for row in own) / len(own),
                "solution_quality_1pct": sum(bool(row["quality_pass_1pct"]) for row in own) / len(own),
                "qte_known_cases": len(known_qte),
                "qte_rate_known_cases": (
                    sum(bool(row["qte_pass"]) for row in known_qte) / len(known_qte)
                    if known_qte
                    else None
                ),
            }
        )

    payload = {
        "run_id": args.run_id,
        "model": args.model,
        "reasoning_effort": "xhigh",
        "exec_mode": args.exec_mode,
        "time_limit": args.time_limit,
        "memory_limit": args.memory,
        "memory_reserve": args.memory_reserve,
        "memory_enforcement": (
            "RLIMIT_AS per process + cross-process admission control"
            if args.exec_mode == "bubblewrap"
            else "backend-specific"
        ),
        "bubblewrap_network_namespace": bubblewrap_network_isolated,
        "papers": paper_summaries,
        "instances": rows,
        "notes": [
            "tiny gate uses the repository's 10% gap rule",
            "reported solution quality uses the paper's official 1% rule",
            "QTE is null when the public data lacks a recoverable Gurobi runtime",
            "bubblewrap network isolation is best-effort; use Docker for strict official isolation",
            "bubblewrap memory uses per-process RLIMIT_AS, not an aggregate cgroup; candidate child processes inherit the same per-process cap",
        ],
    }
    (evaluation_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with (evaluation_dir / "instances.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        f"# FrontierOR Codex harness 评估：{args.run_id}",
        "",
        "| case | tiny gate | execution | feasibility | quality@1% | QTE（有基线时间的样本） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in paper_summaries:
        qte = item["qte_rate_known_cases"]
        qte_text = "N/A" if qte is None else f"{qte:.3f} ({item['qte_known_cases']} cases)"
        lines.append(
            f"| {item['paper_id']} | {str(item['tiny_gate']).lower()} | "
            f"{item['execution_rate']:.3f} | {item['feasibility']:.3f} | "
            f"{item['solution_quality_1pct']:.3f} | {qte_text} |"
        )
    lines.extend(
        [
            "",
            "> 注：公开数据没有统一附带每个实例的 Gurobi wall-clock 字段；脚本仅在可从参考解或收敛日志恢复时计算 QTE。",
            "",
        ]
    )
    (evaluation_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(evaluation_dir / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
