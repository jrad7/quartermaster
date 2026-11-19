"""Jira Cloud or Jira Server / Data Center through its REST API (stdlib `urllib`).

Comments are read and written through REST v2, whose bodies are wiki-markup strings on both
Cloud and Server. Search uses `/rest/api/3/search/jql` on Cloud (the old `/search` is gone
there) and `/rest/api/2/search` on Server.

Config (`tracker` section):
    "type": "jira",
    "url": "https://acme.atlassian.net",       base URL, no trailing /rest
    "project": "PROJ",                          used by the default JQL
    "jql": "...",                               optional: the open-issue query (keep ORDER BY created ASC)
    "deployment": "cloud" | "server",           optional: default cloud for *.atlassian.net
    "auth": "basic" | "bearer",                 basic = email + API token (Cloud),
                                                bearer = personal access token (Server/DC)
    "email_env": "JIRA_EMAIL",                  env var holding the account email (basic)
    "token_env": "JIRA_API_TOKEN"               env var holding the token
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import BOT_TOKEN, PLAN_TOKEN_RE, Tracker, TrackerError, norm_time

ISSUE_FIELDS = "summary,labels,created,updated,reporter,status"


class Jira(Tracker):
    kind = "jira"
    markup = "jira-wiki"
    max_comment = 32000  # Jira's limit is 32767 characters

    def __init__(self, conf: dict, workspace=None, opener=None) -> None:
        self.base = (conf.get("url") or "").rstrip("/")
        if not self.base.startswith(("https://", "http://")):
            raise TrackerError("tracker.url must be the Jira base URL, such as https://acme.atlassian.net")
        self.project = conf.get("project")
        self.jql = conf.get("jql") or (
            f'project = "{self.project}" AND statusCategory != Done ORDER BY created ASC' if self.project else None)
        if not self.jql:
            raise TrackerError("tracker needs a project key or a jql query")
        host = urllib.parse.urlparse(self.base).hostname or ""
        self.cloud = conf.get("deployment", "cloud" if host.endswith(".atlassian.net") else "server") == "cloud"
        self.auth_kind = conf.get("auth", "basic" if self.cloud else "bearer")
        self.email_env = conf.get("email_env", "JIRA_EMAIL")
        self.token_env = conf.get("token_env", "JIRA_API_TOKEN")
        self._opener = opener or urllib.request.urlopen
        self._cache: dict[str, tuple[str, list[dict]]] = {}  # key -> (updated, comments)
        self._cache_lock = threading.Lock()

    # --- plumbing ---

    def _auth_header(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise TrackerError(f"${self.token_env} is not set (the Jira API token)")
        if self.auth_kind == "bearer":
            return f"Bearer {token}"
        email = os.environ.get(self.email_env)
        if not email:
            raise TrackerError(f"${self.email_env} is not set (the Jira account email for basic auth)")
        return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def api(self, path: str, method: str = "GET", body: object | None = None,
            params: dict | None = None) -> object:
        url = f"{self.base}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": self._auth_header(), "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {})})
            try:
                with self._opener(req, timeout=60) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:500]
                if e.code == 429 and attempt < 2:
                    time.sleep(min(int(e.headers.get("Retry-After") or 10), 60))
                    continue
                raise TrackerError(f"Jira {method} {path}: HTTP {e.code}: {text}") from None
            except urllib.error.URLError as e:
                raise TrackerError(f"Jira {method} {path}: {e.reason}") from None
        raise TrackerError(f"Jira {method} {path}: rate limited")

    # --- shapes ---

    @staticmethod
    def _user_id(u: dict | None) -> str:
        u = u or {}
        return u.get("accountId") or u.get("name") or u.get("key") or ""

    def _issue(self, i: dict) -> dict:
        f = i.get("fields") or {}
        desc = f.get("description")
        return {"key": i["key"], "title": f.get("summary") or "",
                "body": desc if isinstance(desc, str) else "",
                "url": self.issue_url(i["key"]),
                "author": self._user_id(f.get("reporter")),
                "author_name": (f.get("reporter") or {}).get("displayName") or "",
                "created": norm_time(f.get("created")), "updated": norm_time(f.get("updated")),
                "state": (f.get("status") or {}).get("name") or "",
                "labels": list(f.get("labels") or [])}

    def _comment(self, c: dict, key: str) -> dict:
        return {"id": str(c["id"]), "issue": key, "author": self._user_id(c.get("author")),
                "author_name": (c.get("author") or {}).get("displayName") or "",
                "body": c.get("body") if isinstance(c.get("body"), str) else "",
                "created": norm_time(c.get("created")),
                "url": f"{self.issue_url(key)}?focusedCommentId={c['id']}"}

    # --- markers: {{...}} is monospace in wiki markup, and HTML comments do not survive ---

    def bot_marker(self) -> str:
        return "{{" + BOT_TOKEN + "}}"

    def plan_marker(self, key: str, rev: int, fix: str | None = None) -> str:
        return "{{qm-plan issue=" + key + f" rev={rev}" + (f" fix={fix}" if fix else "") + "}}"

    @staticmethod
    def _plain(body: str | None) -> str:
        return (body or "").replace("\\", "")  # Jira may escape characters on its round trip

    def is_bot(self, body: str | None) -> bool:
        return BOT_TOKEN in self._plain(body)

    def plan_markers(self, body: str | None) -> list[tuple[str, int, str | None]]:
        return [(k, int(r), f or None) for k, r, f in PLAN_TOKEN_RE.findall(self._plain(body))]

    # --- interface ---

    def whoami(self) -> str:
        me = self.api("/rest/api/2/myself")
        assert isinstance(me, dict)
        return self._user_id(me)

    def describe(self) -> str:
        return f"Jira {self.base} ({self.jql})"

    def issue_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def get_issue(self, key: str) -> dict:
        i = self.api(f"/rest/api/2/issue/{urllib.parse.quote(key)}",
                     params={"fields": ISSUE_FIELDS + ",description"})
        assert isinstance(i, dict)
        return self._issue(i)

    def open_issues(self) -> list[dict]:
        out: list[dict] = []
        if self.cloud:
            token = None
            while True:
                r = self.api("/rest/api/3/search/jql", params={
                    "jql": self.jql, "fields": ISSUE_FIELDS, "maxResults": 100, "nextPageToken": token})
                assert isinstance(r, dict)
                out += [self._issue(i) for i in r.get("issues", [])]
                token = r.get("nextPageToken")
                if not token or r.get("isLast"):
                    break
        else:
            start = 0
            while True:
                r = self.api("/rest/api/2/search", params={
                    "jql": self.jql, "fields": ISSUE_FIELDS, "maxResults": 100, "startAt": start})
                assert isinstance(r, dict)
                batch = r.get("issues", [])
                out += [self._issue(i) for i in batch]
                start += len(batch)
                if not batch or start >= int(r.get("total", 0)):
                    break
        return sorted(out, key=lambda i: (i["created"], i["key"]))

    def comments(self, key: str) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            r = self.api(f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment",
                         params={"startAt": start, "maxResults": 100, "orderBy": "created"})
            assert isinstance(r, dict)
            batch = r.get("comments", [])
            out += [self._comment(c, key) for c in batch]
            start += len(batch)
            if not batch or start >= int(r.get("total", 0)):
                break
        return sorted(out, key=_order)

    def recent_comments(self, issues: list[dict]) -> list[dict]:
        """Jira has no "every recent comment" call: read each open issue's comments, again
        only when its `updated` time has moved since the last read (a comment moves it)."""
        since = norm_time((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat())
        out: list[dict] = []
        keep = set()
        for issue in issues:
            key = issue["key"]
            keep.add(key)
            with self._cache_lock:
                cached = self._cache.get(key)
            if cached is None or cached[0] != issue["updated"] or not issue["updated"]:
                cached = (issue["updated"], self.comments(key))
                with self._cache_lock:
                    self._cache[key] = cached
            out += [c for c in cached[1] if c["created"] >= since]
        with self._cache_lock:
            for key in set(self._cache) - keep:
                del self._cache[key]
        return sorted(out, key=_order)

    def post_comment(self, key: str, body: str) -> dict:
        c = self.api(f"/rest/api/2/issue/{urllib.parse.quote(key)}/comment", "POST", {"body": body})
        assert isinstance(c, dict)
        with self._cache_lock:
            self._cache.pop(key, None)
        return self._comment(c, key)

    def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        ops = [{"remove": x} for x in remove] + [{"add": x} for x in add]
        if not ops:
            return
        for name in add:
            if re.search(r"\s", name):
                raise TrackerError(f"Jira labels cannot contain spaces: {name!r}")
        self.api(f"/rest/api/2/issue/{urllib.parse.quote(key)}", "PUT", {"update": {"labels": ops}})

    def transition(self, key: str, target: str) -> str | None:
        path = f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions"
        r = self.api(path)
        assert isinstance(r, dict)
        want = target.strip().lower()
        for t in r.get("transitions", []):
            names = {(t.get("name") or "").lower(), ((t.get("to") or {}).get("name") or "").lower()}
            if want in names:
                self.api(path, "POST", {"transition": {"id": t["id"]}})
                return f"moved {key} to {(t.get('to') or {}).get('name') or t.get('name')}"
        have = ", ".join(sorted({(t.get("to") or {}).get("name") or t.get("name", "?")
                                 for t in r.get("transitions", [])})) or "none"
        return f"no transition to {target!r} from {key}'s current status (available: {have})"


def _order(c: dict) -> tuple:
    return (c["created"], int(c["id"]) if c["id"].isdigit() else 0, c["id"])
