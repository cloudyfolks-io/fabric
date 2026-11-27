"""Exercise the serialized gate writer with local GitHub API fixtures."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import e2e_control as control
import e2e_gate_recovery as recovery
import e2e_invalidate as invalidate
import e2e_selector as selector
import e2e_target_history as history


ROOT = Path(__file__).resolve().parents[1]
GATE = (ROOT / ".github/workflows/x86-e2e-gate.yaml").read_text()
MUTATE = textwrap.dedent(GATE.split("name: Reconcile the fixed gate from durable state\n        run: |\n")[1])
WAITING = "The latest authorized x86 E2E approval is waiting for its trusted executor."


class GateReplayTest(unittest.TestCase):
    def test_promoted_approval_stays_bound_to_one_completed_result(self):
        start = MUTATE.index("stillCurrentRecovery() {")
        guard = MUTATE[start:MUTATE.index("\n}", start) + 2]
        for snapshot in ("latest-run.json", "pre-write-latest-run.json"):
            self.assertIn(f"stillCurrentRecovery {snapshot} || exit 0", MUTATE)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "run.json")
            for action in ("approval", "reservation"):
                for run_id, attempt, conclusion, allowed in (
                    (12, 1, "failure", True), (12, 1, "success", True), (12, 1, "cancelled", False),
                    (13, 1, "success", False), (12, 2, "success", False),
                ):
                    path.write_text(json.dumps(dict(id=run_id, run_attempt=attempt, status="completed", conclusion=conclusion)))
                    result = subprocess.run(["bash", "-eu", "-c", guard + '\nstillCurrentRecovery "$1"', "check", str(path)],
                                            env=dict(os.environ, RECOVERY_ACTION=action, RUN_ACTION="completed",
                                                     RUN_ID="12", RUN_ATTEMPT="1"))
                    self.assertEqual(result.returncode == 0, allowed)

    def test_failed_recorder_then_identical_command(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(Path(directory))

    def exercise(self, work):
        head, base = "a" * 40, "b" * 40
        pr = {"number": 7, "state": "open", "head": {"sha": head}, "base": {"sha": base, "ref": "main"}}
        catalog = json.loads((ROOT / ".github/e2e-selection.json").read_text())
        revision = selector.catalogRevision(catalog)
        bot = {"login": "github-actions[bot]", "type": "Bot"}
        timeline = [[]]

        def comment(identifier, body, when, user):
            entry = {"event": "commented", "id": identifier, "body": body, "created_at": when, "user": user}
            timeline[0].append(entry)
            return entry

        def authorize(identifier, when):
            entry = comment(identifier, "/test e2e", when, {"login": "writer", "type": "User"})
            decision = control.decideDispatch({"comment": entry, "action": "created", "issue": {"pull_request": {}}},
                                              "write", pr, head, catalog["groups"], revision)
            self.assertTrue(decision["accepted"])
            return control.mergeApprovedRequests(decision, [])

        old = authorize(10, "2025-01-02T00:00:00Z")
        key = old["requestKey"]
        comment(11, control.renderRequestMarker(old), "2025-01-02T00:01:00Z", bot)
        target = f"x86-e2e-target base={base} ref=main"
        check = {"id": 101, "head_sha": head, "app": {"slug": "github-actions"},
                 "name": "x86-e2e / required-gate", "external_id": f"x86-e2e-pr-7-{head}",
                 "conclusion": "success", "output": {"title": "gate", "summary": "Passed",
                 "text": f"x86-e2e-gate request={key} approval=10\n{target}"}}

        def run(generation):
            return {"id": generation, "display_title": f"x86-e2e pr=7 head={head} approval={generation} "
                    "generation=1 groups=- full=0 ref=main", "head_branch": f"x86-e2e/pr-7-a-{generation}-d-1",
                    "event": "workflow_dispatch", "head_repository": {"full_name": "org/repo"},
                    "head_sha": base, "actor": bot, "path": ".github/workflows/build-x86-image.yaml",
                    "status": "completed", "conclusion": "success", "run_attempt": 1}

        runs = [run(10)]

        def api(path, *args, **kwargs):
            if path.startswith("commits/"):
                return [{"check_runs": [check]}]
            if path == "pulls/7":
                return pr
            if path.startswith("issues/7/timeline"):
                return timeline
            if path.startswith("issues/7/comments?"):
                return timeline
            if path == "issues/7/comments":
                return comment(100 + len(timeline[0]), kwargs["body"]["body"], "2025-01-03T00:00:00Z", bot)
            self.assertEqual(path, "check-runs/101")
            check.update(kwargs["body"])

        def duplicate(request, recorded=False):
            snapshot = history.TargetHistory(timeline, [check])
            generations = [g for g in snapshot.command_dates if snapshot.is_fresh(g)]
            return control.duplicateExecutor(runs, request, snapshot.comment_pages(), generations,
                                             currentCheck=check, recorded=recorded)

        repeated = authorize(12, "2025-01-02T00:02:00Z")
        with patch.dict(check["output"], text=f"x86-e2e-gate request={key} approval=12\n{target}"):
            self.assertIsNone(duplicate(repeated))
        for status, conclusion in [("in_progress", None), ("completed", "failure"), ("completed", "success")]:
            runs[0].update(status=status, conclusion=conclusion)
            self.assertEqual(duplicate(repeated), runs[0])
        runs[0]["conclusion"] = "cancelled"
        self.assertIsNone(duplicate(repeated))
        self.assertIsNone(duplicate(repeated, recorded=True))
        self.assertEqual(duplicate(old, recorded=True), runs[0])
        runs[0]["conclusion"] = "success"
        with patch.dict(os.environ, RECORDER_RESULT="failure"), patch.object(invalidate, "api", api):
            invalidate.invalidate("7", head, "2025-01-01T00:00:00Z")
        self.assertEqual(check["conclusion"], "action_required")
        self.assertIsNone(duplicate(old))
        self.assertIsNone(recovery.pendingExecutor(runs, "org/repo", api))

        # The old tuple and request key match even after authorization is renewed.
        fresh = authorize(20, "2025-01-03T00:00:01Z")
        self.assertEqual(fresh["requestKey"], key)
        comment(21, control.renderApprovalIntent(old), "2025-01-03T00:00:02Z", bot)
        (work / "hack").symlink_to(ROOT / "hack")
        (work / "catalog.json").write_text(json.dumps(catalog))
        (work / "pr.json").write_text(json.dumps(pr))
        (work / "event.json").write_text(json.dumps({"repository": {"default_branch": "main"}}))
        gh = work / "gh"
        gh.write_text("""#!/bin/bash
