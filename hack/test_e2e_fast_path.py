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
        self.assertEqual(workflow.count("*restore-e2e-binaries"), 19)

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
