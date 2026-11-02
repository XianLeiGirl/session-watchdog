# Codex 会话看门狗

`codex_session_watchdog.py` 监控指定会话的 rollout JSONL：

- `task_started`：正常运行，不操作；
- `task_complete` 且没有 `error`：正常结束，不操作；
- `turn_aborted(reason=interrupted)`：视为人工停止，绝不自动续跑；
- 其他 `task_complete.error`：执行 `codex queue --thread <ID> --message 继续 --resume`。

脚本只依赖 Python 标准库和本机 `codex` CLI。

## 下载并安装命令入口

一条命令下载完整工具到 `~/session-watchdog` 并创建命令入口：

```bash
curl -fsSL https://raw.githubusercontent.com/XianLeiGirl/session-watchdog/main/install.sh | bash
```

也可以先克隆或解压完整目录，再运行：

```bash
bash ~/session-watchdog/install.sh
```

安装脚本会创建或修复命令入口：

```text
~/.local/bin/session-watchdog-plugin -> ~/session-watchdog/scripts/pluginctl.py
```

它不会自动启动服务。命令入口安装完成后，再执行
`session-watchdog-plugin plug-in` 安装插件并启动后台服务。若 `~/.local/bin`
不在 `PATH` 中，安装脚本会输出可直接执行的绝对路径。

## 配置文件动态监控

插件配置位于 `~/session-watchdog/config/config.json`：

```json
{
  "session_ids": [
    "019a0000-0000-7000-8000-000000000001",
    "019a0000-0000-7000-8000-000000000002"
  ],
  "poll_seconds": 300,
  "message": "继续",
  "max_send_attempts": 0,
  "max_resumes_per_hour": 0
}
```

可以直接编辑 JSON，或通过统一命令修改：

```bash
session-watchdog-plugin configure --poll-seconds 300 --message "继续" \
  --max-send-attempts 0 --max-resumes-per-hour 0
session-watchdog-plugin add <SESSION_ID>
session-watchdog-plugin remove <SESSION_ID>
```

配置每 5 秒热加载一次。`poll_seconds` 控制会话检查间隔，`message` 控制异常后发送的文本；
`max_send_attempts` 限制同一异常的发送失败重试次数，`max_resumes_per_hour` 限制每个会话滚动一小时内
成功续跑次数，两者为 `0` 均表示不限。修改后无需重启。新增 ID 会立即纳入并检查；删除 ID 会在最多 5 秒内停止监控。配置暂时写坏时
保留最后一次有效列表，不会误清空全部监控项。

## 后台常驻与开机自启

插件通过 systemd 用户服务负责后台运行、异常重启和登录后自动启动。一键插拔：

```bash
session-watchdog-plugin plug-in
session-watchdog-plugin plug-out
```

查看状态和日志：

```bash
systemctl --user status codex-session-watchdog.service
journalctl --user -u codex-session-watchdog.service -f
```

修改 `config.json` 不需要重启服务。`plug-out` 默认保留配置，`plug-out --purge` 才删除配置和状态。

如果希望退出登录后用户服务仍保持运行，可由具备权限的管理员启用 linger：

```bash
loginctl enable-linger "$USER"
```

## 行为与其他用法

在新 Codex 会话中可使用明确的插件 commands：

```text
/session-watchdog-status
/session-watchdog-list
/session-watchdog-add <SESSION_ID>
/session-watchdog-remove <SESSION_ID>
/session-watchdog-configure poll_seconds=300 message="继续"
/session-watchdog-plug-in
/session-watchdog-plug-out
/session-watchdog-service restart
```

这些 command 固定映射到对应的 `session-watchdog-plugin` 子命令；缺少必要参数时会先询问，不猜测值。

