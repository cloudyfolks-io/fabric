import copy
import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
WORKFLOW = (WORKFLOWS / "x86-e2e-gate.yaml").read_text()
HEAD = "1" * 40
BASE = "2" * 40
MARKER = f"x86-e2e-gate request={'a' * 64} approval=123"
TARGET = f"x86-e2e-target base={BASE} ref=release-1.2"
EDIT = {"event": "base_ref_changed", "created_at": "2026-11-20T08:00:00Z"}
COMMAND = {"event": "commented", "id": 123, "body": "/test e2e", "created_at": "2026-11-20T08:00:01Z",
           "user": {"login": "writer", "type": "User"}}
RECORDED = dict(COMMAND, id=124, user={"login": "github-actions[bot]", "type": "Bot"},
                body=f"<!-- x86-e2e-retarget edited_at={EDIT['created_at']} -->", created_at="2026-11-20T08:00:05Z")


class RetargetTest(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        anchor = WORKFLOW.index("&reconcile_target")
        self.handler = textwrap.dedent(re.search(
            r"^        run: \|\n((?:          [^\n]*\n|\n)+)", WORKFLOW[anchor:], re.M
        )[1])
        self.pr = {"number": 7, "state": "open", "head": {"sha": HEAD},
                   "base": {"ref": "release-1.2", "sha": BASE}}
        self.check = {
            "id": 17, "name": "x86-e2e / required-gate", "external_id": f"x86-e2e-pr-7-{HEAD}",
            "app": {"slug": "github-actions"}, "head_sha": HEAD, "conclusion": "success",
            "output": {"title": "x86 E2E gate", "text": MARKER, "summary": "Passed"},
        }

    def run_handler(self, checks, live=None, timeline=None, edited_at=EDIT["created_at"], recorder_result="success"):
        self.api.reset_mock()

        def respond(path, *args, **kwargs):
            if path.startswith("commits/"):
                self.assertIn(f"commits/{HEAD}/check-runs?", path)
                self.assertIn("filter=all", path)
                self.assertEqual(args, ("--paginate", "--slurp"))
                return [{"check_runs": []}, {"check_runs": checks}]
            if path == "pulls/7":
                return live or self.pr
            if path == "issues/7/timeline?per_page=100":
                self.assertEqual(args, ("--paginate", "--slurp"))
                if isinstance(timeline, Exception):
                    raise timeline
                self.assertIsNotNone(timeline)
                return timeline
            self.assertEqual((path, args), ("check-runs/17", ("--method", "PATCH", "--input", "-")))
            return {}

        self.api.side_effect = respond
        env = {"PR_NUMBER": "7", "HEAD_SHA": HEAD, "GITHUB_REPOSITORY": "org/repo"}
        if edited_at is not None:
            env["EDITED_AT"] = edited_at
            env["RECORDER_RESULT"] = recorder_result

        def gh(command, *, input, text):
            self.assertEqual(command[:2], ["gh", "api"])
            self.assertTrue(command[2].startswith("repos/org/repo/"))
            response = self.api(command[2].removeprefix("repos/org/repo/"), *command[3:],
                                body=json.loads(input) if input is not None else None)
            return json.dumps(response)

        with patch.dict(os.environ, env, clear=True), patch.object(subprocess, "check_output", gh):
            exec(self.handler, {"__name__": "__main__"})
        return [call for call in self.api.call_args_list if "PATCH" in call.args]

    def test_only_base_edits_take_the_retarget_lock(self):
        self.assertFalse((WORKFLOWS / "x86-e2e-retarget.yaml").exists())
        self.assertIn("  pull_request_target:\n    types: [edited]", WORKFLOW)
        sections = re.split(r"^  ([\w-]+):\n", WORKFLOW.split("\njobs:\n", 1)[1], flags=re.M)
        jobs = dict(zip(sections[1::2], sections[2::2]))
        retarget = jobs["retarget"]
        recorder = jobs["record-retarget"]
        # The default single pending slot lets a later writer replace a queued failed-recorder invalidation.
        for job in jobs.values():
            if "group: x86-e2e-required-gate-" in job:
                self.assertRegex(job, r"group: x86-e2e-required-gate-[^\n]+\n      cancel-in-progress: false\n      queue: max\n")
        self.assertIn("    needs: record-retarget\n", retarget)
        self.assertIn("github.event_name == 'pull_request_target' && github.event.changes.base.ref.from != ''", recorder)
        self.assertNotIn("uses:", recorder)
        self.assertEqual(re.findall(r"^      ([\w-]+: (?:read|write))$", recorder, re.M), ["issues: write"])
        self.assertIn("EDITED_AT: ${{ github.event.pull_request.updated_at }}", recorder)
        self.assertIn("    if: always() && github.event_name == 'pull_request_target' && github.event.action == 'edited' && "
                      "github.event.changes.base.ref.from != ''\n", retarget)
        self.assertIn("      RECORDER_RESULT: ${{ needs.record-retarget.result }}\n", retarget)
        self.assertIn("group: x86-e2e-required-gate-${{ github.event.pull_request.number }}"
                      "-${{ github.event.pull_request.head.sha }}", retarget)
        self.assertIn("cancel-in-progress: false", retarget)
        self.assertNotIn("uses:", retarget)
        self.assertEqual(set(re.findall(r"^      ([\w-]+: (?:read|write))$", retarget, re.M)),
                         {"pull-requests: read", "checks: write"})
        for variable, field in [("EDITED_AT", "updated_at"), ("PR_NUMBER", "number"), ("HEAD_SHA", "head.sha")]:
            self.assertIn(f"{variable}: ${{{{ github.event.pull_request.{field} }}}}", retarget)
        self.assertIn("github.event_name == 'schedule'", jobs["discover"].split("    runs-on:")[0])
        self.assertIn("github.event_name == 'workflow_dispatch'", jobs["approval"].split("    outputs:")[0])
        gate_guard = jobs["gate"].split("    outputs:")[0]
        for event in ("schedule", "workflow_dispatch", "workflow_run"):
            self.assertIn(f"github.event_name == '{event}'", gate_guard)
        self.assertNotIn("pull_request_target", gate_guard.replace("github.event_name != 'pull_request_target'", ""))
        mutate = jobs["mutate"]
        self.assertIn("needs.gate.outputs.mutate == 'true' || needs.approval.outputs.mutate == 'true'", mutate)
        self.assertIn("cancel-in-progress: false", mutate)
        self.assertNotIn("EDITED_AT:", mutate)
        self.assertEqual(retarget.split("    steps:\n", 1)[1].splitlines()[0], "      - &reconcile_target")
        self.assertEqual(mutate.split("    steps:\n", 1)[1].splitlines()[0], "      - *reconcile_target")
        for variable, field in [("PR_NUMBER", "prNumber"), ("HEAD_SHA", "headSHA")]:
            source = "needs.gate.outputs.mutate == 'true' && needs.gate.outputs." + field
            value = "${{ " + source + " || needs.approval.outputs." + field + " }}"
            self.assertIn(f"      {variable}: {value}\n", mutate)
            self.assertIn(value, re.search(r"^      group: x86-e2e-required-gate-(.*)$", mutate, re.M)[1])

    def test_legacy_and_stale_bindings_block_without_rewriting_markers(self):
        for lines in [[], [TARGET.replace("release-1.2", "main")],
                      [TARGET.replace(BASE, HEAD)], [TARGET, TARGET]]:
            with self.subTest(lines=lines):
                text = "\n".join([MARKER, *lines, "other metadata"])
                self.check["output"]["text"] = text
                patch, = self.run_handler([self.check])
                self.assertEqual(patch.args, ("check-runs/17", "--method", "PATCH", "--input", "-"))
                body = patch.kwargs["body"]
                self.assertEqual(set(body), {"status", "conclusion", "output"})
                self.assertEqual((body["status"], body["conclusion"]), ("completed", "action_required"))
                self.assertEqual(body["output"]["text"], text)
                self.assertEqual(body["output"]["title"], self.check["output"]["title"])
                self.assertFalse(self.run_handler([dict(self.check, **body)]))

    def test_matching_live_green_survives_delayed_delivery(self):
        self.check["output"]["text"] = "\n".join([MARKER, TARGET, "other metadata"])
        older = dict(EDIT, created_at="2026-11-19T08:00:00Z")
        self.assertFalse(self.run_handler([self.check], timeline=[[older], [EDIT, COMMAND]]))

    def test_release_refresh_uses_legacy_dispatch_schema_and_can_only_invalidate(self):
        dispatcher = (WORKFLOWS / "x86-e2e-dispatcher.yaml").read_text()
        refresh = dispatcher.split("\n  invalidate-base:\n")[1].split("\n  dispatch:\n")[0]
        post = refresh[refresh.index("gh api --method POST"):refresh.index("done < open-pulls.tsv")]
        # Capture the actual gh arguments. The old default-branch gate (a9beb961)
        # requires these nine inputs; its only optional input is executorRunID.
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory, "event.json")
            event.write_text(json.dumps({"repository": {"default_branch": "main"}}))
            args = subprocess.check_output(
                ["bash", "-eu", "-c", 'gh() { printf "%s\\n" "$@"; }\n' + post],
                env=dict(os.environ, GITHUB_EVENT_PATH=str(event), GITHUB_REF_NAME="release-1.2",
                         GITHUB_REPOSITORY="org/repo", prNumber="7", headSHA=HEAD,
                         GITHUB_SHA=BASE, catalogRevision="b" * 64), text=True,
            ).splitlines()
        self.assertEqual(args[:4], ["api", "--method", "POST", "repos/org/repo/actions/workflows/x86-e2e-gate.yaml/dispatches"])
        self.assertEqual(args[4::2], ["-f"] * 10)
        fields = dict(field.split("=", 1) for field in args[5::2])
        self.assertEqual(fields, {
            "ref": "main", "inputs[prNumber]": "7", "inputs[headSHA]": HEAD, "inputs[baseSHA]": BASE,
            "inputs[approvalGeneration]": "1", "inputs[catalogRevision]": "b" * 64,
            "inputs[requestedGroups]": "[]", "inputs[full]": "false",
            "inputs[recordIntent]": "false", "inputs[baseRefresh]": "true",
        })
        declarations = WORKFLOW.split("\n    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        required = re.findall(r"^      (\w+):\n(?:        [^\n]*\n)*?        required: true$", declarations, re.M)
        self.assertEqual({f"inputs[{name}]" for name in required}, fields.keys() - {"ref"})
        job, approval = WORKFLOW.split("\n  base-refresh:\n")[1].split("\n  approval:\n")
        self.assertIn("inputs.executorRunID == '' && inputs.baseRefresh", job)
        self.assertIn("group: x86-e2e-required-gate-${{ inputs.prNumber }}-${{ inputs.headSHA }}", job)
        self.assertEqual(job.split("    steps:\n")[1].strip(), "- *reconcile_target")
        self.assertIn("!inputs.baseRefresh", approval.split("    outputs:")[0])
        self.assertIn("!inputs.baseRefresh && inputs.executorRunID != ''", WORKFLOW)
        # A previous approval for the same head and branch cannot preserve an old base SHA.
        self.check["output"]["text"] = f"{MARKER}\n{TARGET.replace(BASE, '3' * 40)}"
        update, = self.run_handler([self.check], edited_at=None)
        self.assertEqual(update.kwargs["body"]["conclusion"], "action_required")

    def test_unsuccessful_recorder_invalidates_even_fresh_green_once(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        # A timed-out recorder also yields needs.record-retarget.result == 'failure'.
        for result in ("failure", "cancelled", "skipped"):
            with self.subTest(result=result):
                patches = self.run_handler([self.check], timeline=[[EDIT, COMMAND]], recorder_result=result)
                patch, = patches
                body = patch.kwargs["body"]
                self.assertEqual((body["status"], body["conclusion"]), ("completed", "action_required"))
                self.assertEqual(body["output"]["text"], self.check["output"]["text"])
                self.assertFalse(any("timeline" in call.args[0] for call in self.api.call_args_list))
                self.assertFalse(self.run_handler([dict(self.check, **body)], recorder_result=result))

    def test_normal_mutation_green_without_any_target_edit(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        self.assertFalse(self.run_handler([self.check], timeline=[[COMMAND]], edited_at=None))

    def test_replacement_reads_recorded_cutoff_without_native_event(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        for edited_at in (None, EDIT["created_at"]):
            for command, stale in [(COMMAND, False), (dict(COMMAND, created_at=EDIT["created_at"]), True)]:
                patches = self.run_handler([self.check], timeline=[[RECORDED], [command]], edited_at=edited_at)
                self.assertEqual(bool(patches), stale)
        for spoof in [dict(RECORDED, user=dict(RECORDED["user"], type="User")),
                      dict(RECORDED, user=dict(RECORDED["user"], login="other[bot]")),
                      dict(RECORDED, body=RECORDED["body"] + "\n")]:
            patches = self.run_handler([self.check], timeline=[[spoof, COMMAND]])
            self.assertEqual(patches[0].kwargs["body"]["conclusion"], "action_required")

    def test_recorder_requires_timeline_visibility(self):
        script = textwrap.dedent(re.search(r"shell: python\n        run: \|\n((?:          [^\n]*\n|\n)+)",
                                           WORKFLOW.split("  record-retarget:\n")[1])[1])
        spoof = dict(RECORDED, user=COMMAND["user"])
        env = dict(EDITED_AT=EDIT["created_at"], PR_NUMBER="7", GITHUB_REPOSITORY="org/repo")
        for snapshots, posts, fails in [
            ([[EDIT]], 0, False),
            ([[RECORDED]], 0, False),
            ([[], [RECORDED], [], [RECORDED]], 0, False),
            ([[spoof], [spoof], [spoof], [RECORDED]], 1, False),
            ([[], [], [EDIT]], 1, False),
            ([[]] * 31, 1, True),
        ]:
            with self.subTest(snapshots=snapshots), patch.dict(os.environ, env), \
                    patch.object(time, "sleep"), patch.object(subprocess, "run") as post, \
                    patch.object(subprocess, "check_output", side_effect=[json.dumps([s]) for s in snapshots]) as get:
                if fails:
                    with self.assertRaisesRegex(RuntimeError, "not visible"):
                        exec(script, {})
                else:
                    exec(script, {})
                self.assertEqual(get.call_count, len(snapshots))
                for i, call in enumerate(get.call_args_list):
                    resource = "comments" if i == 1 else "timeline"
                    self.assertEqual(call.args[0][-1], f"repos/org/repo/issues/7/{resource}?per_page=100")
                self.assertEqual(post.call_count, posts)
                if posts:
                    post.assert_called_once_with(["gh", "api", "repos/org/repo/issues/7/comments",
                                                  "-f", f"body={RECORDED['body']}"], check=True)

    def test_matching_neutral_and_skipped_cannot_satisfy_required_check(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        for conclusion in ("neutral", "skipped"):
            with self.subTest(conclusion=conclusion):
                check = dict(self.check, status="completed", conclusion=conclusion)
                patch, = self.run_handler([check], timeline=[[EDIT, COMMAND]])
                self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_matching_failure_and_in_progress_need_no_timeline(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        for status, conclusion in [("completed", "failure"), ("in_progress", None)]:
            with self.subTest(status=status):
                self.assertFalse(self.run_handler([dict(self.check, status=status, conclusion=conclusion)]))

    def test_queued_normal_writers_recheck_the_live_binding(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        live = copy.deepcopy(self.pr)
        live["base"]["ref"] = "main"
        for conclusion in (None, "failure", "success"):
            for edited_at in (None, EDIT["created_at"]):
                with self.subTest(conclusion=conclusion, edited_at=edited_at):
                    patch, = self.run_handler([dict(self.check, conclusion=conclusion)], live, edited_at=edited_at)
                    self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")
        self.check["output"]["text"] = MARKER
        patch, = self.run_handler([self.check], edited_at=None)
        self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_matching_green_after_a_b_a_requires_a_new_command(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        back = dict(EDIT, created_at="2026-11-20T08:00:02Z")
        for edited_at in (None, EDIT["created_at"], back["created_at"]):
            with self.subTest(edited_at=edited_at):
                patch, = self.run_handler([self.check], timeline=[[EDIT, COMMAND], [back]], edited_at=edited_at)
                self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")
                fresh = dict(COMMAND, created_at="2026-11-20T08:00:03Z")
                self.assertFalse(self.run_handler([self.check], timeline=[[EDIT, back], [fresh]], edited_at=edited_at))

    def test_direct_edit_requires_proof_of_that_specific_delivery(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        older = dict(EDIT, created_at="2026-11-19T08:00:00Z")
        patch, = self.run_handler([self.check], timeline=[[older], [COMMAND]])
        self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_matching_green_requires_command_after_latest_retarget(self):
        self.check["output"]["text"] = "\n".join([MARKER, TARGET])
        for timeline in [
            [[EDIT, dict(COMMAND, created_at=EDIT["created_at"])]],
            [[EDIT, COMMAND], [dict(EDIT, created_at="2026-11-20T08:00:02Z")]],
            [[COMMAND]], [[EDIT]], [[EDIT, dict(COMMAND, id=124)]],
            [[EDIT, dict(COMMAND, body="/test e2e please!")]],
            [[EDIT, dict(COMMAND, created_at="invalid")]],
            subprocess.CalledProcessError(1, "gh api"),
        ]:
            with self.subTest(timeline=timeline):
                patch, = self.run_handler([self.check], timeline=timeline)
                self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_matching_green_requires_one_valid_request_marker(self):
        for marker in ["", MARKER.replace("123", "0"), MARKER.replace("123", "bad"), f"{MARKER}\n{MARKER}"]:
            with self.subTest(marker=marker):
                self.check["output"]["text"] = f"{marker}\n{TARGET}"
                patch, = self.run_handler([self.check])
                self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_no_foreign_check_or_creation(self):
        for fields in [{"app": None}, {"app": {"slug": "other"}}, {"name": "candidate"},
                       {"external_id": f"x86-e2e-pr-8-{HEAD}"}, {"head_sha": BASE}]:
            self.assertFalse(self.run_handler([dict(self.check, **fields)]))
        self.assertFalse(self.run_handler([]))

    def test_obsolete_edit_is_ignored(self):
        for fields in [{"state": "closed"}, {"head": {"sha": BASE}}]:
            self.assertFalse(self.run_handler([self.check], dict(self.pr, **fields)))

    def test_base_tip_can_advance_while_edit_waits(self):
        self.check["output"]["text"] = TARGET
        live = copy.deepcopy(self.pr)
        live["base"]["sha"] = "3" * 40
        self.assertEqual(len(self.run_handler([self.check], live)), 1)


if __name__ == "__main__":
    unittest.main()
