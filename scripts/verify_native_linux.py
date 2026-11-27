"""Verify guided setup and Codex wake in a Linux network namespace with only loopback."""

import argparse
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
from queue import Empty, Queue
import runpy
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time

try:
    from websockets.sync.client import unix_connect
except ImportError as error:
    raise SystemExit("Install websockets 15 or later for this optional Linux verification.") from error


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "riftwire.py"
FIXTURES = runpy.run_path(str(ROOT / ".github/verification/native_windows.py"))
WAIT_FOR_SSH = runpy.run_path(str(ROOT / "scripts/verify_project_link.py"))["wait_for_ssh"]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def launch(cleanup, args, **kwargs):
    process = subprocess.Popen(args, **kwargs)
    cleanup.callback(stop, process)
    return process


def run(args, env, accepted=(0,)):
    result = subprocess.run(args, env=env, cwd=env["HOME"], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=30)
    if result.returncode not in accepted:
        raise RuntimeError(f"{Path(args[0]).name} exited {result.returncode}: {result.stderr[-3000:]}")
    return result


class CodexSession:
    def __init__(self, channel):
        self.channel = channel
        self.request_id = 0

    def send(self, message):
        self.channel.send(json.dumps(message))

    def wait_for(self, matches):
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                item = json.loads(self.channel.recv(timeout=max(0.001, deadline - time.monotonic())))
                if matches(item):
                    return item
        except TimeoutError as error:
            raise RuntimeError("The native Codex app server did not return the fixture event.") from error
        raise RuntimeError("The native Codex app server did not return the fixture event.")

    def call(self, method, params):
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        response = self.wait_for(lambda item: item.get("id") == self.request_id)
        if "error" in response:
            raise RuntimeError(f"Codex {method} failed. {response['error']}")
        return response["result"]


