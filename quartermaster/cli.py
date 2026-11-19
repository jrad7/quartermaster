"""quartermaster: the command line. Run `quartermaster --help`.

    quartermaster check                   test the config and the tracker credentials
    quartermaster info                    tracker, markup, owner, labels, plan dir (for skills)
    quartermaster issue KEY               dump an issue and its comments (untrusted data)
    quartermaster oldest [--any]          oldest open issue that still needs a plan
    quartermaster approval KEY            exit 0 iff the plan's current revision is approved
    quartermaster label KEY STATUS        set the single status label (or `none`)
    quartermaster marker KEY REV [--fix COMMENT_ID]
                                          the plan marker text to put in a plan comment
    quartermaster comment KEY FILE        post FILE (`-` for stdin) as a bot-marked comment
    quartermaster labels                  create the status labels (GitHub; Jira needs none)
    quartermaster status                  issues, the runs going, and what starts next (dry run)
    quartermaster once | watch            poll once (and wait for the runs it starts) / forever
    quartermaster tail [-f] [--lane code|plan] [FILE]
                                          a run's stream log, readable (-f follows it)
    quartermaster dashboard [--port N]    the local web page
    quartermaster init jira|github        write a starter quartermaster.json here

The project is the nearest quartermaster.json at or above the working directory, or
--config / $QM_CONFIG.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, core, trackers


def _init(kind: str, force: bool) -> None:
    path = Path.cwd() / config.CONFIG_NAME
    if path.exists() and not force:
        sys.exit(f"quartermaster: {path} exists (--force to overwrite)")
    src = Path(__file__).resolve().parent.parent / "templates" / f"quartermaster.{kind}.json"
    path.write_text(src.read_text())
    print(f"wrote {path}; edit it, then run `quartermaster check`")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="quartermaster", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to quartermaster.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("info")
    sub.add_parser("issue").add_argument("key")
    sub.add_parser("oldest").add_argument("--any", action="store_true", help="any author, not only the owner")
    sub.add_parser("approval").add_argument("key")
    p = sub.add_parser("label")
    p.add_argument("key")
    p.add_argument("status")
    p = sub.add_parser("marker")
    p.add_argument("key")
    p.add_argument("rev", type=int)
    p.add_argument("--fix", help="the owner fix comment id this round answers")
    p = sub.add_parser("comment")
    p.add_argument("key")
    p.add_argument("file")
    sub.add_parser("labels")
    sub.add_parser("status")
    sub.add_parser("once")
    sub.add_parser("watch")
    p = sub.add_parser("tail", help="a run's log, readable; -f to follow it")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--lane", choices=(core.CODE, core.PLAN))
    p.add_argument("log", type=Path, nargs="?")
    p = sub.add_parser("dashboard")
    p.add_argument("--port", type=int)
    p = sub.add_parser("init")
    p.add_argument("kind", choices=("jira", "github"))
    p.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        _init(a.kind, a.force)
        return

    cfg = config.load(a.config)
    try:
        tracker = trackers.make(cfg)
        # Commands that never compare authors need no owner lookup (and no request for it).
        lazy_owner = a.cmd in ("tail", "marker")
        core.configure(cfg, tracker, owner="" if lazy_owner else None)
    except trackers.TrackerError as e:
        sys.exit(f"quartermaster: {e}")

    try:
        _run(a, cfg, tracker)
    except trackers.TrackerError as e:
        sys.exit(f"quartermaster: {e}")


def _run(a, cfg, tracker) -> None:
    if a.cmd == "check":
        issues = tracker.open_issues()
        me = tracker.whoami()
        print(f"config   {cfg.path}")
        print(f"tracker  {tracker.describe()}")
        print(f"auth as  {me}; owner {core.OWNER}{' (same account)' if me == core.OWNER else ''}")
        print(f"issues   {len(issues)} open")
        print(f"plans    {cfg.plan_dir}{'' if cfg.plan_dir.is_dir() else ' (missing: create it)'}")
        print(f"state    {cfg.state_dir}")
        for k, v in cfg.commands.items():
            print(f"command  {k}: {v}")
        skills = cfg.workspace / ".claude" / "skills"
        for k in ("plan", "work"):
            name = cfg.commands[k].split()[0].lstrip("/")
            if not (skills / name / "SKILL.md").exists() and not (Path.home() / ".claude/skills" / name).exists():
                print(f"warning  no skill {name!r} in {skills} or ~/.claude/skills (templates/skills has one)")
    elif a.cmd == "info":
        print(json.dumps({
            "project": cfg.name, "tracker": tracker.kind, "where": tracker.describe(),
            "markup": tracker.markup, "owner": core.OWNER, "command_prefix": cfg.command_prefix,
            "plan_dir": str(cfg.plan_dir), "workspace": str(cfg.workspace),
            "plan_file": str(cfg.plan_dir / "issue-<KEY>.md"),
            "labels": {s: cfg.label(s) for s in core.STATUSES}, "skip_label": cfg.label(core.SKIP[0]),
            "bot_marker": tracker.bot_marker(), "plan_marker_example": tracker.plan_marker("KEY", 1),
            "max_comment_chars": tracker.max_comment,
        }, indent=1))
    elif a.cmd == "issue":
        print(core.dump_issue(a.key))
    elif a.cmd == "oldest":
        issue = core.oldest(any_author=a.any)
        if not issue:
            sys.exit(1)
        print(f"{issue['key']}\t{issue['title']}")
    elif a.cmd == "approval":
        r = core.approval(a.key)
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["approved"] else 1)
    elif a.cmd == "label":
        core.set_status(a.key, a.status)
    elif a.cmd == "marker":
        print(tracker.plan_marker(a.key, a.rev, a.fix))
    elif a.cmd == "comment":
        text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
        c = core.post_comment(a.key, text)
        print(f"{c['id']}\t{c['url']}\t{c['created']}")
    elif a.cmd == "labels":
        core.ensure_labels()
    elif a.cmd == "status":
        core.cmd_status()
    elif a.cmd == "tail":
        core.cmd_tail(a.follow, a.log, a.lane)
    elif a.cmd == "once":
        core.once()
    elif a.cmd == "watch":
        core.watch()
    elif a.cmd == "dashboard":
        from . import dashboard
        dashboard.serve(a.port or cfg.dashboard_port)


if __name__ == "__main__":
    main()
