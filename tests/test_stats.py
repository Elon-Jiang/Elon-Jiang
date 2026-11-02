import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stats", ROOT / "scripts/generate_stats.py")
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


class ContributionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "profile.json").read_text())
        self.data = {"username": "Elon-Jiang", "updated_at": "2026-10-31T10:00:00+00:00", "profile": {},
                     "repositories": [{"full_name": "other/project", "url": "https://github.com/other/project", "stars": 1200}],
                     "pull_requests": []}

    def pr(self, number, state="open", merged_at=None, draft=False):
        return {"id": number, "number": number, "repository": "other/project", "title": "Fix <edge> & [input] | case",
                "url": f"https://github.com/other/project/pull/{number}", "state": state, "draft": draft,
                "created_at": "2026-10-30T17:00:00Z", "merged_at": merged_at}

    def test_closed_unmerged_is_never_counted_as_merged(self):
        self.data["pull_requests"] = [self.pr(1, "closed"), self.pr(2), self.pr(3, "closed", "2026-10-31T02:00:00Z")]
        summary = stats.summarize(self.data)
        self.assertEqual((summary["merged"], summary["open"], summary["closed"]), (1, 1, 1))
        markdown = stats.readme(self.config, self.data, summary)
        self.assertIn("1 已合并 / 1 待合并 / 1 关闭未合并", markdown)

    def test_zero_merges_shows_explicitly_labelled_submission_chart(self):
        self.data["pull_requests"] = [self.pr(1)]
        card = stats.stats_svg(self.config, self.data, stats.summarize(self.data), "light")
        self.assertIn("近 30 天 PR 提交时间", card)
        # 17:00 UTC on Oct 30 is Oct 31 in Shanghai.
        self.assertIn("2026-10-31: 1 个 PR", card)
        self.assertNotIn("近 30 天 PR 合并时间", card)

    def test_merge_chart_uses_merge_date_instead_of_creation_date(self):
        pr = self.pr(1, "closed", "2026-10-31T02:00:00Z")
        pr["created_at"] = "2025-01-01T00:00:00Z"
        self.data["pull_requests"] = [pr]
        card = stats.stats_svg(self.config, self.data, stats.summarize(self.data), "dark")
        self.assertIn("近 30 天 PR 合并时间", card)
        self.assertIn("2026-10-31: 1 个 PR", card)

    def test_drafts_and_closed_prs_are_labelled_in_details(self):
        self.data["pull_requests"] = [self.pr(1, draft=True), self.pr(2, "closed")]
        markdown = stats.readme(self.config, self.data, stats.summarize(self.data))
        self.assertIn("| 草稿 |", markdown)
        self.assertIn("| 关闭未合并 |", markdown)
        self.assertIn("\\| case", markdown)

    def test_incomplete_or_truncated_search_fails_instead_of_showing_partial_counts(self):
        for response in [{"incomplete_results": True, "total_count": 21, "items": []},
                         {"incomplete_results": False, "total_count": 1001, "items": []}]:
            with patch.object(stats, "api_get", return_value=response):
                with self.assertRaises(RuntimeError):
                    stats.search_prs("Elon-Jiang", None)

    def test_actual_cards_are_valid_self_contained_svg_in_both_themes(self):
        data = json.loads((ROOT / "data/public-contributions.json").read_text())
        config = copy.deepcopy(self.config)
        config["intro"] = 'Text <script> & "quotes"'
        for theme in ("light", "dark"):
            hero = stats.hero_svg(config, theme)
            card = stats.stats_svg(config, data, stats.summarize(data), theme)
            for svg in (hero, card):
                ET.fromstring(svg)
                self.assertNotIn("<script>", svg)
                self.assertNotIn("{accent}", svg)
                self.assertNotIn("<foreignObject", svg)
                self.assertNotIn("@import", svg)


if __name__ == "__main__":
    unittest.main()
