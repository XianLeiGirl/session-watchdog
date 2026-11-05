#!/usr/bin/env node

import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

import { DEFAULT_MESSAGE, DEFAULT_POLL_SECONDS, loadConfig, validateThreadId } from "./config.js";
import { listSessions, SessionWatchdog, type WatchdogOptions } from "./watchdog.js";
import {
  configure,
  configureFeishu,
  controlContext,
  controlService,
  disableFeishu,
  loadRawConfig,
  plugIn,
  plugOut,
  updateSessions,
  watchdogStatus,
} from "./control.js";
import { notifyFeishu } from "./notifier.js";

const VERSION = "0.2.0";
interface CliOptions {
  threads: string[];
  codexHome: string;
  codexBinary: string;
  stateFile?: string;
  config?: string;
  pollSeconds: number;
  message: string;
  once: boolean;
  dryRun: boolean;
  ignoreOldErrors: boolean;
  maxErrorAge: number;
  transientErrorsOnly: boolean;
  staleSeconds: number;
  maxResumesPerHour: number;
  maxSendAttempts: number;
  commandTimeout: number;
  listSessions: boolean;
  listLimit: number;
  initConfig: boolean;
}

const HELP = `session-watchdog <command> [arguments]

Manage the Codex plugin, background service, and monitored sessions.

Commands:
  plug-in | install           Install/update the plugin and background service
  plug-out | uninstall       Remove plugin/service; --purge also deletes state
  status                     Show plugin, service, config, and Feishu state
  list                       List monitored session IDs
  add <SESSION_ID>...         Add monitored sessions
  remove <SESSION_ID>...      Remove monitored sessions
  configure [options]         Update hot-reloaded runtime settings
  configure-feishu --chat-id <ID> [--bytedcli <path>]
  disable-feishu              Restore the previous Codex notify hook
  service <start|stop|restart>
  start | stop | restart      Direct service-control aliases

Running without a command starts the foreground watcher.

Watcher options:
  --config <path>             JSON config (default: ~/.codex/session-watchdog/config.json)
  --codex-home <path>         Codex home (default: $CODEX_HOME or ~/.codex)
  --codex-binary <path>       Codex CLI executable (default: codex)
  --state-file <path>         Persistent deduplication state
  --poll-seconds <number>     Poll interval (default: 300)
  --message <text>            Resume message (default: 继续)
  --once                      Inspect once and exit
  --dry-run                   Record intended actions without invoking Codex
  --transient-errors-only     Retry only recognized transient errors
  --ignore-old-errors         Ignore an initial old failure
  --max-error-age <seconds>   Old failure threshold (default: 600)
  --stale-seconds <seconds>   Resume silent running turns; 0 disables
  --max-resumes-per-hour <n>  Per-session hourly limit; 0 means unlimited
  --max-send-attempts <n>     Queue failure retry limit; 0 means unlimited
  --command-timeout <seconds> Codex command timeout (default: 60)
  --list-sessions             List recent Codex sessions and exit
  --list-limit <n>            Number of sessions to list (default: 20)
  --init-config               Create an empty config and exit
  --version                   Print version
  --help                      Show this help
`;

function emit(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

function commandOption(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) throw new Error(`${name} requires a value`);
  return value;
}

async function runControlCommand(args: string[]): Promise<boolean> {
  const command = args[0];
  if (command === undefined || ![
    "plug-in", "install", "plug-out", "uninstall", "status", "list", "add",
    "remove", "configure", "configure-feishu", "disable-feishu", "service",
    "start", "stop", "restart", "__notify-feishu",
  ].includes(command)) return false;
  const context = controlContext();
  if (command === "__notify-feishu") {
    process.exitCode = notifyFeishu(context, args[1] ?? "{}");
    return true;
  }
  if (command === "plug-in" || command === "install") emit(plugIn(context));
  else if (command === "plug-out" || command === "uninstall") emit(plugOut(context, args.includes("--purge")));
  else if (command === "status") emit(watchdogStatus(context));
  else if (command === "list") emit(loadRawConfig(context, false).session_ids);
  else if (command === "add" || command === "remove") {
    const sessionIds = args.slice(1).filter((value) => !value.startsWith("--"));
    if (sessionIds.length === 0) throw new Error(`${command} requires at least one session UUID`);
    emit(updateSessions(context, sessionIds, command === "remove"));
  } else if (command === "configure") {
    const poll = commandOption(args, "--poll-seconds");
    const message = commandOption(args, "--message");
    const attempts = commandOption(args, "--max-send-attempts");
    const resumes = commandOption(args, "--max-resumes-per-hour");
    emit(configure(context, {
      ...(poll === undefined ? {} : { pollSeconds: numberValue(poll, "--poll-seconds") }),
      ...(message === undefined ? {} : { message }),
      ...(attempts === undefined ? {} : { maxSendAttempts: numberValue(attempts, "--max-send-attempts", true) }),
      ...(resumes === undefined ? {} : { maxResumesPerHour: numberValue(resumes, "--max-resumes-per-hour", true) }),
    }));
  } else if (command === "configure-feishu") {
    const chatId = commandOption(args, "--chat-id");
    if (chatId === undefined) throw new Error("configure-feishu requires --chat-id");
    emit(await configureFeishu(context, chatId, commandOption(args, "--bytedcli")));
  } else if (command === "disable-feishu") emit(disableFeishu(context));
  else {
    const action = command === "service" ? args[1] : command;
    if (action !== "start" && action !== "stop" && action !== "restart") {
      throw new Error("service requires start, stop, or restart");
    }
    controlService(context, action);
    emit(watchdogStatus(context));
  }
  return true;
}

