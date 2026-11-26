import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import riftwire as rw
import riftwire_receiver as receiver


def result(command, output="", code=0):
    return subprocess.CompletedProcess(command, code, output, "")


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="receiver-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.folder = self.root / "Riftwire 100%"
        self.folder.mkdir()
        (self.folder / "riftwire.py").write_text("# fixture\n", encoding="utf-8")
        (self.folder / "settings.json").write_text("{}\n", encoding="utf-8")

    def ensure(self, enabled=True):
        return receiver.ensure(self.folder, enabled, write=rw.safe_write, lock=rw.receiver_lock)

    def test_systemd_serializes_paths_without_environment_expansion(self):
        argv = ['/opt/Python $HOME %i/py"thon', '-I', '-B', '/home/user/with spaces/$HOME %u/relay.py',
                'listen-peer', '--folder', '/home/user/with spaces/$HOME %u']
        unit = receiver.systemd_unit(argv, receiver.MARKER, "test").decode()
        line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertEqual(line, 'ExecStart=":/opt/Python $HOME %%i/py\\"thon" "-I" "-B" '
                               '"/home/user/with spaces/$HOME %%u/relay.py" "listen-peer" '
                               '"--folder" "/home/user/with spaces/$HOME %%u"')

    def test_systemd_setup_resume_upgrade_and_opt_out(self):
        config = self.root / "config"
        units = config / "systemd" / "user"
        commands = []
        loaded = False

        def systemctl(argv, **kwargs):
            nonlocal loaded
            args = argv[3:]
            commands.append(args[0])
            if args[0] == "daemon-reload":
                loaded = any(units.glob("*.service"))
            if "--property=FragmentPath" in args and loaded:
                return result(argv, str(units / args[-1]) + "\n")
            return result(argv)

        with (patch.object(sys, "platform", "linux"),
              patch.dict(os.environ, XDG_CONFIG_HOME=str(config)),
              patch.object(receiver.shutil, "which", return_value="systemctl"),
              patch.object(receiver, "command", side_effect=systemctl)):
            self.assertEqual(self.ensure()["state"], "running")
            unit, = units.glob("*.service")
            original = unit.read_bytes()
            self.assertIn(b'"listen-peer" "--folder"', original)
            self.assertIn("daemon-reload", commands)
            self.assertNotIn("stop", commands)

            commands.clear()
            self.assertEqual(self.ensure()["state"], "running")
            self.assertEqual(unit.read_bytes(), original)
            self.assertNotIn("daemon-reload", commands)
            self.assertNotIn("stop", commands)

            commands.clear()
            (self.folder / "settings.json").write_text('{"updated":true}', encoding="utf-8")
            self.assertEqual(self.ensure()["state"], "running")
            self.assertLess(commands.index("stop"), commands.index("start"))
            self.assertNotEqual(unit.read_bytes(), original)

            commands.clear()
            self.assertEqual(self.ensure(False)["state"], "disabled")
            self.assertIn("disable", commands)
            self.assertFalse(unit.exists())
            self.assertTrue((self.folder / "settings.json").is_file())

            commands.clear()
            self.assertEqual(self.ensure(False)["state"], "disabled")
            self.assertEqual(commands, [])

    def test_systemd_without_user_manager_leaves_no_startup_file(self):
        config = self.root / "config"
        with (patch.object(sys, "platform", "linux"),
              patch.dict(os.environ, XDG_CONFIG_HOME=str(config)),
              patch.object(receiver.shutil, "which", return_value="systemctl"),
              patch.object(receiver, "command", return_value=result([], code=1))):
            status = self.ensure()
        self.assertEqual(status["state"], "unsupported")
        self.assertIn("user manager", status["detail"])
        self.assertFalse(config.exists())

    def test_malformed_or_unrelated_startup_file_is_preserved(self):
        file = self.root / "foreign.plist"
        before = plistlib.dumps({"ProgramArguments": ["example"]})
        file.write_bytes(before)
        with self.assertRaisesRegex(ValueError, "different startup entry"):
            receiver.load_managed(file, lambda content: False)
        self.assertEqual(file.read_bytes(), before)

    def test_launchd_can_configure_or_remove_startup_outside_the_desktop_session(self):
        with (patch.object(sys, "platform", "darwin"),
              patch.object(receiver.os, "getuid", return_value=123, create=True),
              patch.object(Path, "home", return_value=self.root),
              patch.object(Path, "is_file", return_value=True),
              patch.object(receiver, "command", return_value=result([], code=1)) as invoke):
            self.assertEqual(self.ensure()["state"], "configured")
            entry, = (self.root / "Library" / "LaunchAgents").glob("*.plist")
            settings = plistlib.loads(entry.read_bytes())
            self.assertEqual(settings["ProgramArguments"][-3:], ["listen-peer", "--folder", str(self.folder)])
            self.assertIs(settings["RunAtLoad"], True)
            invoke.assert_called_once_with(["/bin/launchctl", "print", "gui/123"], check=False)
            self.assertEqual(self.ensure(False)["state"], "disabled")
            self.assertFalse(entry.exists())

    def test_windows_paths_are_passed_as_values_and_env_expansion_is_refused(self):
        windows = self.root / "Windows test"
        power = windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        power.parent.mkdir(parents=True)
        power.touch()
        argv = [str(self.root / "Python %" / "python.exe"), "-I", "-B", str(self.folder / "riftwire.py"),
                "listen-peer", "--folder", str(self.folder)]
        with (patch.dict(os.environ, SystemRoot=str(windows)),
              patch.object(receiver, "command", return_value=result([], "configured\n")) as invoke):
            self.assertEqual(receiver.windows_task(argv, "name", "owner", "build", True, rw.safe_write), "configured")
            args, kwargs = invoke.call_args
            self.assertEqual(args[0][0], str(power))
            self.assertEqual(args[0][-1], receiver.WINDOWS_TASK)
            self.assertNotIn(str(self.folder), args[0][-1])
            self.assertEqual(kwargs["env"]["RIFTWIRE_TASK_PROGRAM"], argv[0])
            self.assertEqual(kwargs["env"]["RIFTWIRE_TASK_ARGUMENTS"], subprocess.list2cmdline(argv[1:]))
            invoke.reset_mock()
            argv[0] = str(self.root / "%TEMP%" / "python.exe")
            with self.assertRaisesRegex(ValueError, "scheduler expands variables"):
                receiver.windows_task(argv, "name", "owner", "build", True, rw.safe_write)
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
