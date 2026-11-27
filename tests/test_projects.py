import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest import mock

import riftwire_projects as projects


REPOSITORY = "https://example.test/teams/release"


class ProjectConnection:
    def __init__(self, coordinator, worker, roles):
        self.coordinator = coordinator
        self.worker = worker.information()
        self.invite = coordinator.grant(self.worker, "release", roles)
        self.drop_receipt = False
        self.dropped_report = None

    def call(self, method, arguments):
        if method == "report" and self.drop_receipt:
            self.drop_receipt = False
            self.dropped_report = arguments
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
        (self.checkout / "example.txt").write_bytes(b"original\n")
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

    def test_job_list_is_bounded_newest_first_and_read_only(self):
        empty = projects.ProjectStore(self.root / "uninitialized")
        self.assertEqual(projects.job_list(store=empty), {"jobs": [], "has_more": False})
        self.assertFalse(empty.root.exists())
        for limit in (0, 51, True, "25"):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "integers from 1 to 50"):
                projects.job_list(limit=limit, store=empty)
        spec = self.definition(self.step("check", "checker", "test", artifacts=["proof.txt"]))
        first = self.hub.start(spec, self.commit, {"summary": "Private implementation brief."})
        second = self.hub.start({**spec, "name": "Latest fixture"}, self.commit, {})
        current = projects.job_list(limit=1, store=self.hub)
        self.assertTrue(current["has_more"])
        self.assertEqual(current["jobs"], [{"job": second["job"], "project": "release", "name": "Latest fixture",
                                          "commit": self.commit, "created": second["created"], "steps": {"pending": 1}}])
        actual = projects.job_list(project="release", store=self.hub)
        self.assertEqual([row["job"] for row in actual["jobs"]], [second["job"], first["job"]])
        self.assertFalse(actual["has_more"])
        empty.root.mkdir()
        with self.assertRaisesRegex(ValueError, "job was not found"):
            projects.job_history(first["job"], store=empty)
        self.assertFalse(empty.database.exists())

    def test_remote_list_and_history_honor_the_projects_grant(self):
        spec = self.definition(self.step("check", "checker", "test"))
        hidden = self.hub.start({**spec, "project": "private"}, self.commit, {})["job"]
        visible = self.hub.start(spec, self.commit, {})["job"]
        remote = ProjectConnection(self.hub, self.other, ["checker"])
        with self.assertRaisesRegex(ValueError, "no grant"):
            remote.call("list", {"project": "private"})
        with self.assertRaisesRegex(ValueError, "no grant"):
            remote.call("history", {"job": hidden})
        with mock.patch.object(projects, "connection", return_value=remote):
            listed = projects.job_list(via="release-team", store=self.other)
            self.assertEqual([row["job"] for row in listed["jobs"]], [visible])
            self.assertEqual(projects.job_history(visible, via="release-team", store=self.other),
                             {"job": visible, "attempts": [], "next_before": None})
            self.hub.grant(self.other.information(), "private", ["checker"])
            with self.assertRaisesRegex(ValueError, "invited for a different project"):
                projects.job_list(project="private", via="release-team", store=self.other)
            self.hub.revoke(self.other.information()["id"], "release")
            with self.assertRaisesRegex(ValueError, "no grant"):
                projects.job_list(via="release-team", store=self.other)
            with self.assertRaisesRegex(ValueError, "no grant"):
                projects.job_history(visible, via="release-team", store=self.other)

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
        retried = projects.run_worker(self.other, verify, remote=remote)
        self.assertEqual(retried["outcome"], "passed")
        self.assertEqual(count.read_text(), "started\n")
        receipt = self.hub.result(job, "check")
        report = {"job": job, "step": "check", "attempt": retried["attempt"], "envelope": receipt["envelope"]}
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [executor.submit(remote.call, "report", report) for _ in range(2)]
            self.assertEqual([attempt.result() for attempt in attempts], [retried, retried])
        history = remote.call("history", {"job": job, "step": "check"})["attempts"]
        self.assertEqual([row["result_sha256"] for row in history], [retried["result_sha256"]])
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

    def test_another_worker_must_approve_the_signed_interruption_checkpoint(self):
        spec = self.definition(self.step("fix", "editor", "patch"))
        remote = ProjectConnection(self.hub, self.other, ["editor"])
        job = self.hub.start(spec, self.commit, {})["job"]
        interrupted = self.profile("editor", "patch", "import time\nfrom pathlib import Path\n"
                                   "Path('example.txt').write_bytes(b'preserved\\n')\ntime.sleep(10)", timeout=2)
        checkpoint = projects.run_worker(self.hub, interrupted)
        self.assertEqual(checkpoint["outcome"], "interrupted")
        original_receipt = self.hub.result(job, "fix")
        self.hub.requeue(job, "fix")
        reopened = projects.ProjectStore(self.hub.root)
        previous_attempt, = projects.job_history(job, store=reopened)["attempts"]
        self.assertEqual(previous_attempt["attempt"], checkpoint["attempt"])
        self.assertFalse(previous_attempt["current"])
        self.assertEqual(previous_attempt["result_sha256"], checkpoint["result_sha256"])
        self.assertEqual(reopened.result(job, "fix", attempt=checkpoint["attempt"]), original_receipt)
        with self.assertRaisesRegex(ValueError, "no signed result yet"):
            reopened.result(job, "fix")
        resume = self.profile("editor", "patch", "from pathlib import Path\n"
                              "assert Path('example.txt').read_text(encoding='utf-8') == 'preserved\\n'\n"
                              "Path('example.txt').write_bytes(b'finished\\n')")
        with mock.patch.object(projects, "run_action") as action:
            pending = projects.run_worker(self.other, resume, remote=remote)
            self.assertEqual(pending["status"], "needs-checkpoint-approval")
            self.assertEqual(pending["checkpoint_result_sha256"], checkpoint["result_sha256"])
            reviewed_patch = Path(pending["review_directory"]) / "riftwire.patch"
            self.assertEqual(reviewed_patch.read_bytes(), projects.artifact_bytes(
                original_receipt["envelope"]["result"]["artifacts"]["riftwire.patch"]))
            with self.assertRaisesRegex(ValueError, "approval does not match"):
                projects.run_worker(self.other, resume, remote=remote, approve_checkpoint=projects.sha(b"other"))
            action.assert_not_called()
        completed = projects.run_worker(self.other, resume, remote=remote,
                                        approve_checkpoint=pending["approval_digest"])
        self.assertEqual(completed["outcome"], "passed", completed.get("summary") or reopened.status(job))
        saved = self.hub.result(job, "fix")["envelope"]["result"]
        self.assertIn(b"+finished", projects.artifact_bytes(saved["artifacts"]["riftwire.patch"]))
        newest = projects.job_history(job, "fix", limit=1, store=reopened)
        self.assertEqual(newest["next_before"], completed["attempt"])
        self.assertEqual(newest["attempts"][0]["attempt"], completed["attempt"])
        self.assertTrue(newest["attempts"][0]["current"])
        older = projects.job_history(job, "fix", limit=1, before=newest["next_before"], store=reopened)
        self.assertEqual(older, {"job": job, "attempts": [previous_attempt], "next_before": None})
        proof = reopened.result(job, "fix", attempt=checkpoint["attempt"])
        checked = projects.verify_record(proof, self.hub.information(), spec, self.commit, job)
        self.assertEqual(checked["outcome"], "interrupted")
        with self.assertRaisesRegex(ValueError, "does not hold that active claim"):
            self.hub.report(self.hub.information()["id"], job, "fix", checkpoint["attempt"], proof["envelope"])
        self.assertEqual(reopened.result(job, "fix")["envelope"]["result"], saved)

    def test_abandonment_preserves_signed_history_and_requires_requeue(self):
        spec = self.definition(self.step("first", "checker", "test"),
                               self.step("second", "checker", "test", needs=["first"]))
        job = self.hub.start(spec, self.commit, {})["job"]
        profile = self.profile("checker", "test", "print('Checked the fixture.')")
        first = projects.run_worker(self.hub, profile)
        signed = self.hub.result(job, "first")
        reopened = projects.ProjectStore(self.hub.root)
        with reopened.connect() as db:
            self.assertFalse(reopened.has_attempt_events(db))
        self.assertEqual(reopened.status(job)["steps"][0]["attempt"], first["attempt"])
        self.assertEqual(reopened.result(job, "first", attempt=first["attempt"]), signed)
        original, = reopened.history(job)["attempts"]
        self.assertEqual(original["result_sha256"], first["result_sha256"])
        with self.assertRaisesRegex(ValueError, "already has a signed result"):
            reopened.abandon(job, "first", first["attempt"], "The result was delayed.")
        self.assertEqual(reopened.result(job, "first"), signed)

        remote = ProjectConnection(reopened, self.other, ["checker"])
        claim = {"project": "release", "role": "checker", "actions": ["test"], "repository": REPOSITORY}
        stranded = remote.call("claim", claim)["attempt"]
        reason = "The worker disconnected before reporting."
        args = {"job": job, "step": "second", "attempt": stranded, "reason": reason}
        with self.assertRaisesRegex(ValueError, "Unknown project operation"):
            remote.call("abandon", args)
        with self.assertRaisesRegex(ValueError, "does not have that active claim"):
            reopened.abandon(job, "second", first["attempt"], reason)
        decision = reopened.abandon(**args)
        active = reopened.status(job)["steps"][1]
        self.assertEqual((active["state"], active["attempt"], active["summary"]), ("abandoned", stranded, reason))
        self.assertIsNone(remote.call("claim", claim))
        with self.assertRaisesRegex(ValueError, "no signed result for the selected step"):
            reopened.result(job, "second", attempt=stranded)

        self.assertEqual(reopened.requeue(job, "second")["steps"][1]["state"], "ready")
        replacement = projects.run_worker(reopened, profile)
        self.assertEqual(replacement["outcome"], "passed")
        self.assertNotEqual(replacement["attempt"], stranded)
        self.assertEqual(reopened.abandon(**args), decision)
        with self.assertRaisesRegex(ValueError, "different abandonment reason"):
            reopened.abandon(job, "second", stranded, "An updated reason.")
        self.assertEqual(reopened.status(job)["steps"][1]["attempt"], replacement["attempt"])

        latest = reopened.history(job, limit=1)
        self.assertEqual(latest["attempts"][0]["result_sha256"], replacement["result_sha256"])
        middle = reopened.history(job, limit=1, before=latest["next_before"])
        abandoned, = middle["attempts"]
        self.assertEqual((abandoned["attempt"], abandoned["outcome"], abandoned["current"]), (stranded, "abandoned", False))
        self.assertEqual(abandoned["operator_decision"], decision["operator_decision"])
        self.assertNotIn("result_sha256", abandoned)
        oldest = reopened.history(job, limit=1, before=middle["next_before"])
        self.assertEqual(oldest, {"job": job, "attempts": [original], "next_before": None})
        signed_again = reopened.result(job, "first")
        self.assertEqual(projects.verify_record(signed_again, reopened.information(), spec, self.commit, job)["outcome"], "passed")

    def test_abandonment_during_receipt_verification_rejects_the_late_result(self):
        spec = self.definition(self.step("check", "checker", "test"))
        job = self.hub.start(spec, self.commit, {})["job"]
        remote = ProjectConnection(self.hub, self.other, ["checker"])
        remote.drop_receipt = True
        profile = self.profile("checker", "test", "print('Checked the fixture.')")
        with self.assertRaises(ConnectionError):
            projects.run_worker(self.other, profile, remote=remote)
        receipt = remote.dropped_report
        checking, released = Event(), Event()
        verify = projects.verify_result

        def pause_verification(*args):
            checking.set()
            if not released.wait(10):
                raise TimeoutError("The coordinator did not release the test verifier.")
            return verify(*args)

        with mock.patch.object(projects, "verify_result", side_effect=pause_verification), ThreadPoolExecutor(max_workers=1) as executor:
            report = executor.submit(remote.call, "report", receipt)
            try:
                self.assertTrue(checking.wait(10))
                decision = self.hub.abandon(job, "check", receipt["attempt"], "Author stopped responding.")
            finally:
                released.set()
            with self.assertRaisesRegex(ValueError, "abandoned this project attempt"):
                report.result(timeout=15)
        with self.assertRaisesRegex(ValueError, "abandoned this project attempt"):
            remote.call("report", receipt)
        recorded, = remote.call("history", {"job": job})["attempts"]
        self.assertEqual(recorded["operator_decision"], decision["operator_decision"])
        self.assertNotIn("result_sha256", recorded)
        self.hub.requeue(job, "check")
        with self.assertRaisesRegex(ValueError, "does not hold that active claim"):
            remote.call("report", receipt)
        self.assertEqual(self.hub.status(job)["steps"][0]["state"], "ready")

    def test_templates_reject_commands_and_paths_with_platform_aliases(self):
        bad = self.definition(self.step("build", "builder", "build", command=["arbitrary"]))
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            projects.workflow(bad)
        for path in ("../secret", ".git/config", ".Git/config", "dir/CON.txt", "foo/bar.", "C:/outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                projects.artifact_path(path)
