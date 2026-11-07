"""Scope regression tests. In-memory JSON-RPC peer only; no socket or Codex turn."""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

import codex_ws


PROJECT = str(Path(__file__).resolve().parent.parent)
THREAD = "fake-thread"
TURN = "fake-turn"


def effective(kind="workspaceWrite", **changes):
    sandbox = {"type": kind, "networkAccess": True}
    if kind == "workspaceWrite":
        sandbox["writableRoots"] = []
    result = {"thread": {"id": THREAD, "cwd": PROJECT, "status": {"type": "idle"}, "turns": []},
              "cwd": PROJECT, "sandbox": sandbox, "approvalPolicy": "on-request",
              "model": "fake-model", "modelProvider": "fake", "approvalsReviewer": "user"}
    result.update(changes)
    return result


class FakeServer:
    """Exercise the driver's real call()/notification/approval handling without I/O."""
    def __init__(self, state=None):
        self.state = effective() if state is None else state
        self.sent, self.inbox = [], []
        self.before_turn = []
        self.error = None
        self.closed = False

    def send(self, message):
        self.sent.append(copy.deepcopy(message))
        method = message.get("method")
        if "id" not in message or method is None:
            return
        if self.error and method in ("thread/start", "thread/resume"):
            self.inbox.append({"id": message["id"], "error": self.error})
            return
        if method in ("thread/start", "thread/resume"):
            result = copy.deepcopy(self.state)
        elif method == "thread/read":
            result = {"thread": copy.deepcopy(self.state["thread"])}
        elif method == "turn/start":
            self.inbox.extend(copy.deepcopy(self.before_turn))
            result = {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        elif method == "initialize":
            result = {"userAgent": "fake-0.166.0"}
        else:
            raise AssertionError(f"unexpected fake RPC: {method}")
        self.inbox.append({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if method == "turn/start":
            self.inbox.append({"method": "turn/completed", "params": {
                "threadId": THREAD, "turn": {"id": TURN, "status": "completed", "items": [
                    {"type": "agentMessage", "text": "fake completion"}]}}})

    def recv(self, timeout=None):
        return self.inbox.pop(0) if self.inbox else None

    def close(self):
        self.closed = True

    def calls(self, method):
        return [m["params"] for m in self.sent if m.get("method") == method]


loader = importlib.machinery.SourceFileLoader("codex_turn", str(Path(__file__).with_name("codex-turn")))
spec = importlib.util.spec_from_loader(loader.name, loader)
codex_turn = importlib.util.module_from_spec(spec)
loader.exec_module(codex_turn)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.log = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.log)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.server = FakeServer()
        self.client = codex_ws.Codex()
        self.client.ws = self.server

    def assert_no_turn(self):
        self.assertEqual(self.server.calls("turn/start"), [])

    def test_new_coding_defaults_and_pinned_turn(self):
        self.client.thread_start(PROJECT)
        start = self.server.calls("thread/start")[0]
        self.assertEqual(start["cwd"], PROJECT)
        self.assertEqual(start["sandbox"], "workspace-write")
        self.assertEqual(start["approvalPolicy"], "on-request")
        self.assertEqual(start["config"]["sandbox_workspace_write"],
                         {"network_access": True, "writable_roots": []})
        self.client.turn_start(THREAD, "new authorized task")
        turn = self.server.calls("turn/start")[0]
        self.assertEqual(turn["cwd"], PROJECT)
        self.assertEqual(turn["sandboxPolicy"], self.server.state["sandbox"])
        self.assertEqual(turn["approvalPolicy"], "on-request")
        self.assertNotIn("sandbox", turn)  # turn uses SandboxPolicy, not SandboxMode
        with self.assertRaisesRegex(codex_ws.ScopeError, "unknown"):
            self.client.turn_start(THREAD, "must verify again")

    def test_historical_read_only_mismatch_prevents_turn(self):
        self.server.state = effective("readOnly")  # server ignored coding overrides
        with self.assertRaisesRegex(codex_ws.ScopeError, "intended workspaceWrite, effective readOnly"):
            self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)
        with self.assertRaises(codex_ws.ScopeError):
            self.client.turn_start(THREAD, "must not run")
        self.assert_no_turn()

    def test_explicit_historical_transition_only_submits_fresh_input(self):
        self.server.state["thread"]["turns"] = [{"id": "old-rejected-turn", "status": "failed"}]
        self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT, excludeTurns=True)
        params = self.server.calls("thread/resume")[0]
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["cwd"], PROJECT)
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["config"], {"sandbox_workspace_write.writable_roots": []})
        self.client.turn_start(THREAD, "fresh future work")
        self.assertEqual(self.server.calls("turn/start")[0]["input"],
                         [{"type": "text", "text": "fresh future work"}])
        self.assertNotIn("old-rejected-turn", json.dumps(self.server.sent))

    def test_implicit_resume_preserves_read_only_without_overrides(self):
        self.server.state = effective("readOnly")
        self.client.thread_resume(THREAD, excludeTurns=True)
        self.assertEqual(self.server.calls("thread/resume"), [{"threadId": THREAD, "excludeTurns": True}])
        self.client.turn_start(THREAD, "audit only")
        self.assertEqual(self.server.calls("turn/start")[0]["sandboxPolicy"]["type"], "readOnly")

    def test_explicit_read_only_resume_and_new_legacy_alias(self):
        self.server.state = effective("readOnly")
        self.client.thread_resume(THREAD, intent="read-only", cwd=PROJECT)
        self.assertEqual(self.server.calls("thread/resume")[0]["sandbox"], "read-only")
        self.client.turn_start(THREAD, "audit")
        self.client.thread_start(PROJECT, sandbox="read-only")
        self.assertEqual(self.server.calls("thread/start")[0]["sandbox"], "read-only")

    def test_read_only_request_cannot_accept_writable_response(self):
        with self.assertRaises(codex_ws.ScopeError):
            self.client.thread_resume(THREAD, intent="read-only")
        self.assert_no_turn()

    def test_read_only_preserves_broad_audit_cwd_without_write_scope(self):
        self.server.state = effective("readOnly", cwd="/workspace")
        self.client.thread_resume(THREAD)
        self.client.turn_start(THREAD, "read-only audit")
        self.assertEqual(self.server.calls("turn/start")[0]["cwd"], "/workspace")
        self.assertEqual(self.server.calls("turn/start")[0]["sandboxPolicy"]["type"], "readOnly")

    def test_read_only_approval_preservation_and_explicit_mismatch(self):
        self.server.state = effective("readOnly", approvalPolicy="untrusted")
        self.client.thread_start(PROJECT, sandbox="read-only", approval="untrusted")
        self.client.thread_resume(THREAD)
        self.client.turn_start(THREAD, "preserve audit policy")
        self.assertEqual(self.server.calls("turn/start")[0]["approvalPolicy"], "untrusted")
        with self.assertRaisesRegex(codex_ws.ScopeError, "approval mismatch"):
            self.client.thread_resume(THREAD, intent="read-only")

    def test_close_discards_preflight_state(self):
        self.client.thread_start(PROJECT)
        self.client.close()
        with self.assertRaises(codex_ws.ScopeError):
            self.client.turn_start(THREAD, "must rejoin and verify")
        self.assert_no_turn()

    def test_writable_resume_requires_explicit_intent_and_cwd(self):
        with self.assertRaisesRegex(codex_ws.ScopeError, "explicit coding intent"):
            self.client.thread_resume(THREAD)
        for kwargs in [{"intent": "coding"}, {"cwd": PROJECT}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(codex_ws.ScopeError):
                self.client.thread_resume(THREAD, **kwargs)
        self.assertEqual(len(self.server.calls("thread/resume")), 1)
        self.assert_no_turn()

    def test_effective_cwd_mismatch_stops_start_and_resume(self):
        self.server.state["cwd"] = str(Path(PROJECT).parent)
        for action in [lambda: self.client.thread_start(PROJECT),
                       lambda: self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)]:
            with self.subTest(action=action), self.assertRaisesRegex(codex_ws.ScopeError, "cwd mismatch"):
                action()
        self.assert_no_turn()

    def test_top_level_effective_cwd_overrides_historical_thread_metadata(self):
        self.server.state["thread"]["cwd"] = str(Path(PROJECT).parent)
        self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)
        self.client.turn_start(THREAD, "new cwd authorized")
        self.assertEqual(self.server.calls("turn/start")[0]["cwd"], PROJECT)

    def test_unknown_effective_state_never_submits_turn(self):
        variants = [None, {}, {"thread": None}]
        for field in ["cwd", "sandbox", "approvalPolicy"]:
            state = effective()
            del state[field]
            variants.append(state)
        for sandbox in ["workspace-write", {"type": "workspace-write"}, {"type": "externalSandbox"},
                        {"type": "dangerFullAccess"}, {"type": "workspaceWrite", "writableRoots": None},
                        {"type": "workspaceWrite", "networkAccess": "true"},
                        {"type": "workspaceWrite", "unknownPermission": True}]:
            variants.append(effective(sandbox=sandbox))
        for state in variants:
            with self.subTest(state=state):
                self.server.state = state
                with self.assertRaises(codex_ws.ScopeError):
                    self.client.thread_start(PROJECT)
                with self.assertRaises(codex_ws.ScopeError):
                    self.client.turn_start(THREAD, "must not run")
        self.assert_no_turn()
        self.assertNotIn("scope verified", self.log.getvalue())

    def test_additional_roots_must_stay_within_exact_project(self):
        for root in ["/", "/workspace", str(Path(PROJECT).parent), "/tmp", "relative",
                     PROJECT + "-sibling", PROJECT + "/../sibling"]:
            with self.subTest(root=root):
                self.server.state["sandbox"]["writableRoots"] = [root]
                with self.assertRaisesRegex(codex_ws.ScopeError, "writable root"):
                    self.client.thread_start(PROJECT)
        self.assert_no_turn()
        self.server.state["sandbox"]["writableRoots"] = [PROJECT, PROJECT + "/scripts"]
        self.client.thread_start(PROJECT)

    def test_schema_default_additional_roots_are_empty(self):
        del self.server.state["sandbox"]["writableRoots"]
        self.client.thread_start(PROJECT)
        self.client.turn_start(THREAD, "use schema default")

    def test_approval_and_network_mismatches_fail(self):
        for changes in [{"approvalPolicy": "never"}, {"approvalPolicy": None},
                        {"approvalPolicy": {"new-policy": True}},
                        {"sandbox": {"type": "workspaceWrite", "networkAccess": False}}]:
            self.server.state = effective(**changes)
            with self.subTest(changes=changes), self.assertRaises(codex_ws.ScopeError):
                self.client.thread_start(PROJECT)
        self.assert_no_turn()

    def test_no_network_is_pinned(self):
        self.server.state["sandbox"]["networkAccess"] = False
        self.client.thread_start(PROJECT, network=False)
        self.client.turn_start(THREAD, "offline task")
        self.assertFalse(self.server.calls("turn/start")[0]["sandboxPolicy"]["networkAccess"])

    def test_intent_conflict_and_broad_cwd_rejected_before_rpc(self):
        for kwargs in [{"intent": "read-only", "sandbox": "workspace-write"},
                       {"intent": "coding", "sandbox": "read-only"},
                       {"sandbox": "danger-full-access"}, {"approval": "never"},
                       {"config": {"sandbox_mode": "danger-full-access"}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(codex_ws.ScopeError):
                self.client.thread_start(PROJECT, **kwargs)
        for cwd in [None, "", "/workspace", "/"]:
            with self.subTest(cwd=cwd), self.assertRaises(codex_ws.ScopeError):
                self.client.thread_start(cwd)
        self.assertEqual(self.server.sent, [])

    def test_active_or_unknown_status_blocks_permission_transition(self):
        for status in [{"type": "active", "activeFlags": ["waitingOnApproval"]}, {}, None]:
            self.server.state["thread"]["status"] = status
            with self.subTest(status=status), self.assertRaises(codex_ws.ScopeError):
                self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)
        self.assertEqual(self.server.calls("thread/resume"), [])
        self.assert_no_turn()

    def test_active_resume_response_blocks_new_turn(self):
        self.server.state = effective("readOnly")
        self.server.state["thread"]["status"] = {"type": "active", "activeFlags": []}
        with self.assertRaises(codex_ws.ScopeError):
            self.client.thread_resume(THREAD)
        self.assert_no_turn()

    def test_conflicting_thread_id_is_rejected(self):
        self.server.state["thread"]["id"] = "other-thread"
        with self.assertRaisesRegex(codex_ws.ScopeError, "conflicting thread/read"):
            self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)
        self.assert_no_turn()

    def test_failed_resume_invalidates_previous_verification(self):
        self.client.thread_start(PROJECT)
        self.server.state = effective("readOnly")
        with self.assertRaises(codex_ws.ScopeError):
            self.client.thread_resume(THREAD, intent="coding", cwd=PROJECT)
        with self.assertRaises(codex_ws.ScopeError):
            self.client.turn_start(THREAD, "stale scope")
        self.assert_no_turn()

    def test_no_scoped_turn_without_verification_or_with_permission_override(self):
        with self.assertRaises(codex_ws.ScopeError):
            self.client.turn_start(THREAD, "unknown")
        self.client.thread_start(PROJECT)
        for extra in [{"cwd": "/workspace"}, {"sandboxPolicy": {"type": "dangerFullAccess"}},
                      {"approvalPolicy": "never"}, {"threadId": "another-thread"}]:
            with self.subTest(extra=extra), self.assertRaises(codex_ws.ScopeError):
                self.client.turn_start(THREAD, "override", **extra)
        self.assert_no_turn()

    def test_rpc_error_preserved_without_fallback_or_turn(self):
        self.server.error = {"code": -32602, "message": "unsupported sandbox"}
        with self.assertRaises(codex_ws.RPCError) as raised:
            self.client.thread_start(PROJECT)
        self.assertEqual(raised.exception.error, self.server.error)
        self.assertEqual(len(self.server.calls("thread/start")), 1)
        self.assert_no_turn()

    def test_driver_decline_provenance_does_not_rewrite_server_rejection(self):
        notice = {"method": "error", "params": {"message": 'Rejected("rejected by user")'}}
        observed = []
        self.client.on_event = observed.append
        self.server.before_turn = [
            {"id": "approval-file", "method": "item/fileChange/requestApproval", "params": {}},
            {"id": "approval-command", "method": "item/commandExecution/requestApproval", "params": {}},
            {"id": "approval-legacy", "method": "applyPatchApproval", "params": {}}, notice]
        self.client.thread_start(PROJECT)
        self.client.turn_start(THREAD, "fake task")
        replies = [m for m in self.server.sent if isinstance(m.get("id"), str)]
        self.assertEqual([r["result"]["decision"] for r in replies], ["decline", "decline", "denied"])
        self.assertIn("source=driver policy=decline", self.log.getvalue())
        self.assertIn("requestId=approval-file", self.log.getvalue())
        self.assertNotIn("rejected by user", self.log.getvalue())
        self.assertEqual(observed, [notice])
        self.assertEqual(self.client.next_event(), notice)


