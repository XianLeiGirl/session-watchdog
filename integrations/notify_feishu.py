#!/usr/bin/env python3
"""Send Codex completed-turn notifications to Feishu."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(
    os.environ.get(
        "SESSION_WATCHDOG_CONFIG",
        BUNDLE_ROOT / "config" / "config.json",
    )
).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
LOG_PATH = CONFIG_PATH.parent / "notify_feishu.log"
MAX_TASK_NAME_CHARS = 80
INTERNAL_PROMPT_PREFIXES = (
    "Generate a concise, single-line task title",
    "Write a brief catch-up for a user returning to this task.",
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


def is_internal_prompt(payload: dict[str, Any]) -> bool:
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
            and candidate.lstrip().startswith(INTERNAL_PROMPT_PREFIXES)
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


def idempotency_key(event_key: str) -> str:
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32]
    return f"codex-{digest}"


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
    return 0


def notify_completion(raw_payload: str) -> int:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        log_error(f"invalid Codex notification payload: {error}")
        return 1

    if payload.get("type") != "agent-turn-complete":
        return 0
    if payload.get("error") is not None:
        return 0
    for key in ("status", "turn-status", "turn_status"):
        value = payload.get(key)
        if isinstance(value, str) and value.lower() in {
            "aborted",
            "cancelled",
            "canceled",
            "error",
            "failed",
            "interrupted",
        }:
            return 0
    if is_internal_prompt(payload):
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
    if len(sys.argv) == 2 and sys.argv[1] == "--status":
        return status()
    if len(sys.argv) < 2:
        log_error("missing Codex notification payload")
        return 1
    return notify_completion(sys.argv[-1])


if __name__ == "__main__":
    raise SystemExit(main())
