import { spawn, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

import { DEFAULT_MESSAGE, DEFAULT_POLL_SECONDS, parseConfig, validateThreadId } from "./config.js";

const SERVICE = "codex-session-watchdog.service";
const LAUNCH_LABEL = "com.xianleigirl.session-watchdog";
const MARKETPLACE = "session-watchdog-npm";
const PLUGIN = "session-watchdog";
const SELECTOR = `${PLUGIN}@${MARKETPLACE}`;

export interface ControlPaths {
  home: string;
  codexHome: string;
  configDir: string;
  configPath: string;
  statePath: string;
  cliPath: string;
  nodePath: string;
}

export interface ControlContext extends ControlPaths {
  platform: NodeJS.Platform;
}

interface RawConfig {
  session_ids: string[];
  poll_seconds: number;
  message: string;
  max_send_attempts: number;
  max_resumes_per_hour: number;
  feishu: {
    enabled: boolean;
    chat_id: string;
    bytedcli: string;
  };
}

export class ControlError extends Error {}

export function controlContext(overrides: Partial<ControlContext> = {}): ControlContext {
  const home = overrides.home ?? homedir();
  const codexHome = overrides.codexHome ?? resolve(process.env.CODEX_HOME ?? join(home, ".codex"));
  const configDir = overrides.configDir ?? join(codexHome, "session-watchdog");
  return {
    home,
    codexHome,
    configDir,
    configPath: overrides.configPath ?? join(configDir, "config.json"),
    statePath: overrides.statePath ?? join(configDir, "state.json"),
    cliPath: overrides.cliPath ?? realpathSync(process.argv[1] ?? ""),
    nodePath: overrides.nodePath ?? realpathSync(process.execPath),
    platform: overrides.platform ?? process.platform,
  };
}

function run(
  command: string,
  args: string[],
  check = true,
  env: NodeJS.ProcessEnv = process.env,
): ReturnType<typeof spawnSync> {
  const result = spawnSync(command, args, { encoding: "utf8", env });
  if (check && (result.error !== undefined || result.status !== 0)) {
    const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new ControlError(
      `command failed (${result.status ?? "spawn"}): ${command} ${args.join(" ")}${details ? `: ${details}` : ""}`,
    );
  }
  return result;
}

export function findExecutable(name: string): string | undefined {
  if (name.includes("/")) {
    const path = resolve(name);
    return existsSync(path) ? path : undefined;
  }
  const extensions = process.platform === "win32" ? ["", ".cmd", ".exe"] : [""];
  for (const directory of (process.env.PATH ?? "").split(process.platform === "win32" ? ";" : ":")) {
    for (const extension of extensions) {
      const candidate = join(directory, `${name}${extension}`);
      if (existsSync(candidate)) return candidate;
    }
  }
  return undefined;
}

function atomicWrite(path: string, content: string, mode: number): void {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = join(dirname(path), `.${basename(path)}.${process.pid}.tmp`);
  writeFileSync(temporary, content, { encoding: "utf8", mode });
  renameSync(temporary, path);
}

export function loadRawConfig(context: ControlContext, create = false): RawConfig {
  if (!existsSync(context.configPath)) {
    if (!create) return defaultConfig();
    saveRawConfig(context, defaultConfig());
  }
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(context.configPath, "utf8"));
    parseConfig(raw);
  } catch (error) {
    throw new ControlError(`cannot read ${context.configPath}: ${(error as Error).message}`);
  }
  const document = raw as Partial<RawConfig>;
  return {
    ...defaultConfig(),
    ...document,
    feishu: { ...defaultConfig().feishu, ...(document.feishu ?? {}) },
  };
}

function defaultConfig(): RawConfig {
  return {
    session_ids: [],
    poll_seconds: DEFAULT_POLL_SECONDS,
    message: DEFAULT_MESSAGE,
    max_send_attempts: 0,
    max_resumes_per_hour: 0,
    feishu: { enabled: false, chat_id: "", bytedcli: "bytedcli" },
  };
}

