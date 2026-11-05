import assert from "node:assert/strict";
import { appendFileSync, chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  discoverRollout,
  isTransientError,
  parseConfig,
  RolloutTracker,
  SessionWatchdog,
  StateStore,
  configure,
  controlContext,
  loadRawConfig,
  plugIn,
  plugOut,
  updateSessions,
} from "../dist/esm/index.js";
import { notifyFeishu } from "../dist/esm/notifier.js";

function temporary(name) {
  const path = join(tmpdir(), `session-watchdog-${process.pid}-${Date.now()}-${name}`);
  mkdirSync(path, { recursive: true });
  return path;
}

const SESSION = "019a1812-6920-7000-8000-000000000001";

test("classifies transient and permanent Codex failures", () => {
  assert.equal(isTransientError({ message: "rate limit exceeded" }), true);
  assert.equal(isTransientError({ codex_error_info: { http_status_code: 503 } }), true);
  assert.equal(isTransientError({ codex_error_info: "ContextWindowExceeded" }), false);
});

test("parses and deduplicates config session IDs", () => {
  const config = parseConfig({
    session_ids: [SESSION, SESSION.toUpperCase()],
    poll_seconds: 15,
    message: "继续",
    max_send_attempts: 2,
  });
  assert.deepEqual(config.sessionIds, [SESSION]);
  assert.equal(config.pollSeconds, 15);
  assert.equal(config.maxSendAttempts, 2);
  assert.throws(() => parseConfig({ session_ids: ["invalid"] }), /UUID/);
});

test("rollout tracker distinguishes failure and manual abort", () => {
  const root = temporary("tracker");
  const path = join(root, `rollout-${SESSION}.jsonl`);
  writeFileSync(
    path,
    `${JSON.stringify({ timestamp: "2026-11-05T00:00:00Z", type: "session_meta", payload: { id: SESSION } })}\n` +
      `${JSON.stringify({ timestamp: "2026-11-05T00:00:01Z", type: "event_msg", payload: { type: "task_started", turn_id: "turn-1" } })}\n` +
      `${JSON.stringify({ timestamp: "2026-11-05T00:00:02Z", type: "event_msg", payload: { type: "task_complete", turn_id: "turn-1", error: { message: "HTTP 429" } } })}\n`,
  );
  const tracker = new RolloutTracker(SESSION, path);
  assert.equal(tracker.poll(), true);
  assert.equal(tracker.snapshot.state, "failed");
  appendFileSync(
    path,
    `${JSON.stringify({ timestamp: "2026-11-05T00:00:03Z", type: "event_msg", payload: { type: "turn_aborted", turn_id: "turn-2", reason: "interrupted" } })}\n`,
  );
  tracker.poll();
  assert.equal(tracker.snapshot.state, "interrupted");
  assert.equal(tracker.snapshot.abortReason, "interrupted");
});

test("rollout tracker waits for an incomplete final JSONL record", () => {
  const root = temporary("partial");
  const path = join(root, `rollout-${SESSION}.jsonl`);
  const record = JSON.stringify({
    timestamp: "2026-11-05T00:00:01Z",
    type: "event_msg",
    payload: { type: "task_started", turn_id: "turn-1" },
  });
  writeFileSync(path, record);
  const tracker = new RolloutTracker(SESSION, path);
  tracker.poll();
  assert.equal(tracker.snapshot.state, "unknown");
  appendFileSync(path, "\n");
  tracker.poll();
  assert.equal(tracker.snapshot.state, "running");
});

test("discovers migrated rollout files from session metadata", () => {
  const codexHome = temporary("discover");
  const directory = join(codexHome, "sessions", "2026", "11", "05");
  mkdirSync(directory, { recursive: true });
  const path = join(directory, "rollout-migrated.jsonl");
  writeFileSync(path, `${JSON.stringify({ type: "session_meta", payload: { session_id: SESSION } })}\n`);
  assert.equal(discoverRollout(codexHome, SESSION), path);
});

test("state store deduplicates actions and backs off failed sends", () => {
  const root = temporary("state");
  const path = join(root, "state.json");
  const store = new StateStore(path);
  const now = Date.now() / 1_000;
  store.markFailure("failure", now, 2);
  assert.equal(store.event("failure").attempts, 1);
  assert.equal(store.event("failure").next_attempt_at, now + 5);
  store.markFailure("failure", now + 10, 2);
  assert.equal(store.event("failure").handled, true);
  store.markAction(SESSION, "queued", now + 20);
  assert.equal(store.recentActionCount(SESSION, now + 21), 1);
  store.save();
  assert.equal(JSON.parse(readFileSync(path, "utf8")).events.failure.result, "abandoned");
});

test("dry run never invokes Codex and records the failed event once", async () => {
  const codexHome = temporary("dry-run");
  const directory = join(codexHome, "sessions");
  mkdirSync(directory, { recursive: true });
  const rollout = join(directory, `rollout-${SESSION}.jsonl`);
  writeFileSync(
    rollout,
    `${JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "task_complete", turn_id: "turn-dry", error: { message: "rate limit" } } })}\n`,
  );
  const stateFile = join(codexHome, "state.json");
  const messages = [];
  const watchdog = new SessionWatchdog({
    codexHome,
    codexBinary: join(codexHome, "missing-codex"),
    stateFile,
    threads: [SESSION],
    pollSeconds: 1,
    message: "继续",
    once: true,
    dryRun: true,
    logger: {
      info: (message) => messages.push(message),
      warn: (message) => messages.push(message),
      error: (message) => messages.push(message),
    },
  });
  await watchdog.run();
  assert.equal(messages.some((message) => message.startsWith("DRY RUN:")), true);
  const state = JSON.parse(readFileSync(stateFile, "utf8"));
  assert.equal(Object.values(state.events)[0].reason, "dry_run");
});

