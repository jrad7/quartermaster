"""The tracker interface Quartermaster talks to, and the normalised issue and comment shapes.

An issue is a dict:
    key       "PROJ-12" (Jira) or "42" (GitHub): always a string
    title, body, url, author (stable id: GitHub login, Jira accountId or username),
    author_name (for display), created and updated (UTC, `YYYY-MM-DDTHH:MM:SS.mmmZ`),
    state (the tracker's own status name), labels (list of strings)

A comment is a dict:
    id (string), issue (the issue key), author, author_name, body, created, url

Timestamps are all in the one format so plain string comparison orders them.
"""

from __future__ import annotations

import datetime as dt
import re

BOT_TOKEN = "qm-bot"
PLAN_TOKEN_RE = re.compile(r"qm-plan issue=([A-Za-z0-9_-]+) rev=(\d+)(?: fix=([A-Za-z0-9_-]+))?")


class TrackerError(RuntimeError):
    pass


def norm_time(s: str | None) -> str:
    """Any ISO 8601 time (`...Z`, `...+00:00`, Jira's `...000-0600`) as UTC with milliseconds."""
    if not s:
        return ""
    t = s.strip().replace("Z", "+00:00")
    t = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", t)  # Jira's -0600 -> -06:00
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        return s
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    d = d.astimezone(dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


class Tracker:
    """What a backend provides. Methods raise TrackerError on API failures."""

    kind = "base"
    markup = "markdown"  # how comment bodies are written: "markdown" or "jira-wiki"
    max_comment = 65000

    # --- markers (how hidden machine-readable text is written in this tracker's markup) ---

    def bot_marker(self) -> str:
        raise NotImplementedError

    def plan_marker(self, key: str, rev: int, fix: str | None = None) -> str:
        raise NotImplementedError

    def is_bot(self, body: str | None) -> bool:
        return BOT_TOKEN in (body or "")

    def plan_markers(self, body: str | None) -> list[tuple[str, int, str | None]]:
        return [(k, int(r), f or None) for k, r, f in PLAN_TOKEN_RE.findall(body or "")]

    # --- identity ---

    def whoami(self) -> str:
        """The account the credentials act as (the id comments are compared against)."""
        raise NotImplementedError

    def describe(self) -> str:
        """One line naming where the issues come from, for logs."""
        raise NotImplementedError

    # --- reads ---

    def get_issue(self, key: str) -> dict:
        raise NotImplementedError

    def open_issues(self) -> list[dict]:
        """Open issues, oldest first."""
        raise NotImplementedError

    def comments(self, key: str) -> list[dict]:
        """An issue's comments, oldest first."""
        raise NotImplementedError

    def recent_comments(self, issues: list[dict]) -> list[dict]:
        """Comments from the last 30 days that a poll should look at, oldest first. ISSUES is
        the open issue list the poll just read (a backend may use it to know where to look)."""
        raise NotImplementedError

    def issue_url(self, key: str) -> str:
        raise NotImplementedError

    # --- writes ---

    def post_comment(self, key: str, body: str) -> dict:
        raise NotImplementedError

    def set_labels(self, key: str, add: list[str], remove: list[str]) -> None:
        raise NotImplementedError

    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Create LABELS (name -> (colour, description)) where the tracker needs that."""

    def react(self, comment_id: str, reaction: str) -> None:
        """Mark a comment (eyes, rocket, confused, +1); a no-op where the tracker has none."""

    def transition(self, key: str, target: str) -> str | None:
        """Move the issue to the workflow status (or transition) named TARGET. Returns what
        happened for the log, or None where the tracker has no workflow."""
        return None
