"""One-invitation setup and verification over an existing SSH listener."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace


def hub_mailbox(core, settings):
    return core.Mailbox(core.DATA / "mail.sqlite3", "hub", settings["hosts"],
                        settings["codex"], settings.get("codex_home"))


def enroll(core, payload):
    # Only the SSH account owner can run this command. It is absent from ssh-gate.
    if not isinstance(payload, dict) or set(payload) != {"code", "request"}:
        raise ValueError("Provide a guided pairing code and its public-key request.")
    pairing = core.load_module(core.PAIRING_MODULE)
    box = hub_mailbox(core, core.read_settings(core.DATA))
    core.output(pairing.enroll(core, box, payload["code"], payload["request"]))


def setup_hub(core, pairing, args, tid):
    if os.name == "nt":
        raise ValueError("Run the hub setup on the Unix login that accepts SSH.")
    if args.resume:
        if any(value is not None for value in (args.device, args.ssh, args.port, args.host_key)):
            raise ValueError("Resume with the setup code alone. The invitation already has its SSH route.")
        name = target = port = host_key = None
    else:
        if not args.device or not core.ALIAS.fullmatch(args.device) or not args.ssh:
            raise ValueError("Supply --device NAME and the hub owner's reachable --ssh user@host.")
        name, target = args.device, args.ssh
        pairing.destination(target)
        port = pairing.ssh_port(22 if args.port is None else args.port)
        key_path = Path(args.host_key or "/etc/ssh/ssh_host_ed25519_key.pub").expanduser()
        try:
            host_key = core.ed25519_public_key(key_path.read_text(encoding="ascii"))
        except FileNotFoundError as exc:
            raise ValueError("Supply the trusted SSH server's Ed25519 public key with --host-key FILE.") from exc

    folder = core.DATA
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with core.receiver_lock(folder, "guided-setup.lock") as locked:
        if not locked:
            raise ValueError("Another hub setup is preparing an invitation. Retry shortly.")
        previous = core.read_settings(folder) if (folder / "settings.json").exists() else {}
        if args.resume and not previous:
            raise ValueError("Resume on the hub login that issued the invitation.")
        mailbox = folder / "mail.sqlite3"
        installed = folder / "riftwire.py"
        if mailbox.is_file() and installed.is_file() and installed.read_bytes() != Path(core.__file__).read_bytes():
            backup = Path(tempfile.mkdtemp(prefix="pre-guided-upgrade-", dir=folder))
            snapshot = core.copy_mailbox(mailbox, backup / "mail.sqlite3")
            core.output({"state": "mailbox_backed_up", **snapshot})
        core.install(SimpleNamespace(mode="install-hub", name=None, peer=None,
                                     codex=args.codex or previous.get("codex"), allow_alternate=True))
        settings = core.read_settings(folder)
        invite = pairing.issue(core, hub_mailbox(core, settings), tid, name, target, port, host_key, code=args.resume)
    core.output({"state": "invited", "code": invite["code"], "invitation": pairing.encode_invitation(invite),
                 "device": invite["name"], "hub_thread": tid, "expires": invite["expires"]})
    core.run_chat_check("hub", settings, tid, code=invite["code"], wait=args.wait)


def owner_enroll(core, settings, invite, request, args):
    owner = {key: value for key, value in settings.items() if key != "identity"}
    if args.owner_identity:
        identity = Path(args.owner_identity).expanduser().resolve()
        if not identity.is_file():
            raise ValueError("The supplied hub-owner SSH identity file is unavailable.")
        owner["identity"] = str(identity)
    command = f"python3 -I -B {core.REMOTE_SCRIPT} setup-enroll"
    ssh = core.ssh_connection_args(owner, interactive=not args.batch)
    payload = core.encode({"code": invite["code"], "request": request}).encode("ascii")
    try:
        result = subprocess.run(ssh + [command], input=payload, capture_output=True, check=False, timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Hub-owner SSH authentication timed out. Resume from the peer's native terminal.") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-400:]
        raise ValueError(f"Hub-owner enrollment failed ({result.returncode}). {detail}")
    expected = {"code": invite["code"], "id": request["id"], "name": request["name"]}
    if json.loads(result.stdout) != expected:
        raise ValueError("The hub-owner session returned a different pairing approval.")


def pinned_route(core, pairing, folder, invite, *, replace_draft=False):
    _, host = pairing.destination(invite["target"])
    port = invite["port"]
    marker = host if port == 22 else f"[{host}]:{port}"
    pin = folder / "setup_known_hosts"
    data = f"{marker} {invite['host_key']}\n".encode("ascii")
    prior = pin.read_bytes() if pin.exists() else None
    if prior is not None and prior != data and not replace_draft:
        raise ValueError("The invitation differs from the saved SSH host pin.")
    if prior != data:
        core.safe_write(pin, data, 0o600)
    return {"target": invite["target"], "port": port, "known_hosts": str(pin.resolve())}


def prepare_peer(core, pairing, folder, args, tid):
    progress = folder / "guided-setup.json"
    saved = json.loads(progress.read_text(encoding="utf-8")) if progress.exists() else {}
    token = args.invite or saved.get("invitation")
    if not token:
        raise ValueError("Use --invite TOKEN with the invitation from the hub setup chat.")
    invite = pairing.decode_invitation(token)
    previous = core.read_settings(folder) if (folder / "settings.json").exists() else {}
    authenticated = bool(previous) or saved.get("authenticated", False)
    if saved:
        last = pairing.decode_invitation(saved["invitation"])
        if last["name"] != invite["name"] or (authenticated and
                any(last[key] != invite[key] for key in ("target", "port", "host_key"))):
            raise ValueError("A different guided SSH route is already saved for this login.")
        if last["code"] == invite["code"] and tid != saved["peer_thread"]:
            raise ValueError("Resume this invitation from the original peer chat.")
    if tid == invite["thread"]:
        raise ValueError("Use a distinct peer chat for the two-chat check.")

    settings = core.native_settings(args.codex or previous.get("codex"))
    settings["ssh_program"] = core.openssh_path()
    background = args.background if args.background is not None else saved.get("background", True)
    pending = None
    if previous:
        if previous["hosts"]["peer"] != invite["name"] or not previous.get("device_id"):
            raise ValueError("This login already has a different peer installation. Keep its saved identity and route.")
        # An installed peer authenticates through its existing route, including any saved SSH alias and pin.
        settings.update({key: previous[key] for key in ("target", "port", "identity", "known_hosts", "mcp_name")
                         if key in previous})
    else:
        settings.update(pinned_route(core, pairing, folder, invite,
                                     replace_draft=bool(args.invite and not authenticated)))
        pending = core.pair_request(folder, invite["name"])
        settings["identity"] = str((folder / "pairing_ed25519").resolve())

    def save_progress(bound):
        state = {"invitation": token, "peer_thread": tid, "background": background, "authenticated": bound}
        core.safe_write(progress, (core.encode(state) + "\n").encode("ascii"), 0o600)

    save_progress(authenticated)
    try:
        checked = core.peer_call(settings, "check")
    except (OSError, ValueError):
        if pending is None:
            raise
        if invite["expires"] <= core.timestamp():
            raise ValueError("The invitation expired before enrollment. Request a fresh invitation from the same hub.") from None
        owner_enroll(core, settings, invite, pending, args)
        save_progress(True)
        checked = core.peer_call(settings, "check")
    if not isinstance(checked, dict) or checked.get("release") != core.VERSION:
        raise ValueError(f"Install Riftwire {core.VERSION} on the hub before finishing guided setup.")
    core.configure_peer(folder, settings, previous, pending, checked, guided=True)
    return invite, settings, background


def run(core, args):
    core.check_wait(args.wait)
    tid = core.verify_thread(args.thread)
    pairing = core.load_module(core.PAIRING_MODULE)
    if args.role == "hub":
        setup_hub(core, pairing, args, tid)
        return
    receiver = core.load_module(core.RECEIVER_MODULE)
    folder = core.peer_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with core.receiver_lock(folder, "guided-setup.lock") as locked:
        if not locked:
            raise ValueError("Another peer setup is running for this login. Retry shortly.")
        invite, settings, background = prepare_peer(core, pairing, folder, args, tid)
        pickup = receiver.ensure(folder, enabled=background, write=core.safe_write, lock=core.receiver_lock)
    core.output({"state": "peer_ready", "code": invite["code"], "peer_thread": tid,
                 "device": settings["hosts"]["peer"], "mcp": settings.get("mcp_name", "riftwire"),
                 "receiver": pickup})
    core.run_chat_check("peer", settings, tid, code=invite["code"], wait=args.wait,
                        pull=pickup["state"] != "running")


def set_receiver(core, enabled):
    folder = core.peer_dir()
    core.read_settings(folder)
    receiver = core.load_module(core.RECEIVER_MODULE)
    with core.receiver_lock(folder, "guided-setup.lock") as locked:
        if not locked:
            raise ValueError("Another peer setup is running for this login. Retry shortly.")
        progress = folder / "guided-setup.json"
        if progress.exists():
            state = json.loads(progress.read_text(encoding="ascii"))
            state["background"] = enabled
            core.safe_write(progress, (core.encode(state) + "\n").encode("ascii"), 0o600)
        report = receiver.ensure(folder, enabled=enabled, write=core.safe_write, lock=core.receiver_lock)
    core.output(report)
    if report["state"] in ("error", "unsupported"):
        raise SystemExit(1)
