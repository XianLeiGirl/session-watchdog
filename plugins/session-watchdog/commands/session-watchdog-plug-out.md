# /session-watchdog-plug-out

## Arguments

- `purge`: optional boolean, default false.

Without an explicit purge request, run exactly:

```bash
session-watchdog-plugin plug-out
```

This stops and removes the runtime stack while preserving configuration and state. Only when the user explicitly requests permanent deletion of configuration and state, run:

```bash
session-watchdog-plugin plug-out --purge
```

Report what was removed and whether configuration was preserved or purged.
