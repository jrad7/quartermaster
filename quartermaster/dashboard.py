"""A local web dashboard for Quartermaster: `quartermaster dashboard [--port N]`.

Stdlib only. Serves on 127.0.0.1 a JSON API over the poller (its status file
STATE_DIR/status.json, written by core.write_status, and its systemd unit when the config
names one in `dashboard.service`), the issue plans, the run logs (with server-sent-event
streams of a growing log), the Claude processes on this machine and the service journal, plus
the static page in static/ beside this file.

`core.configure(...)` must have run before `serve`; nothing is read at import time.

Every request must carry a loopback Host header naming this port (403 otherwise), so a page
on another site cannot reach the API through DNS rebinding. Issue text, plan text and logs
are untrusted data: the API returns them as JSON strings or text/plain, never as HTML.

Controls are POSTs, which also need the header `X-QM-Dashboard: 1`, a same-host `Origin`
when one is sent, and `Content-Type: application/json` (a cross-site page cannot send that
without a CORS preflight, and OPTIONS is always refused):

    /api/service/{start,stop,restart}   sudo -n systemctl <verb> <service>; 409 when the
                                        config names no service
    /api/poller/poll-now                SIGUSR1 to the poller (only one that handles it)
    /api/poller/stop-run                end a Claude run of the poller and its tree; body
                                        {"lane": "code"|"plan"} when both lanes have one

Every action is logged to stdout. Commands run from fixed argv lists.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core
from .trackers import TrackerError

STATIC_DIR = Path(__file__).resolve().parent / "static"
KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
# `- [x] **<task id> <title>**`: the task id is the first word, whatever its shape.
TASK_RE = re.compile(r"^- \[([ x])\] \*\*([^\s*]+)(?:\s+([^*]*))?\*\*")
ROUND_RE = re.compile(r"^###\s+Round\s+(\S+)")
PLAN_FIELDS = ("Status", "Revision", "Branch", "Type", "Size", "Posted")
MAX_CHUNK = 2 * 1024 * 1024  # input bytes per /api/runs/<name> response
TAIL_BYTES = 256 * 1024  # how far back a run's `result` event is looked for
KINDS = ("start", "say", "tool", "end", "result")


# --- small cache -----------------------------------------------------------------------------


class Cache:
    """Values computed at most once per `ttl` seconds, shared by every request thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str, ttl: float, fn, refresh: bool = False) -> tuple[object, float]:
        """(value, age in seconds)."""
        with self._lock:
            hit = self._data.get(key)
        now = time.monotonic()
        if hit and not refresh and now - hit[0] < ttl:
            return hit[1], now - hit[0]
        value = fn()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value, 0.0

    def drop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


CACHE = Cache()


# --- poller and service ------------------------------------------------------------------------


def service_name() -> str | None:
    return core.CFG.service if core.CFG is not None else None


def service_state() -> dict:
    """`systemctl show` of the poller unit as a dict, `managed` False when the config names
    no unit; `error` when systemctl fails."""
    unit = service_name()
    if not unit:
        return {"managed": False}
    props = "ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts"
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", props],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"managed": True, "unit": unit, "error": str(e)}
    out: dict = {"managed": True, "unit": unit}
    for line in r.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = int(value) if key in ("MainPID", "NRestarts") and value.isdigit() else value
    if r.returncode:
        out["error"] = r.stderr.strip() or f"systemctl exit {r.returncode}"
    return out


