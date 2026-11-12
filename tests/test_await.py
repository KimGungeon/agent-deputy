"""사람의 답을 기다리는 이슈(deputy await): 후보에서 빠지고, 사람이 답하면 돌아온다."""
import argparse
import contextlib
import io
import unittest
from unittest import mock

from deputy_module import load

d = load()
CFG = {"members": [{"name": "lead", "role": "coordinator", "owns": []}], "candidate_labels": ["직원요청"]}
AWAIT = "<!-- deputy v1 kind=await by=lead -->\n### 질문\n어느 강아지를 고르나요?"


def comment(body, minute):
    return {"id": f"C{minute}", "body": body, "createdAt": f"2026-11-12T00:{minute:02}:00Z"}


def iss(num, labels=(), comments=()):
    return dict(number=num, title=f"t{num}", url="u", body="", labels=[{"name": x} for x in labels],
                assignees=[], comments=list(comments), state="OPEN")


class TestAwait(unittest.TestCase):
    def test_awaiting_issue_is_not_candidate_or_work(self):
        waiting = iss(7, ["직원요청", "deputy:await"])
        self.assertFalse(d.is_candidate(CFG, waiting))
        with mock.patch.object(d, "active_issues", return_value=[waiting]), \
                mock.patch.object(d, "release_answered", return_value=[]):
            self.assertFalse(d.has_work(CFG))

    def released(self, comments):
        waiting = iss(7, ["직원요청", "deputy:await"], comments)
        with mock.patch.object(d, "list_issues", return_value=[iss(7, ["직원요청", "deputy:await"])]), \
                mock.patch.object(d, "issue", return_value=waiting), \
                mock.patch.object(d, "set_labels") as labels:
            got = d.release_answered()
        return got, labels

    def test_human_reply_after_question_releases(self):
        got, labels = self.released([comment(AWAIT, 1), comment("1번 강아지요", 5)])
        self.assertEqual(got, [7])
        labels.assert_called_once_with(7, remove=["deputy:await"], verify=False)

    def test_no_reply_or_older_or_deputy_comment_keeps_waiting(self):
        for comments in ([comment(AWAIT, 1)],
                         [comment("처음 요청", 0), comment(AWAIT, 1)],
                         [comment(AWAIT, 1), comment("<!-- deputy v1 kind=note by=lead -->\n메모", 3)]):
            with self.subTest(comments=[c["body"][:10] for c in comments]):
                got, labels = self.released(comments)
                self.assertEqual(got, [])
                labels.assert_not_called()

    def test_label_without_question_marker_is_released_by_any_human_comment(self):
        got, _ = self.released([comment("사람 답", 2)])
        self.assertEqual(got, [7])

    def test_await_command_posts_marker_and_label(self):
        calls = []
        with mock.patch.object(d, "gh", side_effect=lambda a, **k: calls.append(a) or ""), \
                mock.patch.object(d, "set_labels") as labels, contextlib.redirect_stdout(io.StringIO()):
            d.cmd_await(CFG, argparse.Namespace(as_="lead", number=7, text="어느 강아지를 고르나요?"))
        body = next(a for a in calls if a[:2] == ["issue", "comment"])
        self.assertIn("kind=await by=lead", body[-1])
        self.assertIn("어느 강아지를 고르나요?", body[-1])
        self.assertTrue(any(a[:3] == ["label", "create", "deputy:await"] for a in calls))
        labels.assert_called_once_with(7, add=["deputy:await"])

    def test_next_releases_answered_before_listing(self):
        out = io.StringIO()
        with mock.patch.object(d, "release_answered", return_value=[7]) as rel, \
                mock.patch.object(d, "active_issues", return_value=[iss(7, ["직원요청"])]), \
                mock.patch.object(d, "issue", return_value=iss(7, ["직원요청"])), contextlib.redirect_stdout(out):
            d.cmd_next(CFG, argparse.Namespace(as_="lead"))
        rel.assert_called_once()
        self.assertIn("#7", out.getvalue())

    def test_doctor_does_not_require_await_label(self):
        self.assertNotIn("deputy:await", d.REQUIRED_LABELS)
        self.assertIn("deputy:await", d.LABELS)


if __name__ == "__main__":
    unittest.main()
