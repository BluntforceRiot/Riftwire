"""Verify a released Riftwire peer with native Codex on a Windows CI runner."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time


class ResponsesFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "Fixture saved.", "annotations": []}]}
        events = [
            {"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed", "output": [message],
             "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
        ]
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

    def log_message(self, fmt, *args):
        pass


class AppServer:
    def __init__(self, proc):
        self.proc = proc
        self.messages = Queue()
        self.counter = 0
        Thread(target=self.collect, daemon=True).start()

    def collect(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        finally:
            self.messages.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, matches):
        end = time.monotonic() + 25
        while time.monotonic() < end:
            item = self.messages.get(timeout=max(0.001, end - time.monotonic()))
            if item is None:
                raise RuntimeError("Codex app-server exited.")
            if matches(item):
                return item
        raise RuntimeError("Codex app-server did not return the requested event.")

    def call(self, method, params):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params})
        reply = self.wait_for(lambda item: item.get("id") == self.counter)
        if "error" in reply:
            raise RuntimeError((method, reply["error"]))
        return reply["result"]

    def tool(self, thread, name, arguments):
        value = self.call("mcpServer/tool/call", {"threadId": thread, "server": "riftwire",
                                                 "tool": name, "arguments": arguments})
        assert not value.get("isError"), (name, value)
        return json.loads(value["content"][0]["text"])


def verify(codex, archive_script):
    with tempfile.TemporaryDirectory(prefix="riftwire-native-win-") as scratch, ExitStack() as cleanup:
        base = Path(scratch)
        provider = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), ResponsesFixture))
        Thread(target=provider.serve_forever, daemon=True).start()
        cleanup.callback(provider.shutdown)
        profile = base / "codex"
        profile.mkdir()
        local_app_data = base / "app data"
        riftwire = local_app_data / "Riftwire"
        riftwire.mkdir(parents=True)
        entry = riftwire / "riftwire.py"
        shutil.copyfile(archive_script, entry)
        (riftwire / "settings.json").write_text(json.dumps({
            "hosts": {"hub": "fixture-hub", "peer": "fixture-win"},
            "codex": str(codex), "codex_home": str(profile), "target": "offline-fixture",
            "ssh_program": str(base / "unused-ssh.exe"),
        }), encoding="utf-8")
        (profile / "config.toml").write_text(
            "check_for_update_on_startup = false\n"
            "model = 'riftwire-fixture'\nmodel_provider = 'fixture'\n"
            "[model_providers.fixture]\nname = 'fixture'\nwire_api = 'responses'\n"
            f"base_url = 'http://127.0.0.1:{provider.server_port}/v1'\n"
            "[analytics]\nenabled = false\n"
            "[mcp_servers.riftwire]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps(['-I','-B',str(entry),'mcp-peer'])}\n"
            "[mcp_servers.riftwire.env]\n"
            f"LOCALAPPDATA = {json.dumps(str(local_app_data))}\n",
            encoding="utf-8",
        )
        allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE",
                   "HOMEDRIVE", "HOMEPATH", "APPDATA", "TEMP", "TMP", "USERNAME")
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        env.update(CODEX_HOME=str(profile), LOCALAPPDATA=str(local_app_data))
        with subprocess.Popen([str(codex), "app-server", "--listen", "stdio://"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, encoding="utf-8", env=env) as proc:
            app = AppServer(proc)
            try:
                info = app.call("initialize", {"clientInfo": {"name": "riftwire_fixture", "version": "1"},
                                               "capabilities": {"experimentalApi": True}})
                assert info["platformFamily"] == "windows", info
                app.send({"method": "initialized"})

                def create_saved_chat(title, folder):
                    location = base / folder
                    location.mkdir()
                    chat = app.call("thread/start", {"cwd": str(location), "ephemeral": False,
                                                     "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
                    app.call("turn/start", {"threadId": chat,
                                            "input": [{"type": "text", "text": "Local CI fixture."}]})
                    done = app.wait_for(lambda item: item.get("method") == "turn/completed" and
                                        item["params"]["threadId"] == chat)
                    assert done["params"]["turn"]["status"] == "completed", done
                    app.call("thread/name/set", {"threadId": chat, "name": title})
                    return chat

                observer = create_saved_chat("Observer", "observer")
                first = create_saved_chat("Planning", "website")
                search = {"local_only": True, "search": "planning"}
                listed = app.tool(observer, "contacts", search)
                assert listed["local_host"] == "fixture-win"
                assert [x["thread_id"] for x in listed["local_chats"]] == [first], listed
                second = create_saved_chat("Planning", "release docs")
                matches = app.tool(observer, "contacts", {**search, "search": "PLANNING"})["local_chats"]
                assert {(x["thread_id"], x["folder"]) for x in matches} == {
                    (first, "website"), (second, "release docs"),
                }, matches
                assert app.tool(observer, "set_favorite", {"thread_id": first, "favorite": True})["favorite"]
                app.call("thread/name/set", {"threadId": first, "name": "Release planning"})
                latest = app.tool(observer, "contacts", {"local_only": True, "limit": 1})["local_chats"]
                assert len(latest) == 1 and latest[0]["title"] == "Release planning", latest
                assert latest[0]["address"] == f"fixture-win/{first}" and latest[0]["favorite"]
                app.call("thread/archive", {"threadId": first})
                remaining = app.tool(observer, "contacts", search)["local_chats"]
                assert [x["thread_id"] for x in remaining] == [second], remaining
                print("PASS: v0.4.0 with native Windows Codex found new local chats, searched titles, "
                      "distinguished duplicate titles by folder, kept the address on rename, "
                      "persisted a favorite, and excluded the archived chat.")
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Run this verification on a native Windows host.")
    verify(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True))
