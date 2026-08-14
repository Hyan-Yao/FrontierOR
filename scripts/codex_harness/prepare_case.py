#!/usr/bin/env python3
"""Prepare a leak-minimized FrontierOR workspace for one Codex harness run."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "frontier-or"
DEFAULT_WORKSPACE_ROOT = Path("/tmp/frontieror_codex_harness")

PUBLIC_FILES = (
    "problem_description.txt",
    "instance_schema.json",
    "solution_schema.json",
    "solution_logger.py",
)

HARNESS_INSTRUCTIONS = """\
# FrontierOR Codex Harness Task

Work only inside this directory. Do not inspect parent directories, other
repositories, benchmark instances, reference solutions, mathematical
formulations, feasibility checkers, or reference solver code. Do not use the
internet. Those artifacts are intentionally hidden for benchmark integrity.

Read `problem_description.txt`, `instance_schema.json`, and
`solution_schema.json`, then implement the requested solver as `code.py`.
The original benchmark asks a chat model to return one Python code block. In
this Codex-harness variant, the equivalent deliverable is the file `code.py`.

You may inspect `solution_logger.py`, run static checks, and create your own
small synthetic inputs from the schemas. Do not request or search for real
benchmark inputs. Before finishing, ensure `code.py` is syntactically valid,
implements every required CLI flag, respects `--time_limit`, and always writes
a schema-conforming JSON solution when it has a feasible incumbent.

Do not merely explain an approach. Finish the implementation in `code.py`.
"""


def load_task_specification() -> str:
    sys.path.insert(0, str(REPO_ROOT))
    import one_shot_eval  # Imported lazily so this script has one source of truth.

    text = one_shot_eval.TASK_SPECIFICATION
    return text.replace(
        "Output only the Python code, enclosed in a single ```python ... ``` block.",
        "Write the complete implementation to `code.py` in the current directory.",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paper_id")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    args = parser.parse_args()

    source = DATA_ROOT / args.paper_id
    if not source.is_dir():
        parser.error(f"unknown paper_id: {args.paper_id}")

    workspace = args.workspace_root / args.run_id / args.paper_id
    if workspace.exists():
        parser.error(
            f"workspace already exists: {workspace}; choose a fresh --run-id "
            "to preserve run independence"
        )
    workspace.mkdir(parents=True)

    for name in PUBLIC_FILES:
        src = source / name
        if not src.is_file():
            parser.error(f"missing public task file: {src}")
        shutil.copy2(src, workspace / name)

    task = HARNESS_INSTRUCTIONS + "\n\n" + load_task_specification()
    (workspace / "TASK.md").write_text(task, encoding="utf-8")

    data_commit = "unknown"
    head = DATA_ROOT / ".git" / "HEAD"
    if head.exists():
        import subprocess

        result = subprocess.run(
            ["git", "-C", str(DATA_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            data_commit = result.stdout.strip()

    manifest = {
        "paper_id": args.paper_id,
        "run_id": args.run_id,
        "model": args.model,
        "reasoning_effort": "xhigh",
        "data_commit": data_commit,
        "visible_files": list(PUBLIC_FILES) + ["TASK.md"],
        "hidden_local_evaluator": True,
    }
    (workspace / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
