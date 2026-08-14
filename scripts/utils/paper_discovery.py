"""Canonical discovery of runnable FrontierOR paper tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List


REQUIRED_PAPER_FILES = (
    "problem_description.txt",
    "instance_schema.json",
    "solution_schema.json",
)


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def is_valid_paper_dir(path: str | Path, *, validate_json: bool = False) -> bool:
    """Return whether *path* contains the minimum runnable task contract.

    Git metadata and incomplete staging directories are intentionally excluded.
    A runnable task needs a problem statement, both JSON schemas, and at least
    one instance JSON below ``instance/``.  ``validate_json`` is useful for
    preflight/tests without making routine discovery parse every instance.
    """
    paper_dir = Path(path)
    if not paper_dir.is_dir() or paper_dir.name.startswith("."):
        return False
    required = [paper_dir / name for name in REQUIRED_PAPER_FILES]
    if not all(_is_nonempty_file(item) for item in required):
        return False
    instance_dir = paper_dir / "instance"
    try:
        instances = sorted(
            item for item in instance_dir.glob("*_instance*.json")
            if _is_nonempty_file(item)
        )
    except OSError:
        return False
    if not instances:
        return False
    if not validate_json:
        return True
    try:
        for item in required[1:]:
            with item.open(encoding="utf-8") as handle:
                json.load(handle)
        for item in instances:
            with item.open(encoding="utf-8") as handle:
                json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return True


def discover_valid_papers(
    data_dir: str | Path, *, validate_json: bool = False
) -> List[str]:
    """Return a stable, sorted manifest of runnable paper IDs."""
    root = Path(data_dir)
    if not root.is_dir():
        return []
    try:
        children: Iterable[Path] = root.iterdir()
        return sorted(
            item.name
            for item in children
            if is_valid_paper_dir(item, validate_json=validate_json)
        )
    except OSError:
        return []
