import { chmodSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

const EVENT_RETENTION_SECONDS = 30 * 86_400;
const ACTION_RETENTION_SECONDS = 7 * 86_400;
const MAX_HANDLED_EVENTS = 1_000;
const MAX_ACTIONS = 1_000;

export interface EventState {
  attempts: number;
  handled: boolean;
  next_attempt_at: number;
  handled_at?: number;
  result?: string;
  reason?: string;
}
export interface ActionState {
  thread_id: string;
  signature: string;
  at: number;
}
interface StateDocument {
  version: number;
  events: Record<string, EventState>;
  actions: ActionState[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Persistent deduplication, retry, and hourly-limit state. */
export class StateStore {
  readonly path: string;
  data: StateDocument = { version: 1, events: {}, actions: [] };

  constructor(path: string) {
    this.path = path;
    this.load();
  }

  private load(): void {
    try {
      const loaded: unknown = JSON.parse(readFileSync(this.path, "utf8"));
      if (!isRecord(loaded)) return;
      if (isRecord(loaded.events)) this.data.events = loaded.events as Record<string, EventState>;
      if (Array.isArray(loaded.actions)) this.data.actions = loaded.actions as ActionState[];
    } catch {
      // Missing or malformed state is safe to ignore; rollout state is authoritative.
    }
  }

  event(signature: string): EventState {
    return (this.data.events[signature] ??= {
      attempts: 0,
      handled: false,
      next_attempt_at: 0,
    });
  }

  recentActionCount(threadId: string, now: number, window = 3_600): number {
    return this.data.actions.filter(
      (action) => action.thread_id === threadId && action.at >= now - window,
    ).length;
  }

  markAction(threadId: string, signature: string, now: number): void {
    Object.assign(this.event(signature), {
      handled: true,
      handled_at: now,
      result: "queued",
    });
    this.data.actions.push({ thread_id: threadId, signature, at: now });
  }

  markIgnored(signature: string, reason: string, now: number): void {
    Object.assign(this.event(signature), {
      handled: true,
      handled_at: now,
      result: "ignored",
      reason,
    });
  }

  markFailure(signature: string, now: number, maxAttempts: number): void {
    const event = this.event(signature);
    const attempts = event.attempts + 1;
    const exhausted = maxAttempts > 0 && attempts >= maxAttempts;
    Object.assign(event, {
      attempts,
      next_attempt_at: now + Math.min(300, 5 * 2 ** Math.max(0, attempts - 1)),
      handled: exhausted,
      result: exhausted ? "abandoned" : "send_failed",
    });
    if (exhausted) event.handled_at = now;
  }

  prune(protectedSignatures: ReadonlySet<string> = new Set(), now = Date.now() / 1_000): boolean {
    const previous = JSON.stringify(this.data);
    this.data.actions = this.data.actions
      .filter((action) => Number.isFinite(action.at) && action.at >= now - ACTION_RETENTION_SECONDS)
      .slice(-MAX_ACTIONS);
    const actionTimes = new Map(this.data.actions.map((action) => [action.signature, action.at]));
    const events: Record<string, EventState> = {};
    const handled: Array<[number, string, EventState]> = [];
    for (const [signature, event] of Object.entries(this.data.events)) {
      if (protectedSignatures.has(signature) || event.handled !== true) {
        events[signature] = event;
        continue;
      }
      const handledAt = event.handled_at ?? actionTimes.get(signature);
      if (handledAt === undefined) events[signature] = event;
      else if (handledAt >= now - EVENT_RETENTION_SECONDS) handled.push([handledAt, signature, event]);
    }
    handled.sort((left, right) => right[0] - left[0]);
    for (const [, signature, event] of handled.slice(0, MAX_HANDLED_EVENTS)) {
      events[signature] = event;
    }
    this.data.events = events;
    return JSON.stringify(this.data) !== previous;
  }

  save(protectedSignatures: ReadonlySet<string> = new Set()): void {
    this.prune(protectedSignatures);
    mkdirSync(dirname(this.path), { recursive: true });
    const temporary = join(dirname(this.path), `.${basename(this.path)}.${process.pid}.tmp`);
    try {
      writeFileSync(temporary, `${JSON.stringify(this.data, null, 2)}\n`, "utf8");
      chmodSync(temporary, 0o600);
      renameSync(temporary, this.path);
    } finally {
      try {
        unlinkSync(temporary);
      } catch {
        // renameSync normally removed it.
      }
    }
  }
}
