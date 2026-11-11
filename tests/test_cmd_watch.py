"""Watch decisions and restart failure isolation (#9); no live sessions."""
import argparse
import contextlib
import io
import subprocess
import unittest
from unittest import mock

from deputy_module import load

deputy = load()
NOW = 10000
AUTH = {"detail": "Please run /login"}


class TestWatchAction(unittest.TestCase):
    def decide(self, state=None, job=None, previous=0):
        return deputy.watch_action(state, job, previous, NOW, 120, 600)

    def test_busy_precedes_grace_auth_and_cooldown(self):
        self.assertEqual(self.decide({"status": "busy", "startedAt": NOW * 1000},
                                     AUTH, NOW), "busy")

    def test_grace_precedes_auth_and_cooldown(self):
        self.assertEqual(self.decide({"status": "idle", "startedAt": (NOW - 119) * 1000},
                                     AUTH, NOW), "grace")

    def test_grace_expires_at_exact_boundary(self):
        self.assertEqual(self.decide({"status": "idle", "startedAt": (NOW - 120) * 1000}),
                         "restart")

    def test_auth_precedes_cooldown_even_without_session(self):
        self.assertEqual(self.decide(job=AUTH, previous=NOW), "auth_blocked")

    def test_cooldown_boundary(self):
        self.assertEqual(self.decide(previous=NOW - 599), "cooldown")
        self.assertEqual(self.decide(previous=NOW - 600), "restart")

    def test_missing_idle_and_stopped_sessions_restart(self):
        for state in (None, {}, {"status": "idle"}, {"status": "stopped", "startedAt": 0}):
            with self.subTest(state=state):
                self.assertEqual(self.decide(state), "restart")

    def test_rate_limit_is_not_auth_block(self):
        self.assertEqual(self.decide(job={"detail": "Rate limit exceeded"}), "restart")


class TestCmdWatch(unittest.TestCase):
    def run_watch(self, outcomes=(), dry=False, states=None, jobs=None, history=None):
        if states is None:
            states = {"a": {"id": "session-a", "status": "idle"}, "b": None}
        if history is None:
            history = {"other-repo": {"untouched": 42}}
        args = argparse.Namespace(grace=120, cooldown=600, dry=dry, loop=0)
        with contextlib.ExitStack() as stack:
            patches = {
                "repo_root": dict(return_value="test-repo"),
                "watch_state_path": dict(return_value="mock-watch-state.json"),
                "read_json": dict(return_value=history),
                "write_json": {},
                "session_states": dict(return_value=states),
                "job_details": dict(return_value=jobs or {}),
                "gh_free": {},
                "has_work": dict(return_value=True),
            }
            mocks = {name: stack.enter_context(mock.patch.object(deputy, name, **kwargs))
                     for name, kwargs in patches.items()}
            stack.enter_context(mock.patch.object(deputy.time, "time", return_value=NOW))
            stack.enter_context(mock.patch.object(deputy.shutil, "which", return_value="mock-bash"))
            run = stack.enter_context(mock.patch.object(deputy.subprocess, "run", side_effect=outcomes))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            deputy.cmd_watch({}, args)
        return run, mocks, output.getvalue()

    def test_failed_restart_does_not_stop_remaining_members(self):
        for failure in (subprocess.TimeoutExpired("mock-bash", 180),
                        OSError("restart failed"), argparse.Namespace(returncode=1)):
            with self.subTest(failure=failure):
                run, mocks, output = self.run_watch(
                    [failure, argparse.Namespace(returncode=0)])
                self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["a", "b"])
                self.assertEqual(mocks["write_json"].call_args.args[1],
                                 {"other-repo": {"untouched": 42}, "test-repo": {"b": NOW}})
                self.assertIn("기동 실패", output)
                self.assertIn("1개 재기동했습니다. 실패 1개", output)
                self.assertEqual(mocks["gh_free"].call_args_list,
                                 [mock.call(["claude", "stop", "session-a"], timeout=20),
                                  mock.call(["claude", "rm", "session-a"], timeout=20)])
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs["timeout"], 180)
                    self.assertNotIn("capture_output", call.kwargs)
                    self.assertNotEqual(call.kwargs["stdout"], subprocess.PIPE)
                    self.assertTrue(call.kwargs["stdout"].closed)

    def test_dry_run_has_no_process_or_history_writes(self):
        history = {"test-repo": {"a": 1}}
        run, mocks, _ = self.run_watch(dry=True, history=history)
        run.assert_not_called()
        mocks["gh_free"].assert_not_called()
        mocks["write_json"].assert_not_called()
        self.assertEqual(history, {"test-repo": {"a": 1}})

    def test_skipped_members_are_not_restarted(self):
        states = {"busy": {"status": "busy"},
                  "grace": {"status": "idle", "startedAt": NOW * 1000},
                  "auth": None, "cooldown": None, "ready": None}
        run, mocks, _ = self.run_watch(
            [argparse.Namespace(returncode=0)], states=states,
            jobs={"auth": AUTH}, history={"test-repo": {"cooldown": NOW}})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "ready")
        mocks["gh_free"].assert_not_called()
        self.assertEqual(mocks["write_json"].call_args.args[1],
                         {"test-repo": {"cooldown": NOW, "ready": NOW}})
