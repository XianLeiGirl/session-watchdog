---
name: session-watchdog
description: Manage the local Codex session-watchdog service, monitored session IDs, and optional Feishu completed-turn notifications. Use when the user asks to install, start, stop, inspect, configure, or uninstall Session Watchdog or its Feishu integration; do not use for unrelated Codex failures or ordinary session work.
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
- Install or update the plugin, worker, user service, and any configured Feishu integration: `plug-in`.
- Add monitored sessions: `add <session-id>...`.
- Remove monitored sessions: `remove <session-id>...`.
- Configure the hot-reloaded interval and continuation text: `configure --poll-seconds <seconds> --message <text>`; either option may be omitted when changing only the other.
- Configure and enable normal Feishu completed-turn notifications: `configure-feishu --chat-id <chat-id> [--bytedcli <path>]`.
- Disable Feishu notifications and restore the prior Codex notify hook: `disable-feishu`.
- Control the installed service: `start`, `stop`, or `restart`.
- Remove the plugin, service, and managed worker while preserving configuration: `plug-out`.
- Permanently remove plugin, service, worker, configuration, and state only when the user explicitly requests a purge: `plug-out --purge`.

The configuration is authoritative and hot-reloaded. Do not restart the service after `add`, `remove`, or `configure`. Feishu settings stay local in the ignored `config/config.json`; never put a real chat ID or credentials in the skill or tracked example config. Feishu is invoked only by Codex's normal completed-turn notify hook; it has no error watcher or local delivery ledger. Do not edit Codex rollout files.

## Mutation boundary

Run `status` and `list` for read-only questions. Run plug-in, configuration changes, Feishu enable/disable, service control, plug-out, or purge only when the user explicitly requests that mutation. A plain `codex plugin remove` does not stop the watchdog service or restore the previous notify hook; use `session-watchdog-plugin plug-out` for a complete removal.

After an operation, report the watchdog state, Feishu state, and monitored session count. When purging, state that the saved configuration and deduplication state were deleted.
