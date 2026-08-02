# Codex AI 成本监控系统

这是一个本地、无第三方依赖的 Codex 成本治理包，目标是回答三件事：

1. 每个 Agent / thread / turn 使用了哪个模型、多少 tokens、产生多少成本。
2. 哪些成本是精确 token 用量，哪些只是 lifecycle hook 估算。
3. 普通任务是否误用了 GPT-5 旗舰模型，以及应该切换到哪个更便宜的模型。

## 已实现

- `codex exec --json` JSONL 导入：读取 `turn.completed.usage`，按 Agent、thread、turn、model 归因。
- Codex lifecycle hooks：记录 `UserPromptSubmit`、`SubagentStart/Stop`、`Stop`；为便于会话消耗追溯，会在本机数据库保存用户问题原文，但不保存回答正文。
- SQLite 本地账本：仓库下的 `data\cost_monitor.db`（只在本机生成，已被 `.gitignore` 排除）。
- 可替换计费表：默认是 OpenAI API 的 USD 价格，同时提供独立的 ChatGPT credits profile；两者不会被相加或互换。
- 模型治理：普通任务默认阻止旗舰模型，建议使用 `gpt-5.6-luna`；日常编码建议 `gpt-5.6-terra`；复杂架构、生产风险或多步骤调试建议 `gpt-5.6-sol`。
- CLI 报告、预算检查和本地 dashboard。
- PowerShell wrapper：让 `codex exec` 的精确用量自动落库。
- Dashboard 刷新时同步 Codex 本地 rollout 的 `token_count` 快照，桌面任务会显示为 `local_snapshot`，并保存对应的用户问题原文。

## 快速开始

从仓库根目录双击 `start.cmd`：它会初始化本地数据库、后台启动看板并打开浏览器。

```powershell
.\start.cmd
.\stop.cmd
```

也可以在 PowerShell 中启动；测试或只想启动服务时加 `-NoBrowser`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\start.ps1 -NoBrowser
```

```powershell
python .\cost_monitor.py init
python .\cost_monitor.py demo
python .\cost_monitor.py report
python .\cost_monitor.py dashboard
```

打开 `http://127.0.0.1:8765/` 查看 dashboard。

如果任务已经在 Codex 桌面端运行，刷新时会自动读取最近 30 天的本地会话快照。也可以手动同步：

```powershell
python .\cost_monitor.py sync-local --days 30
```

`local_snapshot` 是本机 rollout 中的 token 快照，适合实时观察；`codex exec --json` 导入的 `exact` 仍是精确账本。若要让 hooks 参与拦截策略，在 Codex 中用 `/hooks` 审核/信任后重启 Codex 或新开任务。

导入真实的 `codex exec --json` 输出：

```powershell
python .\cost_monitor.py ingest-jsonl `
  --file .\run.jsonl `
  --agent-id backend-reviewer `
  --model gpt-5.6-terra
```

## 接入 Codex hooks

用户级接入会影响之后打开的 Codex 会话；安装器会在替换前生成带时间戳的 `.bak` 备份：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\install.ps1 -Scope User
```

安装后在 Codex 中运行 `/hooks`，检查并信任新加入的 handlers；必要时重启 Codex 或新开会话。只想在当前项目接入时使用 `-Scope Project`。

默认策略是 `block`：当提示词明显属于总结、翻译、格式化、简单解释、状态查询等普通工作，且当前模型属于旗舰集合时，`UserPromptSubmit` 会阻止本轮并给出推荐模型。若误报较多，把 `config/cost-monitor.json` 中的：

```json
"routine_flagship_action": "block"
```

改为 `warn` 即可只提示不阻止。

## 精确成本采集

推荐通过 wrapper 运行非交互任务：

```powershell
$env:CODEX_COST_AGENT_ID = "nightly-tests"
& .\bin\codex-cost.ps1 `
  --model gpt-5.6-terra `
  "运行测试并总结失败原因"
```

wrapper 会保留 JSONL 到 `data/raw/`，并在任务结束后导入 SQLite。`--model` 也可以省略，默认使用 `gpt-5.6-terra`；建议在自动化任务中显式设置 Agent ID 和模型。

也可以直接使用策略评估：

```powershell
python .\cost_monitor.py route `
  --model gpt-5.6-sol `
  --task "总结这个文件的主要函数"
```

## 报告与预算

```powershell
python .\cost_monitor.py report --days 30
python .\cost_monitor.py report --days 30 --json
python .\cost_monitor.py check --days 1
python .\cost_monitor.py doctor
```

配置文件中的预算默认值是每日 20 USD、单 Agent 每日 8 USD、旗舰模型成本占比 35%。这些是治理阈值，不是 OpenAI 账户的硬限额；`check` 只负责报告并以非零退出码提醒 CI/脚本。

## 计费边界

默认 `openai_api_usd` profile 使用配置文件中记录的 OpenAI API 标准短上下文价格，价格来源和日期见 `config/cost-monitor.json`。公式为：

```text
成本 = 未缓存输入 tokens × 输入价
     + 缓存输入 tokens × 缓存价
     + 缓存写入 tokens × 缓存写入价（价格表提供时）
     + 输出 tokens × 输出价
```

ChatGPT 登录下的 Codex 使用共享计划额度/credits，不应假定等同于 API USD。若要以 credits 观察消耗，把 `billing_profile` 改为 `chatgpt_credits`；dashboard 会把单位显示为 `credits`，不会伪造美元。

## 数据准确性与局限

- `codex exec --json` 的 `turn.completed.usage` 记录标为 `exact`，这是主计费账本。
- lifecycle hooks 本身拿到的是模型、thread、prompt 和 stop 信息，不直接提供账单 token；这类记录标为 `estimated` 或 `estimated_lower_bound`。
- app-server / JSON-RPC 的完成事件也可通过 `ingest-jsonl` 导入；只有带 usage 的完成事件才生成精确 run，token usage snapshot 不会被重复累计。
- 未配置价格的模型标为 `unpriced`，不会静默记成 0。
- 账本不保存提示词或回答正文。若用户自己把原始 JSONL 放进 `data/raw/`，其中可能包含正文，应按自己的保留策略清理。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```

## 目录

```text
<repo-root>\
├─ cost_monitor.py             # CLI、SQLite、计价、策略、dashboard
├─ config\cost-monitor.json   # 价格、模型路由、预算、估算参数
├─ hooks\cost_monitor_hook.py # Codex lifecycle hook adapter
├─ .codex\hooks.json           # 项目级 hooks 示例
├─ bin\codex-cost.ps1          # codex exec --json wrapper
├─ start.cmd / start.ps1       # 一键启动本地 dashboard
├─ stop.cmd / stop.ps1         # 停止本地 dashboard
├─ install.ps1                 # 用户级/项目级 hooks 安装器
├─ scripts\install_hooks.py   # 保留原 hooks.json 的合并逻辑
├─ samples\sample-exec.jsonl  # 可回放的 JSONL 示例
├─ samples\sample-app-server.jsonl # app-server completion/snapshot 示例
└─ tests\                     # stdlib unittest
```
