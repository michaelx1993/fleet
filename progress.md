## 2026-09-24 - Task: 诊断「人工能操作的 agent 数量有限」并给出扩容方案

### What was done
- 用本机 Claude Code 数据量化瓶颈：agent 每次被触碰后只自主跑 p50 1.1min，同时干活的主会话 p50 2 / max 6，约 21 agent-小时/天，按 API 标价折合约 $12.5/agent-小时。结论：天花板是人的注意力，杠杆是单次派单的自主时长。
- 新增 `fleet_metrics.py`（只读度量脚本，每周复测用）和 `task-card-template.md`（一张卡 = 一个 agent 跑 1–3 小时的自主契约）。
- 核对 Claude Code 2.1.281 的后台 / 云端 / workflow 能力，并对 `claude --bg` 做了冒烟测试。

### Testing
- `python3 fleet_metrics.py --days 7`：输出与手工分析一致（21.4 agent-h/天、p50 1.1min、并发 p50 2 / p90 4 / max 6、$12.52/agent-h）；`--days 0` 以退出码 1 拒绝。
- `claude --bg` 冒烟：在未信任的 /Users/a/buy_compute 被拒（"Workspace not trusted"）；在 /Users/a/demo3 里会话 9941f637 约 30s 返回 BG_OK，状态 done。
- 发现非 TTY 下 `claude logs <id>` 会新起一个 prompt 为 "logs" 的会话（实测 2 次），不会打印日志。

### Notes
- 改动文件（新建）：`fleet_metrics.py`、`task-card-template.md`、`progress.md`。Obsidian：新建 `工作流/agent-fleet扩容-从逐轮驾驶到派单验收.md`，`CLAUDE.md` 加 1 行索引，`TODO.md` 加 1 节。
- 副作用：后台会话 `bg-smoke-test`（9941f637，/Users/a/demo3，done）仍留在 `claude agents` 列表里；另有 2 个误触发的一次性 "logs" 会话（只读回答，没有改文件）。
- 回滚：删除上面 3 个新建文件和 Obsidian 新笔记；删掉 Obsidian `CLAUDE.md` 里 agent-fleet 那行索引和 `TODO.md` 里「agent fleet 扩容」一节。本目录不是 git 仓库，所以没有 commit。

## 2026-09-24 - Task: 回答「有没有调度框架 / 任务卡怎么回收 / 怎么派给 Codex / 怎么定期巡检再派发」，交付最小闭环

### What was done
- 新增 `fleet.py`（约 570 行，纯标准库）：卡片即队列；每次 tick 依次做巡检、回收（先看 BLOCKED.md，再代提交残留改动，最后跑验收门禁）、反馈（门禁输出注入下一轮 prompt，可切换备用 runtime）、按 runtime 并发上限派发（遵守依赖和优先级，每卡一个 worktree）、看板、事件日志、通知，以及可选的 `on_pass`（推分支 / 开 PR）。同时支持 Claude（`claude -p`）和 Codex（`codex exec` + 显式 CPA 参数）。
- 新增 `README.md`（用法、launchd 巡检示例、已知限制）。
- 调研：原生能力（agent teams、agent view、routines、自托管 runner、GitLab CI）；第三方框架现状（2026-09）；业界一手案例（Anthropic、Cursor、OpenAI、Stripe、Spotify、Airbnb、Gas Town、Shopify）。

### Testing
- 沙盒 `/private/tmp/fleet-demo`（repo + bare remote + home），共 9 张卡：
  - c1 Claude(haiku) done；c3 依赖 c1 并叠在 fleet/c1-mul 上，done；c2 Codex 在修正 CPA 地址并 requeue 后 done。
  - c4 桩 agent 第 1 次故意做错，第 2 次看到反馈、并切到 fallback runtime 后 done。
  - c6 两次超时被强杀后 failed，没有残留进程；c7 缺 accept，派发时被拒。
  - c5 haiku 伪造了 deploy.sh 并自称验收通过，门禁在干净环境里判失败，没有假绿。
  - c8 on_pass 把分支推到 bare remote，done；c9 on_pass 失败，转 review。
  - 两个 tick 并发时一个跳过，锁正常释放；非法 --home 以退出码 1 退出。
- Codex 进程按自身 PID 核验：`model_provider="cpa"`、`gpt-5.6-sol`、`service_tier="default"`、`workspace-write`、`--add-dir <repo>/.git`。
- 测试中修复的 bug：requeue 后尝试次数清零，运行目录被复用，残留的 exit_code 会导致下一次巡检提前回收。现在改为每次派发都新建运行目录。

### Notes
- 改动文件：新建 `fleet.py`、`README.md`，追加 `progress.md`。Obsidian：`工作流/codex-启动参数硬规则.md` 加一条订正（CPA 地址现为 127.0.0.1:8317，旧地址 401）、`工作流/agent-fleet扩容-从逐轮驾驶到派单验收.md` 追加章节、`TODO.md` 更新。
- 沙盒保留在 `/private/tmp/fleet-demo` 供查看（重启后自动消失）；手动清理：`rm -rf /private/tmp/fleet-demo`。
- 回滚：删除 `fleet.py`、`README.md`；Obsidian 两处按日期段落删除。

## 2026-09-24 - Task: 在 GitHub 新建 fleet 仓库并推送

### What was done
- 本目录初始化为 git 仓库（main），新增 `.gitignore`（忽略 `__pycache__`、默认运行目录 `/fleet/`），README 标题改为和仓库名一致。
- 用 `gh repo create` 建了**私有**仓库 https://github.com/michaelx1993/fleet 并推送。

### Testing
- `gh repo view`：visibility=PRIVATE，默认分支 main；`git ls-remote` 得到的远端 main 等于本地 HEAD（dfd9fec）；远端文件列表与本地提交一致（6 个文件）。
- 推送前扫描过密钥：没有 token 或私钥形态的字符串，`CPA_API_KEY` 的值不在任何文件里。

### Notes
- 需要公开时执行：`gh repo edit michaelx1993/fleet --visibility public --accept-visibility-change-consequences`。公开前注意：fleet.py 的默认配置里有 CPA provider 配置（本机地址，不含密钥），progress.md 里有内部路径。
- 回滚：`gh repo delete michaelx1993/fleet`（需要 delete_repo 权限）；本地删除 `.git`。
