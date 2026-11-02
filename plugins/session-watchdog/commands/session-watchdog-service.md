# /session-watchdog-service

## Arguments

- `action`: exactly one of `start`, `stop`, or `restart`; required.

If the action is missing or outside the allowlist, ask for a valid action and stop. Otherwise run exactly one of:

```bash
session-watchdog-plugin start
session-watchdog-plugin stop
session-watchdog-plugin restart
```

Do not install, uninstall, or change configuration. Report the resulting service state.