function takeValue(args: string[], index: number, name: string): [string, number] {
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) throw new Error(`${name} requires a value`);
  return [value, index + 1];
}

function numberValue(value: string, name: string, integer = false): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0 || (integer && !Number.isInteger(number))) {
    throw new Error(`${name} must be a non-negative ${integer ? "integer" : "number"}`);
  }
  return number;
}

function parseArgs(args: string[]): CliOptions {
  const codexHome = resolve(process.env.CODEX_HOME ?? join(homedir(), ".codex"));
  const options: CliOptions = {
    threads: [], codexHome, codexBinary: "codex", pollSeconds: DEFAULT_POLL_SECONDS,
    message: DEFAULT_MESSAGE, once: false, dryRun: false, ignoreOldErrors: false,
    maxErrorAge: 600, transientErrorsOnly: false, staleSeconds: 0,
    maxResumesPerHour: 0, maxSendAttempts: 0, commandTimeout: 60,
    listSessions: false, listLimit: 20, initConfig: false,
  };
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--help") { process.stdout.write(HELP); process.exit(0); }
    if (arg === "--version") { console.log(VERSION); process.exit(0); }
    if (arg === "--once") options.once = true;
    else if (arg === "--dry-run") options.dryRun = true;
    else if (arg === "--ignore-old-errors") options.ignoreOldErrors = true;
    else if (arg === "--transient-errors-only") options.transientErrorsOnly = true;
    else if (arg === "--list-sessions") options.listSessions = true;
    else if (arg === "--init-config") options.initConfig = true;
    else if (arg?.startsWith("--")) {
      const [value, nextIndex] = takeValue(args, index, arg);
      index = nextIndex;
      if (arg === "--config") options.config = resolve(value);
      else if (arg === "--codex-home") options.codexHome = resolve(value);
      else if (arg === "--codex-binary") options.codexBinary = value;
      else if (arg === "--state-file") options.stateFile = resolve(value);
      else if (arg === "--message") options.message = value;
      else if (arg === "--poll-seconds") options.pollSeconds = numberValue(value, arg);
      else if (arg === "--max-error-age") options.maxErrorAge = numberValue(value, arg);
      else if (arg === "--stale-seconds") options.staleSeconds = numberValue(value, arg);
      else if (arg === "--max-resumes-per-hour") options.maxResumesPerHour = numberValue(value, arg, true);
      else if (arg === "--max-send-attempts") options.maxSendAttempts = numberValue(value, arg, true);
      else if (arg === "--command-timeout") options.commandTimeout = numberValue(value, arg);
      else if (arg === "--list-limit") options.listLimit = numberValue(value, arg, true);
      else throw new Error(`unknown option: ${arg}`);
    } else if (arg !== undefined) options.threads.push(validateThreadId(arg));
  }
  return options;
}

function writeInitialConfig(path: string): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(
    path,
    `${JSON.stringify({ session_ids: [], poll_seconds: 300, message: "继续" }, null, 2)}\n`,
    { encoding: "utf8", flag: "wx" },
  );
  chmodSync(path, 0o600);
  console.log(path);
}

async function main(): Promise<void> {
  const rawArgs = process.argv.slice(2);
  if (await runControlCommand(rawArgs)) return;
  const cli = parseArgs(rawArgs);
  const defaultConfig = join(cli.codexHome, "session-watchdog", "config.json");
  const configPath = cli.config ?? (cli.threads.length === 0 ? defaultConfig : undefined);
  if (cli.initConfig) { writeInitialConfig(configPath ?? defaultConfig); return; }
  if (cli.listSessions) {
    console.log("THREAD_ID\tSTATE\tSOURCE\tCWD");
    for (const session of listSessions(cli.codexHome, cli.listLimit)) {
      console.log(`${session.threadId}\t${session.state}\t${session.source ?? "-"}\t${session.cwd ?? "-"}`);
    }
    return;
  }
  if (configPath !== undefined && cli.threads.length > 0) {
    throw new Error("--config cannot be combined with positional session UUIDs");
  }
  let threads = cli.threads;
  if (configPath !== undefined) {
    const config = loadConfig(configPath);
    threads = config.sessionIds;
    cli.pollSeconds = config.pollSeconds ?? cli.pollSeconds;
    cli.message = config.message ?? cli.message;
    cli.maxSendAttempts = config.maxSendAttempts ?? cli.maxSendAttempts;
    cli.maxResumesPerHour = config.maxResumesPerHour ?? cli.maxResumesPerHour;
  }
  const options: WatchdogOptions = {
    codexHome: cli.codexHome,
    codexBinary: cli.codexBinary,
    stateFile: cli.stateFile ?? join(cli.codexHome, "session-watchdog", "state.json"),
    threads,
    pollSeconds: cli.pollSeconds,
    message: cli.message,
    once: cli.once,
    dryRun: cli.dryRun,
    ignoreOldErrors: cli.ignoreOldErrors,
    maxErrorAge: cli.maxErrorAge,
    transientErrorsOnly: cli.transientErrorsOnly,
    staleSeconds: cli.staleSeconds,
    maxResumesPerHour: cli.maxResumesPerHour,
    maxSendAttempts: cli.maxSendAttempts,
    commandTimeout: cli.commandTimeout,
    ...(configPath === undefined ? {} : { configFile: configPath }),
  };
  const watchdog = new SessionWatchdog(options);
  process.once("SIGINT", () => watchdog.stop());
  process.once("SIGTERM", () => watchdog.stop());
  await watchdog.run();
}

main().catch((error: unknown) => {
  console.error(`session-watchdog: ${(error as Error).message}`);
  process.exitCode = 1;
});