默认每 5 分钟检查一次；任何非人工的异常终止都会发送一次“继续”。默认两个限制均为 `0`，因此异常轮次和
发送失败重试都不限次数；可通过 `configure` 设置正整数限制。脚本也会恢复启动时
已经存在的失败；如不希望恢复旧失败，可使用 `--ignore-old-errors`，并通过 `--max-error-age` 设置年龄上限。
默认不把“长时间没有新日志”视为失败；如确实需要，可用 `--stale-seconds 1800` 开启，但它可能在超长
工具调用期间产生误判。

状态和去重信息保存在 `~/session-watchdog/config/state.json`。同一失败事件只发送一次，避免对同一个
已经终止的 turn 每 5 分钟重复刷消息；如果续跑后的新 turn 再次异常，则会继续发送且没有小时上限。
看门狗会在每次会话检查后自动清理状态：已处理且不再对应任何受监控会话当前状态的事件保留 30 天，
并最多保留最近 1000 条；操作记录保留 7 天、最多 1000 条。当前失败签名、未处理事件和等待重试的
事件始终保留，避免清理后重复发送“继续”。

## 飞书正常完成通知

启用并配置目标群聊：

```bash
session-watchdog-plugin configure-feishu --chat-id <FEISHU_CHAT_ID>
```

若首次运行时默认的 `bytedcli` 不在 `PATH` 中，命令会自动运行：

```bash
npm install -g @bytedance-dev/bytedcli@latest --registry https://bnpm.byted.org
bytedcli self skill install --skill bytedcli -g
```

安装成功后，命令会在后台启动 `bytedcli lark config --new`，并把动态生成的一次性
飞书应用/机器人创建链接作为 `agent_creation_url` 返回。打开该链接完成网页配置后，使用同一个
chat ID 再次执行 `configure-feishu`，才会启用通知。引导阶段不会部分写入飞书配置，也不会修改
Codex `notify` hook。自动安装依赖本机已有 `npm`；若安装、Skill 安装或链接生成失败，命令会报错。

如 `bytedcli` 不在 `PATH` 中，可明确指定：

```bash
session-watchdog-plugin configure-feishu \
  --chat-id <FEISHU_CHAT_ID> \
  --bytedcli /absolute/path/to/bytedcli
```

明确指定的路径不存在时会直接报错，不会自动安装并覆盖该路径。

该命令会执行以下动作：

1. 把 chat ID 和解析后的 `bytedcli` 路径保存到不会提交到 Git 的 `config/config.json`；
2. 备份原 Codex `notify` 设置，并把入口改为
   `~/session-watchdog/integrations/notify_feishu.py`；
3. 停止并删除旧版异常通知 watcher，删除其状态和旧投递账本。

它不会启动新的飞书后台服务，也不会维护本地投递账本。新开的 Codex 会话会使用新入口；
已经运行的会话可能继续使用启动时加载的旧入口。原有 `notify` 设置会保存在本地，执行以下命令时恢复：

```bash
session-watchdog-plugin disable-feishu
```

完成通知使用 `thread-id + turn-id` 的 SHA-256 摘要作为飞书幂等键。同一 turn
即使被重复回调也使用同一个键；同一会话的新 turn 会生成新键，所以能够多次完成、多次通知。
异常结束不发送飞书通知，由 Session Watchdog 自动发送配置中的续跑消息。

列出最近会话：

```bash
~/session-watchdog/runtime/codex_session_watchdog.py --list-sessions
```

按配置演练一次，不实际发送消息：

```bash
~/session-watchdog/runtime/codex_session_watchdog.py \
  --config ~/session-watchdog/config/config.json \
  --state-file ~/session-watchdog/config/state.json \
  --once --dry-run
```

不使用 systemd 时，也可以临时放到后台：

```bash
nohup ~/session-watchdog/runtime/codex_session_watchdog.py \
  --config ~/session-watchdog/config/config.json \
  --state-file ~/session-watchdog/config/state.json \
  --log-file ~/session-watchdog/config/watchdog.log \
  > /dev/null 2>&1 &
```

停止时向进程发送 `SIGTERM`，或前台按 `Ctrl-C`。
