import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import riftwire as rw


def public_key(value):
    ed25519 = b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes([value]) * 32
    return "ssh-ed25519 " + base64.b64encode(ed25519).decode("ascii")


@unittest.skipIf(os.name == "nt", "A Riftwire hub requires a Unix account.")
class GuidedSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="riftwire-setup-")
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name).resolve()
        self.homes = {role: self.folder / role for role in ("hub", "peer")}
        self.profiles = {role: home / ".codex" for role, home in self.homes.items()}
        self.profile_env = {}
        self.codex = {role: self.homes[role] / "codex" for role in self.homes}
        self.threads = {role: str(uuid.uuid4()) for role in self.homes}
        self.registrations = {role: {} for role in self.homes}
        for role, home in self.homes.items():
            home.mkdir()
            self.codex[role].touch()
        self.hub = self.homes["hub"] / ".local/share/riftwire"
        self.peer = self.homes["peer"] / ".local/share/riftwire-peer"
        self.authorized = self.homes["hub"] / ".ssh/authorized_keys"
        self.legacy_key = public_key(1).encode("ascii") + b" existing\n"
        self.authorized.parent.mkdir()
        self.authorized.write_bytes(self.legacy_key)
        self.host_key = self.folder / "host.pub"
        self.host_key.write_text(public_key(2) + " host\n", encoding="ascii")
        self.owner_key = self.folder / "owner key"
        self.owner_key.touch()
        self.generated_keys = 0
        self.owner_calls = 0
        self.queue_calls = []
        self.fail_peer_registration = False
        self.drop_owner_reply = False
        patches = (
            patch.object(rw, "DATA", self.hub),
            patch.object(rw.os, "umask"),
            patch.object(rw.shutil, "which", side_effect=lambda name: f"fixture-{name}"),
            patch.object(rw.subprocess, "run", side_effect=self.run_command),
            patch.object(rw, "pickup_notices", side_effect=self.pickup),
            patch.object(rw, "local_codex_chats", side_effect=self.chat_directory),
        )
        for fixture in patches:
            fixture.start()
            self.addCleanup(fixture.stop)

    def chat_directory(self, executable, codex_home):
        role = next(role for role, path in self.codex.items() if str(path) == executable)
        self.assertEqual(codex_home, str(self.profiles[role]))
        return {self.threads[role]: {"title": "Setup", "folder": "Fixture"}}

    def run_cli(self, role, args, payload=b"", original_command=None):
        home = self.homes[role]
        env = {**os.environ, "HOME": str(home),
               "XDG_DATA_HOME": str(home / ".local/share"), "CODEX_THREAD_ID": self.threads[role]}
        env.pop("CODEX_HOME", None)
        profile = self.profile_env.get(role, self.profiles[role])
        if profile is not None:
            env["CODEX_HOME"] = str(profile)
        env.pop("SSH_ORIGINAL_COMMAND", None)
        if original_command is not None:
            env["SSH_ORIGINAL_COMMAND"] = original_command
        stdout, stderr = io.StringIO(), io.StringIO()
        rc = 0
        with (patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["riftwire.py", *args]),
              patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload))), redirect_stdout(stdout),
              redirect_stderr(stderr)):
            try:
                rw.main()
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
                if isinstance(exc.code, str):
                    print(exc.code, file=stderr)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                rc = 1
                print(exc, file=stderr)
        return subprocess.CompletedProcess(args, rc, stdout.getvalue().encode(), stderr.getvalue().encode())

    def run_command(self, args, input=None, check=False, **options):
        executable = str(args[0])
        if executable == "fixture-ssh":
            result = self.ssh(args, input or b"")
        elif executable == "fixture-ssh-keygen":
            key_file = Path(args[args.index("-f") + 1])
            if "-y" in args:
                result = subprocess.CompletedProcess(args, 0, key_file.read_bytes(), b"")
            else:
                self.generated_keys += 1
                key = public_key(10 + self.generated_keys)
                key_file.write_text(key + "\n", encoding="ascii")
                key_file.with_suffix(".pub").write_text(key + "\n", encoding="ascii")
                result = subprocess.CompletedProcess(args, 0, b"", b"")
        else:
            role = next((role for role, path in self.codex.items() if str(path) == executable), None)
            self.assertIsNotNone(role, args)
            result = self.codex_command(role, args, options)
        if check:
            result.check_returncode()
        return result

    def codex_command(self, role, args, options):
        command = args[1:]
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.profiles[role]))
        if command == ["queue", "--help"]:
            return subprocess.CompletedProcess(args, 0, b"--thread --message --resume", b"")
        if command[0] == "queue":
            self.assertEqual(command[command.index("--thread") + 1], self.threads[role])
            self.queue_calls.append((role, "--resume" in command, command[command.index("--message") + 1]))
            return subprocess.CompletedProcess(args, 0, b"queued", b"")
        records = self.registrations[role]
        if command[:2] == ["mcp", "list"]:
            payload = list(records.values())
        elif command[:2] == ["mcp", "get"]:
            payload = records[command[2]]
        elif command[:2] == ["mcp", "add"]:
            if role == "peer" and self.fail_peer_registration:
                self.fail_peer_registration = False
                return subprocess.CompletedProcess(args, 1, b"", b"Native registration interrupted")
            name, program = command[2], command[4]
            records[name] = {"name": name, "enabled": True, "transport": {
                "type": "stdio", "command": program, "args": command[5:],
            }}
            payload = {}
        else:
            self.fail(f"Unexpected Codex command {command!r}")
        return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")

    def ssh(self, args, payload):
        separator = args.index("--")
        self.assertEqual(args[separator + 1], "owner@fixture.invalid")
        options = dict(value.split("=", 1) for value in args[1:separator] if "=" in value)
        for option in ("StrictHostKeyChecking=yes", "ControlPath=none", "ForwardAgent=no", "ClearAllForwardings=yes"):
            self.assertIn(option, args)
        self.assertIn("2208", args)
        known_hosts = Path(options["UserKnownHostsFile"].strip('"').replace("%%", "%"))
        pin = known_hosts.read_text(encoding="ascii")
        if public_key(2) not in pin:
            return subprocess.CompletedProcess(args, 255, b"", b"Host key verification failed")
        command = args[separator + 2]
        remote = shlex.split(command)[4:]
        identity = Path(args[args.index("-i") + 1]) if "-i" in args else None
        if identity == self.owner_key:
            self.owner_calls += 1
            self.assertEqual(remote, ["setup-enroll"])
            completed = self.run_cli("hub", remote, payload)
            if self.drop_owner_reply and completed.returncode == 0:
                self.drop_owner_reply = False
                return subprocess.CompletedProcess(args, 255, b"", b"Connection interrupted after approval")
            return completed
        if identity is not None:
            key = identity.read_text(encoding="ascii").strip()
            for line in self.authorized.read_text(encoding="ascii").splitlines():
                if not line.startswith("restrict,command=") or key not in line:
                    continue
                device_id = line.rsplit(" riftwire-", 1)[1]
                return self.run_cli("hub", ["ssh-gate", "--device", device_id], payload, command)
        return subprocess.CompletedProcess(args, 255, b"", b"Permission denied (publickey)")

    def pickup(self, settings):
        box = rw.Mailbox(self.hub / "mail.sqlite3", "peer", self.hub_mailbox().hosts,
                         device_id=settings["device_id"])
        count = 0
        while True:
            job = box.claim(box.side)
            if job is None:
                return count
            proc = SimpleNamespace(stdout=io.BytesIO((rw.encode(job) + "\n").encode()), stdin=io.BytesIO())
            count += rw.relay_notice_stream(proc, settings)
            ack = json.loads(proc.stdin.getvalue())
            self.assertEqual(box.settle(box.side, ack["id"], ack["token"], ack["state"], ack["detail"]),
                             {"recorded": True})

    def hub_mailbox(self):
        settings = rw.read_settings(self.hub)
        return rw.Mailbox(self.hub / "mail.sqlite3", "hub", settings["hosts"],
                          settings["codex"], settings["codex_home"])

    def setup(self, role, *extra):
        args = ["setup", role, "--codex", str(self.codex[role]), "--wait", "0", *extra]
        return self.run_cli(role, args)

    def hub_setup(self, *extra):
        return self.setup("hub", "--ssh", "owner@fixture.invalid", "--port", "2208", "--device", "second-boot",
                          "--host-key", str(self.host_key), *extra)

    def peer_setup(self, token=None):
        if token is None:
            return self.setup("peer")
        return self.setup("peer", "--invite", token, "--owner-identity", str(self.owner_key), "--batch",
                          "--no-background")

    def report(self, result, field, expected_exit=(0, 2)):
        self.assertIn(result.returncode, expected_exit, result.stderr.decode() + result.stdout.decode())
        for line in reversed(result.stdout.decode().splitlines()):
            if line.startswith("{"):
                value = json.loads(line)
                if field in value:
                    return value
        self.fail(f"Setup did not report {field}: {result.stdout.decode()}")

    def test_interrupted_setup_can_resume_and_preserves_legacy_mail(self):
        original = self.run_cli("hub", ["install-hub", "--codex", str(self.codex["hub"])])
        self.assertEqual(original.returncode, 0, original.stderr.decode())
        box = self.hub_mailbox()
        sent = rw.invoke(box, "send", {"to": self.threads["peer"], "text": "Existing undelivered mail."},
                         self.threads["hub"])
        (self.hub / "riftwire.py").write_text("# Previous installed release\n", encoding="ascii")
        local_hub = {"name": "riftwire", "enabled": True, "transport": {
            "type": "stdio", "command": sys.executable,
            "args": ["-I", "-B", str(self.homes["peer"] / ".local/share/riftwire/riftwire.py"), "mcp", "hub"],
        }}
        self.registrations["peer"]["riftwire"] = local_hub

        hub = self.hub_setup()
        invite = self.report(hub, "invitation")
        backup = self.report(hub, "mailbox")
        self.assertEqual(backup["integrity"], "ok")
        saved_mail = rw.Mailbox(backup["mailbox"], "hub", box.hosts)
        self.assertEqual(rw.invoke(saved_mail, "receipt", {"message_id": sent["id"]}, self.threads["hub"]), sent)
        self.assertEqual(self.report(self.hub_setup(), "invitation")["code"], invite["code"])
        self.fail_peer_registration = True
        interrupted = self.peer_setup(invite["invitation"])
        self.assertEqual(interrupted.returncode, 1, interrupted.stdout.decode())
        self.assertEqual(self.owner_calls, 1)
        self.assertEqual(self.generated_keys, 1)
        approved_keys = self.authorized.read_bytes()
        self.assertTrue(approved_keys.startswith(self.legacy_key))
        self.assertEqual(len(approved_keys.splitlines()), 2)

        self.report(self.peer_setup(), "receipts")
        self.report(self.setup("hub", "--resume", invite["code"]), "receipts")
        passed = self.report(self.peer_setup(), "receipts", expected_exit=(0,))
        self.assertEqual(passed["state"], "passed")
        self.assertEqual(passed["hub_thread"], self.threads["hub"])
        self.assertEqual(passed["peer_thread"], self.threads["peer"])
        self.assertEqual({receipt["notice"] for receipt in passed["receipts"].values()}, {"queued"})
        self.assertTrue(all(receipt["seen"] for receipt in passed["receipts"].values()))
        self.assertEqual(self.report(self.setup("hub", "--resume", invite["code"]), "receipts"), passed)
        self.assertEqual(self.owner_calls, 1)
        self.assertEqual(self.generated_keys, 1)
        self.assertEqual(self.authorized.read_bytes(), approved_keys)
        self.assertEqual([role for role, _, _ in self.queue_calls], ["hub", "peer"])
        self.assertTrue(all(wake for _, wake, _ in self.queue_calls))
        self.assertTrue(self.registrations["hub"] and self.registrations["peer"])
        settings = rw.read_settings(self.peer)
        self.assertEqual(self.registrations["peer"]["riftwire"], local_hub)
        self.assertNotEqual(settings["mcp_name"], "riftwire")
        self.assertEqual(self.registrations["peer"][settings["mcp_name"]]["transport"]["args"][-1], "mcp-peer")
        self.assertIn(settings["mcp_name"], self.queue_calls[-1][2])
        owner_operation = f"python3 -I -B {rw.REMOTE_SCRIPT} setup-enroll"
        denied = self.ssh(rw.ssh_connection_args(settings) + [owner_operation], b"{}")
        self.assertEqual(denied.returncode, 1)
        self.assertIn(b"restricted to the Riftwire peer protocol", denied.stderr)
        with box.connection() as db:
            devices = db.execute("SELECT id,name FROM devices").fetchall()
            self.assertEqual([tuple(device) for device in devices], [(settings["device_id"], "second-boot")])
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
            preserved = db.execute("SELECT body,seen,notice,recipient FROM messages WHERE id=?",
                                   (sent["id"],)).fetchone()
            self.assertEqual(tuple(preserved), ("Existing undelivered mail.", None, "pending", sent["recipient"]))

    def test_invalid_host_pin_stops_before_owner_authentication(self):
        self.host_key.write_text(public_key(3) + " wrong host\n", encoding="ascii")
        invite = self.report(self.hub_setup(), "invitation")
        blocked = self.peer_setup(invite["invitation"])
        self.assertEqual(blocked.returncode, 1, blocked.stdout.decode())
        self.assertIn("Host key verification failed", blocked.stderr.decode())
        self.assertEqual(self.owner_calls, 0)
        self.assertEqual(self.authorized.read_bytes(), self.legacy_key)
        self.assertFalse((self.peer / "settings.json").exists())
        with self.hub_mailbox().connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM devices").fetchone()[0], 0)

        self.host_key.write_text(public_key(2) + " corrected host\n", encoding="ascii")
        corrected = self.report(self.hub_setup(), "invitation")
        self.assertNotEqual(corrected["code"], invite["code"])
        self.report(self.peer_setup(corrected["invitation"]), "receipts")
        self.assertEqual(self.owner_calls, 1)
        self.assertEqual(self.generated_keys, 1)
        saved_pin = Path(rw.read_settings(self.peer)["known_hosts"]).read_bytes()
        refused = self.peer_setup(invite["invitation"])
        self.assertIn(b"A different guided SSH route is already saved", refused.stderr)
        self.assertEqual(Path(rw.read_settings(self.peer)["known_hosts"]).read_bytes(), saved_pin)

    def test_repair_snapshots_existing_hub_and_preserves_its_codex_profile(self):
        self.profiles["hub"] = self.homes["hub"] / "custom profile"
        original = self.run_cli("hub", ["install-hub", "--codex", str(self.codex["hub"])])
        self.assertEqual(original.returncode, 0, original.stderr.decode())
        box = self.hub_mailbox()
        sent = rw.invoke(box, "send", {"to": self.threads["peer"], "text": "Keep the mailbox on repair."},
                         self.threads["hub"])
        (self.hub / "riftwire.py").unlink()
        self.registrations["hub"].clear()
        self.profile_env["hub"] = None
        repaired = self.hub_setup()
        backup = self.report(repaired, "mailbox")
        snapshot = rw.Mailbox(backup["mailbox"], "hub", box.hosts)
        self.assertEqual(rw.invoke(snapshot, "receipt", {"message_id": sent["id"]}, self.threads["hub"]), sent)
        self.assertEqual(rw.read_settings(self.hub)["codex_home"], str(self.profiles["hub"]))
        self.assertIn("riftwire", self.registrations["hub"])
        self.assertTrue((self.hub / "riftwire.py").is_file())
        code = self.report(repaired, "invitation")["code"]
        self.profile_env["hub"] = self.homes["hub"] / "unrelated profile"
        refused = self.setup("hub", "--resume", code)
        self.assertEqual(refused.returncode, 1)
        self.assertIn(b"Resume setup with that CODEX_HOME", refused.stderr)
        self.assertEqual(rw.read_settings(self.hub)["codex_home"], str(self.profiles["hub"]))

    def test_dedicated_resume_saves_proof_and_profile_before_registration(self):
        self.profiles["peer"] = self.homes["peer"] / "custom profile"
        token = self.report(self.hub_setup(), "invitation")["invitation"]
        self.drop_owner_reply = True
        first = self.peer_setup(token)
        self.assertEqual(first.returncode, 1)
        self.assertIn(b"interrupted after approval", first.stderr)
        progress = self.peer / "guided-setup.json"
        self.assertFalse(json.loads(progress.read_bytes())["authenticated"])
        self.profile_env["peer"] = None
        with patch.object(rw, "register_peer", side_effect=OSError("Registration interrupted before saving")):
            resumed = self.peer_setup()
        self.assertEqual(resumed.returncode, 1)
        self.assertIn(b"Registration interrupted before saving", resumed.stderr)
        self.assertFalse((self.peer / "settings.json").exists())
        saved = json.loads(progress.read_bytes())
        self.assertTrue(saved["authenticated"])
        self.assertEqual(saved["codex_home"], str(self.profiles["peer"]))
        self.assertEqual(self.owner_calls, 1)
        self.report(self.peer_setup(), "receipts")
        self.assertEqual(rw.read_settings(self.peer)["codex_home"], str(self.profiles["peer"]))
        self.assertEqual(self.owner_calls, 1)
        self.assertEqual(self.generated_keys, 1)

    def test_guided_setup_refuses_a_symlink_in_its_managed_pin_path(self):
        token = self.report(self.hub_setup(), "invitation")["invitation"]
        redirected = self.folder / "outside-pin"
        original = f"[fixture.invalid]:2208 {public_key(2)}\n".encode("ascii")
        redirected.write_bytes(original)
        self.peer.mkdir(parents=True)
        pin = self.peer / "setup_known_hosts"
        pin.symlink_to(redirected)
        refused = self.peer_setup(token)
        self.assertEqual(refused.returncode, 1)
        self.assertIn(b"The guided SSH pin path must contain a regular file", refused.stderr)
        self.assertTrue(pin.is_symlink())
        self.assertEqual(redirected.read_bytes(), original)
        self.assertEqual(self.generated_keys, 0)
        self.assertEqual(self.owner_calls, 0)


if __name__ == "__main__":
    unittest.main()
