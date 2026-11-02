# /session-watchdog-configure

## Arguments

- `poll_seconds`: positive monitoring interval in seconds; optional.
- `message`: non-empty continuation message; optional.

Require at least one argument. Do not infer an omitted value. Construct and run exactly one command using only the supplied options:

```bash
session-watchdog-plugin configure [--poll-seconds <SECONDS>] [--message <TEXT>]
```

Report the resulting interval and message. Configuration is hot-reloaded; do not restart the service.
