# CI coverage and qualification

Ordinary PR/push workflows validate/build; they do not launch a second full E2E
matrix alongside the trusted dispatcher. The default-branch-controlled dispatcher,
approval protocol, immutable SHA/catalog binding, serialized gate and exact
executed SelectionPlan comparison remain authoritative.

Known feature controller paths select their affected suites plus IPv4
overlay/underlay smoke. Shared and unclassified production paths still select
all 72 entries. Recommended feature groups still require the existing approval.
Use `/test e2e-all` for final release qualification against the current candidate.
Selected PR coverage is never equivalent to a full release pass.

The existing nightly retains IPv4, IPv6, dual-stack and extra suites (including
kubevirt and chart upgrade). A weekly full run deterministically enables the
six existing Valgrind configurations instead of random instrumentation on PRs.

E2E binaries are built once per executor attempt and distributed in a tar
artifact, preserving executable permissions. A source SHA and SHA256 manifest
are checked before reuse; missing/tampered/stale binaries fail rather than
silently rebuilding. Artifacts are never fetched from other runs or caches.
Go caches remain an optimization, not evidence that a binary exists.
