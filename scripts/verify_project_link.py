"""Verify a project invitation and signed artifact delivery over loopback OpenSSH."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "riftwire.py"
REPOSITORY = "https://example.test/team/riftwire-fixture"


def run(*args, **kwargs):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, timeout=50, **kwargs)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()[-1000:]
        raise RuntimeError(f"{Path(args[0]).name} exited with status {result.returncode}. {diagnostic}")
    return result.stdout


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def wait_for_ssh(process, port):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("The disposable OpenSSH server exited. Check that local sshd can start under this login.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("The disposable OpenSSH server did not start within five seconds.")


def verify(scratch, ssh, sshd):
    home, worker = scratch / "coordinator", scratch / "collaborator"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def project(node, *args):
        return json.loads(run(sys.executable, "-I", "-B", ENTRY, "project", "--state", node, *args, env=environment))

    coordinator_id = project(home, "init", "coordinator")
    worker_id = project(worker, "init", "collaborator")
    identity = save(scratch / "worker.json", worker_id)
    invite = scratch / "invitation.json"
    grant = project(home, "invite", identity, "release", "verifier", "--write", invite)
    keys = scratch / "authorized_keys"
    keys.write_text(grant["authorized_key"] + "\n", encoding="ascii")
    keys.chmod(0o600)
    key = scratch / "host"
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    def quoted(path):
        return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    login = pwd.getpwuid(os.getuid()).pw_name
    sshd_config = scratch / "sshd_config"
    sshd_config.write_text(
        f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {quoted(key)}\n"
        f"PidFile {quoted(scratch / 'sshd.pid')}\nAuthorizedKeysFile {quoted(keys)}\nAllowUsers {login}\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nUsePAM no\n"
        "PermitTTY no\nAllowTcpForwarding no\nX11Forwarding no\nAllowAgentForwarding no\nLogLevel ERROR\n",
        encoding="utf-8",
    )
    client_config = scratch / "ssh_config"
    client_config.write_text(f"Host fixture\n  HostName 127.0.0.1\n  User {login}\n  Port {port}\n", encoding="ascii")
    ssh_wrapper = scratch / "ssh"
    ssh_wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(ssh)} -F {shlex.quote(str(client_config))} \"$@\"\n",
                           encoding="utf-8")
    ssh_wrapper.chmod(0o700)
    pin = scratch / "known_hosts"
    host_key = key.with_suffix(".pub").read_text(encoding="ascii")
    pin.write_text(f"[127.0.0.1]:{port} {host_key}", encoding="ascii")
    environment["PATH"] = str(scratch) + os.pathsep + environment.get("PATH", os.defpath)

    source = scratch / "repo"
    run("git", "init", "-q", "--initial-branch=main", source, env=environment)
    (source / "version.txt").write_text("fixture\n", encoding="ascii")
    run("git", "-C", source, "add", "version.txt", env=environment)
    run("git", "-C", source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-qm", "Fixture revision", env=environment)
    commit = run("git", "-C", source, "rev-parse", "HEAD", env=environment).decode("ascii").strip()
    second = scratch / "worker-repo"
    run("git", "clone", "--no-hardlinks", source, second, env=environment)
    manifest = save(scratch / "workflow.json", {
        "version": 1, "project": "release", "repository": REPOSITORY, "name": "Federated release fixture",
        "steps": [
            {"id": "build", "role": "builder", "action": "build", "task": "Build the fixture.",
             "artifacts": ["result.txt"]},
            {"id": "check", "role": "verifier", "action": "verify", "task": "Check the offered artifact.",
             "needs": ["build"], "different_from": ["build"]},
            {"id": "approve", "role": "maintainer", "action": "approve", "task": "Review the signed result.",
             "needs": ["check"]},
        ],
    })

    def profile(role, action, repository, script):
        return save(scratch / (role + ".json"), {
            "version": 1, "project": "release", "repository": REPOSITORY, "role": role,
            "checkout": str(repository), "refs": ["refs/heads/main"],
            "actions": {action: {"command": [sys.executable, "-c", script]}},
        })

    builder = profile("builder", "build", source, "from pathlib import Path\nPath('result.txt').write_bytes(b'fixture')")
    verifier = profile("verifier", "verify", second,
                       "import os\nfrom pathlib import Path\n"
                       "artifact = Path(os.environ['RIFTWIRE_PROJECT_INPUTS']) / 'build/result.txt'\n"
                       "assert artifact.read_bytes() == b'fixture'\nprint('Checked the offered artifact.')")
    with (scratch / "server.log").open("w+b") as log:
        process = subprocess.Popen([sshd, "-D", "-e", "-f", str(sshd_config)], stdout=log, stderr=log,
                                   env=environment)
        try:
            wait_for_ssh(process, port)
            assert project(worker, "join", "team", invite, "--ssh", "fixture", "--known-hosts", pin)["coordinator"] == coordinator_id
            job = project(home, "start", manifest, "--commit", commit)["job"]
            assert project(worker, "list", "--via", "team")["jobs"][0]["job"] == job
            assert project(home, "work", builder)["outcome"] == "passed"
            assert project(worker, "work", verifier, "--via", "team")["outcome"] == "passed"
            assert project(worker, "history", job, "--via", "team", "--step", "check")["attempts"]
            waiting = project(worker, "status", job, "--via", "team")["steps"][-1]
            assert waiting["state"] == "needs-approval"
            approved = project(home, "approve", job, "approve", "--note", "Verified the fixture evidence.")
            assert approved["steps"][-1]["state"] == "passed"
            receipt = save(scratch / "receipt.json", project(home, "result", job, "check"))
            inputs = (receipt, "--worker", identity, "--workflow", manifest, "--commit", commit, "--job", job)
            proof = project(worker, "verify", *inputs)
            assert proof["inputs"]["build"]["result.txt"] == hashlib.sha256(b"fixture").hexdigest()
            archive = project(worker, "archive", *inputs, "--summary", "Checked the offered artifact.")
            assert archive["provenance"]["job"] == job and archive["summary"] == "Checked the offered artifact."
        except BaseException:
            end = log.seek(0, os.SEEK_END)
            log.seek(max(0, end - 1600))
            sys.stderr.write(log.read().decode("utf-8", errors="replace"))
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)


def main():
    ssh = shutil.which("ssh")
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not ssh or not Path(sshd).is_file() or not shutil.which("git") or not shutil.which("ssh-keygen"):
        sys.exit("Install OpenSSH client and server and Git to run the project link fixture.")
    if os.getuid() == 0:
        sys.exit("Run the project link fixture under a regular login.")
    # sshd checks the permission chain leading to authorized_keys. Use a private directory inside the user's home.
    with tempfile.TemporaryDirectory(prefix=".riftwire-project-fixture-", dir=Path.home()) as scratch:
        verify(Path(scratch), str(Path(ssh).resolve()), str(Path(sshd).resolve()))
    print("Verified job discovery, signed artifact delivery, attempt history, and approval between two project stores over pinned SSH.")


if __name__ == "__main__":
    main()
