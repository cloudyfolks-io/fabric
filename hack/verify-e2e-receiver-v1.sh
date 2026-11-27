#!/usr/bin/env bash
# The release sender only uses this versioned default-branch endpoint.
set -euo pipefail
receiver=.github/workflows/x86-e2e-invalidate-v1.yaml
repo="repos/$GITHUB_REPOSITORY"
default=$(jq -er '.repository.default_branch' "$GITHUB_EVENT_PATH")
revision=$(gh api "$repo/git/ref/heads/$default" --jq '.object.sha')
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
for path in "$receiver" hack/e2e_invalidate.py; do
  gh api "$repo/contents/$path?ref=$revision" | jq -e --arg path "$path" \
    '.type == "file" and .path == $path and .size > 0' > /dev/null
done
gh api "$repo/actions/workflows/${receiver##*/}" | jq -e --arg path "$receiver" \
  '.path == $path and .state == "active"' > /dev/null