set -eu
case "$*" in
  *'--method PATCH '*) cp check-run-update.json patch.json ;;
  *'/check-runs?'*)
    if [[ -f late-checks.json && -s write-pull-request.json ]]; then cat late-checks.json; else cat checks.json; fi ;;
  *'/pulls/7') cat pr.json ;;
  *'/timeline?'*) cat timeline.json ;;
  *'/contents/'*) base64 < catalog.json ;;
  *'/workflows/x86-e2e-gate.yaml --jq .id') echo 80 ;;
  *'/workflows/80/runs?'*) echo '[{"workflow_runs":[{"id":81,"workflow_id":80,"event":"schedule",
    "path":".github/workflows/x86-e2e-gate.yaml","head_repository":{"full_name":"org/repo"},"head_branch":"main"}]}]' ;;
  *'/artifacts?'*) cat artifacts.json ;;
  *'/runs?'*) cat runs.json ;;
  *'/actions/runs/'*) cat run.json ;;
  *'/comments '*|*'/dispatches '*) : ;;
  *) echo "Unexpected fixture request: $*" >&2; exit 1 ;;
esac
""")
        gh.chmod(0o755)

        def ready(generation):
            dispatcher = (ROOT / ".github/workflows/x86-e2e-dispatcher.yaml").read_text()
            start = dispatcher.index('          expectedMarker="')
            script = dispatcher[start:dispatcher.index("          approvalReady=false", start)] + "\ncheckApproval"
            (work / "checks.json").write_text(json.dumps(api("commits/")))
            env = dict(os.environ, PATH=f"{work}:{os.environ['PATH']}", GITHUB_REPOSITORY="org/repo",
                       externalId=check["external_id"], REQUEST_KEY=key, HEAD_SHA=head, BASE_SHA=base,
                       BASE_REF="main", APPROVAL_GENERATION=str(generation))
            return subprocess.check_output(["bash", "-eu", "-c", script], cwd=work, env=env, text=True).strip()

        def mutate(action, generation, stale=False, cutoff_changed=False, unchanged=False):
            (work / "write-pull-request.json").unlink(missing_ok=True)
            (work / "checks.json").write_text(json.dumps(api("commits/")))
            (work / "timeline.json").write_text(json.dumps(timeline))
            (work / "runs.json").write_text(json.dumps([{"workflow_runs": runs}]))
            (work / "run.json").write_text(json.dumps(run(generation)))
            env = dict(os.environ, PATH=f"{work}:{os.environ['PATH']}", PYTHONPATH=str(ROOT / "hack"),
                       RUN_ACTION=action, RUN_EVENT="workflow_dispatch",
                       RUN_ID=str(generation) if action == "completed" else "0",
                       RUN_ATTEMPT="1" if action == "completed" else "0",
                       GITHUB_REPOSITORY="org/repo", GITHUB_EVENT_PATH=str(work / "event.json"), HEAD_SHA=head,
                       BASE_SHA=base, BASE_REF="main", PR_NUMBER="7", APPROVAL_GENERATION=str(generation),
                       SOURCE_APPROVAL_GENERATION=str(generation), SOURCE_FULL="false", SOURCE_REQUESTED_GROUPS="[]",
                       FULL="false", REQUESTED_GROUPS="[]", CATALOG_REVISION=revision, TRUSTED_REF=base,
                       STATUS="completed", CONCLUSION="success" if action == "completed" else "action_required",
                       SUMMARY="Passed" if action == "completed" else WAITING, RUN_REQUEST_KEY=key, RUN_URL="url")
            result = subprocess.run(["bash", "-c", MUTATE], cwd=work, env=env, capture_output=True, text=True)
            written = work / "patch.json"
            if cutoff_changed:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("discarding this mutation", result.stdout)
                self.assertFalse(written.exists())
            elif stale:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(written.exists())
            elif unchanged:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(written.exists())
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                check.update(json.loads(written.read_text()))
                written.unlink()

        for action in ("reservation", "approval", "completed"):
            mutate(action, 10, stale=True)
        mutate("reservation", 12, stale=True)
        self.assertEqual(ready(10), "false")
        mutate("reservation", 20)
        self.assertEqual(check["conclusion"], "action_required")
        comment(22, (work / "approval-intent.txt").read_text(), "2025-01-03T00:00:03Z", bot)
        mutate("approval", 20)
        self.assertIn(f"request={key} approval=20\n", check["output"]["text"])
        comment(23, (work / "request-marker.txt").read_text(), "2025-01-03T00:00:04Z", bot)
        self.assertEqual(ready(10), "false")
        self.assertEqual(ready(20), "true")
        self.assertIsNone(duplicate(fresh, recorded=True))
        mutate("completed", 10, stale=True)
        self.assertIsNone(recovery.pendingExecutor(runs, "org/repo", api))
        runs.insert(0, run(20))
        self.assertEqual(recovery.pendingExecutor(runs, "org/repo", api), 20)
        mutate("completed", 20)
        self.assertEqual(check["conclusion"], "success")
        self.assertEqual(duplicate(fresh), runs[0])
        mutate("approval", 10, stale=True)
        mutate("completed", 10, stale=True)

        # Deliver fresh invalidation evidence only at the last read before PATCH.
        invalidated = {**check, "conclusion": "action_required", "output": {**check["output"],
                       "summary": control.invalidationSummary + "\nx86-e2e-invalidated-at 2025-01-04T00:00:00Z"}}
        (work / "late-checks.json").write_text(json.dumps([{"check_runs": [invalidated]}]))
        mutate("completed", 20, cutoff_changed=True)

        (work / "late-checks.json").unlink()
        previous = fresh
        for generation, has_artifact in [(30, False), (40, True)]:
            with self.subTest(prior_decision_artifact=has_artifact):
                cancelled = runs[0]
                cancelled["conclusion"] = "cancelled"
                artifacts = [{"id": 91, "expired": False,
                              "name": f"x86-e2e-gate-decision-{cancelled['id']}-1"}] if has_artifact else []
                (work / "artifacts.json").write_text(json.dumps({"artifacts": artifacts}))
                self.assertEqual(duplicate(previous, recorded=True), cancelled)
                mutate("approval", previous["approvalGeneration"], unchanged=True)
                command = authorize(generation, "2025-01-05T00:00:00Z")
                self.assertIsNone(duplicate(command))
                mutate("reservation", generation)
                comment(generation + 1, (work / "approval-intent.txt").read_text(), "2025-01-05T00:01:00Z", bot)
                mutate("approval", generation)
                comment(generation + 2, (work / "request-marker.txt").read_text(), "2025-01-05T00:02:00Z", bot)
                self.assertEqual(ready(generation), "true")
                self.assertIsNone(duplicate(command, recorded=True))
                runs.insert(0, run(generation))
                previous = command
