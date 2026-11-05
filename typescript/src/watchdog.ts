import { spawn } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describeError, isTransientError } from "./errors.js";
import {
  readFirstRolloutRecord,
  RolloutTracker,
  rolloutMtime,
  snapshotSignature,
  type SessionSnapshot,
} from "./rollout.js";
import { StateStore } from "./state.js";
import { loadConfig } from "./config.js";

export interface Logger {
  info(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

export interface WatchdogOptions {
  codexHome: string;
  codexBinary: string;
  stateFile: string;
  threads: readonly string[];
  pollSeconds: number;
  message: string;
  once?: boolean;
  dryRun?: boolean;
  ignoreOldErrors?: boolean;
  maxErrorAge?: number;
  transientErrorsOnly?: boolean;
  staleSeconds?: number;
  maxResumesPerHour?: number;
  maxSendAttempts?: number;
  commandTimeout?: number;
  logger?: Logger;
  /** Optional hot-reloaded configuration file used by background services. */
  configFile?: string;
}

interface ResolvedWatchdogOptions {
  codexHome: string;
  codexBinary: string;
  stateFile: string;
  threads: readonly string[];
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
  configFile?: string;
}

const consoleLogger: Logger = {
  info: (message) => console.log(message),
  warn: (message) => console.warn(message),
  error: (message) => console.error(message),
};

function listFiles(root: string): string[] {
  if (!existsSync(root)) return [];
  const result: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) result.push(...listFiles(path));
    else if (entry.isFile() && /^rollout-.*\.jsonl$/.test(entry.name)) result.push(path);
  }
  return result;
}

export function discoverRollout(codexHome: string, threadId: string): string | undefined {
  const files = listFiles(join(codexHome, "sessions"));
  let matches = files.filter((path) => path.includes(threadId));
  if (matches.length === 0) {
    matches = files.filter((path) => {
      const payload = readFirstRolloutRecord(path)?.payload;
      return (
        typeof payload === "object" &&
        payload !== null &&
        ((payload as Record<string, unknown>).session_id === threadId ||
          (payload as Record<string, unknown>).id === threadId)
      );
    });
  }
  return matches.sort((left, right) => rolloutMtime(right) - rolloutMtime(left))[0];
}

export function listSessions(codexHome: string, limit = 20): SessionSnapshot[] {
  return listFiles(join(codexHome, "sessions"))
    .sort((left, right) => statSync(right).mtimeMs - statSync(left).mtimeMs)
    .slice(0, limit)
    .map((path) => {
      const guessed = path.match(/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i)?.[0] ?? "unknown";
      const tracker = new RolloutTracker(guessed, path);
      tracker.poll();
      return tracker.snapshot;
    });
}

function runCommand(command: string, args: string[], timeoutSeconds: number): Promise<{
  code: number;
  output: string;
}> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk) => (output += String(chunk)));
    child.stderr.on("data", (chunk) => (output += String(chunk)));
    child.once("error", reject);
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`command timed out after ${timeoutSeconds}s`));
    }, timeoutSeconds * 1_000);
    child.once("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, output: output.trim() });
    });
  });
}

/** Monitors configured Codex rollout files and resumes eligible failures. */
export class SessionWatchdog {
  readonly state: StateStore;
  private readonly trackers = new Map<string, RolloutTracker>();
  private readonly options: ResolvedWatchdogOptions;
  private readonly logger: Logger;
  private stopped = false;

  constructor(options: WatchdogOptions) {
    this.options = {
      codexHome: options.codexHome,
      codexBinary: options.codexBinary,
      stateFile: options.stateFile,
      threads: [...options.threads],
      pollSeconds: options.pollSeconds,
      message: options.message,
      once: options.once ?? false,
      dryRun: options.dryRun ?? false,
      ignoreOldErrors: options.ignoreOldErrors ?? false,
      maxErrorAge: options.maxErrorAge ?? 600,
      transientErrorsOnly: options.transientErrorsOnly ?? false,
      staleSeconds: options.staleSeconds ?? 0,
      maxResumesPerHour: options.maxResumesPerHour ?? 0,
      maxSendAttempts: options.maxSendAttempts ?? 0,
      commandTimeout: options.commandTimeout ?? 60,
      ...(options.configFile === undefined ? {} : { configFile: options.configFile }),
    };
    this.logger = options.logger ?? consoleLogger;
    this.state = new StateStore(options.stateFile);
  }

  stop(): void {
    this.stopped = true;
  }

  async run(): Promise<void> {
    let nextPoll = 0;
    while (!this.stopped) {
      const now = Date.now();
      if (now >= nextPoll) {
        await this.pollOnce();
        if (this.options.once || this.stopped) return;
        nextPoll = now + this.options.pollSeconds * 1_000;
      } else {
        if (this.reloadConfig()) nextPoll = 0;
      }
      await this.sleep(Math.min(5_000, Math.max(50, nextPoll - Date.now())));
    }
  }

  async pollOnce(): Promise<void> {
    this.reloadConfig();
    for (const threadId of this.options.threads) await this.pollThread(threadId);
    this.state.save(this.protectedSignatures());
  }

