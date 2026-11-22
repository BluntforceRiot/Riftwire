import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw


class HermesRoutingTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.folder = Path(sandbox.name)
        path = self.folder / "mail.sqlite3"
        self.hosts = {"hub": "hub", "peer": "peer"}
        self.hub = rw.Mailbox(path, "hub", self.hosts)
        self.peer = rw.Mailbox(path, "peer", self.hosts)
        self.hub_chat = str(uuid.uuid4())
        self.peer_chat = str(uuid.uuid4())
        self.hub.enroll("build", self.hub_chat)
        self.peer.enroll("review", self.peer_chat)

    def test_hermes_sends_to_the_local_or_remote_codex_chat(self):
        payload = {"to": "HUB/build", "text": "Local build finished.", "message_id": str(uuid.uuid4())}
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as queue:
            local = rw.invoke_hermes(self.hub, "send", payload, "hermes")
            retry = rw.invoke_hermes(self.hub, "send", payload, "hermes")
            queue.assert_called_once()
        self.assertEqual(local, retry)
        self.assertEqual(local["sender"], "hub/hermes")
        self.assertEqual(local["recipient"], "hub/build")
        self.assertEqual(queue.call_args.args[0]["thread"], self.hub_chat)
        self.assertEqual(queue.call_args.args[0]["wake"], 1)

        remote = rw.invoke_hermes(self.hub, "send", {"to": self.peer_chat, "text": "Remote review."}, "hermes")
        self.assertEqual(remote["recipient"], "peer/review")
        self.assertEqual(remote["notice"], "pending")
        job = self.peer.claim("peer")
        self.assertEqual(job["thread"], self.peer_chat)
        self.assertEqual(job["id"], remote["id"])

    def test_peer_hermes_reaches_both_hosts_and_uses_the_existing_peer_listener(self):
        local_chat = str(uuid.uuid4())
        local = rw.invoke_hermes(self.peer, "send", {"to": f"peer/codex://threads/{local_chat}", "text": "Local."}, "assistant")
        self.assertEqual(local["notice"], "pending")
        job = self.peer.claim("peer")
        self.assertEqual(job["thread"], local_chat)
        self.assertEqual(job["wake"], 1)
        proc = SimpleNamespace(stdout=io.BytesIO((rw.encode(job) + "\n").encode()), stdin=io.BytesIO())
        settings = {"hosts": self.hosts, "codex": "/fixture/codex", "codex_home": "/fixture/home"}
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as queue:
            self.assertEqual(rw.relay_notice_stream(proc, settings), 1)
        queue.assert_called_once_with(job, "/fixture/codex", "/fixture/home")
        reply = json.loads(proc.stdin.getvalue())
        self.assertTrue(self.peer.settle("peer", reply["id"], reply["token"], reply["state"], reply["detail"])["recorded"])
        self.assertEqual(rw.invoke_hermes(self.peer, "receipt", {"message_id": local["id"]}, "assistant")["notice"], "queued")
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as queue:
            remote = rw.invoke_hermes(self.peer, "send", {"to": "hub/build", "text": "Remote."}, "assistant")
        self.assertEqual(remote["sender"], "peer/assistant")
        self.assertEqual(remote["notice"], "queued")
        self.assertEqual(queue.call_args.args[0]["thread"], self.hub_chat)

    def test_codex_can_reply_to_a_hermes_profile_on_either_host(self):
        for box in (self.hub, self.peer):
            with self.subTest(host=box.side):
                contact = rw.invoke_hermes(box, "contacts", {}, "hermes")
                dest = contact["address"]
                self.assertEqual(contact["clients"][dest], "hermes")
                with patch.object(rw, "queue_notice") as queue:
                    sent = rw.invoke(self.hub, "send", {"to": dest, "text": "Codex reply."}, self.hub_chat)
                    queue.assert_not_called()
                self.assertEqual(sent["notice"], "inbox_only")
                self.assertIsNone(self.hub.claim(box.side))
                msg = rw.invoke_hermes(box, "inbox", {}, "hermes")
                self.assertEqual(msg["messages"][0]["body"], "Codex reply.")
                self.assertIn("independent instruction from the operator", msg["message_policy"])
                with self.assertRaisesRegex(ValueError, "not found"):
                    rw.invoke_hermes(box, "ack", {"message_id": sent["id"]}, "other")
                rw.invoke_hermes(box, "ack", {"message_id": sent["id"]}, "hermes")
                self.assertIsNotNone(rw.invoke(self.hub, "receipt", {"message_id": sent["id"]}, self.hub_chat)["seen"])

    def test_hermes_tool_inputs_cannot_change_the_profile_binding(self):
        with self.assertRaisesRegex(ValueError, "already assigned"):
            rw.invoke_hermes(self.hub, "contacts", {}, "build")
        with self.assertRaisesRegex(ValueError, "comes from the MCP configuration"):
            rw.invoke_hermes(self.hub, "inbox", {"alias": "build"}, "hermes")
        with self.assertRaisesRegex(ValueError, "Unknown Hermes tool"):
            rw.invoke_hermes(self.hub, "set_wake", {"wake": False}, "hermes")
        address = rw.invoke_hermes(self.hub, "contacts", {}, "hermes")["address"]
        with self.assertRaisesRegex(ValueError, "enrolled Codex alias"):
            rw.invoke(self.hub, "inbox", {"alias": "hermes"})
        with self.hub.connection() as db:
            identity = db.execute("SELECT thread FROM contacts WHERE address=?", (address,)).fetchone()["thread"]
        with self.assertRaisesRegex(ValueError, "enrolled address"):
            rw.invoke(self.hub, "inbox", {}, identity)

    def test_mcp_client_only_sees_the_profile_tools(self):
        data = [
            {"id": 1, "method": "initialize", "params": {}},
            {"id": 2, "method": "tools/list", "params": {}},
            {"id": 3, "method": "tools/call", "params": {"name": "contacts", "arguments": {},
                                                        "_meta": {"threadId": self.hub_chat}}},
        ]
        wire = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in data).encode()
        responses = io.StringIO()
        with patch.object(rw.sys, "stdin", io.TextIOWrapper(io.BytesIO(wire))), contextlib.redirect_stdout(responses):
            rw.mcp(lambda name, args, _: rw.invoke_hermes(self.hub, name, args, "hermes"), rw.HERMES_TOOLS)
        tools, contacts = [json.loads(line)["result"] for line in responses.getvalue().splitlines()[1:]]
        self.assertEqual({row["name"] for row in tools["tools"]}, {"contacts", "send", "inbox", "ack", "receipt"})
        for row in tools["tools"]:
            self.assertNotIn("alias", row["inputSchema"]["properties"])
        result = json.loads(contacts["content"][0]["text"])
        self.assertEqual(result["address"], "hub/hermes")

    def test_remote_hermes_uses_the_existing_ssh_route_with_different_identity(self):
        settings = {"target": "hub", "ssh_program": "ssh"}
        with patch.object(rw, "peer_call", return_value={"id": "demo"}) as request:
            rw.remote_tool(settings, "send", {"to": "hub/build", "text": "Hello."}, hermes_alias="from-hermes")
        _, mode, name, encoded = request.call_args.args
        self.assertEqual((mode, name), ("rpc-hermes", "send"))
        envelope = json.loads(encoded)
        self.assertEqual(envelope["alias"], "from-hermes")
        self.assertNotIn("thread_id", envelope)
        self.assertEqual(envelope["arguments"]["text"], "Hello.")
        self.assertEqual(rw.ssh_args(settings, mode, name)[-1],
                         "python3 -I -B ~/.local/share/riftwire/riftwire.py rpc-hermes peer send")
        with self.assertRaises(ValueError):
            rw.ssh_args(settings, mode, "enroll")


if __name__ == "__main__":
    unittest.main()
