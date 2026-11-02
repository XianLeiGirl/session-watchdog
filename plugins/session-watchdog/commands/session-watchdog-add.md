# /session-watchdog-add

## Arguments

- `session_ids`: one or more Codex session UUIDs; required.

If no session ID is supplied, ask for it and stop. Do not guess an ID. Otherwise run only this watchdog mutation:

```bash
session-watchdog-plugin add <SESSION_ID> [SESSION_ID ...]
```

Pass every supplied ID as a separate argument. Report the monitored IDs and count from the returned JSON.
