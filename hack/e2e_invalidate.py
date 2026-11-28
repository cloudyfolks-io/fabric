"""Reconcile fixed checks; only a failed pull_request_target recorder writes comments."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

# The workflow uses -I and checks out the pinned workflow revision.
sys.path.append(str(Path(__file__).resolve().parent))
from e2e_target_history import TargetHistory, control, issued_generation, retarget_marker, timestamp, trusted_bot


def api(path, *args, body=None, paginate=False):
    flags = ["--paginate", "--slurp"] if paginate else []
    result = subprocess.check_output(
        ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{path}", *flags, *args],
        input=json.dumps(body) if body is not None else None,
        text=True,
    )
    return json.loads(result)


def record_failure(number, edited_at):
    timestamp(edited_at)
    path = f"issues/{number}/comments"
    comments = [c for page in api(f"{path}?per_page=100", paginate=True) for c in page if trusted_bot(c)]
    receipt = f"<!-- x86-e2e-retarget-failure edited_at={edited_at} -->"
    dates = [c["created_at"] for c in comments if c["body"] == receipt]
    if not dates:
        dates.append(api(path, "--method", "POST", "--input", "-", body={"body": receipt})["created_at"])
    cutoff = max(dates, key=timestamp)
    # 52c4318 understands only this exact marker. Repair a partial write on retry.
    marker = retarget_marker(cutoff)
    if not any(c["body"] == marker for c in comments):
        api(path, "--method", "POST", "--input", "-", body={"body": marker})
    return cutoff


def verify_push_target(ref, sha):
    pushed_ref = os.environ.get("PUSHED_REF", "")
    pushed_sha = os.environ.get("PUSHED_SHA", "")
    if (pushed_ref or pushed_sha) and not (
        control.supportedBaseRef(pushed_ref) and re.fullmatch(r"[0-9a-f]{40}", pushed_sha)
    ):
        raise ValueError("invalid original push target")
    if not control.supportedBaseRef(ref) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid PR target")
    tip = api(f"git/ref/heads/{ref}")
    if tip["ref"] != f"refs/heads/{ref}" or tip["object"]["type"] != "commit" or tip["object"]["sha"] != sha:
        raise ValueError("PR target does not match the branch tip")
    if ref == pushed_ref and sha != pushed_sha:
        # Both live reads could still show the pre-push tip. Require proof of a later revision.
        if pushed_sha == "0" * 40 or os.environ.get("FORCED_PUSH") != "false":
            raise ValueError("cannot order revisions after deletion or force-push")
        comparison = api(f"compare/{pushed_sha}...{sha}?per_page=1")
        if comparison["status"] != "ahead":
            raise ValueError("branch tip is not a descendant of the pushed revision")


def invalidate(number, head, edited_at=None):
    if not re.fullmatch(r"[1-9][0-9]*", number) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid fixed check identity")
    checks = control.fixedChecks(int(number), head, api)
    failed = edited_at is not None and os.environ.get("RECORDER_RESULT") != "success"
    if not checks and not failed:
        return
    errors = (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, AttributeError)
    target = None
    push_receiver = (os.environ.get("VERIFY_PUSH_TARGET") == "true"
                     or "PUSHED_REF" in os.environ or "PUSHED_SHA" in os.environ)
    try:
        live = api(f"pulls/{number}")
        if live["state"] != "open" or live["head"]["sha"] != head:
            return
        ref, sha = live["base"]["ref"], live["base"]["sha"]
        if push_receiver:
            verify_push_target(ref, sha)
        target = f"x86-e2e-target base={sha} ref={ref}"
    except errors as error:
        print(f"Could not verify live target: {type(error).__name__}")
    history = None
    cutoff = None
    record_error = None
    if failed:
        try:
            cutoff = record_failure(number, edited_at)
            history = TargetHistory(api(f"issues/{number}/timeline?per_page=100", paginate=True), checks)
            if timestamp(cutoff) not in history.recorded:
                raise ValueError("failure cutoff not visible in PR timeline")
        except errors as error:
            record_error = error

    current = max(checks, key=lambda check: check["id"], default={})
    current_generation = issued_generation(current)
    invalidation = control.invalidationSummary
    if cutoff:
        invalidation += f"\n{control.cutoffPrefix}{cutoff}"
    for check in checks:
        conclusion = check["conclusion"]
        output = check["output"]
        lines = (output["text"] or "").splitlines()
        bindings = [line for line in lines if line.startswith("x86-e2e-target ")]
        waiting = (conclusion == "action_required" and output["summary"]
                   == "The latest authorized x86 E2E approval is waiting for its trusted executor.")
        summary = invalidation
        if check["id"] != current["id"]:
            # A sibling cannot set a PR-wide time cutoff or erase an existing one.
            if conclusion == "action_required" and not waiting:
                continue
            summary = control.supersededSummary
            generation = issued_generation(check)
            if generation and generation != current_generation:
                summary += f"\n{control.supersededPrefix}{generation}"
        elif conclusion == "action_required" and not (waiting and (failed or push_receiver)) and not record_error:
            continue
        elif bindings == [target] and not record_error:
            if not waiting and conclusion not in {"success", "skipped", "neutral"}:
                continue
            generation = issued_generation(check)
            if (waiting or conclusion == "success") and generation:
                try:
                    if history is None:
                        history = TargetHistory(api(f"issues/{number}/timeline?per_page=100", paginate=True), checks)
                    expected = cutoff or edited_at
                    if (expected is None or timestamp(expected) in history.edits) and history.is_fresh(generation):
                        continue
                except errors as error:
                    print(f"Could not verify retarget evidence: {type(error).__name__}")
        api(f"check-runs/{check['id']}", "--method", "PATCH", "--input", "-", body={
            "status": "completed",
            "conclusion": "action_required",
            "output": {"title": output["title"], "summary": summary, "text": output["text"] or ""},
        })
    if record_error:
        raise record_error


if __name__ == "__main__":
    invalidate(os.environ["PR_NUMBER"], os.environ["HEAD_SHA"], os.environ.get("EDITED_AT"))
