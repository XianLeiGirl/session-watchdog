import {
  closeSync,
  existsSync,
  fstatSync,
  openSync,
  readFileSync,
  readSync,
  statSync,
} from "node:fs";

export type SessionState =
  | "unknown"
  | "running"
  | "completed"
  | "failed"
  | "interrupted";

export interface SessionSnapshot {
  threadId: string;
  state: SessionState;
  turnId?: string;
  eventTime?: Date;
  lastActivity?: Date;
  error?: unknown;
  abortReason?: string;
  cwd?: string;
  source?: string;
}

interface RolloutRecord {
  timestamp?: unknown;
  type?: unknown;
  payload?: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseTime(value: unknown): Date | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? undefined : time;
}

export function snapshotSignature(snapshot: SessionSnapshot): string {
  return [
    snapshot.threadId,
    snapshot.turnId ?? "no-turn",
    snapshot.state,
    snapshot.eventTime?.toISOString() ?? "no-time",
  ].join(":");
}

/** Incrementally reads complete JSONL records from a Codex rollout file. */
export class RolloutTracker {
  readonly path: string;
  snapshot: SessionSnapshot;
  offset = 0;
  initialized = false;
  malformedLines = 0;

  constructor(threadId: string, path: string) {
    this.path = path;
    this.snapshot = { threadId, state: "unknown" };
  }

  poll(): boolean {
    const before = snapshotSignature(this.snapshot);
    if (!existsSync(this.path)) return false;
    const descriptor = openSync(this.path, "r");
    try {
      const size = fstatSync(descriptor).size;
      if (size < this.offset) {
        this.offset = 0;
        this.snapshot = { threadId: this.snapshot.threadId, state: "unknown" };
      }
      if (size === this.offset) {
        this.initialized = true;
        return false;
      }
      const buffer = Buffer.alloc(size - this.offset);
      readSync(descriptor, buffer, 0, buffer.length, this.offset);
      const lastNewline = buffer.lastIndexOf(0x0a);
      if (lastNewline < 0) {
        this.initialized = true;
        return false;
      }
      const complete = buffer.subarray(0, lastNewline + 1).toString("utf8");
      this.offset += lastNewline + 1;
      for (const line of complete.split("\n")) {
        if (line.length === 0) continue;
        try {
          const record: unknown = JSON.parse(line);
          if (isRecord(record)) this.apply(record as RolloutRecord);
        } catch {
          this.malformedLines += 1;
        }
      }
    } finally {
      closeSync(descriptor);
    }
    this.initialized = true;
    return before !== snapshotSignature(this.snapshot);
  }

  private apply(record: RolloutRecord): void {
    const recordTime = parseTime(record.timestamp);
    if (recordTime !== undefined) this.snapshot.lastActivity = recordTime;
    if (!isRecord(record.payload)) return;
    const payload = record.payload;

    if (record.type === "session_meta") {
      const actualId = payload.session_id ?? payload.id;
      if (typeof actualId === "string") this.snapshot.threadId = actualId;
      if (typeof payload.cwd === "string") this.snapshot.cwd = payload.cwd;
      if (typeof payload.source === "string") this.snapshot.source = payload.source;
      return;
    }
    if (record.type !== "event_msg") return;

    const eventType = payload.type;
    const turnId = typeof payload.turn_id === "string" ? payload.turn_id : undefined;
    if (eventType === "task_started") {
      this.snapshot.state = "running";
      if (turnId !== undefined) this.snapshot.turnId = turnId;
      if (recordTime !== undefined) this.snapshot.eventTime = recordTime;
      delete this.snapshot.error;
      delete this.snapshot.abortReason;
    } else if (eventType === "task_complete") {
      const hasError = payload.error !== undefined && payload.error !== null;
      this.snapshot.state = hasError ? "failed" : "completed";
      if (turnId !== undefined) this.snapshot.turnId = turnId;
      if (recordTime !== undefined) this.snapshot.eventTime = recordTime;
      if (hasError) this.snapshot.error = payload.error;
      else delete this.snapshot.error;
      delete this.snapshot.abortReason;
    } else if (eventType === "turn_aborted") {
      this.snapshot.state = "interrupted";
      if (turnId !== undefined) this.snapshot.turnId = turnId;
      if (recordTime !== undefined) this.snapshot.eventTime = recordTime;
      delete this.snapshot.error;
      this.snapshot.abortReason = String(payload.reason ?? "interrupted");
    }
  }
}

export function readFirstRolloutRecord(path: string): Record<string, unknown> | undefined {
  try {
    const firstLine = readFileSync(path, "utf8").split("\n", 1)[0];
    if (firstLine === undefined) return undefined;
    const record: unknown = JSON.parse(firstLine);
    return isRecord(record) ? record : undefined;
  } catch {
    return undefined;
  }
}

export function rolloutMtime(path: string): number {
  return statSync(path).mtimeMs;
}
