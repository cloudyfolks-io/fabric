from pathlib import Path
import unittest


class HelmCandidateTest(unittest.TestCase):
    def testInstallUsesLocallyBuiltCandidateBeforePublication(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/helm-testing.yaml").read_text()
        build = workflow.index("run: make image-fabric-amd64\n")
        create = workflow.index("- name: Create kind cluster")
        load = workflow.index("run: make kind-load-image")
        install = workflow.index("ct install --config charts/fabric/ct.yaml")
        self.assertLess(build, create)
        self.assertLess(create, load)
        self.assertLess(load, install)
        self.assertNotIn("docker push", workflow)
        self.assertIn("go-version-file: go.mod", workflow)
        self.assertIn("kubectl -n kube-system describe pods", workflow)


if __name__ == "__main__":
    unittest.main()