export function saveRawConfig(context: ControlContext, document: RawConfig): void {
  parseConfig(document);
  atomicWrite(context.configPath, `${JSON.stringify(document, null, 2)}\n`, 0o600);
}

function servicePaths(context: ControlContext): { definition: string; installed: boolean } {
  if (context.platform === "linux") {
    const definition = join(context.home, ".config", "systemd", "user", SERVICE);
    return { definition, installed: existsSync(definition) };
  }
  if (context.platform === "darwin") {
    const definition = join(context.home, "Library", "LaunchAgents", `${LAUNCH_LABEL}.plist`);
    return { definition, installed: existsSync(definition) };
  }
  throw new ControlError(`background service is not supported on ${context.platform}`);
}

function xml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function serviceDefinition(context: ControlContext): string {
  const args = [
    context.cliPath,
    "--config",
    context.configPath,
    "--state-file",
    context.statePath,
  ];
  if (context.platform === "linux") {
    const quote = (value: string): string => `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
    return `[Unit]\nDescription=Codex session watchdog (npm)\nAfter=default.target\n\n[Service]\nType=simple\nExecStart=${quote(context.nodePath)} ${args.map(quote).join(" ")}\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n`;
  }
  const argumentsXml = [context.nodePath, ...args].map((value) => `      <string>${xml(value)}</string>`).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n  <key>Label</key><string>${LAUNCH_LABEL}</string>\n  <key>ProgramArguments</key><array>\n${argumentsXml}\n  </array>\n  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>StandardOutPath</key><string>${xml(join(context.configDir, "watchdog.log"))}</string>\n  <key>StandardErrorPath</key><string>${xml(join(context.configDir, "watchdog-error.log"))}</string>\n</dict></plist>\n`;
}

function launchDomain(): string {
  return `gui/${process.getuid?.() ?? 0}`;
}

export function serviceStatus(context: ControlContext): Record<string, unknown> {
  const { definition, installed } = servicePaths(context);
  let active = false;
  let enabled = installed;
  if (installed && context.platform === "linux") {
    active = run("systemctl", ["--user", "is-active", SERVICE], false).status === 0;
    enabled = run("systemctl", ["--user", "is-enabled", SERVICE], false).status === 0;
  } else if (installed && context.platform === "darwin") {
    active = run("launchctl", ["print", `${launchDomain()}/${LAUNCH_LABEL}`], false).status === 0;
  }
  return { installed, enabled, active, definition };
}

export function installService(context: ControlContext): void {
  loadRawConfig(context, true);
  const { definition } = servicePaths(context);
  atomicWrite(definition, serviceDefinition(context), 0o644);
  if (context.platform === "linux") {
    run("systemctl", ["--user", "daemon-reload"]);
    run("systemctl", ["--user", "enable", "--now", SERVICE]);
    run("systemctl", ["--user", "restart", SERVICE]);
  } else {
    run("launchctl", ["bootout", launchDomain(), definition], false);
    run("launchctl", ["bootstrap", launchDomain(), definition]);
    run("launchctl", ["enable", `${launchDomain()}/${LAUNCH_LABEL}`]);
    run("launchctl", ["kickstart", "-k", `${launchDomain()}/${LAUNCH_LABEL}`]);
  }
}

export function controlService(context: ControlContext, action: "start" | "stop" | "restart"): void {
  const { definition, installed } = servicePaths(context);
  if (!installed) throw new ControlError("watchdog service is not installed; run session-watchdog plug-in first");
  if (context.platform === "linux") {
    run("systemctl", ["--user", action, SERVICE]);
  } else if (action === "stop") {
    run("launchctl", ["bootout", launchDomain(), definition], false);
  } else if (action === "start") {
    run("launchctl", ["bootstrap", launchDomain(), definition], false);
    run("launchctl", ["kickstart", "-k", `${launchDomain()}/${LAUNCH_LABEL}`]);
  } else {
    run("launchctl", ["kickstart", "-k", `${launchDomain()}/${LAUNCH_LABEL}`]);
  }
}

export function uninstallService(context: ControlContext): void {
  const { definition } = servicePaths(context);
  if (context.platform === "linux") {
    run("systemctl", ["--user", "disable", "--now", SERVICE], false);
    rmSync(definition, { force: true });
    run("systemctl", ["--user", "daemon-reload"], false);
  } else {
    run("launchctl", ["bootout", launchDomain(), definition], false);
    rmSync(definition, { force: true });
  }
}

function pluginBundle(context: ControlContext): string {
  return join(context.home, ".local", "share", "session-watchdog", "plugin-marketplace");
}

const COMMANDS: Record<string, string> = {
  status: "Run `session-watchdog status` and report the returned JSON without changing state.",
  list: "Run `session-watchdog list` and report the monitored session IDs.",
  add: "Require one or more session UUIDs, then run `session-watchdog add <SESSION_ID>...`.",
  remove: "Require one or more session UUIDs, then run `session-watchdog remove <SESSION_ID>...`.",
  configure: "Run `session-watchdog configure` using only options explicitly supplied by the user.",
  "plug-in": "Run `session-watchdog plug-in` to install/update the plugin and service.",
  "plug-out": "Run `session-watchdog plug-out`; add `--purge` only when explicitly requested.",
  service: "Require start, stop, or restart, then run `session-watchdog service <action>`.",
  "configure-feishu": "Require --chat-id, then run `session-watchdog configure-feishu --chat-id <ID>`.",
  "disable-feishu": "Run `session-watchdog disable-feishu` and report the restored notify state.",
};

function writePluginBundle(context: ControlContext): void {
  const root = pluginBundle(context);
  const plugin = join(root, "plugins", PLUGIN);
  mkdirSync(join(root, ".agents", "plugins"), { recursive: true });
  mkdirSync(join(plugin, ".codex-plugin"), { recursive: true });
  mkdirSync(join(plugin, "commands"), { recursive: true });
  mkdirSync(join(plugin, "skills", PLUGIN, "agents"), { recursive: true });
  atomicWrite(join(root, ".agents", "plugins", "marketplace.json"), `${JSON.stringify({ name: MARKETPLACE, interface: { displayName: "Session Watchdog npm" }, plugins: [{ name: PLUGIN, source: { source: "local", path: `./plugins/${PLUGIN}` }, policy: { installation: "AVAILABLE", authentication: "ON_INSTALL" }, category: "Developer Tools" }] }, null, 2)}\n`, 0o644);
  atomicWrite(join(plugin, ".codex-plugin", "plugin.json"), `${JSON.stringify({ name: PLUGIN, version: "0.2.0", description: "Manage resilient Codex session monitoring from the npm CLI.", author: { name: "XianLeiGirl" }, license: "UNLICENSED", keywords: ["codex", "watchdog", "recovery"], skills: "./skills/", interface: { displayName: "Session Watchdog", shortDescription: "Manage resilient Codex session monitoring", longDescription: "Configure monitored sessions, run a background watchdog, and resume abnormal Codex failures.", developerName: "XianLeiGirl", category: "Developer Tools", capabilities: ["Interactive", "Write"] } }, null, 2)}\n`, 0o644);
  for (const [name, instruction] of Object.entries(COMMANDS)) {
    atomicWrite(join(plugin, "commands", `session-watchdog-${name}.md`), `# /session-watchdog-${name}\n\n${instruction}\n`, 0o644);
  }
  atomicWrite(
    join(plugin, "commands", "session-watchdog.md"),
    "# /session-watchdog\n\nRoute the requested action to the matching `session-watchdog` command. Require all mandatory arguments and never infer session IDs. Use `--purge` only when explicitly requested.\n",
    0o644,
  );
  atomicWrite(join(plugin, "skills", PLUGIN, "SKILL.md"), `---\nname: session-watchdog\ndescription: Manage the npm-installed Session Watchdog service, monitored Codex session IDs, and optional Feishu completion notifications.\n---\n\n# Session Watchdog\n\nUse \`session-watchdog <action>\`. Read-only: \`status\`, \`list\`. Mutations: \`plug-in\`, \`plug-out\`, \`add\`, \`remove\`, \`configure\`, \`start\`, \`stop\`, \`restart\`, \`configure-feishu\`, and \`disable-feishu\`. Never purge unless explicitly requested. Do not edit Codex rollout files.\n`, 0o644);
  atomicWrite(join(plugin, "skills", PLUGIN, "agents", "openai.yaml"), `interface:\n  display_name: "Session Watchdog"\n  short_description: "Manage resilient Codex session monitoring"\n  default_prompt: "Use $session-watchdog to show the watchdog status."\n`, 0o644);
}

function codexJson(args: string[]): unknown {
  const codex = findExecutable("codex");
  if (codex === undefined) throw new ControlError("codex executable was not found on PATH");
  const result = run(codex, [...args, "--json"]);
  try {
    return JSON.parse(result.stdout as string);
  } catch {
    throw new ControlError(`Codex returned invalid JSON: ${result.stdout}`);
  }
}

function marketplaceInstalled(): boolean {
  const document = codexJson(["plugin", "marketplace", "list"]) as { marketplaces?: Array<{ name?: string }> };
  return document.marketplaces?.some((item) => item.name === MARKETPLACE) === true;
}

function pluginInstalled(): boolean {
  const document = codexJson(["plugin", "list"]) as { installed?: Array<{ name?: string }> };
  return document.installed?.some((item) => item.name === PLUGIN) === true;
}

function installPlugin(context: ControlContext): void {
  writePluginBundle(context);
  if (!marketplaceInstalled()) codexJson(["plugin", "marketplace", "add", pluginBundle(context)]);
  if (pluginInstalled()) codexJson(["plugin", "remove", SELECTOR]);
  codexJson(["plugin", "add", SELECTOR]);
}

function uninstallPlugin(): void {
  if (pluginInstalled()) codexJson(["plugin", "remove", SELECTOR]);
}

export function watchdogStatus(context: ControlContext): Record<string, unknown> {
  const config = loadRawConfig(context, false);
  let marketplace = false;
  let plugin = false;
  let pluginError: string | undefined;
  try {
    marketplace = marketplaceInstalled();
    plugin = pluginInstalled();
  } catch (error) {
    pluginError = (error as Error).message;
  }
  return {
    marketplace_registered: marketplace,
    plugin_installed: plugin,
    service: serviceStatus(context),
    config: context.configPath,
    session_ids: config.session_ids,
    session_count: config.session_ids.length,
    poll_seconds: config.poll_seconds,
    message: config.message,
    max_send_attempts: config.max_send_attempts,
    max_resumes_per_hour: config.max_resumes_per_hour,
    feishu: { enabled: config.feishu.enabled, configured: config.feishu.enabled && config.feishu.chat_id.length > 0 },
    ...(pluginError === undefined ? {} : { plugin_error: pluginError }),
  };
}

export function updateSessions(context: ControlContext, values: string[], remove: boolean): Record<string, unknown> {
  const config = loadRawConfig(context, true);
  const requested = values.map(validateThreadId);
  config.session_ids = remove
    ? config.session_ids.filter((id) => !new Set(requested).has(id))
    : [...new Set([...config.session_ids, ...requested])];
  saveRawConfig(context, config);
  return watchdogStatus(context);
}

export function configure(context: ControlContext, changes: { pollSeconds?: number; message?: string; maxSendAttempts?: number; maxResumesPerHour?: number }): Record<string, unknown> {
  if (Object.keys(changes).length === 0) throw new ControlError("configure requires at least one option");
  const config = loadRawConfig(context, true);
  if (changes.pollSeconds !== undefined) {
    if (changes.pollSeconds <= 0) throw new ControlError("poll_seconds must be greater than 0");
    config.poll_seconds = changes.pollSeconds;
  }
  if (changes.message !== undefined) {
    if (changes.message.trim() === "") throw new ControlError("message must be non-empty");
    config.message = changes.message;
  }
  if (changes.maxSendAttempts !== undefined && (!Number.isInteger(changes.maxSendAttempts) || changes.maxSendAttempts < 0)) {
    throw new ControlError("max_send_attempts must be an integer greater than or equal to 0");
  }
  if (changes.maxResumesPerHour !== undefined && (!Number.isInteger(changes.maxResumesPerHour) || changes.maxResumesPerHour < 0)) {
    throw new ControlError("max_resumes_per_hour must be an integer greater than or equal to 0");
  }
  if (changes.maxSendAttempts !== undefined) config.max_send_attempts = changes.maxSendAttempts;
  if (changes.maxResumesPerHour !== undefined) config.max_resumes_per_hour = changes.maxResumesPerHour;
  saveRawConfig(context, config);
  return watchdogStatus(context);
}

export function plugIn(context: ControlContext): Record<string, unknown> {
  loadRawConfig(context, true);
  installPlugin(context);
  installService(context);
  return watchdogStatus(context);
}

export function plugOut(context: ControlContext, purge: boolean): Record<string, unknown> {
  const config = loadRawConfig(context, false);
  if (config.feishu.enabled || existsSync(notifyBackupPath(context))) {
    restoreNotify(context);
    config.feishu.enabled = false;
    saveRawConfig(context, config);
  }
  uninstallService(context);
  try { uninstallPlugin(); } catch { /* Preserve service removal if Codex is unavailable. */ }
  if (purge) rmSync(context.configDir, { recursive: true, force: true });
  const result = watchdogStatus(context);
  return { ...result, purged: purge };
}

function codexConfigPath(context: ControlContext): string {
  return join(context.codexHome, "config.toml");
}

function notifyBackupPath(context: ControlContext): string {
  return join(context.configDir, "notify-backup.json");
}

function parseNotify(text: string): { present: boolean; value?: string[] } {
  const lines = text.split(/(?<=\n)/);
  const rootEnd = lines.findIndex((line) => line.trimStart().startsWith("["));
  const root = lines.slice(0, rootEnd < 0 ? lines.length : rootEnd);
  const matches = root.filter((line) => /^\s*notify\s*=/.test(line));
  if (matches.length > 1) throw new ControlError("Codex config contains multiple top-level notify settings");
  if (matches.length === 0) return { present: false };
  const source = matches[0]?.split("=", 2)[1]?.trim();
  if (source === undefined || !source.endsWith("]")) {
    throw new ControlError("multi-line Codex notify settings are not supported");
  }
  try {
    const value: unknown = JSON.parse(source);
    if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) throw new Error();
    return { present: true, value };
  } catch {
    throw new ControlError("top-level Codex notify setting must be an array of strings");
  }
}

