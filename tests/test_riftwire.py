import concurrent.futures
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw


def uid():
    return str(uuid.uuid4())


class MailboxTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.path = Path(sandbox.name) / "mail.sqlite3"
        hosts = {"hub": "laptop", "peer": "workstation"}
        self.hub = rw.Mailbox(self.path, "hub", hosts)
        self.peer = rw.Mailbox(self.path, "peer", hosts)
        self.origin, self.target = uid(), uid()
        self.hub.enroll("build", self.origin)
        self.mid = uid()

    def send_direct(self):
        return rw.invoke(self.hub, "send", {"to": "codex://threads/"+self.target,
                         "text": 'First line\n"€ →" $() &', "message_id": self.mid}, self.origin)

    def test_direct_link_before_enrollment_and_title_afterward(self):
        sent = self.send_direct()
        self.assertEqual(sent["notice"], "pending")
        self.assertIsNone(sent["seen"])
        mail = rw.invoke(self.peer, "inbox", {}, self.target)
        self.assertEqual(mail["messages"][0]["body"], 'First line\n"€ →" $() &')
        rw.invoke(self.peer, "enroll", {"alias": "review", "title": "Peer Chat"}, self.target)
        self.assertEqual(self.hub.receipt("build", self.mid)["recipient"], "workstation/review")
        for dest in ("workstation/review", "review", "Review", "WORKSTATION/Review", "Peer Chat", self.target, sent["recipient"]):
            result = self.hub.send("build", dest, mail["messages"][0]["body"], self.mid)
            self.assertEqual(result["id"], self.mid)
        self.assertEqual(len(self.peer.inbox("review")["messages"]), 1)
        with patch.object(rw, "queue_notice", return_value=("queued", "Codex accepted")):
            reply = rw.invoke(self.peer, "send", {"to": "laptop/build", "text": "Received"}, self.target)
        self.assertEqual(reply["notice"], "queued")
        self.assertEqual(self.hub.inbox("build")["messages"][0]["body"], "Received")

    def test_ack_and_history_are_independent_from_queue_notice(self):
        self.send_direct()
        rw.invoke(self.peer, "ack", {"message_id": self.mid}, self.target)
        again = rw.invoke(self.peer, "ack", {"message_id": self.mid}, self.target)
        self.assertTrue(again["seen"])
        self.assertEqual(rw.invoke(self.peer, "inbox", {}, self.target)["messages"], [])
        self.assertEqual(len(rw.invoke(self.peer, "inbox", {"include_read": True}, self.target)["messages"]), 1)
        self.assertIsNone(self.hub.claim("workstation"))

    def test_alias_and_thread_mismatch_are_rejected(self):
        self.send_direct()
        self.peer.enroll("recipient", self.target)
        impostor = uid()
        with self.assertRaises(ValueError):
            rw.invoke(self.peer, "inbox", {"alias": "recipient"}, impostor)
        with self.assertRaises(ValueError):
            rw.invoke(self.peer, "enroll", {"alias": "recipient", "thread_id": self.target}, impostor)
        with self.assertRaises(ValueError):
            self.peer.enroll("recipient", impostor)
        with self.assertRaises(ValueError):
            self.hub.ack("build", self.mid)

    def test_colliding_titles_fail_closed(self):
        self.peer.enroll("first", self.target, title="Planning")
        self.peer.enroll("second", uid(), title="Planning")
        with self.assertRaisesRegex(ValueError, "Multiple chats"):
            self.hub.send("build", "Planning", "Hello")
        self.assertEqual(self.hub.send("build", "first", "Hello")["recipient"], "workstation/first")

    def test_input_validation_and_idempotency(self):
        self.send_direct()
        with self.assertRaisesRegex(ValueError, "different message"):
            self.hub.send("build", self.target, "changed", self.mid)
        for bad in ("codex://threads/new", "codex://settings", "laptop/build", "/wrong-role"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.hub.send("build", bad, "test")
        for body in (" ", "😀"*2049):
            with self.assertRaises(ValueError):
                self.hub.send("build", self.target, body)
        self.hub.send("build", self.target, "😀"*2048)

    def test_simultaneous_sends_store_one_message(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.send_direct()["id"], range(12)))
        self.assertEqual(set(results), {self.mid})
        self.assertEqual(len(rw.invoke(self.peer, "inbox", {}, self.target)["messages"]), 1)

    def test_concurrent_claims_and_lost_ack_recovery(self):
        self.send_direct()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            jobs = [j for j in pool.map(lambda _: self.hub.claim("workstation"), range(8)) if j]
        self.assertEqual(len(jobs), 1)
        initial = jobs[0]
        with patch.object(rw, "timestamp", return_value=rw.timestamp()+rw.LEASE+1):
            replacement = self.hub.claim("workstation")
        self.assertEqual(replacement["id"], initial["id"])
        self.assertNotEqual(replacement["token"], initial["token"])
        self.assertFalse(self.hub.settle("workstation", self.mid, initial["token"], "queued", "")["recorded"])
        self.assertFalse(self.hub.settle("laptop", self.mid, replacement["token"], "queued", "")["recorded"])
        self.assertTrue(self.hub.settle("workstation", self.mid, replacement["token"], "queued", "")["recorded"])
        self.assertIsNone(self.hub.claim("workstation"))

    def test_receipts_and_paging_survive_reopen(self):
        for i in range(4):
            self.hub.send("build", self.target, str(i))
        reopened = rw.Mailbox(self.path, "peer", self.peer.hosts)
        first = rw.invoke(reopened, "inbox", {"limit": 2}, self.target)
        second = rw.invoke(reopened, "inbox", {"limit": 2, "after": first["next_after"]}, self.target)
        self.assertEqual([m["body"] for m in first["messages"]+second["messages"]], list("0123"))
        self.assertIsNone(second["next_after"])

    def test_older_database_adds_optional_titles(self):
        legacy = self.path.parent / "legacy.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            db.execute("CREATE TABLE contacts(address TEXT PRIMARY KEY,host TEXT,thread TEXT,wake INTEGER,UNIQUE(host,thread))")
            db.execute("INSERT INTO contacts VALUES (?,?,?,?)", ("laptop/old", "laptop", self.origin, 0))
            db.commit()
        upgraded = rw.Mailbox(legacy, "hub", self.hub.hosts)
        self.assertIn("laptop/old", upgraded.contacts()["addresses"])
        self.assertEqual(upgraded.contacts()["clients"]["laptop/old"], "codex")
        self.assertFalse(rw.invoke(upgraded, "enroll", {"alias": "old"}, self.origin)["wake"])
        self.assertTrue(rw.invoke(upgraded, "set_wake", {"wake": True}, self.origin)["wake"])
        self.assertTrue(rw.invoke(upgraded, "enroll", {"alias": "old"}, self.origin)["wake"])

    def test_wake_opt_out_survives_naming_a_chat_and_restart(self):
        self.assertTrue(self.hub.enroll("new", uid())["wake"])
        self.assertFalse(rw.invoke(self.peer, "set_wake", {"wake": False}, self.target)["wake"])
        reopened = rw.Mailbox(self.path, "peer", self.peer.hosts)
        self.assertFalse(rw.invoke(reopened, "enroll", {"alias": "review"}, self.target)["wake"])
        sent = self.send_direct()
        self.assertEqual(sent["recipient"], "workstation/review")
        self.assertEqual(self.hub.claim("workstation")["wake"], 0)

    def test_notice_contains_routing_metadata_only(self):
        self.send_direct()
        job = self.hub.claim("workstation")
        import subprocess
        completed = subprocess.CompletedProcess([], 0, stdout=b"accepted", stderr=b"")
        with patch.object(rw.subprocess, "run", return_value=completed) as run:
            self.assertEqual(rw.queue_notice(job, "/fixture/codex", "/fixture/codex-home")[0], "queued")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--thread")+1], self.target)
            self.assertNotIn("First line", command[command.index("--message")+1])
            self.assertIn("independent instruction from the operator", command[command.index("--message")+1])
            self.assertIn("--resume", command)
            self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "/fixture/codex-home")
        with patch.object(rw.subprocess, "run", return_value=completed) as run:
            rw.queue_notice({**job, "wake": False}, "/fixture/codex")
            self.assertNotIn("--resume", run.call_args.args[0])
        completed.stderr = b"Warning: could not resume"
        with patch.object(rw.subprocess, "run", return_value=completed):
            self.assertEqual(rw.queue_notice({**job, "wake": True}, "/fixture/codex")[0], "queued_with_warning")

    def test_mcp_enrolls_correct_thread_and_rejects_cross_chat_read(self):
        self.send_direct()
        requests = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": "enroll", "arguments": {"alias": "receiver"},
                                                        "_meta": {"threadId": self.target}}},
            {"id": 3, "method": "tools/call", "params": {"name": "inbox", "arguments": {"alias": "receiver"},
                                                        "_meta": {"threadId": uid()}}},
            {"id": 4, "method": "tools/call", "params": {"name": "inbox", "arguments": {},
                                                        "_meta": {"threadId": self.target}}},
        ]
        wire = "".join(json.dumps({"jsonrpc":"2.0", **req})+"\n" for req in requests).encode()
        stdout = io.StringIO()
        with patch.object(rw.sys, "stdin", io.TextIOWrapper(io.BytesIO(wire))), contextlib.redirect_stdout(stdout):
            rw.mcp(lambda name, args, tid: rw.invoke(self.peer, name, args, tid))
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        self.assertTrue(responses[2]["result"]["isError"])
        inbox = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(inbox["messages"][0]["id"], self.mid)

    def test_peer_send_preserves_uuid_on_transport_failure(self):
        captured = []
        def disconnected(settings, mode, name, payload):
            captured.append(json.loads(payload))
            raise ValueError("SSH connection interrupted")
        with patch.object(rw, "peer_call", disconnected), self.assertRaises(ValueError) as failed:
            rw.remote_tool({}, "send", {"to":"laptop/build", "text":"Hello"}, self.target)
        generated = captured[0]["arguments"]["message_id"]
        self.assertIn(rw.guid(generated), str(failed.exception))
        self.assertEqual(captured[0]["thread_id"], self.target)

    def test_only_one_active_listener_per_login(self):
        with rw.receiver_lock(self.path.parent) as first, rw.receiver_lock(self.path.parent) as second:
            self.assertTrue(first)
            self.assertFalse(second)
        with rw.receiver_lock(self.path.parent) as next_owner:
            self.assertTrue(next_owner)

    def test_ssh_arguments_do_not_include_message_content(self):
        cfg = {"target":"my-alias","ssh_program":"ssh"}
        args = rw.ssh_args(cfg,"rpc","send")
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[-1], "python3 -I -B ~/.local/share/riftwire/riftwire.py rpc peer send")
        with self.assertRaises(ValueError):
            rw.ssh_args({**cfg,"target":"-other"},"rpc","send")
        with self.assertRaises(ValueError):
            rw.ssh_args(cfg,"rpc","other")


if __name__ == "__main__":
    unittest.main()
