import copy
import importlib.util
import json
import re
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

    def test_adopted_highlights_survive_generation_without_inflating_merges(self):
        self.data["pull_requests"] = [self.pr(809, "closed"), self.pr(810, "closed")]
        summary = stats.summarize(self.data)
        markdown = stats.readme(self.config, self.data, summary)
        self.assertEqual(summary["merged"], 0)
        self.assertIn("0 merged / 0 open / 2 closed", markdown)
        self.assertIn("### Adopted upstream", markdown)
        for item in self.config["adopted_contributions"]:
            self.assertIn(item["commit"], markdown)
            self.assertIn(item["credit"], markdown)
        self.assertEqual(markdown, stats.readme(self.config, self.data, summary))
        self.config.pop("adopted_contributions")
        self.assertNotIn("### Adopted upstream", stats.readme(self.config, self.data, summary))

    def test_closed_unmerged_is_never_counted_as_merged(self):
        self.data["pull_requests"] = [self.pr(1, "closed"), self.pr(2), self.pr(3, "closed", "2026-10-31T02:00:00Z")]
        summary = stats.summarize(self.data)
        self.assertEqual((summary["merged"], summary["open"], summary["closed"]), (1, 1, 1))
        markdown = stats.readme(self.config, self.data, summary)
        self.assertIn("1 merged / 1 open / 1 closed", markdown)

    def test_zero_merges_shows_explicitly_labelled_submission_chart(self):
        self.data["pull_requests"] = [self.pr(1)]
        card = stats.stats_svg(self.config, self.data, stats.summarize(self.data), "light")
        self.assertIn("Opened PRs, last 30 days", card)
        # 17:00 UTC on Oct 30 is Oct 31 in Shanghai.
        self.assertIn("2026-10-31: 1 PR", card)
        self.assertNotIn("Merged PRs, last 30 days", card)

    def test_merge_chart_uses_merge_date_instead_of_creation_date(self):
        pr = self.pr(1, "closed", "2026-10-31T02:00:00Z")
        pr["created_at"] = "2025-01-01T00:00:00Z"
        self.data["pull_requests"] = [pr]
        card = stats.stats_svg(self.config, self.data, stats.summarize(self.data), "dark")
        self.assertIn("Merged PRs, last 30 days", card)
        self.assertIn("2026-10-31: 1 PR", card)

    def test_drafts_and_closed_prs_are_labelled_in_details(self):
        self.data["pull_requests"] = [self.pr(1, draft=True), self.pr(2, "closed")]
        markdown = stats.readme(self.config, self.data, stats.summarize(self.data))
        self.assertIn("| Draft |", markdown)
        self.assertIn("| Closed |", markdown)
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

    def repo_pr(self, number, repo, merged):
        pr = self.pr(number, "closed" if merged else "open", "2026-10-31T02:00:00Z" if merged else None)
        pr["repository"] = repo
        pr["url"] = f"https://github.com/{repo}/pull/{number}"
        return pr

    def test_featured_repositories_rank_by_stars_and_require_a_merge(self):
        self.data["repositories"] = [
            {"full_name": "small/many-merges", "url": "https://github.com/small/many-merges", "stars": 900},
            {"full_name": "big/one-merge", "url": "https://github.com/big/one-merge", "stars": 30000},
            {"full_name": "huge/open-only", "url": "https://github.com/huge/open-only", "stars": 100000},
        ]
        self.data["pull_requests"] = [self.repo_pr(1, "small/many-merges", True), self.repo_pr(2, "small/many-merges", True),
                                      self.repo_pr(3, "big/one-merge", True), self.repo_pr(4, "huge/open-only", False)]
        summary = stats.summarize(self.data)
        self.assertEqual([r["full_name"] for r in summary["featured"]], ["big/one-merge", "small/many-merges"])
        self.assertEqual(len(summary["repos"]), 3)
        card = stats.stats_svg(self.config, self.data, summary, "light")
        self.assertNotIn("huge/open-only", card)
        self.assertLess(card.index("big/one-merge"), card.index("small/many-merges"))

    def test_without_any_merge_all_repositories_are_shown_by_stars(self):
        self.data["pull_requests"] = [self.pr(1)]
        summary = stats.summarize(self.data)
        self.assertEqual([r["full_name"] for r in summary["featured"]], ["other/project"])

    def test_generated_text_is_english_only(self):
        data = json.loads((ROOT / "data/public-contributions.json").read_text())
        summary = stats.summarize(data)
        outputs = [stats.hero_svg(self.config, "light"), stats.stats_svg(self.config, data, summary, "light"),
                   stats.preview(self.config, data, summary)]
        readme = stats.readme(self.config, data, summary)
        # PR titles come from upstream projects, so check the README without the PR table rows.
        outputs.append("\n".join(l for l in readme.splitlines() if not l.startswith("| [")))
        for output in outputs:
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", output))


if __name__ == "__main__":
    unittest.main()
