import copy
import unittest

import e2e_gate_recovery as recovery


class GateRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.run = {
            "id": 42,
            "status": "completed",
            "event": "workflow_dispatch",
            "path": ".github/workflows/build-x86-image.yaml",
            "actor": {"login": "github-actions[bot]"},
            "head_repository": {"full_name": "cloudyfolks-io/fabric"},
            "head_branch": "x86-e2e/pr-39-a-1001-d-2001",
            "head_sha": "b" * 40,
            "display_title": (
                f"x86-e2e pr=39 head={'a' * 40} approval=1001 "
                "generation=2001 groups=- full=0"
            ),
        }
        self.pullRequest = {
            "state": "open",
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
        }
        self.check = {
            "id": 100,
            "name": "x86-e2e / required-gate",
            "conclusion": "action_required",
            "output": {
                "summary": (
                    "The latest authorized x86 E2E approval "
                    "is waiting for its trusted executor."
                )
            },
        }

    def api(self, path):
        if path == "pulls/39":
            return self.pullRequest
        self.assertEqual(path, f"commits/{'a' * 40}/check-runs?per_page=100")
        return {"check_runs": [self.check]}

    def find(self, run=None):
        return recovery.pendingExecutor(
            [run or self.run], "cloudyfolks-io/fabric", self.api
        )

    def testMissingHandoffFoundWithoutTrustingConclusion(self):
        for conclusion in ("success", "failure", "cancelled"):
            self.assertEqual(self.find({**self.run, "conclusion": conclusion}), 42)

    def testRejectUntrustedOrIncompleteRuns(self):
        changes = [
            {"status": "in_progress"},
            {"event": "pull_request"},
            {"path": ".github/workflows/other.yaml"},
            {"actor": {"login": "attacker"}},
            {"head_repository": {"full_name": "attacker/fabric"}},
            {"head_branch": "main"},
            {"display_title": "invalid"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.find({**self.run, **change}))

    def testRejectStaleAndClosedPullRequests(self):
        original = copy.deepcopy(self.pullRequest)
        for change in (
            {"state": "closed"},
            {"head": {"sha": "c" * 40}},
            {"base": {"sha": "c" * 40, "ref": "main"}},
            {"base": {"sha": "b" * 40, "ref": "release-next"}},
        ):
            self.pullRequest = {**original, **change}
            self.assertIsNone(self.find())

    def testDoesNotRetryAnEvaluatedDecision(self):
        for conclusion in ("success", "failure"):
            self.check["conclusion"] = conclusion
            self.assertIsNone(self.find())
        self.check["conclusion"] = "action_required"
        self.check["output"]["summary"] = "Additional deferred-group approval is required."
        self.assertIsNone(self.find())

    def testOlderWaitingCheckDoesNotOverrideNewerResult(self):
        def api(path):
            if path == "pulls/39":
                return self.pullRequest
            return {
                "check_runs": [
                    self.check,
                    {**self.check, "id": 101, "conclusion": "success"},
                ]
            }

        self.assertIsNone(
            recovery.pendingExecutor([self.run], "cloudyfolks-io/fabric", api)
        )


if __name__ == "__main__":
    unittest.main()