function fakeControlContext(root, platform = "linux") {
  const cliPath = join(root, "session-watchdog.js");
  writeFileSync(cliPath, "#!/usr/bin/env node\n");
  chmodSync(cliPath, 0o755);
  return controlContext({
    home: root,
    codexHome: join(root, ".codex"),
    configDir: join(root, ".codex", "session-watchdog"),
    configPath: join(root, ".codex", "session-watchdog", "config.json"),
    statePath: join(root, ".codex", "session-watchdog", "state.json"),
    cliPath,
    nodePath: process.execPath,
    platform,
  });
}

test("Feishu notifier ignores internal catch-up and title-generation turns", () => {
  const root = temporary("notify-filter");
  const context = fakeControlContext(root);
  mkdirSync(context.configDir, { recursive: true });
  const log = join(root, "notifications.log");
  const bytedcli = join(root, "bytedcli");
  writeFileSync(
    bytedcli,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> '${log}'\nprintf '{"ok":true}'\n`,
  );
  chmodSync(bytedcli, 0o755);
  writeFileSync(context.configPath, JSON.stringify({
    session_ids: [],
    feishu: { enabled: true, chat_id: "oc_test", bytedcli },
  }));

  for (const prompt of [
    "Write a brief catch-up for a user returning to this task. Return JSON with summary.",
    "Generate a concise, single-line task title for this conversation.",
  ]) {
    assert.equal(notifyFeishu(context, JSON.stringify({
      type: "agent-turn-complete",
      "thread-id": SESSION,
      "turn-id": prompt.slice(0, 12),
      "input-messages": [{ text: `  ${prompt}` }],
    })), 0);
  }
  assert.equal(existsSync(log), false);

  assert.equal(notifyFeishu(context, JSON.stringify({
    type: "agent-turn-complete",
    "thread-id": SESSION,
    "turn-id": "normal-turn",
    "input-messages": [{ text: "帮我检查部署状态" }],
  })), 0);
  assert.equal(readFileSync(log, "utf8").includes("帮我检查部署状态"), true);
});

test("management commands add, remove, and configure sessions", () => {
  const root = temporary("control-config");
  const context = fakeControlContext(root);
  updateSessions(context, [SESSION], false);
  let config = loadRawConfig(context);
  assert.deepEqual(config.session_ids, [SESSION]);

  configure(context, {
    pollSeconds: 20,
    message: "resume",
    maxSendAttempts: 3,
    maxResumesPerHour: 4,
  });
  config = loadRawConfig(context);
  assert.equal(config.poll_seconds, 20);
  assert.equal(config.message, "resume");
  assert.equal(config.max_send_attempts, 3);
  assert.equal(config.max_resumes_per_hour, 4);

  updateSessions(context, [SESSION], true);
  assert.deepEqual(loadRawConfig(context).session_ids, []);
});

test("plug-in installs an npm-backed systemd service and plugin bundle", () => {
  const root = temporary("control-plugin");
  const bin = join(root, "bin");
  mkdirSync(bin);
  const log = join(root, "commands.log");
  const codex = join(bin, "codex");
  writeFileSync(
    codex,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> '${log}'\ncase "$*" in\n  *'marketplace list'*) printf '{"marketplaces":[]}' ;;\n  *'plugin list'*) printf '{"installed":[]}' ;;\n  *) printf '{}' ;;\nesac\n`,
  );
  chmodSync(codex, 0o755);
  const systemctl = join(bin, "systemctl");
  writeFileSync(systemctl, `#!/bin/sh\nprintf '%s\\n' "$*" >> '${log}'\nexit 0\n`);
  chmodSync(systemctl, 0o755);
  const oldPath = process.env.PATH;
  process.env.PATH = `${bin}:${oldPath}`;
  try {
    const context = fakeControlContext(root);
    const state = plugIn(context);
    assert.equal(state.service.installed, true);
    const unit = readFileSync(
      join(root, ".config", "systemd", "user", "codex-session-watchdog.service"),
      "utf8",
    );
    assert.match(unit, /session-watchdog\.js/);
    assert.match(unit, /--config/);
    assert.equal(
      readFileSync(log, "utf8").includes("plugin marketplace add"),
      true,
    );
    assert.equal(
      readFileSync(log, "utf8").includes("enable --now codex-session-watchdog.service"),
      true,
    );

    const removed = plugOut(context, true);
    assert.equal(removed.purged, true);
  } finally {
    process.env.PATH = oldPath;
  }
});

test("plug-in installs a launchd agent on macOS", () => {
  const root = temporary("control-launchd");
  const bin = join(root, "bin");
  mkdirSync(bin);
  const codex = join(bin, "codex");
  writeFileSync(
    codex,
    "#!/bin/sh\ncase \"$*\" in\n  *'marketplace list'*) printf '{\"marketplaces\":[]}' ;;\n  *'plugin list'*) printf '{\"installed\":[]}' ;;\n  *) printf '{}' ;;\nesac\n",
  );
  chmodSync(codex, 0o755);
  const launchctl = join(bin, "launchctl");
  writeFileSync(launchctl, "#!/bin/sh\nexit 0\n");
  chmodSync(launchctl, 0o755);
  const oldPath = process.env.PATH;
  process.env.PATH = `${bin}:${oldPath}`;
  try {
    const context = fakeControlContext(root, "darwin");
    plugIn(context);
    const plist = readFileSync(
      join(root, "Library", "LaunchAgents", "com.xianleigirl.session-watchdog.plist"),
      "utf8",
    );
    assert.match(plist, /RunAtLoad/);
    assert.match(plist, /KeepAlive/);
    assert.match(plist, /session-watchdog\.js/);
  } finally {
    process.env.PATH = oldPath;
  }
});
