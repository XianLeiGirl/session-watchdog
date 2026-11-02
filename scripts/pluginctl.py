#!/usr/bin/env python3
"""One-command plug-in/plug-out wrapper for the local Session Watchdog plugin."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence


MARKETPLACE = "session-watchdog-local"
PLUGIN = "session-watchdog"
SELECTOR = f"{PLUGIN}@{MARKETPLACE}"
ROOT = Path(__file__).resolve().parent.parent
MANAGER = (
    ROOT
    / "plugins"
    / PLUGIN
    / "skills"
    / PLUGIN
    / "scripts"
    / "watchdogctl.py"
)


class PluginError(RuntimeError):
    pass


def codex_binary() -> str:
    binary = shutil.which("codex")
    if binary is None:
        raise PluginError("codex executable was not found on PATH")
    return binary


def run(command: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        details = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise PluginError(f"command failed ({result.returncode}): {' '.join(command)}{': ' + details if details else ''}")
    return result


def codex_json(*arguments: str) -> Any:
    result = run([codex_binary(), *arguments])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PluginError(f"Codex returned invalid JSON: {result.stdout}") from exc


def marketplace_installed() -> bool:
    document = codex_json("plugin", "marketplace", "list", "--json")
    return any(item.get("name") == MARKETPLACE for item in document.get("marketplaces", []))


def installed_plugin() -> Optional[dict[str, Any]]:
    document = codex_json("plugin", "list", "--json")
    return next(
        (item for item in document.get("installed", []) if item.get("name") == PLUGIN),
        None,
    )


def plugin_installed() -> bool:
    return installed_plugin() is not None


def source_version() -> str:
    manifest = ROOT / "plugins" / PLUGIN / ".codex-plugin" / "plugin.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError(f"cannot read plugin manifest {manifest}: {exc}") from exc
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise PluginError(f"plugin manifest has no valid version: {manifest}")
    return version


def manager(*arguments: str) -> Any:
    result = run([sys.executable, str(MANAGER), *arguments])
    return json.loads(result.stdout)


def plug_in() -> Any:
    if not marketplace_installed():
        codex_json("plugin", "marketplace", "add", str(ROOT), "--json")
    installed = installed_plugin()
    if installed is not None and installed.get("version") != source_version():
        codex_json("plugin", "remove", SELECTOR, "--json")
        installed = None
    if installed is None:
        codex_json("plugin", "add", SELECTOR, "--json")
    service = manager("install")
    return {"plugin_installed": True, "marketplace": MARKETPLACE, "service": service}


def plug_out(purge: bool) -> Any:
    service = manager("uninstall", *(["--purge"] if purge else []))
    if plugin_installed():
        codex_json("plugin", "remove", SELECTOR, "--json")
    return {"plugin_installed": False, "marketplace": MARKETPLACE, "service": service}


def status() -> Any:
    return {
        "marketplace_registered": marketplace_installed(),
        "plugin_installed": plugin_installed(),
        "service": manager("status"),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install, remove, or inspect the local Session Watchdog plugin stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Actions:
  plug-in   Register the local marketplace, install/enable the Codex plugin,
            deploy the worker and systemd unit, then enable and start the service.
            Existing session configuration and state are preserved.

  plug-out  Stop and disable the systemd service, remove its runtime worker and
            unit, then uninstall the Codex plugin. Source files, this command,
            marketplace registration, configuration, and state are preserved.
            Add --purge to also delete configuration and state.

  status    Show marketplace registration, plugin installation, service state,
            configured interval/message, monitored sessions, and Feishu state.

  list      List monitored session IDs.
  add       Add one or more monitored session IDs.
  remove    Stop monitoring one or more session IDs.
  configure Hot-reload the monitoring interval and/or continuation message.
  configure-feishu
            Save the Feishu chat ID and bytedcli path, install the Codex notify
            hook, and start the fatal-error notification service.
  disable-feishu
            Stop Feishu notifications and restore the previous Codex notify hook.
  notification-status
            Check whether a specific thread/turn completion or error notification
            has a successful local delivery record.
  start/stop/restart
            Control the installed systemd service.
""",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="actions",
        metavar=(
            "{plug-in,plug-out,status,list,add,remove,configure,configure-feishu,"
            "disable-feishu,notification-status,start,stop,restart}"
        ),
    )
    subparsers.add_parser(
        "plug-in",
        help="install/update the plugin stack and start its service",
        description=(
            "Register the marketplace, install the Codex plugin, deploy the worker "
            "and systemd unit, then enable and start the service. Existing "
            "configuration and state are preserved."
        ),
    )
    plug_out_parser = subparsers.add_parser(
        "plug-out",
        help="stop the service and uninstall the plugin stack",
        description=(
            "Stop and disable the service, remove its runtime worker and unit, "
            "then uninstall the Codex plugin. Configuration and state are "
            "preserved unless --purge is supplied."
        ),
    )
    plug_out_parser.add_argument(
        "--purge",
        action="store_true",
        help="also permanently delete saved configuration and deduplication state",
    )
    subparsers.add_parser(
        "status",
        help="show marketplace, plugin, service, and monitored-session status",
        description=(
            "Show marketplace registration, plugin installation, service state, "
            "runtime settings, and monitored session IDs."
        ),
    )
    subparsers.add_parser(
        "list",
        help="list monitored session IDs",
        description="List the session IDs currently present in the hot-reloaded configuration.",
    )
    for command, verb in (("add", "add"), ("remove", "remove")):
        child = subparsers.add_parser(
            command,
            help=f"{verb} one or more monitored session IDs",
            description=f"{verb.capitalize()} one or more Codex session UUIDs in the hot-reloaded configuration.",
        )
        child.add_argument("session_ids", nargs="+", metavar="SESSION_ID")
    configure_parser = subparsers.add_parser(
        "configure",
        help="set the monitoring interval and/or continuation message",
        description=(
            "Update poll_seconds and/or message in the hot-reloaded configuration. "
            "The service does not need to restart."
        ),
    )
    configure_parser.add_argument(
        "--poll-seconds",
        type=float,
        help="positive number of seconds between session checks",
    )
    configure_parser.add_argument(
        "--message",
        help="non-empty text queued after an abnormal turn failure",
    )
    feishu_parser = subparsers.add_parser(
        "configure-feishu",
        help="configure and enable Codex completion/error notifications in Feishu",
        description=(
            "Save the Feishu chat ID and bytedcli executable, install the Codex "
            "notify hook, and start the fatal-error notification service."
        ),
    )
    feishu_parser.add_argument(
        "--chat-id",
        required=True,
        help="Feishu group chat ID used by bytedcli",
    )
    feishu_parser.add_argument(
        "--bytedcli",
        help="bytedcli executable name or absolute path (default: resolve bytedcli on PATH)",
    )
    subparsers.add_parser(
        "disable-feishu",
        help="disable Feishu notifications and restore the previous Codex notify hook",
        description=(
            "Stop the Feishu error watcher, remove its managed Codex notify hook, "
            "and restore the notify setting saved before Feishu was enabled."
        ),
    )
    notification_parser = subparsers.add_parser(
        "notification-status",
        help="check the Feishu delivery record for one Codex thread and turn",
        description=(
            "Report whether the local delivery ledger contains a successful "
            "completion notification for THREAD_ID and TURN_ID. Add --error to "
            "query the fatal-error notification instead."
        ),
    )
    notification_parser.add_argument("thread_id", metavar="THREAD_ID")
    notification_parser.add_argument("turn_id", metavar="TURN_ID")
    notification_parser.add_argument(
        "--error",
        action="store_true",
        help="query the fatal-error notification instead of completion",
    )
    for command in ("start", "stop", "restart"):
        subparsers.add_parser(
            command,
            help=f"{command} the installed watchdog service",
            description=f"{command.capitalize()} the installed systemd user service.",
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "plug-in":
            result = plug_in()
        elif args.command == "plug-out":
            result = plug_out(args.purge)
        elif args.command == "status":
            result = status()
        elif args.command in {"list", "start", "stop", "restart"}:
            result = manager(args.command)
        elif args.command in {"add", "remove"}:
            result = manager(args.command, *args.session_ids)
        elif args.command == "configure":
            arguments = ["configure"]
            if args.poll_seconds is not None:
                arguments.extend(["--poll-seconds", str(args.poll_seconds)])
            if args.message is not None:
                arguments.extend(["--message", args.message])
            result = manager(*arguments)
        elif args.command == "configure-feishu":
            arguments = ["configure-feishu", "--chat-id", args.chat_id]
            if args.bytedcli is not None:
                arguments.extend(["--bytedcli", args.bytedcli])
            result = manager(*arguments)
        elif args.command == "disable-feishu":
            result = manager("disable-feishu")
        elif args.command == "notification-status":
            arguments = ["notification-status", args.thread_id, args.turn_id]
            if args.error:
                arguments.append("--error")
            result = manager(*arguments)
        else:
            raise PluginError(f"unsupported action: {args.command}")
    except (PluginError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
