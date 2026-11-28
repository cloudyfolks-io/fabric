import copy
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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
                "generation=2001 groups=- full=0 ref=main"
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
            "external_id": f"x86-e2e-pr-39-{'a' * 40}",
            "head_sha": "a" * 40,
            "app": {"slug": "github-actions"},
            "conclusion": "action_required",
            "output": {
                "text": f"x86-e2e-gate request={'c' * 64} approval=1001\nx86-e2e-target base={'b' * 40} ref=main",
                "summary": (
                    "The latest authorized x86 E2E approval "
                    "is waiting for its trusted executor."
                )
            },
        }

        self.timeline = [[{"event": "commented", "id": 1001, "user": {"login": "writer", "type": "User"},
                           "created_at": "2026-11-01T09:00:00Z", "body": "/test e2e"}]]

    def api(self, path, paginate=False):
        if path == "pulls/39":
            return self.pullRequest
        if path == "issues/39/timeline?per_page=100":
            self.assertTrue(paginate)
            return self.timeline
        self.assertEqual(path, f"commits/{'a' * 40}/check-runs?check_name=x86-e2e%20%2F%20required-gate&filter=all&per_page=100")
        self.assertTrue(paginate)
        return [{"check_runs": [{**self.check, "external_id": "another-pr"}]}, {"check_runs": [self.check]}]

    def find(self, run=None):
        return recovery.pendingExecutor(
            [run or self.run], "cloudyfolks-io/fabric", self.api
        )

    def testMissingHandoffFoundWithoutTrustingConclusion(self):
        for conclusion in ("success", "failure", "cancelled"):
            self.assertEqual(self.find({**self.run, "conclusion": conclusion}), 42)

    def testRecoveredRunRequiresFreshOriginalApproval(self):
        self.timeline.append([{"event": "base_ref_changed", "created_at": "2026-11-01T09:00:01Z"}])
        self.assertIsNone(self.find())

    def testScheduleRecoversOnlyIndependentExecutorAlongsideStaleTarget(self):
        repository = self.run["head_repository"]["full_name"]
        head = self.pullRequest["head"]["sha"]
        staleRun = {
            **self.run, "id": 43, "head_branch": "x86-e2e/pr-40-a-1002-d-2001",
            "display_title": f"x86-e2e pr=40 head={head} approval=1002 generation=2001 groups=- full=0 ref=main",
        }
        staleWaiting = copy.deepcopy(self.check)
        staleWaiting["id"] = 102
        staleWaiting["external_id"] = f"x86-e2e-pr-40-{head}"
        staleWaiting["output"]["text"] = staleWaiting["output"]["text"].replace("approval=1001", "approval=1002")
        legacy = {**staleWaiting, "id": 99, "conclusion": "success", "output": {"text": ""}}

        def gh(command, text):
            path = command[-1].removeprefix(f"repos/{repository}/")
            if path == "pulls?state=open&per_page=100":
                response = [[{**self.pullRequest, "number": number} for number in (40, 39)]]
            elif path == "actions/workflows/build-x86-image.yaml/runs?event=workflow_dispatch&status=completed&per_page=100":
                response = {"workflow_runs": runs}
            elif path.startswith("commits/"):
                response = [{"check_runs": [legacy, staleWaiting, self.check]}]
            elif path == "pulls/40":
                response = self.pullRequest
            elif path == "issues/40/timeline?per_page=100":
                response = [[{**self.timeline[0][0], "id": 1002}]]
            else:
                response = self.api(path, paginate="--paginate" in command)
            return json.dumps(response)

        for runs, expected in (([staleRun, self.run], ["runId=42"]), ([staleRun], [])):
            with self.subTest(expected=expected), tempfile.NamedTemporaryFile(mode="r") as output:
                with patch.dict(os.environ, GITHUB_REPOSITORY=repository, GITHUB_OUTPUT=output.name), \
                     patch.object(subprocess, "check_output", gh):
                    recovery.main()
                self.assertEqual(output.read().splitlines(), ["stalePR=40", f"staleHead={head}", *expected])

    def testRejectUntrustedOrIncompleteRuns(self):
        changes = [
            {"status": "in_progress"},
            {"event": "pull_request"},
            {"path": ".github/workflows/other.yaml"},
            {"actor": {"login": "attacker"}},
            {"head_repository": {"full_name": "attacker/fabric"}},
            {"head_branch": "main"},
            {"display_title": "invalid"},
            {"display_title": self.run["display_title"].split(" ref=")[0]},
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
            {"base": {"sha": "b" * 40, "ref": "release-1.13"}},
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

    def testLegacyGreenDiscoveredUntilInvalidated(self):
        pulls = [{"number": 39, **self.pullRequest}]
        self.check["conclusion"] = "success"
        self.assertIsNone(recovery.staleTarget(pulls, self.api))
        self.timeline.append([{"event": "base_ref_changed", "created_at": "2026-11-01T09:00:01Z"}])
        self.assertEqual(recovery.staleTarget(pulls, self.api), (39, "a" * 40))
        self.check["output"]["text"] = ""
        self.assertEqual(recovery.staleTarget(pulls, self.api), (39, "a" * 40))
        self.check["conclusion"] = "action_required"
        self.assertIsNone(recovery.staleTarget(pulls, self.api))

    def testUnreadableEvidenceSelectsInvalidationButNeverRecovery(self):
        def api(path, paginate=False):
            if "timeline" in path:
                raise subprocess.CalledProcessError(1, "gh")
            return self.api(path, paginate)

        self.assertIsNone(recovery.pendingExecutor([self.run], "cloudyfolks-io/fabric", api))
        self.check["conclusion"] = "success"
        pulls = [{"number": 39, **self.pullRequest}]
        self.assertEqual(recovery.staleTarget(pulls, api), (39, self.check["head_sha"]))
        self.timeline = [[{"event": "commented"}]]
        self.assertEqual(recovery.staleTarget(pulls, self.api), (39, self.check["head_sha"]))
    def testWaitingCheckMustIdentifyOriginalTargetAndGeneration(self):
        text = self.check["output"]["text"]
        for stale in ("", text.splitlines()[0], text.replace("main", "release-1.13"),
                      text.replace("approval=1001", "approval=1002")):
            self.check["output"]["text"] = stale
            self.assertIsNone(self.find())

    def testOlderWaitingCheckDoesNotOverrideNewerResult(self):
        def api(path, paginate=False):
            if path == "pulls/39":
                return self.pullRequest
            return [{
                "check_runs": [
                    self.check,
                    {**self.check, "id": 101, "conclusion": "success"},
                ]
            }]

        self.assertIsNone(
            recovery.pendingExecutor([self.run], "cloudyfolks-io/fabric", api)
        )


if __name__ == "__main__":
    unittest.main()