def native_chat(cleanup, codex, home):
    profile, runtime = home / ".codex", home / "run"
    for directory in (profile, runtime):
        directory.mkdir(mode=0o700, parents=True)
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(home), "CODEX_HOME": str(profile),
           "XDG_RUNTIME_DIR": str(runtime)}
    provider = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), FIXTURES["ResponsesFixture"]))
    provider.requests = Queue()
    Thread(target=provider.serve_forever, daemon=True).start()
    cleanup.callback(provider.shutdown)
    (profile / "config.toml").write_text(
        "check_for_update_on_startup = false\nmodel = 'riftwire-fixture'\nmodel_provider = 'fixture'\n"
        "[analytics]\nenabled = false\n[model_providers.fixture]\nname = 'fixture'\nwire_api = 'responses'\n"
        f"base_url = 'http://127.0.0.1:{provider.server_port}/v1'\n", encoding="utf-8")
    log = cleanup.enter_context((home / "app-server.log").open("w+", encoding="utf-8"))
    server = launch(cleanup, [codex, "app-server", "--listen", "unix://"], env=env, cwd=home,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    control = profile / "app-server-control" / "app-server-control.sock"
    deadline = time.monotonic() + 8
    while not control.is_socket():
        if server.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(f"Codex did not start its disposable app-server socket. {Path(log.name).read_text()[-2000:]}")
        time.sleep(0.05)
    channel = cleanup.enter_context(unix_connect(str(control), proxy=None, open_timeout=5, close_timeout=2))
    client = CodexSession(channel)
    initialized = client.call("initialize", {"clientInfo": {"name": "riftwire_linux_fixture", "version": "1"}})
    if initialized["platformFamily"] != "unix" or initialized.get("platformOs") != "linux":
        raise RuntimeError(f"Expected native Linux Codex, received {initialized}")
    client.send({"method": "initialized"})
    thread = client.call("thread/start", {"cwd": str(home), "ephemeral": False,
                                          "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
    client.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Seed the fixture chat."}]})
    completed_turn(client, thread)
    provider.requests.get(timeout=5)
    chat = client.call("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
    if chat["status"]["type"] != "idle":
        raise RuntimeError(f"The saved fixture chat was expected to be idle. {chat['status']}")
    env["CODEX_THREAD_ID"] = thread
    return env, client, provider


def completed_turn(client, thread):
    event = client.wait_for(lambda event: event.get("method") == "turn/completed" and
                            event["params"]["threadId"] == thread)
    turn = event["params"]["turn"]
    if turn["status"] != "completed":
        raise RuntimeError(f"The fixture turn failed. {turn.get('error')}")
    return turn["id"]


def verify_wake(env, client, provider, message_id):
    notice = f"Riftwire message {message_id} from "
    try:
        route, request = provider.requests.get(timeout=12)
    except Empty as exc:
        raise RuntimeError(f"The idle Codex chat did not wake for {message_id}.") from exc
    if route != "/v1/responses" or notice not in json.dumps(request["input"]):
        raise RuntimeError(f"The idle Codex chat did not receive the expected wake notice for {message_id}.")
    return {"turn": completed_turn(client, env["CODEX_THREAD_ID"]), "notice_id": message_id}


def verify(codex, sshd):
    with tempfile.TemporaryDirectory(prefix="rw-linux-") as scratch, ExitStack() as cleanup:
        base = Path(scratch)
        hub = native_chat(cleanup, codex, base / "hub")
        peer = native_chat(cleanup, codex, base / "peer")
        version = run([codex, "--version"], hub[0]).stdout.strip()
        owner, host = base / "owner", base / "host"
        for key in (owner, host):
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], hub[0])
        keys = base / "hub" / ".ssh" / "authorized_keys"
        keys.parent.mkdir(mode=0o700)
        owner_key = owner.with_suffix(".pub").read_bytes()
        keys.write_bytes(owner_key)
        keys.chmod(0o600)
        login = pwd.getpwuid(os.getuid()).pw_name
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = str(listener.getsockname()[1])
        config = base / "sshd_config"
        config.write_text(
            f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {host}\nPidFile {base / 'sshd.pid'}\n"
            f"AuthorizedKeysFile {keys}\nAllowUsers {login}\n"
            f"SetEnv HOME={hub[0]['HOME']} CODEX_HOME={hub[0]['CODEX_HOME']} XDG_RUNTIME_DIR={hub[0]['XDG_RUNTIME_DIR']}\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\nPermitTTY no\nPermitUserRC no\nDisableForwarding yes\nLogLevel VERBOSE\n",
            encoding="utf-8")
        ssh_log = cleanup.enter_context((base / "sshd.log").open("w+", encoding="utf-8"))
        server = launch(cleanup, [sshd, "-D", "-e", "-f", config], env=hub[0], cwd=hub[0]["HOME"],
                        stdin=subprocess.DEVNULL, stdout=ssh_log, stderr=ssh_log)
        WAIT_FOR_SSH(server, int(port))

        def setup(role, env, *args):
            result = run([sys.executable, "-I", "-B", ENTRY, "setup", role, "--codex", codex, "--wait", "0", *args],
                         env, accepted=(0, 2))
            return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

        invitation = setup("hub", hub[0], "--ssh", f"{login}@127.0.0.1", "--port", port,
                           "--device", "linux-peer", "--host-key", host.with_suffix(".pub"))[0]
        greeting = setup("peer", peer[0], "--invite", invitation["invitation"], "--batch",
                         "--owner-identity", owner, "--no-background")[-1]
        wakes = {"hub": verify_wake(*hub, greeting["receipts"]["hello"]["id"])}
        setup("hub", hub[0], "--resume", invitation["code"])
        proof = setup("peer", peer[0])[-1]
        if proof["state"] != "passed" or setup("hub", hub[0], "--resume", invitation["code"])[-1] != proof:
            raise RuntimeError(f"The two guided setup chats did not record matching read receipts. {proof}")
        wakes["peer"] = verify_wake(*peer, proof["receipts"]["reply"]["id"])
        if (proof["hub_thread"], proof["peer_thread"]) != (hub[0]["CODEX_THREAD_ID"], peer[0]["CODEX_THREAD_ID"]):
            raise RuntimeError("Guided delivery routed to a different fixture chat.")
        fingerprint = run(["ssh-keygen", "-lf", owner.with_suffix(".pub")], hub[0]).stdout.split()[1]
        authentication = Path(ssh_log.name).read_text().splitlines()
        owner_logins = sum(line.startswith("Accepted publickey ") and fingerprint in line for line in authentication)
        authorized = keys.read_bytes().splitlines(keepends=True)
        if owner_logins != 1 or len(authorized) != 2 or authorized[0] != owner_key or not authorized[1].startswith(b"restrict,command="):
            raise RuntimeError("Guided setup did not reuse the dedicated peer credential after enrollment.")
        return {"codex": version, "owner_ssh_authentications": owner_logins, "native_wake": wakes, **proof}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True, help="Path to the installed native Linux Codex CLI.")
    parser.add_argument("--sshd", type=Path, default=Path("/usr/sbin/sshd"), help="Path to the installed OpenSSH server.")
    args = parser.parse_args()
    if sys.platform != "linux" or os.getuid() == 0 or {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise SystemExit("Run under a regular Linux login in a disposable network namespace with only loopback.")
    print(json.dumps(verify(args.codex.resolve(strict=True), args.sshd.resolve(strict=True)), indent=2))