function writeNotify(context: ControlContext, command?: string[]): void {
  const path = codexConfigPath(context);
  const text = existsSync(path) ? readFileSync(path, "utf8") : "";
  parseNotify(text);
  const lines = text.split(/(?<=\n)/);
  const rootEndCandidate = lines.findIndex((line) => line.trimStart().startsWith("["));
  const rootEnd = rootEndCandidate < 0 ? lines.length : rootEndCandidate;
  const index = lines.slice(0, rootEnd).findIndex((line) => /^\s*notify\s*=/.test(line));
  const replacement = command === undefined ? undefined : `notify = ${JSON.stringify(command)}\n`;
  if (index >= 0) {
    if (replacement === undefined) lines.splice(index, 1);
    else lines[index] = replacement;
  } else if (replacement !== undefined) {
    lines.unshift(replacement);
  }
  atomicWrite(path, lines.join(""), 0o600);
}

function desiredNotify(context: ControlContext): string[] {
  return [context.nodePath, context.cliPath, "__notify-feishu"];
}

function installNotify(context: ControlContext): void {
  const path = codexConfigPath(context);
  const text = existsSync(path) ? readFileSync(path, "utf8") : "";
  const previous = parseNotify(text);
  if (!existsSync(notifyBackupPath(context))) {
    atomicWrite(notifyBackupPath(context), `${JSON.stringify(previous, null, 2)}\n`, 0o600);
  }
  writeNotify(context, desiredNotify(context));
}

