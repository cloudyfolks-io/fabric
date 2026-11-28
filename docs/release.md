# Release

`.github/workflows/release.yaml` is the only release path. It builds and
pushes the images to `ghcr.io/cloudyfolks-io`, packages the Helm chart,
pushes it to `oci://ghcr.io/cloudyfolks-io/charts` and makes the GitHub
release. Nothing is released from a workstation.

## Base image

`dist/images/Dockerfile` starts from `ghcr.io/cloudyfolks-io/fabric-base`.
The file `BASE_VERSION` holds the tag of that base image. The workflow
builds the base image from `dist/images/Dockerfile.base` and
`dist/images/Dockerfile.base-dpdk` when the registry has no image for the
tag in `BASE_VERSION`. It pushes these tags:

- `fabric-base:<BASE_VERSION>` for amd64 and arm64
- `fabric-base:<BASE_VERSION>-debug` for amd64 and arm64
- `fabric-base:<BASE_VERSION>-amd64-legacy`
- `fabric-base:<BASE_VERSION>-dpdk`

To ship a new base image, change `BASE_VERSION` in the same pull request as
the change to the base Dockerfiles. The next tag push or `workflow_dispatch`
run builds and pushes the new base tags before it builds the `fabric` image.
Pull request CI builds the base image only when the base inputs change.
Other CI runs pull the published tags with `make pull-base`.

## x86 E2E gate rollout

1. Drain gate and dispatcher runs from the old revision, including queued
   retries. Do not rerun old workflow revisions during rollout.
2. Install `.github/workflows/x86-e2e-invalidate-v1.yaml`, its control code,
   the verifier, gate and sender together on `main`. Enable the v1 workflow.
3. Only then create numeric release branches from upgraded code, or upgrade an
   existing release through its normal PR checks. The release selection check
   and push sender verify the active v1 receiver on `main`. An old release
   sender still addresses its old gate; installing a receiver cannot redirect it.

Keep the v1 endpoint and verifier available until all v1 senders are retired.
Install the receiver with optional `pushedRef`, `pushedSHA` and `forcedPush` inputs on
`main` before enabling senders that include them. Earlier v1 senders omit
these inputs; the upgraded receiver still checks the current branch tip.
For a delayed delivery on the same branch, keeping a newer success requires
proof that the current tip descends from the pushed revision. After an
ambiguous force-push, authorize again for the current target.
Introduce incompatible changes under a new endpoint version.

If the receiver was disabled or rollout was interrupted, restore it first,
then rerun the failed base-update sender to reconcile existing fixed checks.
Upgrade the branch-bound executor and catalog on each release before accepting
fresh approvals there.

## Cut a release

1. Set the new version in `VERSION`, for example `v1.1.0`.
2. Run `make sync-version`. It copies `VERSION` into the chart image
   tags, the chart version, the chart appVersion and the CRD subchart
   version. `make verify-version` runs in CI and fails when they drift.
   Then run `helm-docs` in `charts/fabric` to regenerate `README.md`
   from `values.yaml`. CI does not check the chart README.
3. Commit both changes and merge them to `main`.
4. Push the tag: `git tag v1.1.0 && git push origin v1.1.0`.

The tag push starts the workflow. It refuses to publish when the chart
version does not match the tag.

## Test build

Run the workflow with `workflow_dispatch` to build and push a
`dev-<short sha>` image and a `0.0.0-dev-<short sha>` chart. This makes
no GitHub release and no tag.
