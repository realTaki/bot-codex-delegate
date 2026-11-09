# bot-codex-delegate

> **This repository is no longer maintained and is archived.** The author moved coding work to an Oracle cloud VM for a more stable machine that does not freeze the way the previous cloud computer did. The docs and scripts here are left as-is for reference; they will not receive updates.

**Let your [Grok Bot](https://x.ai/bot) run Codex the way a Cursor Project runs cloud agents: hand off the work, get woken when it's done.**

The bot is the coordinator. A resident `codex app-server` on the bot's cloud computer is its worker. A low-effort waiter keeps the computer awake while Codex works. Codex spends the quota, not the bot.

[中文说明](#中文说明)

---

## The problem

As [@0xmomonifty](https://x.com/0xmomonifty/status/2115269648698716426) put it: when Grok Bot drives Codex, it babysits it.

- **Babysitting burns bot quota.** The bot checks progress round after round. Its own quota goes to keeping Codex company.
- **Letting go stalls Codex.** If the bot ends its conversation, the cloud computer goes to sleep when no bot turn is active, and Codex freezes with it.
- **Background commands don't help.** A `block_until_ms: 0` Shell command isn't bot activity, so the computer can still fall asleep under it and freeze Codex.
- **Routines don't help.** A progress-check routine is a fresh bot every time, re-reading the context, and it doesn't fire on time. The computer sleeps in the gaps.

## The fix

Treat Codex like a cloud agent inside a project.

1. **A resident server.** `codex app-server` on `ws://127.0.0.1:4500` (WebSocket JSON-RPC, localhost only), kept up by tmux and a watchdog, with `/readyz` and `/healthz`. Same login and session history as the Codex CLI.
2. **Dispatch to a waiter, then get woken.** Same trick as [bot-keepalive](https://github.com/realTaki/bot-keepalive): a bot turn waiting on a foreground tool call counts as active, and one Shell call can block for 13 minutes. The main bot hands each Codex turn to one low-effort waiter subagent. The waiter runs the blocking turn command (`scripts/codex-turn`) in a foreground Shell call, keeps waiting on it one call per ~13 minutes until Codex finishes or a deadline passes, then returns a short summary. The main chat stays free and wakes once, when the waiter is done.
3. **Steer and interrupt.** `turn/steer` adds instructions to a running turn; `turn/interrupt` stops it and the thread stays usable. Interrupt isn't a hard stop: commands already scheduled can still land, so check the working tree after.
4. **Reuse sessions.** One named session per project × task type (`<bot>-<project>-<task type>`), found with `thread/list`, picked up with `thread/resume`, compacted when it gets long, archived when done. Never two writers in one working tree.
5. **Many jobs at once.** One waiter per running turn, each parallel turn in its own clone. Or, if a bot-keepalive watcher is running, launch the turns detached and register their PIDs with it instead.

| | Babysitting | Progress routine | bot-codex-delegate |
|---|---|---|---|
| Computer stays awake while Codex works | Only while the bot is checking | No, sleeps between runs | Yes, the waiter is always in a foreground wait |
| Main chat | Busy watching | Free | Free |
| Bot quota while Codex works | A full model turn every check | A new bot, re-reading context, each run | One low-effort tool call per ~13 min |
| Knows when Codex is done | Only when it checks | Only when the routine fires | When the command returns |
| Steer or stop mid-turn | Awkward | No | `turn/steer`, `turn/interrupt` |
| Codex context across tasks | Depends | Depends | One named session per task type |

Cost has the same shape as bot-keepalive, whose author measured about 1.8M tokens a day for a watcher running all day. A short Codex turn costs the waiter one or two tool calls.

## Safe defaults

The installer starts the server with sandbox `workspace-write` **with network access** and approval policy `on-request`. Codex writes inside the project folder and asks before stepping outside it. The model comes from your own Codex config.

Full access is **opt-in only**: `CODEX_DELEGATE_FULL_ACCESS=1` switches the server to `danger-full-access` with approvals `never`. Then Codex can read and change any file your user can, including SSH keys, API tokens and other projects on the machine. Only turn it on for a throwaway computer and work you trust.

The scoped driver defaults new coding threads to workspace-write/on-request in an explicit project cwd, and it **declines** approval requests unless you pass `--approve accept` (or `acceptForSession`). Coding resumes now require `--intent coding --cwd PROJECT`; effective scope is verified before a turn. `--intent read-only` (or new-thread `--sandbox read-only`) preserves audit intent; `--no-network` tightens new coding threads. The driver rejects full access and outside-project writable roots. See the [scope and migration details](scripts/codex_ws.README.md#scope-preflight-and-migration-stable-01660-protocol); installer configuration is unchanged.

The endpoint, state dir, log path, tmux session name and model are all env vars; see the top of [`install-codex-app-server.sh`](scripts/install-codex-app-server.sh). The server only listens on loopback because it has no auth.

## Install

Send your Grok Bot this link and ask it to save it as a skill:

```
https://github.com/realTaki/bot-codex-delegate/blob/main/skills/bot-codex-delegate/SKILL.md
```

The skill tells the bot to set up the server on its computer (Codex CLI must be installed and logged in):

```bash
git clone https://github.com/realTaki/bot-codex-delegate
bash bot-codex-delegate/scripts/install-codex-app-server.sh   # idempotent
codex-app-server-ctl status
codex-sessions                                                # named Codex sessions
```

What's in here:

- [`skills/bot-codex-delegate/SKILL.md`](skills/bot-codex-delegate/SKILL.md): the workflow and rules for the bot.
- [`scripts/install-codex-app-server.sh`](scripts/install-codex-app-server.sh): app-server in tmux + watchdog + `codex-app-server-ctl status|start|stop|restart|schema`, logs in `~/logs/`.
- [`scripts/codex-sessions`](scripts/codex-sessions): list, find, name, compact and archive sessions (Python stdlib only).
- [`scripts/codex-turn`](scripts/codex-turn): run one turn and block until it really finishes (up to `--max-hours`), then print a JSON summary with status, last agent message and changed files. This is what the waiter runs. `codex_ws.py turn` gives up after 15 minutes with no new event (configurable with `--turn-timeout`), so use this for long turns that may go quiet, or to cap a turn by wall-clock time.
- [`scripts/codex_ws.py`](scripts/codex_ws.py): stdlib-only driver for start / turn / steer / interrupt / list / read ([docs](scripts/codex_ws.README.md)).

```bash
python3 scripts/codex_ws.py start --cwd ./my-repo "first task"      # prints threadId
python3 scripts/codex_ws.py turn --intent coding --cwd ./my-repo --effort medium <threadId> "next task"
python3 scripts/codex_ws.py turn --intent read-only <threadId> "audit only; change nothing"
python3 scripts/codex_ws.py steer <threadId> <turnId> "also update the changelog"
python3 scripts/codex_ws.py interrupt <threadId> <turnId>
python3 scripts/codex_ws.py list --cwd ./my-repo
```

Driver settings: `--host`/`--port` (`CODEX_WS_HOST`/`CODEX_WS_PORT`, default `127.0.0.1:4500`), `start --model` (`CODEX_WS_MODEL`, default: the server's), `--state FILE` (`CODEX_WS_STATE`, off by default). Global flags go before the subcommand.

---

## 中文说明

> **本仓库已停止维护并已 archive。** 作者已把相关工作迁到一台 Oracle 云主机上，以获得更稳定、不会像原先那台云电脑那样挂掉的环境。这里的文档和脚本仅供参考，不再更新。

**让 Grok Bot 像 Cursor Project 用云端 agent 一样用 Codex：派活出去，干完了叫醒它。** Bot 当协调者，云电脑上常驻的 `codex app-server` 当干活的 agent，一个 low effort 的等待 subagent 让电脑在 Codex 干活时保持清醒。额度花在 Codex 干活上，不花在 bot 盯着上。

**问题。** 正如 [@0xmomonifty](https://x.com/0xmomonifty/status/2115269648698716426) 说的：Grok Bot 驱动 Codex 时一直在“陪着”它。一轮一轮查进度，bot 自己的额度全花在陪 Codex 上；可要是 bot 撒手结束对话，没有 bot turn 在跑，云电脑就休眠，Codex 也跟着卡住。用 `block_until_ms: 0` 的后台命令也不行：后台命令不算 bot 活跃，电脑照样会休眠，把 Codex 冻住。设个查进度的 Routine 也不行：每次都是新 bot，从头读上下文，触发不准时，间隙里电脑还是会睡。

**解法。**
1. **常驻服务。** `codex app-server` 跑在 `ws://127.0.0.1:4500`（WebSocket JSON-RPC，只监听本机），tmux + watchdog 保活，健康检查 `/readyz`、`/healthz`。和 Codex CLI 共用登录和会话记录。
2. **派给等待 subagent，完成后叫醒。** 和 [bot-keepalive](https://github.com/realTaki/bot-keepalive) 同一个思路：等待前台 tool call 返回的 turn 算活跃，单次 Shell 调用最长能阻塞 13 分钟。主 bot 把每个 Codex turn 交给一个 low effort 的等待 subagent，它在前台 Shell（`block_until_ms: 760000`）里跑会阻塞到 turn 结束的 `scripts/codex-turn`；13 分钟没返回，就对同一条命令接着前台等（AwaitShell），直到 Codex 跑完或到截止时间，再把 thread id、状态、最后一条回复、改动文件简短汇报给主 bot。主对话一直空闲，只在等待者结束时被叫醒一次。
3. **中途转向、打断。** `turn/steer` 给正在跑的 turn 补指令；`turn/interrupt` 打断，会话还能接着用。注意打断不是硬停止：已经排上的命令可能还会执行，打断后先看一下工作区。
4. **复用会话。** 每个项目 × 任务类型一个命名会话（`<bot>-<项目>-<任务类型>`），用 `thread/list` 找、`thread/resume` 接上、太长就压缩、做完就归档。同一个工作区永远只能有一个写入者。
5. **多个任务并行。** 每个正在跑的 turn 配一个等待者，并行的 turn 各用各的 clone。任务很多时，如果 bot-keepalive 的全局守护已经在跑，也可以把 turn 脱离会话启动，把进程 PID 登记给它。

**成本。** 每约 13 分钟一次 low effort tool call，和 bot-keepalive 一样（作者实测全天守护一天约 180 万 Token）。短的 Codex turn 只要一两次调用。

**安全默认值。** 默认沙箱 `workspace-write` 且允许联网，审批策略 `on-request`：Codex 在项目目录里写文件，越界之前先问。模型用你自己 Codex 配置里的。完全权限只能显式开启：`CODEX_DELEGATE_FULL_ACCESS=1` 会改成 `danger-full-access` + 审批 `never`，这时 Codex 能读写你这个用户能碰到的任何文件，包括别处的 SSH 密钥、API token 和其他项目。只在一次性的机器、可信的任务上用。地址、目录、日志、tmux 会话名、模型都能用环境变量改，服务只监听本机（它没有鉴权）。

**安装。** 把上面那个 `SKILL.md` 链接发给你的 Grok Bot，让它存成技能。技能会让 bot 在自己电脑上运行 `scripts/install-codex-app-server.sh`（可重复运行），需要先装好并登录 Codex CLI。等待者用的是 `scripts/codex-turn`（`codex_ws.py turn` 连续 15 分钟没有新事件就会放弃，可用 `--turn-timeout` 调；长任务用这个）。驱动脚本 `scripts/codex_ws.py`（只用标准库）负责 start / turn / steer / interrupt / list / read，地址用 `--host`/`--port` 或 `CODEX_WS_HOST`/`CODEX_WS_PORT`，模型用 `start --model` 或 `CODEX_WS_MODEL`（默认用服务端的）。驱动默认拒绝审批请求，`--approve accept` 才放行。恢复编码任务必须明确传入 `--intent coding --cwd 项目目录`，发送新 turn 前校验实际范围；只读审查用 `--intent read-only`，不会自动升级。驱动现在拒绝完全权限及项目外写入根目录；安装器和全局配置保持不变，迁移限制见驱动文档。

## License

MIT