def read_status() -> dict | None:
    try:
        status = json.loads((core.STATE_DIR / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return status if isinstance(status, dict) else None


def active_runs(status: dict | None = None) -> dict[str, dict]:
    """lane -> the run going in it, from status.json's `runs` (or a lone `run`, taken as the
    code lane's)."""
    status = read_status() if status is None else status
    status = status or {}
    runs = status.get("runs")
    if isinstance(runs, dict):
        return {lane: r for lane, r in runs.items() if isinstance(r, dict)}
    return {"code": status["run"]} if isinstance(status.get("run"), dict) else {}


def live_logs(status: dict | None = None) -> set[str]:
    """File names of the logs being written by runs going now."""
    return {Path(str(r["log"])).name for r in active_runs(status).values() if r.get("log")}


def _parse_iso(s: object) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return None


def poller_pid(service: dict, status: dict | None = None) -> int:
    """The poller's pid: the unit's MainPID when systemd manages it, else the `pid` its status
    file names (a poller started by hand), when that process is still alive. 0 when unknown."""
    if service.get("managed"):
        return int(service.get("MainPID") or 0) if service.get("ActiveState") == "active" else 0
    status = read_status() if status is None else status
    pid = (status or {}).get("pid")
    return pid if isinstance(pid, int) and pid > 0 and _alive(pid) else 0


def poller_state(service: dict) -> dict | None:
    """status.json, plus `stale` when the poller is up but the file has not moved for over two
    poll intervals and a minute, and `alive` (whether its process is running). A run in
    progress does not write, so it never counts."""
    status = read_status()
    if status is None:
        return None
    alive = bool(poller_pid(service, status))
    updated = _parse_iso(status.get("updated"))
    interval = status.get("interval") if isinstance(status.get("interval"), int) else 120
    status["alive"] = alive
    status["stale"] = bool(
        alive and status.get("phase") != "idle" and updated is not None and not active_runs(status)
        and time.time() - updated > 2 * interval + 60)
    return status


# --- plans -----------------------------------------------------------------------------------


def parse_plan(text: str) -> dict:
    """The header fields and the task list of an issue plan file."""
    out: dict = {}
    for field in PLAN_FIELDS:
        m = re.search(rf"^\*\*{field}:\*\*\s*(.+)$", text, re.M)
        out[field.lower()] = m.group(1).strip() if m else None
    tasks, current_round = [], None
    for line in text.splitlines():
        m = ROUND_RE.match(line)
        if m:
            current_round = m.group(1)
            continue
        m = TASK_RE.match(line)
        if m:
            tasks.append({"id": m.group(2), "title": (m.group(3) or "").strip(), "done": m.group(1) == "x",
                          "round": current_round})
    out["tasks"] = tasks
    return out


def plan_files() -> dict[str, Path]:
    """Issue key -> plan file, active ones winning over completed ones of the same key."""
    out: dict[str, Path] = {}
    for d in (core.PLAN_DIR / "completed", core.PLAN_DIR):
        for p in d.glob("issue-*.md"):
            key = p.name[len("issue-"):-len(".md")]
            if KEY_RE.fullmatch(key):
                out[key] = p
    return out


def plan_summary(path: Path) -> dict:
    data = parse_plan(path.read_text())
    data["file"] = str(path)
    data["completed"] = path.parent.name == "completed"
    return data


def key_order(key: str) -> tuple:
    """Natural order for issue keys: "PROJ-9" before "PROJ-10", "9" before "10"."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", key) if p)


def _issue_url(key: str) -> str | None:
    try:
        return core.T.issue_url(key)
    except (TrackerError, NotImplementedError, OSError):
        return None


def issues_payload(refresh: bool) -> dict:
    error = None
    try:
        issues, age = CACHE.get("issues", 60, core.T.open_issues, refresh)
    except (TrackerError, OSError) as e:
        issues, age, error = [], 0.0, str(e)
    prefix = core.CFG.label_prefix
    plans = plan_files()
    out = []
    for i in issues:  # type: ignore[union-attr]
        key = str(i["key"])
        out.append({
            "key": key, "title": i.get("title"), "url": i.get("url") or _issue_url(key), "open": True,
            "author": i.get("author_name") or i.get("author"), "created": i.get("created"),
            "state": i.get("state"),
            "labels": [x for x in i.get("labels") or [] if x.startswith(prefix)],
            "plan": plan_summary(plans[key]) if key in plans else None,
        })
    open_keys = {x["key"] for x in out}
    for key in set(plans) - open_keys:
        out.append({"key": key, "title": None, "url": _issue_url(key), "open": None if error else False,
                    "author": None, "created": None, "state": None, "labels": [],
                    "plan": plan_summary(plans[key])})
    out.sort(key=lambda x: key_order(x["key"]))
    return {"issues": out, "cache_age": round(age, 1), "tracker_error": error,
            "tracker": core.T.kind, "label_prefix": prefix}


# --- runs ------------------------------------------------------------------------------------


def valid_run_name(name: str) -> bool:
    return core.parse_log_name(name) is not None


def run_path(name: str) -> Path | None:
    if not valid_run_name(name):
        return None
    p = core.STATE_DIR / "logs" / name
    return p if p.is_file() else None


_SUMMARY_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_SUMMARY_LOCK = threading.Lock()


def run_result(path: Path) -> dict | None:
    """The last `result` event in the final TAIL_BYTES of a run log."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read().decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        if '"result"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return ev
    return None


def run_summary(path: Path, live: set[str] | None = None) -> dict:
    st = path.stat()
    key = (st.st_size, st.st_mtime_ns)
    with _SUMMARY_LOCK:
        hit = _SUMMARY_CACHE.get(path.name)
    if hit and hit[0] == key:
        data = dict(hit[1])
    else:
        parsed = core.parse_log_name(path.name)
        assert parsed
        ts, issue, kind = parsed
        d, t = ts.split("-")
        data = {"name": path.name, "issue": issue, "kind": kind, "lane": core.LANE_OF.get(kind, core.CODE),
                "started": f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}",
                "size": st.st_size, "mtime": int(st.st_mtime), "path": str(path)}
        ev = run_result(path) or {}
        for k in ("subtype", "is_error", "num_turns", "total_cost_usd", "duration_ms"):
            data[k] = ev.get(k)
        with _SUMMARY_LOCK:
            _SUMMARY_CACHE[path.name] = (key, data)
        data = dict(data)
    data["live"] = path.name in (live or ())
    return data


def list_runs(issue: str | None = None) -> list[dict]:
    logs = core.STATE_DIR / "logs"
    live = live_logs()
    out = []
    for p in logs.glob("*.jsonl") if logs.is_dir() else []:
        parsed = core.parse_log_name(p.name)
        if not parsed or (issue is not None and parsed[1] != issue):
            continue
        try:
            out.append(run_summary(p, live))
        except OSError:
            continue
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


def tag_line(text: str, is_error: bool = False) -> dict:
    word = text.split(" ", 1)[0]
    kind = word if word in KINDS else "raw"
    return {"kind": kind, "text": text, "error": bool(is_error and kind in ("end", "result"))}


def readable(line: str) -> list[dict]:
    is_error = False
    if '"result"' in line:
        try:
            is_error = bool(json.loads(line).get("is_error"))
        except (json.JSONDecodeError, AttributeError):
            pass
    return [tag_line(t, is_error) for t in core.describe_event(line)]


def read_run_lines(path: Path, start: int, raw: bool) -> dict:
    """Complete lines from byte offset START, at most MAX_CHUNK bytes of input. `next` is the
    offset to ask for next time: a line still being written is left for the next read."""
    size = path.stat().st_size
    start = max(0, min(start, size))
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(MAX_CHUNK)
    end = data.rfind(b"\n") + 1
    if end == 0 and len(data) >= MAX_CHUNK:  # one line longer than the cap: pass it whole
        end = len(data)
    chunk = data[:end].decode("utf-8", "replace")
    lines: list = []
    for line in chunk.splitlines():
        if raw:
            lines.append(line)
        else:
            lines += readable(line)
    return {"lines": lines, "next": start + end, "size": size, "more": start + end < size}


def run_finished(path: Path) -> bool:
    return run_result(path) is not None and path.name not in live_logs()


# --- sessions --------------------------------------------------------------------------------


def _proc_table() -> dict[int, dict]:
    """pid -> {ppid, starttime ticks} for every process."""
    out = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
        except OSError:
            continue
        fields = text[text.rfind(")") + 2:].split()
        out[int(stat.parent.name)] = {"ppid": int(fields[1]), "start": int(fields[19])}
    return out


def _children_map(table: dict[int, dict]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    return children


def _under(pid: int, children: dict[int, list[int]]) -> list[int]:
    out, todo = [], [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def classify_argv(argv: list[str]) -> str:
    """The kind of a `claude` process from its arguments (the poller's runs are found by pid)."""
    rest = argv[1:]
    if rest[:1] == ["daemon"]:
        return "daemon"
    if rest[:1] and rest[0] in ("bg-spare", "bg-pty-host"):
        return "background"
    if "--no-session-persistence" in rest or ("--input-format" in rest and "-p" not in rest):
        return "helper"  # SDK helpers: fed stream-json on stdin, not typed into
    if "-p" in rest or "--print" in rest:
        return "headless"
    return "interactive"


def clip_argv(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            out.append("…")
            skip = False
            continue
        out.append(a if len(a) <= 200 else a[:199] + "…")
        if a == "--append-system-prompt":
            skip = True
    return out


def _claude_names() -> set[str]:
    names = {"claude"}
    if core.CFG is not None and core.CFG.claude:
        names.add(os.path.basename(core.CFG.claude))
    return names


def sessions() -> list[dict]:
    table = _proc_table()
    children = _children_map(table)
    status = read_status()
    by_pid = {r.get("claude_pid"): r for r in active_runs(status).values()}
    main_pid = poller_pid(service_state(), status)
    poller_pids = set(_under(main_pid, children)) if main_pid else set()
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        uptime = 0.0
    hz = os.sysconf("SC_CLK_TCK")
    names = _claude_names()
    out = []
    for pid in sorted(table):
        argv = proc_argv(pid)
        if not argv or os.path.basename(argv[0]) not in names:
            continue
        cls = classify_argv(argv)
        entry: dict = {"pid": pid, "class": cls, "argv": clip_argv(argv),
                       "age_s": int(uptime - table[pid]["start"] / hz) if uptime else None,
                       "children": len(_under(pid, children))}
        run = by_pid.get(pid)
        if run or pid in poller_pids:
            entry["class"] = "poller-run"
            entry["issue"] = str(run["issue"]) if run and run.get("issue") is not None else None
            entry["lane"] = run.get("lane") if run else None
            entry["log"] = Path(str(run.get("log"))).name if run and run.get("log") else None
        try:
            entry["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            entry["cwd"] = None
        try:
            m = re.search(r"^VmRSS:\s+(\d+) kB", Path(f"/proc/{pid}/status").read_text(), re.M)
            entry["rss_kb"] = int(m.group(1)) if m else None
        except OSError:
            entry["rss_kb"] = None
        out.append(entry)
    order = ["poller-run", "interactive", "headless", "background", "daemon", "helper"]
    out.sort(key=lambda e: (order.index(e["class"]), e["pid"]))
    return out


# --- journal ---------------------------------------------------------------------------------


def journal_argv(lines: int, follow: bool = False) -> list[str]:
    unit = service_name()
    if not unit:
        raise ActionError(409, "not managed by systemd: the config names no dashboard.service")
    lines = max(1, min(int(lines), 2000))
    argv = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    return argv + ["-f"] if follow else argv


# --- overview --------------------------------------------------------------------------------


def overview() -> dict:
    def build() -> dict:
        service = service_state()
        runs = list_runs()
        return {
            "project": core.CFG.name, "tracker": core.T.describe(), "tracker_kind": core.T.kind,
            "owner": core.OWNER, "label_prefix": core.CFG.label_prefix,
            "service": service, "poller": poller_state(service),
            "newest_run": runs[0] if runs else None, "now": core.now_iso(),
        }
    value, _ = CACHE.get("overview", 2, build)
    return value  # type: ignore[return-value]


# --- controls (POST) -------------------------------------------------------------------------


SERVICE_VERBS = ("start", "stop", "restart")


def sudoers_hint(unit: str) -> str:
    return ("sudo needs a password: add a NOPASSWD sudoers rule for "
            f"`systemctl start|stop|restart {unit}` (the dashboard runs `sudo -n`)")


class ActionError(Exception):
    """An action that cannot run now: `code` is the HTTP status to answer with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def log_action(name: str, result: object) -> None:
    print(f"[{core.now_iso()}] action {name}: {result}", flush=True)


def service_argv(verb: str) -> list[str]:
    if verb not in SERVICE_VERBS:
        raise ActionError(404, "no such action")
    unit = service_name()
    if not unit:
        raise ActionError(409, "not managed by systemd: the config names no dashboard.service")
    return ["sudo", "-n", "systemctl", verb, unit]


def service_action(verb: str) -> dict:
    """Start, stop or restart the poller unit. Stop waits for the unit's TimeoutStopSec."""
    argv = service_argv(verb)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"systemctl {verb} took over 90 s"}
    except OSError as e:
        return {"ok": False, "output": str(e)}
    output = (r.stdout + r.stderr).strip()
    if r.returncode and "password" in r.stderr.lower():
        output = f"{sudoers_hint(argv[-1])}\n{output}"
    CACHE.drop("overview")
    return {"ok": r.returncode == 0, "output": output}


def proc_argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return raw.rstrip(b"\0").decode("utf-8", "replace").split("\0") if raw else []


def catches_signal(pid: int, sig: int) -> bool:
    """Whether PID has a handler for SIG (`SigCgt` in /proc/<pid>/status). SIGUSR1's default
    action ends the process, so a process without a handler must never get it."""
    try:
        m = re.search(r"^SigCgt:\s*([0-9a-fA-F]+)", Path(f"/proc/{pid}/status").read_text(), re.M)
    except OSError:
        return False
    return bool(m and int(m.group(1), 16) >> (sig - 1) & 1)


def is_watch_poller(argv: list[str]) -> bool:
    """`quartermaster watch`, `python3 .../bin/quartermaster watch` or
    `python3 -m quartermaster watch` (with any options between)."""
    if "watch" not in argv[1:]:
        return False
    return any(os.path.basename(a) == "quartermaster" or a in ("quartermaster", "quartermaster.cli")
               for a in argv)


def poll_now() -> dict:
    """Wake the poller's sleep with SIGUSR1 (it runs as this user, so no sudo)."""
    service = service_state()
    pid = poller_pid(service)
    if not pid:
        raise ActionError(409, "the poller is not running")
    argv = proc_argv(pid)
    if not is_watch_poller(argv):
        raise ActionError(409, f"pid {pid} is not `quartermaster watch`")
    if not catches_signal(pid, signal.SIGUSR1):
        raise ActionError(409, f"pid {pid} has no SIGUSR1 handler: restart it once")
    os.kill(pid, signal.SIGUSR1)
    return {"ok": True, "pid": pid}


def _alive(pid: int) -> bool:
    """Running and not a zombie."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text[text.rfind(")") + 2:].split()[0] != "Z"


def kill_tree(pids: list[int], wait: float = 10.0, group: int | None = None) -> None:
    """SIGTERM every pid (and GROUP's process group), then SIGKILL what is left after WAIT s."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if group:
            try:
                os.killpg(group, sig)
            except OSError:
                pass
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if sig == signal.SIGKILL:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.2)
        if not any(_alive(p) for p in pids):
            return


def stop_run(lane: object = None) -> dict:
    """End a Claude run of the poller (LANE's, or the only one going) and everything under it.
    The poller's wait_for_run sees the exit and posts its usual failed-run comment."""
    if lane is not None and lane not in (core.CODE, core.PLAN):
        raise ActionError(400, "lane must be \"code\" or \"plan\"")
    runs = active_runs()
    if lane is None and len(runs) > 1:
        raise ActionError(409, f"runs are going in the {' and '.join(sorted(runs))} lanes: name one")
    run = runs.get(lane) if lane is not None else next(iter(runs.values()), None)
    if not isinstance(run, dict) or not run.get("claude_pid") or not run.get("log"):
        raise ActionError(409, f"no poller run is going{f' in the {lane} lane' if lane else ''}")
    pid = int(run["claude_pid"])
    argv = proc_argv(pid)
    if not argv or not _alive(pid):
        raise ActionError(409, f"run pid {pid} is gone")
    if "-p" not in argv[1:] or os.path.basename(argv[0]) != os.path.basename(core.CFG.claude):
        raise ActionError(409, f"pid {pid} is not a headless claude run")
    try:
        out = os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        out = None
    if out != str(run["log"]):
        raise ActionError(409, f"pid {pid} does not write {Path(str(run['log'])).name}")
    pids = [pid] + core._descendants(pid)
    threading.Thread(target=kill_tree, args=(pids,), daemon=True).start()
    return {"ok": True, "pid": pid, "processes": len(pids), "log": Path(str(run["log"])).name,
            "lane": run.get("lane")}


# --- request guard and static files --------------------------------------------------------


def host_ok(host: str | None, port: int) -> bool:
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def post_refusal(headers, port: int) -> str | None:
    """Why a POST is refused, or None. On top of the Host check: the custom header (a
    cross-site page cannot send it without a CORS preflight, which is never granted), a
    same-host Origin when one is sent, and a JSON body type (no simple-form posts)."""
    if not host_ok(headers.get("Host"), port):
        return "forbidden host"
    if headers.get("X-QM-Dashboard") != "1":
        return "missing X-QM-Dashboard header"
    origin = headers.get("Origin")
    if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return "forbidden origin"
    if (headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
        return "Content-Type must be application/json"
    return None


def static_path(url_path: str, root: Path = STATIC_DIR) -> Path | None:
    """The file under ROOT that URL_PATH names, or None if it would leave ROOT or is missing."""
    rel = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
    if "\0" in rel:
        return None
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        return None
    return p if p.is_file() else None


# --- HTTP ------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "quartermaster-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter: errors only
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(f"[{core.now_iso()}] {self.address_string()} {fmt % args}\n")

    @property
    def port(self) -> int:
        return self.server.server_address[1]  # type: ignore[attr-defined]

    # responses

    def _headers(self, code: int, ctype: str, length: int | None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self._headers(code, ctype, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, value: object, code: int = 200) -> None:
        self.send_bytes(code, json.dumps(value).encode(), "application/json")

    def send_error_json(self, code: int, message: str) -> None:
        self.send_json({"error": message}, code)

    # SSE

    def sse_start(self) -> None:
        self.close_connection = True
        self._headers(200, "text/event-stream", None)

    def sse(self, data: object, event: str | None = None, id_: object = None) -> None:
        msg = ""
        if event:
            msg += f"event: {event}\n"
        if id_ is not None:
            msg += f"id: {id_}\n"
        msg += f"data: {json.dumps(data)}\n\n"
        self.wfile.write(msg.encode())
        self.wfile.flush()

    def sse_ping(self) -> None:
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    # routing

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not host_ok(self.headers.get("Host"), self.port):
            self.send_error_json(403, "forbidden host")
            return
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if path.startswith("/api/"):
                self.route_api(path, q)
            else:
                self.route_static(path)
        except ActionError as e:
            self.send_error_json(e.code, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:  # no CORS preflight is ever granted
        self.send_error_json(405, "method not allowed")

    def do_POST(self) -> None:
        reason = post_refusal(self.headers, self.port)
        if reason:
            self.send_error_json(403, reason)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 65536:
            self.send_error_json(400, "bad Content-Length")
            return
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self.send_error_json(400, "body is not JSON")
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if m := re.fullmatch(r"/api/service/([a-z]+)", path):
                if m.group(1) not in SERVICE_VERBS:
                    raise ActionError(404, "no such action")
                result = service_action(m.group(1))
            elif path == "/api/poller/poll-now":
                result = poll_now()
            elif path == "/api/poller/stop-run":
                result = stop_run(data.get("lane") if isinstance(data, dict) else None)
            else:
                raise ActionError(404, "no such action")
        except ActionError as e:
            log_action(path, f"refused {e.code}: {e}")
            self.send_error_json(e.code, str(e))
            return
        log_action(path, result)
        self.send_json(result)

    def route_static(self, path: str) -> None:
        p = static_path(path)
        if p is None:
            self.send_error_json(404, "not found")
            return
        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(
            p.suffix, mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_bytes(200, p.read_bytes(), ctype)

    def route_api(self, path: str, q: dict[str, list[str]]) -> None:
        def arg(name: str, default: str = "") -> str:
            return q.get(name, [default])[0]

        if path == "/api/overview":
            self.send_json(overview())
        elif path == "/api/issues":
            self.send_json(issues_payload(arg("refresh") == "1"))
        elif m := re.fullmatch(r"/api/plans/([A-Za-z0-9_-]+)", path):
            p = plan_files().get(m.group(1))
            if p is None:
                self.send_error_json(404, "no plan file")
            else:
                self.send_bytes(200, p.read_bytes(), "text/plain; charset=utf-8")
        elif path == "/api/runs":
            issue = arg("issue")
            self.send_json({"runs": list_runs(issue if KEY_RE.fullmatch(issue) else None)})
        elif m := re.fullmatch(r"/api/runs/([^/]+)/stream", path):
            self.stream_run(m.group(1), arg("from", "0"))
        elif m := re.fullmatch(r"/api/runs/([^/]+)", path):
            p = run_path(m.group(1))
            if p is None:
                self.send_error_json(404, "no such run")
                return
            start = arg("from", "0")
            self.send_json(read_run_lines(p, int(start) if start.isdigit() else 0, arg("raw") == "1"))
        elif path == "/api/sessions":
            self.send_json({"sessions": sessions()})
        elif path == "/api/journal":
            n = arg("lines", "200")
            self.journal(int(n) if n.isdigit() else 200)
        elif path == "/api/journal/stream":
            self.stream_journal()
        else:
            self.send_error_json(404, "no such endpoint")

    # endpoints with subprocesses or streams

    def journal(self, lines: int) -> None:
        argv = journal_argv(lines)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.send_json({"lines": [], "error": str(e)})
            return
        self.send_json({"lines": r.stdout.splitlines(),
                        "error": (r.stderr.strip() or None) if r.returncode else None})

    def stream_run(self, name: str, start: str) -> None:
        p = run_path(name)
        if p is None:
            self.send_error_json(404, "no such run")
            return
        pos = int(start) if start.isdigit() else 0
        self.sse_start()
        quiet = 0.0
        while True:
            chunk = read_run_lines(p, pos, raw=False)
            if chunk["next"] != pos or chunk["lines"]:
                pos = chunk["next"]
                if chunk["lines"]:
                    self.sse({"lines": chunk["lines"], "next": pos}, id_=pos)
                    quiet = 0.0
            if chunk["more"]:
                continue
            if run_finished(p):
                self.sse({"next": pos}, event="end")
                return
            time.sleep(1)
            quiet += 1
            if quiet >= 15:
                self.sse_ping()  # finds a closed client
                quiet = 0.0

    def stream_journal(self) -> None:
        argv = journal_argv(50, follow=True)
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            self.send_error_json(500, str(e))
            return
        self.sse_start()
        try:
            assert proc.stdout is not None
            while True:
                ready, _, _ = select.select([proc.stdout], [], [], 15)
                if not ready:
                    self.sse_ping()
                    continue
                line = proc.stdout.readline()
                if not line:
                    self.sse({}, event="end")
                    return
                self.sse({"line": line.rstrip("\n")})
        finally:
            proc.kill()
            proc.wait()


def make_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(port: int) -> None:
    """Serve the dashboard for the project core is configured for, until Ctrl-C."""
    if core.CFG is None or core.T is None:
        raise RuntimeError("core.configure(...) must run before dashboard.serve")
    server = make_server(port)
    print(f"{core.NAME} dashboard for {core.CFG.name}: http://127.0.0.1:{server.server_address[1]}/",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
