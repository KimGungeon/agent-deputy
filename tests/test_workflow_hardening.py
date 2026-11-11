"""Cross-command regressions for unattended operation; no remote writes."""
import argparse
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from deputy_module import load

d = load()
CFG = {"members": [{"name": "a", "owns": ["bin"]},
                   {"name": "b", "owns": ["settings"]},
                   {"name": "lead", "role": "coordinator", "owns": []}]}


def comment(body, second, ident=None):
    return {"id": ident or f"C{second}", "body": body,
            "createdAt": f"2026-09-30T00:00:{second:02}Z"}


def issue(comments=(), labels=("deputy:wip",)):
    return dict(number=999, title="fixture", url="https://example.invalid/999",
                body="", comments=list(comments), labels=[{"name": x} for x in labels],
                assignees=[], state="OPEN")


def next_output(iss, member):
    out = io.StringIO()
    with mock.patch.object(d, "active_issues", return_value=[iss]), \
            mock.patch.object(d, "issue", return_value=iss), contextlib.redirect_stdout(out):
        d.cmd_next(CFG, argparse.Namespace(as_=member))
    return out.getvalue()


class TestHumanInstructions(unittest.TestCase):
    def setUp(self):
        self.human = comment("STOP requested by human", 2)
        self.iss = issue([comment("<!-- deputy v1 kind=claim by=a -->", 1), self.human,
                          comment("<!-- deputy v1 kind=note by=b --> unrelated", 3)])

    def test_unrelated_note_does_not_hide_directive_for_any_role(self):
        for member in ("a", "b", "lead"):
            with self.subTest(member=member):
                output = next_output(self.iss, member)
                self.assertIn(self.human["body"], output)
                self.assertNotIn("deputy done", output)

    def test_unlabelled_issue_directive_is_visible(self):
        self.iss["labels"] = []
        self.assertIn(self.human["body"], next_output(self.iss, "a"))

    def test_ack_is_specific_to_member_and_comment_version(self):
        args = argparse.Namespace(as_="a", number=999, comment="C2", result="Stopped work")
        with mock.patch.object(d, "issue", return_value=self.iss), \
                mock.patch.object(d, "gh") as gh, contextlib.redirect_stdout(io.StringIO()):
            d.cmd_ack(CFG, args)
        body = gh.call_args.args[0][-1]
        self.iss["comments"].append(comment(body, 4))
        self.assertNotIn(self.human["body"], next_output(self.iss, "a"))
        self.assertIn(self.human["body"], next_output(self.iss, "b"))
        self.human["body"] = "STOP requested by human (edited)"
        self.assertIn(self.human["body"], next_output(self.iss, "a"))

    def test_ack_rejects_unknown_or_bot_comment_without_writing(self):
        for ident in ("missing", "C1"):
            with mock.patch.object(d, "issue", return_value=self.iss), \
                    mock.patch.object(d, "gh") as gh, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                d.cmd_ack(CFG, argparse.Namespace(as_="a", number=999, comment=ident, result="done"))
            gh.assert_not_called()

    def test_displayed_version_cannot_ack_a_later_edit(self):
        ref = d.human_comment_ref(self.human)
        self.human["body"] += " edited before ack"
        with mock.patch.object(d, "issue", return_value=self.iss), \
                mock.patch.object(d, "gh") as gh, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            d.cmd_ack(CFG, argparse.Namespace(as_="a", number=999, comment=ref, result="done"))
        gh.assert_not_called()

    def test_new_comment_remains_pending_after_ack_of_previous(self):
        ref = d.human_comment_ref(self.human)
        ack = f"<!-- deputy v1 kind=ack by=a -->\n<!-- deputy-ack ref={ref} -->"
        later = comment("New instruction", 4)
        self.iss["comments"].extend([comment(ack, 3, "ack"), later])
        self.assertEqual(d.pending_human_comments(self.iss, "a"), [later])

    def test_cli_parses_ack(self):
        args = d.main_parser().parse_args(["--as", "a", "ack", "999", "--comment", "C2", "--result", "done"])
        self.assertEqual(args.fn, d.cmd_ack)
        self.assertEqual(args.as_, "a")


