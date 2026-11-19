"""GitHub issues through the `gh` CLI (`gh api`), which must be logged in with write access."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import urllib.parse

from .base import BOT_TOKEN, Tracker, TrackerError, norm_time


def _slug_from_remote(cwd) -> str:
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, check=True,
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise TrackerError(f"origin is not a GitHub remote: {url}")
    return m.group(1)


class GitHub(Tracker):
    kind = "github"
    markup = "markdown"
    max_comment = 65000

    def __init__(self, conf: dict, workspace) -> None:
        self.slug = conf.get("repo") or _slug_from_remote(workspace)
        self.gh = conf.get("gh", "gh")

    # --- plumbing ---

    def api(self, path: str, method: str = "GET", body: object | None = None) -> object:
        args = [self.gh, "api", "-X", method, path]
        data = None
        if body is not None:
            args += ["--input", "-"]
            data = json.dumps(body)
        r = subprocess.run(args, capture_output=True, text=True, input=data)
        if r.returncode:
            raise TrackerError(f"gh api {method} {path}: {r.stderr.strip() or r.stdout.strip()}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def api_list(self, path: str) -> list[dict]:
        sep = "&" if "?" in path else "?"
        items: list[dict] = []
        page = 1
        while True:
            batch = self.api(f"{path}{sep}per_page=100&page={page}")
            assert isinstance(batch, list)
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    # --- shapes ---

    def _issue(self, i: dict) -> dict:
        return {"key": str(i["number"]), "title": i.get("title") or "", "body": i.get("body") or "",
                "url": i.get("html_url") or self.issue_url(str(i["number"])),
                "author": i["user"]["login"], "author_name": i["user"]["login"],
                "created": norm_time(i.get("created_at")), "updated": norm_time(i.get("updated_at")),
                "state": i.get("state") or "", "labels": [lab["name"] for lab in i.get("labels", [])]}

    def _comment(self, c: dict, key: str | None = None) -> dict:
        if key is None:
            key = c["issue_url"].rsplit("/", 1)[1]
        return {"id": str(c["id"]), "issue": key, "author": c["user"]["login"],
                "author_name": c["user"]["login"], "body": c.get("body") or "",
                "created": norm_time(c.get("created_at")), "url": c.get("html_url") or ""}

    # --- interface ---

    def bot_marker(self) -> str:
        return f"<!-- {BOT_TOKEN} -->"

    def plan_marker(self, key: str, rev: int, fix: str | None = None) -> str:
        return f"<!-- qm-plan issue={key} rev={rev}{f' fix={fix}' if fix else ''} -->"

    def whoami(self) -> str:
        me = self.api("user")
        assert isinstance(me, dict)
        return me["login"]

    def describe(self) -> str:
        return f"GitHub {self.slug}"

    def issue_url(self, key: str) -> str:
        return f"https://github.com/{self.slug}/issues/{key}"

    def get_issue(self, key: str) -> dict:
        i = self.api(f"repos/{self.slug}/issues/{key}")
        assert isinstance(i, dict)
        return self._issue(i)

    def open_issues(self) -> list[dict]:
        items = self.api_list(f"repos/{self.slug}/issues?state=open&sort=created&direction=asc")
        return [self._issue(i) for i in items if "pull_request" not in i]

    def comments(self, key: str) -> list[dict]:
        return [self._comment(c, key) for c in self.api_list(f"repos/{self.slug}/issues/{key}/comments")]

    def recent_comments(self, issues: list[dict]) -> list[dict]:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = self.api_list(f"repos/{self.slug}/issues/comments?sort=created&direction=asc&since={since}")
        out = [self._comment(c) for c in items]
        return sorted(out, key=lambda c: (c["created"], int(c["id"])))

    def post_comment(self, key: str, body: str) -> dict:
        c = self.api(f"repos/{self.slug}/issues/{key}/comments", "POST", {"body": body})
        assert isinstance(c, dict)
        return self._comment(c, key)

    def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        for name in remove:
            self.api(f"repos/{self.slug}/issues/{key}/labels/{urllib.parse.quote(name, safe='')}", "DELETE")
        if add:
            self.api(f"repos/{self.slug}/issues/{key}/labels", "POST", {"labels": add})

    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        have = {lab["name"] for lab in self.api_list(f"repos/{self.slug}/labels")}
        for name, (color, desc) in labels.items():
            if name not in have:
                self.api(f"repos/{self.slug}/labels", "POST", {"name": name, "color": color, "description": desc})
                print(f"created label {name}")

    def react(self, comment_id: str, reaction: str) -> None:
        try:
            self.api(f"repos/{self.slug}/issues/comments/{comment_id}/reactions", "POST", {"content": reaction})
        except TrackerError as e:
            print(f"quartermaster: reaction failed: {e}", flush=True)
