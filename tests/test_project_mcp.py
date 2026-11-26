import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import riftwire


class ProjectMCPTests(unittest.TestCase):
    def test_codex_and_hermes_can_discover_and_inspect_project_jobs(self):
        projects = riftwire.project_module()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = projects.ProjectStore(folder)
            store.schema()
            job = store.start(
                {"version": 1, "project": "builds", "name": "Build fixture",
                 "repository": "https://example.test/team/fixture",
                 "steps": [{"id": "test", "role": "tester", "action": "test", "task": "Run the existing suite."}]},
                "a" * 40, {},
            )["job"]
            settings = {"hosts": {"hub": "home", "peer": "worker"}, "codex": "/fixture/codex"}
            for role, tools in (("hub", riftwire.LOCAL_TOOLS), ("peer", riftwire.LOCAL_HERMES_TOOLS)):
                with self.subTest(role=role), patch.object(projects, "default_root", return_value=folder):
                    requests = [{"id": 1, "method": "tools/list", "params": {}}]
                    calls = [
                        ("project_list", {"project": "builds", "limit": 1}),
                        ("project_status", {"job": job}),
                        ("project_history", {"job": job}),
                        ("project_list", {"store": "somewhere-else"}),
                    ]
                    for pos, (name, args) in enumerate(calls, start=2):
                        requests.append({"id": pos, "method": "tools/call", "params": {"name": name, "arguments": args}})
                    wire = "".join(json.dumps({"jsonrpc": "2.0", **request}) + "\n" for request in requests)
                    responses = io.StringIO()

                    def mailbox(*_):
                        self.fail("Project calls should use the project store or its authenticated connection.")

                    with patch.object(riftwire.sys, "stdin", io.TextIOWrapper(io.BytesIO(wire.encode()))):
                        with contextlib.redirect_stdout(responses):
                            riftwire.mcp(riftwire.with_local_directory(mailbox, folder, settings, role), tools)
                    results = [json.loads(line)["result"] for line in responses.getvalue().splitlines()]
                    self.assertTrue({"project_list", "project_status", "project_history"} <=
                                    {tool["name"] for tool in results[0]["tools"]})
                    listing, status, history = (json.loads(result["content"][0]["text"]) for result in results[1:4])
                    self.assertEqual([row["job"] for row in listing["jobs"]], [job])
                    self.assertEqual(status["steps"][0]["state"], "ready")
                    self.assertEqual(history["attempts"], [])
                    self.assertTrue(results[4]["isError"])

    def test_mailbox_protocol_excludes_project_operations(self):
        self.assertTrue(riftwire.PROJECT_TOOLS.keys().isdisjoint(riftwire.TOOLS))
        self.assertTrue(riftwire.PROJECT_TOOLS.keys().isdisjoint(riftwire.HERMES_TOOLS))


if __name__ == "__main__":
    unittest.main()
