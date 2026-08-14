#!/usr/bin/env python3
"""Build a self-contained HTML analysis for a stopped Qwen3 CORAL batch."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import shutil
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ID = "qwen3-coder-plus-coral-all180-a2-24g-20260806"
TEST_INSTANCES = ("large_2", "large_3", "large_4", "large_5")
INSTANCE_LINE = re.compile(
    r"^\s+Instance (?P<instance>large_[2-5]): "
    r"(?P<status>PASS|FAIL)(?: (?P<reason>[a-z_]+))? \((?P<details>.*)\)$"
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def parse_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def human_bytes(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def fmt_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def pct(numerator: float, denominator: float) -> str:
    if not denominator:
        return "—"
    return f"{100 * numerator / denominator:.1f}%"


def read_filtered_csv(path: Path, run_id: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        return [], []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader if row.get("run_id") == run_id]
        return list(reader.fieldnames or []), rows


def write_csv_snapshot(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fieldnames:
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_case_log(path: Path) -> dict[str, dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return parsed
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = INSTANCE_LINE.match(raw_line)
        if not match:
            continue
        details = match.group("details")
        gap_match = re.search(r"gap=([-+0-9.eE]+)%", details)
        time_match = re.search(r"time=([-+0-9.eE]+)s", details)
        elapsed_match = re.fullmatch(r"([-+0-9.eE]+)s", details)
        objective_match = re.search(r"LLM=([^,]+)", details)
        feasible_match = re.search(r"feasible=(True|False)", details)
        parsed[match.group("instance")] = {
            "status": match.group("status").lower(),
            "reason": match.group("reason") or "",
            "gap_pct": parse_float(gap_match.group(1)) if gap_match else None,
            "candidate_time": (
                parse_float(time_match.group(1))
                if time_match
                else parse_float(elapsed_match.group(1)) if elapsed_match else None
            ),
            "objective_text": objective_match.group(1).strip() if objective_match else "",
            "feasible_text": feasible_match.group(1) if feasible_match else "",
            "raw": raw_line.strip(),
        }
    return parsed


def classify_runtime_error(error: str) -> str:
    lowered = error.lower()
    if "memory admission denied" in lowered:
        return "host memory admission denied"
    if "execution timed out" in lowered:
        return "candidate timeout"
    if "candidate exceeded or could not operate within memory limit" in lowered or "memoryerror" in lowered:
        return "candidate memory limit/error"
    for name in ("IndexError", "AttributeError", "KeyError", "TypeError", "ValueError", "NameError"):
        if name.lower() in lowered:
            return f"Python {name}"
    if "gurobipy" in lowered or "model.optimize" in lowered:
        return "Gurobi/optimizer exception"
    if "traceback" in lowered:
        return "other Python exception"
    return "other process/runtime error"


def compact_error_evidence(error: str, limit: int = 1400) -> str:
    """Keep useful saved evidence while making it safe to embed in the report."""
    if not error:
        return "No error text was recorded."
    cleaned = error.replace(str(ROOT), "<repo>").replace("\r", "")
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n… [saved error text truncated in report]"
    return cleaned


def summarize_instance_failure(item: dict[str, Any]) -> str:
    """Return the most specific concise diagnosis supported by saved artifacts."""
    if item.get("status") == "pass":
        gap = item.get("gap_pct")
        return f"Passed; feasible with {gap:.2f}% gap." if gap is not None else "Passed and marked feasible."

    reason = item.get("fail_reason") or "unknown failure"
    error = item.get("error") or ""
    if reason == "runtime_error":
        category = item.get("error_category") or classify_runtime_error(error)
        if category == "host memory admission denied":
            match = re.search(
                r"memory admission denied: MemAvailable=(\d+)MiB.*candidate_limit=(\d+)MiB.*host_reserve=(\d+)MiB",
                error,
            )
            if match:
                available, limit, reserve = map(int, match.groups())
                return (
                    f"Launch denied by memory guard: {available / 1024:.1f} GiB available, "
                    f"but {limit / 1024:.0f} GiB candidate + {reserve / 1024:.0f} GiB reserve "
                    f"required {(limit + reserve) / 1024:.0f} GiB."
                )
        if category == "candidate timeout":
            match = re.search(r"Execution timed out after (\d+) seconds", error)
            return f"Candidate exceeded the {match.group(1) if match else 'configured'}s execution limit."
        if category == "candidate memory limit/error":
            return "Candidate exceeded, or could not operate within, the 24 GiB per-process address-space limit."
        if category.startswith("Python "):
            exception = next(
                (line.strip() for line in reversed(error.splitlines()) if category.split()[-1] in line),
                category,
            )
            return f"Candidate Python failure: {exception[:260]}"
        if category == "Gurobi/optimizer exception":
            function_match = re.findall(r'File "[^"]+", line \d+, in ([A-Za-z_][A-Za-z0-9_]*)', error)
            function = function_match[-1] if function_match else "solver function"
            truncation = " The captured traceback ends before the terminal exception text." if not re.search(
                r"(?:Error|Exception):\s*[^\n]+$", error.strip()
            ) else ""
            return f"Candidate exited while calling Gurobi from {function}.{truncation}"
        return f"Candidate process/runtime failure: {category}."
    if reason == "gap_exceeds":
        return error or "Feasible solution exceeded the configured 10% gap threshold."
    if reason == "infeasible":
        violations = error.partition("Violations:")[2].strip()
        if violations:
            first = "; ".join(part.strip() for part in violations.split(";")[:2])
            extra = max(0, len(violations.split(";")) - 2)
            return f"Checker found infeasibility: {first}" + (f"; plus {extra} more recorded violation(s)." if extra else "")
        return error or "Feasibility checker rejected the solution."
    if reason == "checker_error":
        return "The paper-specific feasibility checker crashed while reading or validating the candidate output."
    return error or reason.replace("_", " ")


def summarize_case_outcomes(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No final-test rows were produced."
    pieces: list[str] = []
    pass_count = sum(row.get("status") == "pass" for row in rows)
    if pass_count:
        pieces.append(f"{pass_count}× pass")
    grouped: Counter[tuple[str, str]] = Counter()
    for row in rows:
        if row.get("status") == "pass":
            continue
        reason = row.get("fail_reason") or "unknown"
        detail = row.get("error_category") if reason == "runtime_error" else reason
        grouped[(reason, detail or reason)] += 1
    for (reason, detail), count in grouped.most_common():
        label = detail if reason == "runtime_error" else reason.replace("_", " ")
        pieces.append(f"{count}× {label}")
    return "; ".join(pieces) + "."


def count_tree(paths: list[Path]) -> tuple[int, int, int]:
    files = 0
    symlinks = 0
    size = 0
    for root in paths:
        for path in root.rglob("*"):
            if path.is_symlink():
                symlinks += 1
            elif path.is_file():
                files += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
    return files, symlinks, size


def badge(text: str, kind: str) -> str:
    return f'<span class="badge {html.escape(kind)}">{html.escape(text)}</span>'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--report-dir")
    args = parser.parse_args()

    run_id = args.run_id
    batch_dir = ROOT / "eval" / "coral_batches" / run_id
    coral_dir = ROOT / "eval" / "coral" / run_id
    report_dir = (
        Path(args.report_dir).resolve()
        if args.report_dir
        else ROOT / "eval" / "coral_reports" / f"{run_id}-stopped-20260807"
    )
    data_dir = report_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    state = read_json(batch_dir / "state.json")
    progress = read_json(batch_dir / "progress.json")
    config = read_json(batch_dir / "config.json")
    manifest = read_json(batch_dir / "manifest.json")

    csv_sources = {
        "test_results": ROOT / "eval" / "eval_test_results_coral.csv",
        "dev_results": ROOT / "eval" / "eval_dev_results_coral.csv",
        "api_cost": ROOT / "eval" / "self_evolve_api_cost.csv",
    }
    snapshots: dict[str, list[dict[str, str]]] = {}
    for name, source in csv_sources.items():
        fields, rows = read_filtered_csv(source, run_id)
        snapshots[name] = rows
        write_csv_snapshot(data_dir / f"{name}.csv", fields, rows)

    for source_name in ("state.json", "progress.json", "config.json", "manifest.json", "preflight.json"):
        source = batch_dir / source_name
        if source.is_file():
            shutil.copy2(source, data_dir / source_name)

    test_rows = snapshots["test_results"]
    dev_rows = snapshots["dev_results"]
    cost_rows = snapshots["api_cost"]
    test_by_case: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in test_rows:
        test_by_case[row.get("paper_id", "")].append(row)

    counts = Counter(case.get("status", "unknown") for case in state["cases"].values())
    completed_ids = sorted(
        case_id for case_id, value in state["cases"].items() if value.get("status") == "completed"
    )
    failed_ids = sorted(
        case_id for case_id, value in state["cases"].items() if value.get("status") == "failed"
    )
    interrupted_ids = sorted(
        case_id for case_id, value in state["cases"].items() if value.get("status") == "running"
    )
    pending_ids = sorted(
        case_id for case_id, value in state["cases"].items() if value.get("status") == "pending"
    )

    log_results: dict[str, dict[str, dict[str, Any]]] = {}
    for case_id in completed_ids:
        log_results[case_id] = parse_case_log(batch_dir / "case_logs" / f"{case_id}.log")

    instance_details: list[dict[str, Any]] = []
    runtime_categories: Counter[str] = Counter()
    for row in test_rows:
        case_id = row.get("paper_id", "")
        instance = row.get("instance", "")
        log_result = log_results.get(case_id, {}).get(instance, {})
        reason = row.get("fail_reason") or ""
        error_category = classify_runtime_error(row.get("error", "")) if reason == "runtime_error" else ""
        if error_category:
            runtime_categories[error_category] += 1
        detail = {
            "paper_id": case_id,
            "instance": instance,
            "status": row.get("status") or "unknown",
            "fail_reason": reason,
            "feasible": parse_bool(row.get("feasible")),
            "gap_pct": log_result.get("gap_pct"),
            "candidate_time": parse_float(row.get("time")) or log_result.get("candidate_time"),
            "objective": parse_float(row.get("obj")),
            "aocc": parse_float(row.get("aocc")),
            "error_category": error_category,
            "error": row.get("error") or "",
        }
        detail["diagnosis"] = summarize_instance_failure(detail)
        detail["error_evidence"] = compact_error_evidence(detail["error"])
        instance_details.append(detail)

    case_summaries: list[dict[str, Any]] = []
    for case_id in completed_ids:
        case_state = state["cases"][case_id]
        rows = [item for item in instance_details if item["paper_id"] == case_id]
        passes = sum(item["status"] == "pass" for item in rows)
        feasible = sum(item["feasible"] for item in rows)
        quality_1pct = sum(
            item["feasible"] and item["gap_pct"] is not None and item["gap_pct"] <= 1.0
            for item in rows
        )
        feasible_gaps = [
            item["gap_pct"] for item in rows if item["feasible"] and item["gap_pct"] is not None
        ]
        feasible_times = [
            item["candidate_time"]
            for item in rows
            if item["feasible"] and item["candidate_time"] is not None
        ]
        reasons = Counter(item["fail_reason"] or "pass" for item in rows)
        if passes == 4:
            outcome = "all pass"
            outcome_key = "all-pass"
        elif passes:
            outcome = "partial pass"
            outcome_key = "partial-pass"
        elif feasible:
            outcome = "feasible, gap fail"
            outcome_key = "gap-fail"
        elif reasons.get("runtime_error") == 4:
            outcome = "all runtime errors"
            outcome_key = "runtime"
        else:
            outcome = "no passing test"
            outcome_key = "no-pass"

        base = coral_dir / case_id / "qwen3-coder-plus"
        attempts_dir = base / "coral_run" / ".coral" / "public" / "attempts"
        attempt_records = []
        for path in sorted(attempts_dir.glob("*.json")) if attempts_dir.is_dir() else []:
            try:
                attempt_records.append(read_json(path))
            except (OSError, json.JSONDecodeError):
                pass
        normalized_attempts = [
            {
                "commit": str(record.get("commit_hash") or ""),
                "title": str(record.get("title") or "untitled attempt"),
                "status": str(record.get("status") or "unknown"),
                "score": parse_float(record.get("score")),
                "feedback": str(record.get("feedback") or ""),
                "timestamp": str(record.get("timestamp") or ""),
            }
            for record in sorted(attempt_records, key=lambda record: str(record.get("timestamp") or ""))
        ]
        attempt_scores = [
            score
            for score in (record["score"] for record in normalized_attempts)
            if score is not None
        ]
        finalized_count = sum(record["status"] != "pending" for record in normalized_attempts)
        pending_count = sum(record["status"] == "pending" for record in normalized_attempts)
        selected_code = base / "selected_code.py"
        selected_candidates = sorted((base / "selected").glob("coral_*_code.py"))
        selected_commit = ""
        if selected_candidates:
            selected_match = re.match(r"coral_([0-9a-f]+)_code\.py", selected_candidates[0].name)
            selected_commit = selected_match.group(1) if selected_match else ""
        selected_lines = None
        selected_bytes = None
        if selected_code.is_file():
            selected_bytes = selected_code.stat().st_size
            selected_lines = len(selected_code.read_text(encoding="utf-8", errors="replace").splitlines())
        case_summaries.append(
            {
                "paper_id": case_id,
                "duration_seconds": parse_float(case_state.get("duration_seconds")) or 0.0,
                "attempt_records": len(normalized_attempts),
                "finalized_attempts": finalized_count,
                "pending_attempts": pending_count,
                "attempts": normalized_attempts,
                "max_attempt_score": max(attempt_scores) if attempt_scores else None,
                "positive_attempts": sum(score > 0 for score in attempt_scores),
                "passes": passes,
                "feasible": feasible,
                "quality_1pct": quality_1pct,
                "mean_gap_pct": statistics.mean(feasible_gaps) if feasible_gaps else None,
                "mean_feasible_time": statistics.mean(feasible_times) if feasible_times else None,
                "reasons": dict(reasons),
                "outcome": outcome,
                "outcome_key": outcome_key,
                "diagnosis": summarize_case_outcomes(rows),
                "selected_commit": selected_commit,
                "selected_lines": selected_lines,
                "selected_bytes": selected_bytes,
            }
        )

    failed_cases: list[dict[str, Any]] = []
    for case_id in failed_ids:
        base = coral_dir / case_id / "qwen3-coder-plus"
        validation_path = batch_dir / "case_commands" / f"{case_id}.validation.json"
        validation = read_json(validation_path) if validation_path.is_file() else {}
        combined_logs = ""
        log_dir = base / "coral_run" / ".coral" / "public" / "logs"
        for path in sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []:
            combined_logs += path.read_text(encoding="utf-8", errors="replace")
        coral_start_path = base / "coral_start.log"
        coral_start_text = (
            coral_start_path.read_text(encoding="utf-8", errors="replace")
            if coral_start_path.is_file()
            else ""
        )
        restart_count = len(re.findall(r"restart #\d+", coral_start_text))
        exit_codes = sorted(set(re.findall(r"exited \(code: (-?\d+)\)", coral_start_text)))
        malformed = "Expected 'function.name' to be a string" in combined_logs
        max_turns = "Maximum steps for this agent have been reached" in combined_logs
        if malformed and max_turns:
            cause = "Turn budget exhausted, then malformed endpoint tool call"
        elif malformed:
            cause = "Malformed endpoint tool call (function.name was not a string)"
        elif max_turns:
            cause = "Repeated 20-turn exhaustion without finalizing an attempt"
        else:
            cause = "Agent exited repeatedly without finalizing an attempt"
        if malformed:
            evidence = "OpenCode/AI SDK: Expected 'function.name' to be a string."
        elif max_turns:
            evidence = "OpenCode: Maximum steps for this agent have been reached."
        else:
            evidence = next(
                (line.strip() for line in coral_start_text.splitlines() if "ERROR" in line),
                "No more-specific agent error was captured.",
            )
        failed_cases.append(
            {
                "paper_id": case_id,
                "duration_seconds": parse_float(state["cases"][case_id].get("duration_seconds")) or 0.0,
                "cause": cause,
                "attempts": validation.get("finalized_attempts", 0),
                "restart_events": restart_count,
                "exit_codes": exit_codes,
                "evidence": evidence,
                "failures": validation.get("failures", []),
            }
        )

    interrupted_cases: list[dict[str, Any]] = []
    for case_id in interrupted_ids:
        base = coral_dir / case_id / "qwen3-coder-plus"
        attempts_dir = base / "coral_run" / ".coral" / "public" / "attempts"
        attempt_statuses: list[str] = []
        for path in sorted(attempts_dir.glob("*.json")) if attempts_dir.is_dir() else []:
            try:
                attempt_statuses.append(str(read_json(path).get("status") or "unknown"))
            except (OSError, json.JSONDecodeError):
                attempt_statuses.append("unreadable")
        partial = parse_case_log(batch_dir / "case_logs" / f"{case_id}.log")
        interrupted_cases.append(
            {
                "paper_id": case_id,
                "attempt_records": len(attempt_statuses),
                "finalized_attempts": sum(status != "pending" for status in attempt_statuses),
                "pending_attempts": sum(status == "pending" for status in attempt_statuses),
                "test_instances_seen": sorted(partial),
                "error": state["cases"][case_id].get("error") or "interrupted during stop",
            }
        )

    result_statuses = Counter(row.get("status") or "unknown" for row in test_rows)
    failure_reasons = Counter(row.get("fail_reason") or "pass" for row in test_rows)
    feasible_count = sum(parse_bool(row.get("feasible")) for row in test_rows)
    quality_1pct_count = sum(
        item["feasible"] and item["gap_pct"] is not None and item["gap_pct"] <= 1.0
        for item in instance_details
    )
    gap_known_count = sum(item["gap_pct"] is not None for item in instance_details)
    completed_durations = [item["duration_seconds"] for item in case_summaries]
    all_pass_cases = sum(item["passes"] == 4 for item in case_summaries)
    any_pass_cases = sum(item["passes"] > 0 for item in case_summaries)
    any_feasible_cases = sum(item["feasible"] > 0 for item in case_summaries)
    attempt_records = sum(item["attempt_records"] for item in case_summaries)
    finalized_attempts = sum(item["finalized_attempts"] for item in case_summaries)
    pending_attempts = sum(item["pending_attempts"] for item in case_summaries)
    positive_attempts = sum(item["positive_attempts"] for item in case_summaries)
    token_prompt = sum(int(float(row.get("prompt_tokens") or 0)) for row in cost_rows)
    token_completion = sum(int(float(row.get("completion_tokens") or 0)) for row in cost_rows)
    reported_cost = sum(float(row.get("api_cost") or 0) for row in cost_rows)

    files, symlinks, raw_size = count_tree([batch_dir, coral_dir])
    archive = ROOT / "eval" / "coral_archives" / f"{run_id}-stopped-20260807.tar.zst"
    archive_hash_path = Path(str(archive) + ".sha256")
    archive_hash = ""
    if archive_hash_path.is_file():
        archive_hash = archive_hash_path.read_text(encoding="utf-8").split()[0]

    total_cases = int(manifest.get("case_count") or len(state["cases"]))
    terminal_cases = counts.get("completed", 0) + counts.get("failed", 0)
    generated_at = datetime.now(timezone.utc).isoformat()
    analysis = {
        "run_id": run_id,
        "generated_at": generated_at,
        "run_status": state.get("status"),
        "stopped_at": state.get("updated_at"),
        "case_counts": dict(counts),
        "total_cases": total_cases,
        "terminal_cases": terminal_cases,
        "terminal_coverage": terminal_cases / total_cases if total_cases else None,
        "test_rows": len(test_rows),
        "test_statuses": dict(result_statuses),
        "failure_reasons": dict(failure_reasons),
        "feasible_count": feasible_count,
        "quality_1pct_reconstructed_count": quality_1pct_count,
        "gap_reconstructed_count": gap_known_count,
        "all_pass_cases": all_pass_cases,
        "any_pass_cases": any_pass_cases,
        "any_feasible_cases": any_feasible_cases,
        "finalized_attempts_completed_cases": finalized_attempts,
        "attempt_records_completed_cases": attempt_records,
        "pending_attempt_records_completed_cases": pending_attempts,
        "positive_scored_attempts": positive_attempts,
        "runtime_error_categories": dict(runtime_categories),
        "completed_duration": {
            "sum_seconds": sum(completed_durations),
            "median_seconds": statistics.median(completed_durations) if completed_durations else None,
            "p90_seconds": percentile(completed_durations, 0.9),
            "max_seconds": max(completed_durations) if completed_durations else None,
        },
        "metric_completeness": {
            "csv_gap_nonempty": sum(bool(row.get("gap")) for row in test_rows),
            "csv_delta_time_nonempty": sum(bool(row.get("delta_time")) for row in test_rows),
            "log_gap_reconstructed": gap_known_count,
            "qte_available": False,
        },
        "api_accounting": {
            "rows": len(cost_rows),
            "prompt_tokens": token_prompt,
            "completion_tokens": token_completion,
            "reported_cost": reported_cost,
            "scope": "seed/accounting rows only; CORAL agent usage is not fully priced here",
        },
        "artifacts": {
            "regular_files": files,
            "symlinks": symlinks,
            "raw_bytes": raw_size,
            "archive": str(archive),
            "archive_bytes": archive.stat().st_size if archive.is_file() else None,
            "archive_sha256": archive_hash,
        },
        "completed_cases": case_summaries,
        "failed_cases": failed_cases,
        "interrupted_cases": interrupted_cases,
        "pending_cases": pending_ids,
        "instances": instance_details,
    }
    (data_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    outcome_segments = [
        ("pass", result_statuses.get("pass", 0), "green"),
        ("gap exceeds", failure_reasons.get("gap_exceeds", 0), "amber"),
        ("infeasible", failure_reasons.get("infeasible", 0), "orange"),
        ("checker error", failure_reasons.get("checker_error", 0), "purple"),
        ("runtime error", failure_reasons.get("runtime_error", 0), "red"),
    ]
    segment_html = "".join(
        f'<div class="segment {kind}" style="width:{100 * count / len(test_rows):.4f}%" '
        f'title="{html.escape(label)}: {count}"></div>'
        for label, count, kind in outcome_segments
        if count
    )
    legend_html = "".join(
        f'<span><i class="dot {kind}"></i>{html.escape(label)} <b>{count}</b> '
        f'({pct(count, len(test_rows))})</span>'
        for label, count, kind in outcome_segments
    )

    slowest = sorted(case_summaries, key=lambda item: item["duration_seconds"], reverse=True)[:8]
    slowest_html = "".join(
        f'<tr><td>{html.escape(item["paper_id"])}</td><td>{human_duration(item["duration_seconds"])}</td>'
        f'<td>{item["passes"]}/4</td><td>{html.escape(item["outcome"])}</td></tr>'
        for item in slowest
    )

    completed_rows_html = []
    for item in case_summaries:
        kind = {
            "all-pass": "ok",
            "partial-pass": "warn",
            "gap-fail": "info",
            "runtime": "bad",
            "no-pass": "muted",
        }[item["outcome_key"]]
        attempts_display = str(item["finalized_attempts"])
        if item["pending_attempts"]:
            attempts_display += f' + {item["pending_attempts"]} pending'
        completed_rows_html.append(
            f'<tr data-search="{html.escape(item["paper_id"].lower())}" '
            f'data-outcome="{html.escape(item["outcome_key"])}">'
            f'<td class="mono">{html.escape(item["paper_id"])}</td>'
            f'<td data-value="{item["duration_seconds"]}">{human_duration(item["duration_seconds"])}</td>'
            f'<td>{html.escape(attempts_display)}</td>'
            f'<td>{item["passes"]}/4</td><td>{item["feasible"]}/4</td>'
            f'<td>{item["quality_1pct"]}/4</td>'
            f'<td data-value="{item["mean_gap_pct"] if item["mean_gap_pct"] is not None else 1e99}">'
            f'{fmt_number(item["mean_gap_pct"])}%</td>'
            f'<td>{fmt_number(item["mean_feasible_time"])}s</td>'
            f'<td>{badge(item["outcome"], kind)}</td>'
            f'<td class="diag-summary">{html.escape(item["diagnosis"])}</td>'
            f'<td>{item["selected_lines"] if item["selected_lines"] is not None else "—"}</td></tr>'
        )

    instance_rows_html = []
    for item in sorted(instance_details, key=lambda row: (row["paper_id"], row["instance"])):
        if item["status"] == "pass":
            status_badge = badge("pass", "ok")
            outcome_filter = "pass"
        else:
            reason = item["fail_reason"] or "fail"
            status_badge = badge(reason.replace("_", " "), "bad" if reason == "runtime_error" else "warn")
            outcome_filter = reason
        detail = item["diagnosis"]
        evidence_html = ""
        if item["error"]:
            evidence_html = (
                '<details class="error-evidence"><summary>Saved error evidence</summary>'
                f'<pre>{html.escape(item["error_evidence"])}</pre></details>'
            )
        instance_rows_html.append(
            f'<tr data-search="{html.escape((item["paper_id"] + " " + item["instance"] + " " + detail).lower())}" '
            f'data-result="{html.escape(outcome_filter)}">'
            f'<td class="mono">{html.escape(item["paper_id"])}</td><td>{html.escape(item["instance"])}</td>'
            f'<td>{status_badge}</td><td>{"yes" if item["feasible"] else "no / unknown"}</td>'
            f'<td>{fmt_number(item["gap_pct"])}%</td><td>{fmt_number(item["candidate_time"])}s</td>'
            f'<td>{fmt_number(item["objective"], 4)}</td><td>{html.escape(detail)}{evidence_html}</td></tr>'
        )

    failed_rows_html = "".join(
        f'<tr><td class="mono">{html.escape(item["paper_id"])}</td>'
        f'<td>{human_duration(item["duration_seconds"])}</td><td>{item["attempts"]}</td>'
        f'<td>{html.escape(item["cause"])}<br><span class="small">Restart events: {item["restart_events"]}; '
        f'exit codes: {html.escape(", ".join(item["exit_codes"]) or "unknown")}</span></td>'
        f'<td>{html.escape("; ".join(item["failures"]))}'
        f'<details class="error-evidence"><summary>Saved agent evidence</summary><pre>{html.escape(item["evidence"])}</pre></details></td></tr>'
        for item in failed_cases
    )
    interrupted_html = "".join(
        f'<li><span class="mono">{html.escape(item["paper_id"])}</span>: {item["finalized_attempts"]} finalized '
        f'and {item["pending_attempts"]} pending attempt records; '
        f'test artifacts seen: {html.escape(", ".join(item["test_instances_seen"]) or "none")}. '
        f'{html.escape(item["error"])}</li>'
        for item in interrupted_cases
    ) or "<li>None</li>"

    diagnostic_entries: list[str] = []
    for item in case_summaries:
        rows = sorted(
            (row for row in instance_details if row["paper_id"] == item["paper_id"]),
            key=lambda row: row["instance"],
        )
        attempt_parts: list[str] = []
        for record in item["attempts"]:
            attempt_kind = (
                "info"
                if record["status"] == "improved"
                else "warn" if record["status"] in {"regressed", "pending"} else "muted"
            )
            feedback_html = (
                f'<small>{html.escape(record["feedback"])}</small>' if record["feedback"] else ""
            )
            attempt_parts.append(
                '<div class="attempt-card">'
                f'<div>{badge(record["status"], attempt_kind)} '
                f'<code>{html.escape(record["commit"][:12])}</code></div>'
                f'<b>{html.escape(record["title"])}</b>'
                f'<span>score: {fmt_number(record["score"], 6)}</span>{feedback_html}</div>'
            )
        attempt_html = "".join(attempt_parts)

        instance_parts: list[str] = []
        for row in rows:
            result_badge = (
                badge("pass", "ok")
                if row["status"] == "pass"
                else badge(
                    (row["fail_reason"] or "fail").replace("_", " "),
                    "bad" if row["fail_reason"] == "runtime_error" else "warn",
                )
            )
            evidence = ""
            if row["error"]:
                evidence = (
                    '<details class="error-evidence"><summary>Exact saved evidence</summary>'
                    f'<pre>{html.escape(row["error_evidence"])}</pre></details>'
                )
            instance_parts.append(
                '<div class="instance-card">'
                f'<div class="instance-head"><b>{html.escape(row["instance"])}</b>{result_badge}</div>'
                f'<div class="instance-metrics"><span>feasible <b>{"yes" if row["feasible"] else "no/unknown"}</b></span>'
                f'<span>gap <b>{fmt_number(row["gap_pct"])}%</b></span>'
                f'<span>time <b>{fmt_number(row["candidate_time"])}s</b></span>'
                f'<span>objective <b>{fmt_number(row["objective"], 4)}</b></span></div>'
                f'<p>{html.escape(row["diagnosis"])}</p>{evidence}</div>'
            )
        instance_card_html = "".join(instance_parts)
        search_text = " ".join(
            [item["paper_id"], item["diagnosis"]]
            + [row["diagnosis"] + " " + row["error_category"] for row in rows]
        ).lower()
        diagnostic_entries.append(
            f'<details class="case-diag diagnostic-entry" data-search="{html.escape(search_text)}" data-state="completed">'
            f'<summary><span class="mono">{html.escape(item["paper_id"])}</span>{badge("completed", "ok")}'
            f'<span>{item["passes"]}/4 pass</span><span class="diag-headline">{html.escape(item["diagnosis"])}</span></summary>'
            '<div class="diag-body"><div class="diag-facts">'
            f'<span>case runtime <b>{human_duration(item["duration_seconds"])}</b></span>'
            f'<span>attempts <b>{item["finalized_attempts"]} finalized, {item["pending_attempts"]} pending</b></span>'
            f'<span>selected commit <b><code>{html.escape(item["selected_commit"] or "unknown")}</code></b></span>'
            f'<span>selected code <b>{item["selected_lines"] or 0} lines</b></span></div>'
            f'<h4>Attempt trail</h4><div class="attempt-grid">{attempt_html}</div>'
            f'<h4>Final-test evidence</h4><div class="instance-grid">{instance_card_html}</div></div></details>'
        )

    for item in failed_cases:
        search_text = f'{item["paper_id"]} {item["cause"]} {item["evidence"]}'.lower()
        validation_items = "".join(
            f'<li>{html.escape(failure)}</li>' for failure in item["failures"]
        )
        diagnostic_entries.append(
            f'<details class="case-diag diagnostic-entry" data-search="{html.escape(search_text)}" data-state="failed">'
            f'<summary><span class="mono">{html.escape(item["paper_id"])}</span>{badge("validation failed", "bad")}'
            f'<span>0 test rows</span><span class="diag-headline">{html.escape(item["cause"])}</span></summary>'
            '<div class="diag-body"><div class="diag-facts">'
            f'<span>case runtime <b>{human_duration(item["duration_seconds"])}</b></span>'
            f'<span>restart events <b>{item["restart_events"]}</b></span>'
            f'<span>agent exit codes <b>{html.escape(", ".join(item["exit_codes"]) or "unknown")}</b></span>'
            f'<span>attempts <b>{item["attempts"]}</b></span></div>'
            f'<h4>Root evidence</h4><pre>{html.escape(item["evidence"])}</pre>'
            f'<h4>Post-run validation failures</h4><ul>{validation_items}</ul>'
            '</div></details>'
        )

    for item in interrupted_cases:
        diagnostic_entries.append(
            f'<details class="case-diag diagnostic-entry" data-search="{html.escape((item["paper_id"] + " interrupted resumable").lower())}" data-state="interrupted">'
            f'<summary><span class="mono">{html.escape(item["paper_id"])}</span>{badge("interrupted", "warn")}'
            f'<span class="diag-headline">Stopped safely; native resume remains available.</span></summary>'
            '<div class="diag-body"><div class="diag-facts">'
            f'<span>finalized attempts <b>{item["finalized_attempts"]}</b></span>'
            f'<span>pending records <b>{item["pending_attempts"]}</b></span>'
            f'<span>final tests seen <b>{html.escape(", ".join(item["test_instances_seen"]) or "none")}</b></span></div>'
            f'<p>{html.escape(item["error"])}</p></div></details>'
        )
    diagnostics_html = "".join(diagnostic_entries)

    example = next(item for item in case_summaries if item["paper_id"] == "brandao2016")
    example_rows = sorted(
        (row for row in instance_details if row["paper_id"] == "brandao2016"),
        key=lambda row: row["instance"],
    )
    example_attempts_html = "".join(
        '<div class="example-event">'
        f'<span class="event-time">{html.escape(record["timestamp"][11:19] or "—")}</span>'
        f'<div><b>{html.escape(record["status"].title())}: {html.escape(record["title"])}</b>'
        f'<p><code>{html.escape(record["commit"][:12])}</code> · score {fmt_number(record["score"], 6)}'
        f'{" · not evaluated before selection" if record["status"] == "pending" else ""}</p></div></div>'
        for record in example["attempts"]
    )
    example_final_html = "".join(
        '<div class="example-result">'
        f'<b>{html.escape(row["instance"])}</b>'
        f'{badge("pass", "ok") if row["status"] == "pass" else badge((row["fail_reason"] or "fail").replace("_", " "), "warn")}'
        f'<span>gap {fmt_number(row["gap_pct"])}%</span><span>time {fmt_number(row["candidate_time"])}s</span>'
        f'<small>{html.escape(row["diagnosis"])}</small></div>'
        for row in example_rows
    )
    pending_html = ", ".join(html.escape(case_id) for case_id in pending_ids)

    runtime_max = max(runtime_categories.values(), default=1)
    runtime_category_html = "".join(
        f'<div class="reason-row"><span>{html.escape(name)}</span>'
        f'<div class="reason-track"><i style="width:{100 * count / runtime_max:.2f}%"></i></div>'
        f'<b>{count}</b></div>'
        for name, count in runtime_categories.most_common()
    )

    config_body = config.get("config", {})
    candidate_config = config_body.get("candidate") or {}
    guard_config = config_body.get("global_guard") or {}
    archive_rel = f"../../coral_archives/{archive.name}"
    report_title = "Qwen3-Coder-Plus × CORAL stopped-run analysis"
    html_text = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report_title)}</title>
<style>
:root {{ --ink:#17202a; --muted:#667085; --line:#e4e7ec; --paper:#f7f8fa; --card:#fff; --navy:#132238; --blue:#2e5aac; --green:#16845b; --amber:#d08a00; --orange:#d65f2e; --red:#c53b3f; --purple:#7455aa; }}
* {{ box-sizing:border-box; }} body {{ margin:0; color:var(--ink); background:var(--paper); font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ width:min(1380px,calc(100% - 36px)); margin:0 auto; }}
header {{ color:#fff; background:linear-gradient(125deg,#101d30,#203b62 62%,#2e5aac); padding:42px 0 34px; }}
h1 {{ margin:8px 0 5px; font-size:clamp(28px,4vw,46px); line-height:1.08; letter-spacing:-.035em; }}
.eyebrow {{ text-transform:uppercase; letter-spacing:.13em; font-size:12px; opacity:.78; }}
.subtitle {{ color:#d7e2f4; max-width:880px; font-size:16px; }}
.status {{ display:inline-flex; align-items:center; gap:8px; padding:5px 10px; border:1px solid #ffffff4a; border-radius:999px; background:#ffffff14; font-weight:700; }}
.status:before {{ content:""; width:8px; height:8px; border-radius:50%; background:#ffca5c; }}
main {{ padding:26px 0 60px; }} .grid {{ display:grid; gap:14px; }} .cards {{ grid-template-columns:repeat(6,minmax(0,1fr)); margin-top:-48px; }}
.card,.panel {{ background:var(--card); border:1px solid var(--line); border-radius:14px; box-shadow:0 7px 26px #15233b0a; }}
.card {{ padding:17px; min-height:112px; }} .card .value {{ font-size:28px; line-height:1.1; font-weight:780; letter-spacing:-.03em; margin:10px 0 3px; }}
.label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.07em; }} .hint {{ color:var(--muted); font-size:12px; }}
.panel {{ padding:22px; margin-top:18px; }} h2 {{ margin:0 0 14px; font-size:21px; letter-spacing:-.018em; }} h3 {{ margin:22px 0 9px; font-size:16px; }}
.two {{ grid-template-columns:minmax(0,1.55fr) minmax(310px,.75fr); }}
.stack {{ display:flex; height:24px; overflow:hidden; border-radius:8px; background:#edf0f4; margin:15px 0 12px; }} .segment {{ min-width:2px; }}
.green,.dot.green {{ background:var(--green); }} .amber,.dot.amber {{ background:var(--amber); }} .orange,.dot.orange {{ background:var(--orange); }} .red,.dot.red {{ background:var(--red); }} .purple,.dot.purple {{ background:var(--purple); }}
.legend {{ display:flex; flex-wrap:wrap; gap:8px 18px; color:var(--muted); }} .legend span {{ white-space:nowrap; }} .dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }}
.callout {{ border-left:4px solid var(--amber); background:#fff8e8; padding:12px 14px; border-radius:7px; margin:11px 0; }} .callout.bad {{ border-color:var(--red); background:#fff1f1; }} .callout.info {{ border-color:var(--blue); background:#f0f5ff; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }} th {{ color:#475467; background:#fafbfc; position:sticky; top:0; font-size:12px; text-transform:uppercase; letter-spacing:.045em; cursor:default; }} th[data-sort] {{ cursor:pointer; }} tbody tr:hover {{ background:#f8faff; }}
.table-wrap {{ overflow:auto; max-height:620px; border:1px solid var(--line); border-radius:10px; }} .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
.badge {{ display:inline-block; border-radius:999px; padding:3px 8px; white-space:nowrap; font-size:11px; font-weight:750; }} .badge.ok {{ color:#086645; background:#dff6ed; }} .badge.warn {{ color:#855800; background:#fff0c7; }} .badge.bad {{ color:#962d31; background:#ffe0e1; }} .badge.info {{ color:#234f9a; background:#e5eeff; }} .badge.muted {{ color:#596273; background:#edf0f4; }}
.toolbar {{ display:flex; gap:9px; flex-wrap:wrap; margin:0 0 12px; }} input,select {{ border:1px solid #ccd2dc; border-radius:8px; padding:8px 10px; background:#fff; color:var(--ink); }} input {{ min-width:240px; }}
.reason-row {{ display:grid; grid-template-columns:minmax(170px,1.4fr) 2fr 40px; align-items:center; gap:10px; margin:9px 0; }} .reason-track {{ height:9px; background:#edf0f4; border-radius:99px; overflow:hidden; }} .reason-track i {{ display:block; height:100%; background:var(--red); border-radius:99px; }}
.metrics {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }} .metric {{ padding:12px; border:1px solid var(--line); border-radius:9px; }} .metric b {{ display:block; font-size:19px; }}
.workflow {{ display:grid; grid-template-columns:repeat(7,minmax(130px,1fr)); gap:22px; margin:18px 0 20px; }}
.flow-step {{ position:relative; min-height:148px; padding:15px 14px; border:1px solid var(--line); border-top:4px solid var(--blue); border-radius:11px; background:#fbfcfe; }}
.flow-step.agent {{ border-top-color:var(--purple); background:#faf7ff; }} .flow-step.eval {{ border-top-color:var(--amber); background:#fffaf0; }} .flow-step.sandbox {{ border-top-color:var(--green); background:#f3fbf7; }}
.flow-step:not(:last-child):after {{ content:"→"; position:absolute; right:-19px; top:57px; color:#8390a3; font-size:23px; font-weight:800; }}
.flow-num {{ display:flex; align-items:center; justify-content:center; width:25px; height:25px; margin-bottom:9px; border-radius:50%; color:#fff; background:var(--navy); font-size:12px; font-weight:800; }}
.flow-step h3 {{ margin:0 0 6px; font-size:14px; }} .flow-step p {{ margin:0; color:var(--muted); font-size:12px; line-height:1.42; }}
.loop-box {{ border:1px solid #d9cfec; border-radius:12px; background:linear-gradient(100deg,#f8f4ff,#fffaf0); padding:16px; }}
.loop-title {{ display:flex; align-items:center; gap:9px; margin-bottom:12px; font-weight:780; }} .loop-title:before {{ content:"↻"; display:grid; place-items:center; width:29px; height:29px; border-radius:50%; color:#fff; background:var(--purple); font-size:19px; }}
.loop-flow {{ display:grid; grid-template-columns:1.2fr 24px 1fr 24px 1fr 24px 1.1fr 24px 1.2fr; align-items:stretch; gap:7px; }}
.loop-node {{ padding:11px; border-radius:8px; border:1px solid #ded8ea; background:#fff; font-size:12px; }} .loop-node b {{ display:block; margin-bottom:3px; color:var(--navy); }} .loop-arrow {{ display:grid; place-items:center; color:#8d7ca9; font-size:18px; font-weight:800; }}
.loop-return {{ margin:11px 0 0; padding-top:10px; border-top:1px dashed #cfc5df; color:#5f5275; font-size:12px; text-align:center; }}
.lane-key {{ display:flex; flex-wrap:wrap; gap:9px 16px; margin-top:13px; color:var(--muted); font-size:12px; }} .lane-key span:before {{ content:""; display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:6px; background:var(--blue); }} .lane-key .agent-key:before {{ background:var(--purple); }} .lane-key .eval-key:before {{ background:var(--amber); }} .lane-key .sandbox-key:before {{ background:var(--green); }}
.artifact-trail {{ margin-top:14px; padding:11px 13px; border-radius:8px; color:#344054; background:#f2f4f7; font-size:12px; overflow-wrap:anywhere; }}
.deep-pipeline {{ margin-top:16px; border:1px solid var(--line); border-radius:10px; overflow:hidden; }} .deep-pipeline>summary {{ padding:13px 15px; background:#f8fafc; }} .deep-pipeline .table-wrap {{ max-height:none; border:0; border-top:1px solid var(--line); border-radius:0; }}
.owner {{ font-weight:750; white-space:nowrap; }} .owner.batch {{ color:var(--blue); }} .owner.agent {{ color:var(--purple); }} .owner.eval {{ color:#9a6500; }} .owner.sandbox {{ color:var(--green); }}
.decision-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:11px; margin-top:15px; }} .decision {{ position:relative; padding:13px; border:1px solid var(--line); border-radius:9px; background:#fff; font-size:12px; }} .decision b {{ display:block; margin-bottom:5px; }} .decision .yes {{ color:var(--green); }} .decision .no {{ color:var(--red); }}
.command {{ display:block; margin-top:6px; padding:8px; color:#d8e6fb; background:#17263b; border-radius:6px; font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }}
.example-shell {{ display:grid; grid-template-columns:minmax(280px,.8fr) minmax(0,1.5fr); gap:18px; }} .example-context {{ padding:15px; border-radius:10px; color:#e7effb; background:var(--navy); }} .example-context h3 {{ margin-top:0; color:#fff; }} .example-context code {{ color:#d9e8ff; background:#ffffff17; }}
.example-timeline {{ position:relative; padding-left:17px; border-left:2px solid #cabbe4; }} .example-event {{ display:grid; grid-template-columns:64px 1fr; gap:10px; position:relative; padding:0 0 14px 11px; }} .example-event:before {{ content:""; position:absolute; left:-23px; top:5px; width:9px; height:9px; border-radius:50%; background:var(--purple); box-shadow:0 0 0 4px #f7f2ff; }} .event-time {{ color:var(--muted); font:11px ui-monospace,SFMono-Regular,Menlo,monospace; }} .example-event p {{ margin:3px 0 0; color:var(--muted); font-size:12px; }}
.example-results {{ display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin-top:11px; }} .example-result {{ display:grid; gap:5px; padding:11px; border:1px solid var(--line); border-radius:8px; }} .example-result small {{ color:var(--muted); }}
.diagnostic-list {{ display:grid; gap:8px; }} .case-diag {{ margin:0; border:1px solid var(--line); border-radius:9px; background:#fff; overflow:hidden; }} .case-diag>summary {{ display:grid; grid-template-columns:155px 120px 80px minmax(260px,1fr); align-items:center; gap:9px; padding:11px 13px; }} .case-diag[open]>summary {{ background:#f8fafc; border-bottom:1px solid var(--line); }} .diag-headline {{ color:#475467; font-weight:500; }} .diag-body {{ padding:15px; }} .diag-body h4 {{ margin:15px 0 8px; }}
.diag-facts {{ display:flex; flex-wrap:wrap; gap:8px; }} .diag-facts>span {{ padding:7px 9px; border-radius:7px; background:#f2f4f7; font-size:12px; }} .attempt-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:9px; }} .attempt-card {{ display:grid; gap:5px; padding:11px; border:1px solid var(--line); border-radius:8px; font-size:12px; }} .attempt-card small {{ color:var(--muted); white-space:pre-line; }}
.instance-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:9px; }} .instance-card {{ padding:11px; border:1px solid var(--line); border-radius:8px; min-width:0; }} .instance-head {{ display:flex; justify-content:space-between; gap:8px; align-items:center; }} .instance-metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:4px 8px; margin:9px 0; color:var(--muted); font-size:11px; }} .instance-card p {{ font-size:12px; }}
.error-evidence {{ margin-top:7px; }} .error-evidence summary {{ color:var(--blue); font-size:11px; }} pre {{ margin:7px 0 0; padding:10px; border:1px solid #dfe3e9; border-radius:7px; background:#f7f8fa; white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; max-height:310px; overflow:auto; }} .diag-summary {{ min-width:220px; font-size:12px; color:#475467; }}
details {{ margin-top:13px; }} summary {{ cursor:pointer; font-weight:700; }} code {{ background:#edf0f4; padding:2px 5px; border-radius:4px; }} a {{ color:var(--blue); }} ul {{ padding-left:20px; }} .small {{ color:var(--muted); font-size:12px; }} footer {{ color:var(--muted); margin-top:24px; }}
@media(max-width:1050px) {{ .cards {{ grid-template-columns:repeat(3,1fr); }} .two,.example-shell {{ grid-template-columns:1fr; }} .workflow {{ grid-template-columns:repeat(4,1fr); }} .flow-step:after {{ display:none; }} .loop-flow {{ grid-template-columns:1fr; }} .loop-arrow {{ transform:rotate(90deg); }} .decision-grid,.instance-grid,.example-results {{ grid-template-columns:repeat(2,1fr); }} .case-diag>summary {{ grid-template-columns:135px 110px 70px 1fr; }} }} @media(max-width:650px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} .metrics,.decision-grid,.attempt-grid,.instance-grid,.example-results {{ grid-template-columns:1fr; }} .workflow {{ grid-template-columns:1fr; gap:9px; }} .flow-step {{ min-height:0; }} .case-diag>summary {{ grid-template-columns:1fr; }} .wrap {{ width:min(100% - 20px,1380px); }} }}
@media print {{ body {{ background:#fff; }} header {{ background:#fff; color:#000; padding:15px 0; }} .subtitle {{ color:#333; }} .cards {{ margin-top:10px; }} .card,.panel {{ box-shadow:none; break-inside:avoid; }} .toolbar {{ display:none; }} .table-wrap {{ max-height:none; overflow:visible; }} }}
</style>
</head>
<body>
<header><div class="wrap"><div class="eyebrow">FrontierOR evaluation report</div><h1>{html.escape(report_title)}</h1><p class="subtitle">A frozen, artifact-backed analysis of the partial 180-case run. This report separates batch completion, candidate feasibility, quality gates, infrastructure failures, and metric completeness.</p><span class="status">Stopped safely at {html.escape(str(state.get("updated_at")))}</span></div></header>
<main class="wrap">
<section class="grid cards">
  <div class="card"><div class="label">Terminal coverage</div><div class="value">{terminal_cases}/{total_cases}</div><div class="hint">{pct(terminal_cases,total_cases)} of manifest</div></div>
  <div class="card"><div class="label">Completed cases</div><div class="value">{counts.get("completed",0)}</div><div class="hint">+ {counts.get("failed",0)} validation failures</div></div>
  <div class="card"><div class="label">Test pass</div><div class="value">{result_statuses.get("pass",0)}/{len(test_rows)}</div><div class="hint">{pct(result_statuses.get("pass",0),len(test_rows))} instance pass rate</div></div>
  <div class="card"><div class="label">Feasible</div><div class="value">{feasible_count}/{len(test_rows)}</div><div class="hint">{pct(feasible_count,len(test_rows))} including gap failures</div></div>
  <div class="card"><div class="label">≤1% gap, reconstructed</div><div class="value">{quality_1pct_count}/{len(test_rows)}</div><div class="hint">{pct(quality_1pct_count,len(test_rows))} of all tests</div></div>
  <div class="card"><div class="label">Preserved raw data</div><div class="value">{human_bytes(raw_size)}</div><div class="hint">{files:,} files + {symlinks:,} links</div></div>
</section>

<section class="grid two">
<div class="panel"><h2>Executive findings</h2>
  <div class="stack">{segment_html}</div><div class="legend">{legend_html}</div>
  <h3>What the partial run established</h3>
  <ul>
    <li><b>{all_pass_cases}</b> of {len(case_summaries)} completed cases passed all four large tests; <b>{any_pass_cases}</b> had at least one pass and <b>{any_feasible_cases}</b> had at least one feasible result.</li>
    <li><b>{feasible_count}</b> results were feasible. Nine feasible results still failed the configured quality gap, leaving <b>{result_statuses.get("pass",0)}</b> official passes.</li>
    <li><b>{failure_reasons.get("runtime_error",0)}</b> of {len(test_rows)} tests ended in runtime errors, the dominant limitation of this snapshot.</li>
    <li>The logs reconstruct <b>{gap_known_count}</b> gaps and show <b>{quality_1pct_count}</b> results at or below 1% gap. The CSV itself omitted all gap and delta-time fields, so official QTE is unavailable.</li>
  </ul>
  <div class="callout bad"><b>Configuration interaction:</b> 16 runtime errors across four cases were host-memory admission denials. Case admission was 48 GiB, but each candidate launch required 24 GiB limit + 32 GiB host reserve = 56 GiB. A case could therefore start at 48 GiB and later have all four tests rejected below 56 GiB.</div>
  <div class="callout"><b>Interpret partial-run metrics carefully:</b> this is 55 terminal cases plus one interrupted case, not the full 180-case benchmark. The case mix is alphabetically ordered and is not a random sample.</div>
</div>
<div class="panel"><h2>Run snapshot</h2>
  <div class="metrics">
    <div class="metric"><span class="label">Completed runtime</span><b>{human_duration(sum(completed_durations))}</b><span class="small">sum across 51 cases</span></div>
    <div class="metric"><span class="label">Median case</span><b>{human_duration(statistics.median(completed_durations) if completed_durations else None)}</b></div>
    <div class="metric"><span class="label">P90 case</span><b>{human_duration(percentile(completed_durations,.9))}</b></div>
    <div class="metric"><span class="label">Finalized attempts</span><b>{finalized_attempts}</b><span class="small">completed cases</span></div>
    <div class="metric"><span class="label">Positive-score attempts</span><b>{positive_attempts}</b></div>
    <div class="metric"><span class="label">Pending</span><b>{counts.get("pending",0)}</b></div>
  </div>
  <h3>Fixed configuration</h3>
  <ul class="small">
    <li>Model: <code>{html.escape(str(config_body.get("agent_model")))}</code></li>
    <li>Attempts: {config_body.get("attempts")}; one agent; max {config_body.get("max_turns")} turns</li>
    <li>Candidate: Bubblewrap, 1 CPU, {html.escape(str(candidate_config.get("memory")))} per-process address-space ceiling</li>
    <li>Admission: {guard_config.get("case_admission_mem_available_gib")} GiB; reserve: {html.escape(str(candidate_config.get("memory_reserve")))}</li>
  </ul>
</div></section>

<section class="panel" id="workflow"><h2>Pipeline workflow: how CORAL + OpenCode optimize one case</h2>
<p>OpenCode is the coding agent and tool runtime; CORAL is the experiment loop that versions attempts, invokes the grader, returns evidence, and selects the candidate. The batch controller wraps that per-case loop with discovery, resource admission, persistence, and resume.</p>
<div class="workflow">
  <div class="flow-step"><span class="flow-num">1</span><h3>Discover & admit</h3><p>Load one frozen paper from the 180-case manifest. Check disk and host memory before starting the serial case.</p></div>
  <div class="flow-step"><span class="flow-num">2</span><h3>Generate seed</h3><p>Call the OpenAI-compatible endpoint with upstream model <code>qwen3-coder-plus</code> to create the initial <code>code.py</code>.</p></div>
  <div class="flow-step"><span class="flow-num">3</span><h3>Create CORAL workspace</h3><p>Build the task repo, grader config, shared <code>.coral</code> state, Git checkpoint, and isolated OpenCode configuration.</p></div>
  <div class="flow-step agent"><span class="flow-num">4</span><h3>OpenCode agent</h3><p>Run <code>frontier/qwen3-coder-plus</code> for up to 20 turns. It reads the task, edits code, runs tools, and submits attempts.</p></div>
  <div class="flow-step eval"><span class="flow-num">5</span><h3>CORAL grades</h3><p>Tiny gate first; passing candidates continue to the median-τg dev instance. Score and failure evidence feed back to the agent.</p></div>
  <div class="flow-step"><span class="flow-num">6</span><h3>Select best code</h3><p>After two finalized attempts or budget exhaustion, CORAL selects the strongest checkpoint as <code>selected_code.py</code>.</p></div>
  <div class="flow-step sandbox"><span class="flow-num">7</span><h3>Final test & persist</h3><p>Run <code>large_2…large_5</code> in Bubblewrap, then save solutions, logs, CSV rows, metadata, and resumable state.</p></div>
</div>
<div class="loop-box">
  <div class="loop-title">The optimization feedback loop (same native OpenCode session)</div>
  <div class="loop-flow">
    <div class="loop-node"><b>Inspect & modify</b>Read schemas and current solver; use filesystem, shell, and editing tools.</div><div class="loop-arrow">→</div>
    <div class="loop-node"><b>Finalize attempt</b>Commit a concrete candidate and register it in CORAL's attempt store.</div><div class="loop-arrow">→</div>
    <div class="loop-node"><b>Tiny gate</b>Up to 300s; require feasibility and gap ≤10% before dev scoring.</div><div class="loop-arrow">→</div>
    <div class="loop-node"><b>Median dev</b>Evaluate the selected median-τg instance for up to 3600s when the gate passes.</div><div class="loop-arrow">→</div>
    <div class="loop-node"><b>Evidence back to agent</b>Resume by <code>sessionID</code> with score, feasibility, gap, runtime, and errors; pivot after five non-improvements.</div>
  </div>
  <div class="loop-return">↩ The loop continues in the same OpenCode conversation until two attempts are finalized. A dead agent may restart up to three times with 5/10/20s backoff; the saved session and Git state make interruption resumable.</div>
</div>
<div class="lane-key"><span>Batch controller / CORAL state</span><span class="agent-key">OpenCode + Qwen agent</span><span class="eval-key">CORAL evaluator</span><span class="sandbox-key">Bubblewrap candidate execution</span></div>
<div class="artifact-trail"><b>Artifact trail:</b> <code>task.yaml</code> → <code>agent-1.*.log</code> → <code>.coral/public/attempts/*.json</code> → <code>selected_code.py</code> → <code>final_eval/*</code> → <code>eval_test_results_coral.csv</code> → batch validation and the next case.</div>
<details class="deep-pipeline" open><summary>Detailed operational sequence, decisions, and outputs</summary>
<div class="table-wrap"><table><thead><tr><th>#</th><th>Owner</th><th>Actual operation</th><th>Decision / failure behavior</th><th>Persistent output</th></tr></thead><tbody>
<tr><td>1</td><td><span class="owner batch">Batch</span></td><td>Read the frozen, sorted 180-paper manifest and case state. Completed cases are skipped; an interrupted case is eligible for resume.</td><td>One paper worker keeps cases strictly serial.</td><td><code>manifest.json</code>, <code>state.json</code>, config fingerprint</td></tr>
<tr><td>2</td><td><span class="owner batch">Batch</span></td><td>Check host <code>MemAvailable</code> and disk before starting a case.</td><td>Below 48 GiB memory or 50 GiB disk: wait without calling the model.</td><td>Wait reason and timestamps in batch state/log</td></tr>
<tr><td>3</td><td><span class="owner batch">Seed client</span></td><td>Send the problem and schemas to <code>${{OPENAI_BASE_URL}}/chat/completions</code> with upstream model <code>qwen3-coder-plus</code>.<span class="command">OpenAI(base_url=env).chat.completions.create(model="qwen3-coder-plus", messages=…)</span></td><td>The seed is unique to this run; another model's seed is not reused.</td><td><code>coral_task/seed/code.py</code> and <code>README.md</code></td></tr>
<tr><td>4</td><td><span class="owner batch">CORAL setup</span></td><td>Write the task/grader definition: tiny gate, median-τg dev, two attempts, one OpenCode agent, 20 turns.</td><td>Config must match Bubblewrap, 24 GiB, 32 GiB reserve, and provider/model IDs.</td><td><code>task.yaml</code>, task repo, local Git history</td></tr>
<tr><td>5</td><td><span class="owner agent">OpenCode bootstrap</span></td><td>Seed run-local packages, prewarm <code>frontier/qwen3-coder-plus</code>, and verify model discovery.</td><td>A provider/model failure stops the case before agent work is trusted.</td><td>Provider marker, redacted prewarm log, isolated XDG state</td></tr>
<tr><td>6</td><td><span class="owner agent">OpenCode</span></td><td>Force <code>PWD</code> to the agent worktree and launch JSON events.<span class="command">opencode run --model frontier/qwen3-coder-plus --format json --print-logs --log-level ERROR "Begin."</span></td><td>Resume adds <code>--continue --session &lt;sessionID&gt;</code>. Dead agents receive bounded 5/10/20s backoff.</td><td><code>agent-1.N.log</code>, session ID, PID state</td></tr>
<tr><td>7</td><td><span class="owner agent">Qwen agent</span></td><td>Read <code>README.md</code>/<code>code.py</code>, reason about the formulation, edit the solver, and run local checks through OpenCode tools.</td><td>Ground-truth solutions and private grader data remain forbidden.</td><td>Modified worktree, tool stream, notes and diffs</td></tr>
<tr><td>8</td><td><span class="owner agent">Qwen → CORAL</span></td><td>Submit a concrete candidate; <code>coral eval</code> commits code and registers the attempt.<span class="command">coral eval -m "description of the optimization change"</span></td><td>Twenty turns without submission ends the agent process; repeated unproductive exits are capped.</td><td>Git commit and <code>.coral/public/attempts/&lt;hash&gt;.json</code></td></tr>
<tr><td>9</td><td><span class="owner eval">CORAL grader</span></td><td>Run <code>tiny</code> for up to 300s inside Bubblewrap.</td><td>Infeasible, runtime/checker error, or gap &gt;10%: skip dev and return evidence immediately.</td><td>Stage-1 solution, feasibility result, execution log, metadata</td></tr>
<tr><td>10</td><td><span class="owner eval">CORAL grader</span></td><td>If tiny passes, evaluate the chosen median-τg dev instance for up to 3600s with staged-QTE scoring.</td><td>Failures receive zero/degraded score; feasible quality and timing inform the score when available.</td><td><code>coral_eval/stage2/*</code> and scored metadata</td></tr>
<tr><td>11</td><td><span class="owner agent">CORAL → OpenCode</span></td><td>Interrupt safely, extract <code>sessionID</code>, and resume the same conversation with score, gap, feasibility, runtime, and errors.</td><td>After five non-improvements the heartbeat requests a pivot; otherwise it reflects and continues.</td><td>Next agent log, heartbeat record, preserved conversation</td></tr>
<tr><td>12</td><td><span class="owner batch">Selection</span></td><td>After two evaluated attempts or budget exhaustion, choose the highest-scoring commit and copy <code>selected_code.py</code>.</td><td>A pending attempt JSON can exist if submission overlaps selection; this report counts it separately.</td><td><code>selected/coral_&lt;hash&gt;_code.py</code>, <code>selected_code.py</code></td></tr>
<tr><td>13</td><td><span class="owner sandbox">Final evaluator</span></td><td>Run selected code on <code>large_2</code>…<code>large_5</code>: 1 CPU, 24 GiB <code>RLIMIT_AS</code>, no candidate network, up to 3600s each.</td><td>Record pass, gap-exceeds, infeasible, checker error, or runtime error. Batch validation requires attempts, selected code, checkpoint, and four rows.</td><td><code>final_eval/*</code>, result CSV, validation JSON, progress</td></tr>
</tbody></table></div></details>
<div class="decision-grid">
  <div class="decision"><b>Case admission</b><span class="yes">YES:</span> ≥48 GiB → seed/setup.<br><span class="no">NO:</span> wait without starting.</div>
  <div class="decision"><b>Candidate launch</b><span class="yes">YES:</span> ≥56 GiB → Bubblewrap.<br><span class="no">NO:</span> memory-admission <code>runtime_error</code>; observed 16 times.</div>
  <div class="decision"><b>Tiny gate</b><span class="yes">PASS:</span> feasible and gap ≤10% → median dev.<br><span class="no">FAIL:</span> skip dev → feedback.</div>
  <div class="decision"><b>Attempt budget</b><span class="yes">&lt;2 evaluated:</span> resume session.<br><span class="no">≥2 evaluated:</span> select best → final tests.</div>
</div>
</section>

<section class="panel" id="example"><h2>Worked example: <span class="mono">brandao2016</span></h2>
<div class="example-shell"><div class="example-context"><h3>Case setup</h3>
<p>This bin-packing case used <code>large_2</code> as its median-τg dev instance. The complete case took {human_duration(example["duration_seconds"])} and produced a {example["selected_lines"]}-line selected solver.</p>
<p><b>Why it is illustrative:</b> one approach scored well, a follow-up regressed, CORAL retained the better commit, and held-out tests exposed a generalization gap on one instance.</p>
<ul><li>Finalized attempts: {example["finalized_attempts"]}</li><li>Pending records: {example["pending_attempts"]}</li><li>Selected commit: <code>{html.escape(example["selected_commit"])}</code></li><li>Final outcome: {html.escape(example["diagnosis"])}</li></ul>
<p><a href="../../coral/{html.escape(run_id)}/brandao2016/qwen3-coder-plus/coral_task/task.yaml">task.yaml</a> · <a href="../../coral/{html.escape(run_id)}/brandao2016/qwen3-coder-plus/coral_run/.coral/public/logs/">agent logs</a> · <a href="../../coral/{html.escape(run_id)}/brandao2016/qwen3-coder-plus/coral_run/.coral/public/attempts/">attempt records</a> · <a href="../../coral/{html.escape(run_id)}/brandao2016/qwen3-coder-plus/selected_code.py">selected code</a></p>
</div><div><h3>Observed attempt timeline</h3><div class="example-timeline">{example_attempts_html}</div></div></div>
<h3>Selection and held-out final tests</h3>
<p>CORAL selected the 0.981132 Best-Fit-Decreasing + MIP commit, not the later 0.0 regression. The extra pending record was never evaluated before selection and is therefore not counted as finalized.</p>
<div class="example-results">{example_final_html}</div>
<div class="callout info"><b>Interpretation:</b> the selected method generalized to three of four held-out instances. <code>large_3</code> was feasible but 15.91% from the reference, exceeding the 10% pass threshold; this is a quality failure, not infeasibility or a crash.</div>
</section>

<section class="panel"><h2>Completed-case analysis</h2>
<div class="toolbar"><input id="caseSearch" placeholder="Filter case ID"><select id="caseOutcome"><option value="">All outcomes</option><option value="all-pass">All pass</option><option value="partial-pass">Partial pass</option><option value="gap-fail">Feasible, gap fail</option><option value="runtime">All runtime errors</option><option value="no-pass">Other no-pass</option></select></div>
<div class="table-wrap"><table id="caseTable"><thead><tr><th data-sort="text">Case</th><th data-sort="number">Runtime</th><th>Attempts</th><th>Pass</th><th>Feasible</th><th>≤1% gap</th><th data-sort="number">Mean feasible gap</th><th>Mean feasible time</th><th>Outcome</th><th>Failure / diagnostic summary</th><th>Code lines</th></tr></thead><tbody>{''.join(completed_rows_html)}</tbody></table></div>
<p class="small">Mean gap and ≤1% counts are reconstructed from the per-case textual logs because the test CSV gap column is empty.</p>
</section>

<section class="panel" id="case-diagnostics"><h2>Detailed diagnostics for every touched case</h2>
<p>Expand any case to inspect its attempt trail, selected commit, all four final-test outcomes, concise root-cause interpretation, and exact saved error excerpts. Validation failures and the interrupted case are included.</p>
<div class="toolbar"><input id="diagnosticSearch" placeholder="Filter case or failure text"><select id="diagnosticState"><option value="">All touched cases</option><option value="completed">Completed</option><option value="failed">Validation failed</option><option value="interrupted">Interrupted</option></select></div>
<div id="diagnosticList" class="diagnostic-list">{diagnostics_html}</div>
</section>

<section class="grid two">
<div class="panel"><h2>Runtime-error diagnosis</h2>{runtime_category_html}
<p class="small">Categories are mutually exclusive, based on the saved test error text. Many optimizer tracebacks were truncated by the execution-output cap, so “Gurobi/optimizer exception” cannot always be narrowed further.</p></div>
<div class="panel"><h2>Slowest completed cases</h2><table><thead><tr><th>Case</th><th>Runtime</th><th>Pass</th><th>Outcome</th></tr></thead><tbody>{slowest_html}</tbody></table></div>
</section>

<section class="panel"><h2>Case-level validation failures</h2>
<p>These are pipeline-completion failures, not solver infeasibility. Each produced zero finalized attempts, no selected code, and no four-row test output.</p>
<div class="table-wrap"><table><thead><tr><th>Case</th><th>Runtime</th><th>Attempts</th><th>Root signal</th><th>Missing artifacts</th></tr></thead><tbody>{failed_rows_html}</tbody></table></div>
<h3>Interrupted case retained for resume</h3><ul>{interrupted_html}</ul>
</section>

<section class="panel"><h2>All 204 test-instance outcomes</h2>
<div class="toolbar"><input id="instanceSearch" placeholder="Filter case, instance, or error"><select id="instanceResult"><option value="">All results</option><option value="pass">Pass</option><option value="runtime_error">Runtime error</option><option value="infeasible">Infeasible</option><option value="gap_exceeds">Gap exceeds</option><option value="checker_error">Checker error</option></select></div>
<div class="table-wrap"><table id="instanceTable"><thead><tr><th>Case</th><th>Instance</th><th>Result</th><th>Feasible</th><th>Gap</th><th>Candidate time</th><th>Objective</th><th>Detail</th></tr></thead><tbody>{''.join(instance_rows_html)}</tbody></table></div>
</section>

<section class="grid two">
<div class="panel"><h2>Metric integrity</h2>
  <div class="callout info"><b>QTE cannot be computed:</b> 0/{len(test_rows)} test CSV rows contain <code>delta_time</code>. The reference-time comparison needed for QTE is absent.</div>
  <div class="callout info"><b>Official 1% quality is misleadingly zero:</b> 0/{len(test_rows)} CSV rows contain <code>gap</code>. This report reconstructs {gap_known_count} gaps from logs and finds {quality_1pct_count} at ≤1%.</div>
  <p>Dev-result coverage is {len(dev_rows)} rows. Their dominant status is <code>{html.escape(Counter(row.get("status") or "unknown" for row in dev_rows).most_common(1)[0][0] if dev_rows else "missing")}</code>; consult the attempt metadata and raw logs rather than treating that CSV as a complete optimization trace.</p>
  <p>API accounting has {len(cost_rows)} rows, {token_prompt:,} prompt tokens and {token_completion:,} completion tokens, with reported cost ${reported_cost:,.2f}. These rows do not fully price OpenCode/CORAL agent usage at the custom endpoint.</p>
</div>
<div class="panel"><h2>Preservation & reproducibility</h2>
  <ul>
    <li><a href="{html.escape(archive_rel)}">Compressed raw archive</a>: {human_bytes(archive.stat().st_size if archive.is_file() else None)}</li>
    <li>Archive SHA-256: <code>{html.escape(archive_hash)}</code></li>
    <li><a href="data/artifact_files.sha256">SHA-256 inventory</a>: {files:,} regular files</li>
    <li><a href="data/artifact_symlinks.tsv">Symlink inventory</a>: {symlinks:,} entries</li>
    <li><a href="data/analysis.json">Machine-readable analysis</a></li>
    <li><a href="data/test_results.csv">Frozen test rows</a> · <a href="data/dev_results.csv">dev rows</a> · <a href="data/api_cost.csv">API accounting rows</a></li>
    <li><a href="../../coral_batches/{html.escape(run_id)}/run.log">Batch log</a> · <a href="../../coral_batches/{html.escape(run_id)}/state.json">resume state</a> · <a href="../../coral/{html.escape(run_id)}/">all CORAL artifacts</a></li>
  </ul>
  <p class="small">The archive excludes secrets and includes the complete batch directory, complete CORAL run directory, and the three global result/accounting CSV files as they existed at stop time.</p>
</div></section>

<details class="panel"><summary>Pending manifest entries ({len(pending_ids)})</summary><p class="mono small">{pending_html}</p></details>
<footer>Generated {html.escape(generated_at)} from frozen local artifacts. Run ID: <span class="mono">{html.escape(run_id)}</span>.</footer>
</main>
<script>
function wireFilter(inputId, selectId, tableId, attr) {{
  const input=document.getElementById(inputId), select=document.getElementById(selectId), table=document.getElementById(tableId);
  function apply() {{ const q=input.value.trim().toLowerCase(), v=select.value; for (const row of table.tBodies[0].rows) {{ row.hidden=!!((q && !row.dataset.search.includes(q)) || (v && row.dataset[attr]!==v)); }} }}
  input.addEventListener('input',apply); select.addEventListener('change',apply);
}}
wireFilter('caseSearch','caseOutcome','caseTable','outcome'); wireFilter('instanceSearch','instanceResult','instanceTable','result');
function wireEntryFilter() {{
  const input=document.getElementById('diagnosticSearch'), select=document.getElementById('diagnosticState'), list=document.getElementById('diagnosticList');
  function apply() {{ const q=input.value.trim().toLowerCase(), v=select.value; for (const entry of list.children) {{ entry.hidden=!!((q && !entry.dataset.search.includes(q)) || (v && entry.dataset.state!==v)); }} }}
  input.addEventListener('input',apply); select.addEventListener('change',apply);
}}
wireEntryFilter();
for (const th of document.querySelectorAll('th[data-sort]')) {{ th.addEventListener('click',()=>{{ const table=th.closest('table'), body=table.tBodies[0], index=[...th.parentNode.children].indexOf(th), asc=th.dataset.asc!=='1'; const rows=[...body.rows]; rows.sort((a,b)=>{{ const av=a.cells[index].dataset.value??a.cells[index].innerText, bv=b.cells[index].dataset.value??b.cells[index].innerText; return (th.dataset.sort==='number' ? Number(av)-Number(bv) : av.localeCompare(bv))*(asc?1:-1); }}); rows.forEach(r=>body.appendChild(r)); th.dataset.asc=asc?'1':'0'; }}); }}
</script>
</body></html>
"""
    (report_dir / "index.html").write_text(html_text, encoding="utf-8")
    print(json.dumps({
        "report": str(report_dir / "index.html"),
        "analysis": str(data_dir / "analysis.json"),
        "completed": len(completed_ids),
        "failed": len(failed_ids),
        "interrupted": len(interrupted_ids),
        "test_rows": len(test_rows),
        "pass_rows": result_statuses.get("pass", 0),
        "feasible_rows": feasible_count,
        "quality_1pct_reconstructed": quality_1pct_count,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
