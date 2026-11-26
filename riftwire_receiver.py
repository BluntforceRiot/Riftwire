"""Run the outbound peer receiver under the user's native login."""

from hashlib import sha256
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from xml.parsers.expat import ExpatError


MARKER = "Riftwire managed peer receiver v1"


class Unavailable(ValueError):
    """The login does not have a supported startup provider."""


def command(argv, *, env=None, check=True):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                            errors="replace", timeout=20, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and result.returncode:
        detail = " ".join(result.stderr.split())[-360:]
        raise ValueError(detail or f"{Path(argv[0]).name} exited with code {result.returncode}.")
    return result


def load_managed(path, owns):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"A different startup entry occupies {path}.")
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    if not owns(content):
        raise ValueError(f"A different startup entry occupies {path}.")
    return content


def systemd_quote(value):
    # Disable shell-style expansion in ExecStart with its colon prefix. Systemd
    # still expands percent specifiers and reads C escapes inside double quotes.
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    value = re.sub(r"[\x01-\x1f\x7f]", lambda match: f"\\x{ord(match[0]):02x}", value)
    return f'"{value}"'


def systemd_unit(argv, marker, build):
    executable, *arguments = argv
    line = " ".join(map(systemd_quote, [":" + executable, *arguments]))
    return (f"# {marker}\n# Build {build}\n[Unit]\nDescription=Riftwire peer receiver\n"
            f"[Service]\nType=exec\nExecStart={line}\nRestart=on-failure\nRestartSec=5s\n"
            "[Install]\nWantedBy=default.target\n").encode()


def systemd(argv, name, marker, build, enabled, write):
    config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config.is_absolute():
        config = Path.home() / ".config"
    unit = name + ".service"
    path = config / "systemd" / "user" / unit
    prior = load_managed(path, lambda content: content.startswith(f"# {marker}\n".encode()))
    if not enabled and prior is None:
        return "disabled"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise Unavailable("systemctl is unavailable. Codex MCP can receive messages while connected.")
    ctl = [systemctl, "--user", "--no-pager"]
    if command(ctl + ["show", "--property=Version", "--value"], check=False).returncode:
        raise Unavailable("The systemd user manager is unavailable for this login. Codex MCP can receive messages while connected.")

    def check_fragment():
        fragment = command(ctl + ["show", "--property=FragmentPath", "--value", unit], check=False).stdout.rstrip("\n")
        if fragment and Path(fragment).resolve() != path.resolve():
            raise ValueError(f"A different systemd unit already uses the name {unit}.")
        return bool(fragment)

    loaded = check_fragment()
    if not enabled:
        command(ctl + ["disable", "--now", unit])
        path.unlink()
        command(ctl + ["daemon-reload"])
        return "disabled"

    content = systemd_unit(argv, marker, build)
    changed = content != prior
    needs_reload = command(ctl + ["show", "--property=NeedDaemonReload", "--value", unit], check=False)
    stale = needs_reload.stdout.strip() == "yes"
    if loaded and (changed or stale):
        # Publishing new bytes after the old process stops lets a retry reliably
        # start those bytes even if this invocation is interrupted during reload.
        command(ctl + ["stop", unit])
    if changed:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write(path, content, 0o600)
    if changed or stale or not loaded:
        command(ctl + ["daemon-reload"])
        if not check_fragment():
            raise ValueError("The systemd user manager did not load the installed Riftwire unit.")
    command(ctl + ["enable", unit])
    command(ctl + ["start", unit])
    active = command(ctl + ["is-active", "--quiet", unit], check=False).returncode == 0
    return "running" if active else "configured"


