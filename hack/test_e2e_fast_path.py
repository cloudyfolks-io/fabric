import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastPathTest(unittest.TestCase):
    def test_embedded_python_compiles(self):
        import re
        import textwrap
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        blocks = re.findall(r"<<'PY'\n(.*?)\n          PY", workflow, re.S)
        self.assertEqual(len(blocks), 7)
        for index, block in enumerate(blocks):
            compile(textwrap.dedent(block), f"workflow-python-{index}", "exec")

    def test_workflow_has_no_random_memory_checks(self):
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        self.assertNotIn("$RANDOM", workflow)
        self.assertEqual(workflow.count('if [ "$CI_MEMORY_CHECK" = true ]; then'), 6)
        self.assertIn("CI_MEMORY_CHECK: ${{ github.event_name == 'schedule' }}", workflow)
        self.assertIn("sourceSHA: ${{ steps.source.outputs.sha }}", workflow)
        self.assertEqual(workflow.count("*restore-e2e-binaries"), 18)

    def test_hosted_artifact_matches_its_candidate_checkout(self):
        workflow = (ROOT / ".github/workflows/build-x86-image.yaml").read_text()
        producer = workflow.split("  build-e2e-binaries:\n")[1].split("\n  netpol-path-filter:")[0]
        hosted = workflow.split("  fabric-hosted-ovn-central-e2e:\n")[1].split("\n  chart-test:")[0]
        self.assertIn("hostedSourceSHA: ${{ steps.hosted-source.outputs.sha }}", producer)
        self.assertIn("go-version-file: go.mod", producer)
        self.assertIn('sha=$(git rev-parse HEAD)', producer)
        self.assertIn('go test -c -ldflags "-w -s"', producer)
        self.assertIn("ref: ${{ inputs.headSHA || github.sha }}", hosted)
        self.assertIn("source-sha: ${{ needs.build-e2e-binaries.outputs.hostedSourceSHA }}", hosted)
        self.assertIn("artifact-name: e2e-hosted-binaries-${{ github.run_id }}-${{ github.run_attempt }}", hosted)
        self.assertNotIn("*restore-e2e-binaries", hosted)
        self.assertIn('directory: "."', hosted)

    def test_composite_verifier_rejects_cross_source_artifacts(self):
        import os
        import shutil
        import tarfile
        import textwrap
        if shutil.which("sha256sum") is None:
            self.skipTest("GNU sha256sum required")
        action = (ROOT / ".github/actions/e2e-binaries/action.yml").read_text()
        verifier = textwrap.dedent(action.split("      run: |\n")[1])
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