function restoreNotify(context: ControlContext): void {
  const path = codexConfigPath(context);
  const current = parseNotify(existsSync(path) ? readFileSync(path, "utf8") : "");
  if (JSON.stringify(current.value) !== JSON.stringify(desiredNotify(context))) {
    rmSync(notifyBackupPath(context), { force: true });
    return;
  }
  let backup: { present?: boolean; value?: string[] } = { present: false };
  try {
    backup = JSON.parse(readFileSync(notifyBackupPath(context), "utf8")) as typeof backup;
  } catch {
    // A missing backup means notify did not previously exist.
  }
  writeNotify(context, backup.present === true ? backup.value : undefined);
  rmSync(notifyBackupPath(context), { force: true });
}

function resolveBytedcli(value: string): string {
  const executable = findExecutable(value);
  if (executable === undefined) throw new ControlError(`bytedcli executable was not found: ${value}`);
  return realpathSync(executable);
}

function installBytedcli(): string {
  const npm = findExecutable("npm");
  if (npm === undefined) throw new ControlError("npm was not found on PATH");
  run(
    npm,
    ["install", "-g", "@bytedance-dev/bytedcli@latest"],
    true,
    { ...process.env, NPM_CONFIG_REGISTRY: "http://bnpm.byted.org" },
  );
  const bytedcli = resolveBytedcli("bytedcli");
  run(bytedcli, ["self", "skill", "install", "--skill", "bytedcli", "-g"]);
  return bytedcli;
}

