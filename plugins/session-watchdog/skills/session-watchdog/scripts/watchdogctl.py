#!/usr/bin/env python3
"""Deterministic controller for the Session Watchdog hybrid plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


SERVICE = "codex-session-watchdog.service"
LEGACY_FEISHU_SERVICE = "codex-feishu-error-notifier.service"
DEFAULT_POLL_SECONDS = 300.0
DEFAULT_MESSAGE = "继续"


class ControlError(RuntimeError):
    pass


def home_dir() -> Path:
    return Path.home()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", home_dir() / ".codex")).expanduser()


def bundle_root() -> Path:
    return Path(__file__).resolve().parents[5]


def config_dir() -> Path:
    return bundle_root() / "config"


def config_path() -> Path:
    return config_dir() / "config.json"


def managed_dir() -> Path:
    return bundle_root() / "runtime"


def worker_path() -> Path:
    return managed_dir() / "codex_session_watchdog.py"


def unit_path() -> Path:
    return bundle_root() / "systemd" / SERVICE


def unit_link_path() -> Path:
    return home_dir() / ".config" / "systemd" / "user" / SERVICE


def notifier_path() -> Path:
    return bundle_root() / "integrations" / "notify_feishu.py"


def feishu_unit_path() -> Path:
    return bundle_root() / "systemd" / LEGACY_FEISHU_SERVICE


def feishu_unit_link_path() -> Path:
    return home_dir() / ".config" / "systemd" / "user" / LEGACY_FEISHU_SERVICE


def feishu_wants_link_path() -> Path:
    return (
        home_dir()
        / ".config"
        / "systemd"
        / "user"
        / "default.target.wants"
        / LEGACY_FEISHU_SERVICE
    )


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def notify_backup_path() -> Path:
    return config_dir() / "notify_backup.json"


def legacy_config_dir() -> Path:
    return codex_home() / "session-watchdog"


def legacy_managed_dir() -> Path:
    return home_dir() / ".local" / "share" / "codex-session-watchdog"


def legacy_script_path() -> Path:
    return home_dir() / "codex_session_watchdog.py"


def bundled_worker() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "codex_session_watchdog.py"


def run(command: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        details = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise ControlError(f"command failed ({result.returncode}): {' '.join(command)}{': ' + details if details else ''}")
    return result


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", "--user", *arguments], check=check)


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def canonical_session_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ControlError(f"invalid Codex session UUID: {value}") from exc


def load_config(create: bool = False) -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        if not create:
            return {"session_ids": []}
        atomic_write(
            path,
            json.dumps(
                {
                    "session_ids": [],
                    "poll_seconds": DEFAULT_POLL_SECONDS,
                    "message": DEFAULT_MESSAGE,
                    "feishu": {
                        "enabled": False,
                        "chat_id": "",
                        "bytedcli": "bytedcli",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            0o600,
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("session_ids"), list):
        raise ControlError(f'{path} must contain a "session_ids" array')
    poll_seconds = document.get("poll_seconds", DEFAULT_POLL_SECONDS)
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or poll_seconds <= 0
    ):
        raise ControlError("poll_seconds must be a number greater than 0")
    message = document.get("message", DEFAULT_MESSAGE)
    if not isinstance(message, str) or not message.strip():
        raise ControlError("message must be a non-empty string")
    return document


def save_config(document: Dict[str, Any]) -> None:
    atomic_write(
        config_path(),
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        0o600,
    )


def service_state() -> Dict[str, Any]:
    enabled = systemctl("is-enabled", SERVICE, check=False)
    active = systemctl("is-active", SERVICE, check=False)
    document = load_config(create=False)
    session_ids = document.get("session_ids", [])
    return {
        "installed": (
            unit_path().is_file()
            and unit_link_path().exists()
            and worker_path().is_file()
        ),
        "enabled": enabled.stdout.strip() == "enabled",
        "active": active.stdout.strip() == "active",
        "service": SERVICE,
        "worker": str(worker_path()),
        "unit": str(unit_path()),
        "config": str(config_path()),
        "session_ids": session_ids,
        "session_count": len(session_ids),
        "poll_seconds": document.get("poll_seconds", DEFAULT_POLL_SECONDS),
        "message": document.get("message", DEFAULT_MESSAGE),
        "feishu": feishu_state(document),
    }


def unit_content(codex_binary: str) -> str:
    worker = str(worker_path()).replace("\\", "\\\\").replace('"', '\\"')
    config = str(config_path()).replace("\\", "\\\\").replace('"', '\\"')
    state = str(config_dir() / "state.json").replace("\\", "\\\\").replace('"', '\\"')
    codex = codex_binary.replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description=Codex session watchdog
After=default.target

[Service]
Type=simple
ExecStart=\"{worker}\" --config \"{config}\" --state-file \"{state}\" --codex-binary \"{codex}\"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def desired_notify_command() -> List[str]:
    return [str(Path(sys.executable).resolve()), str(notifier_path())]


def is_legacy_feishu_notify(command: Optional[Sequence[str]]) -> bool:
    if not command:
        return False
    try:
        script = Path(command[-1]).expanduser().resolve()
        legacy = (codex_home() / "notify_feishu.py").resolve()
    except (OSError, RuntimeError):
        return False
    return script == legacy


def read_codex_config() -> tuple[str, Dict[str, Any]]:
    path = codex_config_path()
    if not path.exists():
        return "", {}
    try:
        text = path.read_text(encoding="utf-8")
        document = tomllib.loads(text) if text.strip() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ControlError(f"cannot read Codex config {path}: {exc}") from exc
    return text, document


def current_notify_command(document: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
    if document is None:
        _, document = read_codex_config()
    if "notify" not in document:
        return None
    value = document["notify"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ControlError("top-level Codex notify setting must be an array of strings")
    return list(value)


def write_notify_command(command: Optional[Sequence[str]]) -> None:
    text, document = read_codex_config()
    current_notify_command(document)
    lines = text.splitlines(keepends=True)
    root_end = len(lines)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            root_end = index
            break

    matches = [
        index
        for index, line in enumerate(lines[:root_end])
        if re.match(r"^\s*notify\s*=", line)
    ]
    if len(matches) > 1:
        raise ControlError("Codex config contains multiple top-level notify settings")
    if matches and "]" not in lines[matches[0]].split("=", 1)[1]:
        raise ControlError("multi-line Codex notify settings are not supported")

    replacement = None
    if command is not None:
        encoded = ", ".join(json.dumps(item, ensure_ascii=False) for item in command)
        replacement = f"notify = [{encoded}]\n"

    if matches:
        index = matches[0]
        if replacement is None:
            del lines[index]
        else:
            lines[index] = replacement
    elif replacement is not None:
        lines.insert(0, replacement)

    updated = "".join(lines)
    try:
        tomllib.loads(updated) if updated.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ControlError(f"refusing to write invalid Codex config: {exc}") from exc
    atomic_write(codex_config_path(), updated, 0o600)


def install_codex_notify() -> None:
    _, document = read_codex_config()
    previous = current_notify_command(document)
    backup = notify_backup_path()
    saved_backup: Optional[Dict[str, Any]] = None
    if backup.exists():
        try:
            candidate = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlError(f"cannot read notify backup {backup}: {exc}") from exc
        if not isinstance(candidate, dict):
            raise ControlError(f"invalid notify backup: {backup}")
        saved_backup = candidate

    if saved_backup is not None and is_legacy_feishu_notify(saved_backup.get("value")):
        atomic_write(
            backup,
            json.dumps({"present": False, "value": None}, indent=2) + "\n",
            0o600,
        )
    elif not backup.exists():
        migrate_legacy = is_legacy_feishu_notify(previous)
        atomic_write(
            backup,
            json.dumps(
                {
                    "present": "notify" in document and not migrate_legacy,
                    "value": None if migrate_legacy else previous,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            0o600,
        )
    write_notify_command(desired_notify_command())


def restore_codex_notify() -> bool:
    _, document = read_codex_config()
    if current_notify_command(document) != desired_notify_command():
        # The user changed notify after installation. Preserve that newer value
        # and discard our stale backup so a future enable captures it afresh.
        try:
            notify_backup_path().unlink()
        except FileNotFoundError:
            pass
        return False

    backup = notify_backup_path()
    if backup.exists():
        try:
            saved = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlError(f"cannot read notify backup {backup}: {exc}") from exc
        if saved.get("present") is True:
            value = saved.get("value")
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ControlError(f"invalid notify backup: {backup}")
            write_notify_command(value)
        else:
            write_notify_command(None)
        backup.unlink()
    else:
        write_notify_command(None)
    return True


def resolve_bytedcli(value: str) -> str:
    if "/" in value:
        path = Path(value).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ControlError(f"bytedcli executable was not found: {path}")
        return str(path.resolve())
    resolved = shutil.which(value)
    if resolved is None:
        raise ControlError(f"bytedcli executable was not found on PATH: {value}")
    return str(Path(resolved).resolve())


def feishu_state(document: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if document is None:
        document = load_config(create=False)
    raw = document.get("feishu")
    feishu = raw if isinstance(raw, dict) else {}
    enabled = feishu.get("enabled") is True
    chat_id = feishu.get("chat_id")
    bytedcli = feishu.get("bytedcli")
    try:
        notify_installed = current_notify_command() == desired_notify_command()
        notify_error = None
    except ControlError as exc:
        notify_installed = False
        notify_error = str(exc)
    result = {
        "enabled": enabled,
        "mode": "completion-only",
        "configured": (
            enabled
            and isinstance(chat_id, str)
            and bool(chat_id.strip())
            and isinstance(bytedcli, str)
            and bool(bytedcli.strip())
        ),
        "notify_installed": notify_installed,
    }
    if notify_error is not None:
        result["notify_error"] = notify_error
    return result


def install_feishu() -> Dict[str, Any]:
    cleanup_legacy_feishu_runtime()
    document = load_config(create=True)
    raw = document.get("feishu")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return feishu_state(document)
    chat_id = raw.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise ControlError("configure Feishu chat_id before installing notifications")
    configured_binary = raw.get("bytedcli", "bytedcli")
    if not isinstance(configured_binary, str) or not configured_binary.strip():
        raise ControlError("feishu.bytedcli must be a non-empty string")
    raw["bytedcli"] = resolve_bytedcli(configured_binary.strip())
    document["feishu"] = raw
    save_config(document)

    notifier = notifier_path()
    if not notifier.is_file():
        raise ControlError(f"bundled Feishu notifier is missing: {notifier}")
    os.chmod(notifier, 0o755)
    install_codex_notify()
    return feishu_state(document)


def uninstall_feishu() -> Dict[str, Any]:
    cleanup_legacy_feishu_runtime()
    restore_codex_notify()
    return feishu_state()


def cleanup_legacy_feishu_runtime() -> None:
    unit = feishu_unit_path()
    link = feishu_unit_link_path()
    wants = feishu_wants_link_path()
    managed = unit.exists() or wants.is_symlink()
    if link.is_symlink():
        try:
            managed = managed or link.resolve() == unit.resolve()
        except OSError:
            pass
    elif link.is_file():
        try:
            managed = "notify_feishu.py --watch" in link.read_text(encoding="utf-8")
        except OSError:
            managed = False

    if managed:
        systemctl("stop", LEGACY_FEISHU_SERVICE, check=False)
        systemctl("disable", LEGACY_FEISHU_SERVICE, check=False)
        for path in (wants, link, unit):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        systemctl("daemon-reload", check=False)
        systemctl("reset-failed", LEGACY_FEISHU_SERVICE, check=False)

    obsolete_state = (
        config_dir() / "notify_feishu_watch_state.json",
        config_dir() / "notify_feishu_deliveries.json",
        config_dir() / ".notify_feishu_deliveries.lock",
        codex_home() / "notify_feishu_watch_state.json",
    )
    for path in obsolete_state:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def migrate_legacy_files() -> None:
    old_config = legacy_config_dir()
    if old_config.is_dir() and old_config.resolve() != config_dir().resolve():
        config_dir().mkdir(parents=True, exist_ok=True)
        for source in old_config.iterdir():
            destination = config_dir() / source.name
            if not destination.exists():
                shutil.move(str(source), str(destination))
        try:
            old_config.rmdir()
        except OSError:
            pass


def install_link(source: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == source.resolve():
            return
        link.unlink()
    elif link.exists():
        link.unlink()
    link.symlink_to(source)


def install_unit_link() -> None:
    install_link(unit_path(), unit_link_path())


def cleanup_legacy_files() -> None:
    old_worker = legacy_managed_dir() / "codex_session_watchdog.py"
    try:
        old_worker.unlink()
    except FileNotFoundError:
        pass
    try:
        legacy_managed_dir().rmdir()
    except OSError:
        pass
    try:
        legacy_script_path().unlink()
    except FileNotFoundError:
        pass


def install() -> Dict[str, Any]:
    source = bundled_worker()
    if not source.is_file():
        raise ControlError(f"bundled worker is missing: {source}")
    codex_binary = shutil.which("codex")
    if codex_binary is None:
        raise ControlError("codex executable was not found on PATH")
    migrate_legacy_files()
    atomic_copy(source, worker_path(), 0o755)
    load_config(create=True)
    atomic_write(unit_path(), unit_content(codex_binary), 0o644)
    install_unit_link()
    systemctl("daemon-reload")
    systemctl("enable", "--now", SERVICE)
    # `enable --now` does not restart an already-active legacy deployment.
    # Restart explicitly so the process always uses the just-installed worker.
    systemctl("restart", SERVICE)
    state = service_state()
    if not state["active"]:
        raise ControlError(f"{SERVICE} did not become active")
    cleanup_legacy_files()
    document = load_config(create=True)
    feishu = document.get("feishu")
    if isinstance(feishu, dict) and feishu.get("enabled") is True:
        install_feishu()
    else:
        cleanup_legacy_feishu_runtime()
    return service_state()


def uninstall(purge: bool) -> Dict[str, Any]:
    uninstall_feishu()
    systemctl("disable", "--now", SERVICE, check=False)
    try:
        unit_link_path().unlink()
    except FileNotFoundError:
        pass
    systemctl("daemon-reload", check=False)
    systemctl("reset-failed", SERVICE, check=False)
    try:
        worker_path().unlink()
    except FileNotFoundError:
        pass
    try:
        managed_dir().rmdir()
    except OSError:
        pass
    if purge and config_dir().is_dir():
        shutil.rmtree(config_dir())
    try:
        unit_path().unlink()
    except FileNotFoundError:
        pass
    try:
        unit_path().parent.rmdir()
    except OSError:
        pass
    state = service_state()
    state["purged"] = purge
    return state


def update_sessions(values: Sequence[str], remove: bool) -> Dict[str, Any]:
    document = load_config(create=True)
    current = [canonical_session_id(str(item)) for item in document.get("session_ids", [])]
    requested = [canonical_session_id(value) for value in values]
    if remove:
        removing = set(requested)
        updated = [item for item in current if item not in removing]
    else:
        updated = list(dict.fromkeys([*current, *requested]))
    document["session_ids"] = updated
    save_config(document)
    return service_state()


def configure(poll_seconds: Optional[float], message: Optional[str]) -> Dict[str, Any]:
    if poll_seconds is None and message is None:
        raise ControlError("configure requires --poll-seconds and/or --message")
    document = load_config(create=True)
    if poll_seconds is not None:
        if poll_seconds <= 0:
            raise ControlError("poll_seconds must be greater than 0")
        document["poll_seconds"] = poll_seconds
    if message is not None:
        if not message.strip():
            raise ControlError("message must be non-empty")
        document["message"] = message
    save_config(document)
    return service_state()


def configure_feishu(chat_id: str, bytedcli: Optional[str]) -> Dict[str, Any]:
    if not chat_id.strip():
        raise ControlError("Feishu chat ID must be non-empty")
    document = load_config(create=True)
    previous = document.get("feishu")
    feishu = dict(previous) if isinstance(previous, dict) else {}
    binary = bytedcli or str(feishu.get("bytedcli", "bytedcli"))
    feishu.update(
        {
            "enabled": True,
            "chat_id": chat_id.strip(),
            "bytedcli": resolve_bytedcli(binary),
        }
    )
    document["feishu"] = feishu
    save_config(document)
    install_feishu()
    return service_state()


def disable_feishu() -> Dict[str, Any]:
    document = load_config(create=True)
    previous = document.get("feishu")
    feishu = dict(previous) if isinstance(previous, dict) else {}
    feishu["enabled"] = False
    document["feishu"] = feishu
    save_config(document)
    uninstall_feishu()
    return service_state()


def control_service(action: str) -> Dict[str, Any]:
    if not unit_path().is_file():
        raise ControlError("watchdog is not installed; run install first")
    systemctl(action, SERVICE)
    return service_state()


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install")
    subparsers.add_parser("status")
    subparsers.add_parser("list")
    for command in ("add", "remove"):
        child = subparsers.add_parser(command)
        child.add_argument("session_ids", nargs="+")
    for command in ("start", "stop", "restart"):
        subparsers.add_parser(command)
    configure_parser = subparsers.add_parser("configure")
    configure_parser.add_argument("--poll-seconds", type=float)
    configure_parser.add_argument("--message")
    feishu_parser = subparsers.add_parser("configure-feishu")
    feishu_parser.add_argument("--chat-id", required=True)
    feishu_parser.add_argument("--bytedcli")
    subparsers.add_parser("disable-feishu")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--purge", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install":
            result = install()
        elif args.command in {"status", "list"}:
            result = service_state()
            if args.command == "list":
                result = result["session_ids"]
        elif args.command in {"add", "remove"}:
            result = update_sessions(args.session_ids, remove=args.command == "remove")
        elif args.command == "configure":
            result = configure(args.poll_seconds, args.message)
        elif args.command == "configure-feishu":
            result = configure_feishu(args.chat_id, args.bytedcli)
        elif args.command == "disable-feishu":
            result = disable_feishu()
        elif args.command in {"start", "stop", "restart"}:
            result = control_service(args.command)
        elif args.command == "uninstall":
            result = uninstall(args.purge)
        else:
            raise ControlError(f"unsupported command: {args.command}")
    except ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
