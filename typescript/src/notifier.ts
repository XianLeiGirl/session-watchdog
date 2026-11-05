import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { type ControlContext, loadRawConfig } from "./control.js";

const INTERNAL_PROMPT_PREFIXES = [
  "Generate a concise, single-line task title",
  "Write a brief catch-up for a user returning to this task.",
] as const;

function compact(value: string, limit = 80): string {
  const text = value.trim().replace(/\s+/g, " ");
  return text.length > limit ? `${text.slice(0, limit).trim()}…` : text;
}

function title(payload: Record<string, unknown>): string {
  if (typeof payload["thread-title"] === "string" && payload["thread-title"].trim()) {
    return compact(payload["thread-title"]);
  }
  const inputs = payload["input-messages"];
  if (Array.isArray(inputs)) {
    for (const item of inputs) {
      if (typeof item === "string" && item.trim()) return compact(item);
      if (typeof item === "object" && item !== null) {
        for (const key of ["text", "content", "message"]) {
          const candidate = (item as Record<string, unknown>)[key];
          if (typeof candidate === "string" && candidate.trim()) return compact(candidate);
        }
      }
    }
  }
  return "未命名任务";
}

function isInternalPrompt(payload: Record<string, unknown>): boolean {
  const inputs = payload["input-messages"];
  if (!Array.isArray(inputs)) return false;

  for (const item of inputs) {
    const candidates = typeof item === "string"
      ? [item]
      : typeof item === "object" && item !== null
        ? ["text", "content", "message"].map((key) => (item as Record<string, unknown>)[key])
        : [];
    if (candidates.some((candidate) =>
      typeof candidate === "string"
      && INTERNAL_PROMPT_PREFIXES.some((prefix) => candidate.trimStart().startsWith(prefix)))) {
      return true;
    }
  }
  return false;
}

function idempotencyKey(eventKey: string): string {
  return `codex-${createHash("sha256").update(eventKey).digest("hex").slice(0, 32)}`;
}

export function notifyFeishu(context: ControlContext, rawPayload: string): number {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(rawPayload) as Record<string, unknown>;
  } catch {
    return 1;
  }
  if (payload.type !== "agent-turn-complete" || payload.error != null) return 0;
  for (const key of ["status", "turn-status", "turn_status"]) {
    const value = payload[key];
    if (typeof value === "string" && ["aborted", "cancelled", "canceled", "error", "failed"].includes(value.toLowerCase())) return 0;
  }
  if (isInternalPrompt(payload)) return 0;
  const config = loadRawConfig(context, false);
  if (!config.feishu.enabled) return 0;
  const thread = String(payload["thread-id"] ?? "unknown-thread");
  const turn = String(payload["turn-id"] ?? payload["turn_id"] ?? "unknown-turn");
  const result = spawnSync(
    config.feishu.bytedcli,
    [
      "--no-auto-upgrade",
      "--json",
      "lark",
      "im",
      "messages-send",
      "--chat-id",
      config.feishu.chat_id,
      "--as",
      "bot",
      "--text",
      `✅ ${title(payload)}`,
      "--idempotency-key",
      idempotencyKey(`${thread}|${turn}`),
    ],
    {
      encoding: "utf8",
      timeout: 30_000,
      env: {
        ...process.env,
        BYTEDCLI_NO_AUTO_UPGRADE: "1",
        LARKSUITE_CLI_NO_UPDATE_NOTIFIER: "1",
        LARKSUITE_CLI_NO_SKILLS_NOTIFIER: "1",
      },
    },
  );
  if (result.error !== undefined || result.status !== 0) return 1;
  try {
    return (JSON.parse(result.stdout) as { ok?: boolean }).ok === true ? 0 : 1;
  } catch {
    return 1;
  }
}
