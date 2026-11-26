import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import riftwire_continuum as continuum
import riftwire_projects as projects


JOB = "7d161ce0-26b3-4fa4-8326-42ebc3f2effa"
ATTEMPT = "19eb4cc7-9b73-47a8-9708-8091b671f6c9"
CHAT = "b7411b5c-c62b-4e12-8c92-01e1bc5140dd"
COORDINATOR = "f" * 64
COMMIT = "2" * 40
SUMMARY = "The fixture passed."


@unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH signature support is needed")
class ContinuumContractTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="rw-continuum-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.node = projects.ProjectStore(self.root / "worker")
        self.worker = self.node.initialize("worker")
        self.definition = projects.workflow({
            "version": 1,
            "project": "release",
            "repository": "https://example.test/teams/release",
            "name": "Release fixture",
            "steps": [
                {"id": "build", "role": "builder", "action": "build", "task": "Write the fixture.",
                 "artifacts": ["release.txt"]},
                {"id": "verify", "role": "verifier", "action": "verify", "task": "Read the fixture.",
                 "needs": ["build"], "artifacts": ["checks.txt"]},
            ],
        })

    def receipt(self, attempt=ATTEMPT, summary=f"{SUMMARY}\nUnselected fixture text.", chat=CHAT):
        assignment = {
            "version": 1,
            "coordinator": COORDINATOR,
            "member": self.worker["id"],
            "job": JOB,
            "attempt": attempt,
            "project": self.definition["project"],
            "repository": self.definition["repository"],
            "commit": COMMIT,
            "workflow_sha256": projects.object_id(self.definition),
            "step": self.definition["steps"][1],
            "brief": projects.briefing({"summary": "Unselected fixture brief."}),
            "inputs": {"build": {"result_sha256": "1" * 64, "worker": "3" * 64,
                                 "summary": "Unselected prior result.",
                                 "artifacts": {"release.txt": projects.blob(b"fixture input")}}},
            "checkpoint": None,
        }
        report = {
            "version": 1,
            "member": self.worker["id"],
            "assignment": projects.object_id(assignment),
            "outcome": "passed",
            "summary": summary,
            "artifacts": {"checks.txt": projects.blob(b"fixture output")},
            "chat": chat,
        }
        return {"worker": self.worker, "assignment": assignment,
                "envelope": {"result": report, "signature": projects.sign(self.node.key, report)}}

    def archive(self, receipt, **kwargs):
        return continuum.archive_record(projects, receipt, self.worker, self.definition, COMMIT, JOB, **kwargs)

    def test_projects_only_selected_text_link_and_evidence_digests(self):
        receipt = self.receipt()
        archived = self.archive(receipt, summary=SUMMARY, include_chat=True)
        attempt = {"coordinator": COORDINATOR, "job": JOB, "step": "verify", "attempt": ATTEMPT}
        schema = {"schema": "riftwire.continuum.result", "version": 1}
        self.assertEqual(archived, {
            **schema,
            "id": projects.object_id({**schema, **attempt}),
            "project": "release",
            "outcome": "passed",
            "provenance": {**attempt, "commit": COMMIT, "worker": self.worker["id"],
                           "workflow_sha256": projects.object_id(self.definition),
                           "receipt_sha256": projects.object_id(receipt)},
            "artifacts": {"checks.txt": projects.sha(b"fixture output")},
            "summary": SUMMARY,
            "links": {"chat": f"codex://threads/{CHAT}"},
        })
        self.assertNotIn("Unselected", json.dumps(archived))
        self.assertNotIn(self.worker["public_key"], json.dumps(archived))

    def test_reprojection_has_one_id_per_attempt(self):
        receipt = self.receipt()
        archived = self.archive(receipt)
        self.assertNotIn("summary", archived)
        self.assertNotIn("links", archived)
        self.assertEqual(archived, self.archive(json.loads(projects.canonical(receipt))))
        enriched = self.archive(receipt, summary=SUMMARY, include_chat=True)
        self.assertEqual(archived["id"], enriched["id"])
        self.assertEqual(archived["provenance"], enriched["provenance"])

        other = self.archive(self.receipt(attempt="79723145-e895-4654-b912-d68bdd8c6591", chat=None), include_chat=True)
        self.assertNotEqual(archived["id"], other["id"])
        self.assertNotIn("links", other)

        conflict = self.archive(self.receipt(summary="A different signed account of the same attempt."))
        self.assertEqual(archived["id"], conflict["id"])
        self.assertNotEqual(archived["provenance"]["receipt_sha256"], conflict["provenance"]["receipt_sha256"])

    def test_rejects_changes_to_the_signed_result_and_the_independent_expectations(self):
        receipt = self.receipt()
        tampered = copy.deepcopy(receipt)
        tampered["envelope"]["result"]["summary"] = "The result was edited."
        with self.assertRaisesRegex(ValueError, "signature"):
            self.archive(tampered)

        different_worker = projects.ProjectStore(self.root / "different").initialize("different")
        different_workflow = {**self.definition, "name": "Different workflow"}
        for worker, definition, commit, job in (
            (different_worker, self.definition, COMMIT, JOB),
            (self.worker, different_workflow, COMMIT, JOB),
            (self.worker, self.definition, "a" * 40, JOB),
            (self.worker, self.definition, COMMIT, "cbc86291-d4c0-46b2-8f75-92cc19cbf03d"),
        ):
            with self.subTest(worker=worker["name"], workflow=definition["name"], commit=commit, job=job):
                with self.assertRaisesRegex(ValueError, "expected worker, revision, workflow, or job"):
                    continuum.archive_record(projects, receipt, worker, definition, commit, job)

    def test_requires_an_exact_excerpt_when_a_summary_is_selected(self):
        with self.assertRaisesRegex(ValueError, "exact excerpt"):
            self.archive(self.receipt(), summary="An operator paraphrase.")

    def test_archive_cli_exports_from_an_isolated_python_process(self):
        receipt = self.receipt()
        receipt_path = self.root / "receipt.json"
        worker_path = self.root / "trusted-worker.json"
        workflow_path = self.root / "workflow.json"
        projects.write_json(receipt_path, receipt)
        projects.write_json(worker_path, self.worker)
        projects.write_json(workflow_path, self.definition)
        state = self.root / "uninitialized-project-state"
        entry = Path(__file__).resolve().parents[1] / "riftwire.py"

        output = subprocess.check_output([
            sys.executable, "-I", "-B", str(entry), "project", "--state", str(state), "archive", str(receipt_path),
            "--worker", str(worker_path), "--workflow", str(workflow_path), "--commit", COMMIT, "--job", JOB,
            "--summary", SUMMARY, "--include-chat",
        ], cwd=self.root, timeout=20)

        self.assertEqual(json.loads(output), self.archive(receipt, summary=SUMMARY, include_chat=True))
        self.assertFalse(state.exists())
