#!/usr/bin/env python3
"""Filter x86 E2E evidence using one PR timeline, before selecting or merging it."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import e2e_control as control


def retarget_marker(stamp):
    return f"<!-- x86-e2e-retarget edited_at={stamp} -->"


def trusted_bot(item):
    user = item["user"]
    return user["login"] == "github-actions[bot]" and user["type"] == "Bot"


def positive_id(value):
    return type(value) is int and value > 0


def issued_generation(check):
    lines = ((check.get("output") or {}).get("text") or "").splitlines()
    markers = [line for line in lines if line.startswith("x86-e2e-gate")]
    if len(markers) == 1:
        match = re.fullmatch(r"x86-e2e-gate request=[0-9a-f]{64} approval=([1-9][0-9]*)", markers[0])
        if match:
            return int(match[1])
    return None


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
    ):
        raise ValueError("invalid timeline created_at")
    return datetime.fromisoformat(value[:-1] + "+00:00")


class TargetHistory:
    def __init__(self, pages, checks=()):
        self.latest_edit = None
        self.edits = set()
        self.recorded = set()
        self.unknown_cutoff = False
        self.command_dates = {}
        self.pages = []
        cutoffs = []
        if not isinstance(pages, list) or not pages:
            raise ValueError("missing timeline pages")
        seen_ids = set()
        for page in pages:
            if not isinstance(page, list):
                raise ValueError("invalid timeline page")
            comments = []
            for item in page:
                edit = None
                if (not isinstance(item, dict) or not isinstance(item.get("event"), str)
                        or not re.fullmatch(r"[a-z][a-z0-9_-]*", item["event"])):
                    raise ValueError("invalid timeline event")
                if item["event"] == "base_ref_changed":
                    edit = timestamp(item.get("created_at"))
                elif item["event"] == "commented":
                    identifier = item.get("id")
                    body = item.get("body")
                    user = item.get("user")
                    if (not positive_id(identifier) or not isinstance(body, str)
                            or not isinstance(user, dict)
                            or not all(isinstance(user.get(k), str) and user[k] for k in ("login", "type"))):
                        raise ValueError("invalid timeline comment")
                    if identifier in seen_ids:
                        raise ValueError("duplicate timeline comment id")
                    seen_ids.add(identifier)
                    posted = timestamp(item.get("created_at"))
                    if trusted_bot(item):
                        recorded = re.fullmatch(r"<!-- x86-e2e-retarget edited_at=(\S+) -->", body)
                        if recorded:
                            edit = timestamp(recorded[1])
                            self.recorded.add(edit)
                        failed = re.fullmatch(r"<!-- x86-e2e-retarget-failure edited_at=(\S+) -->", body)
                        if failed:
                            self.edits.add(timestamp(failed[1]))
                            edit = posted
                            cutoffs.append(posted)
                    comments.append((posted, item))
                    try:
                        command = control.parseCommand(body)
                    except ValueError:
                        command = None
                    if command:
                        self.command_dates[identifier] = posted
                if edit is not None:
                    self.edits.add(edit)
            self.pages.append(comments)

        self.superseded = set()
        superseded_ids = set()
        for check in checks:
            output = check["output"]
            lines = (output.get("text") or "").splitlines()
            summary = (output.get("summary") or "").splitlines()
            if summary[:1] == [control.supersededSummary]:
                superseded_ids.add(check["id"])
                if summary[1:]:
                    generation = issued_generation(check)
                    if not generation or summary[1:] != [f"{control.supersededPrefix}{generation}"]:
                        raise ValueError("invalid superseded approval marker")
                    self.superseded.add(generation)
            if check.get("conclusion") == "action_required" and summary[:1] == [control.invalidationSummary]:
                lines.extend(summary[1:])
                if not any(line.startswith(control.cutoffPrefix) for line in summary[1:]):
                    # Legacy PATCH retained the command, which can predate the downgrade.
                    try:
                        cutoffs.append(timestamp(check.get("completed_at")))
                    except ValueError:
                        self.unknown_cutoff = True
            cutoffs.extend(timestamp(line[len(control.cutoffPrefix):]) for line in lines
                           if line.startswith(control.cutoffPrefix))
        self.invalidation = max(cutoffs, default=None)
        if superseded_ids and max(check["id"] for check in checks) in superseded_ids:
            raise ValueError("the current issued check is missing from the API response")
        self.latest_edit = max(self.edits, default=None)
        if self.invalidation and (self.latest_edit is None or self.invalidation > self.latest_edit):
            self.latest_edit = self.invalidation

    def is_fresh(self, generation):
        if (self.unknown_cutoff or not positive_id(generation) or generation not in self.command_dates
                or generation in self.superseded):
            return False
        return self.latest_edit is None or self.command_dates[generation] > self.latest_edit

    def comment_pages(self):
        pages = []
        for page in self.pages:
            comments = []
            for timestamp, item in page:
                if self.unknown_cutoff or (self.latest_edit is not None and timestamp <= self.latest_edit):
                    continue
                user = item["user"]
                if user.get("login") == "github-actions[bot]" and user.get("type") == "Bot":
                    body = item["body"]
                    fresh = True
                    for kind, parse in (("intent", control.parseApprovalIntent),
                                        ("request", control.parseRequestMarker)):
                        if f"<!-- x86-e2e-{kind} " in body:
                            try:
                                marker = parse(body)
                            except (ValueError, TypeError):
                                marker = None
                            if not marker or not self.is_fresh(marker["approvalGeneration"]):
                                fresh = False
                    if not fresh:
                        continue
                comments.append(item)
            pages.append(comments)
        return pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("comments", "validate"):
        command = commands.add_parser(name)
        command.add_argument("pr", type=int)
        command.add_argument("--head", required=True)
        if name == "validate":
            command.add_argument("generation", type=int, nargs="+")
            command.add_argument("--failed-executor", nargs=2, metavar=("RUN_FILE", "CATALOG_REVISION"))
        else:
            command.add_argument("--generations-file", type=Path)
            command.add_argument("--check-file", type=Path)
            command.add_argument("--cutoff-file", type=Path)
            command.add_argument("--persist-cutoff", action="store_true")
    args = parser.parse_args()
    if args.pr <= 0:
        parser.error("expected positive PR number")
    if not re.fullmatch(r"[0-9a-f]{40}", args.head):
        parser.error("expected PR HEAD revision")
    repository = os.environ["GITHUB_REPOSITORY"]

    def api(path, paginate):
        flags = ["--paginate", "--slurp"] if paginate else []
        return json.loads(subprocess.check_output(["gh", "api", *flags, f"repos/{repository}/{path}"]))

    checks = control.fixedChecks(args.pr, args.head, api)
    history = TargetHistory(api(f"issues/{args.pr}/timeline?per_page=100", paginate=True), checks)
    if args.command == "validate":
        if args.failed_executor:
            run_file, catalog_revision = args.failed_executor
            run = json.loads(Path(run_file).read_text())
            metadata = control.parseExecutorRunName(run["display_title"])
            metadata.update(baseSHA=run["head_sha"], catalogRevision=catalog_revision)
            check = max(checks, key=lambda item: item["id"], default={})
            if (run.get("status") != "completed" or run.get("conclusion") != "failure"
                    or metadata["prNumber"] != args.pr or metadata["headSHA"] != args.head
                    or not control.matchesExecutorCheck(check, metadata)):
                raise ValueError("failed executor no longer matches the fixed check")
            args.generation.append(metadata["approvalGeneration"])
        if not all(history.is_fresh(generation) for generation in args.generation):
            raise ValueError("original E2E command missing or not strictly after latest target edit or invalidation")
    else:
        if args.persist_cutoff and history.invalidation and history.invalidation not in history.recorded:
            # An older writer can replace the entire check output after this writer releases the lock.
            stamp = history.invalidation.isoformat().replace("+00:00", "Z")
            path = f"repos/{repository}/issues/{args.pr}/comments"
            subprocess.run(["gh", "api", path, "-f", f"body={retarget_marker(stamp)}"], check=True)
            visible = TargetHistory(api(f"issues/{args.pr}/timeline?per_page=100", paginate=True))
            if history.invalidation not in visible.recorded:
                raise ValueError("invalidation cutoff not visible in PR timeline")
        if args.cutoff_file:
            if history.unknown_cutoff:
                raise ValueError("invalidated check missing valid completed_at")
            marker = ""
            if history.invalidation:
                marker = control.cutoffPrefix + history.invalidation.isoformat().replace("+00:00", "Z")
            args.cutoff_file.write_text(marker)
        if args.generations_file:
            generations = [identifier for identifier in history.command_dates if history.is_fresh(identifier)]
            args.generations_file.write_text(json.dumps(generations))
        if args.check_file:
            args.check_file.write_text(json.dumps(max(checks, key=lambda check: check["id"], default={})))
        print(json.dumps(history.comment_pages()))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"E2E target history: {error}")