class TestClaimOwnership(unittest.TestCase):
    def test_losing_claim_does_not_transfer_ownership(self):
        iss = issue([comment("<!-- deputy v1 kind=claim by=a -->", 1),
                     comment("<!-- deputy v1 kind=claim by=b -->", 2),
                     comment("<!-- deputy v1 kind=note by=b --> withdrawal", 3)])
        self.assertIn("deputy done", next_output(iss, "a"))
        self.assertNotIn("deputy done", next_output(iss, "b"))
        self.assertEqual(d.decide_action(CFG, "b", iss)[0], "other-wip")
        self.assertEqual(d.decide_action(CFG, "a", iss)[0], "mine-wip")
        out = io.StringIO()
        with mock.patch.object(d, "list_issues", return_value=[iss]), \
                mock.patch.object(d, "issue", return_value=iss), contextlib.redirect_stdout(out):
            d.cmd_status(CFG, argparse.Namespace(no_done=True, web=False))
        self.assertIn("← a", out.getvalue())
        self.assertNotIn("← b", out.getvalue())

    def test_claim_race_loser_exits_without_assigning_or_labelling(self):
        before = issue(labels=())
        after = issue([comment("<!-- deputy v1 kind=claim by=a -->", 1),
                       comment("<!-- deputy v1 kind=claim by=b -->", 2)])
        with mock.patch.object(d, "issue", side_effect=[before, after]), \
                mock.patch.object(d, "consensus", return_value={"decision": "CLAIM", "reason": "agreed"}), \
                mock.patch.object(d, "gh") as gh, mock.patch.object(d, "set_labels") as labels, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            d.cmd_claim(CFG, argparse.Namespace(as_="b", number=999, force=False))
        self.assertEqual(stopped.exception.code, 3)
        labels.assert_not_called()
        self.assertFalse(any("--add-assignee" in c.args[0] for c in gh.call_args_list))


class TestCompletion(unittest.TestCase):
    def run_done(self, close_result="", state="CLOSED"):
        original = issue()
        after = None if state is None else dict(original, state=state)
        out = io.StringIO()
        def gh_call(args, **kwargs):
            return close_result if args[:2] == ["issue", "close"] else ""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(d, "issue", side_effect=[original, after]))
            stack.enter_context(mock.patch.object(d, "open_children", return_value=[]))
            gh = stack.enter_context(mock.patch.object(d, "gh", side_effect=gh_call))
            labels = stack.enter_context(mock.patch.object(d, "set_labels"))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = 0
            try:
                d.cmd_done(CFG, argparse.Namespace(as_="a", number=999, close=True,
                                                  result="tested", no_next=True))
            except SystemExit as e:
                code = e.code
        return code, gh, labels, out.getvalue()

    def test_close_failure_preserves_tracking_and_no_done_marker(self):
        for result, state in ((None, "OPEN"), ("", "OPEN"), ("", None)):
            with self.subTest(result=result, state=state):
                code, gh, labels, output = self.run_done(result, state)
                self.assertNotEqual(code, 0)
                labels.assert_not_called()
                self.assertFalse(any("--remove-assignee" in c.args[0] for c in gh.call_args_list))
                self.assertFalse(any("kind=done" in str(c) for c in gh.call_args_list))
                self.assertNotIn("(이슈 닫음)", output)

    def test_verified_close_records_done_then_cleans_tracking(self):
        code, gh, labels, output = self.run_done()
        self.assertEqual(code, 0)
        self.assertEqual(gh.call_args_list[0].args[0][:2], ["issue", "close"])
        self.assertTrue(any("kind=done" in str(c) for c in gh.call_args_list))
        labels.assert_called_once()
        self.assertIn("(이슈 닫음)", output)


