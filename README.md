# fleet：agent 派单调度工具箱

| 文件 | 用途 |
|---|---|
| `fleet.py` | 派单 → 巡检 → 回收 → 反馈的最小闭环（单文件，纯标准库） |
| `task-card-template.md` | 任务卡模板：一张卡 = 一个 agent 自主跑 1–3 小时 |
| `fleet_metrics.py` | 度量你实际驱动了多少 agent 工作（agent-小时/天、单次自主时长、并发、$/agent-小时） |

## fleet.py 快速开始

```bash
python3 fleet.py --home ~/fleet init                 # 生成 cards/ state/ runs/ worktrees/ 和 config.json
cp task-card-template.md ~/fleet/cards/<id>.md       # 写卡，文件名去掉 .md 就是 id
python3 fleet.py --home ~/fleet tick                 # 跑一次对账（幂等，可以随便重复跑）
python3 fleet.py --home ~/fleet status               # 看板，同时写入 ~/fleet/STATUS.md
```

卡片 front matter（只有 `repo` 和 `accept` 必填）：

| 字段 | 含义 |
|---|---|
| `repo` | 目标 git 仓库路径 |
| `accept` | 验收命令，在 worktree 根目录执行，退出码 0 才算过；**门禁说了算，agent 的自述不算** |
| `runtime` / `fallback` | 用哪个 agent（`claude`、`codex` 或 config 里自定义的）；从第 2 次尝试起换成 `fallback` |
| `model` | 覆盖该 runtime 的默认模型 |
| `base` / `depends_on` | 从哪个分支开 worktree；依赖的卡全部 `done` 才派发（可以叠在依赖卡的分支上） |
| `priority` / `risk` | 数字越小越先派；`risk: low` 过门禁直接 `done`，否则进 `review` 等人 |
| `max_attempts` / `timeout_min` | 重试次数；单次时长上限，超时会杀掉该卡自己的进程组 |
| `on_pass` | 过门禁后在 worktree 里执行，例如推分支、开 PR/MR（也可以在 config 里全局设置） |

## 一次 tick 做什么

1. **巡检**：正在跑的卡结束了吗？进程消失了吗？超时了吗？（只杀命令行里带本卡运行目录的进程）
2. **回收**：有 `BLOCKED.md` 就转 `blocked`；否则代为提交残留改动，再跑 `accept`
3. **反馈**：没过且还有次数，就回到 `queued`，把门禁输出塞进下一轮 prompt（可以换 runtime）；次数用完转 `failed`
4. **派发**：按各 runtime 的并发上限补空位，遵守优先级和依赖；每张卡一个独立 worktree 和分支 `fleet/<id>`
5. **汇报**：重写 `STATUS.md`，追加 `events.jsonl`，状态进入 done/review/blocked/failed 时调用 `notify`

状态流转：`queued → running → done | review | blocked | failed`；门禁失败且还有次数时回到 `queued`。

## 接入 Codex 或其它 agent

在 `config.json` 的 `runtimes` 里加一项 argv 模板（prompt 从 stdin 进来），再在 `caps` 里给它一个并发上限。可用占位符：`{model}` `{worktree}` `{gitdir}` `{run}`。
默认已配好 `claude`（`claude -p`）和 `codex`（`codex exec`，显式传 CPA 参数，默认 tier，`--add-dir` 放行主仓库 `.git` 以便提交）。
不同 runtime 各自消耗各自的额度，所以混用本身就能扩容。

## 定期巡检（三选一）

- 前台常驻：在 tmux 里跑 `python3 fleet.py --home ~/fleet watch 300`
- 开机自启：launchd，把下面内容存成 `~/Library/LaunchAgents/local.fleet.tick.plist`，然后执行 `launchctl load` 加载。
  `CPA_API_KEY` 这类密钥不要写进 plist，改用一个 wrapper 脚本从你的 shell 配置里加载。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.fleet.tick</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/python3</string><string>/Users/a/buy_compute/fleet.py</string>
    <string>--home</string><string>/Users/a/fleet</string><string>tick</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/a/.local/bin</string>
  </dict>
  <key>StandardErrorPath</key><string>/Users/a/fleet/tick.err</string>
</dict></plist>
```

- 在 Claude 会话里：`/loop 10m python3 fleet.py --home ~/fleet tick`（跟会话同生命周期，7 天后过期）。好处是这个会话还能顺便读 `STATUS.md`、拆新卡、处理 blocked。

## 人工操作

```bash
python3 fleet.py --home ~/fleet requeue <id> --note "说明"   # 次数清零重派，note 会进入下一轮 prompt
python3 fleet.py --home ~/fleet stop <id>                    # 杀掉这张卡自己的进程组，标记 failed
```

## 已知限制

- 单机运行；隔离靠 worktree 而不是容器，agent 仍能访问本机网络和文件
- 自动提交用 `git add -A`，目标仓库必须有 `.gitignore`（沙盒测试里 `__pycache__` 就被提交进去了）
- 不做合并：`on_pass` 负责推分支、开 PR，合并交给 CI 和分支保护
- 不感知额度：并发上限是静态配置
- 卡片里的 `accept` / `on_pass` 会在本机执行，只从可信来源收卡
- worker 会加载你的全局 `~/.claude/CLAUDE.md`；合同里写了"不许写仓库外路径"，但更稳妥的做法是给 worker 单独一套配置
