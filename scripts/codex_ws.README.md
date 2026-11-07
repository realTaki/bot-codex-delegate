# codex_ws.py

A small driver that lets a bot or orchestrator use [Codex](https://github.com/openai/codex) as its coding agent.
It talks to a running `codex app-server` over its WebSocket JSON-RPC 2.0 transport, so you can start threads,
run turns, steer or interrupt them, and list or read threads from a script or from another program.

It's one Python file using only the standard library (Python 3.8+, no `pip install`). You can use it as a CLI
or import it as a library.

## Prerequisites

- Codex CLI is installed and logged in (`codex login`).
- A `codex app-server` is running with a WebSocket listener the driver can reach, for example:

  ```sh
  codex app-server --listen ws://127.0.0.1:4500
  ```

  Bind to `127.0.0.1` only. The app-server runs commands on your machine, so don't expose it to a network.

## Endpoint and configuration

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Host | `--host` | `CODEX_WS_HOST` | `127.0.0.1` |
| Port | `--port` | `CODEX_WS_PORT` | `4500` |
| Turn idle timeout (seconds) | `--turn-timeout` | `CODEX_WS_TURN_TIMEOUT` | `900` |
| Model | `start --model` | `CODEX_WS_MODEL` | server default (not sent) |
| Thread registry file | `--state` | `CODEX_WS_STATE` | off |

Global flags (`--host`, `--port`, `--turn-timeout`, `--state`, `-v`, `--approve`) go **before** the subcommand.
Put subcommand options before its positional arguments. For `codex-turn`, put every option before
the contiguous `THREAD "PROMPT"` pair (or the single prompt with `--new`); interleaving options between
thread and prompt can fail with its argparse positional group.
Events stream to stderr; JSON results go to stdout.

## Subcommands

### `start`: create a thread, optionally run a first turn

```sh
python3 codex_ws.py start --cwd /absolute/path/to/my-repo
python3 codex_ws.py start --cwd ./my-repo --model gpt-5 --effort low "add a unit test for parse()"
python3 codex_ws.py start --cwd ./my-repo --intent read-only "audit parse(); change nothing"
```

Prints `{"threadId", "model", "cwd", "approvalPolicy", "sandbox"}`. If you pass a prompt, it then streams that
turn the same way `turn` does.
Options: `--intent coding|read-only`, `--sandbox read-only|workspace-write` (compatibility alias), `--no-network`,
`--approval untrusted|on-request|never`, `--effort`, `--label`.
New threads default to coding. Coding requires `on-request`; conflicting intent/sandbox flags fail.
`--sandbox read-only` still selects read-only intent. Relative explicit cwd paths are canonicalized;
the directory must exist. Coding cannot use broad roots such as `/`, `/workspace`, or the user's home.

### `turn`: run a turn on an existing thread and wait for it to finish

```sh
python3 codex_ws.py turn --intent coding --cwd ./my-repo --effort medium <threadId> "now run the tests and fix failures"
python3 codex_ws.py turn --intent coding --cwd ./my-repo --steer "keep the public API unchanged" <threadId> "refactor X"
python3 codex_ws.py turn --intent read-only <threadId> "audit only; change nothing"
```

Calls `thread/resume` first, then `turn/start`, and streams until `turn/completed`. It prints the
`turnId` right away and ends with `{"turnId", "status", "error", "durationMs", "finalMessage"}`. The exit code
is 0 when the status is `completed` or `interrupted`. `--steer` sends its text once the first command starts.

The wait uses an **idle** timeout, not a total wall-clock cap: `--turn-timeout SEC` (env `CODEX_WS_TURN_TIMEOUT`,
default 900) gives up only after SEC seconds with no new events from the server. Long turns are never killed
while Codex is still streaming. It is a global option, so put it before the subcommand:
`python3 codex_ws.py --turn-timeout 1800 turn --intent coding --cwd ./my-repo <threadId> "..."`.

### Scope preflight and migration (stable 0.166.0 protocol)

Both `turn` and `codex-turn` require **explicit `--intent coding --cwd PROJECT`** for resumed coding.
They send `cwd`, `sandbox: "workspace-write"`, and `approvalPolicy: "on-request"` on `thread/resume`,
and clear configured additional writable roots. Intent is a caller declaration of authorization;
the driver does not infer it from prompt wording, thread names, the process cwd, or a saved registry.
`--cwd` alone on resume is rejected; combine it with explicit coding or read-only intent.

Omitting intent on resume sends no permission overrides and permits only a confirmed read-only thread.
`--intent read-only` explicitly requests read-only, with an optional cwd. An audit is never silently
converted to coding. A caller may explicitly authorize coding in a historical read-only thread for
**future new work** using both coding intent and cwd. Only the newly supplied prompt is submitted;
prior rejected actions are never replayed or retried. Historical instructions may still say read-only:
state the newly authorized task clearly, or use a fresh coding thread if that history conflicts.

Before submitting input, the driver checks the **top-level effective** `cwd`, structured `sandbox`,
and `approvalPolicy` returned by `thread/start` or `thread/resume`. `thread.cwd` is historical metadata
and is not a substitute for effective cwd. Workspace policy uses `type: "workspaceWrite"` and
`writableRoots` (camelCase); thread requests use `sandbox: "workspace-write"` (a mode string).
In this stable policy, cwd is implicitly writable and `writableRoots` lists additional roots with a
schema default of `[]`. Every additional root must resolve within the authorized project. Standard
temporary directories remain governed by the returned `excludeSlashTmp`/`excludeTmpdirEnvVar` flags.
The driver never adds sibling projects or guesses broad writable roots. Network policy from a resume
response is pinned; new coding threads retain network-on by default and `--no-network` is checked.

The verified `cwd`, `sandboxPolicy`, and `approvalPolicy` are pinned on `turn/start`. Verification is
consumed once: each later library turn needs a fresh start/resume on that connection. Missing,
unsupported, malformed, or conflicting effective state fails with `ScopeError` (CLI exit 2) before
`turn/start`. No fallback to experimental permission profiles, full access, or automatic approval occurs.
Unknown status or an active thread also fails. Explicit scope changes first read thread status and
require idle/not-loaded state; they never steer a currently running or approval-blocked historical turn.
`steer` and `interrupt` retain their existing control behavior without permission overrides.

This is validation of server-reported policy, not a real filesystem-write probe or proof that every
project path is writable. OS permissions, managed policies and protected metadata paths can still
deny writes. No schema downgrade or retry through another entry point is attempted. A rejected or
unsupported resume may have changed server thread defaults before validation fails; no turn is sent,
and the driver does not attempt a rollback. Check the reported thread before continuing. Keep one
coordinator per thread/worktree: this protocol provides no atomic guard against another client
changing or starting the same thread between preflight and turn submission.

The installed stable schema files used for this change were `v2/ThreadStartParams.json`,
`ThreadResumeParams.json`, `ThreadStartResponse.json`, `ThreadResumeResponse.json`, and
`TurnStartParams.json` under `schema-0.166.0`. Other servers must return that scope contract;
unknown representations fail closed. Granular approval objects and external sandboxes are unsupported
by these scoped methods. No experimental API capability is enabled by these CLIs.

The long-wait helper uses the same preflight:

```sh
scripts/codex-turn --new --cwd /absolute/path/to/my-repo "implement X"
scripts/codex-turn --intent coding --cwd /absolute/path/to/my-repo <threadId> "next authorized task"
scripts/codex-turn --intent read-only <threadId> "audit only"
```

Migration limits: bare resume commands for coding must add intent and cwd. Library callers must
also use `thread_resume(tid, intent="coding", cwd=...)` before each later coding turn. Scoped methods
reject permission/config overrides through `**extra`; use their named arguments. The former
`start --danger-full-access` option is retained only to produce a migration error; full access and
coding with `--approval never`/`untrusted` are no longer supported by this scoped driver. Existing
explicit `--approve` choices remain compatible, with **decline still the default**; neither CLI
selects accept automatically. Installer/server/global configuration is unchanged by this fix.

### Session-helper compatibility

Repository `scripts/codex-sessions` is a standalone protocol client; it does not import `codex_ws.py`
and needs no intent/cwd migration for its list/name/compact/archive commands. Use its explicit
repository path rather than assuming a PATH-installed helper is identical. On the maintenance host,
`/home/box/.local/bin/codex-sessions` is a separate older helper importing
`/workspace/dev-files/codex-driver`, as identified by the caller. This patch does not migrate that
helper or external installed bot skills; their separate behavior is outside this compatibility check.

### `steer`: add input to an active turn (from another process)

```sh
python3 codex_ws.py steer <threadId> <turnId> "also update the changelog"
```

### `interrupt`: stop an active turn

```sh
python3 codex_ws.py interrupt <threadId> <turnId>
```

Note: commands that are already queued can still run after `turn/completed` reports `interrupted`. If side
effects matter, check the workspace yourself.

### `list` and `read`

```sh
python3 codex_ws.py list --cwd ./my-repo --limit 10     # thread/list
python3 codex_ws.py read --turns <threadId>              # thread/read (with turns and items)
```

### `saved`: show the optional local registry

```sh
python3 codex_ws.py --state ./threads.json start --cwd ./my-repo --label mybot-myproject-bugfix
python3 codex_ws.py --state ./threads.json saved
```

The registry is off unless you set `--state` or `CODEX_WS_STATE`. When it's on, it's a plain JSON list:

```json
[{"id": "<threadId>", "label": "mybot-myproject-bugfix", "cwd": "/path/to/my-repo",
  "model": "<model>", "created": "2026-01-01T12:00:00+0000"}]
```

The label is free text. One naming scheme you could use is `<bot>-<project>-<task-type>`.

## Library use

```python
from codex_ws import Codex
c = Codex(host="127.0.0.1", port=4500, on_event=print)
c.connect()                                        # initialize + initialized
tid  = c.thread_start("/abs/path/to/repo")["thread"]["id"]
turn = c.turn_start(tid, "do X", effort="low")["turn"]
c.turn_steer(tid, turn["id"], "also Y")
done = c.wait_turn(tid, turn["id"])                # params of turn/completed
c.thread_resume(tid, intent="coding", cwd="/abs/path/to/repo", excludeTurns=True)
next_turn = c.turn_start(tid, "new authorized task")["turn"]
c.call("thread/loaded/list", {})                   # any raw JSON-RPC method
```

The client is single-threaded. Notifications that arrive while it waits for a response are buffered,
server pings get an automatic pong, and approval requests get the `approve=` answer (default `decline`).
Driver approval replies are logged with `source=driver`, configured policy, request ID, method and
decision. A driver decline is not evidence that a human rejected an action. Server notifications and
errors retain their original content, including any server wording such as `rejected by user`.
Raw `call()` is a low-level API and does not provide the scoped methods' preflight guarantees.

Methods used: `initialize`, `initialized`, `thread/start`, `thread/resume`, `thread/list`, `thread/read`,
`turn/start`, `turn/steer`, `turn/interrupt`. Inspect the installed stable schema for your CLI version
when diagnosing an unsupported response; do not enable experimental fields as a fallback.

## Security

**New coding default: explicit project cwd, `workspace-write`, network on, `on-request`, approvals declined.**
Codex can read files and write inside the thread's `cwd` (plus the temp dirs the sandbox allows), and it can
reach the network. Any request to step outside the sandbox gets declined unless you pass `--approve accept`
or `--approve acceptForSession`. Use `--no-network` to turn network off, or `--sandbox read-only` to block writes.

The scoped driver rejects full-access policies, extra roots outside the project, and coding approval
policies other than `on-request`. It makes no server-wide or global permission changes.

Other notes:
- Keep the app-server on loopback. The WebSocket has no authentication of its own, so anyone who can reach
  the port can run commands as that user.
- Point threads at the repo you intend to change, not at your home directory.

## Regression tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

Run from the repository root. Tests use an in-memory JSON-RPC peer, never a live server or coding
subagent. An installed-schema contract check runs when the local 0.166.0 schema is available and is
skipped elsewhere. A real integration smoke test is separate and remains the caller's responsibility.

## License

MIT
