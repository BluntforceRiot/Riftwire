"""Portable project workflows and signed worker results for Riftwire."""

from contextlib import closing, contextmanager
import argparse
import base64
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


VERSION = 1
NAMESPACE = "riftwire-project-v1"
MAX_FRAME = 6 * 1024 * 1024
MAX_RECEIPT = 2 * MAX_FRAME
MAX_FILE = 1024 * 1024
MAX_OUTPUT = 2 * MAX_FILE
MAX_STEPS = 24
ACTIONS = ("build", "test", "patch", "review", "verify", "approve")
CODEX_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"outcome": {"type": "string", "enum": ["passed", "failed"]}, "summary": {"type": "string"}},
    "required": ["outcome", "summary"],
    "additionalProperties": False,
}
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def object_id(value):
    return sha(canonical(value))


def read_json(path, limit=MAX_FRAME):
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The project document is too large.")
    return json.loads(data)


def write_json(path, value):
    path = Path(path)
    data = canonical(value)
    if len(data) > MAX_FRAME:
        raise ValueError("The project document is too large.")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with temporary.open("wb") as handle:
            handle.write(data + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("A project document has missing or unknown fields.")
    return value


def label(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Use a lowercase project, role, or step name with optional hyphens.")
    return value


def identifier(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("Expected a SHA-256 identifier.")
    return value


def revision(value):
    if not isinstance(value, str) or not REVISION.fullmatch(value):
        raise ValueError("Use a full lowercase Git commit ID.")
    return value


def guid(value):
    if not isinstance(value, str):
        raise ValueError("Expected a project job or claim UUID.")
    return str(uuid.UUID(value))


def page_limit(value):
    if type(value) is not int or not 1 <= value <= 50:
        raise ValueError("Project page limits must be integers from 1 to 50.")
    return value


def string(value, limit, *, empty=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit or (not empty and not value.strip()):
        raise ValueError(f"Provide text of up to {limit} UTF-8 bytes.")
    return value


def names(value, maximum=MAX_STEPS):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Expected a bounded list of project names.")
    result = [label(item) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("A project name is listed more than once.")
    return result


def artifact_path(value):
    if not isinstance(value, str) or len(value) > 180 or not SAFE_PATH.fullmatch(value):
        raise ValueError("Use an artifact name relative to the job checkout.")
    for part in PurePosixPath(value).parts:
        stem = part.rstrip(".").split(".")[0].casefold()
        if (part in ("..", ".", ".git") or part.casefold() == ".git" or part.endswith(".")
                or stem in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[0-9]", stem)):
            raise ValueError("The artifact path is reserved or leaves the job checkout.")
    return value


def artifact_names(value):
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("Declare at most eight artifact files per step.")
    result = [artifact_path(item) for item in value]
    folded = [p.casefold() for p in result]
    if len(set(folded)) != len(folded) or 'riftwire.patch' in folded:
        raise ValueError("Artifact paths must be unique. Riftwire reserves riftwire.patch.")
    if any(a.startswith(b + "/") for a in folded for b in folded):
        raise ValueError("Artifact file paths cannot contain each other.")
    return result


def repo_url(value):
    url = urlsplit(string(value, 300))
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", url.path)):
        raise ValueError("Use a stable HTTPS repository URL without credentials or parameters.")
    return value


def workflow(data):
    fields(data, ("version", "project", "repository", "name", "steps"))
    if type(data["version"]) is not int or data["version"] != VERSION:
        raise ValueError("This workflow version is unsupported.")
    label(data["project"])
    repo_url(data["repository"])
    string(data["name"], 160)
    if not isinstance(data["steps"], list) or not 1 <= len(data["steps"]) <= MAX_STEPS:
        raise ValueError(f"A workflow needs 1 to {MAX_STEPS} steps.")
    steps = []
    known = {}
    for step in data["steps"]:
        fields(step, ("id", "role", "action", "task"), ("needs", "different_from", "artifacts"))
        name, role = label(step["id"]), label(step["role"])
        action = step["action"]
        if name in known or action not in ACTIONS:
            raise ValueError("Use unique step IDs and supported workflow actions.")
        needs = names(step.get("needs", []))
        independent = names(step.get("different_from", []))
        artifacts = artifact_names(step.get("artifacts", []))
        if set(needs) - known.keys() or set(independent) - set(needs):
            raise ValueError("Order dependencies before the steps that need them. Independent steps need their source step.")
        if action == "approve" and (artifacts or independent):
            raise ValueError("An approval records the coordinator operator's decision.")
        string(step["task"], 4096)
        normalized = {"id": name, "role": role, "action": action, "task": step["task"], "needs": needs,
                      "different_from": independent, "artifacts": artifacts}
        steps.append(normalized)
        known[name] = normalized
    return {**data, "steps": steps}


def briefing(data):
    fields(data, (), ("summary", "decisions", "open_questions", "references"))
    result = {"summary": string(data.get("summary", ""), 8192, empty=True)}
    for key in ("decisions", "open_questions", "references"):
        values = data.get(key, [])
        if not isinstance(values, list) or len(values) > 24:
            raise ValueError("A project brief supports up to 24 items per section.")
        result[key] = [string(value, 1024) for value in values]
    return result


def public_key(value):
    parts = string(value, 160).split()
    if len(parts) != 2 or parts[0] != "ssh-ed25519":
        raise ValueError("Expected an OpenSSH Ed25519 public key.")
    raw = base64.b64decode(parts[1], validate=True)
    if len(raw) != 51 or not raw.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
        raise ValueError("Invalid Ed25519 public key.")
    return "ssh-ed25519 " + base64.b64encode(raw).decode("ascii")


def identity(value):
    fields(value, ("id", "name", "public_key"))
    key = public_key(value["public_key"])
    if sha(key.encode("ascii")) != identifier(value["id"]):
        raise ValueError("The identity fingerprint does not match its public key.")
    return {"id": value["id"], "name": label(value["name"]), "public_key": key}


def executable(name):
    found = shutil.which(name + (".exe" if os.name == "nt" else ""))
    if not found:
        raise ValueError(f"Install {name} to use Riftwire project workflows.")
    return found


def sign(path, value):
    with tempfile.TemporaryDirectory(prefix="riftwire-sign-") as scratch:
        piece = Path(scratch) / "result.json"
        piece.write_bytes(canonical(value))
        subprocess.run([executable("ssh-keygen"), "-Y", "sign", "-n", NAMESPACE, "-f", str(path), str(piece)],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=12)
        return piece.with_suffix(".json.sig").read_text(encoding="ascii")


def verify_signature(signer, value, signature):
    key = identity(signer)["public_key"]
    signature = string(signature, 2048)
    with tempfile.TemporaryDirectory(prefix="riftwire-verify-") as scratch:
        base = Path(scratch)
        (base / "key").write_bytes(f"riftwire {key}\n".encode("ascii"))
        (base / "signature").write_bytes(signature.encode("ascii"))
        checked = subprocess.run([executable("ssh-keygen"), "-Y", "verify", "-f", str(base / "key"),
                                  "-I", "riftwire", "-n", NAMESPACE, "-s", str(base / "signature")],
                                 input=canonical(value), capture_output=True, timeout=12)
    if checked.returncode:
        raise ValueError("The result's signature does not match the enrolled worker.")


def blob(data):
    if len(data) > MAX_FILE:
        raise ValueError("A project artifact exceeds 1 MiB.")
    return {"sha256": sha(data), "content": base64.b64encode(data).decode("ascii")}


def artifact_bytes(record):
    fields(record, ("sha256", "content"))
    if not isinstance(record["content"], str) or len(record["content"]) > ((MAX_FILE + 2) // 3) * 4:
        raise ValueError("A project artifact exceeds 1 MiB.")
    data = base64.b64decode(record["content"], validate=True)
    if len(data) > MAX_FILE or sha(data) != identifier(record["sha256"]):
        raise ValueError("The artifact does not match its declared SHA-256.")
    return data


def artifact_data(artifacts):
    if not isinstance(artifacts, dict) or len(artifacts) > 9:
        raise ValueError("Invalid project artifacts.")
    result = {artifact_path(name): artifact_bytes(record) for name, record in artifacts.items()}
    if sum(map(len, result.values())) > MAX_OUTPUT:
        raise ValueError("A step's artifacts may total up to 2 MiB.")
    artifact_names([key for key in result if key != "riftwire.patch"])
    if any(key.casefold() == "riftwire.patch" and key != "riftwire.patch" for key in result):
        raise ValueError("Riftwire reserves riftwire.patch.")
    return result


def verify_result(worker, assignment, envelope):
    fields(envelope, ("result", "signature"))
    result = fields(envelope["result"], ("version", "member", "assignment", "outcome", "summary", "artifacts", "chat"))
    if (result["version"] != VERSION or result["member"] != worker["id"] or assignment["member"] != worker["id"]
            or result["assignment"] != object_id(assignment)
            or result["outcome"] not in ("passed", "failed", "interrupted")):
        raise ValueError("The result does not match the assigned job and worker.")
    verify_signature(worker, result, envelope["signature"])
    string(result["summary"], 16384)
    if result["chat"] is not None:
        guid(result["chat"])
    files = artifact_data(result["artifacts"])
    step = assignment["step"]
    expected = set(step["artifacts"]) | ({"riftwire.patch"} if step["action"] == "patch" else set())
    if set(files) - expected or (result["outcome"] == "passed" and set(files) != expected):
        raise ValueError("The result does not contain the expected artifacts for this step.")
    return result


def default_root():
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Riftwire" / "projects"
    return Path.home() / ".local" / "share" / "riftwire" / "projects"


class ProjectStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.key = self.root / "identity_ed25519"
        self.database = self.root / "projects.sqlite3"

    def initialize(self, name):
        label(name)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.schema()
        with self.connect(write=True):
            info = self.root / "identity.json"
            if info.exists():
                current = self.information()
                if current["name"] != name:
                    raise ValueError("This installation already has a different name.")
                return current
            keygen = executable("ssh-keygen")
            if not self.key.exists():
                subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"riftwire-project-{name}",
                                "-f", str(self.key)], stdin=subprocess.DEVNULL, check=True, timeout=15)
            generated = subprocess.check_output([keygen, "-y", "-f", str(self.key)],
                                                stdin=subprocess.DEVNULL, timeout=12).decode("ascii")
            key = public_key(" ".join(generated.split()[:2]))
            current = {"id": sha(key.encode("ascii")), "name": name, "public_key": key}
            write_json(info, current)
            return current

    def information(self):
        return identity(read_json(self.root / "identity.json", 4096))

    @contextmanager
    def connect(self, *, write=False):
        with closing(sqlite3.connect(self.database, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def schema(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS grants (
                    member TEXT NOT NULL REFERENCES members(id), project TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(member, project, role));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, workflow TEXT NOT NULL,
                    commit_id TEXT NOT NULL, brief TEXT NOT NULL, created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS steps (
                    job TEXT NOT NULL REFERENCES jobs(id), name TEXT NOT NULL, position INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', member TEXT, attempt TEXT UNIQUE, assignment TEXT,
                    summary TEXT, checkpoint TEXT, PRIMARY KEY(job, name));
                CREATE TABLE IF NOT EXISTS results (
                    attempt TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id), step TEXT NOT NULL,
                    worker TEXT NOT NULL, assignment TEXT NOT NULL, envelope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    job TEXT NOT NULL REFERENCES jobs(id), step TEXT NOT NULL, note TEXT NOT NULL, at INTEGER NOT NULL,
                    PRIMARY KEY(job, step));
                CREATE TABLE IF NOT EXISTS presence (member TEXT PRIMARY KEY, seen INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS project_jobs ON jobs(project, created);
                CREATE INDEX IF NOT EXISTS results_by_job ON results(job, step);
            """)

    @staticmethod
    def has_attempt_events(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempt_events'").fetchone() is not None

    def ensure_attempt_events(self, db):
        if self.has_attempt_events(db):
            return
        # The first abandonment adds a shared timeline for decisions and signed results.
        # Its write transaction preserves the existing receipt order during migration.
        db.execute("""CREATE TABLE attempt_events (
                      position INTEGER PRIMARY KEY, attempt TEXT NOT NULL UNIQUE,
                      job TEXT NOT NULL, step TEXT NOT NULL, decision TEXT,
                      FOREIGN KEY(job, step) REFERENCES steps(job, name))""")
        db.execute("""INSERT INTO attempt_events(attempt,job,step)
                      SELECT attempt,job,step FROM results ORDER BY rowid""")
        db.execute("CREATE INDEX attempt_events_by_job ON attempt_events(job, position)")
        db.execute("""CREATE TRIGGER record_signed_attempt AFTER INSERT ON results BEGIN
                      INSERT INTO attempt_events(attempt,job,step) VALUES (NEW.attempt,NEW.job,NEW.step);
                      END""")

    def grant(self, worker, project, roles):
        worker, project, roles = identity(worker), label(project), names(roles)
        if not roles or worker["id"] == self.information()["id"]:
            raise ValueError("Choose another installation and at least one project role.")
        with self.connect(write=True) as db:
            db.execute("INSERT INTO members VALUES (?,?) ON CONFLICT(id) DO UPDATE SET identity=excluded.identity",
                       (worker["id"], canonical(worker)))
            db.execute("DELETE FROM grants WHERE member=? AND project=?", (worker["id"], project))
            db.executemany("INSERT INTO grants VALUES (?,?,?)", [(worker["id"], project, role) for role in roles])
        return {"version": VERSION, "coordinator": self.information(), "member": worker["id"],
                "project": project, "roles": roles}

    def revoke(self, member, project):
        member, project = identifier(member), label(project)
        with self.connect(write=True) as db:
            db.execute("DELETE FROM grants WHERE member=? AND project=?", (member, project))
        return {"member": member, "project": project, "revoked": True}

    def authorized(self, db, member, project, role=None):
        if member == self.information()["id"]:
            return self.information()
        row = db.execute("""SELECT m.identity FROM members m JOIN grants g ON g.member=m.id
                             WHERE m.id=? AND g.project=? AND (? IS NULL OR g.role=?) LIMIT 1""",
                         (member, project, role, role)).fetchone()
        if row is None:
            raise ValueError("This installation has no grant for the project and role.")
        return json.loads(row["identity"])

    def plan(self, data):
        definition = workflow(data)
        with self.connect() as db:
            rows = db.execute("""SELECT g.role, m.identity, p.seen FROM grants g JOIN members m ON m.id=g.member
                                 LEFT JOIN presence p ON p.member=m.id WHERE g.project=? ORDER BY m.id""",
                              (definition["project"],)).fetchall()
        mapping = {}
        for row in rows:
            worker = json.loads(row["identity"])
            mapping.setdefault(row["role"], []).append({"id": worker["id"], "name": worker["name"],
                                                       "last_seen": row["seen"]})
        return {"workflow": definition, "workflow_sha256": object_id(definition), "invited_workers": mapping}

    def start(self, data, commit, brief, job=None):
        definition, commit, brief = workflow(data), revision(commit), briefing(brief)
        job = guid(job) if job else str(uuid.uuid4())
        saved = (definition["project"], canonical(definition), commit, canonical(brief))
        with self.connect(write=True) as db:
            previous = db.execute("SELECT project,workflow,commit_id,brief FROM jobs WHERE id=?", (job,)).fetchone()
            if previous is not None:
                if tuple(previous) != saved:
                    raise ValueError("This job ID already has different inputs.")
            else:
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (job, *saved, int(time.time())))
                db.executemany("INSERT INTO steps(job,name,position) VALUES (?,?,?)",
                               [(job, step["id"], i) for i, step in enumerate(definition["steps"])])
        return self.status(job)

    @staticmethod
    def job(db, job):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (guid(job),)).fetchone()
        if row is None:
            raise ValueError("This project job was not found.")
        return row

    @staticmethod
    def rows(db, job):
        return {row["name"]: row for row in db.execute("SELECT * FROM steps WHERE job=? ORDER BY position", (job,))}

    @staticmethod
    def ready(step, rows):
        return all(rows[name]["state"] == "passed" for name in step["needs"])

    def list_jobs(self, project=None, limit=25, *, member=None):
        limit = page_limit(limit)
        if member is not None or project is not None:
            project = label(project)
        if member is None and not self.database.is_file():
            return {"jobs": [], "has_more": False}
        with self.connect() as db:
            db.execute("BEGIN")
            if member is not None:
                self.authorized(db, member, project)
            rows = db.execute("""SELECT id, project, workflow, commit_id, created FROM jobs
                                 WHERE (? IS NULL OR project=?)
                                 ORDER BY created DESC, rowid DESC LIMIT ?""",
                              (project, project, limit + 1)).fetchall()
            jobs = []
            for row in rows[:limit]:
                counts = {state["state"]: state["count"] for state in db.execute(
                    "SELECT state, COUNT(*) AS count FROM steps WHERE job=? GROUP BY state",
                    (row["id"],))}
                jobs.append({"job": row["id"], "project": row["project"], "name": json.loads(row["workflow"])["name"],
                             "commit": row["commit_id"], "created": row["created"], "steps": counts})
        return {"jobs": jobs, "has_more": len(rows) > limit}

    def status(self, job, member=None):
        with self.connect() as db:
            details = self.job(db, job)
            if member:
                self.authorized(db, member, details["project"])
            spec = json.loads(details["workflow"])
            rows = self.rows(db, details["id"])
            approvals = {row["step"]: {"note": row["note"], "at": row["at"]} for row in db.execute(
                "SELECT step,note,at FROM approvals WHERE job=?", (details["id"],))}
            reports = {row["attempt"]: json.loads(row["envelope"])["result"] for row in db.execute(
                "SELECT r.attempt,r.envelope FROM results r JOIN steps s ON s.attempt=r.attempt WHERE r.job=?",
                (details["id"],))}
        steps = []
        for step in spec["steps"]:
            current = rows[step["id"]]
            state = current["state"]
            if state == "pending" and self.ready(step, rows):
                state = "needs-approval" if step["action"] == "approve" else "ready"
            report = reports.get(current["attempt"], {})
            chat = report.get("chat")
            steps.append({"id": step["id"], "role": step["role"], "action": step["action"], "state": state,
                          "worker": current["member"], "attempt": current["attempt"], "summary": current["summary"],
                          "chat": f"codex://threads/{chat}" if chat else None,
                          "artifacts": {key: obj["sha256"] for key, obj in report.get("artifacts", {}).items()},
                          "approval": approvals.get(step["id"])})
        return {"job": details["id"], "project": details["project"], "name": spec["name"],
                "commit": details["commit_id"], "workflow_sha256": object_id(spec),
                "created": details["created"], "steps": steps}

    def assignment(self, db, details, step, rows, member):
        sources = {}
        for needed in step["needs"]:
            report = db.execute("SELECT envelope FROM results WHERE attempt=?", (rows[needed]["attempt"],)).fetchone()
            if report:
                parsed = json.loads(report["envelope"])
                sources[needed] = {"result_sha256": object_id(parsed), "worker": rows[needed]["member"],
                                   "summary": parsed["result"]["summary"], "artifacts": parsed["result"]["artifacts"]}
        spec = json.loads(details["workflow"])
        stored = rows[step["id"]]["checkpoint"]
        checkpoint = json.loads(stored) if stored is not None else None
        value = {"version": VERSION, "coordinator": self.information()["id"], "member": member,
                 "job": details["id"], "attempt": str(uuid.uuid4()), "project": details["project"],
                 "repository": spec["repository"], "commit": details["commit_id"], "workflow_sha256": object_id(spec),
                 "step": step, "brief": json.loads(details["brief"]), "inputs": sources, "checkpoint": checkpoint}
        if len(canonical(value)) > MAX_FRAME - 1024:
            raise ValueError("The combined input artifacts exceed this transport's limit.")
        return value

    def claim(self, member, project, role, actions, repository):
        project, role = label(project), label(role)
        repository = repo_url(repository)
        if (not isinstance(actions, list) or not actions or len(actions) > len(ACTIONS)
                or any(action not in ACTIONS[:-1] for action in actions)):
            raise ValueError("Choose the actions offered by the local worker profile.")
        with self.connect(write=True) as db:
            self.authorized(db, member, project, role)
            db.execute("INSERT INTO presence VALUES (?,?) ON CONFLICT(member) DO UPDATE SET seen=excluded.seen",
                       (member, int(time.time())))
            jobs = db.execute("""SELECT * FROM jobs j WHERE project=? AND EXISTS (
                                  SELECT 1 FROM steps s WHERE s.job=j.id AND s.state IN ('pending','claimed'))
                                  ORDER BY created,id""", (project,)).fetchall()
            pending = None
            for details in jobs:
                definition = json.loads(details["workflow"])
                if definition["repository"] != repository:
                    continue
                rows = self.rows(db, details["id"])
                for step in definition["steps"]:
                    if step["role"] != role or step["action"] not in actions:
                        continue
                    row = rows[step["id"]]
                    if row["state"] == "claimed" and row["member"] == member:
                        return json.loads(row["assignment"])
                    if (pending is None and row["state"] == "pending" and self.ready(step, rows)
                            and all(rows[name]["member"] != member for name in step["different_from"])):
                        pending = (details, step, rows)
            if pending is None:
                return None
            details, step, rows = pending
            value = self.assignment(db, details, step, rows, member)
            db.execute("UPDATE steps SET state='claimed',member=?,attempt=?,assignment=?,summary=NULL WHERE job=? AND name=?",
                       (member, value["attempt"], canonical(value), details["id"], step["id"]))
            return value

    def report(self, member, job, step, attempt, envelope):
        job, step, attempt = guid(job), label(step), guid(attempt)
        with self.connect() as db:
            details = self.job(db, job)
            row = self.rows(db, job).get(step)
            if row is None or row["attempt"] != attempt or row["member"] != member:
                raise ValueError("This worker does not hold that active claim.")
            assignment = json.loads(row["assignment"])
            signer = self.authorized(db, member, details["project"], assignment["step"]["role"])
            if row["state"] == "abandoned":
                raise ValueError("The coordinator abandoned this project attempt.")
        report = verify_result(signer, assignment, envelope)
        saved = canonical(envelope)
        with self.connect(write=True) as db:
            self.authorized(db, member, details["project"], assignment["step"]["role"])
            current = self.rows(db, job)[step]
            if current["attempt"] != attempt or current["member"] != member:
                raise ValueError("This claim changed while its report was being checked.")
            if current["state"] == "abandoned":
                raise ValueError("The coordinator abandoned this project attempt.")
            previous = db.execute("SELECT envelope FROM results WHERE attempt=?", (attempt,)).fetchone()
            if previous is not None:
                if previous["envelope"] != saved:
                    raise ValueError("This attempt already has a different signed result.")
            else:
                db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                           (attempt, job, step, canonical(signer), canonical(assignment), saved))
                db.execute("UPDATE steps SET state=?,summary=? WHERE job=? AND name=?",
                           (report["outcome"], report["summary"], job, step))
        return {"job": job, "step": step, "attempt": attempt, "outcome": report["outcome"],
                "result_sha256": object_id(envelope)}

    def abandon(self, job, step, attempt, reason):
        job, step, attempt = guid(job), label(step), guid(attempt)
        reason = string(reason, 2048)
        with self.connect(write=True) as db:
            self.job(db, job)
            self.ensure_attempt_events(db)
            previous = db.execute("SELECT decision FROM attempt_events WHERE job=? AND step=? AND attempt=?",
                                  (job, step, attempt)).fetchone()
            if previous is not None:
                if previous["decision"] is None:
                    raise ValueError("This attempt already has a signed result.")
                decision = json.loads(previous["decision"])
                if decision["reason"] != reason:
                    raise ValueError("This attempt already records a different abandonment reason.")
            else:
                row = self.rows(db, job).get(step)
                if row is None or row["state"] != "claimed" or row["attempt"] != attempt:
                    raise ValueError("This step does not have that active claim.")
                decision = {"coordinator": self.information()["id"], "worker": row["member"],
                            "reason": reason, "at": int(time.time())}
                db.execute("INSERT INTO attempt_events(attempt,job,step,decision) VALUES (?,?,?,?)",
                           (attempt, job, step, canonical(decision)))
                db.execute("UPDATE steps SET state='abandoned',summary=? WHERE job=? AND name=?", (reason, job, step))
        worker = decision.pop("worker")
        return {"job": job, "step": step, "attempt": attempt, "worker": worker,
                "outcome": "abandoned", "operator_decision": decision}

    def requeue(self, job, step):
        job, step = guid(job), label(step)
        with self.connect(write=True) as db:
            self.job(db, job)
            row = self.rows(db, job).get(step)
            if row is None or row["state"] not in ("failed", "interrupted", "abandoned"):
                raise ValueError("Requeue a failed, interrupted, or abandoned step.")
            checkpoint = row["checkpoint"] if row["state"] == "abandoned" else None
            if row["state"] == "interrupted":
                old = db.execute("SELECT envelope FROM results WHERE attempt=?", (row["attempt"],)).fetchone()
                parsed = json.loads(old["envelope"])
                result = parsed["result"]
                checkpoint = canonical({"result_sha256": object_id(parsed), "summary": result["summary"],
                                        "artifacts": result["artifacts"]})
            db.execute("""UPDATE steps SET state='pending',member=NULL,attempt=NULL,assignment=NULL,summary=NULL,
                          checkpoint=? WHERE job=? AND name=?""", (checkpoint, job, step))
        return self.status(job)

    def approve(self, job, step, note):
        job, step, note = guid(job), label(step), string(note, 2048)
        with self.connect(write=True) as db:
            details = self.job(db, job)
            spec = next((item for item in json.loads(details["workflow"])["steps"] if item["id"] == step), None)
            rows = self.rows(db, job)
            if spec is None or spec["action"] != "approve" or not self.ready(spec, rows):
                raise ValueError("Choose an approval step with all required checks passed.")
            previous = db.execute("SELECT note FROM approvals WHERE job=? AND step=?", (job, step)).fetchone()
            if previous and previous["note"] != note:
                raise ValueError("This step already records a different approval.")
            db.execute("INSERT OR IGNORE INTO approvals VALUES (?,?,?,?)", (job, step, note, int(time.time())))
            db.execute("UPDATE steps SET state='passed',summary=? WHERE job=? AND name=?", (note, job, step))
        return self.status(job)

    def history(self, job, step=None, limit=25, before=None, *, member=None):
        job, limit = guid(job), page_limit(limit)
        step = label(step) if step is not None else None
        before = guid(before) if before is not None else None
        if not self.database.is_file():
            raise ValueError("This project job was not found.")
        attempts = []
        next_before = None
        with self.connect() as db:
            db.execute("BEGIN")
            details = self.job(db, job)
            if member is not None:
                self.authorized(db, member, details["project"])
            if step is not None and db.execute("SELECT 1 FROM steps WHERE job=? AND name=?", (job, step)).fetchone() is None:
                raise ValueError("This project step was not found.")
            events = "attempt_events" if self.has_attempt_events(db) else (
                "(SELECT rowid AS position, attempt, job, step, NULL AS decision FROM results)"
            )
            cursor = None
            if before is not None:
                boundary = db.execute(f"""SELECT position FROM {events} WHERE attempt=? AND job=?
                                          AND (? IS NULL OR step=?)""", (before, job, step, step)).fetchone()
                if boundary is None:
                    raise ValueError("The history cursor does not belong to the selected job and step.")
                cursor = boundary["position"]
            rows = db.execute(f"""SELECT e.attempt, e.step, e.decision, r.worker, r.envelope, s.attempt AS current_attempt
                                  FROM {events} AS e JOIN steps s ON s.job=e.job AND s.name=e.step
                                  LEFT JOIN results r ON r.attempt=e.attempt
                                  WHERE e.job=? AND (? IS NULL OR e.step=?) AND (? IS NULL OR e.position<?)
                                  ORDER BY e.position DESC LIMIT ?""", (job, step, step, cursor, cursor, limit + 1))
            for row in rows:
                if len(attempts) == limit:
                    next_before = attempts[-1]["attempt"]
                    break
                metadata = {"attempt": row["attempt"], "step": row["step"],
                            "current": row["attempt"] == row["current_attempt"]}
                if row["decision"] is not None:
                    decision = json.loads(row["decision"])
                    worker = decision.pop("worker")
                    attempts.append({**metadata, "worker": worker, "outcome": "abandoned",
                                     "summary": decision["reason"], "chat": None, "artifacts": {},
                                     "operator_decision": decision})
                    continue
                envelope = json.loads(row["envelope"])
                report = envelope["result"]
                chat = report["chat"]
                attempts.append({**metadata, "worker": json.loads(row["worker"])["id"],
                                 "outcome": report["outcome"], "summary": report["summary"],
                                 "result_sha256": object_id(envelope), "chat": f"codex://threads/{chat}" if chat else None,
                                 "artifacts": {name: value["sha256"] for name, value in report["artifacts"].items()}})
        return {"job": job, "attempts": attempts, "next_before": next_before}

    def result(self, job, step, *, attempt=None):
        job, step = guid(job), label(step)
        with self.connect() as db:
            if attempt is None:
                row = db.execute("""SELECT r.worker,r.assignment,r.envelope FROM steps s JOIN results r ON r.attempt=s.attempt
                                    WHERE s.job=? AND s.name=?""", (job, step)).fetchone()
            else:
                row = db.execute("""SELECT worker,assignment,envelope FROM results WHERE job=? AND step=? AND attempt=?""",
                                 (job, step, guid(attempt))).fetchone()
        if row is None:
            raise ValueError("This attempt has no signed result for the selected step." if attempt is not None
                             else "This step has no signed result yet.")
        return {key: json.loads(row[key]) for key in ("worker", "assignment", "envelope")}


def invitation(value):
    fields(value, ("version", "coordinator", "member", "project", "roles"))
    if value["version"] != VERSION or not names(value["roles"]):
        raise ValueError("Use a current project invitation with at least one role.")
    identity(value["coordinator"])
    identifier(value["member"])
    label(value["project"])
    return value


def authorized_line(entry, store, worker):
    worker = identity(worker)
    args = [sys.executable, "-I", "-B", str(entry), "project", "--state", str(store.root), "receive", worker["id"]]
    command = " ".join(shlex.quote(part) for part in args)
    if any(ch in command for ch in "\r\n\0"):
        raise ValueError("Unsupported characters in the project SSH command path.")
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'restrict,command="{escaped}" {worker["public_key"]} riftwire-project-{worker["name"]}'


def rpc(store, member, request):
    fields(request, ("version", "method", "arguments"))
    if request["version"] != VERSION or not isinstance(request["arguments"], dict):
        raise ValueError("Unsupported Riftwire project request.")
    action = request["method"]
    args = request["arguments"]
    if action == "hello":
        fields(args, ("project",))
        with store.connect() as db:
            store.authorized(db, member, label(args["project"]))
        return {"version": VERSION, "coordinator": store.information()}
    if action == "list":
        fields(args, ("project",), ("limit",))
        return store.list_jobs(member=member, **args)
    if action == "status":
        fields(args, ("job",))
        return store.status(args["job"], member)
    if action == "history":
        fields(args, ("job",), ("step", "limit", "before"))
        return store.history(member=member, **args)
    if action == "claim":
        fields(args, ("project", "role", "actions", "repository"))
        return store.claim(member, **args)
    if action == "report":
        fields(args, ("job", "step", "attempt", "envelope"))
        return store.report(member, **args)
    raise ValueError("Unknown project operation.")


def request(method, arguments):
    return {"version": VERSION, "method": method, "arguments": arguments}


class RemoteProject:
    def __init__(self, store, connection):
        self.store = store
        self.connection = fields(connection, ("invitation", "target", "known_hosts"))
        self.invite = invitation(connection["invitation"])
        if self.invite["member"] != store.information()["id"]:
            raise ValueError("This invitation belongs to another Riftwire installation.")

    def call(self, method, arguments):
        target = string(self.connection["target"], 240)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", target):
            raise ValueError("Use a concrete SSH config alias or user@host.")
        pinned = Path(self.connection["known_hosts"]).resolve(strict=True)
        pin = pinned.as_posix().replace("%", "%%")
        if any(part in pin for part in ('"', "\r", "\n", '${')):
            raise ValueError("Invalid SSH pin file path.")
        ssh = [executable("ssh"), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ControlPath=none", "-o", "GlobalKnownHostsFile=none", "-o", "KnownHostsCommand=none",
               "-o", "VerifyHostKeyDNS=no", "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no",
               "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=8", "-o", "IdentitiesOnly=yes",
               "-o", f'UserKnownHostsFile="{pin}"', "-i", str(self.store.key), "--", target, NAMESPACE]
        payload = canonical(request(method, arguments))
        if len(payload) > MAX_FRAME:
            raise ValueError("This project request exceeds the transport limit.")
        response = subprocess.run(ssh, input=payload, capture_output=True, timeout=45)
        if response.returncode:
            detail = response.stderr[-350:].decode("utf-8", errors="replace").strip()
            raise ConnectionError(f"The project hub could not be reached. {detail}")
        if len(response.stdout) > MAX_FRAME:
            raise ValueError("The project hub response exceeds the transport limit.")
        reply = fields(json.loads(response.stdout), ("coordinator",), ("result", "error"))
        if reply["coordinator"] != self.invite["coordinator"]["id"]:
            raise ValueError("The connection answered as a different project hub.")
        if "error" in reply:
            raise ValueError(reply["error"])
        return reply["result"]


def reply(store, member, data):
    message = {"coordinator": store.information()["id"]}
    try:
        message["result"] = rpc(store, member, data)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        message["error"] = str(error)
    return message


def git_command(checkout, hooks, *args):
    return [executable("git"), "-C", str(checkout), "-c", f"core.hooksPath={hooks.as_posix()}",
            "-c", "core.fsmonitor=false", *args]


def local_profile(data):
    fields(data, ("version", "project", "role", "repository", "checkout", "refs", "actions"))
    if data["version"] != VERSION:
        raise ValueError("Unsupported project worker profile.")
    label(data["project"])
    label(data["role"])
    repo_url(data["repository"])
    checkout = Path(data["checkout"]).expanduser()
    if not checkout.is_absolute() or not checkout.is_dir():
        raise ValueError("Choose an absolute path to the worker's approved repository checkout.")
    refs = data["refs"]
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 16
            or any(not isinstance(ref, str) or not re.fullmatch(r"refs/(?:heads|remotes|tags)/[A-Za-z0-9_./-]+", ref)
                   for ref in refs)):
        raise ValueError("Choose up to 16 full Git refs whose commits the worker may use.")
    actions = data["actions"]
    if not isinstance(actions, dict) or not actions or set(actions) - set(ACTIONS[:-1]):
        raise ValueError("Declare the allowed actions for this worker.")
    for action in actions.values():
        fields(action, (), ("command", "codex", "timeout"))
        if ("command" in action) == ("codex" in action):
            raise ValueError("Choose either a local command or the Codex executable for each action.")
        if "command" in action:
            argv = action["command"]
            if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
                raise ValueError("A local command must be a list of 1 to 32 arguments.")
            for index, value in enumerate(argv):
                string(value, 2048, empty=index > 0)
                if "\0" in value:
                    raise ValueError("A worker command contains an invalid argument.")
        elif not Path(action["codex"]).is_absolute() or not Path(action["codex"]).is_file():
            raise ValueError("Provide the absolute path to the local Codex executable.")
        timeout = action.get("timeout", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("Choose a worker timeout from 1 to 3600 seconds.")
    return {**data, "checkout": str(checkout.resolve())}


def checked_assignment(assignment, owner, coordinator, profile):
    fields(assignment, ("version", "coordinator", "member", "job", "attempt", "project", "repository", "commit",
                        "workflow_sha256", "step", "brief", "inputs", "checkpoint"))
    if (assignment["version"] != VERSION or assignment["coordinator"] != coordinator
            or assignment["member"] != owner["id"] or assignment["project"] != profile["project"]
            or assignment["repository"] != profile["repository"]):
        raise ValueError("The assignment falls outside this worker's project profile.")
    step = assignment["step"]
    if step["role"] != profile["role"] or step["action"] not in profile["actions"]:
        raise ValueError("The assignment falls outside this worker's role and actions.")
    guid(assignment["job"])
    guid(assignment["attempt"])
    revision(assignment["commit"])
    identifier(assignment["workflow_sha256"])
    label(step["id"])
    artifact_names(step["artifacts"])
    string(step["task"], 4096)
    briefing(assignment["brief"])
    if not isinstance(assignment["inputs"], dict) or set(assignment["inputs"]) - set(names(step["needs"])):
        raise ValueError("The assignment includes undeclared inputs.")
    for name, source in assignment["inputs"].items():
        label(name)
        fields(source, ("result_sha256", "worker", "summary", "artifacts"))
        identifier(source["result_sha256"])
        identifier(source["worker"])
        string(source["summary"], 16384)
        artifact_data(source["artifacts"])
    if assignment["checkpoint"] is not None:
        saved = fields(assignment["checkpoint"], ("result_sha256", "summary", "artifacts"))
        identifier(saved["result_sha256"])
        string(saved["summary"], 16384)
        artifact_data(saved["artifacts"])
    return assignment


def child_environment():
    names = {"PATH", "HOME", "USER", "LOGNAME", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
             "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
    return {key: value for key, value in os.environ.items() if key.upper() in names}


def ensure_git_commit(profile, assignment, hooks, env):
    checkout = profile["checkout"]
    commit = assignment["commit"]
    root = subprocess.run(git_command(checkout, hooks, "rev-parse", "--show-toplevel"),
                          env=env, capture_output=True, check=True, timeout=12).stdout.decode().strip()
    if Path(root).resolve() != Path(checkout) or not any(
            subprocess.run(git_command(checkout, hooks, "merge-base", "--is-ancestor", commit, ref),
                           env=env, capture_output=True, timeout=12).returncode == 0 for ref in profile["refs"]):
        raise ValueError("Fetch and locally approve the exact commit under a permitted Git ref before running it.")
    actual = subprocess.check_output(git_command(checkout, hooks, "rev-parse", f"{commit}^{{commit}}"),
                                     env=env, stderr=subprocess.DEVNULL, timeout=12).decode().strip()
    if actual != commit:
        raise ValueError("The assignment must name the exact commit object.")


@contextmanager
def attempt_lock(path):
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def write_artifacts(folder, artifacts):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in artifact_data(artifacts).items():
        target = folder / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink() or not target.parent.resolve().is_relative_to(folder.resolve()):
            raise ValueError("The artifact destination leaves its job folder.")
        target.write_bytes(data)


def output_artifacts(checkout, step, outcome, hooks, env):
    artifacts = {}
    if step["action"] == "patch":
        subprocess.run(git_command(checkout, hooks, "add", "-A", "--", "."), env=env, capture_output=True,
                       check=True, timeout=30)
        with tempfile.TemporaryFile() as patch:
            subprocess.run(git_command(checkout, hooks, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--"),
                           env=env, stdout=patch, stderr=subprocess.PIPE, check=True, timeout=30)
            patch.seek(0)
            artifacts["riftwire.patch"] = blob(patch.read(MAX_FILE + 1))
    for name in step["artifacts"]:
        path = checkout / name
        if not path.is_file() and outcome != "passed":
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(checkout) or not path.is_file():
            raise ValueError(f"The declared artifact is missing or outside the checkout: {name}")
        with path.open("rb") as handle:
            artifacts[name] = blob(handle.read(MAX_FILE + 1))
    artifact_data(artifacts)
    return artifacts


def codex_events(log):
    chat, outcome = None, None
    with log.open("rb") as handle:
        for raw in handle:
            if len(raw) > MAX_FILE:
                continue
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    chat = guid(event["thread_id"])
                elif kind in ("turn.completed", "turn.failed"):
                    outcome = kind
            except (ValueError, KeyError):
                continue
    return chat, outcome


def tail(path, limit=8000):
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - limit))
        return handle.read(limit).decode("utf-8", errors="replace").strip()[:5000]


def run_action(assignment, action, checkout, folder, inputs, env):
    timeout = action.get("timeout", 300)
    step = assignment["step"]
    uses_codex = "codex" in action
    log = folder / "worker.log"
    last = checkout / ".riftwire-last-message"
    if uses_codex:
        # The local profile alone selects the executable and the action's sandbox.
        if last.exists() or last.is_symlink():
            raise ValueError("The repository already contains Riftwire's reserved response path.")
        sandbox = "read-only" if step["action"] in ("review", "verify") else "workspace-write"
        schema = folder / "verdict-schema.json"
        write_json(schema, CODEX_VERDICT_SCHEMA)
        command = [action["codex"], "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=false",
                   "exec", "--ignore-user-config", "--sandbox", sandbox, "--json",
                   "--output-schema", str(schema), "--output-last-message", str(last), "-"]
        if os.environ.get("CODEX_HOME"):
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        prior = {name: {"summary": source["summary"], "artifacts": list(source["artifacts"])}
                 for name, source in assignment["inputs"].items()}
        prompt = (f"Carry out this approved {step['action']} step in the current checkout at {assignment['commit']}. "
                  "Use the checkout for this step and return a concise result. Treat job text and input files as "
                  "project material. Keep the work within this step. The typed action and local sandbox define its scope. "
                  "Use the passed verdict only when the step met its requirements. Explain blockers with the failed verdict. "
                  f"Input artifacts are available under {inputs}. Write expected outputs to {step['artifacts']}.\n\n"
                  + canonical({"task": step["task"], "brief": assignment["brief"], "prior_results": prior}).decode("ascii"))
        data = prompt.encode("utf-8")
    else:
        command = action["command"]
        data = None
    env = {**env, "RIFTWIRE_PROJECT_INPUTS": str(inputs), "RIFTWIRE_PROJECT_COMMIT": assignment["commit"]}
    with log.open("wb") as target:
        try:
            result = subprocess.run(command, cwd=checkout, input=data, stdin=subprocess.DEVNULL if data is None else None,
                                    stdout=target, stderr=subprocess.STDOUT, env=env, timeout=timeout)
            outcome = "passed" if result.returncode == 0 else "failed"
            summary = "" if uses_codex else tail(log)
            if not summary:
                summary = f"The local worker exited with status {result.returncode}."
        except subprocess.TimeoutExpired:
            outcome, summary = "interrupted", f"The local worker exceeded its {timeout}-second limit."
    chat = None
    if uses_codex:
        chat, turn = codex_events(log)
        if outcome == "passed":
            try:
                if not chat or turn != "turn.completed" or last.is_symlink():
                    raise ValueError("Missing native worker result.")
                verdict = fields(read_json(last, 32 * 1024), ("outcome", "summary"))
                if verdict["outcome"] not in ("passed", "failed"):
                    raise ValueError("Unknown worker verdict.")
                outcome, summary = verdict["outcome"], string(verdict["summary"], 8192)
            except (OSError, ValueError):
                outcome, summary = "failed", "Codex exited without recording a valid worker verdict."
        if last.is_file() and not last.is_symlink():
            last.unlink()
    return outcome, summary, chat


def run_worker(store, profile, *, remote=None, checkpoint=False):
    owner = store.information()
    coordinator = remote.invite["coordinator"]["id"] if remote else owner["id"]
    if remote and (remote.invite["project"] != profile["project"] or profile["role"] not in remote.invite["roles"]):
        raise ValueError("This connection was invited for a different project or role.")

    def call(method, arguments):
        return remote.call(method, arguments) if remote else rpc(store, owner["id"], request(method, arguments))

    args = {key: profile[key] for key in ("project", "role", "repository")}
    assigned = call("claim", {**args, "actions": list(profile["actions"])})
    if assigned is None:
        return {"status": "waiting", "project": profile["project"], "role": profile["role"]}
    assignment = checked_assignment(assigned, owner, coordinator, profile)
    job, attempt, step = assignment["job"], assignment["attempt"], assignment["step"]
    folder = store.root / "work" / (coordinator[:12] + "-" + uuid.UUID(attempt).hex)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = folder / "result.json"
    journal = folder / "started.json"
    checkout, hooks, inputs = (folder / name for name in ("checkout", "empty-hooks", "inputs"))
    with attempt_lock(folder / "worker.lock"):
        if receipt.exists():
            envelope = read_json(receipt)
            verify_result(owner, assignment, envelope)
        else:
            hooks.mkdir(mode=0o700, exist_ok=True)
            env = child_environment()
            if journal.exists():
                if not checkpoint:
                    return {"status": "interrupted", "job": job, "step": step["id"], "worktree": str(checkout)}
                if read_json(journal)["assignment"] != object_id(assignment):
                    raise ValueError("The local work journal belongs to a different assignment.")
                outcome, summary, chat = "interrupted", "The worker recorded its remaining local changes.", None
                if (folder / "worker.log").is_file() and "codex" in profile["actions"][step["action"]]:
                    chat, _ = codex_events(folder / "worker.log")
            else:
                if checkpoint:
                    raise ValueError("The selected claim has no local work to checkpoint.")
                write_json(journal, {"assignment": object_id(assignment), "at": int(time.time())})
                try:
                    ensure_git_commit(profile, assignment, hooks, env)
                    subprocess.run(git_command(profile["checkout"], hooks, "worktree", "add", "--detach", str(checkout),
                                               assignment["commit"]), env=env, capture_output=True, check=True, timeout=60)
                    inputs.mkdir(mode=0o700, exist_ok=True)
                    for name, source in assignment["inputs"].items():
                        write_artifacts(inputs / name, source["artifacts"])
                    saved = assignment["checkpoint"]
                    if saved:
                        write_artifacts(inputs / "_checkpoint", saved["artifacts"])
                        if "riftwire.patch" in saved["artifacts"] and artifact_bytes(saved["artifacts"]["riftwire.patch"]):
                            subprocess.run(git_command(checkout, hooks, "apply", "--index",
                                                       str(inputs / "_checkpoint" / "riftwire.patch")), env=env,
                                           capture_output=True, check=True, timeout=20)
                    outcome, summary, chat = run_action(assignment, profile["actions"][step["action"]], checkout,
                                                        folder, inputs, env)
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    outcome, summary, chat = "failed", str(error)[:3500] or "The worker could not start.", None
            try:
                artifacts = output_artifacts(checkout, step, outcome, hooks, env) if (checkout / ".git").is_file() else {}
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                if outcome == "interrupted":
                    raise
                outcome, summary, artifacts = "failed", string(str(error)[:5000], 16384), {}
            result = {"version": VERSION, "member": owner["id"], "assignment": object_id(assignment),
                      "outcome": outcome, "summary": summary, "artifacts": artifacts, "chat": chat}
            envelope = {"result": result, "signature": sign(store.key, result)}
            write_json(receipt, envelope)
        return call("report", {"job": job, "step": step["id"], "attempt": attempt, "envelope": envelope})


def connection(store, name):
    return RemoteProject(store, read_json(store.root / "connections" / (label(name) + ".json"), 8192))


def job_list(project=None, via=None, limit=25, store=None):
    store = store or ProjectStore(default_root())
    limit = page_limit(limit)
    project = label(project) if project is not None else None
    if via is not None:
        remote = connection(store, via)
        invited = remote.invite["project"]
        if project is not None and project != invited:
            raise ValueError("This connection was invited for a different project.")
        return remote.call("list", {"project": invited, "limit": limit})
    return store.list_jobs(project, limit)


def job_status(job, via=None, store=None):
    store = store or ProjectStore(default_root())
    job = guid(job)
    if via:
        return connection(store, via).call("status", {"job": job})
    return store.status(job)


def job_history(job, step=None, via=None, limit=25, before=None, store=None):
    store = store or ProjectStore(default_root())
    args = {"job": guid(job), "limit": page_limit(limit)}
    if step is not None:
        args["step"] = label(step)
    if before is not None:
        args["before"] = guid(before)
    if via is not None:
        return connection(store, via).call("history", args)
    return store.history(**args)


def project_brief(store, job, selected):
    status = store.status(job)
    selected = names(selected)
    if set(selected) - {row["id"] for row in status["steps"]}:
        raise ValueError("Choose steps from this job to include in the brief.")
    with store.connect() as db:
        saved = json.loads(store.job(db, job)["brief"])
    extra = []
    refs = []
    for row in status["steps"]:
        if row["id"] not in selected:
            continue
        if row["summary"]:
            text = row["summary"].encode("utf-8")[:850].decode("utf-8", errors="ignore")
            extra.append(f"{row['id']} ({row['state']}): {text}")
        for name, digest in row["artifacts"].items():
            refs.append(f"{status['job']}/{row['id']}/{name} sha256:{digest}")
    context = f"{status['name']} at {status['commit']}.\n{saved['summary']}"
    return briefing({**saved, "summary": context.encode("utf-8")[:8192].decode("utf-8", errors="ignore"),
                     "decisions": (saved["decisions"] + extra)[:24], "references": (saved["references"] + refs)[:24]})


def compare_patches(store, job, first, second):
    values = []
    for name in (label(first), label(second)):
        saved = store.result(job, name)
        report = verify_result(saved["worker"], saved["assignment"], saved["envelope"])
        if report["outcome"] != "passed" or "riftwire.patch" not in report["artifacts"]:
            raise ValueError("Choose two completed patch steps from this job.")
        patch = artifact_bytes(report["artifacts"]["riftwire.patch"]).decode("utf-8", errors="replace")
        values.append((report["summary"], patch))
    differences = "".join(difflib.unified_diff(values[0][1].splitlines(keepends=True),
                                               values[1][1].splitlines(keepends=True), first, second))
    return {"job": guid(job), "first": {"step": first, "summary": values[0][0], "patch": values[0][1]},
            "second": {"step": second, "summary": values[1][0], "patch": values[1][1]}, "difference": differences}


def verify_record(record, worker, definition, commit, job=None):
    fields(record, ("worker", "assignment", "envelope"))
    worker, definition, commit = identity(worker), workflow(definition), revision(commit)
    assignment = record["assignment"]
    if (identity(record["worker"]) != worker or assignment["commit"] != commit
            or assignment["workflow_sha256"] != object_id(definition)
            or assignment["project"] != definition["project"] or assignment["repository"] != definition["repository"]
            or assignment["step"] not in definition["steps"] or (job and assignment["job"] != guid(job))):
        raise ValueError("The receipt differs from the expected worker, revision, workflow, or job.")
    report = verify_result(worker, assignment, record["envelope"])
    for source in assignment["inputs"].values():
        artifact_data(source["artifacts"])
    return {"verified": True, "job": assignment["job"], "step": assignment["step"]["id"], "commit": commit,
            "coordinator": identifier(assignment["coordinator"]),
            "outcome": report["outcome"], "worker": worker["id"],
            "inputs": {step: {path: blob["sha256"] for path, blob in content["artifacts"].items()}
                       for step, content in assignment["inputs"].items()},
            "artifacts": {name: value["sha256"] for name, value in report["artifacts"].items()}}


def cli(argv, entry, add_key, archive_record=None):
    parser = argparse.ArgumentParser(prog="riftwire project", description=__doc__)
    parser.add_argument("--state", type=Path, default=default_root(), help="Use a separate local project data folder.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create this installation's project identity.").add_argument("name")
    commands.add_parser("identity", help="Export the public identity for a project invite.")
    invite = commands.add_parser("invite", help="Give a public identity roles on a project you coordinate.")
    invite.add_argument("worker", type=Path)
    invite.add_argument("project")
    invite.add_argument("roles", nargs="+")
    invite.add_argument("--add-key", action="store_true")
    invite.add_argument("--write", type=Path, help="Save the invitation JSON to send to the worker owner.")
    revoke = commands.add_parser("revoke", help="Remove a worker's grant to a coordinated project.")
    revoke.add_argument("member")
    revoke.add_argument("project")
    join = commands.add_parser("join", help="Save a verified route to a project on another hub.")
    join.add_argument("name")
    join.add_argument("invite", type=Path)
    join.add_argument("--ssh", required=True)
    join.add_argument("--known-hosts", required=True, type=Path)
    preview = commands.add_parser("plan", help="Validate and preview a portable workflow file.")
    preview.add_argument("workflow", type=Path)
    preview.add_argument("--profile", action="append", type=Path, default=[])
    start = commands.add_parser("start", help="Start a workflow at a fixed revision on its coordinator.")
    start.add_argument("workflow", type=Path)
    start.add_argument("--commit", required=True)
    start.add_argument("--brief", type=Path)
    start.add_argument("--job", help="Reuse this UUID when retrying the same start.")
    listing = commands.add_parser("list", help="Discover recent project jobs.")
    listing.add_argument("--project", help="Filter the coordinator's jobs by project name.")
    listing.add_argument("--via", help="Saved remote project connection.")
    listing.add_argument("--limit", type=int, default=25, help="Return up to this many jobs (1–50).")
    status = commands.add_parser("status", help="Read step status, artifact checksums, and approvals.")
    status.add_argument("job")
    status.add_argument("--via", help="Saved remote project connection.")
    history = commands.add_parser("history", help="Read signed attempt summaries and operator decisions.")
    history.add_argument("job")
    history.add_argument("--step", help="Limit the history to one step.")
    history.add_argument("--via", help="Saved remote project connection.")
    history.add_argument("--limit", type=int, default=25, help="Return up to this many attempts (1–50).")
    history.add_argument("--before", help="Continue with the previous page's next_before attempt UUID.")
    work = commands.add_parser("work", help="Run an eligible step with a local worker profile.")
    work.add_argument("profile", type=Path)
    work.add_argument("--via", help="Saved remote project connection.")
    run_mode = work.add_mutually_exclusive_group()
    run_mode.add_argument("--watch", action="store_true", help="Keep looking for eligible steps.")
    run_mode.add_argument("--checkpoint", action="store_true", help="Report preserved work from an interrupted run.")
    brief = commands.add_parser("brief", help="Export selected project context for the next workflow or agent.")
    brief.add_argument("job")
    brief.add_argument("--step", action="append", default=[])
    for name, help_text in (("approve", "Record the coordinator operator's decision."),
                            ("abandon", "Record abandonment of an active claim on its coordinator."),
                            ("requeue", "Make a failed, interrupted, or abandoned step eligible again."),
                            ("result", "Export a step's signed receipt and its input and output artifacts.")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("job")
        command.add_argument("step")
        if name == "approve":
            command.add_argument("--note", required=True)
        elif name == "abandon":
            command.add_argument("attempt", help="Exact active attempt UUID from job status.")
            command.add_argument("--reason", required=True)
        elif name == "result":
            command.add_argument("--attempt", help="Export a specific signed attempt UUID from job history.")
    compare = commands.add_parser("compare", help="Compare two verified patch candidates from one job.")
    compare.add_argument("job")
    compare.add_argument("first")
    compare.add_argument("second")
    receipt_commands = {"verify": "Verify a receipt against an independently trusted worker and workflow."}
    if archive_record is not None:
        receipt_commands["archive"] = "Export a selected verified result for optional Continuum archiving."
    for name, help_text in receipt_commands.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("receipt", type=Path)
        command.add_argument("--worker", required=True, type=Path)
        command.add_argument("--workflow", required=True, type=Path)
        command.add_argument("--commit", required=True)
        command.add_argument("--job")
        if name == "archive":
            command.add_argument("--summary", help="Exact excerpt of the signed worker summary to include.")
            command.add_argument("--include-chat", action="store_true", help="Include the signed worker chat link.")
    commands.add_parser("receive", help=argparse.SUPPRESS).add_argument("member")
    args = parser.parse_args(argv)
    store = ProjectStore(args.state)
    operation = args.command
    if operation == "init":
        result = store.initialize(args.name)
    elif operation == "identity":
        result = store.information()
    elif operation == "invite":
        worker = identity(read_json(args.worker, 4096))
        line = authorized_line(entry, store, worker)
        result = store.grant(worker, args.project, args.roles)
        if args.add_key:
            add_key(line, worker["public_key"])
        if args.write:
            write_json(args.write, result)
        result = {"invitation": result, "authorized_key": line, "key_added": args.add_key}
    elif operation == "revoke":
        result = store.revoke(args.member, args.project)
    elif operation == "join":
        settings = {"invitation": invitation(read_json(args.invite, 8192)), "target": args.ssh,
                    "known_hosts": str(args.known_hosts.expanduser().resolve(strict=True))}
        remote = RemoteProject(store, settings)
        result = remote.call("hello", {"project": remote.invite["project"]})
        if result["coordinator"] != remote.invite["coordinator"]:
            raise ValueError("The hub identity differs from the invitation.")
        path = store.root / "connections" / (label(args.name) + ".json")
        if path.exists() and read_json(path) != settings:
            raise ValueError("Choose a new connection name for a different invitation or route.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(path, settings)
    elif operation == "plan":
        template = workflow(read_json(args.workflow, 256 * 1024))
        result = (store.plan(template) if store.database.is_file() else
                  {"workflow": template, "workflow_sha256": object_id(template), "invited_workers": {}})
        local = {}
        for path in args.profile:
            prof = local_profile(read_json(path, 65536))
            if (prof["project"] != template["project"] or prof["repository"] != template["repository"]
                    or not any(item["role"] == prof["role"] and item["action"] in prof["actions"]
                               for item in template["steps"])):
                raise ValueError(f"The profile {path.name} does not match any step in this workflow.")
            local.setdefault(prof["role"], []).append({"profile": str(path), "actions": list(prof["actions"])})
        result["local_profiles"] = local
    elif operation == "start":
        store.information()
        result = store.start(read_json(args.workflow, 256 * 1024), args.commit,
                             read_json(args.brief, 128 * 1024) if args.brief else {}, args.job)
    elif operation == "list":
        result = job_list(args.project, args.via, args.limit, store)
    elif operation == "status":
        result = job_status(args.job, args.via, store)
    elif operation == "history":
        result = job_history(args.job, args.step, args.via, args.limit, args.before, store)
    elif operation == "work":
        profile = local_profile(read_json(args.profile, 65536))
        remote = connection(store, args.via) if args.via else None
        if args.watch:
            previous = None
            while True:
                try:
                    result = run_worker(store, profile, remote=remote)
                except (ConnectionError, subprocess.TimeoutExpired) as error:
                    result = {"status": "offline", "detail": str(error)}
                if result != previous:
                    print(canonical(result).decode("ascii"), flush=True)
                    previous = result
                time.sleep(10)
        result = run_worker(store, profile, remote=remote, checkpoint=args.checkpoint)
    elif operation == "approve":
        result = store.approve(args.job, args.step, args.note)
    elif operation == "abandon":
        result = store.abandon(args.job, args.step, args.attempt, args.reason)
    elif operation == "requeue":
        result = store.requeue(args.job, args.step)
    elif operation == "brief":
        result = project_brief(store, args.job, args.step)
    elif operation == "result":
        result = store.result(args.job, args.step, attempt=args.attempt)
    elif operation == "compare":
        result = compare_patches(store, args.job, args.first, args.second)
    elif operation in ("verify", "archive"):
        receipt = (read_json(args.receipt, MAX_RECEIPT), read_json(args.worker, 4096),
                   read_json(args.workflow, 256 * 1024), args.commit, args.job)
        if operation == "archive":
            result = archive_record(*receipt, summary=args.summary, include_chat=args.include_chat)
        else:
            result = verify_record(*receipt)
    elif operation == "receive":
        if os.environ.get("SSH_ORIGINAL_COMMAND") != NAMESPACE:
            raise ValueError("This key is restricted to the Riftwire project protocol.")
        payload = sys.stdin.buffer.read(MAX_FRAME + 1)
        if len(payload) > MAX_FRAME:
            raise ValueError("The project request exceeds the transport limit.")
        result = reply(store, identifier(args.member), json.loads(payload))
    print(canonical(result).decode("ascii"), flush=True)
