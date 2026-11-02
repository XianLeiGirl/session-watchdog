#!/usr/bin/env python3
"""Deterministic controller for the Session Watchdog hybrid plugin."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


SERVICE = "codex-session-watchdog.service"
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


def install_unit_link() -> None:
    link = unit_link_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == unit_path().resolve():
            return
        link.unlink()
    elif link.exists():
        link.unlink()
    link.symlink_to(unit_path())


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
    return state


def uninstall(purge: bool) -> Dict[str, Any]:
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
