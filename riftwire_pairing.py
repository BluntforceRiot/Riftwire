"""Durable invitations for the owner-authenticated Riftwire setup."""
import base64
import binascii
import ipaddress
import json
import re
import uuid


DEVICE_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
SSH_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
KEY_HEADER = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
INVITATION_FIELDS = {"version", "code", "thread", "name", "target", "port", "host_key", "expires"}
MAX_TOKEN_SIZE = 1024


def destination(target):
    if not isinstance(target, str):
        raise ValueError("Use the hub's SSH login and DNS name or IPv4 address as user@host.")
    user, _, host = target.partition("@")
    labels = host.removesuffix(".").split(".")
    if (not SSH_USER.fullmatch(user) or len(host.removesuffix(".")) > 253
            or not all(DNS_LABEL.fullmatch(label) for label in labels)):
        raise ValueError("Use the hub's SSH login and DNS name or IPv4 address as user@host.")
    if len(labels) == 4 and all(label.isdecimal() for label in labels):
        try:
            ipaddress.IPv4Address(".".join(labels))
        except ipaddress.AddressValueError as exc:
            raise ValueError("Use a valid IPv4 address for the hub.") from exc
    return user, host


def ssh_port(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Choose an SSH port from 1 to 65535.")
    return port


def normalized_host_key(value):
    if not isinstance(value, str):
        raise ValueError("Provide the hub's verified OpenSSH Ed25519 public host key.")
    algorithm, _, key = value.partition(" ")
    if algorithm != "ssh-ed25519" or len(key) != 68:
        raise ValueError("Provide the hub's verified OpenSSH Ed25519 public host key.")
    try:
        blob = base64.b64decode(key, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid Ed25519 public host key.") from exc
    if len(blob) != len(KEY_HEADER) + 32 or not blob.startswith(KEY_HEADER):
        raise ValueError("Invalid Ed25519 public host key.")
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def validate_invitation(invite):
    if not isinstance(invite, dict) or invite.keys() != INVITATION_FIELDS:
        raise ValueError("Use a complete Riftwire setup invitation.")
    if type(invite["version"]) is not int or invite["version"] != 1:
        raise ValueError("This Riftwire setup invitation version is unsupported.")
    for field in ("code", "thread"):
        value = invite[field]
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("The setup invitation must use canonical chat and setup UUIDs.")
    if not isinstance(invite["name"], str) or not DEVICE_NAME.fullmatch(invite["name"]):
        raise ValueError("Use a unique lowercase device name, such as laptop-linux.")
    destination(invite["target"])
    ssh_port(invite["port"])
    if normalized_host_key(invite["host_key"]) != invite["host_key"]:
        raise ValueError("The setup invitation has an invalid public host key.")
    if type(invite["expires"]) is not int or not 0 < invite["expires"] < 2**63:
        raise ValueError("The setup invitation has an invalid expiry time.")
    return invite


def encode_invitation(invite):
    raw = json.dumps(validate_invitation(invite), sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_invitation(token):
    if not isinstance(token, str) or len(token) > MAX_TOKEN_SIZE or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("Paste the complete Riftwire setup invitation.")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        invite = json.loads(raw)
        canonical = encode_invitation(invite)
    except (ValueError, binascii.Error, RecursionError) as exc:
        raise ValueError("The Riftwire setup invitation is invalid.") from exc
    if canonical != token:
        raise ValueError("The Riftwire setup invitation is invalid.")
    return invite


def require_hub(box):
    if box.side != box.hosts["hub"] or box.device_id is not None:
        raise ValueError("Manage guided pairing from the hub owner login.")


def invitation_row(db, code):
    return db.execute("""SELECT t.code,t.hub_thread AS thread,t.expires,t.finished,
                                p.name,p.target,p.port,p.host_key,p.device_id
                         FROM guided_pairings p JOIN chat_tests t USING (code)
                         WHERE t.code=?""", (code,)).fetchone()


def stored_invitation(row):
    return {"version": 1, **{field: row[field] for field in INVITATION_FIELDS - {"version"}}}


def issue(core, box, thread, name, target, port, host_key, code=None):
    """Create a targeted setup test, or reopen the same owner's invitation."""
    require_hub(box)
    thread = core.guid(thread)
    now = core.timestamp()
    if code is not None:
        with box.connection() as db:
            row = invitation_row(db, core.guid(code))
            if row is None or row["thread"] != thread:
                raise ValueError("This setup invitation was not found for this hub chat.")
            if row["expires"] <= now and row["finished"] is None:
                raise ValueError("This setup invitation expired. Issue a new invitation from the hub chat.")
            for field, supplied in (("name", name), ("target", target), ("port", port),
                                    ("host_key", core.ed25519_public_key(host_key) if host_key else None)):
                if supplied is not None and supplied != row[field]:
                    raise ValueError("The supplied route differs from this setup invitation.")
            return stored_invitation(row)

    if not isinstance(name, str) or not DEVICE_NAME.fullmatch(name) or name in box.hosts.values():
        raise ValueError("Choose a unique lowercase device name distinct from the hub and legacy peer.")
    destination(target)
    ssh_port(port)
    host_key = core.ed25519_public_key(host_key)
    with box.connection(write=True) as db:
        device = db.execute("SELECT id,revoked FROM devices WHERE name=?", (name,)).fetchone()
        if device and device["revoked"] is not None:
            raise ValueError("That device name is revoked. Choose a name for a new installation.")
        device_id = device["id"] if device else None
        existing = db.execute("""SELECT p.code FROM guided_pairings p JOIN chat_tests t USING (code)
                                 WHERE t.hub_thread=? AND p.name=? AND p.target=? AND p.port=?
                                   AND p.host_key=? AND p.device_id IS ? AND t.expires>?
                                   AND t.finished IS NULL
                                 ORDER BY t.created DESC LIMIT 1""",
                              (thread, name, target, port, host_key, device_id, now)).fetchone()
        if existing:
            return stored_invitation(invitation_row(db, existing["code"]))
        db.execute("DELETE FROM chat_tests WHERE expires < ?", (now - 86400,))
        code = str(uuid.uuid4())
        db.execute("INSERT INTO chat_tests(code,hub_thread,created,expires,peer_host) VALUES(?,?,?,?,?)",
                   (code, thread, now, now + core.TEST_LIFETIME, name))
        db.execute("""INSERT INTO guided_pairings(code,name,target,port,host_key,device_id)
                      VALUES(?,?,?,?,?,?)""", (code, name, target, port, host_key, device_id))
        return stored_invitation(invitation_row(db, code))


def enroll(core, box, code, request):
    """Consume the invitation through an authenticated hub owner command."""
    require_hub(box)
    code = core.guid(code)
    device_id, name, public_key = core.validate_pair_request(request)
    with box.connection(write=True) as db:
        row = invitation_row(db, code)
        if row is None or row["expires"] <= core.timestamp():
            raise ValueError("This setup invitation expired or was not found. Issue a new invitation from the hub chat.")
        if name != row["name"]:
            raise ValueError("The pairing request is for a different device name.")
        if row["device_id"] not in (None, device_id):
            raise ValueError("This setup invitation is bound to a different installation.")
        box._approve_device(db, device_id, name, public_key)
        # A failed key write rolls back enrollment. A key written before an
        # interrupted commit still needs the active device row at the gate.
        core.append_restricted_key(core.restricted_key(device_id, public_key), public_key)
        db.execute("UPDATE guided_pairings SET device_id=? WHERE code=?", (device_id, code))
    return {"code": code, "id": device_id, "name": name}
