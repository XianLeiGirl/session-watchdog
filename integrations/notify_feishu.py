#!/usr/bin/env python3
"""Send Codex completion and fatal-error notifications to Feishu."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(
    os.environ.get(
        "SESSION_WATCHDOG_CONFIG",
        BUNDLE_ROOT / "config" / "config.json",
    )
).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
LOG_PATH = CONFIG_PATH.parent / "notify_feishu.log"
SESSIONS_PATH = CODEX_HOME / "sessions"
WATCH_STATE_PATH = CONFIG_PATH.parent / "notify_feishu_watch_state.json"
DELIVERY_STATE_PATH = CONFIG_PATH.parent / "notify_feishu_deliveries.json"
DELIVERY_LOCK_PATH = CONFIG_PATH.parent / ".notify_feishu_deliveries.lock"
WATCH_INTERVAL_SECONDS = 1.0
MAX_DELIVERY_RECORDS = 10000
MAX_TASK_NAME_CHARS = 80
TASK_TITLE_PROMPT_PREFIX = "Generate a concise, single-line task title"
THREAD_ID_PATTERN = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


class FeishuConfigError(RuntimeError):
    pass


def log_error(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(message.rstrip() + "\n")


def as_text(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def compact_text(value: str, limit: int) -> str:
    compacted = " ".join(value.split())
    if len(compacted) > limit:
        return compacted[:limit].rstrip() + "…"
    return compacted


def load_feishu_settings() -> tuple[str, str] | None:
    try:
        document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeishuConfigError(f"cannot read {CONFIG_PATH}: {exc}") from exc

    feishu = document.get("feishu")
    if not isinstance(feishu, dict) or feishu.get("enabled") is not True:
        return None

    chat_id = feishu.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise FeishuConfigError("feishu.chat_id must be a non-empty string")

    configured_binary = feishu.get("bytedcli", "bytedcli")
    if not isinstance(configured_binary, str) or not configured_binary.strip():
        raise FeishuConfigError("feishu.bytedcli must be a non-empty string")
    configured_binary = configured_binary.strip()

    if "/" in configured_binary:
        binary = str(Path(configured_binary).expanduser())
        if not Path(binary).is_file():
            raise FeishuConfigError(f"bytedcli executable was not found: {binary}")
    else:
        resolved = shutil.which(configured_binary)
        if resolved is None:
            raise FeishuConfigError(
                f"bytedcli executable was not found on PATH: {configured_binary}"
            )
        binary = resolved
    return chat_id.strip(), binary


def title_from_state(thread_id: str) -> str | None:
    state_databases = sorted(
        CODEX_HOME.glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for database in state_databases:
        try:
            connection = sqlite3.connect(
                f"file:{database}?mode=ro",
                uri=True,
                timeout=1,
            )
            try:
                row = connection.execute(
                    "SELECT name, title FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue

        if row:
            for candidate in row:
                if isinstance(candidate, str) and candidate.strip():
                    return compact_text(candidate, MAX_TASK_NAME_CHARS)
    return None


def title_from_inputs(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, str) and item.strip():
            return compact_text(item, MAX_TASK_NAME_CHARS)
        if isinstance(item, dict):
            for key in ("text", "content", "message"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return compact_text(candidate, MAX_TASK_NAME_CHARS)
    return None


def is_internal_title_generation(payload: dict[str, Any]) -> bool:
    value = payload.get("input-messages")
    if not isinstance(value, list):
        return False

    for item in value:
        candidates: list[Any]
        if isinstance(item, str):
            candidates = [item]
        elif isinstance(item, dict):
            candidates = [item.get(key) for key in ("text", "content", "message")]
        else:
            continue

        if any(
            isinstance(candidate, str)
            and candidate.lstrip().startswith(TASK_TITLE_PROMPT_PREFIX)
            for candidate in candidates
        ):
            return True
    return False


def task_name(payload: dict[str, Any]) -> str:
    direct_title = payload.get("thread-title")
    if isinstance(direct_title, str) and direct_title.strip():
        return compact_text(direct_title, MAX_TASK_NAME_CHARS)

    thread_id = as_text(payload.get("thread-id"), "")
    if thread_id:
        stored_title = title_from_state(thread_id)
        if stored_title:
            return stored_title

    input_title = title_from_inputs(payload.get("input-messages"))
    if input_title:
        return input_title
    return "未命名任务"


def build_message(payload: dict[str, Any]) -> str:
    return f"✅ {task_name(payload)}"


def summarize_error(value: Any) -> str:
    if isinstance(value, dict):
        message = value.get("message")
        text = message if isinstance(message, str) else json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)

    known_reasons = (
        "invalid_encrypted_content",
        "rate_limit_exceeded",
        "context_length_exceeded",
        "insufficient_quota",
    )
    lowered = text.lower()
    for reason in known_reasons:
        if reason in lowered:
            return reason
    return compact_text(text, 120) or "unknown error"


def build_error_message(payload: dict[str, Any], thread_id: str) -> str:
    return (
        f"❌ {task_name({'thread-id': thread_id})}：Codex 异常中断"
        f"（{summarize_error(payload.get('error'))}）"
    )


def idempotency_key(event_key: str) -> str:
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32]
    return f"codex-{digest}"


def load_delivery_events() -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(DELIVERY_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read delivery state {DELIVERY_STATE_PATH}: {exc}") from exc
    events = document.get("events", {})
    if not isinstance(events, dict):
        raise RuntimeError(f"invalid delivery state: {DELIVERY_STATE_PATH}")
    return {
        str(key): value
        for key, value in events.items()
        if isinstance(value, dict)
    }


def save_delivery_events(events: dict[str, dict[str, Any]]) -> None:
    if len(events) > MAX_DELIVERY_RECORDS:
        newest = sorted(
            events.items(),
            key=lambda item: str(item[1].get("sent_at", "")),
            reverse=True,
        )[:MAX_DELIVERY_RECORDS]
        events = dict(newest)
    DELIVERY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DELIVERY_STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "events": events}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, DELIVERY_STATE_PATH)


def response_message_id(response: dict[str, Any]) -> str | None:
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("message_id", "messageId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def send_feishu(message: str, event_key: str) -> int:
    try:
        settings = load_feishu_settings()
    except FeishuConfigError as exc:
        log_error(str(exc))
        return 1
    if settings is None:
        return 0
    chat_id, bytedcli = settings

    environment = os.environ.copy()
    environment.update(
        {
            "BYTEDCLI_NO_AUTO_UPGRADE": "1",
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    command = [
        bytedcli,
        "--json",
        "lark",
        "im",
        "messages-send",
        "--chat-id",
        chat_id,
        "--as",
        "bot",
        "--text",
        message,
        "--idempotency-key",
        idempotency_key(event_key),
    ]

    DELIVERY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DELIVERY_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        os.chmod(DELIVERY_LOCK_PATH, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            events = load_delivery_events()
        except RuntimeError as exc:
            log_error(str(exc))
            return 1
        if event_key in events:
            return 0

        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            log_error(f"failed to run Feishu notifier: {error}")
            return 1

        if result.returncode != 0:
            log_error(f"Feishu notifier exited {result.returncode}: {result.stderr.strip()}")
            return result.returncode

        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            log_error("Feishu notifier returned non-JSON output")
            return 1

        if response.get("ok") is not True:
            log_error("Feishu notifier returned ok != true")
            return 1

        record: dict[str, Any] = {
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "idempotency_key": idempotency_key(event_key),
        }
        message_id = response_message_id(response)
        if message_id is not None:
            record["message_id"] = message_id
        events[event_key] = record
        try:
            save_delivery_events(events)
        except OSError as exc:
            # Feishu has already accepted the idempotent request. A later retry
            # still uses the same server-side idempotency key.
            log_error(f"failed to save delivery state: {exc}")
        return 0


def thread_id_from_path(path: Path) -> str:
    match = THREAD_ID_PATTERN.search(path.name)
    return match.group(1) if match else "unknown-thread"


def is_task_error_event(event: Any) -> bool:
    if not isinstance(event, dict) or event.get("type") != "event_msg":
        return False
    payload = event.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("type") == "task_complete"
        and payload.get("error") is not None
    )


class SessionErrorWatcher:
    """Incrementally read Codex rollout files and report fatal turn errors."""

    def __init__(
        self,
        sessions_path: Path,
        state_path: Path,
        on_error: Callable[[dict[str, Any], Path], bool],
    ) -> None:
        self.sessions_path = sessions_path
        self.state_path = state_path
        self.on_error = on_error
        self.offsets: dict[str, int] = {}

    def _rollout_files(self) -> list[Path]:
        if not self.sessions_path.exists():
            return []
        return sorted(self.sessions_path.rglob("*.jsonl"))

    def _relative_key(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.sessions_path))
        except ValueError:
            return str(path)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.state_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps({"version": 1, "offsets": self.offsets}, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.state_path)

    def initialize(self) -> None:
        try:
            raw_state = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw_offsets = raw_state.get("offsets", {})
            if isinstance(raw_offsets, dict):
                self.offsets = {
                    str(key): int(value)
                    for key, value in raw_offsets.items()
                    if isinstance(value, int) and value >= 0
                }
                return
        except (OSError, ValueError, json.JSONDecodeError):
            pass

        # First startup begins at EOF so installing never replays old errors.
        self.offsets = {
            self._relative_key(path): path.stat().st_size
            for path in self._rollout_files()
        }
        self._save_state()

    def scan_once(self) -> int:
        reported = 0
        changed = False
        for path in self._rollout_files():
            key = self._relative_key(path)
            offset = self.offsets.get(key, 0)
            try:
                size = path.stat().st_size
                if offset > size:
                    offset = 0
                if offset == size:
                    continue
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        line_start = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            stream.seek(line_start)
                            break
                        try:
                            event = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            log_error(f"invalid rollout line in {path}: {error}")
                            offset = stream.tell()
                            changed = True
                            continue

                        if is_task_error_event(event):
                            payload = event["payload"]
                            if not self.on_error(payload, path):
                                break
                            reported += 1
                        offset = stream.tell()
                        changed = True
            except OSError as error:
                log_error(f"failed to read Codex rollout {path}: {error}")
                continue
            self.offsets[key] = offset

        if changed:
            try:
                self._save_state()
            except OSError as error:
                log_error(f"failed to save notifier watcher state: {error}")
        return reported


def notify_task_error(payload: dict[str, Any], path: Path) -> bool:
    thread_id = thread_id_from_path(path)
    turn_id = as_text(payload.get("turn_id"), "unknown-turn")
    message = build_error_message(payload, thread_id)
    return send_feishu(message, f"{thread_id}|{turn_id}|error") == 0


def watch_errors() -> int:
    try:
        if load_feishu_settings() is None:
            raise FeishuConfigError("Feishu notifications are disabled")
    except FeishuConfigError as exc:
        log_error(str(exc))
        return 1

    watcher = SessionErrorWatcher(SESSIONS_PATH, WATCH_STATE_PATH, notify_task_error)
    watcher.initialize()
    while True:
        watcher.scan_once()
        time.sleep(WATCH_INTERVAL_SECONDS)


def notify_completion(raw_payload: str) -> int:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        log_error(f"invalid Codex notification payload: {error}")
        return 1

    if payload.get("type") != "agent-turn-complete":
        return 0
    if is_internal_title_generation(payload):
        return 0

    event_key = "|".join(
        [
            as_text(payload.get("thread-id"), "unknown-thread"),
            as_text(payload.get("turn-id"), "unknown-turn"),
        ]
    )
    return send_feishu(build_message(payload), event_key)


def status() -> int:
    try:
        settings = load_feishu_settings()
        document = {
            "enabled": settings is not None,
            "configured": settings is not None,
            "bytedcli": settings[1] if settings is not None else None,
        }
        code = 0
    except FeishuConfigError as exc:
        document = {"enabled": True, "configured": False, "error": str(exc)}
        code = 1
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return code


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--watch":
        return watch_errors()
    if len(sys.argv) == 2 and sys.argv[1] == "--status":
        return status()
    if len(sys.argv) < 2:
        log_error("missing Codex notification payload")
        return 1
    return notify_completion(sys.argv[-1])


if __name__ == "__main__":
    raise SystemExit(main())
