import argparse
import concurrent.futures
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw


def chat_id():
    return str(uuid.uuid4())


class ChatCheckTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        path = Path(sandbox.name) / "mail.sqlite3"
        hosts = {"hub": "hub", "peer": "peer"}
        self.hub = rw.Mailbox(path, "hub", hosts)
        self.peer = rw.Mailbox(path, "peer", hosts)
        self.hub_id, self.peer_id = chat_id(), chat_id()
        queue = patch.object(rw, "queue_notice", return_value=("queued", "Local queue accepted"))
        self.queue = queue.start()
        self.addCleanup(queue.stop)

    def step(self, role, code=None):
        box, tid = (self.hub, self.hub_id) if role == "hub" else (self.peer, self.peer_id)
        return rw.chat_check_step(role, lambda name, args: rw.invoke(box, name, args, tid), code)

    def test_round_trip_requires_both_queues_and_reads(self):
        self.assertEqual(self.step("peer"), {"state": "waiting_for_hub"})
        invite = self.step("hub")
        self.assertEqual(invite["state"], "waiting_for_peer")
        code = invite["code"]
        hello = self.step("peer")["receipts"]["hello"]
        self.assertEqual(hello["notice"], "queued")
        self.assertIsNone(hello["seen"])
        self.assertEqual(self.step("peer", code)["state"], "in_progress")
        proof = self.step("hub", code)["receipts"]
        self.assertTrue(proof["hello"]["seen"])
        self.assertEqual(proof["reply"]["notice"], "pending")
        self.assertEqual(self.step("peer", code)["state"], "in_progress")
        job = self.peer.claim("peer")
        self.peer.settle("peer", job["id"], job["token"], "queued", "Local queue accepted")
        result = self.step("peer", code)
        self.assertEqual(result["state"], "passed")
        self.assertEqual((result["hub_thread"], result["peer_thread"]), (self.hub_id, self.peer_id))
        for receipt in result["receipts"].values():
            self.assertEqual(receipt["notice"], "queued")
            self.assertTrue(receipt["seen"])
        self.assertEqual(self.step("hub", code), result)
        self.assertEqual(self.step("peer", code), result)
        with self.hub.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.step("peer"), {"state": "waiting_for_hub"})
        self.assertNotEqual(self.step("hub")["code"], code)

    def test_test_driver_only_reads_and_acknowledges_its_own_messages(self):
        unrelated = rw.invoke(self.peer, "send", {"to": self.hub_id, "text": "Ordinary mail"}, self.peer_id)
        code = self.step("hub")["code"]
        self.step("peer", code)
        self.step("hub", code)
        remaining = rw.invoke(self.hub, "inbox", {}, self.hub_id)["messages"]
        self.assertEqual([m["id"] for m in remaining], [unrelated["id"]])
        self.assertIsNone(remaining[0]["seen"])

    def test_failed_native_notice_cannot_pass_and_fresh_test_can_start(self):
        self.queue.return_value = ("queue_failed", "Codex did not accept the notice")
        code = self.step("hub")["code"]
        result = self.step("peer", code)
        self.assertEqual(result["state"], "queue_failed")
        self.assertIsNone(result["receipts"]["hello"]["seen"])
        self.assertEqual(self.step("hub", code)["state"], "queue_failed")
        self.assertEqual(self.step("peer")["state"], "waiting_for_hub")
        self.assertNotEqual(self.step("hub")["code"], code)

    def test_ambiguous_invitations_require_code_and_participants_are_bound(self):
        code = self.step("hub")["code"]
        other = self.hub.test_session(chat_id())
        with self.assertRaisesRegex(ValueError, "Multiple setup checks"):
            self.step("peer")
        self.step("peer", code)
        with self.assertRaisesRegex(ValueError, "different peer chat"):
            self.peer.test_session(chat_id(), code)
        with self.assertRaisesRegex(ValueError, "different hub chat"):
            self.hub.test_session(other["hub_thread"], code)
        with self.assertRaisesRegex(ValueError, "distinct peer chat"):
            self.peer.test_session(other["hub_thread"], other["code"])

    def test_two_peers_cannot_claim_the_same_check(self):
        code = self.step("hub")["code"]
        def join(tid):
            try:
                return self.peer.test_session(tid, code)["peer_thread"]
            except ValueError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            winners = [tid for tid in pool.map(join, [self.peer_id, chat_id()]) if tid]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.hub.test_session(self.hub_id, code)["peer_thread"], winners[0])

    def test_expiry_and_reissue(self):
        first = self.step("hub")
        second = self.step("hub")
        with self.assertRaisesRegex(ValueError, "expired"):
            self.step("peer", first["code"])
        with patch.object(rw, "timestamp", return_value=second["expires"]):
            self.assertEqual(self.step("peer")["state"], "waiting_for_hub")
            with self.assertRaisesRegex(ValueError, "expired"):
                self.step("hub", second["code"])

    def test_mismatched_message_body_rejected(self):
        code = self.step("hub")["code"]
        session = self.peer.test_session(self.peer_id, code)
        rw.invoke(self.peer, "send", {"to": self.hub_id, "text": "Different message",
                  "message_id": session["receipts"]["hello"]["id"]}, self.peer_id)
        with self.assertRaisesRegex(ValueError, "unexpected content"):
            self.step("hub", code)

    def test_explicit_thread_cannot_override_native_chat(self):
        args = argparse.Namespace(role="hub", wait=0, code=None, thread=self.peer_id)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.hub_id}):
            with self.assertRaisesRegex(ValueError, "another chat"):
                rw.verify_chat(args)


if __name__ == "__main__":
    unittest.main()
