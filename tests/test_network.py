import base64
from concurrent.futures import ThreadPoolExecutor
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
        self.hub_thread = str(uuid.uuid4())
        self.hub.enroll("desk-chat", self.hub_thread, wake=False)
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

    def test_retry_copies_to_the_selected_boot_and_preserves_the_original(self):
        original = self.hub.send("desk-chat", "windows/work", "Continuum brief")
        args = {"message_id": original["id"], "to": f"debian/{self.chat}"}
        copied = rw.invoke(self.hub, "retry_message", args, self.hub_thread)
        self.assertEqual((copied["recipient"], copied["retry_of"]), ("debian/work", original["id"]))
        self.assertNotEqual(copied["id"], original["id"])
        self.assertEqual(self.hub.receipt("desk-chat", original["id"]), original)
        self.assertEqual(self.hub.outbox("desk-chat")["messages"][0]["retry_of"], original["id"])
        replay = self.linux.inbox("work")["messages"][0]
        self.assertEqual((replay["id"], replay["retry_of"], replay["body"]),
                         (copied["id"], original["id"], "Continuum brief"))
        self.assertEqual(self.windows.inbox("work")["messages"][0]["id"], original["id"])
        self.windows.ack("work", original["id"])
        self.assertEqual(rw.invoke(self.hub, "retry_message", args, self.hub_thread), copied)

    def test_concurrent_retry_is_one_copy_even_after_the_target_gets_an_alias(self):
        original = self.hub.send("desk-chat", "windows/work", "Queued mail")
        thread = str(uuid.uuid4())
        old_alias = self.linux.alias_for(thread)
        destinations = [f"debian/{thread}", f"debian/{old_alias}",
                        f"debian/codex://threads/{thread}"]

        def reroute(to):
            return self.hub.retry_message("desk-chat", original["id"], to)

        with ThreadPoolExecutor(max_workers=3) as pool:
            copies = list(pool.map(reroute, destinations))
        first = copies[0]
        self.assertEqual(copies, [first] * len(destinations))
        self.linux.enroll("new-alias", thread)
        for target in (*destinations, "debian/new-alias"):
            updated = reroute(target)
            self.assertEqual(updated["id"], first["id"])
            self.assertEqual(updated["recipient"], "debian/new-alias")
        mailbox = self.linux.inbox("new-alias")["messages"]
        self.assertEqual([entry["id"] for entry in mailbox], [first["id"]])

    def test_retry_requires_the_original_sender_and_an_existing_active_codex_target(self):
        original = self.hub.send("desk-chat", "windows/work", "Private note")
        with self.assertRaisesRegex(ValueError, "sent by this chat"):
            self.windows.retry_message("work", original["id"], "debian/work")
        with self.assertRaisesRegex(ValueError, "sent by this chat"):
            rw.invoke(self.hub, "retry_message", {"message_id": original["id"], "to": "debian/work"},
                      str(uuid.uuid4()))
        self.linux.bind_hermes("personality")
        invalid_destinations = (("windows/work", "different Codex chat"),
                                ("desk/desk-chat", "different Codex chat"),
                                ("debian/personality", "existing Codex address"),
                                (f"debian/{uuid.uuid4()}", "existing Codex address"),
                                ("other/work", "known Riftwire device"),
                                (self.chat, "existing Codex address"))
        for target, error in invalid_destinations:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, error):
                self.hub.retry_message("desk-chat", original["id"], target)
        self.hub.revoke_device("debian")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.hub.retry_message("desk-chat", original["id"], "debian/work")
        self.assertEqual(len(self.hub.outbox("desk-chat")["messages"]), 1)
        self.assertNotIn("retry_message", rw.HERMES_TOOLS)

    def test_retry_does_not_copy_read_mail_handoffs_or_room_posts(self):
        original = self.hub.send("desk-chat", "windows/work", "Already received")
        self.windows.ack("work", original["id"])
        handoff = self.hub.request_handoff("desk-chat", "windows/work", "example", "test", "Run the tests")
        self.hub.set_room("example", ["desk/desk-chat", "windows/work"])
        room = self.hub.post_room("desk-chat", "example", "A quiet update")["deliveries"][0]
        for message in (original, handoff, room):
            with self.subTest(message=message["id"]), self.assertRaisesRegex(ValueError, "unread direct"):
                self.hub.retry_message("desk-chat", message["id"], "debian/work")
        self.assertEqual(self.linux.inbox("work")["messages"], [])

    def test_retry_can_finish_an_interrupted_hub_notice_without_copying_again(self):
        original = self.linux.send("work", "windows/work", "Offline handover")
        with patch.object(self.linux, "dispatch_hub_message", side_effect=ValueError("Connection interrupted")):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                self.linux.retry_message("work", original["id"], "desk/desk-chat")
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as queue:
            retry = self.linux.retry_message("work", original["id"], "desk/desk-chat")
            self.assertEqual(self.linux.retry_message("work", original["id"], "desk/desk-chat"), retry)
            queue.assert_called_once()
        self.assertEqual(retry["notice"], "queued")
        self.assertEqual(len(self.hub.inbox("desk-chat")["messages"]), 1)

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
