import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import e2e_target_history as target


REPO = "owner/repo"
BOT = {"login": "github-actions[bot]", "type": "Bot"}
REQUEST = {
    "baseRef": "main", "headSHA": "a" * 40, "baseSHA": "b" * 40,
    "catalogRevision": "c" * 64, "requestedGroups": [], "full": False,
}


def date(second):
    return f"2026-01-01T00:00:{second:02d}Z"


def edit(second):
    return {"event": "base_ref_changed", "created_at": date(second)}


def comment(identifier, second, body="/test e2e", user=None):
    return {"event": "commented", "created_at": date(second), "id": identifier,
            "user": user or {"login": "writer", "type": "User"}, "body": body}


class HistoryTests(unittest.TestCase):
    def test_return_to_identical_target(self):
        # The same target tuple appears in both generations. Page order and marker
        # publication time cannot prove when the original approval happened.
        pages = [[comment(1, 1), edit(3)], [edit(2), comment(2, 3), comment(3, 4)]]
        for render in (target.control.renderApprovalIntent, target.control.renderRequestMarker):
            pages.append([
                comment(10 * len(pages) + generation, 5, render(dict(REQUEST, approvalGeneration=generation)), BOT)
                for generation in (1, 2, 3, 8)
            ])
        data = target.TargetHistory(pages)
        self.assertEqual([[item["id"] for item in page] for page in data.comment_pages()],
                         [[], [3], [23], [33]])
        self.assertFalse(data.is_fresh(2))
        self.assertFalse(data.is_fresh(8))
        self.assertTrue(data.is_fresh(3))

        def run(generation):
            return {"display_title": f"x86-e2e pr=1 head={'a' * 40} approval={generation} "
                                     "generation=101 groups=- full=0 ref=main",
                    "path": ".github/workflows/build-x86-image.yaml",
                    "actor": {"login": "github-actions[bot]"},
                    "head_branch": f"x86-e2e/pr-1-a-{generation}-d-101"}

        stale, fresh = run(1), run(3)
        generations = {identifier for identifier in data.command_dates if data.is_fresh(identifier)}
        self.assertIs(target.control.latestExecutorRun([stale, fresh], 1, "a"*40, "main", freshGenerations=generations), fresh)
        self.assertIsNone(target.control.latestExecutorRun([stale], 1, "a"*40, "main", freshGenerations=generations))

    def test_no_edit_still_requires_original(self):
        data = target.TargetHistory([[comment(1, 0), comment(2, 0, "hello")]])
        self.assertTrue(data.is_fresh(1))
        for identifier in (2, 3, True, -1):
            self.assertFalse(data.is_fresh(identifier))

    def test_only_exact_bot_records_supply_event_time(self):
        def marker(second):
            return f"<!-- x86-e2e-retarget edited_at={date(second)} -->"

        data = target.TargetHistory([
            [comment(1, 3), comment(2, 4), comment(3, 8, marker(3), BOT)],
            [comment(4, 9, marker(3), BOT), comment(5, 7, marker(2), BOT),
             comment(6, 8, marker(6), dict(BOT, type="User")),
             comment(7, 8, marker(6), dict(BOT, login="other[bot]")),
             comment(8, 8, marker(6) + "\n", BOT)],
        ])
        self.assertFalse(data.is_fresh(1))
        self.assertTrue(data.is_fresh(2))

    def test_invalid_snapshot(self):
        for pages in (None, [], [None], [[{}]], [[{"event": "base_ref_changed"}]],
                      [[dict(edit(0), created_at="yesterday")]], [[comment(1, 0)] * 2],
                      [[dict(comment(1, 0), id=True)]]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                target.TargetHistory(pages)

    def test_cli_reuses_single_fetch(self):
        pages = [[comment(1, 0), edit(1)], [comment(2, 2)]]

        def cli(*args):
            output = io.StringIO()
            with patch("sys.argv", ["history", *args]), contextlib.redirect_stdout(output):
                target.main()
            return output.getvalue()

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", GITHUB_REPOSITORY=REPO), \
                patch.object(target.subprocess, "check_output", return_value=json.dumps(pages)) as fetch:
            path = Path(directory, "generations.json")
            self.assertEqual(json.loads(cli("comments", "1", "--generations-file", str(path))), [[], [pages[1][0]]])
            self.assertEqual(json.loads(path.read_text()), [2])
            fetch.assert_called_once_with(["gh", "api", "--paginate", "--slurp",
                                          f"repos/{REPO}/issues/1/timeline?per_page=100"])
            cli("validate", "1", "2")
            with self.assertRaises(ValueError):
                cli("validate", "1", "1")
            fetch.side_effect = subprocess.CalledProcessError(1, "gh")
            with self.assertRaises(subprocess.CalledProcessError):
                cli("comments", "1")


if __name__ == "__main__":
    unittest.main()
