#!/usr/bin/env python3
"""Watch selected Codex sessions and continue failed turns.

The watchdog reads Codex rollout JSONL files and deliberately treats
``turn_aborted`` as a manual interruption.  A transient ``task_complete``
error (for example HTTP 429) is recovered with ``codex queue --resume``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


VERSION = "0.1.0"
LOG = logging.getLogger("codex-session-watchdog")
DEFAULT_POLL_SECONDS = 300.0
DEFAULT_MESSAGE = "继续"

TRANSIENT_ERROR_CODES = {
    "flexunavailable",
    "internalservererror",
    "policycapacityunavailable",
    "ratelimitexceeded",
    "responsestreamconnectionfailed",
    "response-stream-connection-failed",
    "response-stream-disconnected",
    "response-too-many-failed-attempts",
    "serveroverloaded",
}
TRANSIENT_OBJECT_KEYS = {
    "httpconnectionfailed",
    "responsestreamconnectionfailed",
    "responsestreamdisconnected",
    "responsetoomanyfailedattempts",
}
PERMANENT_ERROR_CODES = {
    "badrequest",
    "contextwindowexceeded",
    "cyberpolicy",
    "misalignmentpolicyviolation",
    "modelnotfound",
    "sandboxerror",
    "sessionbudgetexceeded",
    "threadrollbackfailed",
    "toomanydenials",
    "unauthorized",
    "usagelimitexceeded",
}
TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_MESSAGE_RE = re.compile(
    r"(?:\b429\b|too many requests|rate[ _-]?limit|server overloaded|"
    r"temporar(?:y|ily) unavailable|timed? out|connection (?:failed|reset)|"
    r"stream (?:disconnect|connection fail)|exceeded retry limit)",
    re.IGNORECASE,
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def canonical_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def nested_http_status(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        for key, child in value.items():
            if canonical_token(key) == "httpstatuscode":
                try:
                    return int(child)
                except (TypeError, ValueError):
                    pass
            status = nested_http_status(child)
            if status is not None:
                return status
    elif isinstance(value, list):
        for child in value:
            status = nested_http_status(child)
            if status is not None:
                return status
    return None


def error_info(error: Any) -> Any:
    if not isinstance(error, dict):
        return None
    return error.get("codex_error_info", error.get("codexErrorInfo"))


def is_transient_error(error: Any) -> bool:
    """Return True only for errors that are normally safe to retry."""

    if not error:
        return False

    info = error_info(error)
    status = nested_http_status(info if info is not None else error)
    if status in TRANSIENT_HTTP_STATUSES or (status is not None and status >= 500):
        return True

    if isinstance(info, str):
        normalized_info = canonical_token(info)
        if normalized_info in PERMANENT_ERROR_CODES:
            return False
        if normalized_info in {canonical_token(code) for code in TRANSIENT_ERROR_CODES}:
            return True
    if isinstance(info, dict):
        if any(canonical_token(key) in TRANSIENT_OBJECT_KEYS for key in info):
            return True

    message = error.get("message", "") if isinstance(error, dict) else str(error)
    return bool(TRANSIENT_MESSAGE_RE.search(message))


def describe_error(error: Any) -> str:
    if not error:
        return "unknown error"
    if isinstance(error, dict):
        message = str(error.get("message") or "unknown error")
        info = error_info(error)
        status = nested_http_status(info if info is not None else error)
        suffix = f" (HTTP {status})" if status is not None and str(status) not in message else ""
        return message + suffix
    return str(error)


@dataclass
class SessionSnapshot:
    thread_id: str
    state: str = "unknown"
    turn_id: Optional[str] = None
    event_time: Optional[dt.datetime] = None
    last_activity: Optional[dt.datetime] = None
    error: Any = None
    abort_reason: Optional[str] = None
    cwd: Optional[str] = None
    source: Optional[str] = None

    @property
    def signature(self) -> str:
        event_part = self.event_time.isoformat() if self.event_time else "no-time"
        return f"{self.thread_id}:{self.turn_id or 'no-turn'}:{self.state}:{event_part}"


@dataclass
class RolloutTracker:
    thread_id: str
    path: Path
    snapshot: SessionSnapshot = field(init=False)
    offset: int = 0
    initialized: bool = False
    malformed_lines: int = 0

    def __post_init__(self) -> None:
        self.snapshot = SessionSnapshot(thread_id=self.thread_id)

    def poll(self) -> bool:
        """Read appended complete JSONL records and return whether state changed."""

        before = self.snapshot.signature
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return False
        if size < self.offset:
            self.offset = 0
            self.snapshot = SessionSnapshot(thread_id=self.thread_id)

        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line_start = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    self.offset = line_start
                    break
                self.offset = stream.tell()
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.malformed_lines += 1
                    continue
                self._apply(record)

        self.initialized = True
        return before != self.snapshot.signature

    def _apply(self, record: Dict[str, Any]) -> None:
        record_time = parse_time(record.get("timestamp"))
        if record_time is not None:
            self.snapshot.last_activity = record_time

        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record_type == "session_meta":
            actual_id = payload.get("session_id") or payload.get("id")
            if isinstance(actual_id, str):
                self.snapshot.thread_id = actual_id
            self.snapshot.cwd = payload.get("cwd")
            self.snapshot.source = payload.get("source")
            return
        if record_type != "event_msg":
            return

        event_type = payload.get("type")
        if event_type == "task_started":
            self.snapshot.state = "running"
            self.snapshot.turn_id = payload.get("turn_id")
            self.snapshot.event_time = record_time
            self.snapshot.error = None
            self.snapshot.abort_reason = None
        elif event_type == "task_complete":
            self.snapshot.state = "failed" if payload.get("error") else "completed"
            self.snapshot.turn_id = payload.get("turn_id") or self.snapshot.turn_id
            self.snapshot.event_time = record_time
            self.snapshot.error = payload.get("error")
            self.snapshot.abort_reason = None
        elif event_type == "turn_aborted":
            # Codex emits this for user/manual interruption.  It must never be
            # conflated with task_complete.error.
            self.snapshot.state = "interrupted"
            self.snapshot.turn_id = payload.get("turn_id") or self.snapshot.turn_id
            self.snapshot.event_time = record_time
            self.snapshot.error = None
            self.snapshot.abort_reason = str(payload.get("reason") or "interrupted")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, Any] = {"version": 1, "events": {}, "actions": []}
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring unreadable state file %s: %s", self.path, exc)
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("events", {})
            self.data.setdefault("actions", [])

    def event(self, signature: str) -> Dict[str, Any]:
        return self.data["events"].setdefault(
            signature, {"attempts": 0, "handled": False, "next_attempt_at": 0.0}
        )

    def recent_action_count(self, thread_id: str, now: float, window: float = 3600.0) -> int:
        actions = self.data.get("actions", [])
        return sum(
            1
            for item in actions
            if item.get("thread_id") == thread_id
            and isinstance(item.get("at"), (int, float))
            and item["at"] >= now - window
        )

    def mark_action(self, thread_id: str, signature: str, now: float) -> None:
        event = self.event(signature)
        event.update({"handled": True, "handled_at": now, "result": "queued"})
        actions = self.data.setdefault("actions", [])
        actions.append({"thread_id": thread_id, "signature": signature, "at": now})
        self.data["actions"] = [
            item for item in actions if item.get("at", 0) >= now - 7 * 86400
        ][-1000:]

    def mark_ignored(self, signature: str, reason: str, now: float) -> None:
        self.event(signature).update(
            {"handled": True, "handled_at": now, "result": "ignored", "reason": reason}
        )

    def mark_failure(self, signature: str, now: float, max_attempts: int) -> None:
        event = self.event(signature)
        attempts = int(event.get("attempts", 0)) + 1
        backoff = min(300.0, 5.0 * (2 ** max(0, attempts - 1)))
        exhausted = max_attempts > 0 and attempts >= max_attempts
        event.update(
            {
                "attempts": attempts,
                "next_attempt_at": now + backoff,
                "handled": exhausted,
                "result": "abandoned" if exhausted else "send_failed",
            }
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


class SessionConfig:
    """Hot-reload and validate the authoritative runtime configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._signature: Optional[Tuple[Any, ...]] = None
        self._initialized = False

    def load(self, force: bool = False) -> Optional[Dict[str, Any]]:
        try:
            stat = self.path.stat()
            signature: Tuple[Any, ...] = ("file", stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = ("missing",)

        if not force and self._initialized and signature == self._signature:
            return None
        self._signature = signature
        self._initialized = True

        if signature[0] == "missing":
            raise ValueError(f"configuration file does not exist: {self.path}")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read configuration {self.path}: {exc}") from exc

        if isinstance(document, list):
            raw_ids = document
            document = {"session_ids": document}
        elif isinstance(document, dict):
            raw_ids = document.get("session_ids")
        else:
            raw_ids = None
        if not isinstance(raw_ids, list):
            raise ValueError('configuration must contain a "session_ids" array')

        result: List[str] = []
        seen = set()
        for index, raw_id in enumerate(raw_ids):
            if not isinstance(raw_id, str):
                raise ValueError(f"session_ids[{index}] must be a string")
            try:
                thread_id = validate_thread_id(raw_id)
            except argparse.ArgumentTypeError as exc:
                raise ValueError(f"session_ids[{index}]: {exc}") from exc
            if thread_id not in seen:
                seen.add(thread_id)
                result.append(thread_id)
        loaded: Dict[str, Any] = {"session_ids": result}
        if "poll_seconds" in document:
            poll_seconds = document["poll_seconds"]
            if (
                isinstance(poll_seconds, bool)
                or not isinstance(poll_seconds, (int, float))
                or poll_seconds <= 0
            ):
                raise ValueError("poll_seconds must be a number greater than 0")
            loaded["poll_seconds"] = float(poll_seconds)
        if "message" in document:
            message = document["message"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message must be a non-empty string")
            loaded["message"] = message
        return loaded


def validate_thread_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a Codex session UUID: {value}") from exc


def discover_rollout(codex_home: Path, thread_id: str) -> Optional[Path]:
    root = codex_home / "sessions"
    if not root.is_dir():
        return None
    matches = list(root.glob(f"**/rollout-*{thread_id}.jsonl"))
    if not matches:
        # Older or migrated files do not always carry the UUID in their name.
        for candidate in root.glob("**/rollout-*.jsonl"):
            try:
                with candidate.open("rb") as stream:
                    first = json.loads(stream.readline())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            payload = first.get("payload", {})
            if payload.get("session_id") == thread_id or payload.get("id") == thread_id:
                matches.append(candidate)
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime_ns)


def status_line(snapshot: SessionSnapshot) -> str:
    suffix = ""
    if snapshot.state == "failed":
        suffix = f": {describe_error(snapshot.error)}"
    elif snapshot.state == "interrupted":
        suffix = f": {snapshot.abort_reason}"
    return f"{snapshot.thread_id} [{snapshot.state}] turn={snapshot.turn_id or '-'}{suffix}"


class Watchdog:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.default_poll_seconds = args.poll_seconds
        self.default_message = args.message
        self.threads: List[str] = list(args.threads)
        self.trackers: Dict[str, RolloutTracker] = {}
        self.last_logged: Dict[str, str] = {}
        self.stop_requested = False
        self.state = StateStore(args.state_file)
        self.config = SessionConfig(args.config) if args.config is not None else None

    def stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        LOG.info("Codex session watchdog started; press Ctrl-C to stop")

        next_config_reload = 0.0
        next_session_check = 0.0
        first_iteration = True

        while not self.stop_requested:
            now = time.monotonic()
            if self.config is not None and now >= next_config_reload:
                added = self._reload_config(force=first_iteration)
                next_config_reload = now + self.args.config_reload_seconds
                if added or first_iteration:
                    next_session_check = 0.0

            checked = False
            if now >= next_session_check:
                for thread_id in list(self.threads):
                    self._poll_thread(thread_id)
                next_session_check = now + self.args.poll_seconds
                checked = True

            if self.args.once and checked:
                break
            first_iteration = False
            deadlines = [next_session_check]
            if self.config is not None:
                deadlines.append(next_config_reload)
            sleep_for = max(0.05, min(deadlines) - time.monotonic())
            self._sleep(min(0.5, sleep_for))
        return 0

    def _reload_config(self, force: bool = False) -> bool:
        assert self.config is not None
        try:
            loaded = self.config.load(force=force)
        except ValueError as exc:
            LOG.error("%s; keeping the last valid session list", exc)
            return False
        if loaded is None:
            return False

        loaded_threads = loaded["session_ids"]
        loaded_poll_seconds = loaded.get("poll_seconds", self.default_poll_seconds)
        loaded_message = loaded.get("message", self.default_message)
        poll_changed = loaded_poll_seconds != self.args.poll_seconds
        message_changed = loaded_message != self.args.message

        previous = set(self.threads)
        current = set(loaded_threads)
        added = [thread_id for thread_id in loaded_threads if thread_id not in previous]
        removed = [thread_id for thread_id in self.threads if thread_id not in current]
        self.threads = loaded_threads
        self.args.poll_seconds = loaded_poll_seconds
        self.args.message = loaded_message
        for thread_id in removed:
            self.trackers.pop(thread_id, None)
            self.last_logged.pop(thread_id, None)
            LOG.info("%s: removed from configuration; monitoring stopped", thread_id)
        for thread_id in added:
            LOG.info("%s: added from configuration", thread_id)
        if poll_changed:
            LOG.info("Configuration changed: poll_seconds=%s", loaded_poll_seconds)
        if message_changed:
            LOG.info("Configuration changed: message=%r", loaded_message)
        LOG.info(
            "Configuration loaded: monitoring %d session(s), poll_seconds=%s",
            len(self.threads),
            self.args.poll_seconds,
        )
        return bool(added) or poll_changed

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def _poll_thread(self, thread_id: str) -> None:
        tracker = self.trackers.get(thread_id)
        if tracker is None or not tracker.path.exists():
            path = discover_rollout(self.args.codex_home, thread_id)
            if path is None:
                if self.last_logged.get(thread_id) != "missing":
                    LOG.warning("%s: rollout file not found yet", thread_id)
                    self.last_logged[thread_id] = "missing"
                return
            tracker = RolloutTracker(thread_id=thread_id, path=path)
            self.trackers[thread_id] = tracker
            LOG.info("%s: monitoring %s", thread_id, path)

        was_initialized = tracker.initialized
        changed = tracker.poll()
        snapshot = tracker.snapshot
        log_key = snapshot.signature
        if changed or self.last_logged.get(thread_id) != log_key:
            LOG.info(status_line(snapshot))
            self.last_logged[thread_id] = log_key

        if snapshot.state == "failed":
            self._handle_failure(snapshot, existing=not was_initialized)
        elif snapshot.state == "running" and self.args.stale_seconds > 0:
            self._handle_stale(snapshot)

    def _handle_failure(self, snapshot: SessionSnapshot, existing: bool) -> None:
        signature = snapshot.signature
        now_epoch = time.time()
        event_state = self.state.event(signature)
        if event_state.get("handled") or now_epoch < event_state.get("next_attempt_at", 0):
            return

        if self.args.transient_errors_only and not is_transient_error(snapshot.error):
            LOG.error(
                "%s: non-transient failure; not continuing automatically: %s",
                snapshot.thread_id,
                describe_error(snapshot.error),
            )
            self.state.mark_ignored(signature, "non_transient", now_epoch)
            self.state.save()
            return

        if (
            existing
            and self.args.ignore_old_errors
            and int(event_state.get("attempts", 0)) == 0
            and snapshot.event_time is not None
        ):
            age = max(0.0, (utc_now() - snapshot.event_time).total_seconds())
            if age > self.args.max_error_age:
                LOG.warning(
                    "%s: failure is %.0fs old and --ignore-old-errors is enabled",
                    snapshot.thread_id,
                    age,
                )
                self.state.mark_ignored(signature, "too_old", now_epoch)
                self.state.save()
                return

        self._continue(snapshot.thread_id, signature, "failed turn")

    def _handle_stale(self, snapshot: SessionSnapshot) -> None:
        if snapshot.last_activity is None:
            return
        age = max(0.0, (utc_now() - snapshot.last_activity).total_seconds())
        if age < self.args.stale_seconds:
            return
        signature = f"{snapshot.thread_id}:{snapshot.turn_id or 'no-turn'}:stale"
        event_state = self.state.event(signature)
        now_epoch = time.time()
        if event_state.get("handled") or now_epoch < event_state.get("next_attempt_at", 0):
            return
        LOG.warning("%s: no rollout activity for %.0fs", snapshot.thread_id, age)
        self._continue(snapshot.thread_id, signature, "stale running turn")

    def _continue(self, thread_id: str, signature: str, reason: str) -> None:
        now_epoch = time.time()
        if (
            self.args.max_resumes_per_hour > 0
            and self.state.recent_action_count(thread_id, now_epoch)
            >= self.args.max_resumes_per_hour
        ):
            LOG.error(
                "%s: hourly auto-resume limit (%d) reached; leaving session untouched",
                thread_id,
                self.args.max_resumes_per_hour,
            )
            self.state.event(signature)["next_attempt_at"] = now_epoch + 60.0
            self.state.save()
            return

        command = [
            self.args.codex_binary,
            "queue",
            "--thread",
            thread_id,
            "--message",
            self.args.message,
            "--resume",
        ]
        if self.args.dry_run:
            LOG.warning("DRY RUN: would continue %s after %s: %s", thread_id, reason, command)
            self.state.mark_ignored(signature, "dry_run", now_epoch)
            return

        LOG.warning("%s: %s; queueing %r", thread_id, reason, self.args.message)
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.args.command_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("%s: failed to invoke Codex: %s", thread_id, exc)
            self.state.mark_failure(signature, now_epoch, self.args.max_send_attempts)
            self.state.save()
            return

        details = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if result.returncode == 0:
            LOG.info(
                "%s: continuation queued successfully%s",
                thread_id,
                f": {details}" if details else "",
            )
            self.state.mark_action(thread_id, signature, now_epoch)
        else:
            LOG.error(
                "%s: codex queue exited %d%s",
                thread_id,
                result.returncode,
                f": {details}" if details else "",
            )
            self.state.mark_failure(signature, now_epoch, self.args.max_send_attempts)
        self.state.save()


def list_sessions(codex_home: Path, limit: int) -> int:
    root = codex_home / "sessions"
    if not root.is_dir():
        LOG.error("Codex sessions directory does not exist: %s", root)
        return 1
    candidates = sorted(
        root.glob("**/rollout-*.jsonl"), key=lambda path: path.stat().st_mtime_ns, reverse=True
    )[:limit]
    print("THREAD_ID\tSTATE\tSOURCE\tCWD")
    for path in candidates:
        guessed = path.stem.rsplit("-", 5)[-5:]
        thread_id = "-".join(guessed)
        tracker = RolloutTracker(thread_id=thread_id, path=path)
        tracker.poll()
        snapshot = tracker.snapshot
        source = snapshot.source or "-"
        cwd = snapshot.cwd or "-"
        print(
            f"{snapshot.thread_id}\t{snapshot.state}\t{source}\t{cwd}"
        )
    return 0


def write_initial_config(path: Path) -> int:
    if path.exists():
        LOG.error("Configuration already exists; leaving it unchanged: %s", path)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
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
            encoding="utf-8",
        )
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    print(path)
    return 0


