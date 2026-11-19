"""A fake tracker and a configured core module for tests. Nothing here touches the network."""

from __future__ import annotations

import tempfile
from pathlib import Path

from quartermaster import config, core
from quartermaster.trackers.base import BOT_TOKEN, Tracker

OWNER = "owner-1"


class FakeTracker(Tracker):
    kind = "fake"
    markup = "markdown"

    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.comment_list: list[dict] = []
        self.posted: list[tuple[str, str]] = []
        self.label_calls: list[tuple[str, list, list]] = []
        self.transitions: list[tuple[str, str]] = []
        self._next = 1000

    def bot_marker(self) -> str:
        return f"<!-- {BOT_TOKEN} -->"

    def plan_marker(self, key, rev, fix=None) -> str:
        return f"<!-- qm-plan issue={key} rev={rev}{f' fix={fix}' if fix else ''} -->"

    def whoami(self) -> str:
        return OWNER

    def describe(self) -> str:
        return "fake tracker"

    def issue_url(self, key) -> str:
        return f"https://example.test/{key}"

    def get_issue(self, key) -> dict:
        return self.issues[key]

    def open_issues(self) -> list[dict]:
        return sorted(self.issues.values(), key=lambda i: (i["created"], i["key"]))

    def comments(self, key) -> list[dict]:
        return [c for c in self.comment_list if c["issue"] == key]

    def recent_comments(self, issues) -> list[dict]:
        return list(self.comment_list)

    def post_comment(self, key, body) -> dict:
        self._next += 1
        c = comment(str(self._next), key, body, who="bot", t=f"2026-11-19T12:00:{self._next % 60:02d}.000Z")
        self.comment_list.append(c)
        self.posted.append((key, body))
        return c

    def set_labels(self, key, add, remove) -> None:
        self.label_calls.append((key, add, remove))
        i = self.issues[key]
        i["labels"] = [x for x in i["labels"] if x not in remove] + list(add)

    def transition(self, key, target):
        self.transitions.append((key, target))
        return f"moved {key} to {target}"


def comment(cid: str, key: str, body: str, who: str | None = None, t: str | None = None) -> dict:
    return {"id": str(cid), "issue": key, "author": who or OWNER, "author_name": who or OWNER,
            "body": body, "created": t or f"2026-11-14T10:00:{int(cid) % 60:02d}.000Z",
            "url": f"https://example.test/{key}#c{cid}"}


def issue(key: str, *labels: str, created: str | None = None, author: str = OWNER) -> dict:
    n = int(key.rsplit("-", 1)[-1])
    return {"key": key, "title": f"issue {key}", "body": "", "url": f"https://example.test/{key}",
            "author": author, "author_name": author, "created": created or f"2026-11-01T00:00:{n:02d}.000Z",
            "updated": "", "state": "To Do", "labels": list(labels)}


def L(status: str) -> str:
    return "claude-" + status


class Project:
    """A temp project: config, plan dir and state dir, with core bound to a FakeTracker."""

    def __init__(self, **overrides) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        data = {"name": "test", "tracker": {"type": "jira", "url": "https://x.test", "project": "P"},
                "owner": OWNER, "state_dir": str(root / "state"), "auto_plan": False, **overrides}
        self.cfg = config.from_dict(data, root / "quartermaster.json")
        self.cfg.plan_dir.mkdir(parents=True)
        self.tracker = FakeTracker()
        core.configure(self.cfg, self.tracker)

    def plan(self, key: str, rev: int = 1, extra: str = "") -> Path:
        p = self.cfg.plan_dir / f"issue-{key}.md"
        p.write_text(f"**Status:** planned\n**Revision:** {rev}\n{extra}")
        return p

    def close(self) -> None:
        self.tmp.cleanup()