async function startLarkSetup(context: ControlContext, bytedcli: string): Promise<Record<string, unknown>> {
  mkdirSync(context.configDir, { recursive: true });
  const logPath = join(context.configDir, "bytedcli-lark-config.log");
  const descriptor = await import("node:fs").then(({ openSync }) => openSync(logPath, "w", 0o600));
  const child = spawn(bytedcli, ["--no-auto-upgrade", "lark", "config", "--new"], {
    detached: true,
    stdio: ["ignore", descriptor, descriptor],
  });
  child.unref();
  const deadline = Date.now() + 60_000;
  const pattern = /(?:verification_url|verification_uri_complete|console_url)["'=:\s]+(https:\/\/[^\s"'<>]+)/i;
  while (Date.now() < deadline) {
    const output = readFileSync(logPath, "utf8");
    const url = output.match(pattern)?.[1];
    if (url !== undefined) {
      return { agent_creation_url: url, setup_log: logPath, setup_pid: child.pid };
    }
    if (child.exitCode !== null) {
      throw new ControlError(`bytedcli could not create a Feishu setup link: ${output.trim()}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  child.kill("SIGTERM");
  throw new ControlError(`timed out waiting for bytedcli setup; see ${logPath}`);
}

export async function configureFeishu(
  context: ControlContext,
  chatId: string,
  requestedBytedcli?: string,
): Promise<Record<string, unknown>> {
  if (chatId.trim() === "") throw new ControlError("Feishu chat ID must be non-empty");
  const config = loadRawConfig(context, true);
  const configured = requestedBytedcli ?? config.feishu.bytedcli ?? "bytedcli";
  let bytedcli: string;
  try {
    bytedcli = resolveBytedcli(configured);
  } catch (error) {
    if (requestedBytedcli !== undefined && requestedBytedcli !== "bytedcli") throw error;
    bytedcli = installBytedcli();
    return {
      bytedcli,
      bytedcli_installed: true,
      bytedcli_skill_installed: true,
      feishu_configured: false,
      setup_required: true,
      ...(await startLarkSetup(context, bytedcli)),
      next_step: "Open agent_creation_url, finish the Feishu app setup, then rerun configure-feishu.",
    };
  }
  config.feishu = { enabled: true, chat_id: chatId.trim(), bytedcli };
  saveRawConfig(context, config);
  installNotify(context);
  return watchdogStatus(context);
}

export function disableFeishu(context: ControlContext): Record<string, unknown> {
  const config = loadRawConfig(context, true);
  config.feishu.enabled = false;
  saveRawConfig(context, config);
  restoreNotify(context);
  return watchdogStatus(context);
}