  private reloadConfig(): boolean {
    if (this.options.configFile === undefined) return false;
    try {
      const config = loadConfig(this.options.configFile);
      const previousThreads = this.options.threads.join("\n");
      const previousPollSeconds = this.options.pollSeconds;
      this.options.threads = [...config.sessionIds];
      this.options.pollSeconds = config.pollSeconds ?? this.options.pollSeconds;
      this.options.message = config.message ?? this.options.message;
      this.options.maxSendAttempts =
        config.maxSendAttempts ?? this.options.maxSendAttempts;
      this.options.maxResumesPerHour =
        config.maxResumesPerHour ?? this.options.maxResumesPerHour;
      const configured = new Set(this.options.threads);
      for (const threadId of this.trackers.keys()) {
        if (!configured.has(threadId)) this.trackers.delete(threadId);
      }
      return (
        previousThreads !== this.options.threads.join("\n") ||
        previousPollSeconds !== this.options.pollSeconds
      );
    } catch (error) {
      this.logger.error(
        `${(error as Error).message}; keeping the last valid configuration`,
      );
      return false;
    }
  }

  private sleep(milliseconds: number): Promise<void> {
    return new Promise((resolve) => {
      const started = Date.now();
      const check = (): void => {
        if (this.stopped || Date.now() - started >= milliseconds) resolve();
        else setTimeout(check, Math.min(500, milliseconds));
      };
      check();
    });
  }

  private protectedSignatures(): Set<string> {
    const result = new Set<string>();
    for (const tracker of this.trackers.values()) {
      if (tracker.snapshot.state === "failed") result.add(snapshotSignature(tracker.snapshot));
    }
    return result;
  }

  private async pollThread(threadId: string): Promise<void> {
    let tracker = this.trackers.get(threadId);
    if (tracker === undefined || !existsSync(tracker.path)) {
      const path = discoverRollout(this.options.codexHome, threadId);
      if (path === undefined) {
        this.logger.warn(`${threadId}: rollout file not found yet`);
        return;
      }
      tracker = new RolloutTracker(threadId, path);
      this.trackers.set(threadId, tracker);
      this.logger.info(`${threadId}: monitoring ${path}`);
    }

    const existing = !tracker.initialized;
    tracker.poll();
    const snapshot = tracker.snapshot;
    this.logger.info(`${snapshot.threadId} [${snapshot.state}] turn=${snapshot.turnId ?? "-"}`);
    if (snapshot.state === "failed") await this.handleFailure(snapshot, existing);
    else if (snapshot.state === "running" && this.options.staleSeconds > 0) {
      await this.handleStale(snapshot);
    }
  }

  private async handleFailure(snapshot: SessionSnapshot, existing: boolean): Promise<void> {
    const signature = snapshotSignature(snapshot);
    const now = Date.now() / 1_000;
    const event = this.state.event(signature);
    if (event.handled || now < event.next_attempt_at) return;
    if (this.options.transientErrorsOnly && !isTransientError(snapshot.error)) {
      this.logger.error(
        `${snapshot.threadId}: non-transient failure; not continuing automatically: ${describeError(snapshot.error)}`,
      );
      this.state.markIgnored(signature, "non_transient", now);
      return;
    }
    if (
      existing &&
      this.options.ignoreOldErrors &&
      event.attempts === 0 &&
      snapshot.eventTime !== undefined &&
      now - snapshot.eventTime.getTime() / 1_000 > this.options.maxErrorAge
    ) {
      this.state.markIgnored(signature, "too_old", now);
      return;
    }
    await this.continue(snapshot.threadId, signature, "failed turn");
  }

  private async handleStale(snapshot: SessionSnapshot): Promise<void> {
    if (snapshot.lastActivity === undefined) return;
    const now = Date.now() / 1_000;
    if (now - snapshot.lastActivity.getTime() / 1_000 < this.options.staleSeconds) return;
    const signature = `${snapshot.threadId}:${snapshot.turnId ?? "no-turn"}:stale`;
    const event = this.state.event(signature);
    if (event.handled || now < event.next_attempt_at) return;
    await this.continue(snapshot.threadId, signature, "stale running turn");
  }

  private async continue(threadId: string, signature: string, reason: string): Promise<void> {
    const now = Date.now() / 1_000;
    if (
      this.options.maxResumesPerHour > 0 &&
      this.state.recentActionCount(threadId, now) >= this.options.maxResumesPerHour
    ) {
      this.logger.error(`${threadId}: hourly auto-resume limit reached`);
      this.state.event(signature).next_attempt_at = now + 60;
      return;
    }
    const args = ["queue", "--thread", threadId, "--message", this.options.message, "--resume"];
    if (this.options.dryRun) {
      this.logger.warn(`DRY RUN: would continue ${threadId} after ${reason}`);
      this.state.markIgnored(signature, "dry_run", now);
      return;
    }
    try {
      const result = await runCommand(this.options.codexBinary, args, this.options.commandTimeout);
      if (result.code === 0) {
        this.logger.info(`${threadId}: continuation queued successfully${result.output ? `: ${result.output}` : ""}`);
        this.state.markAction(threadId, signature, now);
      } else {
        this.logger.error(`${threadId}: codex queue exited ${result.code}${result.output ? `: ${result.output}` : ""}`);
        this.state.markFailure(signature, now, this.options.maxSendAttempts);
      }
    } catch (error) {
      this.logger.error(`${threadId}: failed to invoke Codex: ${(error as Error).message}`);
      this.state.markFailure(signature, now, this.options.maxSendAttempts);
    }
  }
}
