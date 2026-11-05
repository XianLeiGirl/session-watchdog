# session-watchdog

TypeScript/Node.js implementation of
[Session Watchdog](https://github.com/XianLeiGirl/session-watchdog) runtime. It
monitors Codex rollout JSONL files and resumes failed turns while leaving normal
completion and manual interruption untouched.

The repository also keeps the original Python implementation. The npm package
provides the same management command set and supports a systemd user service on
Linux and a launchd agent on macOS.

## Install and use

```sh
npm install --global session-watchdog
session-watchdog plug-in
session-watchdog status
```

The default config is `~/.codex/session-watchdog/config.json`:

```json
{
  "session_ids": ["019a0000-0000-7000-8000-000000000001"],
  "poll_seconds": 300,
  "message": "继续",
  "max_send_attempts": 0,
  "max_resumes_per_hour": 0
}
```

List recent sessions:

```sh
npx session-watchdog --list-sessions
```

Add one or more sessions, then inspect the hot-reloaded configuration:

```sh
session-watchdog add <SESSION_ID> [SESSION_ID ...]
session-watchdog list
session-watchdog remove <SESSION_ID>
```

Configure the watcher without restarting it:

```sh
session-watchdog configure --poll-seconds 300 --message "继续" \
  --max-send-attempts 3 --max-resumes-per-hour 5
```

Service and plugin lifecycle:

```sh
session-watchdog start
session-watchdog stop
session-watchdog restart
session-watchdog plug-out
```

`plug-out` preserves configuration and state. Use `plug-out --purge` only when
you intentionally want to delete them.

Optional internal Feishu completion notifications retain the Python command
contract:

```sh
session-watchdog configure-feishu --chat-id <FEISHU_CHAT_ID>
session-watchdog disable-feishu
```

When `bytedcli` is absent, the first command bootstraps it and returns a
one-time Feishu setup URL; complete that setup and rerun the command.

Run `session-watchdog --help` for all options.

## Library API

```ts
import { SessionWatchdog } from "session-watchdog";

const watchdog = new SessionWatchdog({
  codexHome: "/Users/alice/.codex",
  codexBinary: "codex",
  stateFile: "/Users/alice/.codex/session-watchdog/state.json",
  threads: ["019a0000-0000-7000-8000-000000000001"],
  pollSeconds: 300,
  message: "继续",
});

await watchdog.run();
```

The package has zero runtime dependencies and supports Node.js 18 or newer.

## Development

```sh
npm install
npm run check
npm publish --dry-run
```