def systemd_unit(script_path: Path, config_path: Path, codex_binary: str = "codex") -> str:
    executable = str(script_path).replace("\\", "\\\\").replace('"', '\\"')
    config = str(config_path).replace("\\", "\\\\").replace('"', '\\"')
    codex = str(codex_binary).replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description=Codex session watchdog
After=default.target

[Service]
Type=simple
ExecStart=\"{executable}\" --config \"{config}\" --codex-binary \"{codex}\"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def build_parser() -> argparse.ArgumentParser:
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    parser = argparse.ArgumentParser(
        description="Monitor Codex session rollouts and auto-continue failed turns."
    )
    parser.add_argument(
        "threads",
        nargs="*",
        type=validate_thread_id,
        help="static Codex session UUID(s); omit to use the default config file",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--codex-home", type=Path, default=default_home)
    parser.add_argument("--codex-binary", default=shutil.which("codex") or "codex")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        help="hot-reloaded JSON configuration; cannot be combined with positional UUIDs",
    )
    parser.add_argument(
        "--config-reload-seconds",
        type=float,
        default=5.0,
        help="configuration reload interval (default: 5)",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="create an empty configuration file and exit",
    )
    parser.add_argument(
        "--print-systemd-unit",
        action="store_true",
        help="print a user-service unit for the selected configuration and exit",
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--once", action="store_true", help="inspect once and exit")
    parser.add_argument("--dry-run", action="store_true", help="log actions without invoking Codex")
    parser.add_argument(
        "--ignore-old-errors",
        action="store_true",
        help="do not recover an initial failure older than --max-error-age",
    )
    parser.add_argument(
        "--max-error-age",
        type=float,
        default=600.0,
        help="maximum age in seconds for an initial failure (default: 600)",
    )
    parser.add_argument(
        "--transient-errors-only",
        action="store_true",
        help="only continue recognized transient errors instead of every task_complete error",
    )
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=0.0,
        help="queue a continuation after this many silent seconds; 0 disables it (default)",
    )
    parser.add_argument(
        "--max-resumes-per-hour",
        type=int,
        default=0,
        help="hourly limit per session; 0 means unlimited (default)",
    )
    parser.add_argument(
        "--max-send-attempts",
        type=int,
        default=0,
        help="codex queue retry limit; 0 means unlimited (default)",
    )
    parser.add_argument("--command-timeout", type=float, default=60.0)
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--list-limit", type=int, default=20)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--verbose", action="store_true")
    return parser


