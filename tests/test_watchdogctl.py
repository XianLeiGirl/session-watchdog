import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "plugins"
    / "session-watchdog"
    / "skills"
    / "session-watchdog"
    / "scripts"
    / "watchdogctl.py"
)
SPEC = importlib.util.spec_from_file_location("session_watchdogctl", MODULE_PATH)
assert SPEC and SPEC.loader
watchdogctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdogctl)

WORKER_PATH = (
    ROOT
    / "plugins"
    / "session-watchdog"
    / "skills"
    / "session-watchdog"
    / "assets"
    / "codex_session_watchdog.py"
)
WORKER_SPEC = importlib.util.spec_from_file_location("session_watchdog_worker", WORKER_PATH)
assert WORKER_SPEC and WORKER_SPEC.loader
worker = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = worker
WORKER_SPEC.loader.exec_module(worker)

PLUGINCTL_PATH = ROOT / "scripts" / "pluginctl.py"
PLUGINCTL_SPEC = importlib.util.spec_from_file_location("session_watchdog_pluginctl", PLUGINCTL_PATH)
assert PLUGINCTL_SPEC and PLUGINCTL_SPEC.loader
pluginctl = importlib.util.module_from_spec(PLUGINCTL_SPEC)
PLUGINCTL_SPEC.loader.exec_module(pluginctl)


SESSION_A = "019a1812-6920-7000-8000-000000000001"
SESSION_B = "019a1812-6920-7000-8000-000000000002"


class WatchdogControllerTests(unittest.TestCase):
    def test_unified_command_delegates_session_add(self):
        output = io.StringIO()
        with (
            mock.patch.object(pluginctl, "manager", return_value={"session_count": 1}) as manager,
            redirect_stdout(output),
        ):
            result = pluginctl.main(["add", SESSION_A])
        self.assertEqual(result, 0)
        manager.assert_called_once_with("add", SESSION_A)
        self.assertEqual(json.loads(output.getvalue())["session_count"], 1)

    def test_unified_command_delegates_configuration(self):
        output = io.StringIO()
        with (
            mock.patch.object(pluginctl, "manager", return_value={"poll_seconds": 30}) as manager,
            redirect_stdout(output),
        ):
            result = pluginctl.main(
                ["configure", "--poll-seconds", "30", "--message", "继续完成"]
            )
        self.assertEqual(result, 0)
        manager.assert_called_once_with(
            "configure", "--poll-seconds", "30.0", "--message", "继续完成"
        )

    def test_runtime_settings_are_hot_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "session_ids": [SESSION_A],
                        "poll_seconds": 17,
                        "message": "请继续处理",
                    }
                ),
                encoding="utf-8",
            )
            args = worker.build_parser().parse_args(
                [
                    "--config",
                    str(config),
                    "--state-file",
                    str(root / "state.json"),
                ]
            )
            instance = worker.Watchdog(args)
            self.assertTrue(instance._reload_config(force=True))
            self.assertEqual(instance.threads, [SESSION_A])
            self.assertEqual(args.poll_seconds, 17.0)
            self.assertEqual(args.message, "请继续处理")

    def test_install_restarts_an_already_active_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            worker = root / "share" / "worker.py"
            config = root / "config" / "config.json"
            unit = root / "systemd" / "watchdog.service"
            unit_link = root / "systemd-user" / "watchdog.service"
            calls = []

            def fake_systemctl(*arguments, **_kwargs):
                calls.append(arguments)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(watchdogctl, "bundled_worker", return_value=source),
                mock.patch.object(watchdogctl, "worker_path", return_value=worker),
                mock.patch.object(watchdogctl, "config_path", return_value=config),
                mock.patch.object(watchdogctl, "unit_path", return_value=unit),
                mock.patch.object(watchdogctl, "unit_link_path", return_value=unit_link),
                mock.patch.object(watchdogctl, "migrate_legacy_files"),
                mock.patch.object(watchdogctl, "cleanup_legacy_files"),
                mock.patch.object(watchdogctl, "systemctl", side_effect=fake_systemctl),
                mock.patch.object(watchdogctl.shutil, "which", return_value="/bin/codex"),
                mock.patch.object(
                    watchdogctl,
                    "service_state",
                    return_value={"active": True, "installed": True},
                ),
            ):
                state = watchdogctl.install()

            self.assertTrue(state["active"])
            self.assertIn(("enable", "--now", watchdogctl.SERVICE), calls)
            self.assertIn(("restart", watchdogctl.SERVICE), calls)

    def test_add_and_remove_sessions_preserve_other_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"session_ids": [SESSION_A], "custom": "kept"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(watchdogctl, "config_path", return_value=path),
                mock.patch.object(
                    watchdogctl,
                    "service_state",
                    side_effect=lambda: json.loads(path.read_text(encoding="utf-8")),
                ),
            ):
                watchdogctl.update_sessions([SESSION_B], remove=False)
                added = json.loads(path.read_text(encoding="utf-8"))
                watchdogctl.update_sessions([SESSION_A], remove=True)
                removed = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(added["session_ids"], [SESSION_A, SESSION_B])
            self.assertEqual(removed["session_ids"], [SESSION_B])
            self.assertEqual(removed["custom"], "kept")

    def test_configure_updates_interval_and_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"session_ids": [SESSION_A]}), encoding="utf-8")
            with (
                mock.patch.object(watchdogctl, "config_path", return_value=path),
                mock.patch.object(
                    watchdogctl,
                    "service_state",
                    side_effect=lambda: json.loads(path.read_text(encoding="utf-8")),
                ),
            ):
                watchdogctl.configure(45, "继续完成任务")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["poll_seconds"], 45)
            self.assertEqual(document["message"], "继续完成任务")


if __name__ == "__main__":
    unittest.main()
