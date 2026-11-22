import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import riftwire as rw


def uid():
    return str(uuid.uuid4())


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.settings = {"hosts": {"hub": "laptop", "peer": "desktop"},
                         "codex": str(self.folder / "codex"), "codex_home": str(self.folder / "codex-home")}
        self.path = self.folder / "mail.sqlite3"
        self.hub = rw.Mailbox(self.path, "hub", self.settings["hosts"])
        self.peer = rw.Mailbox(self.path, "peer", self.settings["hosts"])
        self.first = uid()
        self.second = uid()
        self.chats = {
            self.first: {"title": "Planning", "folder": "website"},
            self.second: {"title": "Planning", "folder": "docs"},
        }
        self.hub_tool = rw.with_local_directory(
            lambda name, args, tid: rw.invoke(self.hub, name, args, tid), self.folder, self.settings, "hub"
        )

    def test_contacts_follow_new_chats_renames_and_archives_without_enrollment(self):
        with patch.object(rw, "local_codex_chats", return_value=self.chats), patch.object(rw, "queue_notice") as queue:
            first_page = self.hub_tool("contacts", {"search": "planning", "limit": 1}, None)
            second_page = self.hub_tool("contacts", {"search": "planning", "offset": first_page["local_next_offset"]}, None)
            folder_match = self.hub_tool("contacts", {"search": "DOCS"}, None)
            self.assertEqual(first_page["local_chats"][0]["address"], f"laptop/{self.first}")
            self.assertEqual(second_page["local_chats"], folder_match["local_chats"])
            self.assertEqual(second_page["local_chats"][0]["thread_id"], self.second)
            self.assertIsNone(second_page["local_next_offset"])

            new_id = uid()
            renamed = {self.first: {"title": "Build planning", "folder": "website"},
                       new_id: {"title": "New chat", "folder": "tools"}}
            with patch.object(rw, "local_codex_chats", return_value=renamed):
                rows = self.hub_tool("contacts", {}, None)["local_chats"]
            self.assertEqual([(row["address"], row["title"]) for row in rows],
                             [(f"laptop/{self.first}", "Build planning"), (f"laptop/{new_id}", "New chat")])
            queue.assert_not_called()
        self.assertEqual(self.hub.contacts()["addresses"], [])
        self.assertFalse((self.folder / "favorites.sqlite3").exists())

    def test_favorite_prioritizes_the_same_id_after_a_rename(self):
        with patch.object(rw, "local_codex_chats", return_value=self.chats):
            with self.assertRaisesRegex(ValueError, "saved, unarchived"):
                self.hub_tool("set_favorite", {"thread_id": uid(), "favorite": True}, None)
            self.hub_tool("set_favorite", {"thread_id": self.second, "favorite": True}, None)
            self.chats[self.second]["title"] = "Documentation planning"
            reopened = rw.ChatDirectory(self.folder, self.settings, "hub")
            top = reopened.contacts(limit=1)["local_chats"][0]
            self.assertEqual(top["thread_id"], self.second)
            self.assertEqual(top["title"], "Documentation planning")
            self.assertTrue(top["favorite"])
            self.hub_tool("set_favorite", {"thread_id": self.second, "favorite": False}, None)
            self.assertEqual(reopened.contacts(limit=1)["local_chats"][0]["thread_id"], self.first)

    def test_peer_queries_its_own_directory_and_can_show_it_offline(self):
        mailbox = Mock(return_value=self.peer.contacts())
        call_peer = rw.with_local_directory(mailbox, self.folder, self.settings, "peer")
        with patch.object(rw, "local_codex_chats", return_value=self.chats) as discover:
            listing = call_peer("contacts", {"search": "docs"}, None)
            mailbox.assert_called_once_with("contacts", {}, None)
            discover.assert_called_once_with(self.settings["codex"], self.settings["codex_home"])
            self.assertEqual(listing["local_chats"][0]["address"], f"desktop/{self.second}")
            mailbox.reset_mock(side_effect=True)
            mailbox.side_effect = OSError("SSH is unavailable")
            offline = call_peer("contacts", {"local_only": True}, None)
            mailbox.assert_not_called()
            self.assertEqual(len(offline["local_chats"]), 2)
            self.assertEqual(call_peer("contacts", {}, None)["mailbox_error"], "SSH is unavailable")

    def test_unavailable_local_index_preserves_shared_contacts(self):
        self.hub.enroll("review", self.first)
        with patch.object(rw, "local_codex_chats", side_effect=OSError):
            received = self.hub_tool("contacts", {}, None)
        self.assertEqual(received["addresses"], ["laptop/review"])
        self.assertEqual(received["local_chats"], [])
        self.assertIn("local_chats_error", received)

    def test_local_delivery_by_id_does_not_publish_chat_on_the_peer(self):
        self.hub.enroll("origin", self.first)
        with patch.object(rw, "local_codex_chats", return_value=self.chats):
            contact = self.hub_tool("contacts", {"search": "docs"}, self.first)["local_chats"][0]
        self.chats[self.second]["title"] = "Renamed"
        self.hub_tool("set_wake", {"wake": False}, self.second)
        with patch.object(rw, "queue_notice", return_value=("queued", "")) as notice:
            receipt = self.hub_tool("send", {"to": contact["address"], "text": "Local note"}, self.first)
        self.assertEqual(receipt["notice"], "queued")
        self.assertEqual(notice.call_args.args[0]["thread"], self.second)
        self.assertEqual(notice.call_args.args[0]["wake"], 0)
        self.assertEqual(self.hub_tool("inbox", {}, self.second)["messages"][0]["body"], "Local note")
        remote = rw.invoke(self.peer, "contacts", {})
        self.assertEqual(remote["addresses"], ["laptop/origin"])
        self.assertNotIn("local_chats", remote)

    def test_sharing_changes_the_peer_list_and_shared_title_lookup(self):
        self.peer.enroll("sender", uid())
        hidden = self.hub.enroll("review", self.second, title="Shared review", share=False)
        self.assertFalse(hidden["shared"])
        self.assertFalse(self.hub.enroll("review", self.second, wake=False)["shared"])
        self.assertNotIn("laptop/review", self.peer.contacts()["addresses"])
        with self.assertRaisesRegex(ValueError, "No matching"):
            self.peer.send("sender", "Shared review", "Test")
        self.hub.enroll("review", self.second, share=True)
        self.assertEqual(self.peer.contacts()["titles"]["laptop/review"], "Shared review")
        with patch.object(rw, "queue_notice", return_value=("queued", "")):
            self.assertEqual(self.peer.send("sender", "Shared review", "Test")["recipient"], "laptop/review")
        self.hub.enroll("review", self.second, share=False)
        self.assertNotIn("laptop/review", self.peer.contacts()["titles"])
        self.assertNotIn("set_favorite", rw.TOOLS)
        self.assertNotIn("set_favorite", rw.HERMES_TOOLS)
        self.assertIn("set_favorite", rw.LOCAL_HERMES_TOOLS)

    def test_upgrade_preserves_enrollments_but_hides_unpublished_uuids(self):
        legacy = self.folder / "before-directory.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            db.execute("""CREATE TABLE contacts (
                          address TEXT PRIMARY KEY, host TEXT NOT NULL, thread TEXT NOT NULL,
                          wake INTEGER NOT NULL, client TEXT NOT NULL DEFAULT 'codex', UNIQUE(host, thread))""")
            db.executemany("INSERT INTO contacts (address,host,thread,wake) VALUES (?,?,?,?)",
                           [("laptop/published", "laptop", self.first, 0),
                            (f"laptop/{rw.generated_alias(self.second)}", "laptop", self.second, 1)])
        reopened = rw.Mailbox(legacy, "peer", self.settings["hosts"])
        self.assertEqual(reopened.contacts()["addresses"], ["laptop/published"])
        local = rw.Mailbox(legacy, "hub", self.settings["hosts"])
        self.assertFalse(local.enroll("published", self.first)["wake"])


class AppServerDirectoryTests(unittest.TestCase):
    def server(self, responses):
        proc = Mock()
        proc.stdin = io.BytesIO()
        proc.stdout = io.BytesIO("".join(json.dumps(row) + "\n" for row in responses).encode())
        proc.poll.return_value = 0
        return proc

    def test_native_request_is_scoped_paginated_and_drops_preview_data(self):
        first, last = uid(), uid()
        entry = {"cwd": str(Path.cwd() / "Example"), "ephemeral": False, "source": "appServer",
                 "name": "Readable title", "preview": "Test transcript data"}
        server = self.server([
            {"id": 1, "result": {"userAgent": "Codex Desktop/0.171.0 (test)"}},
            {"method": "thread/started", "params": {}},
            {"id": 2, "result": {"data": [
                {**entry, "id": first},
                {**entry, "id": uid(), "ephemeral": True},
                {**entry, "id": uid(), "parentThreadId": first},
                {**entry, "id": uid(), "source": {"subAgent": "review"}},
            ], "nextCursor": "next"}},
            {"id": 3, "result": {"data": [{**entry, "id": last, "name": None}], "nextCursor": None}},
        ])
        with patch.object(rw.subprocess, "Popen", return_value=contextlib.nullcontext(server)) as start:
            chats = rw.local_codex_chats("fixture-codex", "/fixture/profile")
        self.assertEqual(chats, {first: {"title": "Readable title", "folder": "Example"},
                                last: {"title": "Untitled chat", "folder": "Example"}})
        self.assertEqual(start.call_args.args[0][:4], ["fixture-codex", "app-server", "--listen", "stdio://"])
        self.assertEqual(start.call_args.kwargs["env"]["CODEX_HOME"], "/fixture/profile")
        requests = [json.loads(row) for row in server.stdin.getvalue().splitlines()]
        self.assertEqual([row["method"] for row in requests], ["initialize", "initialized", "thread/list", "thread/list"])
        self.assertTrue(requests[2]["params"]["currentAccountOnly"])
        self.assertTrue(requests[2]["params"]["useStateDbOnly"])
        self.assertFalse(requests[2]["params"]["archived"])
        self.assertEqual(requests[3]["params"]["cursor"], "next")

    def test_old_server_is_not_asked_for_unscoped_chat_history(self):
        server = self.server([{"id": 1, "result": {"userAgent": "codex-cli/0.150.0 (test)"}}])
        with patch.object(rw.subprocess, "Popen", return_value=contextlib.nullcontext(server)):
            with self.assertRaisesRegex(ValueError, "Codex 0.171.0 or later"):
                rw.local_codex_chats("fixture-codex", None)
        self.assertEqual(len(server.stdin.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
