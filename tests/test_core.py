"""Quartermaster's poller logic against a fake tracker: lanes, ordering, approval, comments,
the status file, the wake signal and the run-log reader. Run: python3 -m unittest discover tests"""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from quartermaster import core

from helpers import OWNER, L, Project, comment, issue


class Base(unittest.TestCase):
    overrides: dict = {}

    def setUp(self) -> None:
        self.p = Project(**self.overrides)
        self.addCleanup(self.p.close)
        self.t = self.p.tracker


class WriteStatusTest(Base):
    def test_write_status_merges_and_is_atomic(self) -> None:
        core.write_status(pid=42, phase="starting", interval=120)
        core.write_status(phase="running", run={"issue": "P-16", "claude_pid": 7})
        path = core.STATE_DIR / "status.json"
        status = json.loads(path.read_text())
        self.assertEqual((status["pid"], status["interval"], status["phase"]), (42, 120, "running"))
        self.assertRegex(status["updated"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        core.write_status(run=None)
        self.assertIsNone(json.loads(path.read_text())["run"])
        self.assertEqual(sorted(p.name for p in core.STATE_DIR.iterdir()), ["status.json"])

    def test_write_status_survives_a_corrupt_file(self) -> None:
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "status.json").write_text("{not json")
        core.write_status(phase="polling")
        self.assertEqual(json.loads((core.STATE_DIR / "status.json").read_text())["phase"], "polling")

    def test_runs_per_lane(self) -> None:
        read = lambda: json.loads((core.STATE_DIR / "status.json").read_text())  # noqa: E731
        core.set_lane_run("plan", {"issue": "P-5"})
        core.set_lane_run("code", {"issue": "P-4"})
        self.assertEqual(read()["run"], {"issue": "P-4"})
        core.set_lane_run("code", None, last_run={"issue": "P-4", "rc": 0})
        self.assertEqual((read()["runs"], read()["last_run"]["issue"]), ({"plan": {"issue": "P-5"}}, "P-4"))


class WakeTest(unittest.TestCase):
    def setUp(self) -> None:
        core.WAKE.clear()

    def test_sleep_ends_early_on_wake(self) -> None:
        timer = threading.Timer(0.3, core.WAKE.set)
        timer.start()
        t0 = time.monotonic()
        self.assertTrue(core.sleep_or_wake(30, step=0.1))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertFalse(core.WAKE.is_set())
        timer.join()

    def test_sleep_runs_its_full_length_without_wake(self) -> None:
        t0 = time.monotonic()
        self.assertFalse(core.sleep_or_wake(0.3, step=0.1))
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)


