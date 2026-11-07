---
name: bot-codex-delegate
description: Use this when a Grok Bot hands coding or other long work to a local Codex CLI on its cloud computer. The bot acts as the coordinator and a resident `codex app-server` is its worker: hand each turn to one low-effort waiter subagent that blocks in foreground Shell calls (so the computer stays awake), get woken when it completes, steer or interrupt mid-turn, and reuse one named Codex session per project and task type.
---

# Delegate work to a resident Codex app-server

## Why
Driving Codex turn by turn and checking on it every round spends the bot's own quota just
watching. Ending the conversation instead lets the cloud computer sleep, and Codex freezes with
it. A background Shell command (`block_until_ms: 0`) doesn't help: it is not bot activity, so
the computer can still sleep while it runs. A progress-check routine starts a fresh bot each time,
which re-reads the context, fires at imprecise times, and leaves gaps where the computer sleeps.

What works (the [bot-keepalive](https://github.com/realTaki/bot-keepalive) idea): a bot turn
that is **waiting on a foreground tool call counts as active**, and one Shell call can block for
up to 13 minutes. So the main bot hands each Codex turn to **one low-effort waiter subagent**.
The waiter runs the blocking turn command in a foreground Shell call and keeps waiting on it,
one tool call per ~13 minutes, until Codex finishes. The computer stays awake, the main chat
stays free, and the main bot wakes once, when the waiter reports back. The quota goes to Codex
doing the work, plus one cheap tool call per ~13 minutes.

## 1. Setup (once per computer)
Codex CLI must be installed and logged in. Then:

```bash
git clone https://github.com/realTaki/bot-codex-delegate
bash bot-codex-delegate/scripts/install-codex-app-server.sh
codex-app-server-ctl status
```

The installer is idempotent. It runs `codex app-server --listen ws://127.0.0.1:4500` in a tmux
session, adds a watchdog that recreates the session or kills a hung server, a control script
(`codex-app-server-ctl status|start|stop|restart|schema`), logs under `~/logs/`, and the
`codex-sessions` helper. Settings come from env vars (see the script header) and are saved to
`~/.codex-delegate/env.sh`; `CODEX_APP_SERVER_URL` changes the listen endpoint (loopback only, since
the server has no auth). It leaves `CODEX_HOME` alone, so the server shares the CLI's login,
config and session history.

Defaults: sandbox `workspace-write` with network access, approval policy `on-request`, model
from Codex's own config. Full access (`danger-full-access` + approvals `never`) is opt-in only
via `CODEX_DELEGATE_FULL_ACCESS=1`. Never turn it on by yourself: with it, Codex can read and
change any file the user can, including keys and tokens outside the project. Ask the user first.

After a Codex upgrade, run `codex-app-server-ctl schema [DIR]` to regenerate the JSON schemas
and check method names there, then `codex-app-server-ctl restart`.

Run **one** server per computer. All bots on the computer share it. Check
`codex-app-server-ctl status` before installing or starting anything.

## 2. Protocol in short
WebSocket, JSON-RPC 2.0, one JSON message per text frame. Health: `/readyz`, `/healthz`.

- `initialize {clientInfo}` → notification `initialized`. Pass
  `capabilities.experimentalApi: true` only if you need experimental methods.
- `thread/start {cwd, sandbox: "workspace-write", approvalPolicy: "on-request"}` for coding.
  Always pass the project's `cwd`; never rely on the server's own directory. Save `thread.id`.
- `turn/start {threadId, input: [{type: "text", text}], effort?}` → stream events → wait for
  `turn/completed`. Effort and model can be set per turn.
- On an existing thread, before a new coding turn resume with explicit project `cwd`, `sandbox: "workspace-write"`, and
  `approvalPolicy: "on-request"`. Validate the effective response before `turn/start`; the scoped
  drivers below do this. Deliberate read-only tasks use read-only intent instead.
- With approval policy `on-request`, the server may send approval requests (commands, file
  changes) to the client. The driver must answer them (accept or decline) or the turn waits.

### Driver: `scripts/codex_ws.py`
One stdlib-only Python file ([docs](../../scripts/codex_ws.README.md)). Global flags go **before**
the subcommand; subcommand options precede positional arguments. Events stream to stderr, JSON results
to stdout. In these examples, `<repo>` is the explicit **absolute authorized project directory**,
including when the command runs after `cd` or from a separate job folder.
Use absolute checkout paths for `<path>` and `<delegate-checkout>` below as well.

```bash
D=bot-codex-delegate/scripts/codex_ws.py
python3 $D start --cwd <repo> [--model <model>] [--effort medium] ["<first task>"]   # prints threadId
python3 $D turn --intent coding --cwd <repo> [--effort medium] [--steer "<extra>"] [--interrupt-after SEC] <threadId> "<task>"
python3 $D turn --intent read-only --cwd <repo> <threadId> "<audit; change nothing>"
python3 $D steer <threadId> <turnId> "<extra>"      # from another process, mid-turn
python3 $D interrupt <threadId> <turnId>
python3 $D list [--cwd <repo>] [--limit 20]         # read-only
python3 $D read [--turns] <threadId>                # read-only
python3 $D --state threads.json saved               # optional local registry (start --label ...)
```

- Endpoint: `--host`/`--port` or `CODEX_WS_HOST`/`CODEX_WS_PORT` (default `127.0.0.1:4500`).
- Model: `start --model` or `CODEX_WS_MODEL`; unset means the server's default.
- Registry: `--state FILE` or `CODEX_WS_STATE`; off unless set.
- New coding defaults: explicit project cwd, `workspace-write` with network, `approvalPolicy: on-request`, and approval
  requests are **declined**. `--approve accept|acceptForSession` answers them yes; `--no-network`
  or new-thread `--sandbox read-only` tighten it.
- Coding resumes require **both `--intent coding --cwd <repo>`**; `cd` alone is insufficient.
  `--intent read-only` deliberately requests read-only. Omitted resume intent preserves only a
  confirmed read-only thread, and `--cwd` alone is rejected. Intent is never inferred from task text.
- Before a new turn, the driver validates effective cwd, sandbox, writable roots and approval policy.
  Unknown or conflicting state fails with `ScopeError`; do not retry through another entry point.
  Explicit coding intent plus cwd can authorize **future new work** in an idle historical read-only
  thread. Prior rejected actions are never replayed; historical instructions are not rewritten.
- The scoped driver rejects `--danger-full-access`, outside-project writable roots, and coding with
  `--approval never`/`untrusted`, even though the installer retains its separate legacy full-access option.
  Approval requests still default to decline; driver replies log `source=driver` without rewriting
  server rejection messages. See the [migration limits](../../scripts/codex_ws.README.md#scope-preflight-and-migration-stable-01660-protocol).
- `turn` resumes the thread first, prints the `turnId` right away, and exits 0 on `completed` or
  `interrupted`. It blocks until `turn/completed`, **but gives up with `TimeoutError` after
  `--turn-timeout` seconds (default 900, env `CODEX_WS_TURN_TIMEOUT`) with no new event** (same
  for `start ... "<prompt>"`; it's a global option, so put it before the subcommand). A turn that
  keeps streaming won't be cut off, but for long turns the waiter should still use `codex-turn`.

### Long turns: `scripts/codex-turn`
A small wrapper over `codex_ws.py` (imported as a library, same `--host`/`--port`, env vars and
safe defaults). It keeps one connection open and waits as long as the turn runs.

```bash
T=bot-codex-delegate/scripts/codex-turn
python3 $T --intent coding --cwd <repo> [--effort medium] [--max-hours 6] [--interrupt-at-deadline] <threadId> "<task>"
python3 $T --intent read-only --cwd <repo> <threadId> "<audit; change nothing>"
python3 $T --new --cwd <repo> [--model <model>] [--effort medium] "<first task>"
```

Keep all CLI options before the contiguous `<threadId> "<task>"` pair (or the single prompt with
`--new`). Do not interleave options between thread ID and prompt; this avoids argparse's positional
intermix limitation. The wrapper uses the same scope preflight as `codex_ws.py`.

stdout is JSON lines: `{"threadId","turnId"}` right away, then
`{"threadId","turnId","status","error","durationMs","finalMessage","changedFiles"}`
(`changedFiles` from `git status --porcelain` in the thread's `cwd`). Events go to stderr.
Exit 0 on `completed`/`interrupted`, 1 on `failed`, 2 on a scope, connection or RPC error (a
`{"status":"error"}` line, e.g. unknown thread or server down), 3 when `--max-hours` passed (the turn is left
running unless `--interrupt-at-deadline` was given). `--new` defaults to coding; add `--intent read-only`
for a new audit. A thread created with `codex_ws.py start` still needs explicit coding intent and cwd
when resumed through this wrapper.

## 3. Dispatch through a waiter subagent
The main bot never waits on Codex itself, and never uses a `block_until_ms: 0` background
command as its way of waiting (the computer can sleep under it and freeze Codex).

1. Find or create the session (section 5). Make sure no other writer is in that working tree.
2. Start **one** background executor subagent at **low effort** for this turn. Give it the
   exact command, the deadline (an absolute time or N hours), whether it may interrupt at the
   deadline, and these instructions:
   - Run the command in a **foreground** Shell call with `block_until_ms: 760000` (the per-call
     limit is 13 minutes), events to a log file:
     ```bash
     cd <repo> && python3 <path>/scripts/codex-turn --intent coding --cwd <repo> \
       --effort medium --max-hours <N> <threadId> "<task>" 2>> /tmp/<session-name>.events.log
     ```
     (For a new coding session, add `--new` and omit `<threadId>`. For a deliberate audit, choose
     `--intent read-only` instead of coding, whether new or resumed.)
   - If the call hasn't returned after ~13 minutes, it moves to the background and keeps running.
     Then wait on that same command with back-to-back `AwaitShell` calls (its shell id,
     `block_until_ms: 760000`) until it exits. No pauses and no commentary between waits.
     Never start the command a second time.
   - After the first wait, check once that the turn really started: a `{"threadId","turnId"}`
     line in the output, or `[turn/started]` in the log. If not, report the error and stop.
   - Stop waiting at the deadline. Interrupt only if told to (`--interrupt-at-deadline`, or
     `python3 <path>/scripts/codex_ws.py interrupt <threadId> <turnId>`). Otherwise leave the turn
     running and say so.
   - Only read; never edit files, commit, or start other work.
   - Finish with a short summary: thread id, turn id, status, the last agent message (trimmed),
     changed files, and the log path.
3. End the main turn. The main chat stays free; the main bot wakes when the waiter finishes.
4. Review the diff yourself, then report or send the next turn through a new waiter.

Keep your own queue for follow-ups and send the next turn after the previous one completes.

**Parallel turns.** One waiter per running Codex turn, and each parallel turn needs its own clone
and session. For many jobs at once, you can skip the per-turn waiters: if bot-keepalive's global
watcher is already running (or you start one), launch each turn detached and register its PID
with it, following bot-keepalive's steps:

```bash
J=~/.keepalive/codex/<session-name>; mkdir -p "$J" ~/.keepalive/jobs && cd "$J"
setsid nohup bash -c 'python3 <path>/scripts/codex-turn --intent coding --cwd <repo> <threadId> "<task>" \
  > out.json 2> events.log; touch DONE' < /dev/null > /dev/null 2>&1 &
echo "$! $J" > ~/.keepalive/jobs/<session-name>
```

The job folder sits outside the repo, so `DONE` never lands in the working tree. The watcher
keeps the computer awake until every job ends; read each `out.json` afterwards (the last line is
the summary).

**Cost.** One tool call per ~13 minutes at low effort, the same shape as bot-keepalive (its author
measured about 1.8M tokens a day for a watcher running all day). Short turns cost one or two calls.

## 4. Steer and interrupt
- **Steer** (add instructions to a running turn): `turn/steer {threadId, expectedTurnId, input}`,
  or `codex_ws.py steer <threadId> <turnId> "<text>"`. `expectedTurnId` must be the active turn.
- **Interrupt**: `turn/interrupt {threadId, turnId}` (`codex_ws.py interrupt <threadId> <turnId>`)
  → `turn/completed` with `status: "interrupted"`. The thread stays usable.
- **Interrupt is not a hard stop.** Commands already scheduled in the interrupted turn can keep
  running and land later. After an interrupt, check the working tree (`git status`, `git diff`)
  before sending more, and filter events by `turnId`. If side effects must stop for sure, abandon
  that thread and start a fresh one.

## 5. Reuse sessions
A session keeps the project context; a new one re-reads the whole project. Keep **one named
session per project × task type**, and send every new prompt of that type into it.

Session-helper examples here refer to the repository's standalone `scripts/codex-sessions`, which
does not import `codex_ws.py`. Use its explicit path to avoid a different helper on PATH:

```bash
S=<delegate-checkout>/scripts/codex-sessions
```

Installed helpers and installed bot skills are separate deployments; updating this checkout does
not migrate them. See the [helper compatibility note](../../scripts/codex_ws.README.md#session-helper-compatibility).

- **Name**: `<bot>-<project>-<task type>`, for example `mybot-website-frontend`. Bot name as it
  appears in the app, project = repo or folder name, task type = a short stable label. The name
  is the thread's Codex title, so other bots and the user see it.
- **Find**: `thread/list {searchTerm: "<bot>-<project>", sourceKinds: ["cli","vscode","exec","appServer"]}`
  (the default list leaves out `codex exec` sessions). Filter by folder with `cwd`.
  `python3 "$S" find <term>` does the same.
- **Found**: for coding, use `codex-turn --intent coding --cwd <repo> <threadId> "<task>"`;
  for a deliberate audit use `--intent read-only`. Resume scope must be verified before a new turn.
- **None fits**: `thread/start` with the right `cwd`, run the first turn, then
  `thread/name/set {threadId, name}` (`python3 "$S" name <id> "<name>"`). A new thread is not
  listed until after its first turn.
- **Long context** (slow turns, `contextWindowExceeded`): `thread/compact/start {threadId}`
  (`python3 "$S" compact <id>`). The session and its name stay.
- **Task type done**: `thread/archive {threadId}` (`python3 "$S" archive <id>`). `--archived`
  lists it, `unarchive` brings it back.
- `codex exec` sessions live in the same store; name them the same way and resume them with
  `codex exec resume <id>`.

## 6. Rules
- One server per computer. Don't start a second one; use `codex-app-server-ctl`.
- **Never two writers in one working tree.** Parallel work needs a separate clone and its own
  session.
- Tell Codex explicitly whether to commit or push, and to which branch.
- Dispatch every turn through one low-effort waiter subagent doing foreground waits (or a
  bot-keepalive registration). Never rely on a `block_until_ms: 0` background command, a
  progress-check routine, or a detached run nobody watches.
- One waiter per running turn. Never two waiters, or two writers, on one working tree.
- Verify the launch before telling anyone it is running.
- Keep scoped defaults: explicit project cwd and workspace-write/on-request for coding;
  deliberate audits stay read-only. Full access is unsupported by the scoped driver.
- Never paste secrets into prompts or print auth files.
- The cloud computer sleeps when no bot turn is active, and a sleeping computer freezes Codex
  too. The waiter exists to prevent that; don't end it early.
