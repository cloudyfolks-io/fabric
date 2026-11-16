#!/usr/bin/env python3
"""Find missing completion handoffs; the fixed gate still validates all evidence."""

import json
import os
import subprocess

import e2e_control as e2eControl


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
            or pullRequest["head"]["sha"] != metadata["headSHA"]
            or pullRequest["base"]["sha"] != run.get("head_sha")
        ):
            continue
        checks = api(f"commits/{metadata['headSHA']}/check-runs?per_page=100")
        if any(
            check["name"] == "x86-e2e / required-gate"
            and check.get("conclusion") == "action_required"
            and check.get("output", {}).get("summary")
            == "The latest authorized x86 E2E approval is waiting for its trusted executor."
            for check in checks["check_runs"]
        ):
            return run["id"]
    return None


def main():
    repository = os.environ["GITHUB_REPOSITORY"]

    def api(path):
        return json.loads(
            subprocess.check_output(["gh", "api", f"repos/{repository}/{path}"], text=True)
        )

    runs = api(
        "actions/workflows/build-x86-image.yaml/runs"
        "?event=workflow_dispatch&status=completed&per_page=100"
    )["workflow_runs"]
    runId = pendingExecutor(runs, repository, api)
    if runId is not None:
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"runId={runId}\n")


if __name__ == "__main__":
    main()
