#!/usr/bin/env bash
# The release sender only uses this versioned default-branch endpoint.
set -euo pipefail
case "$#:${1:-}" in
  0:) proof=x86-e2e-invalidate-v1:push-target ;; # Existing v1 callers.
  1:--push-runtime|2:--push-runtime) proof=x86-e2e-invalidate-v1:push-runtime-1 ;;
  *) exit 2 ;;
esac
receiver=.github/workflows/x86-e2e-invalidate-v1.yaml
repo="repos/$GITHUB_REPOSITORY"
default=$(jq -er '.repository.default_branch' "$GITHUB_EVENT_PATH")
tip() {
  gh api "$repo/git/ref/heads/$default" | jq -er --arg ref "refs/heads/$default" \
    'select(.ref == $ref and .object.type == "commit") | .object.sha'
}
revision=$(tip)
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
if [[ $# == 2 ]]; then
  [[ "$revision" == "$2" ]]
fi
tree=$(gh api "$repo/git/commits/$revision" | jq -er --arg revision "$revision" \
  'select(.sha == $revision) | .tree.sha')
[[ "$tree" =~ ^[0-9a-f]{40}$ ]]
# The qualified receiver workflow pins its checkout and checks the verified revision before invoking this handler.
# Approve complete sets only; independently allowlisting versions of each file would allow untested mixtures.
gh api "$repo/git/trees/$tree?recursive=1" | jq -e --arg tree "$tree" '
  # A package, compiled extension or checked-in bytecode can shadow a .py import.
  def shadows_import: test("^hack/__pycache__($|/)") or
                      test("^hack/e2e_(target_history|control|selector)($|/|\\.[^/]*so$)");
  {
    ".github/workflows/x86-e2e-invalidate-v1.yaml": "11aec3780d00581c3511d96742815b4b2bbd5b73",
    "hack/e2e_invalidate.py": "59177329f50af0a2c26cf8b34c67ed2f3fa77a86",
    "hack/e2e_target_history.py": "97b52afe0dfb2c9d13f5d505f4d3c0efe8f485ee",
    "hack/e2e_control.py": "9b58f6986f5a42132bdb2c638d5233edff908da6",
    "hack/e2e_selector.py": "13923dd4c4c9f7e0127a7700da851af0f589fbcf"
  } as $qualified |
  .sha == $tree and .truncated == false and
  all(.tree[].path; shadows_import | not) and
  (INDEX(.tree[]; .path) as $files | all($qualified | to_entries[];
    $files[.key] as $file | $file.type == "blob" and
    ($file.mode == "100644" or $file.mode == "100755") and $file.sha == .value))
' > /dev/null
gh api "$repo/actions/workflows/${receiver##*/}" | jq -e --arg path "$receiver" \
  '.path == $path and .state == "active"' > /dev/null
confirmed=$(tip)
[[ "$confirmed" == "$revision" ]]
echo "$proof"
