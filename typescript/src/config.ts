import { readFileSync } from "node:fs";

export const DEFAULT_POLL_SECONDS = 300;
export const DEFAULT_MESSAGE = "继续";

export interface WatchdogConfig {
  sessionIds: string[];
  pollSeconds?: number;
  message?: string;
  maxSendAttempts?: number;
  maxResumesPerHour?: number;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function validateThreadId(value: string): string {
  const normalized = value.toLocaleLowerCase("en-US");
  if (!UUID_PATTERN.test(normalized)) {
    throw new Error(`not a Codex session UUID: ${value}`);
  }
  return normalized;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isInteger(value) || Number(value) < 0) {
    throw new Error(`${name} must be an integer greater than or equal to 0`);
  }
  return Number(value);
}

export function parseConfig(value: unknown): WatchdogConfig {
  const document: Record<string, unknown> = Array.isArray(value)
    ? { session_ids: value }
    : isRecord(value)
      ? value
      : {};
  const rawIds = document.session_ids;
  if (!Array.isArray(rawIds)) {
    throw new Error('configuration must contain a "session_ids" array');
  }
  const sessionIds: string[] = [];
  const seen = new Set<string>();
  for (const [index, rawId] of rawIds.entries()) {
    if (typeof rawId !== "string") throw new Error(`session_ids[${index}] must be a string`);
    let threadId: string;
    try {
      threadId = validateThreadId(rawId);
    } catch (error) {
      throw new Error(`session_ids[${index}]: ${(error as Error).message}`);
    }
    if (!seen.has(threadId)) {
      seen.add(threadId);
      sessionIds.push(threadId);
    }
  }
  const config: WatchdogConfig = { sessionIds };
  if (document.poll_seconds !== undefined) {
    const pollSeconds = Number(document.poll_seconds);
    if (
      typeof document.poll_seconds === "boolean" ||
      !Number.isFinite(pollSeconds) ||
      pollSeconds <= 0
    ) {
      throw new Error("poll_seconds must be a number greater than 0");
    }
    config.pollSeconds = pollSeconds;
  }
  if (document.message !== undefined) {
    if (typeof document.message !== "string" || document.message.trim() === "") {
      throw new Error("message must be a non-empty string");
    }
    config.message = document.message;
  }
  if (document.max_send_attempts !== undefined) {
    config.maxSendAttempts = nonNegativeInteger(document.max_send_attempts, "max_send_attempts");
  }
  if (document.max_resumes_per_hour !== undefined) {
    config.maxResumesPerHour = nonNegativeInteger(
      document.max_resumes_per_hour,
      "max_resumes_per_hour",
    );
  }
  return config;
}

export function loadConfig(path: string): WatchdogConfig {
  try {
    return parseConfig(JSON.parse(readFileSync(path, "utf8")));
  } catch (error) {
    throw new Error(`cannot read configuration ${path}: ${(error as Error).message}`);
  }
}
