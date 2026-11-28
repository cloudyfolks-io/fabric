#!/usr/bin/env bash
# The release sender only uses this versioned default-branch endpoint.
set -euo pipefail
receiver=.github/workflows/x86-e2e-invalidate-v1.yaml
repo="repos/$GITHUB_REPOSITORY"
default=$(jq -er '.repository.default_branch' "$GITHUB_EVENT_PATH")
tip() {
  gh api "$repo/git/ref/heads/$default" | jq -er --arg ref "refs/heads/$default" \
    'select(.ref == $ref and .object.type == "commit") | .object.sha'
}
revision=$(tip)
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
path=hack/e2e_invalidate.py
gh api "$repo/contents/$path?ref=$revision" | jq -e --arg path "$path" \
  '.type == "file" and .path == $path and .size > 0' > /dev/null
gh api "$repo/contents/$receiver?ref=$revision" | jq -er --arg path "$receiver" \
  'select(.type == "file" and .path == $path and .encoding == "base64") | .content' \
  | base64 --decode | python3 -I -c '
import sys
import yaml

class WorkflowLoader(yaml.SafeLoader):
    # GitHub treats "on" as a literal key. Only true/false are implicit booleans.
    yaml_implicit_resolvers = {
        key: [(tag, rule) for tag, rule in rules if tag != "tag:yaml.org,2002:bool" or key in "tTfF"]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

inputs = yaml.load(sys.stdin, Loader=WorkflowLoader)["on"]["workflow_dispatch"]["inputs"]
expected = {"prNumber": "number", "headSHA": "string", "pushedRef": "string",
            "pushedSHA": "string", "forcedPush": "boolean"}
if (any(inputs[name]["type"] != kind for name, kind in expected.items())
        or any(inputs[name].get("default", "") != "" for name in ("pushedRef", "pushedSHA"))
        or inputs["forcedPush"].get("default", False) is not False
        or any(spec.get("required", False) is not False for name, spec in inputs.items()
               if name not in {"prNumber", "headSHA"})):
    sys.exit("incompatible x86 E2E receiver dispatch inputs")
'
gh api "$repo/actions/workflows/${receiver##*/}" | jq -e --arg path "$receiver" \
  '.path == $path and .state == "active"' > /dev/null
confirmed=$(tip)
[[ "$confirmed" == "$revision" ]]
echo 'x86-e2e-invalidate-v1:push-target'