class RunLogTest(unittest.TestCase):
    def _append(self, log: Path, *events: dict) -> None:
        with log.open("a") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")

    def test_a_resumed_session_drops_its_old_result(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "run.jsonl"
            log.touch()
            run = core._RunLog(log)
            self._append(log, {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"id": "b"}]},
                         {"type": "result", "subtype": "success", "result": "waiting"})
            self.assertTrue(run.scan())
            self.assertEqual(run.pending, 1)
            self._append(log, {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
                         {"type": "assistant", "message": {"content": []}})
            self.assertTrue(run.scan())
            self.assertIsNone(run.result)
            self._append(log, {"type": "result", "subtype": "success", "result": "done"})
            self.assertTrue(run.scan())
            self.assertEqual(run.result["result"], "done")

    def test_describe_event_result_line(self) -> None:
        line = json.dumps({"type": "result", "subtype": "success", "num_turns": 12,
                           "total_cost_usd": 1.234, "result": "done"})
        self.assertEqual(core.describe_event(line), ["end     success after 12 turns, $1.23", "result  done"])


class LogNameTest(Base):
    def test_log_names_round_trip_with_dashed_keys(self) -> None:
        name = core.log_name("PROJ-12", "auto-plan")
        ts, key, kind = core.parse_log_name(name)
        self.assertEqual((key, kind), ("PROJ-12", "auto-plan"))
        self.assertEqual(core.lane_of_log(Path(name)), "plan")
        self.assertEqual(core.lane_of_log(Path(core.log_name("42", "resume"))), "code")
        self.assertEqual(core.parse_log_name("20261114-105926--42--fix-plan.jsonl")[1:], ("42", "fix-plan"))


class CommandTest(Base):
    def test_only_owner_non_bot_first_line_commands(self) -> None:
        self.assertEqual(core.command_of(comment("1", "P-1", "/approve go ahead\nmore")), ("approve", "go ahead"))
        self.assertIsNone(core.command_of(comment("2", "P-1", "/approve", who="someone")))
        self.assertIsNone(core.command_of(comment("3", "P-1", "/approve\n<!-- qm-bot -->")))
        self.assertIsNone(core.command_of(comment("4", "P-1", "please /approve")))

    def test_post_comment_marks_and_refuses_a_leading_command(self) -> None:
        core.post_comment("P-1", "Plan posted.")
        self.assertIn("qm-bot", self.t.posted[-1][1])
        with self.assertRaises(SystemExit):
            core.post_comment("P-1", "<!-- qm-plan issue=P-1 rev=1 -->\n/approve this")

    def test_a_custom_prefix(self) -> None:
        self.p.cfg.command_prefix = "!"
        self.assertEqual(core.command_of(comment("1", "P-1", "!approve")), ("approve", ""))
        self.assertIsNone(core.command_of(comment("2", "P-1", "/approve")))


class ApprovalTest(Base):
    def test_approve_must_follow_the_current_revision(self) -> None:
        self.p.plan("P-3", rev=2)
        self.t.comment_list += [
            comment("1", "P-3", "plan\n<!-- qm-bot -->\n<!-- qm-plan issue=P-3 rev=1 -->", t="2026-11-01T00:00:01.000Z"),
            comment("2", "P-3", "/approve", t="2026-11-01T00:00:02.000Z"),
            comment("3", "P-3", "plan\n<!-- qm-bot -->\n<!-- qm-plan issue=P-3 rev=2 -->", t="2026-11-01T00:00:03.000Z"),
        ]
        a = core.approval("P-3")
        self.assertFalse(a["approved"])
        self.assertIn("no owner /approve", a["reason"])
        self.t.comment_list.append(comment("4", "P-3", "/approve notes", t="2026-11-01T00:00:04.000Z"))
        self.assertTrue(core.approval("P-3")["approved"])

    def test_someone_elses_approve_does_not_count(self) -> None:
        self.p.plan("P-3")
        self.t.comment_list += [
            comment("1", "P-3", "<!-- qm-bot --> <!-- qm-plan issue=P-3 rev=1 -->", t="2026-11-01T00:00:01.000Z"),
            comment("2", "P-3", "/approve", who="intruder", t="2026-11-01T00:00:02.000Z"),
        ]
        self.assertFalse(core.approval("P-3")["approved"])

    def test_a_fix_round_is_approved_by_the_fix_it_names(self) -> None:
        self.p.plan("P-3", rev=4)
        self.t.comment_list += [
            comment("7", "P-3", "/fix the colour", t="2026-11-01T00:00:01.000Z"),
            comment("8", "P-3", "<!-- qm-bot --> <!-- qm-plan issue=P-3 rev=4 fix=7 -->", t="2026-11-01T00:00:02.000Z"),
        ]
        a = core.approval("P-3")
        self.assertEqual((a["approved"], a.get("via")), (True, "fix"))

    def test_no_plan_file(self) -> None:
        self.assertIn("no plan file", core.approval("P-9")["reason"])


class StatusLabelTest(Base):
    overrides = {"transitions": {"in-progress": "In Progress"}}

    def test_one_status_label_and_its_transition(self) -> None:
        self.t.issues["P-1"] = issue("P-1", L("planning"), "frontend")
        core.set_status("P-1", "in-progress")
        self.assertEqual(sorted(self.t.issues["P-1"]["labels"]), sorted(["frontend", L("in-progress")]))
        self.assertEqual(self.t.transitions, [("P-1", "In Progress")])
        core.set_status("P-1", L("done"))  # the full label name works too
        self.assertEqual(sorted(self.t.issues["P-1"]["labels"]), sorted(["frontend", L("done")]))
        core.set_status("P-1", "none")
        self.assertEqual(self.t.issues["P-1"]["labels"], ["frontend"])
        with self.assertRaises(SystemExit):
            core.set_status("P-1", "awaiting-push")


class ChooseActionsTest(Base):
    """The two lanes: planning runs go alongside the one coding run."""

    def setUp(self) -> None:
        super().setUp()
        for k in ("P-4", "P-6", "P-7", "P-8"):  # these have plans; others are open to auto-planning
            self.p.plan(k)

    def choose(self, busy, comments, issues, state=None):
        state = state if state is not None else {"initialized": "x", "seen": []}
        actions = core.choose_actions(state, busy, comments, {i["key"]: i for i in issues})
        return [(kind, k) for kind, k, _, _ in actions], state

    def test_a_plan_command_runs_while_the_code_lane_is_busy(self) -> None:
        got, state = self.choose({"code": "P-4"}, [comment("1", "P-5", "/plan"), comment("2", "P-6", "/approve")],
                                 [issue("P-4", L("in-progress")), issue("P-5"), issue("P-6", L("awaiting-approval"))])
        self.assertEqual(got, [("plan", "P-5")])
        self.assertEqual(state["seen"], ["1"])

    def test_both_lanes_start_when_both_are_free(self) -> None:
        got, _ = self.choose({}, [comment("1", "P-6", "/approve"), comment("2", "P-5", "/plan"),
                                  comment("3", "P-7", "/revise x")], [issue("P-5"), issue("P-6"), issue("P-7")])
        self.assertEqual(got, [("approve", "P-6"), ("plan", "P-5")])

    def test_an_issues_commands_keep_their_order(self) -> None:
        got, state = self.choose({"plan": "P-5"}, [comment("1", "P-7", "/revise more"), comment("2", "P-7", "/approve")],
                                 [issue("P-5"), issue("P-7", L("awaiting-approval"))])
        self.assertEqual((got, state["seen"]), ([], []))

    def test_an_issue_never_has_two_runs(self) -> None:
        got, _ = self.choose({"code": "P-7"}, [comment("1", "P-7", "/revise more")], [issue("P-7", L("in-progress"))])
        self.assertEqual(got, [])
        got, _ = self.choose({"plan": "P-7"}, [], [issue("P-7", L("approved"))])
        self.assertEqual(got, [])

    def test_resume_goes_in_the_code_lane_beside_a_planning_run(self) -> None:
        got, _ = self.choose({"plan": "P-5"}, [], [issue("P-5", L("planning")), issue("P-8", L("in-progress"))])
        self.assertEqual(got, [("resume", "P-8")])

    def test_a_plan_being_worked_finishes_before_another_approve(self) -> None:
        issues = [issue("P-6", L("awaiting-approval")), issue("P-8", L("in-progress"))]
        got, state = self.choose({}, [comment("1", "P-6", "/approve")], issues)
        self.assertEqual((got, state["seen"]), ([("resume", "P-8")], []))
        got, state = self.choose({}, [comment("1", "P-6", "/approve")], [issue("P-6", L("awaiting-approval")), issue("P-8")])
        self.assertEqual((got, state["seen"]), ([("approve", "P-6")], ["1"]))

    def test_resume_prefers_the_in_progress_plan_then_the_oldest(self) -> None:
        got, _ = self.choose({}, [], [issue("P-9", L("approved")), issue("P-8", L("in-progress"))])
        self.assertEqual(got, [("resume", "P-8")])
        got, _ = self.choose({}, [], [issue("P-9", L("approved"), created="2026-01-01T00:00:00.000Z"),
                                      issue("P-7", L("approved"), created="2026-02-01T00:00:00.000Z")])
        self.assertEqual(got, [("resume", "P-9")])  # oldest by creation, not by key

    def test_a_stuck_plan_stops_holding_the_code_lane(self) -> None:
        state = {"initialized": "x", "seen": [], "resumes": {"P-8": 3}}
        got, _ = self.choose({}, [comment("1", "P-6", "/approve")],
                             [issue("P-6", L("awaiting-approval")), issue("P-8", L("in-progress"))], state)
        self.assertEqual(got, [("approve", "P-6")])

    def test_a_fix_plans_in_the_plan_lane(self) -> None:
        got, _ = self.choose({"code": "P-4"}, [comment("1", "P-7", "/fix the colour")], [issue("P-4"), issue("P-7")])
        self.assertEqual(got, [("fix", "P-7")])

    def test_owner_answer_to_needs_info_waits_for_the_plan_lane(self) -> None:
        issues = [issue("P-5", L("needs-info")), issue("P-9")]
        got, state = self.choose({"plan": "P-9"}, [comment("1", "P-5", "Blue, please.")], issues)
        self.assertEqual((got, state["seen"]), ([], []))
        got, state = self.choose({"code": "P-9"}, [comment("1", "P-5", "Blue, please.")], issues)
        self.assertEqual((got, state["seen"]), ([("plan", "P-5")], ["1"]))

    def test_status_is_answered_even_during_that_issues_run(self) -> None:
        got, _ = self.choose({"code": "P-4", "plan": "P-5"}, [comment("1", "P-4", "/status")], [issue("P-4"), issue("P-5")])
        self.assertEqual(got, [("status", "P-4")])

    def test_push_is_not_a_command(self) -> None:
        got, _ = self.choose({}, [comment("1", "P-6", "/push")], [issue("P-6")])
        self.assertEqual(got, [("help", "P-6")])

    def test_auto_plan_uses_the_free_plan_lane(self) -> None:
        self.p.cfg.auto_plan = True
        got, _ = self.choose({"code": "P-4"}, [], [issue("P-4", L("in-progress")), issue("P-11"),
                                                   issue("P-12", author="someone")])
        self.assertEqual(got, [("auto-plan", "P-11")])

    def test_parallel_off_runs_one_session_at_a_time(self) -> None:
        self.p.cfg.parallel = False
        got, _ = self.choose({"code": "P-4"}, [comment("1", "P-5", "/plan")], [issue("P-4"), issue("P-5")])
        self.assertEqual(got, [])
        got, _ = self.choose({}, [comment("1", "P-6", "/approve"), comment("2", "P-5", "/plan")], [issue("P-5"), issue("P-6")])
        self.assertEqual(got, [("approve", "P-6")])

    def test_the_first_poll_only_marks_comments_seen(self) -> None:
        got, state = self.choose({}, [comment("1", "P-5", "/plan")], [issue("P-5")], state={})
        self.assertEqual((got, state["seen"]), ([], ["1"]))


class ExecuteTest(Base):
    def test_approve_without_approval_refuses_with_a_comment(self) -> None:
        self.t.issues["P-2"] = issue("P-2")
        with mock.patch.object(core, "run_claude") as run:
            core.execute(("approve", "P-2", "", comment("5", "P-2", "/approve")), {"resumes": {}})
        run.assert_not_called()
        self.assertIn("not starting", self.t.posted[-1][1])

    def test_commands_come_from_the_config(self) -> None:
        self.p.cfg.commands["plan"] = "/my-triage {key} --deep"
        with mock.patch.object(core, "run_claude", return_value=0) as run:
            core.execute(("plan", "P-2", "", None), {"resumes": {}})
        self.assertEqual(run.call_args[0][2], "/my-triage P-2 --deep")

    def test_the_run_environment(self) -> None:
        self.p.cfg.run_env = {"A": "1"}
        self.p.cfg.plan_env = {"JOBS": "2"}
        env = core.run_env("plan", "P-2")
        self.assertEqual((env["A"], env["JOBS"], env["QM_ISSUE"], env["QM_LANE"]), ("1", "2", "P-2", "plan"))
        self.assertEqual(env["QM_CONFIG"], str(self.p.cfg.path))
        self.assertNotIn("JOBS", core.run_env("code", "P-2"))
        self.assertIn("Never push", core.headless_note("code"))
        self.assertIn("planning run", core.headless_note("plan"))


class LanesTest(Base):
    def test_lanes_run_side_by_side_and_wake_the_poll_loop_when_done(self) -> None:
        core.WAKE.clear()
        lanes = core.Lanes()
        go = threading.Event()
        lanes.start("code", "P-4", go.wait)
        lanes.start("plan", "P-5", go.wait)
        self.assertEqual(lanes.busy(), {"code": "P-4", "plan": "P-5"})
        go.set()
        lanes.join()
        self.assertEqual(lanes.busy(), {})
        self.assertTrue(core.WAKE.is_set())
        core.WAKE.clear()

    def test_a_failing_run_frees_its_lane(self) -> None:
        lanes = core.Lanes()
        with mock.patch("sys.stderr"):
            lanes.start("plan", "P-5", lambda: 1 / 0)
            lanes.join()
        self.assertEqual(lanes.busy(), {})
        self.assertIn("plan lane, P-5", json.loads((core.STATE_DIR / "status.json").read_text())["last_error"])
        core.WAKE.clear()


if __name__ == "__main__":
    unittest.main()