def launchd(argv, name, marker, build, enabled, write):
    label = "org.riftwire." + name
    path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    domain = f"gui/{os.getuid()}"
    job = f"{domain}/{label}"
    settings = {"Label": label, "ProgramArguments": argv, "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 5,
                "EnvironmentVariables": {"RIFTWIRE_RECEIVER_OWNER": marker, "RIFTWIRE_RECEIVER_BUILD": build}}

    def owns(content):
        try:
            return plistlib.loads(content)["EnvironmentVariables"]["RIFTWIRE_RECEIVER_OWNER"] == marker
        except (ValueError, KeyError, TypeError, ExpatError):
            return False

    prior = load_managed(path, owns)
    if not enabled and prior is None:
        return "disabled"
    launchctl = "/bin/launchctl"
    if not Path(launchctl).is_file():
        raise Unavailable("launchctl is unavailable for this login.")
    active_login = command([launchctl, "print", domain], check=False).returncode == 0
    current = command([launchctl, "print", job], check=False) if active_login else None
    loaded = current is not None and current.returncode == 0
    if loaded and not any(line.strip() == f"path = {path}" for line in current.stdout.splitlines()):
        raise ValueError(f"A different launchd job already uses the name {label}.")
    if not enabled:
        if loaded:
            command([launchctl, "bootout", job])
        path.unlink()
        return "disabled"

    content = plistlib.dumps(settings)
    changed = prior != content
    if changed:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Stop the old job before publishing the new launch file. If interrupted,
        # the next invocation can bootstrap the current file.
        if loaded:
            command([launchctl, "bootout", job])
            loaded = False
        write(path, content, 0o600)
    if not active_login:
        return "configured"
    if not loaded:
        command([launchctl, "bootstrap", domain, str(path)])
    current = command([launchctl, "print", job], check=False)
    return "running" if re.search(r"^\s*state = running\s*$", current.stdout, re.M) else "configured"


# Pass only fixed source to PowerShell. Paths and arguments travel as values in
# the process environment and are set on the native task action directly.
WINDOWS_TASK = r"""
$ErrorActionPreference = 'Stop'
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$root = $scheduler.GetFolder('\')
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$name = $env:RIFTWIRE_TASK_NAME
$owner = $env:RIFTWIRE_TASK_OWNER + ' ' + $sid + ' '
$description = $owner + $env:RIFTWIRE_TASK_BUILD
$existing = $root.GetTasks(1) | Where-Object { $_.Name -eq $name }
if ($existing -and !$existing.Definition.RegistrationInfo.Description.StartsWith($owner)) {
    throw 'A different Windows task already uses the Riftwire receiver name.'
}
if ($env:RIFTWIRE_TASK_ENABLE -eq '0') {
    if ($existing) {
        if ($existing.State -eq 4) { $existing.Stop(0) }
        $root.DeleteTask($name, 0)
    }
    'disabled'
    exit 0
}
$same = $false
if ($existing -and $existing.Enabled) {
    $definition = $existing.Definition
    $same = (
        ($definition.RegistrationInfo.Description -ceq $description) -and
        ($definition.Principal.LogonType -eq 3) -and ($definition.Principal.RunLevel -eq 0) -and
        ($definition.Actions.Count -eq 1) -and
        ($definition.Actions.Item(1).Path -ieq $env:RIFTWIRE_TASK_PROGRAM) -and
        ($definition.Actions.Item(1).Arguments -ceq $env:RIFTWIRE_TASK_ARGUMENTS)
    )
}
if ($same) {
    $registered = $existing
} else {
    $task = $scheduler.NewTask(0)
    $task.RegistrationInfo.Description = $description
    $task.Principal.UserId = $sid
    $task.Principal.LogonType = 3
    $task.Principal.RunLevel = 0
    $task.Settings.ExecutionTimeLimit = 'PT0S'
    $task.Settings.DisallowStartIfOnBatteries = $false
    $task.Settings.StopIfGoingOnBatteries = $false
    $task.Settings.MultipleInstances = 2
    $task.Settings.RestartInterval = 'PT1M'
    $task.Settings.RestartCount = 3
    $trigger = $task.Triggers.Create(9)
    $trigger.UserId = $sid
    $action = $task.Actions.Create(0)
    $action.Path = $env:RIFTWIRE_TASK_PROGRAM
    $action.Arguments = $env:RIFTWIRE_TASK_ARGUMENTS
    $flags = 2
    if ($existing) {
        if ($existing.State -eq 4) { $existing.Stop(0) }
        $flags = 4
    }
    $registered = $root.RegisterTaskDefinition($name, $task, $flags, $sid, $null, 3, $null)
}
if ($registered.State -eq 3) {
    try { $null = $registered.Run($null) } catch {
        $failure = $_.Exception.GetBaseException()
        # Interactive-token tasks will start at the next logon when no desktop session exists.
        if ($failure.HResult -ne -2147216608) { throw }
    }
}
if ($registered.State -eq 4) { 'running' } else { 'configured' }
"""


