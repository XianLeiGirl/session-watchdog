# Session Watchdog

Session Watchdog 是一个本地 Codex 会话监控工具。它定期检查配置中的会话，发现非人工触发的异常终止后，自动发送可配置的续跑消息；人工停止和正常结束不会触发续跑。

## 安装

一条命令下载到 `~/session-watchdog` 并创建命令入口：

```bash
curl -fsSL https://raw.githubusercontent.com/XianLeiGirl/session-watchdog/main/install.sh | bash
```

然后安装 Codex 插件和后台服务：

```bash
session-watchdog-plugin plug-in
```

安装脚本不会覆盖已有的非 Session Watchdog 目录或同名普通文件。希望先检查脚本时，可以下载后再执行：

```bash
curl -fsSLO https://raw.githubusercontent.com/XianLeiGirl/session-watchdog/main/install.sh
less install.sh
bash install.sh
```

也可以手动克隆或解压到 `~/session-watchdog`，再运行目录中的 `install.sh`。

## 常用命令

```bash
session-watchdog-plugin status
session-watchdog-plugin add <SESSION_ID>
session-watchdog-plugin remove <SESSION_ID>
session-watchdog-plugin configure --poll-seconds 300 --message "继续" \
  --max-send-attempts 0 --max-resumes-per-hour 0
session-watchdog-plugin list
session-watchdog-plugin plug-out
```

本地配置和运行状态分别保存在 `config/config.json` 与 `config/state.json`，它们不会提交到 Git。
`state.json` 会自动压缩：已处理且不再对应当前会话状态的事件保留 30 天，并最多保留最近
1000 条；当前事件、未处理事件和重试状态不会被清理。配置格式参考 `config.example.json`。
`max_send_attempts` 限制同一异常的发送失败重试次数，`max_resumes_per_hour` 限制每个会话
滚动一小时内成功续跑次数；两者为 `0` 时均表示不限。

## 飞书通知

可启用 Codex 正常完成通知：

```bash
session-watchdog-plugin configure-feishu --chat-id <FEISHU_CHAT_ID>
session-watchdog-plugin status
```

如果首次配置时找不到默认的 `bytedcli`，命令会自动执行：

```bash
npm install -g @bytedance-dev/bytedcli@latest --registry https://bnpm.byted.org
bytedcli self skill install --skill bytedcli -g
```

随后它会启动飞书应用/机器人创建流程，并在输出的 `agent_creation_url` 中返回一次性链接。
请打开链接完成网页配置，然后使用相同的 chat ID 再运行一次 `configure-feishu`。首次引导不会
提前修改 Session Watchdog 的飞书配置或 Codex `notify` hook。若明确传入的自定义
`--bytedcli` 路径不存在，命令仍会报错，不会用自动安装覆盖该选择。

`configure-feishu` 会把 chat ID 和解析后的 `bytedcli` 路径写入不会提交到 Git 的
`config/config.json`，备份原 Codex `notify` 设置，然后把入口改为
`~/session-watchdog/integrations/notify_feishu.py`。它还会清理旧版异常 watcher、旧状态和旧账本；
不会启动新的飞书后台服务，也不会记录本地投递账本。

配置后新开的 Codex 会话会使用该通知入口；已经运行的会话可能继续使用启动时加载的旧入口。
停用时会恢复启用前的 Codex `notify` 设置：

```bash
session-watchdog-plugin disable-feishu
```

完成通知按 `thread-id + turn-id` 生成稳定幂等键：同一个 turn 重复回调不会重复发送；
同一个会话后续完成的新 turn 具有不同 `turn-id`，仍会正常再次通知。异常结束不发送飞书通知，
由 Session Watchdog 按现有配置自动发送“继续”。

完整说明见 [docs/usage.md](docs/usage.md)。
