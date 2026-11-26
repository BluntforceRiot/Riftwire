import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw


def request(name, value):
    prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    key = "ssh-ed25519 " + base64.b64encode(prefix + bytes([value]) * 32).decode("ascii")
    return {"id": str(uuid.uuid4()), "name": name, "public_key": key,
            "version": 1, "expires": rw.timestamp() + 60}


class NetworkTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "mail.sqlite3"
        self.hosts = {"hub": "desk", "peer": "windows"}
        self.hub = rw.Mailbox(self.path, "hub", self.hosts)
        self.windows = rw.Mailbox(self.path, "peer", self.hosts)
        self.linux_request = request("debian", 1)
        self.hub.approve_device(self.linux_request)
        self.linux = rw.Mailbox(self.path, "peer", self.hosts, device_id=self.linux_request["id"])
        self.chat = str(uuid.uuid4())
        self.hub.enroll("desk-chat", str(uuid.uuid4()), wake=False)
        # A shared UUID still addresses a different chat on each boot.
        self.windows.enroll("work", self.chat, wake=True)
        self.linux.enroll("work", self.chat, wake=False)

    def test_each_boot_can_claim_only_its_own_mailbox(self):
        win = self.hub.send("desk-chat", f"windows/{self.chat}", "Windows")
        lin = self.hub.send("desk-chat", f"debian/{self.chat}", "Debian")
        self.assertEqual(self.windows.claim(self.windows.side)["id"], win["id"])
        self.assertIsNone(self.windows.claim(self.windows.side))
        self.assertEqual(self.linux.claim(self.linux.side)["id"], lin["id"])
        self.assertIsNone(self.linux.claim(self.linux.side))
        self.assertEqual(self.hub.contacts()["clients"]["debian/work"], "codex")
        self.linux.record_connection()
        self.assertIsNotNone(self.hub.contacts()["devices"]["debian"]["last_seen"])

    def test_legacy_notification_stream_does_not_consume_cross_peer_delivery(self):
        cross = self.linux.send("work", "windows/work", "A note from Debian")
        normal = self.hub.send("desk-chat", "windows/work", "A note from the hub")
        self.assertEqual(self.windows.claim("windows", legacy=True)["id"], normal["id"])
        self.assertIsNone(self.windows.claim("windows", legacy=True))
        self.assertEqual(self.windows.claim("windows")["id"], cross["id"])

    def test_pairing_is_idempotent_and_revocation_applies_to_open_receivers(self):
        self.assertEqual(self.hub.approve_device(self.linux_request)[0], self.linux_request["id"])
        with self.assertRaisesRegex(ValueError, "already assigned"):
            self.hub.approve_device(request("debian", 2))
        with self.assertRaisesRegex(ValueError, "already assigned"):
            self.hub.approve_device({**self.linux_request, "name": "another-device"})
        with self.assertRaisesRegex(ValueError, "expired"):
            self.hub.approve_device({**request("expired", 3), "expires": rw.timestamp() - 1})
        self.hub.revoke_device("debian")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.linux.claim("debian")
        with self.assertRaisesRegex(ValueError, "revoked"):
            rw.Mailbox(self.path, "peer", self.hosts, device_id=self.linux_request["id"])

    def test_hub_invitation_cannot_be_claimed_by_another_boot(self):
        tid = str(uuid.uuid4())
        code = self.hub.test_session(tid, device="debian")["code"]
        with self.assertRaisesRegex(ValueError, "different device"):
            self.windows.test_session(self.chat, code)
        self.assertEqual(self.linux.test_session(self.chat, code)["peer"], "debian/work")
        self.assertEqual(self.hub.test_session(tid, code)["peer"], "debian/work")

    def test_handoff_progress_is_scoped_to_the_receiver_and_retries_are_safe(self):
        mid = str(uuid.uuid4())
        initial = self.windows.request_handoff("work", "debian/work", "riftwire", "test", "Run the suite.", mid)
        self.assertEqual(initial["state"], "requested")
        again = self.windows.request_handoff("work", "debian/work", "riftwire", "test", "Run the suite.", mid)
        self.assertEqual(again["id"], mid)
        with self.assertRaisesRegex(ValueError, "receiving address"):
            self.windows.update_handoff("work", mid, "completed")
        with self.assertRaisesRegex(ValueError, "not found"):
            self.hub.handoffs("desk-chat", message_id=mid)
        self.linux.ack("work", mid)
        self.assertEqual(self.windows.handoffs("work", mid)["handoffs"][0]["state"], "received")
        self.linux.update_handoff("work", mid, "started")
        self.linux.update_handoff("work", mid, "completed", "All checks passed.")
        self.linux.update_handoff("work", mid, "completed", "All checks passed.")
        result = self.windows.handoffs("work", mid)["handoffs"][0]
        self.assertEqual((result["state"], result["result"]), ("completed", "All checks passed."))
        with self.assertRaisesRegex(ValueError, "already complete"):
            self.linux.update_handoff("work", mid, "started")

    def test_rooms_are_quiet_private_and_retry_as_one_post(self):
        self.hub.set_room("riftwire", ["desk/desk-chat", "windows/work", "debian/work"])
        stranger = self.windows.enroll("stranger", str(uuid.uuid4()))
        self.assertEqual(self.windows.list_rooms("stranger")["rooms"], [])
        self.assertEqual(self.linux.list_rooms("work")["rooms"], ["riftwire"])
        with self.assertRaisesRegex(ValueError, "Join"):
            self.windows.post_room(stranger["address"].split("/")[1], "riftwire", "Unauthorized post")
        post = str(uuid.uuid4())
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as queue:
            first = self.linux.post_room("work", "riftwire", "The suite passed.", post)
            second = self.linux.post_room("work", "riftwire", "The suite passed.", post)
        self.assertEqual(first["deliveries"], second["deliveries"])
        self.assertEqual(len(first["deliveries"]), 2)
        queue.assert_not_called()
        self.assertEqual({entry["notice"] for entry in first["deliveries"]}, {"inbox_only"})
        self.assertIsNone(self.windows.claim("windows"))
        self.assertEqual(self.windows.inbox("work")["messages"][0]["room"], "riftwire")
        self.assertEqual(self.linux.outbox("work")["messages"][0]["room"], "riftwire")

    def test_backup_and_restore_keep_existing_history_and_boot_identities(self):
        sent = self.hub.send("desk-chat", "debian/work", "Keep this message")
        snapshot = self.path.parent / "snapshot.sqlite3"
        restored = self.path.parent / "restored.sqlite3"
        self.assertEqual(rw.copy_mailbox(self.path, snapshot)["integrity"], "ok")
        rw.copy_mailbox(snapshot, restored)
        linux = rw.Mailbox(restored, "peer", self.hosts, device_id=self.linux_request["id"])
        self.assertEqual(linux.inbox("work")["messages"][0]["id"], sent["id"])
        self.assertEqual(linux.claim("debian")["wake"], 0)
        with self.assertRaisesRegex(ValueError, "new path"):
            rw.copy_mailbox(restored, self.path)

    def test_inbox_newest_paginates_without_changing_the_existing_cursor(self):
        messages = [self.hub.send("desk-chat", "debian/work", str(index)) for index in range(3)]
        recent = self.linux.inbox("work", newest=True, limit=2)
        self.assertEqual([m["id"] for m in recent["messages"]], [messages[2]["id"], messages[1]["id"]])
        previous = self.linux.inbox("work", newest=True, before=recent["next_before"])
        self.assertEqual(previous["messages"][0]["id"], messages[0]["id"])
        self.assertEqual(self.linux.inbox("work", limit=2)["next_after"], recent["next_before"])


if __name__ == "__main__":
    unittest.main()
