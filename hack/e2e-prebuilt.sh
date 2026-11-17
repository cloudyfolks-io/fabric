#!/usr/bin/env bash
set -euo pipefail

# Never silently rebuild a missing or mismatched binary in artifact mode.
test "$(cat .e2e-prebuilt)" = "$(git rev-parse HEAD)"
for directory in "$@"; do
  binary="$directory/$(basename "$directory").test"
  test -x "$binary"
  grep -F "  ${binary#./}" .e2e-binaries.sha256 | sha256sum --check --strict -
done