class CLITests(unittest.TestCase):
    def invoke(self, which, args, state=None):
        server = FakeServer(state)
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(codex_ws, "WS", return_value=server), \
                mock.patch.object(codex_ws, "DEFAULT_STATE", None), \
                mock.patch.object(codex_turn, "changed_files", return_value=[]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            if which == "ws":
                rc = codex_ws.main(args)
            else:
                with mock.patch("sys.argv", ["codex-turn"] + args):
                    rc = codex_turn.run()
        return rc, server, output.getvalue(), error.getvalue()

    def test_both_clis_create_and_resume_coding_with_explicit_cwd(self):
        cases = [("ws", ["start", "--cwd", PROJECT, "fresh"]),
                 ("waiter", ["--new", "--cwd", PROJECT, "fresh"]),
                 ("ws", ["turn", THREAD, "fresh", "--intent", "coding", "--cwd", PROJECT]),
                 ("waiter", [THREAD, "fresh", "--intent", "coding", "--cwd", PROJECT])]
        for which, args in cases:
            with self.subTest(which=which, args=args):
                rc, server, output, _ = self.invoke(which, args)
                self.assertEqual(rc, 0, output)
                self.assertEqual(len(server.calls("turn/start")), 1)
                self.assertIn(TURN, output)
                self.assertTrue(server.closed)
                init = server.calls("initialize")[0]
                self.assertNotIn("capabilities", init)  # no experimentalApi

    def test_both_clis_fail_when_resume_ignores_coding_scope(self):
        cases = [("ws", ["turn", THREAD, "fresh", "--intent", "coding", "--cwd", PROJECT]),
                 ("waiter", [THREAD, "fresh", "--intent", "coding", "--cwd", PROJECT])]
        for which, args in cases:
            with self.subTest(which=which):
                rc, server, output, _ = self.invoke(which, args, effective("readOnly"))
                self.assertEqual(rc, 2)
                self.assertIn("ScopeError", output)
                self.assertEqual(server.calls("turn/start"), [])

    def test_both_clis_preserve_read_only(self):
        cases = [("ws", ["start", "--cwd", PROJECT, "--sandbox", "read-only", "audit"]),
                 ("waiter", ["--new", "--cwd", PROJECT, "--intent", "read-only", "audit"]),
                 ("ws", ["turn", THREAD, "audit"]), ("waiter", [THREAD, "audit"]),
                 ("ws", ["turn", THREAD, "audit", "--intent", "read-only"]),
                 ("waiter", [THREAD, "audit", "--intent", "read-only"])]
        for which, args in cases:
            with self.subTest(which=which, args=args):
                rc, server, output, _ = self.invoke(which, args, effective("readOnly"))
                self.assertEqual(rc, 0, output)
                self.assertEqual(server.calls("turn/start")[0]["sandboxPolicy"]["type"], "readOnly")

    def test_cli_missing_coding_cwd_and_legacy_full_access_do_not_connect(self):
        for which, args in [("ws", ["turn", THREAD, "task", "--intent", "coding"]),
                            ("waiter", [THREAD, "task", "--intent", "coding"]),
                            ("ws", ["start", "--cwd", PROJECT, "--danger-full-access"])]:
            with self.subTest(which=which, args=args):
                rc, server, _, _ = self.invoke(which, args)
                self.assertEqual(rc, 2)
                self.assertEqual(server.sent, [])


class InstalledSchemaTests(unittest.TestCase):
    def test_stable_0166_scope_contract(self):
        base = Path("/home/box/.codex-app-server/schema-0.166.0/v2")
        if not base.is_dir():
            self.skipTest("installed 0.166.0 schema not present; fake regressions remain portable")
        for name in ["ThreadStartParams", "ThreadResumeParams"]:
            schema = json.loads((base / (name + ".json")).read_text())
            self.assertTrue({"cwd", "sandbox", "approvalPolicy", "config"} <= schema["properties"].keys())
        for name in ["ThreadStartResponse", "ThreadResumeResponse"]:
            schema = json.loads((base / (name + ".json")).read_text())
            self.assertTrue({"cwd", "sandbox", "approvalPolicy", "thread"} <= set(schema["required"]))
            variants = schema["definitions"]["SandboxPolicy"]["oneOf"]
            policy = next(v for v in variants if v["properties"]["type"]["enum"] == ["workspaceWrite"])
            self.assertEqual(policy["properties"]["writableRoots"]["default"], [])
        turn = json.loads((base / "TurnStartParams.json").read_text())
        self.assertTrue({"cwd", "sandboxPolicy", "approvalPolicy"} <= turn["properties"].keys())
        self.assertNotIn("sandbox", turn["properties"])


if __name__ == "__main__":
    unittest.main()
