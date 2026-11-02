import importlib.util
import io
import json
import sys
import tempfile
import tomllib
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

NOTIFIER_PATH = ROOT / "integrations" / "notify_feishu.py"
NOTIFIER_SPEC = importlib.util.spec_from_file_location(
    "session_watchdog_notify_feishu", NOTIFIER_PATH
)
assert NOTIFIER_SPEC and NOTIFIER_SPEC.loader
notifier = importlib.util.module_from_spec(NOTIFIER_SPEC)
sys.modules[NOTIFIER_SPEC.name] = notifier
NOTIFIER_SPEC.loader.exec_module(notifier)

PLUGINCTL_PATH = ROOT / "scripts" / "pluginctl.py"
PLUGINCTL_SPEC = importlib.util.spec_from_file_location("session_watchdog_pluginctl", PLUGINCTL_PATH)
assert PLUGINCTL_SPEC and PLUGINCTL_SPEC.loader
pluginctl = importlib.util.module_from_spec(PLUGINCTL_SPEC)
PLUGINCTL_SPEC.loader.exec_module(pluginctl)


SESSION_A = "019a1812-6920-7000-8000-000000000001"
SESSION_B = "019a1812-6920-7000-8000-000000000002"


class WatchdogControllerTests(unittest.TestCase):
    def test_completion_deduplication_is_per_turn(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": SESSION_A,
            "turn-id": "turn-a",
            "thread-title": "测试任务",
        }
        with mock.patch.object(notifier, "send_feishu", return_value=0) as send:
            self.assertEqual(notifier.notify_completion(json.dumps(payload)), 0)
            self.assertEqual(notifier.notify_completion(json.dumps(payload)), 0)
            payload["turn-id"] = "turn-b"
            self.assertEqual(notifier.notify_completion(json.dumps(payload)), 0)

        event_keys = [call.args[1] for call in send.call_args_list]
        self.assertEqual(event_keys, [f"{SESSION_A}|turn-a", f"{SESSION_A}|turn-a", f"{SESSION_A}|turn-b"])
        self.assertEqual(
            notifier.idempotency_key(event_keys[0]),
            notifier.idempotency_key(event_keys[1]),
        )
        self.assertNotEqual(
            notifier.idempotency_key(event_keys[0]),
            notifier.idempotency_key(event_keys[2]),
        )

    def test_abnormal_turn_does_not_send_feishu_notification(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": SESSION_A,
            "turn-id": "turn-failed",
            "error": {"message": "rate limit"},
        }
        with mock.patch.object(notifier, "send_feishu", return_value=0) as send:
            self.assertEqual(notifier.notify_completion(json.dumps(payload)), 0)
        send.assert_not_called()

    def test_send_uses_server_idempotency_without_local_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "enabled": True,
                            "chat_id": "oc_test",
                            "bytedcli": "/bin/true",
                        }
                    }
                ),
                encoding="utf-8",
            )
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True}),
                stderr="",
            )
            with (
                mock.patch.object(notifier, "CONFIG_PATH", config),
                mock.patch.object(notifier.subprocess, "run", return_value=completed) as run,
            ):
                self.assertEqual(notifier.send_feishu("done", "thread|turn"), 0)
                self.assertEqual(notifier.send_feishu("done", "thread|turn"), 0)

            self.assertEqual(run.call_count, 2)
            commands = [call.args[0] for call in run.call_args_list]
            keys = [command[command.index("--idempotency-key") + 1] for command in commands]
            self.assertEqual(keys[0], keys[1])
            self.assertEqual([path.name for path in root.iterdir()], ["config.json"])

    def test_codex_notify_setting_is_backed_up_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_config = root / "config.toml"
            backup = root / "notify_backup.json"
            managed_notifier = root / "notify_feishu.py"
            codex_config.write_text(
                'notify = ["/usr/bin/python3", "/legacy/notify.py"]\nmodel = "test"\n',
                encoding="utf-8",
            )
            with (
                mock.patch.object(watchdogctl, "codex_config_path", return_value=codex_config),
                mock.patch.object(watchdogctl, "notify_backup_path", return_value=backup),
                mock.patch.object(watchdogctl, "notifier_path", return_value=managed_notifier),
            ):
                watchdogctl.install_codex_notify()
                installed = tomllib.loads(codex_config.read_text(encoding="utf-8"))
                self.assertEqual(installed["notify"][-1], str(managed_notifier))
                self.assertTrue(watchdogctl.restore_codex_notify())

            restored = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            self.assertEqual(restored["notify"], ["/usr/bin/python3", "/legacy/notify.py"])
            self.assertFalse(backup.exists())

    def test_legacy_feishu_notify_is_migrated_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            codex_home.mkdir()
            legacy = codex_home / "notify_feishu.py"
            managed = root / "session-watchdog" / "integrations" / "notify_feishu.py"
            codex_config = codex_home / "config.toml"
            backup = root / "notify_backup.json"
            codex_config.write_text(
                f'notify = ["/usr/bin/python3", "{legacy}"]\n',
                encoding="utf-8",
            )
            with (
                mock.patch.object(watchdogctl, "codex_home", return_value=codex_home),
                mock.patch.object(watchdogctl, "codex_config_path", return_value=codex_config),
                mock.patch.object(watchdogctl, "notify_backup_path", return_value=backup),
                mock.patch.object(watchdogctl, "notifier_path", return_value=managed),
            ):
                watchdogctl.install_codex_notify()
                saved = json.loads(backup.read_text(encoding="utf-8"))
                self.assertFalse(saved["present"])
                self.assertTrue(watchdogctl.restore_codex_notify())

            restored = tomllib.loads(codex_config.read_text(encoding="utf-8"))
            self.assertNotIn("notify", restored)

    def test_cleanup_removes_old_error_watcher_and_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / "bundle" / "error-watcher.service"
            link = root / "systemd-user" / "error-watcher.service"
            wants = root / "systemd-user" / "default.target.wants" / "error-watcher.service"
            config_dir = root / "config"
            codex_home = root / ".codex"
            unit.parent.mkdir(parents=True)
            link.parent.mkdir(parents=True)
            wants.parent.mkdir(parents=True)
            config_dir.mkdir()
            codex_home.mkdir()
            unit.write_text("managed unit\n", encoding="utf-8")
            link.write_text("ExecStart=python notify_feishu.py --watch\n", encoding="utf-8")
            wants.symlink_to(link)
            obsolete = [
                config_dir / "notify_feishu_watch_state.json",
                config_dir / "notify_feishu_deliveries.json",
                config_dir / ".notify_feishu_deliveries.lock",
                codex_home / "notify_feishu_watch_state.json",
            ]
            for path in obsolete:
                path.write_text("{}\n", encoding="utf-8")
            calls = []

            def fake_systemctl(*arguments, **_kwargs):
                calls.append(arguments)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(watchdogctl, "feishu_unit_path", return_value=unit),
                mock.patch.object(watchdogctl, "feishu_unit_link_path", return_value=link),
                mock.patch.object(watchdogctl, "feishu_wants_link_path", return_value=wants),
                mock.patch.object(watchdogctl, "config_dir", return_value=config_dir),
                mock.patch.object(watchdogctl, "codex_home", return_value=codex_home),
                mock.patch.object(watchdogctl, "systemctl", side_effect=fake_systemctl),
            ):
                watchdogctl.cleanup_legacy_feishu_runtime()

            self.assertFalse(unit.exists())
            self.assertFalse(link.exists())
            self.assertFalse(wants.is_symlink())
            self.assertTrue(all(not path.exists() for path in obsolete))
            self.assertIn(
                ("stop", watchdogctl.LEGACY_FEISHU_SERVICE),
                calls,
            )

    def test_install_bytedcli_uses_requested_package_registry_and_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "bytedcli"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)

            def fake_which(command):
                return "/usr/bin/npm" if command == "npm" else str(binary)

            with (
                mock.patch.object(watchdogctl.shutil, "which", side_effect=fake_which),
                mock.patch.object(
                    watchdogctl,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ) as run,
            ):
                installed = watchdogctl.install_bytedcli()

        self.assertEqual(installed, str(binary.resolve()))
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "/usr/bin/npm",
                        "install",
                        "-g",
                        "@bytedance-dev/bytedcli@latest",
                        "--registry",
                        "https://bnpm.byted.org",
                    ]
                ),
                mock.call(
                    [
                        str(binary.resolve()),
                        "self",
                        "skill",
                        "install",
                        "--skill",
                        "bytedcli",
                        "-g",
                    ]
                ),
            ],
        )

    def test_lark_app_setup_returns_generated_creation_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_popen(command, **kwargs):
                kwargs["stdout"].write(
                    'verification_url: https://open.feishu.cn/setup?code=one-time\n'
                )
                kwargs["stdout"].flush()
                process = mock.Mock(pid=12345)
                process.poll.return_value = None
                return process

            with (
                mock.patch.object(watchdogctl, "config_dir", return_value=root),
                mock.patch.object(
                    watchdogctl.subprocess, "Popen", side_effect=fake_popen
                ) as popen,
            ):
                result = watchdogctl.start_lark_app_setup("/opt/bin/bytedcli")

            self.assertEqual(
                result["agent_creation_url"],
                "https://open.feishu.cn/setup?code=one-time",
            )
            self.assertEqual(result["setup_pid"], 12345)
            self.assertEqual(
                popen.call_args.args[0],
                [
                    "/opt/bin/bytedcli",
                    "--no-auto-upgrade",
                    "lark",
                    "config",
                    "--new",
                ],
            )

    def test_configure_feishu_bootstraps_missing_bytedcli_without_partial_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            original = {"session_ids": [SESSION_A]}
            config.write_text(json.dumps(original), encoding="utf-8")
            with (
                mock.patch.object(watchdogctl, "config_path", return_value=config),
                mock.patch.object(
                    watchdogctl,
                    "resolve_bytedcli",
                    side_effect=watchdogctl.ControlError("missing"),
                ),
                mock.patch.object(
                    watchdogctl,
                    "install_bytedcli",
                    return_value="/opt/bin/bytedcli",
                ) as install,
                mock.patch.object(
                    watchdogctl,
                    "start_lark_app_setup",
                    return_value={
                        "agent_creation_url": "https://open.feishu.cn/setup?code=test",
                        "setup_log": "/tmp/setup.log",
                        "setup_pid": 42,
                    },
                ) as setup,
            ):
                result = watchdogctl.configure_feishu("oc_test", None)

            self.assertTrue(result["bytedcli_installed"])
            self.assertTrue(result["setup_required"])
            self.assertFalse(result["feishu_configured"])
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8")),
                original,
            )
            install.assert_called_once_with()
            setup.assert_called_once_with("/opt/bin/bytedcli")

    def test_configure_feishu_does_not_replace_missing_explicit_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"session_ids": []}), encoding="utf-8")
            with (
                mock.patch.object(watchdogctl, "config_path", return_value=config),
                mock.patch.object(
                    watchdogctl,
                    "resolve_bytedcli",
                    side_effect=watchdogctl.ControlError("missing explicit binary"),
                ),
                mock.patch.object(watchdogctl, "install_bytedcli") as install,
            ):
                with self.assertRaisesRegex(
                    watchdogctl.ControlError, "missing explicit binary"
                ):
                    watchdogctl.configure_feishu(
                        "oc_test", "/custom/bin/bytedcli"
                    )
            install.assert_not_called()

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
                [
                    "configure",
                    "--poll-seconds",
                    "30",
                    "--message",
                    "继续完成",
                    "--max-send-attempts",
                    "4",
                    "--max-resumes-per-hour",
                    "3",
                ]
            )
        self.assertEqual(result, 0)
        manager.assert_called_once_with(
            "configure",
            "--poll-seconds",
            "30.0",
            "--message",
            "继续完成",
            "--max-send-attempts",
            "4",
            "--max-resumes-per-hour",
            "3",
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
                        "max_send_attempts": 4,
                        "max_resumes_per_hour": 3,
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
            self.assertEqual(args.max_send_attempts, 4)
            self.assertEqual(args.max_resumes_per_hour, 3)

    def test_runtime_limits_require_nonnegative_integers(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "session_ids": [SESSION_A],
                        "max_send_attempts": -1,
                        "max_resumes_per_hour": 1,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "max_send_attempts"):
                worker.SessionConfig(config).load(force=True)

    def test_state_pruning_preserves_current_and_retrying_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = worker.StateStore(path)
            now = 2_000_000_000.0
            old = now - worker.STATE_EVENT_RETENTION_SECONDS - 1
            store.data = {
                "version": 1,
                "events": {
                    "expired": {"handled": True, "handled_at": old},
                    "current": {"handled": True, "handled_at": old},
                    "retrying": {
                        "handled": False,
                        "attempts": 3,
                        "next_attempt_at": now + 60,
                    },
                    "recent": {"handled": True, "handled_at": now - 60},
                    "legacy": {"handled": True},
                },
                "actions": [
                    {"signature": "expired", "thread_id": SESSION_A, "at": old},
                    {"signature": "recent", "thread_id": SESSION_A, "at": now - 60},
                ],
            }

            self.assertTrue(store.prune({"current"}, now=now))
            self.assertNotIn("expired", store.data["events"])
            self.assertIn("current", store.data["events"])
            self.assertIn("retrying", store.data["events"])
            self.assertIn("recent", store.data["events"])
            self.assertIn("legacy", store.data["events"])
            self.assertEqual(
                [item["signature"] for item in store.data["actions"]],
                ["recent"],
            )

            store.mark_failure("exhausted", now, max_attempts=1)
            self.assertEqual(store.data["events"]["exhausted"]["handled_at"], now)

    def test_state_pruning_caps_recent_handled_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = worker.StateStore(Path(directory) / "state.json")
            now = 2_000_000_000.0
            total = worker.MAX_HANDLED_STATE_EVENTS + 5
            store.data["events"] = {
                f"event-{index}": {"handled": True, "handled_at": now - index}
                for index in range(total)
            }
            self.assertTrue(store.prune(now=now))
            self.assertEqual(
                len(store.data["events"]),
                worker.MAX_HANDLED_STATE_EVENTS,
            )
            self.assertIn("event-0", store.data["events"])
            self.assertNotIn(f"event-{total - 1}", store.data["events"])

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
                watchdogctl.configure(45, "继续完成任务", 4, 3)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["poll_seconds"], 45)
            self.assertEqual(document["message"], "继续完成任务")
            self.assertEqual(document["max_send_attempts"], 4)
            self.assertEqual(document["max_resumes_per_hour"], 3)


if __name__ == "__main__":
    unittest.main()
