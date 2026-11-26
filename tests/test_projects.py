import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import riftwire_projects as projects


REPOSITORY = "https://example.test/teams/release"


class ProjectConnection:
    def __init__(self, coordinator, worker, roles):
        self.coordinator = coordinator
        self.worker = worker.information()
        self.invite = coordinator.grant(self.worker, "release", roles)
        self.drop_receipt = False

    def call(self, method, arguments):
        if method == "report" and self.drop_receipt:
            self.drop_receipt = False
            raise ConnectionError("Fixture link dropped after the result was saved.")
        return projects.rpc(self.coordinator, self.worker["id"], projects.request(method, arguments))


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "Git and OpenSSH are needed")
class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="rw-project-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.checkout = self.root / "source"
        self.checkout.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        (self.checkout / "example.txt").write_text("original\n", encoding="utf-8")
        self.git("add", "example.txt")
        self.git("-c", "user.email=fixture@example.test", "-c", "user.name=Fixture", "commit", "-qm", "Initial inputs")
        self.commit = self.git("rev-parse", "HEAD")
        self.hub = self.node("primary")
        self.other = self.node("contributor")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.checkout), *args], text=True, timeout=20).strip()

    def node(self, name):
        store = projects.ProjectStore(self.root / name)
        store.initialize(name)
        return store

    @staticmethod
    def definition(*steps):
        return {"version": 1, "project": "release", "repository": REPOSITORY, "name": "Release fixture", "steps": list(steps)}

    @staticmethod
    def step(name, role, action, **options):
        return {"id": name, "role": role, "action": action, "task": f"Run {name} in the fixture checkout.", **options}

    def profile(self, role, action, script, timeout=60):
        return projects.local_profile({"version": 1, "project": "release", "role": role, "repository": REPOSITORY,
                                       "checkout": str(self.checkout), "refs": ["refs/heads/main"],
                                       "actions": {action: {"command": [sys.executable, "-c", script], "timeout": timeout}}})

    def test_one_revision_from_local_build_to_independent_signed_check_and_approval(self):
        spec = self.definition(self.step("build", "builder", "build", artifacts=["release.txt"]),
                               self.step("verify", "verifier", "verify", needs=["build"], different_from=["build"]),
                               self.step("approve", "maintainer", "approve", needs=["verify"]))
        remote = ProjectConnection(self.hub, self.other, ["verifier"])
        job = self.hub.start(spec, self.commit, {"decisions": ["Use the existing checkout."]})["job"]
        with self.assertRaisesRegex(ValueError, "all required checks passed"):
            self.hub.approve(job, "approve", "Ready")
        build = self.profile("builder", "build", "from pathlib import Path\nPath('release.txt').write_bytes(b'exact bytes')")
        self.assertEqual(projects.run_worker(self.hub, build)["outcome"], "passed")
        verify = self.profile("verifier", "verify", "import os\nfrom pathlib import Path\n"
                              "assert (Path(os.environ['RIFTWIRE_PROJECT_INPUTS']) / 'build/release.txt').read_bytes() == b'exact bytes'\n"
                              "print('Read the expected input artifact')")
        self.assertEqual(projects.run_worker(self.hub, verify)["status"], "waiting")
        with self.assertRaisesRegex(ValueError, "different project or role"):
            projects.run_worker(self.other, build, remote=remote)
        self.assertEqual(projects.run_worker(self.other, verify, remote=remote)["outcome"], "passed")
        proof = self.hub.result(job, "verify")
        checked = projects.verify_record(proof, self.other.information(), spec, self.commit, job)
        self.assertEqual(checked["inputs"]["build"]["release.txt"], projects.sha(b"exact bytes"))
        tampered = copy.deepcopy(proof)
        tampered["envelope"]["result"]["summary"] = "The artifact was different."
        with self.assertRaisesRegex(ValueError, "signature"):
            projects.verify_record(tampered, self.other.information(), spec, self.commit)
        with self.assertRaisesRegex(ValueError, "expected worker, revision"):
            projects.verify_record(proof, self.other.information(), spec, "f" * 40)
        with self.assertRaisesRegex(ValueError, "Unknown project operation"):
            remote.call("approve", {"job": job, "step": "approve", "note": "Remote approval"})
        approved = self.hub.approve(job, "approve", "Approved the verified release")
        self.assertTrue(approved["steps"][-1]["approval"])
        selected = projects.project_brief(self.hub, job, ["verify"])
        self.assertEqual(selected["decisions"], ["Use the existing checkout.", "verify (passed): Read the expected input artifact"])
        self.assertIn(self.commit, selected["summary"])

    def test_parallel_patches_use_separate_worktrees(self):
        spec = self.definition(self.step("first", "builder-a", "patch"), self.step("second", "builder-b", "patch"))
        job = self.hub.start(spec, self.commit, {})["job"]
        remote = ProjectConnection(self.hub, self.other, ["builder-b"])
        first = self.profile("builder-a", "patch", "from pathlib import Path\nPath('example.txt').write_bytes(b'first\\n')")
        second = self.profile("builder-b", "patch", "from pathlib import Path\nPath('example.txt').write_bytes(b'second\\n')")
        with ThreadPoolExecutor(max_workers=2) as executor:
            runs = [executor.submit(projects.run_worker, self.hub, first),
                    executor.submit(projects.run_worker, self.other, second, remote=remote)]
            self.assertEqual([run.result()["outcome"] for run in runs], ["passed", "passed"])
        compared = projects.compare_patches(self.hub, job, "first", "second")
        self.assertIn("+first", compared["first"]["patch"])
        self.assertIn("+second", compared["second"]["patch"])
        self.assertEqual((self.checkout / "example.txt").read_text(), "original\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_worker_replays_a_saved_receipt_after_a_connection_drop(self):
        spec = self.definition(self.step("check", "checker", "test"))
        remote = ProjectConnection(self.hub, self.other, ["checker"])
        job = self.hub.start(spec, self.commit, {})["job"]
        count = self.root / "runs.txt"
        script = f"from pathlib import Path\nwith Path({str(count)!r}).open('a') as f:\n    f.write('started\\n')\n"
        verify = self.profile("checker", "test", script)
        remote.drop_receipt = True
        with self.assertRaises(ConnectionError):
            projects.run_worker(self.other, verify, remote=remote)
        self.assertEqual(self.hub.status(job)["steps"][0]["state"], "claimed")
        self.assertEqual(projects.run_worker(self.other, verify, remote=remote)["outcome"], "passed")
        self.assertEqual(count.read_text(), "started\n")
        self.hub.revoke(self.other.information()["id"], "release")
        with self.assertRaisesRegex(ValueError, "no grant"):
            remote.call("status", {"job": job})

    def test_concurrent_workers_cannot_claim_the_same_step(self):
        spec = self.definition(self.step("check", "checker", "test"))
        job = self.hub.start(spec, self.commit, {})["job"]
        links = [ProjectConnection(self.hub, node, ["checker"]) for node in (self.other, self.node("another"))]
        claim = {"project": "release", "role": "checker", "actions": ["test"], "repository": REPOSITORY}
        with ThreadPoolExecutor(max_workers=2) as executor:
            waiting = [executor.submit(link.call, "claim", claim) for link in links]
            results = [future.result() for future in waiting]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        assignment = winners[0]
        self.assertEqual(assignment["job"], job)
        winner = links[results.index(assignment)]
        self.assertEqual(winner.call("claim", claim), assignment)

    def test_another_worker_can_apply_the_signed_interruption_checkpoint(self):
        spec = self.definition(self.step("fix", "editor", "patch"))
        remote = ProjectConnection(self.hub, self.other, ["editor"])
        job = self.hub.start(spec, self.commit, {})["job"]
        interrupted = self.profile("editor", "patch", "import time\nfrom pathlib import Path\n"
                                   "Path('example.txt').write_bytes(b'preserved\\n')\ntime.sleep(10)", timeout=2)
        self.assertEqual(projects.run_worker(self.hub, interrupted)["outcome"], "interrupted")
        self.hub.requeue(job, "fix")
        resume = self.profile("editor", "patch", "from pathlib import Path\n"
                              "assert Path('example.txt').read_bytes() == b'preserved\\n'\n"
                              "Path('example.txt').write_bytes(b'finished\\n')")
        self.assertEqual(projects.run_worker(self.other, resume, remote=remote)["outcome"], "passed")
        saved = self.hub.result(job, "fix")["envelope"]["result"]
        self.assertIn(b"+finished", projects.artifact_bytes(saved["artifacts"]["riftwire.patch"]))

    def test_templates_reject_commands_and_paths_with_platform_aliases(self):
        bad = self.definition(self.step("build", "builder", "build", command=["arbitrary"]))
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            projects.workflow(bad)
        for path in ("../secret", ".git/config", ".Git/config", "dir/CON.txt", "foo/bar.", "C:/outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                projects.artifact_path(path)
