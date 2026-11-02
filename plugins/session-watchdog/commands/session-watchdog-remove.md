# /session-watchdog-remove

## Arguments

- `session_ids`: one or more Codex session UUIDs; required.

If no session ID is supplied, ask for it and stop. Do not guess an ID. Otherwise run only this watchdog mutation:

```bash
session-watchdog-plugin remove <SESSION_ID> [SESSION_ID ...]
```

Pass every supplied ID as a separate argument. This removes IDs from monitoring but does not delete Codex sessions. Report the remaining monitored IDs and count.
