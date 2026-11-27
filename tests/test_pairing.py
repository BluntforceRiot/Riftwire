import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw
import riftwire_pairing as pairing


def fixture_key(value):
    wire = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([value]) * 32
    return "ssh-ed25519 " + base64.b64encode(wire).decode("ascii")


def fixture_request(name, key):
    return {"version": 1, "id": str(uuid.uuid4()), "name": name,
            "public_key": key, "expires": rw.timestamp() + 60}


class GuidedPairingTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir()
        self.keys = self.ssh / "authorized_keys"
        self.owner_entry = fixture_key(1) + " existing\n"
        self.keys.write_text(self.owner_entry, encoding="ascii")
        self.mailbox = self.home / "mail.sqlite3"
        self.hosts = {"hub": "desk", "peer": "windows"}
        self.hub = rw.Mailbox(self.mailbox, "hub", self.hosts)
        self.hub_thread, self.peer_thread = str(uuid.uuid4()), str(uuid.uuid4())
        self.request = fixture_request("debian", fixture_key(2))
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def issue(self, **overrides):
        options = {"thread": self.hub_thread, "name": self.request["name"],
                   "target": "owner@lan.example", "port": 2200, "host_key": fixture_key(3), **overrides}
        return pairing.issue(rw, self.hub, **options)

    def test_invitation_round_trips_and_resumes_without_a_duplicate(self):
        invite = self.issue(host_key=fixture_key(3) + " optional comment")
        self.assertEqual(self.issue(), invite)
        self.assertEqual(pairing.issue(rw, self.hub, self.hub_thread, None, None, None, None,
                                       code=invite["code"]), invite)
        token = pairing.encode_invitation(invite)
        self.assertEqual(pairing.decode_invitation(token), invite)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.issue(port=22, code=invite["code"])
        with self.assertRaisesRegex(ValueError, "this hub chat"):
            self.issue(thread=self.peer_thread, code=invite["code"])
        with self.hub.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM guided_pairings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM chat_tests").fetchone()[0], 1)

    def test_codec_rejects_noncanonical_keys_and_unusable_destination(self):
        invite = self.issue()
        token = pairing.encode_invitation(invite)
        for invalid in (token + "=", token + " ", "e30", "a", "-" * 1025):
            with self.subTest(invalid=invalid[:12]), self.assertRaises(ValueError):
                pairing.decode_invitation(invalid)
        for target in ("-o@host", "owner@" + "$" + "{NAME}", "owner@@host", "owner@x,attacker",
                       "owner@host -F none", "owner@host\n", "owner@123.256.1.1"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                pairing.encode_invitation({**invite, "target": target})
        for changed in ({"port": True}, {"port": 0}, {"expires": True}, {"host_key": "ssh-ed25519 " + "A" * 68},
                        {"host_key": fixture_key(3) + " comment"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pairing.encode_invitation({**invite, **changed})
        self.assertEqual(pairing.destination("my_user@192.168.20.30"), ("my_user", "192.168.20.30"))

    def test_enrollment_is_idempotent_and_cannot_replace_an_approved_key(self):
        invite = self.issue()
        code = invite["code"]
        first = pairing.enroll(rw, self.hub, code, self.request)
        self.assertEqual(first, {"code": code, "id": self.request["id"], "name": self.request["name"]})
        self.assertEqual(pairing.enroll(rw, self.hub, code, self.request), first)
        self.assertEqual(self.issue(), invite)
        keys = self.keys.read_text(encoding="ascii").splitlines()
        self.assertEqual(keys, [self.owner_entry.strip(),
                                rw.restricted_key(self.request["id"], self.request["public_key"])])
        with self.assertRaisesRegex(ValueError, "different device name"):
            pairing.enroll(rw, self.hub, code, {**self.request, "name": "another"})
        with self.assertRaisesRegex(ValueError, "bound to a different installation"):
            pairing.enroll(rw, self.hub, code, fixture_request("debian", fixture_key(4)))
        with self.assertRaisesRegex(ValueError, "already assigned"):
            pairing.enroll(rw, self.hub, code, {**self.request, "public_key": fixture_key(4)})
        self.assertEqual(self.keys.read_text(encoding="ascii").splitlines(), keys)

    def test_existing_key_conflict_rolls_back_database_approval(self):
        self.request["public_key"] = fixture_key(1)
        invite = self.issue()
        with self.assertRaisesRegex(ValueError, "already has a different SSH authorization"):
            pairing.enroll(rw, self.hub, invite["code"], self.request)
        with self.hub.connection() as db:
            self.assertIsNone(db.execute("SELECT device_id FROM guided_pairings").fetchone()["device_id"])
            self.assertIsNone(db.execute("SELECT id FROM devices").fetchone())
        self.assertEqual(self.keys.read_text(encoding="ascii"), self.owner_entry)

    def test_guided_enrollment_binds_the_test_to_one_device_id(self):
        invite = self.issue()
        legacy = rw.Mailbox(self.mailbox, "peer", self.hosts)
        with self.assertRaisesRegex(ValueError, "different device"):
            legacy.test_session(self.peer_thread, invite["code"])
        # An ordinary setup test can still be issued while the guided setup waits.
        manual = self.hub.test_session(self.hub_thread)
        self.assertEqual(legacy.test_session(self.peer_thread)["code"], manual["code"])
        self.hub.approve_device(self.request)
        named = rw.Mailbox(self.mailbox, "peer", self.hosts, device_id=self.request["id"])
        with self.assertRaisesRegex(ValueError, "awaits enrollment"):
            named.test_session(self.peer_thread, invite["code"])
        pairing.enroll(rw, self.hub, invite["code"], self.request)
        self.assertEqual(named.test_session(self.peer_thread), {"state": "waiting_for_hub"})
        self.assertEqual(named.test_session(self.peer_thread, invite["code"])["peer_thread"], self.peer_thread)
        self.assertEqual(self.hub.test_session(self.hub_thread, invite["code"])["peer_thread"], self.peer_thread)

    def test_existing_named_device_can_verify_without_readding_its_key(self):
        self.hub.approve_device(self.request)
        invite = self.issue()
        named = rw.Mailbox(self.mailbox, "peer", self.hosts, device_id=self.request["id"])
        self.assertEqual(named.test_session(self.peer_thread, invite["code"])["state"], "in_progress")
        self.assertEqual(self.keys.read_text(encoding="ascii"), self.owner_entry)
        self.hub.revoke_device(self.request["name"])
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.issue()
        with self.assertRaisesRegex(ValueError, "revoked"):
            named.test_session(self.peer_thread, invite["code"])

    def test_expired_invitation_cannot_add_a_key_and_can_be_reissued(self):
        invite = self.issue()
        with patch.object(rw, "timestamp", return_value=invite["expires"]):
            self.request["expires"] = rw.timestamp() + 60
            with self.assertRaisesRegex(ValueError, "expired"):
                pairing.enroll(rw, self.hub, invite["code"], self.request)
            with self.assertRaisesRegex(ValueError, "expired"):
                self.issue(code=invite["code"])
            self.assertNotEqual(self.issue()["code"], invite["code"])
        self.assertEqual(self.keys.read_text(encoding="ascii"), self.owner_entry)

    def test_revocation_applies_to_an_already_bound_peer(self):
        self.hub.approve_device(self.request)
        peer = rw.Mailbox(self.mailbox, "peer", self.hosts, device_id=self.request["id"])
        peer.enroll("worker", self.peer_thread)
        self.hub.enroll("owner", self.hub_thread)
        sent = self.hub.send("owner", "debian/worker", "A message for the test mailbox.")
        notice = peer.claim(peer.side)
        self.assertEqual(peer.inbox("worker")["messages"][0]["id"], sent["id"])

        self.hub.revoke_device(self.request["name"])
        with self.assertRaisesRegex(ValueError, "revoked"):
            peer.inbox("worker")
        with self.assertRaisesRegex(ValueError, "revoked"):
            peer.ack("worker", sent["id"])
        with self.assertRaisesRegex(ValueError, "revoked"):
            peer.settle(peer.side, sent["id"], notice["token"], "queued", "Delivered.")
        receipt = self.hub.receipt("owner", sent["id"])
        self.assertIsNone(receipt["seen"])
        self.assertEqual(receipt["notice"], "dispatching")


if __name__ == "__main__":
    unittest.main()
