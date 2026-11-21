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

The nightly run checks IPv4 network policy, KubeVirt, IPv4 HA with TLS, chart
upgrade, and router load balancing. Run Scheduled E2E with `full` to include
IPv6, dual-stack, and the extended compatibility suites. The weekly full run
enables Valgrind in six suites.

E2E binaries are built once per executor attempt and distributed in a tar
artifact, preserving executable permissions. A source SHA and SHA256 manifest
are checked before reuse; missing/tampered/stale binaries fail rather than
silently rebuilding. Artifacts are never fetched from other runs or caches.
Go caches remain an optimization, not evidence that a binary exists.

Base-source suites share one archive. Hosted OVN keeps its candidate-source
checkout and shares a separate candidate-bound Kamaji binary across its six
variants. Both archives are built in the same producer run, with distinct SHA
outputs; substituting one for the other is rejected, even if only release
metadata differs between those commits.