def windows_task(argv, name, marker, build, enabled, write):
    executable, *args = argv
    gui = Path(executable).with_name("pythonw.exe")
    if gui.is_file():
        executable = str(gui)
    if enabled and any(re.search(r"%[^%]+%|\$\(Arg\d+\)", part, re.I) for part in (executable, *args)):
        raise ValueError("The task scheduler expands variables in launch paths. Choose a path without %NAME% or $(ArgN).")
    root = Path(os.environ.get("SystemRoot", ""))
    powershell = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not root.is_absolute() or not powershell.is_file():
        raise Unavailable("Windows PowerShell for Task Scheduler is unavailable for this login.")
    env = {**os.environ, "RIFTWIRE_TASK_NAME": name, "RIFTWIRE_TASK_OWNER": marker,
           "RIFTWIRE_TASK_BUILD": build, "RIFTWIRE_TASK_ENABLE": "1" if enabled else "0",
           "RIFTWIRE_TASK_PROGRAM": executable, "RIFTWIRE_TASK_ARGUMENTS": subprocess.list2cmdline(args)}
    result = command([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_TASK], env=env)
    result = result.stdout.strip()
    if result not in ("running", "configured", "disabled"):
        raise ValueError("Task Scheduler returned an unexpected startup status.")
    return result


def ensure(folder, enabled=True, *, write, lock):
    """Set up one background receiver and report its local startup status."""
    provider, update = {"linux": ("systemd", systemd), "darwin": ("launchd", launchd),
                        "win32": ("windows-task", windows_task)}.get(sys.platform, ("codex-mcp", None))
    report = {"provider": provider}
    if update is None:
        return dict(report, state="unsupported", detail="This platform uses the Codex MCP receiver.")
    try:
        folder = Path(folder).resolve(strict=True)
        script = folder / "riftwire.py"
        settings = folder / "settings.json"
        if not script.is_file() or not settings.is_file() or not Path(sys.executable).is_absolute():
            raise ValueError("Install the local peer before configuring its receiver.")
        argv = [sys.executable, "-I", "-B", str(script), "listen-peer", "--folder", str(folder)]
        identifier = sha256(os.fsencode(os.path.normcase(folder))).hexdigest()[:24]
        name = "riftwire-peer-" + identifier
        build = sha256(b"\0".join([script.read_bytes(), settings.read_bytes(), *map(os.fsencode, argv)])).hexdigest()
        with lock(folder, "receiver-setup.lock") as acquired:
            if not acquired:
                raise ValueError("Another receiver setup is in progress. Retry shortly.")
            state = update(argv, name, MARKER + " " + identifier, build, enabled, write)
    except Unavailable as exc:
        return dict(report, state="unsupported", detail=str(exc))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return dict(report, state="error", detail=str(exc))
    details = {"running": "Background receiver is active for this login.",
               "configured": "Background receiver is configured for the next login.",
               "disabled": "Background receiver is disabled. The Codex MCP receiver remains available."}
    return dict(report, state=state, detail=details[state])
