import copy
import hashlib
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

import e2e_control as control
import e2e_gate_recovery as recovery
import e2e_invalidate as invalidate
import e2e_selector as selector


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
        self.comments = []
        self.now = "2026-11-20T08:01:00Z"
        self.pr = {"number": 7, "state": "open", "head": {"sha": HEAD},
                   "base": {"ref": "release-1.2", "sha": BASE}}
        self.check = {
            "id": 17, "name": "x86-e2e / required-gate", "external_id": f"x86-e2e-pr-7-{HEAD}",
            "app": {"slug": "github-actions"}, "head_sha": HEAD, "conclusion": "success",
            "output": {"title": "x86 E2E gate", "text": MARKER, "summary": "Passed"},
        }

    def run_handler(self, checks, live=None, timeline=None, edited_at=EDIT["created_at"], recorder_result="success",
                    push=None, tip=None, comparison=None, forced_push="false", legacy_refresh=False):
        self.api.reset_mock()

        def respond(path, *args, **kwargs):
            if path.startswith("commits/"):
                self.assertIn(f"commits/{HEAD}/check-runs?", path)
                self.assertIn("filter=all", path)
                self.assertEqual(args, ("--paginate", "--slurp"))
                return [{"check_runs": []}, {"check_runs": checks}]
            if path == "pulls/7":
                return live or self.pr
            base = (live or self.pr)["base"]
            if path == f"git/ref/heads/{base['ref']}":
                if isinstance(tip, Exception):
                    raise tip
                self.assertIsNotNone(tip)
                return {"ref": f"refs/heads/{base['ref']}", "object": {"type": "commit", "sha": tip}}
            if push and path == f"compare/{push[1]}...{base['sha']}?per_page=1":
                if isinstance(comparison, Exception):
                    raise comparison
                self.assertIsNotNone(comparison)
                return {"status": comparison}
            if path == "issues/7/timeline?per_page=100":
                self.assertEqual(args, ("--paginate", "--slurp"))
                if isinstance(timeline, Exception):
                    raise timeline
                self.assertIsNotNone(timeline)
                return [*timeline, self.comments]
            if path == "issues/7/comments?per_page=100":
                return [self.comments]
            if path == "issues/7/comments":
                item = dict(RECORDED, id=200 + len(self.comments), created_at=self.now, body=kwargs["body"]["body"])
                self.comments.append(item)
                return item
            self.assertIn(path, {f"check-runs/{check['id']}" for check in checks})
            self.assertEqual(args, ("--method", "PATCH", "--input", "-"))
            return {}

        self.api.side_effect = respond
        env = {"PR_NUMBER": "7", "HEAD_SHA": HEAD, "GITHUB_REPOSITORY": "org/repo"}
        if legacy_refresh:
            env["VERIFY_PUSH_TARGET"] = "true"
        if push is not None:
            env["PUSHED_REF"], env["PUSHED_SHA"] = push
            env["FORCED_PUSH"] = forced_push
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
            invalidate.invalidate("7", HEAD, edited_at)
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
        self.assertIn("ref: ${{ github.workflow_sha }}\n          persist-credentials: false", retarget)
        self.assertEqual(set(re.findall(r"^      ([\w-]+: (?:read|write))$", retarget, re.M)),
                         {"contents: read", "issues: write", "pull-requests: read", "checks: write"})
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
        self.assertIn("      - &reconcile_target\n", retarget)
        self.assertIn("run: python3 -I hack/e2e_invalidate.py", retarget)
        self.assertTrue(mutate.split("    steps:\n")[1].startswith("      - *checkout_control\n      - *reconcile_target\n"))
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

    def test_push_requires_coherent_pr_branch_tip_and_revision(self):
        ref = self.pr["base"]["ref"]
        other = "3" * 40
        unavailable = subprocess.CalledProcessError(1, "gh api")
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        for push, tip, comparison, keep in [
            ((ref, BASE), BASE, None, True),              # Original push.
            ((ref, other), other, None, False),           # PR still shows previous base.
            ((ref, BASE), other, None, False),            # Later push; PR still shows original event.
            ((ref, other), BASE, "behind", False),        # Both live reads lag the push.
            ((ref, other), BASE, "ahead", True),          # Fresh success after a later push.
            ((ref, other), BASE, "diverged", False),      # Ambiguous force-push.
            ((ref, other), BASE, unavailable, False),
            ((ref, BASE), unavailable, None, False),
            ((ref, "0" * 40), BASE, None, False),
            (("main", other), BASE, None, True),          # PR retargeted; original push no longer applies.
            (("", ""), BASE, None, True),                 # Older v1 sender.
            (("", ""), other, None, False),
            ((ref, ""), BASE, None, False),
            (("", BASE), BASE, None, False),
            (("../main", BASE), BASE, None, False),
        ]:
            with self.subTest(push=push, tip=tip, comparison=comparison):
                patches = self.run_handler([self.check], timeline=[[EDIT, COMMAND]], edited_at=None,
                                           push=push, tip=tip, comparison=comparison)
                if keep:
                    self.assertEqual(patches, [])
                else:
                    update, = patches
                    self.assertEqual(update.kwargs["body"]["conclusion"], "action_required")
                    self.assertEqual(update.kwargs["body"]["output"]["text"], self.check["output"]["text"])
        # After a rewind, the old tip also descends from the pushed revision.
        patch, = self.run_handler([self.check], edited_at=None, push=(ref, other), tip=BASE, forced_push="true")
        self.assertEqual(patch.kwargs["body"]["conclusion"], "action_required")

    def test_lagging_push_invalidates_once_and_expires_the_old_generation(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        waiting = "The latest authorized x86 E2E approval is waiting for its trusted executor."
        push = (self.pr["base"]["ref"], "3" * 40)
        for conclusion, summary in [("success", "Passed"), ("action_required", waiting)]:
            check = dict(self.check, conclusion=conclusion, output=dict(self.check["output"], summary=summary))
            update, = self.run_handler([check], edited_at=None, push=push, tip=push[1])
            check.update(update.kwargs["body"], completed_at=self.now)
            self.assertEqual(check["output"]["summary"], control.invalidationSummary)
            self.assertFalse(self.run_handler([check], edited_at=None, push=push, tip=push[1]))
            fresh = dict(COMMAND, id=125, created_at="2026-11-20T08:01:01Z")
            history = invalidate.TargetHistory([[COMMAND, fresh]], [check])
            self.assertFalse(history.is_fresh(COMMAND["id"]))
            self.assertTrue(history.is_fresh(fresh["id"]))

    def test_only_current_check_sets_a_cutoff_on_tip_failure_or_retarget(self):
        fresh = dict(COMMAND, id=125, created_at="2026-11-20T08:00:05Z")
        timeline = [[COMMAND, dict(EDIT, created_at="2026-11-20T08:00:02Z"), fresh]]
        waiting = "The latest authorized x86 E2E approval is waiting for its trusted executor."
        self.check.update(conclusion="action_required", output=dict(
            self.check["output"], text=f"{MARKER}\n{TARGET.replace('release-1.2', 'main')}", summary=waiting))
        current = dict(self.check, id=19, output=dict(self.check["output"],
                       text=f"{MARKER.replace('123', '125')}\n{TARGET}"))
        for tip, current_valid in [(BASE, True), (subprocess.CalledProcessError(1, "gh api"), False)]:
            checks = copy.deepcopy([current, self.check])
            writes = self.run_handler(checks, timeline=timeline, edited_at=None, push=("main", BASE), tip=tip)
            expected = {17: f"{control.supersededSummary}\n{control.supersededPrefix}{COMMAND['id']}"}
            if not current_valid:
                expected[19] = control.invalidationSummary
            for write in writes:
                identifier = int(write.args[0].split("/")[-1])
                body = write.kwargs["body"]
                self.assertEqual(body["conclusion"], "action_required")
                self.assertEqual(body["output"]["summary"], expected.pop(identifier))
                next(c for c in checks if c["id"] == identifier).update(body, completed_at=self.now)
            self.assertEqual(expected, {})
            snapshot = invalidate.TargetHistory(timeline, checks)
            self.assertEqual(snapshot.is_fresh(fresh["id"]), current_valid)
            self.assertFalse(snapshot.is_fresh(COMMAND["id"]))
            self.assertEqual(snapshot.invalidation is None, current_valid)
            self.assertFalse(self.run_handler(checks, timeline=timeline, edited_at=None, push=("main", BASE), tip=BASE))

    def test_two_issued_ids_for_the_same_generation(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        old = dict(self.check, id=16, status="in_progress", conclusion=None)
        update, = self.run_handler([old, self.check], edited_at=None, legacy_refresh=True, tip=BASE,
                                   timeline=[[COMMAND]])
        self.assertEqual(update.args[0], "check-runs/16")
        old.update(update.kwargs["body"], completed_at=self.now)
        self.assertTrue(invalidate.TargetHistory([[COMMAND]], [old, self.check]).is_fresh(COMMAND["id"]))
        with self.assertRaisesRegex(ValueError, "current issued check is missing"):
            invalidate.TargetHistory([[COMMAND]], [old])
        self.assertFalse(control.matchesIssuedCheck(old, "a" * 64, COMMAND["id"], BASE, "release-1.2"))

    def test_release_refresh_addresses_only_the_dedicated_receiver(self):
        dispatcher = (WORKFLOWS / "x86-e2e-dispatcher.yaml").read_text()
        refresh = dispatcher.split("\n  invalidate-base:\n")[1].split("\n  dispatch:\n")[0]
        post = refresh[refresh.index("gh api --method POST"):refresh.index("done < open-pulls.tsv")]
        self.assertLess(refresh.index("bash hack/verify-e2e-receiver-v1.sh"), refresh.index(post))
        self.assertNotIn("x86-e2e-gate.yaml", refresh)
        self.assertIn("PUSHED_REF: ${{ github.event.ref }}", refresh)
        self.assertIn("PUSHED_SHA: ${{ github.event.after }}", refresh)
        self.assertIn("FORCED_PUSH: ${{ toJSON(github.event.forced) }}", refresh)
        self.assertIn("state=open&base=$pushedRef&per_page=100", refresh)
        setup = refresh[refresh.index("set -euo pipefail"):refresh.index("bash hack/verify-e2e-receiver-v1.sh")]
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory, "event.json")
            event.write_text(json.dumps({"repository": {"default_branch": "main"}}))
            args = subprocess.check_output(
                ["bash", "-eu", "-c", 'gh() { if [[ $2 == repos/org/repo/git/ref/heads/main ]]; then '
                 'echo "$TREE"; else printf "%s\\n" "$@"; fi; }\n' + setup + post],
                cwd=WORKFLOWS.parents[1],
                env=dict(os.environ, GITHUB_EVENT_PATH=str(event), PUSHED_REF="refs/heads/release-1.2",
                         GITHUB_REPOSITORY="org/repo", prNumber="7", headSHA=HEAD,
                         PUSHED_SHA=BASE, FORCED_PUSH="false", TREE="4" * 40), text=True,
            ).splitlines()
        self.assertEqual(args[:4], ["api", "--method", "POST", "repos/org/repo/actions/workflows/x86-e2e-invalidate-v1.yaml/dispatches"])
        self.assertEqual(args[4::2], ["-f"] * 7)
        fields = dict(field.split("=", 1) for field in args[5::2])
        self.assertEqual(fields, {
            "ref": "main", "inputs[prNumber]": "7", "inputs[headSHA]": HEAD,
            "inputs[pushedRef]": "release-1.2", "inputs[pushedSHA]": BASE, "inputs[forcedPush]": "false",
            "inputs[verifiedReceiverSHA]": "4" * 40,
        })
        receiver = (WORKFLOWS / "x86-e2e-invalidate-v1.yaml").read_text()
        declarations = receiver.split("\n    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        required = re.findall(r"^      (\w+):\n(?:        [^\n]*\n)*?        required: true$", declarations, re.M)
        self.assertEqual(set(required), {"prNumber", "headSHA"})
        self.assertIn("FORCED_PUSH: ${{ toJSON(inputs.forcedPush) }}", receiver)
        for field, variable in (("pushedRef", "PUSHED_REF"), ("pushedSHA", "PUSHED_SHA")):
            self.assertIn(f"{variable}: ${{{{ inputs.{field} }}}}", receiver)
        job, approval = WORKFLOW.split("\n  base-refresh:\n")[1].split("\n  approval:\n")
        self.assertIn("inputs.executorRunID == '' && inputs.baseRefresh", job)
        self.assertIn('VERIFY_PUSH_TARGET: "true"', job)
        self.assertIn("group: x86-e2e-required-gate-${{ inputs.prNumber }}-${{ inputs.headSHA }}", job)
        self.assertEqual(job.split("    steps:\n")[1].strip(), "- *checkout_control\n      - *reconcile_target")
        self.assertEqual(re.findall(r"^  ([\w-]+):$", receiver.split("\njobs:\n")[1], re.M), ["invalidate"])
        self.assertEqual(re.findall(r"^        run: (.+)$", receiver, re.M), ["|"])
        self.assertIn("VERIFIED_RECEIVER_SHA: ${{ inputs.verifiedReceiverSHA }}", receiver)
        reconcile = textwrap.dedent(receiver.split("        run: |\n")[1])
        for revision, allowed in [(BASE, True), ("", True), (HEAD, False), ("bad", False)]:
            result = subprocess.run(["bash", "-eu", "-c", 'python3() { printf "%s\\n" "$*"; }\n' + reconcile],
                                    env=dict(os.environ, VERIFIED_RECEIVER_SHA=revision, GITHUB_WORKFLOW_SHA=BASE),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, allowed)
            self.assertEqual(result.stdout.strip(), "-I hack/e2e_invalidate.py" if allowed else "")
        self.assertEqual(re.findall(r"^  ([\w-]+): write$", receiver, re.M), ["checks"])
        self.assertIn("queue: max", receiver)
        self.assertIn("!inputs.baseRefresh", approval.split("    outputs:")[0])
        self.assertIn("!inputs.baseRefresh && inputs.executorRunID != ''", WORKFLOW)
        # A previous approval for the same head and branch cannot preserve an old base SHA.
        self.check["output"]["text"] = f"{MARKER}\n{TARGET.replace(BASE, '3' * 40)}"
        catalog = selector.loadCatalog(WORKFLOWS.parent / "e2e-selection.json")
        intent = control.renderApprovalIntent({
            "headSHA": HEAD, "baseSHA": BASE, "baseRef": self.pr["base"]["ref"],
            "catalogRevision": selector.catalogRevision(catalog), "approvalGeneration": COMMAND["id"],
            "requestedGroups": [], "full": False,
        })
        pending = dict(RECORDED, body=intent)
        self.assertEqual(len(control.approvalIntents(self.pr, catalog, [[COMMAND, pending]])), 1)
        update, = self.run_handler([self.check], timeline=[[COMMAND, pending]], edited_at=None, legacy_refresh=True,
                                   tip=BASE)
        self.assertEqual(update.kwargs["body"]["conclusion"], "action_required")
        self.assertEqual(len(self.api.call_args_list), 4)  # Read check, live PR and branch tip; PATCH the check.
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        self.assertFalse(self.run_handler([self.check], timeline=[[COMMAND, pending]], edited_at=None,
                                          legacy_refresh=True, tip=BASE))
        for tip in ("3" * 40, subprocess.CalledProcessError(1, "gh api")):
            update, = self.run_handler([self.check], edited_at=None, legacy_refresh=True, tip=tip)
            self.assertEqual(update.kwargs["body"]["conclusion"], "action_required")

    def test_release_requires_active_versioned_receiver_on_default_branch(self):
        root = WORKFLOWS.parents[1]
        selection = (WORKFLOWS / "build-x86-image.yaml").read_text()
        selection = textwrap.dedent(selection.split("      - name: Require the installed main receiver")[1]
                                    .split("        run: |\n")[1].split("\n      - name:")[0])
        sender = (WORKFLOWS / "x86-e2e-dispatcher.yaml").read_text()
        verify = "default=$(jq -er"
        sender = "set -euo pipefail\n" + verify + sender.split(verify, 1)[1].split("          gh api", 1)[0]
        path = ".github/workflows/x86-e2e-invalidate-v1.yaml"
        verifier = root / "hack/verify-e2e-receiver-v1.sh"
        handler = "hack/e2e_invalidate.py"
        history = "hack/e2e_target_history.py"
        control = "hack/e2e_control.py"
        selector = "hack/e2e_selector.py"
        tree_sha = "4" * 40

        def blob(data):
            return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

        entries = {name: dict(path=name, type="blob", sha=blob((root / name).read_bytes()),
                              mode="100755" if os.access(root / name, os.X_OK) else "100644")
                   for name in (path, handler, history, control, selector)}
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "hack").mkdir()
            local_verifier = work / "hack/verify-e2e-receiver-v1.sh"
            main_verifier = work / "main-verifier.sh"
            (work / "event.json").write_text(json.dumps({"repository": {"default_branch": "main"}}))
            git = work / "git"
            git.write_text("""#!/bin/bash
set -eu
[[ "$*" == 'show refs/remotes/origin/main:hack/verify-e2e-receiver-v1.sh' ]]
cat "$MAIN_VERIFIER"
""")
            git.chmod(0o755)
            gh = work / "gh"
            gh.write_text("""#!/bin/bash
set -eu
case "$2" in
  repos/org/repo/git/ref/heads/main)
    if [[ "${3:-}" == --jq ]]; then
      [[ "$4" == .object.sha ]]
      if [[ "$MODE" == changed-before-verify ]]; then echo "$OTHER"; else echo "$TREE"; fi
      exit 0
    fi
    if [[ "$MODE" == moved && -f tip-read ]]; then TREE=$OTHER; fi
    touch tip-read
    jq -n --arg sha "$TREE" '{ref:"refs/heads/main",object:{type:"commit",sha:$sha}}' ;;
  "repos/org/repo/git/commits/$TREE")
    jq -n --arg sha "$TREE" --arg tree "$TREE_SHA" '{sha:$sha,tree:{sha:$tree}}' ;;
  "repos/org/repo/git/trees/$TREE_SHA?recursive=1") cat active-tree.json ;;
  repos/org/repo/actions/workflows/x86-e2e-invalidate-v1.yaml)
    state=active
    if [[ "$MODE" == disabled ]]; then state=disabled_manually; fi
    jq -n --arg state "$state" '{path: ".github/workflows/x86-e2e-invalidate-v1.yaml", state: $state}' ;;
  *) exit 2 ;;
esac
""")
            gh.chmod(0o755)
            env = dict(os.environ, PATH=f"{work}:{os.environ['PATH']}", GITHUB_REPOSITORY="org/repo",
                       GITHUB_EVENT_PATH=str(work / "event.json"), DEFAULT_BRANCH="main",
                       MAIN_VERIFIER=str(main_verifier), TREE=BASE, TREE_SHA=tree_sha, OTHER=HEAD)
            # Real b8f86f9c Git object IDs; only the deployed tree varies, never the caller checkout.
            old = {path: "252794fa4b3fca0f6204b1b528ea8f2e41fab049",
                   handler: "d216b8e1fc5e1458c6e941fe42b300df22e88dc3",
                   history: "6c53eca2c480945dbd20133713de13b8957920d5",
                   control: "314c26468bf46c57a39c130e7a54cd257c6ad0db"}
            wrong_entrypoint = (root / path).read_text().replace("hack/e2e_invalidate.py", "hack/other.py")
            cases = [("active", {}), ("unrelated-change", {}), ("disabled", {}), ("moved", {}), ("changed-before-verify", {}),
                     ("truncated", {}), ("wrong-tree", {}), ("other-handler", {path: blob(wrong_entrypoint.encode())}),
                     ("pre-push-receiver", old), ("mixed-handler", {handler: old[handler]}),
                     ("mixed-history", {history: old[history]}), ("mixed-control", {control: old[control]}),
                     ("mixed-selector", {selector: HEAD}), ("old-workflow", {path: old[path]}),
                     ("missing-handler", {handler: None}), ("missing-import", {selector: None}),
                     ("symlink", {}), ("shadow-package", {}), ("old-default-verifier", {}),
                     ("old-release-verifier", {}), ("old-default-mixed", {handler: old[handler]}), ("malformed-receipt", {}),
                     ("failed-verifier", {})]
            for mode, changes in cases:
                files = [dict(item, sha=changes.get(name, item["sha"]))
                         for name, item in entries.items() if changes.get(name, True) is not None]
                if mode == "symlink":
                    next(item for item in files if item["path"] == handler)["mode"] = "120000"
                if mode == "shadow-package":
                    files.append(dict(path="hack/e2e_target_history/__init__.py", type="blob", sha=HEAD, mode="100644"))
                if mode == "unrelated-change":
                    files.append(dict(path="docs/release.md", type="blob", sha=HEAD, mode="100644"))
                (work / "active-tree.json").write_text(json.dumps({
                    "sha": HEAD if mode == "wrong-tree" else tree_sha, "truncated": mode == "truncated", "tree": files,
                }))
                bad_verifier = {"malformed-receipt": "printf 'x86-e2e-invalidate-v1:push-runtime-1\\n\\n'",
                                "failed-verifier": "echo 'x86-e2e-invalidate-v1:push-runtime-1'; exit 1"}.get(mode)
                local_verifier.write_text(bad_verifier or verifier.read_text())
                main_verifier.write_text(bad_verifier or verifier.read_text())
                schema_only = "echo 'x86-e2e-invalidate-v1:push-target'"
                if mode in {"old-default-verifier", "old-default-mixed"}:
                    main_verifier.write_text(schema_only)
                if mode == "old-release-verifier":
                    local_verifier.write_text(schema_only)
                for caller, script in [("selection", selection), ("sender", sender)]:
                    with self.subTest(mode=mode, caller=caller):
                        (work / "tip-read").unlink(missing_ok=True)
                        result = subprocess.run(
                            ["bash", "-c", script], cwd=work,
                            env=dict(env, MODE=mode),
                            capture_output=True, text=True,
                        )
                        allowed = (mode in {"active", "unrelated-change"} or (mode, caller) in {
                            ("old-default-verifier", "sender"), ("old-release-verifier", "selection"),
                            ("changed-before-verify", "selection"),
                        })
                        self.assertEqual(result.returncode == 0, allowed, result.stderr)
                if mode == "active":
                    receipt = subprocess.check_output(["bash", str(local_verifier)], cwd=work,
                                                      env=dict(env, MODE=mode), text=True)
                    self.assertEqual(receipt, "x86-e2e-invalidate-v1:push-target\n")

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
                self.assertFalse(self.run_handler([dict(self.check, **body)], timeline=[[EDIT]], recorder_result=result))
                self.assertEqual(len(self.comments), 2)

    def test_failed_edits_while_waiting_survive_old_writes_and_rearm(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        self.check["conclusion"] = "action_required"
        self.check["output"]["summary"] = "The latest authorized x86 E2E approval is waiting for its trusted executor."
        edits = [EDIT["created_at"], "2026-11-20T08:02:00Z"]
        for index, stamp in enumerate(edits):
            if index:
                self.check["output"]["summary"] = control.invalidationSummary
                self.check["completed_at"] = self.now
            self.now = f"2026-11-20T08:0{index * 2 + 1}:00Z"
            updates = self.run_handler([self.check], timeline=[[]], edited_at=stamp, recorder_result="failure")
            self.assertEqual(bool(updates), index == 0)
            if updates:
                self.check.update(updates[0].kwargs["body"])
            self.assertEqual(self.comments[-1]["body"], f"<!-- x86-e2e-retarget edited_at={self.now} -->")
        # A 52c4318 writer replaced the whole check; only comments retain the cutoff.
        self.check["conclusion"] = "success"
        self.check["output"]["summary"] = "Passed"
        for when, stale in [(edits[1], True), (self.now, True), ("2026-11-20T08:03:01Z", False)]:
            timeline = [[dict(COMMAND, created_at=when)]]
            def api(path, paginate):
                return [{"check_runs": [self.check]}] if path.startswith("commits/") else [*timeline, self.comments]
            self.assertEqual(bool(recovery.staleTarget([self.pr], api)), stale)
            for delivery in [None, *edits]:
                patches = self.run_handler([self.check], timeline=timeline, edited_at=delivery, recorder_result="failure")
                self.assertEqual(bool(patches), stale)
                self.assertEqual(len(self.comments), 4)
        self.check["conclusion"] = "action_required"
        self.check["output"]["summary"] = "The latest authorized x86 E2E approval is waiting for its trusted executor."
        fresh = [[dict(COMMAND, created_at="2026-11-20T08:03:01Z")]]
        self.assertFalse(self.run_handler([self.check], timeline=fresh, edited_at=edits[0], recorder_result="failure"))
        self.assertEqual(len(self.comments), 4)

    def test_legacy_sibling_cutoff_matches_discovery(self):
        self.check["output"]["text"] = f"{MARKER}\n{TARGET}"
        blocked = dict(self.check, id=16, conclusion="action_required",
                       output=dict(self.check["output"], summary=control.invalidationSummary))
        for completed, stale in [(EDIT["created_at"], False), (COMMAND["created_at"], True), (None, True)]:
            checks = [self.check, dict(blocked, completed_at=completed)]
            def api(path, paginate):
                return [{"check_runs": checks}] if path.startswith("commits/") else [[COMMAND]]
            self.assertEqual(bool(recovery.staleTarget([self.pr], api)), stale)
            self.assertEqual(bool(self.run_handler(checks, timeline=[[COMMAND]], edited_at=None)), stale)

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
