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


def positive_id(value):
    return type(value) is int and value > 0


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
    ):
        raise ValueError("invalid timeline created_at")
    return datetime.fromisoformat(value[:-1] + "+00:00")


class TargetHistory:
    def __init__(self, pages):
        self.latest_edit = None
        self.command_dates = {}
        self.pages = []
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
                    if user["login"] == "github-actions[bot]" and user["type"] == "Bot":
                        recorded = re.fullmatch(r"<!-- x86-e2e-retarget edited_at=(\S+) -->", body)
                        if recorded:
                            edit = timestamp(recorded[1])
                    comments.append((posted, item))
                    try:
                        command = control.parseCommand(body)
                    except ValueError:
                        command = None
                    if command:
                        self.command_dates[identifier] = posted
                if edit is not None and (self.latest_edit is None or edit > self.latest_edit):
                    self.latest_edit = edit
            self.pages.append(comments)

    def is_fresh(self, generation):
        if not positive_id(generation) or generation not in self.command_dates:
            return False
        return self.latest_edit is None or self.command_dates[generation] > self.latest_edit

    def comment_pages(self):
        pages = []
        for page in self.pages:
            comments = []
            for timestamp, item in page:
                if self.latest_edit is not None and timestamp <= self.latest_edit:
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
        if name == "validate":
            command.add_argument("generation", type=int)
        else:
            command.add_argument("--generations-file", type=Path)
    args = parser.parse_args()
    if args.pr <= 0:
        parser.error("expected positive PR number")
    repository = os.environ["GITHUB_REPOSITORY"]
    pages = subprocess.check_output([
        "gh", "api", "--paginate", "--slurp",
        f"repos/{repository}/issues/{args.pr}/timeline?per_page=100",
    ])
    history = TargetHistory(json.loads(pages))
    if args.command == "validate":
        if not history.is_fresh(args.generation):
            raise ValueError("original E2E command missing or not strictly after latest target edit")
    else:
        if args.generations_file:
            generations = [identifier for identifier in history.command_dates if history.is_fresh(identifier)]
            args.generations_file.write_text(json.dumps(generations))
        print(json.dumps(history.comment_pages()))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"E2E target history: {error}")
