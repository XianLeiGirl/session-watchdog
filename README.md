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
session-watchdog-plugin configure --poll-seconds 300 --message "继续"
session-watchdog-plugin list
session-watchdog-plugin plug-out
```

本地配置和运行状态分别保存在 `config/config.json` 与 `config/state.json`，它们不会提交到 Git。配置格式参考 `config.example.json`。

## 飞书通知

已安装并登录 `bytedcli` 后，可启用 Codex 完成和异常通知：

```bash
session-watchdog-plugin configure-feishu --chat-id <FEISHU_CHAT_ID>
session-watchdog-plugin status
session-watchdog-plugin notification-status <THREAD_ID> <TURN_ID>
```

命令会把 chat ID 保存在不会提交到 Git 的本地配置中，安装 Codex `notify`
入口，并启动异常通知服务。配置后新开的 Codex 会话会使用该通知入口；已经运行的会话可能继续使用
启动时加载的旧入口。停用时会恢复启用前的 Codex `notify` 设置：

```bash
session-watchdog-plugin disable-feishu
```

完成通知按 `thread-id + turn-id` 生成稳定幂等键：同一个 turn 重复回调不会重复发送；
同一个会话后续完成的新 turn 具有不同 `turn-id`，仍会正常再次通知。异常通知还会持久化
rollout 文件读取偏移，避免重启后重放已经处理的事件。成功投递记录保存在本地
`config/notify_feishu_deliveries.json`，可以通过 `notification-status` 查询。

完整说明见 [docs/usage.md](docs/usage.md)。
