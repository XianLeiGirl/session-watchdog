const TRANSIENT_ERROR_CODES = new Set([
  "flexunavailable",
  "internalservererror",
  "policycapacityunavailable",
  "ratelimitexceeded",
  "responsestreamconnectionfailed",
  "responsestreamdisconnected",
  "responsetoomanyfailedattempts",
  "serveroverloaded",
]);
const TRANSIENT_OBJECT_KEYS = new Set([
  "httpconnectionfailed",
  "responsestreamconnectionfailed",
  "responsestreamdisconnected",
  "responsetoomanyfailedattempts",
]);
const PERMANENT_ERROR_CODES = new Set([
  "badrequest",
  "contextwindowexceeded",
  "cyberpolicy",
  "misalignmentpolicyviolation",
  "modelnotfound",
  "sandboxerror",
  "sessionbudgetexceeded",
  "threadrollbackfailed",
  "toomanydenials",
  "unauthorized",
  "usagelimitexceeded",
]);
const TRANSIENT_HTTP_STATUSES = new Set([408, 409, 425, 429, 500, 502, 503, 504]);
const TRANSIENT_MESSAGE = /(?:\b429\b|too many requests|rate[ _-]?limit|server overloaded|temporar(?:y|ily) unavailable|timed? out|connection (?:failed|reset)|stream (?:disconnect|connection fail)|exceeded retry limit)/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function canonicalToken(value: unknown): string {
  return String(value).toLocaleLowerCase("en-US").replace(/[^a-z0-9]/g, "");
}

function nestedHttpStatus(value: unknown): number | undefined {
  if (Array.isArray(value)) {
    for (const child of value) {
      const status = nestedHttpStatus(child);
      if (status !== undefined) return status;
    }
    return undefined;
  }
  if (!isRecord(value)) return undefined;
  for (const [key, child] of Object.entries(value)) {
    if (canonicalToken(key) === "httpstatuscode") {
      const status = Number(child);
      if (Number.isInteger(status)) return status;
    }
    const status = nestedHttpStatus(child);
    if (status !== undefined) return status;
  }
  return undefined;
}

function errorInfo(error: unknown): unknown {
  if (!isRecord(error)) return undefined;
  return error.codex_error_info ?? error.codexErrorInfo;
}

/** Return true only for failures that are normally safe to retry. */
export function isTransientError(error: unknown): boolean {
  if (error === undefined || error === null || error === false) return false;
  const info = errorInfo(error);
  const status = nestedHttpStatus(info ?? error);
  if (status !== undefined && (TRANSIENT_HTTP_STATUSES.has(status) || status >= 500)) {
    return true;
  }
  if (typeof info === "string") {
    const normalized = canonicalToken(info);
    if (PERMANENT_ERROR_CODES.has(normalized)) return false;
    if (TRANSIENT_ERROR_CODES.has(normalized)) return true;
  }
  if (
    isRecord(info) &&
    Object.keys(info).some((key) => TRANSIENT_OBJECT_KEYS.has(canonicalToken(key)))
  ) {
    return true;
  }
  const message = isRecord(error) ? String(error.message ?? "") : String(error);
  return TRANSIENT_MESSAGE.test(message);
}

export function describeError(error: unknown): string {
  if (error === undefined || error === null || error === false) return "unknown error";
  if (!isRecord(error)) return String(error);
  const message = String(error.message ?? "unknown error");
  const info = errorInfo(error);
  const status = nestedHttpStatus(info ?? error);
  return status !== undefined && !message.includes(String(status))
    ? `${message} (HTTP ${status})`
    : message;
}
