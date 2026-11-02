---
name: session-watchdog
description: Manage the local Codex session-watchdog service and its monitored session IDs. Use when the user asks to install, start, stop, inspect, configure, or uninstall Session Watchdog; do not use for unrelated Codex failures or ordinary session work.
---

# Session Watchdog

Use the unified user-facing command:

```bash
session-watchdog-plugin <action> [arguments]
```

The nested `watchdogctl.py` is an internal implementation detail; do not expose its path to the user.

When the user invokes an explicit `/session-watchdog-*` command, follow that command's fixed action and argument contract; do not substitute a different watchdog operation.

## Operations

- Read-only status: `status` or `list`.
- Install or update the plugin, worker, and user service: `plug-in`.
- Add monitored sessions: `add <session-id>...`.
- Remove monitored sessions: `remove <session-id>...`.
- Configure the hot-reloaded interval and continuation text: `configure --poll-seconds <seconds> --message <text>`; either option may be omitted when changing only the other.
- Control the installed service: `start`, `stop`, or `restart`.
- Remove the plugin, service, and managed worker while preserving configuration: `plug-out`.
- Permanently remove plugin, service, worker, configuration, and state only when the user explicitly requests a purge: `plug-out --purge`.

The configuration is authoritative and hot-reloaded. Do not restart the service after `add`, `remove`, or `configure`. Do not edit Codex rollout files.

## Mutation boundary

Run `status` and `list` for read-only questions. Run plug-in, configuration changes, service control, plug-out, or purge only when the user explicitly requests that mutation. A plain `codex plugin remove` does not stop the systemd service; use `session-watchdog-plugin plug-out` for a complete removal.

After an operation, report the service state and monitored session count. When purging, state that the saved configuration and deduplication state were deleted.