class TestProjectSessions(unittest.TestCase):
    def test_foreign_and_missing_paths_excluded_before_ranking(self):
        root = os.path.abspath("project")
        rows = [dict(name="deputy-a", id="local", cwd=root, status="idle"),
                dict(name="deputy-a", id="foreign", cwd=root + "-other", status="busy"),
                dict(name="deputy-b", id="unknown", status="busy")]
        with mock.patch.object(d, "repo_root", return_value=root), \
                mock.patch.object(d, "gh_free", return_value=json.dumps(rows)) as cli:
            states = d.session_states(CFG)
        self.assertEqual(states["a"]["id"], "local")
        self.assertIsNone(states["b"])
        self.assertIn("--cwd", cli.call_args.args[0])

    def test_unavailable_session_list_fails_instead_of_assuming_absent(self):
        for payload in (None, "garbage", "{}", '["invalid row"]'):
            with self.subTest(payload=payload), mock.patch.object(d, "repo_root", return_value=os.getcwd()), \
                    mock.patch.object(d, "gh_free", return_value=payload), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                d.session_states(CFG)

    def test_normalized_project_path_and_local_ranking(self):
        root = os.path.abspath("project")
        rows = [dict(name="deputy-a", id="idle", cwd=root, status="idle", startedAt=2),
                dict(name="deputy-a", id="busy", cwd=root + os.sep + ".", status="busy", startedAt=1)]
        with mock.patch.object(d, "repo_root", return_value=root), \
                mock.patch.object(d, "gh_free", return_value=json.dumps({"agents": rows})):
            self.assertEqual(d.session_states(CFG)["a"]["id"], "busy")
        self.assertFalse(d.same_project("relative", root))

    def test_jobs_are_scoped_to_exact_project(self):
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, "project")
            for name, cwd in (("local", root), ("foreign", root + "-other"), ("unknown", None)):
                folder = os.path.join(home, ".claude", "jobs", name)
                os.makedirs(folder)
                with open(os.path.join(folder, "state.json"), "w", encoding="utf-8") as f:
                    json.dump(dict(name="deputy-a", cwd=cwd, updatedAt=name, detail=name), f)
            with mock.patch.object(d, "HOME", home), mock.patch.object(d, "repo_root", return_value=root):
                self.assertEqual(d.job_details()["a"]["detail"], "local")


class TestWorkflowReplay(unittest.TestCase):
    def test_proposal_reviews_claim_human_ack_and_verified_completion(self):
        """Replay production commands against an in-memory GitHub boundary."""
        record = issue(labels=())
        cfg = dict(CFG, review_timeout_minutes=45, max_revisions=2)

        def gh(args, **kwargs):
            operation = args[:2]
            if operation == ["issue", "comment"]:
                record["comments"].append(comment(args[-1], len(record["comments"]) + 1))
            elif operation == ["issue", "close"]:
                record["state"] = "CLOSED"
            elif operation == ["issue", "edit"]:
                record["assignees"] = [{"login": "shared-account"}] if "--add-assignee" in args else []
            else:
                self.fail(f"Unexpected external operation: {args}")
            return ""

        def labels(number, add=(), remove=(), **kwargs):
            current = (d.label_names(record) - set(remove)) | set(add)
            record["labels"] = [{"name": name} for name in sorted(current)]

        def run(member, *command):
            parsed = d.main_parser().parse_args(["--as", member, *command])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                parsed.fn(cfg, parsed)
            return output.getvalue()

        with mock.patch.object(d, "gh", side_effect=gh), \
                mock.patch.object(d, "issue", side_effect=lambda *a, **kw: copy.deepcopy(record)), \
                mock.patch.object(d, "set_labels", side_effect=labels), \
                mock.patch.object(d, "active_issues", side_effect=lambda *_: [copy.deepcopy(record)] if record["state"] == "OPEN" else []):
            run("a", "propose", "999", "--why", "Repair workflow reliability", "--done-when", "tests pass")
            self.assertIn("deputy review", run("b", "next"))
            for member in ("b", "lead"):
                run(member, "review", "999", "--verdict", "AGREE", "--reason", "Checked regression coverage and scope")
            self.assertEqual(d.consensus(cfg, record)["decision"], "CLAIM")
            run("a", "claim", "999")
            self.assertIn("deputy:wip", d.label_names(record))
            human = comment("Use the revised acceptance criteria", len(record["comments"]) + 1)
            record["comments"].append(human)
            run("b", "note", "999", "Unrelated progress update")
            self.assertIn(human["body"], run("a", "next"))
            run("a", "ack", "999", "--comment", d.human_comment_ref(human), "--result", "Applied revised criteria")
            self.assertNotIn(human["body"], run("a", "next"))
            self.assertIn(human["body"], run("b", "next"))
            run("a", "done", "999", "--close", "--no-next", "--result", "Regression tests passed")
            self.assertEqual(record["state"], "CLOSED")
            self.assertEqual(record["assignees"], [])
            self.assertNotIn("deputy:wip", d.label_names(record))
            self.assertEqual(d.deputy_comments(record)[-1]["kind"], "done")
