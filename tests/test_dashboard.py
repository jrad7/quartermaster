"""Tests for the dashboard's API, guards and page.

HTTP tests start the server on port 0 in a thread; systemctl and journalctl are mocked, the
tracker is helpers.FakeTracker, and STATE_DIR / PLAN_DIR are a temp project's.
"""

from __future__ import annotations

import http.client
import json
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import Project, issue

from quartermaster import dashboard
from quartermaster.trackers import TrackerError

PLAN = """# Issue 9: Something

**Status:** in-progress 2026-11-11
**Revision:** 2
**Branch:** fix/thing
**Type:** bug

## Plan

- [x] **I9.1 First task** *(Done 2026-11-11, abc1234 on fix/thing)*
  - **Why:** because.
- [ ] **I9.2 Second task**

### Round 2

- [ ] **I9.3 Round two task**
"""

ACTIVE = {"managed": True, "unit": "qm-test", "ActiveState": "active", "SubState": "running", "MainPID": 0}


def result_line(**kw) -> str:
    ev = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
          "total_cost_usd": 0.5, "duration_ms": 1234, "result": "ok"}
    ev.update(kw)
    return json.dumps(ev) + "\n"


def say_line(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


class Env(unittest.TestCase):
    """A temp project (state dir with logs/, plan dir with completed/); systemctl mocked."""

    SERVICE: dict | None = ACTIVE
    OVERRIDES: dict = {"dashboard": {"service": "qm-test"}}

    def setUp(self) -> None:
        self.project = Project(**self.OVERRIDES)
        self.addCleanup(self.project.close)
        self.tracker = self.project.tracker
        self.state = self.project.cfg.state_dir
        self.plans = self.project.cfg.plan_dir
        (self.state / "logs").mkdir(parents=True)
        (self.plans / "completed").mkdir(parents=True)
        patches = [mock.patch.object(dashboard, "CACHE", dashboard.Cache()),
                   mock.patch.object(dashboard, "_SUMMARY_CACHE", {})]
        if self.SERVICE is not None:
            patches.append(mock.patch.object(dashboard, "service_state", return_value=dict(self.SERVICE)))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def log(self, name: str, text: str) -> Path:
        p = self.state / "logs" / name
        p.write_text(text)
        return p


class HttpEnv(Env):
    def setUp(self) -> None:
        super().setUp()
        for patch in (mock.patch.object(dashboard.Handler, "log_message", lambda *a: None),
                      mock.patch.object(dashboard, "log_action", lambda *a: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = dashboard.make_server(0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str, host: str | None = None) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def post(self, path: str, headers: dict | None = None, body: bytes = b"{}") -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest("POST", path, skip_host=True)
        hdrs = {"Host": f"127.0.0.1:{self.port}", "X-QM-Dashboard": "1",
                "Content-Type": "application/json", "Content-Length": str(len(body))}
        hdrs.update(headers or {})
        for k, v in hdrs.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders()
        conn.send(body)
        r = conn.getresponse()
        data = json.loads(r.read() or b"null")
        conn.close()
        return r.status, data


def spawn(code: str, *extra: str) -> subprocess.Popen:
    """A python child with argv extras, so its /proc cmdline can look like something else."""
    return subprocess.Popen([sys.executable, "-c", code, *extra], stdout=subprocess.DEVNULL)


def reap(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


CATCHER = ("import signal, sys, time\n"
           "signal.signal(signal.SIGUSR1, lambda *a: (print('woke', flush=True), sys.exit(7)))\n"
           "time.sleep(30)")


class GuardTest(HttpEnv):
    def test_host_guard_rejects_foreign_host(self) -> None:
        self.assertEqual(self.get("/api/overview", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.get("/api/overview", host="127.0.0.1:1")[0], 403)
        status, headers, body = self.get("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        data = json.loads(body)
        self.assertEqual(data["service"]["ActiveState"], "active")
        self.assertEqual((data["project"], data["tracker"], data["owner"]), ("test", "fake tracker", "owner-1"))
        self.assertEqual(self.get("/api/overview", host=f"localhost:{self.port}")[0], 200)

    def test_static_path_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "static"
            root.mkdir()
            (root / "index.html").write_text("hi")
            (Path(d) / "secret.txt").write_text("no")
            self.assertEqual(dashboard.static_path("/", root), (root / "index.html").resolve())
            for bad in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/a/../../secret.txt",
                        "/" + str(Path(d) / "secret.txt"), "/missing.js", "/x%00.html"):
                self.assertIsNone(dashboard.static_path(bad, root), bad)
        # Over HTTP: traversal never reaches a file outside static/.
        self.assertEqual(self.get("/../dashboard.py")[0], 404)
        self.assertEqual(self.get("/%2e%2e/dashboard.py")[0], 404)


class RunsTest(HttpEnv):
    def test_run_name_rejects_paths(self) -> None:
        good = "20261111-235210--16--auto-plan.jsonl"
        self.assertTrue(dashboard.valid_run_name(good))
        for bad in ("../state.json", "x/" + good, good + "/x", "20261111-235210--16--auto plan.jsonl",
                    "20261111-235210--16--Auto.jsonl", "status.json", "..%2fstatus.json",
                    "20261111-235210-issue16-plan.jsonl", "20261111-235210--../x--plan.jsonl"):
            self.assertFalse(dashboard.valid_run_name(bad), bad)
        (self.state / "status.json").write_text("{}")
        self.assertEqual(self.get("/api/runs/..%2fstatus.json")[0], 404)
        self.assertEqual(self.get("/api/runs/status.json")[0], 404)

    def test_run_summary_from_result_tail(self) -> None:
        filler = say_line("x" * 1000) * 400  # ~400 KB, more than the tail window
        self.log("20261111-100000--9--resume.jsonl", filler + result_line(num_turns=12, total_cost_usd=2.5))
        self.log("20261111-110000--10--plan.jsonl", say_line("still going"))
        (self.state / "status.json").write_text(json.dumps(
            {"run": {"log": str(self.state / "logs" / "20261111-110000--10--plan.jsonl")}}))
        status, _, body = self.get("/api/runs")
        self.assertEqual(status, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual([r["name"] for r in runs],
                         ["20261111-110000--10--plan.jsonl", "20261111-100000--9--resume.jsonl"])
        live, done = runs
        self.assertTrue(live["live"])
        self.assertIsNone(live["subtype"])
        self.assertFalse(done["live"])
        self.assertEqual((done["issue"], done["kind"], done["num_turns"], done["total_cost_usd"],
                          done["subtype"], done["is_error"], done["duration_ms"]),
                         ("9", "resume", 12, 2.5, "success", False, 1234))
        self.assertEqual(done["started"], "2026-11-11T10:00:00")
        only = json.loads(self.get("/api/runs?issue=9")[2])["runs"]
        self.assertEqual([r["issue"] for r in only], ["9"])

    def test_string_keys_in_run_names_and_filters(self) -> None:
        # Jira keys: "PROJ-12" is not "PROJ-1", and "--" separates the key from the kind.
        self.log("20261112-100000--PROJ-12--auto-plan.jsonl", say_line("a") + result_line())
        self.log("20261112-110000--PROJ-1--fix-plan.jsonl", say_line("b") + result_line())
        self.log("20261112-120000--PROJ-12--resume.jsonl", say_line("c"))
        runs = json.loads(self.get("/api/runs?issue=PROJ-12")[2])["runs"]
        self.assertEqual([(r["issue"], r["kind"]) for r in runs], [("PROJ-12", "resume"), ("PROJ-12", "auto-plan")])
        self.assertEqual([r["lane"] for r in runs], ["code", "plan"])
        one = json.loads(self.get("/api/runs?issue=PROJ-1")[2])["runs"]
        self.assertEqual([(r["issue"], r["kind"]) for r in one], [("PROJ-1", "fix-plan")])
        status, _, body = self.get("/api/runs/20261112-110000--PROJ-1--fix-plan.jsonl")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["lines"][0]["text"], "say     b")
        # A key that is not a key is no filter at all, never a path.
        self.assertEqual(len(json.loads(self.get("/api/runs?issue=..%2fx")[2])["runs"]), 3)

    def test_run_lines_from_offset(self) -> None:
        p = self.log("20261111-100000--9--resume.jsonl", say_line("one") + say_line("two"))
        first = dashboard.read_run_lines(p, 0, raw=False)
        self.assertEqual([x["text"] for x in first["lines"]], ["say     one", "say     two"])
        self.assertEqual(first["next"], p.stat().st_size)
        # The file grows, with a partly written last line.
        partial = say_line("four")
        with p.open("a") as fh:
            fh.write(say_line("three") + partial[:10])
        second = dashboard.read_run_lines(p, first["next"], raw=False)
        self.assertEqual([x["text"] for x in second["lines"]], ["say     three"])
        with p.open("a") as fh:
            fh.write(partial[10:] + result_line(is_error=True, subtype="error_max_turns"))
        third = dashboard.read_run_lines(p, second["next"], raw=False)
        self.assertEqual([x["text"] for x in third["lines"]][0], "say     four")
        self.assertEqual([x["kind"] for x in third["lines"]], ["say", "end", "result"])
        self.assertTrue(third["lines"][1]["error"])
        self.assertEqual(third["next"], p.stat().st_size)
        # Raw mode over HTTP gives the JSON lines themselves.
        status, _, body = self.get(f"/api/runs/{p.name}?from=0&raw=1")
        self.assertEqual(status, 200)
        raw = json.loads(body)["lines"]
        self.assertEqual(len(raw), 5)
        self.assertEqual(json.loads(raw[0])["type"], "assistant")

    def test_run_stream_ends_after_result(self) -> None:
        p = self.log("20261111-100000--9--resume.jsonl", say_line("hello") + result_line())
        status, headers, body = self.get(f"/api/runs/{p.name}/stream")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        text = body.decode()
        self.assertIn("say     hello", text)
        self.assertIn("event: end", text)


class ControlsTest(HttpEnv):
    def test_post_requires_custom_header_and_json(self) -> None:
        with mock.patch.object(dashboard.subprocess, "run") as run:
            self.assertEqual(self.post("/api/service/start", {"X-QM-Dashboard": None})[0], 403)
            self.assertEqual(self.post("/api/service/start", {"X-QM-Dashboard": "0"})[0], 403)
            self.assertEqual(self.post("/api/service/start", {"X-QM-Dashboard": None, "X-WDI-Dashboard": "1"})[0], 403)
            for ctype in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                self.assertEqual(self.post("/api/service/start", {"Content-Type": ctype})[0], 403, ctype)
            self.assertEqual(self.post("/api/service/start", {"Host": f"evil.example:{self.port}"})[0], 403)
            self.assertEqual(self.post("/api/service/start", body=b"not json")[0], 400)
            run.assert_not_called()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("OPTIONS", "/api/service/start", headers={"Host": f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 405)
        self.assertFalse(any(k.lower().startswith("access-control-") for k, _ in r.getheaders()))

    def test_post_rejects_foreign_origin(self) -> None:
        with mock.patch.object(dashboard.subprocess, "run") as run:
            for origin in ("http://evil.example", f"http://evil.example:{self.port}", "null",
                           f"https://127.0.0.1:{self.port}", "http://127.0.0.1:1"):
                self.assertEqual(self.post("/api/service/start", {"Origin": origin})[0], 403, origin)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.assertEqual(self.post("/api/service/start", {"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_service_action_argv_is_fixed(self) -> None:
        with mock.patch.object(dashboard.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            for verb in ("start", "stop", "restart"):
                status, data = self.post(f"/api/service/{verb}")
                self.assertEqual((status, data["ok"]), (200, True))
                self.assertEqual(run.call_args.args[0], ["sudo", "-n", "systemctl", verb, "qm-test"])
            self.assertEqual(run.call_count, 3)
            self.assertEqual(self.post("/api/service/enable")[0], 404)
            self.assertEqual(self.post("/api/service/start;reboot")[0], 404)
            self.assertEqual(run.call_count, 3)
            run.return_value = subprocess.CompletedProcess([], 1, "", "sudo: a password is required\n")
            status, data = self.post("/api/service/stop")
            self.assertFalse(data["ok"])
            self.assertIn("sudoers", data["output"])

    def test_poll_now_signals_only_a_watch_poller_that_catches_usr1(self) -> None:
        bare = spawn("import time; time.sleep(30)", "/x/quartermaster", "watch")
        self.addCleanup(reap, bare)
        state = dict(ACTIVE, MainPID=bare.pid)
        with mock.patch.object(dashboard, "service_state", return_value=state):
            time.sleep(0.3)
            status, data = self.post("/api/poller/poll-now")
            self.assertEqual(status, 409)  # no SIGUSR1 handler: signalling would kill it
            self.assertIn("SIGUSR1", data["error"])
            self.assertIsNone(bare.poll())
            good = spawn(CATCHER, "/x/bin/quartermaster", "--config", "/x/quartermaster.json", "watch")
            self.addCleanup(reap, good)
            other = spawn(CATCHER, "/x/quartermaster", "once")
            self.addCleanup(reap, other)
            time.sleep(0.5)
            state["MainPID"] = other.pid
            self.assertEqual(self.post("/api/poller/poll-now")[0], 409)
            state["MainPID"] = good.pid
            status, data = self.post("/api/poller/poll-now")
            self.assertEqual((status, data["pid"]), (200, good.pid))
            self.assertEqual(good.wait(timeout=5), 7)
            state["ActiveState"] = "inactive"
            self.assertEqual(self.post("/api/poller/poll-now")[0], 409)

    def test_is_watch_poller(self) -> None:
        self.assertTrue(dashboard.is_watch_poller(["/usr/bin/python3", "-m", "quartermaster", "watch"]))
        self.assertTrue(dashboard.is_watch_poller(["/home/u/.local/bin/quartermaster", "watch"]))
        self.assertFalse(dashboard.is_watch_poller(["/usr/bin/python3", "-m", "quartermaster", "once"]))
        self.assertFalse(dashboard.is_watch_poller(["/usr/bin/python3", "other.py", "watch"]))

    def test_stop_run_refuses_pid_mismatch(self) -> None:
        self.assertEqual(self.post("/api/poller/stop-run")[0], 409)  # no status.json
        log = self.state / "logs" / "20261111-100000--9--resume.jsonl"
        other = self.state / "logs" / "20261111-090000--9--plan.jsonl"
        fake_claude = self.state / "claude"
        fake_claude.symlink_to(sys.executable)
        self.project.cfg.claude = str(fake_claude)
        with log.open("w") as fh, other.open("w") as fh2:
            run = subprocess.Popen([str(fake_claude), "-c", "import time; time.sleep(60)", "-p", "x"], stdout=fh)
            wrong = subprocess.Popen([str(fake_claude), "-c", "import time; time.sleep(60)", "-p", "x"], stdout=fh2)
            notp = subprocess.Popen([str(fake_claude), "-c", "import time; time.sleep(60)"], stdout=fh)
        for p in (run, wrong, notp):
            self.addCleanup(reap, p)

        def status(pid: int) -> None:
            (self.state / "status.json").write_text(json.dumps(
                {"run": {"claude_pid": pid, "log": str(log), "issue": "9"}}))

        for pid in (wrong.pid, notp.pid, 999999999):
            status(pid)
            self.assertEqual(self.post("/api/poller/stop-run")[0], 409, pid)
        self.assertIsNone(wrong.poll())
        self.assertIsNone(notp.poll())
        status(run.pid)
        code, data = self.post("/api/poller/stop-run")
        self.assertEqual((code, data["pid"]), (200, run.pid))
        self.assertEqual(run.wait(timeout=15), -signal.SIGTERM)
        self.assertIsNone(wrong.poll())

    # Two lanes: a planning run goes alongside the coding run.
    def test_two_lanes_both_live_and_stop_needs_a_lane(self) -> None:
        code = self.state / "logs" / "20261114-100000--9--resume.jsonl"
        plan = self.state / "logs" / "20261114-100500--12--plan.jsonl"
        self.log(code.name, say_line("coding"))
        self.log(plan.name, say_line("planning"))
        self.log("20261114-090000--9--resume.jsonl", say_line("x") + result_line())
        (self.state / "status.json").write_text(json.dumps({"runs": {
            "code": {"issue": "9", "lane": "code", "claude_pid": 999999998, "log": str(code)},
            "plan": {"issue": "12", "lane": "plan", "claude_pid": 999999999, "log": str(plan)}}}))
        runs = {r["name"]: r["live"] for r in json.loads(self.get("/api/runs")[2])["runs"]}
        self.assertEqual(runs, {code.name: True, plan.name: True, "20261114-090000--9--resume.jsonl": False})
        code_, data = self.post("/api/poller/stop-run")
        self.assertEqual(code_, 409)
        self.assertIn("name one", data["error"])
        self.assertEqual(self.post("/api/poller/stop-run", body=b'{"lane": "other"}')[0], 400)
        code_, data = self.post("/api/poller/stop-run", body=b'{"lane": "plan"}')
        self.assertEqual(code_, 409)  # that pid is not a live claude run
        self.assertIn("999999999", data["error"])

    def test_a_lone_run_counts_as_the_code_lane(self) -> None:
        self.assertEqual(dashboard.active_runs({"run": {"issue": "3", "log": "/x/a.jsonl"}}),
                         {"code": {"issue": "3", "log": "/x/a.jsonl"}})
        self.assertEqual(dashboard.live_logs({"run": None, "runs": {}}), set())


class NoServiceTest(HttpEnv):
    """A config without dashboard.service: no systemd controls, no journal, and the poller
    found through the pid in its status file instead."""

    SERVICE = None
    OVERRIDES: dict = {}

    def test_service_actions_and_journal_are_409(self) -> None:
        self.assertEqual(dashboard.service_state(), {"managed": False})
        with mock.patch.object(dashboard.subprocess, "run") as run, \
                mock.patch.object(dashboard.subprocess, "Popen") as popen:
            for verb in ("start", "stop", "restart"):
                status, data = self.post(f"/api/service/{verb}")
                self.assertEqual(status, 409, verb)
                self.assertIn("not managed by systemd", data["error"])
            self.assertEqual(self.post("/api/service/enable")[0], 404)
            for path in ("/api/journal", "/api/journal/stream"):
                status, _, body = self.get(path)
                self.assertEqual(status, 409, path)
                self.assertIn("not managed by systemd", json.loads(body)["error"])
            run.assert_not_called()
            popen.assert_not_called()
        ov = json.loads(self.get("/api/overview")[2])
        self.assertEqual(ov["service"], {"managed": False})
        self.assertIsNone(ov["poller"])

    def test_poll_now_uses_the_status_file_pid(self) -> None:
        self.assertEqual(self.post("/api/poller/poll-now")[0], 409)  # no status file
        good = spawn(CATCHER, "/x/quartermaster", "watch")
        self.addCleanup(reap, good)
        time.sleep(0.5)
        (self.state / "status.json").write_text(json.dumps({"pid": good.pid, "phase": "sleeping"}))
        poller = json.loads(self.get("/api/overview")[2])["poller"]
        self.assertTrue(poller["alive"])
        status, data = self.post("/api/poller/poll-now")
        self.assertEqual((status, data["pid"]), (200, good.pid))
        self.assertEqual(good.wait(timeout=5), 7)
        dashboard.CACHE.drop("overview")
        self.assertFalse(json.loads(self.get("/api/overview")[2])["poller"]["alive"])
        self.assertEqual(self.post("/api/poller/poll-now")[0], 409)


class PlanTest(HttpEnv):
    def test_parse_plan_tasks_and_rounds(self) -> None:
        data = dashboard.parse_plan(PLAN)
        self.assertEqual((data["status"], data["revision"], data["branch"], data["type"], data["size"]),
                         ("in-progress 2026-11-11", "2", "fix/thing", "bug", None))
        self.assertEqual(data["tasks"], [
            {"id": "I9.1", "title": "First task", "done": True, "round": None},
            {"id": "I9.2", "title": "Second task", "done": False, "round": None},
            {"id": "I9.3", "title": "Round two task", "done": False, "round": "2"},
        ])

    def test_task_ids_are_any_first_word(self) -> None:
        text = ("- [x] **PROJ-12.1 Add the thing**\n- [ ] **T3 Wire it**\n### Round 2b\n"
                "- [ ] **cleanup**\n- [ ] not a task\n")
        self.assertEqual(dashboard.parse_plan(text)["tasks"], [
            {"id": "PROJ-12.1", "title": "Add the thing", "done": True, "round": None},
            {"id": "T3", "title": "Wire it", "done": False, "round": None},
            {"id": "cleanup", "title": "", "done": False, "round": "2b"},
        ])

    def test_string_keys_in_plans(self) -> None:
        (self.plans / "issue-PROJ-12.md").write_text(PLAN.replace("I9.", "PROJ-12."))
        (self.plans / "completed" / "issue-PROJ-3.md").write_text(PLAN)
        (self.plans / "issue-bad key.md").write_text(PLAN)
        self.tracker.issues["PROJ-12"] = issue("PROJ-12", "claude-in-progress", "bug")
        data = dashboard.issues_payload(refresh=True)
        by_key = {i["key"]: i for i in data["issues"]}
        self.assertEqual(list(by_key), ["PROJ-3", "PROJ-12"])  # natural order
        self.assertEqual(by_key["PROJ-12"]["labels"], ["claude-in-progress"])
        self.assertEqual(by_key["PROJ-12"]["url"], "https://example.test/PROJ-12")
        self.assertEqual(by_key["PROJ-12"]["plan"]["tasks"][0]["id"], "PROJ-12.1")
        self.assertEqual(by_key["PROJ-3"]["url"], "https://example.test/PROJ-3")  # from T.issue_url
        status, headers, body = self.get("/api/plans/PROJ-12")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn(b"PROJ-12.1 First task", body)
        self.assertEqual(self.get("/api/plans/PROJ-3")[0], 200)
        self.assertEqual(self.get("/api/plans/PROJ-99")[0], 404)
        self.assertEqual(self.get("/api/plans/..%2fx")[0], 404)

    def test_issues_include_closed_plans_and_survive_tracker_errors(self) -> None:
        (self.plans / "issue-P-9.md").write_text(PLAN)
        (self.plans / "completed" / "issue-P-3.md").write_text(PLAN.replace("I9.", "I3."))
        self.tracker.issues["P-9"] = issue("P-9", "claude-in-progress", "bug")
        data = dashboard.issues_payload(refresh=True)
        self.assertIsNone(data["tracker_error"])
        self.assertEqual(data["label_prefix"], "claude-")
        by_key = {i["key"]: i for i in data["issues"]}
        self.assertEqual(by_key["P-9"]["labels"], ["claude-in-progress"])
        self.assertEqual(len(by_key["P-9"]["plan"]["tasks"]), 3)
        self.assertFalse(by_key["P-3"]["open"])
        self.assertTrue(by_key["P-3"]["plan"]["completed"])
        with mock.patch.object(self.tracker, "open_issues", side_effect=TrackerError("offline")):
            data = dashboard.issues_payload(refresh=True)
        self.assertEqual(data["tracker_error"], "offline")
        self.assertEqual(sorted(i["key"] for i in data["issues"]), ["P-3", "P-9"])
        self.assertIsNone(data["issues"][0]["open"])

    def test_key_order_is_natural(self) -> None:
        self.assertEqual(sorted(["10", "9", "100"], key=dashboard.key_order), ["9", "10", "100"])
        self.assertEqual(sorted(["P-10", "P-9", "A-2"], key=dashboard.key_order), ["A-2", "P-9", "P-10"])


class SessionsTest(Env):
    def test_classify_claude_argv(self) -> None:
        cases = {
            ("claude", "rc"): "interactive",
            ("claude", "--dangerously-skip-permissions"): "interactive",
            ("/home/u/.local/bin/claude", "daemon", "run", "--origin", "transient"): "daemon",
            ("claude", "bg-spare", "--x"): "background",
            ("claude", "bg-pty-host"): "background",
            ("claude", "-p", "/qm-work PROJ-16", "--output-format", "stream-json"): "headless",
            ("/home/u/.local/bin/claude", "--output-format", "stream-json", "--model=haiku",
             "--no-session-persistence"): "helper",
            ("/home/u/.local/bin/claude", "--output-format", "stream-json", "--input-format", "stream-json",
             "--model=haiku"): "helper",
        }
        for argv, want in cases.items():
            self.assertEqual(dashboard.classify_argv(list(argv)), want, argv)

    def test_clip_argv_drops_system_prompt(self) -> None:
        argv = ["claude", "-p", "x", "--append-system-prompt", "long secret-ish text", "--verbose"]
        self.assertEqual(dashboard.clip_argv(argv), ["claude", "-p", "x", "--append-system-prompt", "…", "--verbose"])

    def test_journal_argv_is_fixed_and_capped(self) -> None:
        self.assertEqual(dashboard.journal_argv(5000),
                         ["journalctl", "-u", "qm-test", "-n", "2000", "--no-pager", "-o", "short-iso"])
        self.assertEqual(dashboard.journal_argv(0, follow=True)[-1], "-f")

    def test_sessions_lists_without_error(self) -> None:
        self.assertIsInstance(dashboard.sessions(), list)


PAGE_FILES = {"index.html": "text/html", "app.css": "text/css", "app.js": "text/javascript"}


class PageTest(HttpEnv):
    def test_index_and_assets_served(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(body.count(b'src="app.js"'), 1)
        for name, ctype in PAGE_FILES.items():
            status, headers, body = self.get(f"/{name}")
            self.assertEqual(status, 200, name)
            self.assertTrue(headers["Content-Type"].startswith(ctype), (name, headers["Content-Type"]))
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertGreater(len(body), 500, name)

    def test_page_has_no_external_urls_or_project_strings(self) -> None:
        # No CDN, font or script from elsewhere: the only absolute URLs the page shows are
        # built at runtime from API data (the tracker's issue links).
        for name in PAGE_FILES:
            text = (dashboard.STATIC_DIR / name).read_text()
            for m in re.finditer(r"https?://[^\s\"'`)]*", text):
                self.fail(f"{name}: {m.group(0)}")
            self.assertNotIn("@import", text, name)
            for word in ("wdi", "WDI", "wow-dungeon", "github.com", "just web", "free_gb", "/push"):
                self.assertNotIn(word, text, f"{name}: {word}")
        html = (dashboard.STATIC_DIR / "index.html").read_text()
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'self'", html)

    def test_page_posts_send_header(self) -> None:
        js = (dashboard.STATIC_DIR / "app.js").read_text()
        # One POST helper, and it sends the guard header and a JSON body type.
        self.assertEqual(js.count('method: "POST"'), 1)
        helper = js[js.index("async function post("):]
        helper = helper[:helper.index("\n}\n")]
        self.assertIn('"X-QM-Dashboard": "1"', helper)
        self.assertIn('"Content-Type": "application/json"', helper)
        # Untrusted text never goes in as HTML.
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)

    # The Overview re-inserts the current-run log on every refresh, which resets its
    # scrollTop; the pane must re-pin itself afterwards.
    def test_overview_run_log_repins_after_render(self) -> None:
        js = (dashboard.STATIC_DIR / "app.js").read_text()
        pane = js[js.index("class LogPane {"):]
        pane = pane[:pane.index("\n}\n")]
        self.assertIn("pin() {", pane)
        self.assertIn("this.stick = true", pane)
        self.assertIn("if (this.stick)", pane)
        self.assertNotIn("const stick = this.atBottom()", pane)
        render = js[js.index("  render(ov) {"):]
        render = render[:render.index("\n  },\n")]
        reinsert = render.index("this.cards.run.replaceChildren(...rows)")
        self.assertGreater(render.find(".pane.pin();", reinsert), reinsert)

    # Two lanes can run at once: each lane's run gets its own log pane and stream in the
    # Current runs card, so the code run's output never lands under the plan run.
    def test_overview_keeps_one_log_per_lane(self) -> None:
        js = (dashboard.STATIC_DIR / "app.js").read_text()
        render = js[js.index("  render(ov) {"):]
        render = render[:render.index("\n  },\n")]
        self.assertIn("this.followRun(lane, name)", render)
        self.assertIn("log.pane.node", render)
        follow = js[js.index("  async followRun(lane, name) {"):]
        follow = follow[:follow.index("\n  },\n")]
        self.assertIn("const log = this.laneLog(lane);", follow)
        self.assertIn("log.pane.add(lines)", follow)


if __name__ == "__main__":
    unittest.main()
