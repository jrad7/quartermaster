"""The Jira backend against a fake HTTP opener: auth, search paging, comments, the comment
cache, labels, transitions and markers. Nothing here touches the network."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

from quartermaster.trackers.base import norm_time
from quartermaster.trackers.jira import Jira


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeJira:
    """Routes requests by (method, path) to handlers; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, object]] = []
        self.routes = {}

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlparse(req.full_url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), u.path, q, body))
        self.last_headers = dict(req.headers)
        out = self.routes[(req.get_method(), u.path)](q, body)
        return FakeResponse(json.dumps(out).encode() if out is not None else b"")


def jissue(key, created, labels=(), reporter="acc-owner", updated=None):
    return {"key": key, "fields": {"summary": f"S {key}", "labels": list(labels), "created": created,
                                   "updated": updated or created, "status": {"name": "To Do"},
                                   "reporter": {"accountId": reporter, "displayName": "Owner"}}}


def jcomment(cid, created, body, author="acc-owner"):
    return {"id": str(cid), "created": created, "body": body,
            "author": {"accountId": author, "displayName": "Owner"}}


class JiraTest(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"JIRA_EMAIL": "me@example.test", "JIRA_API_TOKEN": "tok"})
        env.start()
        self.addCleanup(env.stop)
        self.http = FakeJira()
        self.j = Jira({"type": "jira", "url": "https://acme.atlassian.net/", "project": "PROJ"}, opener=self.http)

    def test_cloud_defaults_and_basic_auth(self) -> None:
        self.assertTrue(self.j.cloud)
        self.assertIn('project = "PROJ"', self.j.jql)
        self.http.routes[("GET", "/rest/api/2/myself")] = lambda q, b: {"accountId": "acc-owner"}
        self.assertEqual(self.j.whoami(), "acc-owner")
        self.assertTrue(self.http.last_headers["Authorization"].startswith("Basic "))

    def test_server_uses_a_bearer_token_and_v2_search(self) -> None:
        j = Jira({"type": "jira", "url": "https://jira.corp.test", "project": "PROJ"}, opener=self.http)
        self.assertFalse(j.cloud)
        pages = [[jissue("PROJ-1", "2026-01-01T00:00:00.000+0000")], [jissue("PROJ-2", "2026-01-02T00:00:00.000+0000")]]
        self.http.routes[("GET", "/rest/api/2/search")] = lambda q, b: {
            "issues": pages[int(q["startAt"])], "total": 2, "startAt": int(q["startAt"])}
        self.assertEqual([i["key"] for i in j.open_issues()], ["PROJ-1", "PROJ-2"])
        self.assertTrue(self.http.last_headers["Authorization"].startswith("Bearer "))

    def test_cloud_search_pages_by_token_and_sorts_oldest_first(self) -> None:
        def search(q, b):
            if q.get("nextPageToken") is None:
                return {"issues": [jissue("PROJ-9", "2026-03-01T00:00:00.000-0600")], "nextPageToken": "t2"}
            return {"issues": [jissue("PROJ-3", "2026-02-01T00:00:00.000+0000", labels=["claude-planning"])],
                    "isLast": True}
        self.http.routes[("GET", "/rest/api/3/search/jql")] = search
        issues = self.j.open_issues()
        self.assertEqual([i["key"] for i in issues], ["PROJ-3", "PROJ-9"])
        self.assertEqual(issues[1]["created"], "2026-03-01T06:00:00.000Z")
        self.assertEqual(issues[0]["labels"], ["claude-planning"])
        self.assertEqual(issues[0]["author"], "acc-owner")

    def test_recent_comments_reads_an_issue_again_only_when_it_changed(self) -> None:
        calls = []

        def comments(q, b):
            calls.append(1)
            return {"comments": [jcomment(10, "2099-01-01T00:00:00.000+0000", "/approve")], "total": 1}
        self.http.routes[("GET", "/rest/api/2/issue/PROJ-1/comment")] = comments
        issue = {"key": "PROJ-1", "updated": "2099-01-01T00:00:00.000Z"}
        got = self.j.recent_comments([issue])
        self.assertEqual([(c["id"], c["issue"], c["author"]) for c in got], [("10", "PROJ-1", "acc-owner")])
        self.j.recent_comments([issue])
        self.assertEqual(len(calls), 1)  # cached
        self.j.recent_comments([{**issue, "updated": "2099-01-02T00:00:00.000Z"}])
        self.assertEqual(len(calls), 2)

    def test_labels_are_one_update(self) -> None:
        self.http.routes[("PUT", "/rest/api/2/issue/PROJ-1")] = lambda q, b: None
        self.j.set_labels("PROJ-1", ["claude-approved"], ["claude-awaiting-approval"])
        self.assertEqual(self.http.requests[-1][3], {"update": {"labels": [
            {"remove": "claude-awaiting-approval"}, {"add": "claude-approved"}]}})

    def test_transition_by_target_status_name(self) -> None:
        self.http.routes[("GET", "/rest/api/2/issue/PROJ-1/transitions")] = lambda q, b: {"transitions": [
            {"id": "11", "name": "Start work", "to": {"name": "In Progress"}},
            {"id": "31", "name": "Finish", "to": {"name": "Done"}}]}
        self.http.routes[("POST", "/rest/api/2/issue/PROJ-1/transitions")] = lambda q, b: None
        self.assertIn("In Progress", self.j.transition("PROJ-1", "in progress"))
        self.assertEqual(self.http.requests[-1][3], {"transition": {"id": "11"}})
        self.assertIn("no transition", self.j.transition("PROJ-1", "Review"))

    def test_markers_survive_jira_escaping(self) -> None:
        m = self.j.plan_marker("PROJ-12", 3, "10045")
        self.assertEqual(m, "{{qm-plan issue=PROJ-12 rev=3 fix=10045}}")
        escaped = "Plan\n{{qm\\-bot}}\n{{qm\\-plan issue=PROJ\\-12 rev=3 fix=10045}}"
        self.assertTrue(self.j.is_bot(escaped))
        self.assertEqual(self.j.plan_markers(escaped), [("PROJ-12", 3, "10045")])

    def test_missing_token_is_a_clear_error(self) -> None:
        from quartermaster.trackers.base import TrackerError
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": ""}):
            with self.assertRaisesRegex(TrackerError, "JIRA_API_TOKEN"):
                self.j.whoami()


class NormTimeTest(unittest.TestCase):
    def test_formats_compare_as_strings(self) -> None:
        self.assertEqual(norm_time("2026-11-14T10:00:05Z"), "2026-11-14T10:00:05.000Z")
        self.assertEqual(norm_time("2026-11-14T04:00:05.123-0600"), "2026-11-14T10:00:05.123Z")
        self.assertLess(norm_time("2026-11-14T10:00:05Z"), norm_time("2026-11-14T04:00:05.123-0600"))


if __name__ == "__main__":
    unittest.main()
