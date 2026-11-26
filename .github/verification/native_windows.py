"""Verify Riftwire chat discovery and project work with native Codex."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
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
from unittest.mock import patch


class ResponsesFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = self.rfile.read(int(self.headers["Content-Length"]))
        if hasattr(self.server, "requests"):
            self.server.requests.put((self.path, json.loads(payload)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": getattr(self.server, "fixture_result", "Fixture saved."),
                                "annotations": []}]}
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
                print("PASS: native Windows Codex found new local chats, searched titles, "
                      "distinguished duplicate titles by folder, kept the address on rename, "
                      "persisted a favorite, and excluded the archived chat.")
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def verify_project_worker(codex, project_script):
    spec = importlib.util.spec_from_file_location("riftwire_projects", project_script)
    projects = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(projects)
    with tempfile.TemporaryDirectory(prefix="riftwire-worker-") as scratch, ExitStack() as cleanup:
        base = Path(scratch)
        codex_home = base / "codex home"
        codex_home.mkdir()
        checkout = base / "approved checkout"
        checkout.mkdir()
        env = projects.child_environment()
        env["CODEX_HOME"] = str(codex_home)
        cleanup.enter_context(patch.dict(os.environ, env, clear=True))

        def git(*args):
            return subprocess.run([projects.executable("git"), "-C", str(checkout), *args], check=True,
                                  capture_output=True, text=True, timeout=20).stdout.strip()

        git("init", "-q", "-b", "approved")
        git("config", "user.name", "Riftwire fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "core.autocrlf", "false")
        git("config", "core.hooksPath", str(base / "empty-hooks"))
        payload = b"Riftwire native worker fixture\n"
        (checkout / "source.txt").write_bytes(payload)
        git("add", "source.txt")
        git("commit", "-qm", "Approved input")
        revision = git("rev-parse", "HEAD")

        store = projects.ProjectStore(base / "project state")
        worker = store.initialize("fixture-worker")
        repo = "https://example.invalid/fixture/riftwire"
        definition = {"version": 1, "project": "fixture", "repository": repo, "name": "Worker fixture", "steps": [
            {"id": "build", "role": "builder", "action": "build", "task": "Build the fixture output.",
             "artifacts": ["output.bin"]},
            {"id": "review", "role": "reviewer", "action": "review", "task": "Review the fixture output.",
             "needs": ["build"]},
            {"id": "approval", "role": "owner", "action": "approve", "task": "Record fixture approval.",
             "needs": ["review"]},
        ]}
        profile = {"version": 1, "project": "fixture", "repository": repo, "checkout": str(checkout),
                   "refs": ["refs/heads/approved"]}
        build = projects.local_profile({**profile, "role": "builder", "actions": {"build": {"command": [
            sys.executable, "-I", "-B", "-c",
            "from pathlib import Path\nPath('output.bin').write_bytes(Path('source.txt').read_bytes())",
        ]}}})
        review = projects.local_profile({**profile, "role": "reviewer",
                                         "actions": {"review": {"codex": str(codex), "timeout": 45}}})

        provider = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), ResponsesFixture))
        provider.requests = Queue()
        Thread(target=provider.serve_forever, daemon=True).start()
        cleanup.callback(provider.shutdown)
        native_run = subprocess.run

        def use_fixture_provider(command, *args, **kwargs):
            if str(command[0]) == str(codex):
                worker_env = kwargs["env"]
                assert worker_env.get("CODEX_HOME") == str(codex_home)
                assert (Path(worker_env["RIFTWIRE_PROJECT_INPUTS"]) / "build" / "output.bin").read_bytes() == payload
                # Inject only the provider settings for the real Codex executable.
                options = [
                    "-c", 'model="riftwire-fixture"', "-c", 'model_provider="fixture"',
                    "-c", 'model_providers.fixture.name="fixture"', "-c", 'model_providers.fixture.wire_api="responses"',
                    "-c", f'model_providers.fixture.base_url="http://127.0.0.1:{provider.server_port}/v1"',
                    "-c", "analytics.enabled=false", "-c", "check_for_update_on_startup=false",
                ]
                command = [command[0], *options, *command[1:]]
                launched = native_run(command, *args, **kwargs)
                if launched.returncode:
                    raise AssertionError(Path(kwargs["stdout"].name).read_text(encoding="utf-8", errors="replace")[-4000:])
                return launched
            return native_run(command, *args, **kwargs)

        with patch.object(projects.subprocess, "run", side_effect=use_fixture_provider):
            for outcome in ("passed", "failed"):
                summary = f"Native worker recorded a {outcome} verdict."
                provider.fixture_result = json.dumps({"outcome": outcome, "summary": summary})
                job = store.start(definition, revision, {})["job"]
                assert projects.run_worker(store, build)["outcome"] == "passed"
                result = projects.run_worker(store, review)
                record = store.result(job, "review")
                assert result["outcome"] == outcome, record["envelope"]["result"]["summary"]
                assert result["result_sha256"] == projects.object_id(record["envelope"]), result
                checked = projects.verify_record(record, worker, definition, revision, job)
                assert checked["outcome"] == outcome, checked
                digest = projects.sha(payload)
                assert checked["inputs"] == {"build": {"output.bin": digest}}, checked
                step = next(item for item in store.status(job)["steps"] if item["id"] == "review")
                thread_id = record["envelope"]["result"]["chat"]
                assert step["chat"] == f"codex://threads/{thread_id}" and step["summary"] == summary, step
                rollouts = list((codex_home / "sessions").rglob(f"rollout-*-{thread_id}.jsonl"))
                assert len(rollouts) == 1 and summary in rollouts[0].read_text(encoding="utf-8"), step["chat"]

                route, request = provider.requests.get(timeout=5)
                assert route == "/v1/responses", route
                assert request["text"]["format"]["schema"] == projects.CODEX_VERDICT_SCHEMA, request.get("text")
                prompt = json.dumps(request["input"])
                assert revision in prompt and "output.bin" in prompt
                approval = next(item for item in store.status(job)["steps"] if item["id"] == "approval")
                if outcome == "passed":
                    assert approval["state"] == "needs-approval", approval
                    store.approve(job, "approval", "Reviewed the fixture receipt.")
                else:
                    try:
                        store.approve(job, "approval", "The failed review must gate approval.")
                    except ValueError as error:
                        assert "required checks passed" in str(error), error
                    else:
                        raise AssertionError("The failed review allowed approval.")

            git("switch", "-q", "-c", "unapproved")
            (checkout / "source.txt").write_bytes(b"Unapproved fixture input\n")
            git("commit", "-qam", "Unapproved input")
            rejected_revision = git("rev-parse", "HEAD")
            git("switch", "-q", "approved")
            rejected = store.start(definition, rejected_revision, {})["job"]
            assert projects.run_worker(store, build)["outcome"] == "failed"
            blocked = store.result(rejected, "build")["envelope"]["result"]
            assert "under a permitted Git ref" in blocked["summary"], blocked
            assert provider.requests.empty()
            assert git("status", "--porcelain") == "" and (checkout / "source.txt").read_bytes() == payload

        print("PASS: the native Codex worker persisted both verdicts and signed their artifact inputs. "
              "The failed verdict gated approval and the worker rejected an unapproved Git revision.")


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Run this verification on a native Windows host.")
    codex = Path(sys.argv[1]).resolve(strict=True)
    entry = Path(sys.argv[2]).resolve(strict=True)
    verify(codex, entry)
    verify_project_worker(codex, entry.with_name("riftwire_projects.py"))
