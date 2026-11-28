import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastPathTest(unittest.TestCase):
    def test_dispatch_builds_require_successful_selection(self):
        from hack.e2e_selector import workflowJobBlocks
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        jobs = workflowJobBlocks(workflow)
        self.assertIn("    if: github.event_name != 'workflow_dispatch' || github.actor == 'github-actions[bot]'\n",
                      jobs["e2e-selection"].split("    steps:\n")[0])
        gate = ("${{ !cancelled() && (github.event_name != 'workflow_dispatch' || "
                "needs.e2e-selection.result == 'success') }}")
        for name in ("build-fabric-base", "build-fabric-dpdk-base", "lint-ut"):
            with self.subTest(job=name):
                header = jobs[name].split("    steps:\n")[0]
                self.assertIn("    needs: e2e-selection\n", header)
                self.assertIn(f"    if: {gate}\n", header)

        # DPDK requires its gated base, even when selection failed on a non-dispatch event.
        dpdk = jobs["build-fabric-dpdk"].split("    steps:\n")[0]
        self.assertIn("    needs:\n      - build-fabric-dpdk-base\n", dpdk)
        self.assertIn("    if: ${{ !cancelled() && github.event_name != 'pull_request' && "
                      "needs.build-fabric-dpdk-base.result == 'success' }}\n", dpdk)

    def test_embedded_python_compiles(self):
        import re
        import textwrap
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        blocks = re.findall(r"<<'PY'\n(.*?)\n          PY", workflow, re.S)
        self.assertEqual(len(blocks), 8)
        for index, block in enumerate(blocks):
            compile(textwrap.dedent(block), f"workflow-python-{index}", "exec")

    def test_workflow_has_no_random_memory_checks(self):
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        self.assertNotIn("$RANDOM", workflow)
        self.assertEqual(workflow.count('if [ "$CI_MEMORY_CHECK" = true ]; then'), 6)
        self.assertIn("CI_MEMORY_CHECK: ${{ github.event_name == 'schedule' }}", workflow)
        self.assertIn("sourceSHA: ${{ steps.source.outputs.sha }}", workflow)
        self.assertEqual(workflow.count("*restore-e2e-binaries"), 18)

    def test_temporary_x86_artifacts_expire_after_one_day(self):
        import re
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        uploads = re.findall(
            r"uses: actions/upload-artifact@\S+\n        with:\n((?: {10}[^\n]+\n)+)", workflow,
        )
        one_day = set()
        for inputs in uploads:
            if "          retention-days: 1\n" in inputs:
                one_day.add(re.search(r"^          name: (.+)$", inputs, re.M).group(1))
        self.assertEqual(one_day, {
            "fabric", "fabric-dpdk", "fabric-base", "fabric-dpdk-base",
            "${{ steps.artifacts.outputs.base }}", "${{ steps.artifacts.outputs.hosted }}",
        })
        plan, = [inputs for inputs in uploads if "name: x86-e2e-selection-" in inputs]
        self.assertIn("          retention-days: 30\n", plan)
        gate = (ROOT / ".github/workflows/x86-e2e-gate.yaml").read_text()
        self.assertIn("          path: gate-mutation.json\n          retention-days: 7\n", gate)

    def test_partial_rerun_uses_retained_producer_outputs(self):
        import os
        import textwrap
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        producer = workflow.split("  build-e2e-binaries:\n")[1].split("\n  netpol-path-filter:")[0]
        script = textwrap.dedent(producer.split("id: artifacts\n        run: |\n")[1].split("\n\n")[0])
        action = (ROOT / ".github/actions/e2e-binaries/action.yml").read_text()
        guard, download = action.split("    - uses: actions/download-artifact@v8\n")
        self.assertIn("ARTIFACT_NAME: ${{ inputs.artifact-name }}", guard)
        resolve = textwrap.dedent(guard.split("      run: |\n")[1])
        self.assertIn("        name: ${{ steps.artifact.outputs.name }}\n", download)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outputs"
            resolved = Path(tmp) / "resolved"
            env = dict(os.environ, GITHUB_RUN_ID="315", GITHUB_RUN_ATTEMPT="1", GITHUB_OUTPUT=str(output))
            subprocess.run(["bash", "-ec", script], env=env, check=True)
            names = dict(line.split("=", 1) for line in output.read_text().splitlines())
            env["GITHUB_RUN_ATTEMPT"] = "2"
            for kind, job_output, expected, archive in (
                ("base", "artifactName", "e2e-binaries-315-1", "binaries.tar"),
                ("hosted", "hostedArtifactName", "e2e-hosted-binaries-315-1", "hosted-binaries/binaries.tar"),
            ):
                with self.subTest(kind=kind):
                    step_value = "${{ steps.artifacts.outputs." + kind + " }}"
                    self.assertIn(f"{job_output}: {step_value}\n", producer)
                    path = "${{ runner.temp }}/" + archive
                    self.assertIn(f"name: {step_value}\n          path: {path}\n", producer)
                    self.assertIn("artifact-name: ${{ needs.build-e2e-binaries.outputs." + job_output + " }}", workflow)
                    self.assertEqual(names[kind], expected)
                    subprocess.run(["bash", "-ec", resolve], check=True,
                                   env=dict(env, ARTIFACT_NAME=names[kind], GITHUB_OUTPUT=str(resolved)))
                    self.assertEqual(resolved.read_text(), f"name={expected}\n")
                    resolved.unlink()
            env["GITHUB_OUTPUT"] = str(Path(tmp) / "rebuilt")
            subprocess.run(["bash", "-ec", script], env=env, check=True)
            self.assertEqual(Path(env["GITHUB_OUTPUT"]).read_text().splitlines(), [
                "base=e2e-binaries-315-2", "hosted=e2e-hosted-binaries-315-2",
            ])

    def test_legacy_and_modern_artifact_name_binding(self):
        import json
        import os
        import re
        import textwrap
        action_path = ".github/actions/e2e-binaries/action.yml"
        workflow_path = ".github/workflows/build-x86-image.yaml"
        action = (ROOT / action_path).read_text()
        resolver = textwrap.dedent(action.split("      run: |\n", 1)[1].split("    - uses:", 1)[0])
        legacy = "91f3cc14492e781fb082f2f5c1b871f49952ea69"
        strict_revision = "0191cb54cfb0bc27e36521a5778f50c77511ede2"
        legacy_caller = """      - &restore-e2e-binaries
        uses: ./.github/actions/e2e-binaries
        with:
          source-sha: ${{ needs.build-e2e-binaries.outputs.sourceSHA }}
          directory: ${{ env.E2E_DIR || '.' }}
"""
        strict_action = """  artifact-name:
    required: true
    description: Same-run artifact name exported by the producer
runs:
  using: composite
  steps:
    - name: Require a producer artifact name before download
      shell: bash
      env:
        ARTIFACT_NAME: ${{ inputs.artifact-name }}
      run: '[[ "$ARTIFACT_NAME" =~ [^[:space:]] ]]'
"""
        # Candidate-control can use a shallow checkout without either pinned object.
        for rev, path, fixture in (
            (legacy, workflow_path, legacy_caller), (strict_revision, action_path, strict_action),
        ):
            shown = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True,
                                   text=True, env=dict(os.environ, GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1"))
            if shown.returncode == 0:
                self.assertIn(fixture, shown.stdout)

        workflow = (ROOT / workflow_path).read_text()
        modern_caller = workflow.split("      - &restore-e2e-binaries\n")[1].split("\n\n")[0]
        source_dir = "test/e2e/source"
        artifact = "e2e-binaries-46119093139-1"
        expressions = {
            "${{ needs.build-e2e-binaries.outputs.sourceSHA }}": legacy,
            "${{ needs.build-e2e-binaries.outputs.artifactName }}": artifact,
            "${{ env.E2E_DIR || '.' }}": source_dir,
            "${{ env.E2E_DIR }}": source_dir,
        }

        def bind_action(metadata, caller, **overrides):
            # Bind only the scalar inputs and env entries used by these action steps.
            inputs, steps = metadata.split("\nruns:\n", 1)
            defaults = {
                name: json.loads(value) for name, value in re.findall(
                    r"(?m)^  ([\w-]+):\n(?:^    .+\n)*?^    default: (.+)$", inputs,
                )
            }
            supplied = {name: expressions[value] for name, value in re.findall(r"(?m)^ {10}([\w-]+): (.+)$", caller)}
            supplied.update(overrides)
            bound = {}
            for name, value in re.findall(r"(?m)^ {8}([A-Z_]+): (.+)$", steps.split("      run:", 1)[0]):
                ref = re.fullmatch(r"\$\{\{ inputs\.([\w-]+) \}\}", value)
                bound[name] = supplied.get(ref[1], defaults.get(ref[1], "")) if ref else value
            return bound

        repo = "cloudyfolks-io/fabric"
        env = dict(os.environ, GITHUB_WORKFLOW_SHA=legacy, GITHUB_REPOSITORY=repo, GITHUB_RUN_ID="46119093139",
                   GITHUB_ACTOR="github-actions[bot]", GITHUB_EVENT_NAME="workflow_dispatch",
                   GITHUB_WORKFLOW_REF=f"{repo}/.github/workflows/build-x86-image.yaml"
                                       "@refs/heads/x86-e2e/pr-52-a-7235626292-d-46119070141")
        strict_guard = re.search(r"(?m)^      run: '(.+)'$", strict_action)[1]
        for caller, expected in ((legacy_caller, 1), (modern_caller, 0)):
            result = subprocess.run(["bash", "-ec", strict_guard],
                                    env=dict(env, **bind_action(strict_action, caller)))
            self.assertEqual(result.returncode, expected)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            env.update(GITHUB_OUTPUT=str(output), GITHUB_RUN_ATTEMPT="1")
            omitted = bind_action(action, legacy_caller)
            explicit = bind_action(action, modern_caller)
            for overrides, expected in (
                ({}, artifact),
                ({"GITHUB_RUN_ATTEMPT": "2"}, "e2e-binaries-46119093139-2"),
                (dict(explicit, GITHUB_RUN_ATTEMPT="2", GITHUB_WORKFLOW_SHA=strict_revision), artifact),
                ({"SOURCE_DIR": "."}, None),
                ({"SOURCE_SHA": "advanced-main"}, None),
                ({"GITHUB_WORKFLOW_SHA": strict_revision}, None),
                (dict(bind_action(action, modern_caller, **{"artifact-name": ""}),
                      GITHUB_WORKFLOW_SHA=strict_revision), None),
                ({"GITHUB_WORKFLOW_REF": f"{repo}/.github/workflows/build-x86-image.yaml@refs/heads/main"}, None),
                ({"GITHUB_ACTOR": "maintainer"}, None),
                ({"GITHUB_EVENT_NAME": "schedule"}, None),
            ):
                with self.subTest(overrides=overrides):
                    result = subprocess.run(["bash", "-ec", resolver], env=env | omitted | overrides)
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(output.exists())
                    else:
                        self.assertEqual(result.returncode, 0)
                        self.assertEqual(output.read_text(), f"name={expected}\n")
                        output.unlink()
            for invalid in (
                "", " ", f"\ufeff{artifact}", f"{artifact} ", f"{artifact}\r", f"{artifact}\nname=other",
                "e2e-binaries-589-1", "e2e-binaries-46119093139-0", "e2e-binaries-46119093139-01",
                "e2e-binaries-46119093139-١", "e2e-other-binaries-46119093139-1",
            ):
                with self.subTest(invalid=invalid):
                    result = subprocess.run(["bash", "-ec", resolver], env=env | bind_action(
                        action, legacy_caller, **{"artifact-name": invalid},
                    ))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())

    def test_hosted_artifact_matches_its_candidate_checkout(self):
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        producer = workflow.split("  build-e2e-binaries:\n")[1].split("\n  netpol-path-filter:")[0]
        hosted = workflow.split("  fabric-hosted-ovn-central-e2e:\n")[1].split("\n  chart-test:")[0]
        self.assertIn("hostedSourceSHA: ${{ steps.hosted-source.outputs.sha }}", producer)
        self.assertIn("go-version-file: go.mod", producer)
        self.assertIn('sha=$(git rev-parse HEAD)', producer)
        self.assertIn('go test -c -ldflags "-w -s"', producer)
        self.assertIn("path: ${{ runner.temp }}/hosted-binaries/binaries.tar", producer)
        self.assertIn("ref: ${{ inputs.headSHA || github.sha }}", hosted)
        self.assertIn("- *checkout-producer-source\n      - uses: ./test/e2e/source/.github/actions/e2e-binaries", hosted)
        self.assertIn("source-sha: ${{ needs.build-e2e-binaries.outputs.hostedSourceSHA }}", hosted)
        self.assertIn("artifact-name: ${{ needs.build-e2e-binaries.outputs.hostedArtifactName }}", hosted)
        self.assertNotIn("*restore-e2e-binaries", hosted)
        self.assertIn('directory: "."', hosted)

    def test_base_suite_checkouts_only_depend_on_binaries_when_needed(self):
        from hack.e2e_selector import workflowJobBlocks
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        jobs = workflowJobBlocks(workflow)
        self.assertNotIn("ref: ${{ github.event.repository.default_branch }}", workflow)
        self.assertIn("  E2E_DIR: test/e2e/source\n", workflow)
        producer = jobs["build-e2e-binaries"]
        self.assertIn("needs: e2e-selection", producer)
        self.assertIn("ref: ${{ github.sha }}\n          persist-credentials: false\n"
                      "          path: test/e2e/source", producer)
        self.assertIn("github.event_name == 'schedule' && '.' || 'test/e2e/source'", producer)
        checkout = jobs["k8s-conformance-e2e"].split("&checkout-producer-source\n")[1].split("\n\n")[0]
        self.assertIn("ref: ${{ needs.build-e2e-binaries.outputs.sourceSHA }}", checkout)
        self.assertIn("path: test/e2e/source", checkout)
        base_suites = {name: job for name, job in jobs.items()
                       if name != "build-e2e-binaries" and "working-directory: ${{ env.E2E_DIR }}" in job}
        self.assertEqual(len(base_suites), 21)
        source_only = set()
        for name, job in base_suites.items():
            with self.subTest(name=name):
                if "restore-e2e-binaries" in job:
                    self.assertIn("      - build-e2e-binaries\n", job.split("runs-on:")[0])
                    self.assertRegex(job, r"      - [*&]checkout-producer-source\n")
                else:
                    self.assertNotIn("build-e2e-binaries", job)
                    self.assertIn("with: *approved-e2e-source", job)
                    source_only.add(name)
        self.assertEqual(source_only, {"cyclonus-netpol-e2e", "fabric-submariner-conformance-e2e"})
        self.assertIn("with: &approved-e2e-source\n          ref: ${{ github.sha }}", producer)
        self.assertNotIn("uses: ./.github/actions/e2e-binaries", workflow)
        self.assertEqual(workflow.count("uses: ./test/e2e/source/.github/actions/e2e-binaries"), 2)

    def test_go_workspace_and_branch_are_scoped_to_base_steps(self):
        import os
        import re
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        steps = re.split(r"(?m)^      - (?=\S)", workflow)
        target = "E2E_BRANCH: ${{ needs.e2e-selection.outputs.baseRef }}"
        for step in steps:
            if "working-directory: ${{ env.E2E_DIR }}" in step:
                self.assertIn("          GOWORK: 'off'\n", step)
                self.assertIn(target, step)
            elif "go-version-file: ${{ env.E2E_DIR }}/go.mod" in step:
                self.assertRegex(step, r"env: (?:\*base-go-environment|&base-go-environment\n          GOWORK: 'off')")
            else:
                self.assertNotIn("GOWORK:", step)
                self.assertNotIn("*base-go-environment", step)
        hosted = workflow.split("- name: Run hosted OVN central E2E\n")[1].split("\n\n")[0]
        self.assertIn(target, hosted)
        producer = workflow.split("- name: Build and package source-bound binaries\n")[1].split("        run:")[0]
        gowork = re.search(r"GOWORK: '([^']+)'", producer).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            base = product / "base"
            base.mkdir()
            for directory, name in ((product, "product"), (base, "base")):
                (directory / "go.mod").write_text(f"module example.test/{name}\n\ngo 1.20\n")
            (product / "go.work").write_text("go 1.20\n\nuse .\n")
            env = dict(os.environ, GOENV="off", GOPROXY="off", GOTOOLCHAIN="local")
            env.pop("GOWORK", None)
            for directory, settings, module in ((product, env, "product"), (base, env, "product"),
                                                (base, dict(env, GOWORK=gowork), "base")):
                got = subprocess.check_output(["go", "list", "-m"], cwd=directory, env=settings, text=True)
                self.assertEqual(got.strip(), "example.test/" + module)

    def test_verified_target_branch_controls_release_version(self):
        import json
        import os
        import textwrap
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        context = textwrap.dedent(workflow.split("          headSHA=\"$GITHUB_SHA\"\n")[1].split("          prNumber=0")[0])
        dispatch = workflow.split('gh api "repos/$GITHUB_REPOSITORY/pulls/$INPUT_PR_NUMBER" > pull-request.json\n')[1]
        validator = context + textwrap.dedent(dispatch.split("            gh api --paginate")[0])
        self.assertIn("ref: ${{ github.event.pull_request.base.sha || github.workflow_sha }}", workflow)
        self.assertIn('fetch --no-tags origin "$PR_HEAD_SHA"', workflow)
        self.assertIn('echo "baseRef=$baseRef" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("baseRef: ${{ steps.context.outputs.baseRef }}", workflow)
        for pr_base, event_ref, expected in (("release-1.13", "48/merge", "release-1.13"),
                                              ("", "main", "main"), ("", "release-1.13", "release-1.13"),
                                              ("", "release-next", None), ("release-1.13\n", "48/merge", None)):
            result = subprocess.run(
                ["bash", "-eu", "-c", context + '\nprintf "%s" "$baseRef"'], text=True, capture_output=True,
                env=dict(os.environ, EVENT_NAME="pull_request" if pr_base else "push",
                         GITHUB_BASE_REF=pr_base, GITHUB_REF_NAME=event_ref),
            )
            if expected is None:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported x86 E2E base branch", result.stderr)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        head, base = "a" * 40, "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            request = {"state": "open", "head": {"sha": head}, "base": {"sha": base}}
            env = dict(os.environ, EVENT_NAME="workflow_dispatch", INPUT_HEAD_SHA=head, INPUT_BASE_SHA=base, INPUT_PR_NUMBER="48",
                       GITHUB_SHA=base, GITHUB_WORKFLOW_SHA=base, checkedOutSHA=base,
                       GITHUB_REF_NAME="x86-e2e/pr-48-a-1-d-2")
            command = ["bash", "-eu", "-c", validator + 'printf "%s" "$baseRef"']
            for branch, version in (("release-1.13", "1.13"), ("main", "999.999"), ("feature/test", None),
                                    ("release-next", None), ("release-1.13-mc", None), ("main\n", None)):
                request["base"]["ref"] = branch
                (Path(tmp) / "pull-request.json").write_text(json.dumps(request))
                result = subprocess.run(command, cwd=tmp, env=dict(env, INPUT_BASE_REF=branch),
                                        capture_output=True, text=True)
                if version is None:
                    self.assertNotEqual(result.returncode, 0)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), branch)
                parsed = subprocess.check_output(
                    ["make", "-s", "-f", str(ROOT / "makefiles/e2e.mk"), "-f", "-", "version"],
                    input='version:\n\t@echo "$(VER_MAJOR).$(VER_MINOR)"\n', text=True,
                    env=dict(os.environ, E2E_BRANCH=result.stdout.strip()), cwd=tmp,
                )
                self.assertEqual(parsed.strip(), version)
            request["base"]["ref"] = "main"
            (Path(tmp) / "pull-request.json").write_text(json.dumps(request))
            for key, value in (("INPUT_BASE_REF", "release-1.13"), ("GITHUB_SHA", head),
                               ("GITHUB_WORKFLOW_SHA", head), ("checkedOutSHA", head),
                               ("INPUT_BASE_SHA", head), ("INPUT_HEAD_SHA", base)):
                with self.subTest(key=key):
                    settings = dict(env, INPUT_BASE_REF="main")
                    settings[key] = value
                    result = subprocess.run(command, cwd=tmp, env=settings, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)

    def test_executed_plan_binds_base_even_on_fallback(self):
        import json
        import os
        import sys
        import textwrap
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        compute = workflow.split("      - name: Compute the selection plan\n")[1].split("\n      - name:")[0]
        script = textwrap.dedent(compute.split("        run: |\n")[1])
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / ".github").symlink_to(ROOT / ".github", target_is_directory=True)
            (checkout / "hack").mkdir()
            # Enforce the pre-rollout argument contract without needing Git history in CI.
            (checkout / "hack/e2e_selector.py").write_text(textwrap.dedent(f"""\
                import argparse
                import runpy
                parser = argparse.ArgumentParser(allow_abbrev=False)
                for option in ('paths-file', 'event-file', 'request-groups-json', 'head-sha',
                               'force-full-reason', 'plan-file', 'summary-file', 'label'):
                    parser.add_argument('--' + option)
                parser.parse_args()
                runpy.run_path({str(ROOT / 'hack/e2e_selector.py')!r}, run_name='__main__')
            """))
            (checkout / "selector-event.json").write_text('{"pull_request":{"labels":[]}}')
            env = dict(os.environ, HEAD_SHA="a" * 40, BASE_REF="release-1.13", BASE_SHA="b" * 40,
                       FORCE_FULL="false", FORCE_FULL_REASON="", REQUESTED_GROUPS="[]", GITHUB_STEP_SUMMARY="summary")
            for fallback in (True, False):
                if not fallback:
                    (checkout / "changed-paths.txt").write_bytes(b"")
                subprocess.run(["bash", "-eu", "-c", script], cwd=tmp, env=env, check=True)
                subprocess.run([sys.executable, str(ROOT / "hack/e2e_selector.py"), "--head-sha", env["HEAD_SHA"],
                                "--paths-file", "changed-paths.txt", "--plan-file", "gate-plan.json",
                                "--base-ref", env["BASE_REF"], "--base-sha", env["BASE_SHA"]], cwd=tmp, check=True)
                result = json.loads((checkout / "e2e-selection-plan.json").read_text())
                self.assertEqual(result, json.loads((checkout / "gate-plan.json").read_text()))
                self.assertEqual((result["baseRef"], result["baseSHA"]), (env["BASE_REF"], env["BASE_SHA"]))
                self.assertEqual(result["catalogRevision"] == "", fallback)

    def test_base_advance_preserves_source_and_rejects_wrong_build_or_restore(self):
        import os
        import shutil
        import textwrap
        if shutil.which("sha256sum") is None:
            self.skipTest("GNU sha256sum required")
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        step = workflow.split("      - name: Build and package source-bound binaries\n")[1].split("\n\n")[0]
        self.assertIn("working-directory: ${{ env.E2E_DIR }}", step)
        build = textwrap.dedent(step.split("        run: |\n")[1])
        action = (ROOT / ".github/actions/e2e-binaries/action.yml").read_text()
        verify = textwrap.dedent(action.rsplit("      run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-qb", "main"], cwd=root, check=True)
            (root / "Makefile").write_text("e2e-build:\n\tmkdir -p test/e2e\n\tprintf fixture > test/e2e/base.test\n")
            subprocess.run(["git", "add", "Makefile"], cwd=root, check=True)
            commit = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit"]
            subprocess.run(commit + ["-qm", "approved"], cwd=root, check=True)
            approved = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            subprocess.run(commit + ["--allow-empty", "-qm", "base advanced"], cwd=root, check=True)
            env = dict(os.environ, GITHUB_SHA=approved, RUNNER_TEMP=tmp, GITHUB_OUTPUT=str(root / "outputs"))
            self.assertNotEqual(subprocess.run(["bash", "-ec", build], cwd=root, env=env).returncode, 0)
            self.assertFalse((root / "outputs").exists())
            self.assertFalse((root / "test/e2e/base.test").exists())
            subprocess.run(["git", "checkout", "-q", "--detach", approved], cwd=root, check=True)
            result = subprocess.run(["bash", "-ec", build], cwd=root, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "outputs").read_text(), f"sha={approved}\n")
            env.update(SOURCE_DIR=tmp, SOURCE_SHA=approved, ARCHIVE=str(root / "binaries.tar"))
            subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
            self.assertNotEqual(subprocess.run(["bash", "-ec", verify], env=env).returncode, 0)
            subprocess.run(["git", "checkout", "-q", "--detach", approved], cwd=root, check=True)
            subprocess.run(["bash", "-ec", verify], env=env, check=True, capture_output=True)
            (root / "test/e2e/base.test").write_text("modified")
            subprocess.run(["tar", "-cf", env["ARCHIVE"], ".e2e-prebuilt", ".e2e-binaries.sha256",
                            "test/e2e/base.test"], cwd=root, check=True)
            result = subprocess.run(["bash", "-ec", verify], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"FAILED", result.stdout)

    def test_hosted_packager_round_trips_through_actual_verifier(self):
        import os
        import re
        import shutil
        import textwrap
        if shutil.which("sha256sum") is None:
            self.skipTest("GNU sha256sum required")
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        block = re.search(r"      - name: Package candidate-bound Hosted OVN binary\n"
                          r"        id: hosted-source\n        run: \|\n(.*?)\n      - uses:",
                          workflow, re.S).group(1)
        packager = textwrap.dedent(block)
        action = (ROOT / ".github/actions/e2e-binaries/action.yml").read_text()
        verifier = textwrap.dedent(action.rsplit("      run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner, tools = (root / name for name in ("source", "runner", "tools"))
            for directory in (source, runner, tools):
                directory.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "head"], cwd=source, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
            # A scheduled producer can share this SHA while ignoring a candidate go.work.
            workspace = source / "go.work"
            workspace.write_text("go 1.20\n\nuse .\n")
            (source / "go.mod").write_text("module example.test/candidate\n\ngo 1.20\n")
            (source / ".e2e-prebuilt").write_text(sha + "\n")
            binary = source / "test/e2e/kamaji/kamaji.test"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"base-fixture")
            binary.chmod(0o755)
            go = tools / "go"
            go.write_text("""#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1:] == ["env", "GOWORK"]:
    os.execv(os.environ["REAL_GO"], [os.environ["REAL_GO"], *sys.argv[1:]])
target = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(b"hosted-fixture")
target.chmod(0o755)
""")
            go.chmod(0o755)
            env = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}", REAL_GO=shutil.which("go"),
                       GOWORK=str(workspace), GOTOOLCHAIN="local", GOPROXY="off",
                       RUNNER_TEMP=str(runner), GITHUB_OUTPUT=str(root / "output"))
            subprocess.run(["bash", "-c", packager], cwd=source, env=env, check=True)
            archive = runner / "hosted-binaries/binaries.tar"
            self.assertTrue(archive.is_file(), "artifact must retain the verifier's binaries.tar basename")
            # download-artifact restores the producer's basename, not its parent.
            download_input = re.search(r"^        path: \$\{\{ runner.temp \}\}/(.+)$", action, re.M).group(1)
            archive_input = re.search(r"^        ARCHIVE: \$\{\{ runner.temp \}\}/(.+)$", action, re.M).group(1)
            download = runner / download_input
            download.mkdir()
            shutil.copy(archive, download / archive.name)
            self.assertEqual(download / archive.name, runner / archive_input)
            (source / "test/e2e/kamaji/kamaji.test").unlink()
            env.update(SOURCE_DIR=str(source), SOURCE_SHA=sha,
                       ARCHIVE=str(runner / archive_input))
            subprocess.run(["bash", "-c", verifier], env=env, check=True, capture_output=True)
            self.assertTrue(os.access(source / "test/e2e/kamaji/kamaji.test", os.X_OK))
            self.assertEqual(binary.read_bytes(), b"hosted-fixture")

    def test_composite_verifier_rejects_cross_source_artifacts(self):
        import os
        import shutil
        import tarfile
        import textwrap
        if shutil.which("sha256sum") is None:
            self.skipTest("GNU sha256sum required")
        action = (ROOT / ".github/actions/e2e-binaries/action.yml").read_text()
        verifier = textwrap.dedent(action.rsplit("      run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shas, trees, archives = [], [], []
            for name in ("base", "head"):
                tree = root / name
                tree.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
                subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "commit", "--allow-empty", "-qm", name], cwd=tree, check=True)
                sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree).decode().strip()
                binary = tree / "test/e2e/kamaji/kamaji.test"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(name.encode())
                binary.chmod(0o755)
                (tree / ".e2e-prebuilt").write_text(sha + "\n")
                (tree / ".e2e-binaries.sha256").write_text(
                    hashlib.sha256(binary.read_bytes()).hexdigest() + "  test/e2e/kamaji/kamaji.test\n")
                archive = root / f"{name}.tar"
                with tarfile.open(archive, "w") as tar:
                    for path in (".e2e-prebuilt", ".e2e-binaries.sha256", "test/e2e/kamaji/kamaji.test"):
                        tar.add(tree / path, arcname=path)
                shas.append(sha)
                trees.append(tree)
                archives.append(archive)
            for checkout, artifact, expected, passed in (
                (0, 0, 0, True), (1, 1, 1, True),
                (1, 0, 0, False), (1, 0, 1, False),
            ):
                with self.subTest(checkout=checkout, artifact=artifact, expected=expected):
                    env = dict(os.environ, SOURCE_DIR=str(trees[checkout]),
                               SOURCE_SHA=shas[expected], ARCHIVE=str(archives[artifact]))
                    result = subprocess.run(["bash", "-c", verifier], env=env, capture_output=True)
                    self.assertEqual(result.returncode == 0, passed, result.stderr)

    def test_nightly_extra_coverage_retained(self):
        workflow = (ROOT / ".github/workflows/scheduled-e2e.yaml").read_text()
        self.assertIn("  kubevirt-e2e:", workflow)
        self.assertIn("chart-upgrade", workflow)

    def test_prebuilt_rejects_missing_tampered_or_wrong_source(self):
        # The production verifier uses GNU sha256sum, available on CI/Linux.
        import shutil
        if shutil.which("sha256sum") is None:
            self.skipTest("GNU sha256sum required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "source"], cwd=root, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
            binary = root / "test/e2e/fabric/fabric.test"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            (root / ".e2e-prebuilt").write_text(sha + "\n")
            manifest = root / ".e2e-binaries.sha256"
            manifest.write_text(hashlib.sha256(b"fixture").hexdigest() + "  test/e2e/fabric/fabric.test\n")
            command = ["bash", str(ROOT / "hack/e2e-prebuilt.sh"), "./test/e2e/fabric"]
            self.assertEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            binary.write_bytes(b"tampered")
            self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            binary.unlink()
            self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            (root / ".e2e-prebuilt").write_text("wrong-source\n")
            self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
