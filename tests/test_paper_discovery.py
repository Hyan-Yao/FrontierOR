from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import one_shot_eval
from scripts.utils.paper_discovery import discover_valid_papers, is_valid_paper_dir
from test_time_self_evolution.scoring.building_blocks import pick_median_tau_g_instance


ROOT = Path(__file__).resolve().parents[1]


def create_paper(root: Path, name: str, *, complete: bool = True) -> Path:
    paper = root / name
    paper.mkdir(parents=True)
    if complete:
        (paper / "problem_description.txt").write_text("problem\n", encoding="utf-8")
        (paper / "instance_schema.json").write_text("{}\n", encoding="utf-8")
        (paper / "solution_schema.json").write_text("{}\n", encoding="utf-8")
        (paper / "instance").mkdir()
        (paper / "instance" / "tiny_instance.json").write_text("{}\n", encoding="utf-8")
    return paper


class PaperDiscoveryTests(unittest.TestCase):
    def test_excludes_git_and_incomplete_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_paper(root, "valid")
            create_paper(root, "incomplete", complete=False)
            create_paper(root, ".git")
            self.assertEqual(discover_valid_papers(root), ["valid"])

    def test_optional_json_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = create_paper(root, "bad-json")
            (paper / "instance_schema.json").write_text("not-json", encoding="utf-8")
            self.assertTrue(is_valid_paper_dir(paper))
            self.assertFalse(is_valid_paper_dir(paper, validate_json=True))
            self.assertEqual(discover_valid_papers(root), ["bad-json"])
            self.assertEqual(discover_valid_papers(root, validate_json=True), [])

    def test_real_manifest_has_exactly_180_sorted_cases(self):
        papers = discover_valid_papers(ROOT / "frontier-or")
        self.assertEqual(len(papers), 180)
        self.assertEqual(papers, sorted(set(papers)))
        self.assertNotIn(".git", papers)
        one_shot_eval.validate_paper_directions(papers)
        self.assertTrue(all(pick_median_tau_g_instance(paper) for paper in papers))

    def test_missing_tau_uses_median_instance_number_fallback(self):
        self.assertEqual(pick_median_tau_g_instance("adulyasak2015"), "large_3")


if __name__ == "__main__":
    unittest.main()
