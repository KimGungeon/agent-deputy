"""밤 무인 운영: 후보 라벨 필터, deputy stop 과 watch 멈춤 표시, Windows 절전 판독, 예약 작업 구성."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from deputy_module import load

d = load()
CFG = {"members": [{"name": "a", "owns": ["bin"]},
                   {"name": "lead", "role": "coordinator", "owns": []}]}


def iss(num, labels=(), assignees=()):
    return dict(number=num, title=f"t{num}", url=f"https://example.invalid/{num}", body="",
                comments=[], labels=[{"name": x} for x in labels],
                assignees=[{"login": x} for x in assignees], state="OPEN")


class TestCandidateLabels(unittest.TestCase):
    def test_without_filter_every_free_issue_is_candidate(self):
        self.assertTrue(d.is_candidate(CFG, iss(1)))
        self.assertTrue(d.is_candidate(CFG, iss(2, ["bug"])))
        self.assertFalse(d.is_candidate(CFG, iss(3, ["deputy:wip"])))
        self.assertFalse(d.is_candidate(CFG, iss(4, assignees=["me"])))

    def test_filter_keeps_only_issues_with_any_listed_label(self):
        cfg = dict(CFG, candidate_labels=["직원요청", "야간"])
        self.assertTrue(d.is_candidate(cfg, iss(1, ["직원요청"])))
        self.assertTrue(d.is_candidate(cfg, iss(2, ["야간", "bug"])))
        self.assertFalse(d.is_candidate(cfg, iss(3, ["bug"])))
        self.assertFalse(d.is_candidate(cfg, iss(4)))
        self.assertFalse(d.is_candidate(cfg, iss(5, ["직원요청", "deputy:next"])))

    def test_active_issues_also_fetches_candidate_labels(self):
        seen = []

        def fake(state="open", labels=None, limit=200):
            seen.append(tuple(labels or ()))
            return [iss(77, ["직원요청"])] if labels == ["직원요청"] else []
        with mock.patch.object(d, "list_issues", side_effect=fake):
            got = d.active_issues(dict(CFG, candidate_labels=["직원요청"]))
        self.assertIn(("직원요청",), seen)
        self.assertEqual([i["number"] for i in got], [77])

    def next_output(self, cfg, member, issues):
        out = io.StringIO()
        with mock.patch.object(d, "active_issues", return_value=issues), \
                mock.patch.object(d, "issue", side_effect=lambda n, **k: next(i for i in issues if i["number"] == n)), \
                contextlib.redirect_stdout(out):
            d.cmd_next(cfg, argparse.Namespace(as_=member))
        return out.getvalue()

    def test_builder_sees_only_labeled_candidates(self):
        cfg = dict(CFG, candidate_labels=["직원요청"])
        out = self.next_output(cfg, "a", [iss(1, ["직원요청"]), iss(2)])
        self.assertIn("#1 t1", out)
        self.assertNotIn("#2 t2", out)

    def test_filtered_empty_backlog_does_not_ask_to_invent_work(self):
        cfg = dict(CFG, candidate_labels=["직원요청"])
        for member in ("a", "lead"):
            with self.subTest(member=member):
                out = self.next_output(cfg, member, [iss(2)])
                self.assertNotIn("gh issue create", out)
                self.assertIn("직원요청", out)


class TestStop(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "watch-state.json")
        self.root = os.path.join(tmp.name, "repo")
        for target, value in (("watch_state_path", lambda: self.state), ("repo_root", lambda: self.root)):
            p = mock.patch.object(d, target, side_effect=value)
            p.start()
            self.addCleanup(p.stop)

    def test_stop_marks_and_resume_clears(self):
        self.assertFalse(d.is_stopped(self.root))
        d.set_stopped(self.root, True)
        self.assertTrue(d.is_stopped(self.root))
        self.assertFalse(d.is_stopped(self.root + "-other"))
        d.set_stopped(self.root, False)
        self.assertFalse(d.is_stopped(self.root))

    def test_stop_ends_only_this_projects_deputy_sessions(self):
        rows = [{"id": "s1", "name": "deputy-a", "cwd": self.root, "kind": "background", "status": "busy"},
                {"id": "s2", "name": "deputy-lead", "cwd": self.root, "kind": "background", "status": "idle"},
                {"id": "s3", "name": "other", "cwd": self.root, "kind": "background", "status": "busy"},
                {"id": "s4", "name": "deputy-a", "cwd": self.root + "-other", "kind": "background"}]
        calls = []

        def fake(argv, timeout=30):
            calls.append(argv)
            return json.dumps(rows) if argv[:2] == ["claude", "agents"] else ""
        with mock.patch.object(d, "gh_free", side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            d.cmd_stop(CFG, argparse.Namespace(resume=False))
        stopped = [c[2] for c in calls if c[:2] == ["claude", "stop"]]
        self.assertEqual(stopped, ["s1", "s2"])
        self.assertTrue(d.is_stopped(self.root))

    def test_resume_only_clears_mark(self):
        d.set_stopped(self.root, True)
        with mock.patch.object(d, "gh_free", side_effect=AssertionError("세션을 건드리면 안 됨")), \
                contextlib.redirect_stdout(io.StringIO()):
            d.cmd_stop(CFG, argparse.Namespace(resume=True))
        self.assertFalse(d.is_stopped(self.root))

    def test_watch_does_not_revive_while_stopped(self):
        d.set_stopped(self.root, True)
        out = io.StringIO()
        with mock.patch.object(d, "session_states", side_effect=AssertionError("되살리면 안 됨")), \
                contextlib.redirect_stdout(out):
            d.cmd_watch(CFG, argparse.Namespace(dry=False, loop=0, cooldown=900, grace=180))
        self.assertIn("deputy stop", out.getvalue())


class TestHasWork(unittest.TestCase):
    def work(self, cfg, issues):
        with mock.patch.object(d, "active_issues", return_value=issues):
            return d.has_work(cfg)

    def test_candidate_or_running_deputy_issue_is_work(self):
        cfg = dict(CFG, candidate_labels=["직원요청"])
        self.assertTrue(self.work(cfg, [iss(1, ["직원요청"])]))
        self.assertTrue(self.work(cfg, [iss(2, ["deputy:wip"], ["me"])]))
        self.assertFalse(self.work(cfg, [iss(3, ["bug"]), iss(4, ["deputy:epic", "deputy:agreed"])]))
        self.assertFalse(self.work(cfg, []))

    def test_watch_without_work_does_not_revive(self):
        out = io.StringIO()
        with mock.patch.object(d, "repo_root", return_value="r"), \
                mock.patch.object(d, "is_stopped", return_value=False), \
                mock.patch.object(d, "has_work", return_value=False), \
                mock.patch.object(d, "session_states", side_effect=AssertionError("되살리면 안 됨")), \
                contextlib.redirect_stdout(out):
            d.cmd_watch(CFG, argparse.Namespace(dry=False, loop=0, cooldown=900, grace=180))
        self.assertIn("할 일이 없어", out.getvalue())


class TestWindowsSleep(unittest.TestCase):
    KO = ("    가능한 최소 설정: 0x00000000\n    가능한 최대 설정: 0xffffffff\n"
          "    현재 AC 전원 설정 색인: 0x{ac}\n    현재 DC 전원 설정 색인: 0x00000384\n")
    EN = "    Current AC Power Setting Index: 0x{ac}\n    Current DC Power Setting Index: 0x00000000\n"

    def test_reads_ac_standby_seconds_in_korean_and_english(self):
        self.assertEqual(d.ac_standby_seconds(self.KO.format(ac="00000000")), 0)
        self.assertEqual(d.ac_standby_seconds(self.EN.format(ac="00000708")), 1800)
        self.assertIsNone(d.ac_standby_seconds("알 수 없는 출력"))


class TestSchedule(unittest.TestCase):
    def test_plan_has_start_watch_stop_tasks_with_cmd_files(self):
        plan = d.schedule_plan(root="C:\\work\\repo", start="00:00", stop="09:30", every=10,
                               python="C:\\Python\\python.exe", deputy="C:\\deputy\\bin\\deputy",
                               bash="C:\\Git\\bin\\bash.exe", up="C:\\deputy\\bin\\deputy-up.sh",
                               folder="C:\\home\\.deputy\\schedule", log="C:\\home\\.deputy\\repo.log")
        names = [p["task"] for p in plan]
        self.assertEqual(names, ["\\deputy\\repo\\start", "\\deputy\\repo\\watch", "\\deputy\\repo\\stop"])
        start, watch, stop = plan
        self.assertEqual(start["args"][start["args"].index("/ST") + 1], "00:00")
        # 감시는 시작 한 간격 뒤부터 돈다. 시작과 동시에 돌면 막 띄운 세션을 '없음'으로 보고 또 띄운다
        self.assertEqual(watch["args"][watch["args"].index("/ST") + 1], "00:10")
        self.assertEqual(watch["args"][watch["args"].index("/RI") + 1], "10")
        self.assertEqual(watch["args"][watch["args"].index("/DU") + 1], "09:10")
        self.assertEqual(stop["args"][stop["args"].index("/ST") + 1], "09:30")
        self.assertIn('"C:/deputy/bin/deputy-up.sh" "C:/work/repo"', start["script"])
        self.assertIn("watch", watch["script"])
        self.assertIn('cd /d "C:\\work\\repo"', watch["script"])
        self.assertIn(" stop", stop["script"])
        for p in plan:
            self.assertEqual(p["args"][p["args"].index("/TR") + 1], f'"{p["file"]}"')
            self.assertTrue(p["file"].startswith("C:\\home\\.deputy\\schedule"))

    def test_window_crossing_midnight(self):
        plan = d.schedule_plan(root="/r/x", start="22:00", stop="06:00", every=15, python="p", deputy="dp",
                               bash="b", up="u", folder="f", log="l")
        watch = plan[1]["args"]
        self.assertEqual(watch[watch.index("/ST") + 1], "22:15")
        self.assertEqual(watch[watch.index("/DU") + 1], "07:30")

    def test_rejects_bad_times(self):
        for start, stop in (("25:00", "09:00"), ("00:00", "00:00"), ("0", "09:00")):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                d.schedule_plan(root="r", start=start, stop=stop, every=10, python="p", deputy="dp",
                                bash="b", up="u", folder="f", log="l")


if __name__ == "__main__":
    unittest.main()