def configure_logging(verbose: bool, log_file: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def positive_or_zero(parser: argparse.ArgumentParser, name: str, value: float) -> None:
    if value < 0:
        parser.error(f"{name} must be >= 0")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    args.codex_home = args.codex_home.expanduser().resolve()
    default_config = args.codex_home / "session-watchdog" / "config.json"
    if args.config is not None:
        args.config = args.config.expanduser().resolve()
    elif not args.threads:
        args.config = default_config
    args.state_file = (
        args.state_file.expanduser().resolve()
        if args.state_file
        else args.codex_home / "session-watchdog" / "state.json"
    )

    positive_or_zero(parser, "--poll-seconds", args.poll_seconds)
    positive_or_zero(parser, "--max-error-age", args.max_error_age)
    positive_or_zero(parser, "--stale-seconds", args.stale_seconds)
    if args.config_reload_seconds <= 0:
        parser.error("--config-reload-seconds must be > 0")
    if args.max_resumes_per_hour < 0 or args.max_send_attempts < 0:
        parser.error("resume/attempt limits must be >= 0")
    if args.list_sessions:
        return list_sessions(args.codex_home, args.list_limit)
    if args.config is not None and args.threads:
        parser.error("--config cannot be combined with positional session UUIDs")
    if args.init_config:
        return write_initial_config(args.config or default_config)
    if args.print_systemd_unit:
        print(
            systemd_unit(
                Path(__file__).resolve(),
                args.config or default_config,
                args.codex_binary,
            ),
            end="",
        )
        return 0
    if args.poll_seconds == 0 and not args.once:
        parser.error("--poll-seconds must be > 0 unless --once is used")
    return Watchdog(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
