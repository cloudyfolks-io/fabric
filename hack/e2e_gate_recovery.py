#!/usr/bin/env python3
"""Find missing completion handoffs; the fixed gate still validates all evidence."""

import json
import os
import re
import subprocess

import e2e_control as e2eControl
from e2e_target_history import TargetHistory


def fixedChecks(prNumber, headSHA, api):
    pages = api(
        f"commits/{headSHA}/check-runs?check_name=x86-e2e%20%2F%20required-gate&filter=all&per_page=100",
        paginate=True,
    )
    return [
        check for page in pages for check in page["check_runs"]
        if check["name"] == "x86-e2e / required-gate"
        and (check.get("app") or {}).get("slug") == "github-actions"
        and check["head_sha"] == headSHA
        and check.get("external_id") == f"x86-e2e-pr-{prNumber}-{headSHA}"
    ]


def checkGeneration(check, base):
    target = f"x86-e2e-target base={base['sha']} ref={base['ref']}"
    lines = (check["output"].get("text") or "").splitlines()
    markers = [line for line in lines if line.startswith("x86-e2e-gate")]
    if [line for line in lines if line.startswith("x86-e2e-target ")] != [target] or len(markers) != 1:
        return None
    match = re.fullmatch(r"x86-e2e-gate request=[0-9a-f]{64} approval=([1-9][0-9]*)", markers[0])
    return int(match[1]) if match else None


def targetHistory(prNumber, api):
    return TargetHistory(api(f"issues/{prNumber}/timeline?per_page=100", paginate=True))


def staleTarget(pulls, api):
    for pr in pulls:
        head = pr["head"]["sha"]
        for check in fixedChecks(pr["number"], head, api):
            if check.get("conclusion") not in {"success", "neutral", "skipped"}:
                continue
            generation = checkGeneration(check, pr["base"])
            if (check["conclusion"] != "success" or not generation
                    or not targetHistory(pr["number"], api).is_fresh(generation)):
                return pr["number"], head
    return None


def pendingExecutor(runs, repository, api):
    for run in sorted(runs, key=lambda item: item["id"], reverse=True):
        if (
            run.get("status") != "completed"
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != ".github/workflows/build-x86-image.yaml"
            or run.get("actor", {}).get("login") != "github-actions[bot]"
            or run.get("head_repository", {}).get("full_name") != repository
        ):
            continue
        try:
            metadata = e2eControl.parseExecutorRunName(run["display_title"])
        except (ValueError, KeyError):
            continue
        if run.get("head_branch") != e2eControl.executorHeadBranch(metadata):
            continue
        pullRequest = api(f"pulls/{metadata['prNumber']}")
        if (
            pullRequest["state"] != "open"
            or pullRequest["base"].get("ref") != metadata["baseRef"]
            or pullRequest["head"]["sha"] != metadata["headSHA"]
            or pullRequest["base"]["sha"] != run.get("head_sha")
        ):
            continue
        gateChecks = fixedChecks(metadata["prNumber"], metadata["headSHA"], api)
        check = max(gateChecks, key=lambda item: item["id"], default={})
        if (
            check.get("conclusion") == "action_required"
            and check.get("output", {}).get("summary")
            == "The latest authorized x86 E2E approval is waiting for its trusted executor."
            and checkGeneration(check, pullRequest["base"]) == metadata["approvalGeneration"]
            and targetHistory(metadata["prNumber"], api).is_fresh(metadata["approvalGeneration"])
        ):
            return run["id"]
    return None


def main():
    repository = os.environ["GITHUB_REPOSITORY"]

    def api(path, paginate=False):
        flags = ["--paginate", "--slurp"] if paginate else []
        return json.loads(
            subprocess.check_output(["gh", "api", *flags, f"repos/{repository}/{path}"], text=True)
        )

    runs = api(
        "actions/workflows/build-x86-image.yaml/runs"
        "?event=workflow_dispatch&status=completed&per_page=100"
    )["workflow_runs"]
    runId = pendingExecutor(runs, repository, api)
    pages = api("pulls?state=open&per_page=100", paginate=True)
    stale = staleTarget((pr for page in pages for pr in page), api)
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        if runId is not None:
            stream.write(f"runId={runId}\n")
        if stale:
            stream.write(f"stalePR={stale[0]}\nstaleHead={stale[1]}\n")


if __name__ == "__main__":
    main()
