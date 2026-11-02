# /session-watchdog

Route the requested action to the most specific Session Watchdog command. Prefer these explicit commands when the action is known:

- `/session-watchdog-status`
- `/session-watchdog-list`
- `/session-watchdog-add`
- `/session-watchdog-remove`
- `/session-watchdog-configure`
- `/session-watchdog-plug-in`
- `/session-watchdog-plug-out`
- `/session-watchdog-service`

## Arguments

- `action`: `status`, `install`, `configure`, `add`, `remove`, `start`, `stop`, `restart`, or `uninstall`.
- `session_ids`: one or more Codex session UUIDs for `add` or `remove`.
- `poll_seconds`: optional positive monitoring interval for `configure`.
- `message`: optional continuation text for `configure`.
- `purge`: optional boolean for `uninstall`; only use it when the user explicitly asks to delete saved configuration and state.

If the action or required arguments are missing, ask for them and stop. Run only the matching `session-watchdog-plugin` subcommand, then report its JSON result.
