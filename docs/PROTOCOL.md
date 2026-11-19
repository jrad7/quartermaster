# The Quartermaster protocol

Work arrives as tracker issues (Jira or GitHub). Claude triages each one into a tasked plan,
posts the plan's overview to the issue, and waits. Only a plan the owner has approved is
worked on. The owner drives it with comment commands that Quartermaster, the poller on the dev
machine, turns into headless Claude Code runs.

| Step | Skill (default) | Writes |
|------|-----------------|--------|
| Triage and plan | `/qm-plan KEY` | `PLANS/issue-KEY.md`, plan comment, status label |
| Work an approved plan | `/qm-work KEY` | branch, commits, local merge, progress comments |

`KEY` is the issue key: `PROJ-12` on Jira, `42` on GitHub. `PLANS` is the config's `plan_dir`.
The skills Quartermaster starts are set by the config's `commands`.

## 1. Lifecycle

```
open issue ──plan or auto──▶ planning ──▶ awaiting-approval ──approve──▶ approved ──▶ in-progress ──▶ done
                                │              ▲    │                                    │
                                ▼              │    └──revise <feedback>──▶ (new rev)    ▼
                           needs-info ──owner reply──┘                                blocked
                                                                       owner reply + continue ┘
done ──test it, then fix (starts at once) or revise (waits for approve)──▶ a new round
```

- **Triage never changes code.** It writes only the plan file, a comment and a status.
- **Nothing starts on its own.** A plan is worked only after an owner `approve` newer than
  the plan's latest revision (§4). Revising a plan voids earlier approvals.
- **Nothing is pushed by Quartermaster.** There is no push command. What the work skill does
  at the end (merge locally, open a PR) is the project's rule, in that skill.

## 2. Owner commands

A comment from the owner whose first line starts with the command prefix (`/` by default,
`command_prefix` in the config) followed by a command word. Text after it, and later lines,
are notes the run reads.

| Command | Effect |
|---------|--------|
| `/plan` | Triage and plan this issue. Needed only for issues not auto-planned (opened by someone else, or labelled `<prefix>skip`). |
| `/revise <feedback>` | Rework the plan. Posts a new revision and voids earlier approvals. On a plan with work done, adds a round that waits for approve. |
| `/fix <correction>` | A small correction while testing: planned as a round and worked as soon as the code lane is free; the fix comment is its approval. |
| `/approve [notes]` | Approve the latest revision; decisions go as recommended unless the notes say otherwise. Work starts. |
| `/continue [answer]` | Resume an approved plan, after answering a `blocked` question or after a stop. |
| `/cancel` | Drop the plan: status `cancelled`. The issue stays open. |
| `/status` | Reply with the plan's status, its tasks and the approval state. |

A plain reply (no command) on a `needs-info` issue counts as the answer and re-runs triage.
Unknown commands (including `/push`) get the help text.

**Jira Cloud's editor** opens its insert menu when you type `/`. Keep typing (`/approve`) and
press Escape or a space before Enter if the menu offers something; or set `command_prefix` to
`!` and use `!approve`.

## 3. Status labels

Exactly one status label at a time, set with `quartermaster label KEY <status>`. The label is
`label_prefix` + status: `claude-planning` by default on Jira, `claude:planning` on GitHub.

| Status | Meaning |
|--------|---------|
| `planning` | triage is running |
| `needs-info` | questions or a recommendation to close are posted; waiting on the owner |
| `awaiting-approval` | plan posted; waiting on approve or revise |
| `approved` | approval verified; work queued |
| `in-progress` | tasks are being done, one per run; the poller starts the next run |
| `blocked` | a task hit a question only the owner can answer |
| `done` | the plan's work is finished; test it, then fix or revise for another round |
| `cancelled` | owner cancelled the plan |
| `skip` (label) | owner-set: never auto-plan this issue |

On Jira, `transitions` in the config can also move the issue through its workflow when a
status is set, such as `{"in-progress": "In Progress", "done": "In Review"}` (matched against
the transition name or its target status, case-insensitively). Do not map a status to a
Done-category status: the default query only sees open issues, so later comments on that
issue would go unseen.

## 4. Approval rule

Every comment Claude or the poller posts carries the bot marker (`qm-bot`: `<!-- qm-bot -->`
on GitHub, `{{qm-bot}}` on Jira) and never starts with the command prefix. A plan overview also
carries the plan marker, from `quartermaster marker KEY R`: `qm-plan issue=KEY rev=R`.

A plan revision R is **approved** when a comment on the issue:

1. is by the owner (config `owner`; `"me"` means the account the tracker credentials act as),
2. does not carry the bot marker,
3. has a first line starting with `<prefix>approve`, and
4. was created after the comment carrying the plan marker for the plan file's current revision.

A fix round is approved instead by the owner fix comment its marker names
(`qm-plan issue=KEY rev=R fix=<comment id>`): it holds only if that comment is the owner's,
starts with `<prefix>fix`, and is older than the marker.

`quartermaster approval KEY` checks this and exits 0 only when it holds. The skills and the
poller use it; nothing else counts, not a label and not a plan file's Status line.

**Same account.** If the poller's credentials are the owner's own account, bot comments are
told apart by the marker alone. To get notified of Claude's comments, use a second account for
the poller and set `owner` to your own account id.

## 5. Plan files

`PLANS/issue-KEY.md`, moved to `PLANS/completed/` when finished. Quartermaster reads three
things from it: the `**Revision:** R` line, the `**Status:**` line (which `cancel` rewrites),
and task lines `- [ ] **<id> <title>**` / `- [x] ...` (a resume that ticks one made progress).

## 6. The poller

`quartermaster watch` polls every `interval` seconds and hands each owner command to headless
Claude Code (`claude -p`, `--permission-mode` from the config, default `bypassPermissions`),
in one of two lanes:

- **code lane:** approve, continue, the work after a fix, and resumes. One run, so one task,
  at a time. It finishes one issue before starting another: its focus is the issue it is
  coding, else the `in-progress` plan, else the oldest `approved` one. An approve or continue
  on another issue waits, unseen, until the focus plan is done, unless the focus ran out of
  resumes (`max_resumes` runs in a row that ticked nothing) or the command is on a stuck
  `in-progress` plan.
- **plan lane:** plan, revise, the planning half of fix, an answer on a `needs-info` issue,
  and auto-plans (the oldest open issue opened by the owner with no status label and no plan;
  `auto_plan: false` turns it off, `auto_plan_any_author: true` takes anyone's).

An issue never has two runs at once, and its commands are taken in the order written: a
command that must wait holds back every later comment on that issue. status and unknown
commands are answered at once; cancel waits for that issue's run. `parallel: false` runs one
session at a time across both lanes.

Safety: only the owner's comments are commands. Comments are marked seen in
`STATE/state.json`; the first poll marks everything already there as seen. A `flock` on the
state dir allows one poller per project. Because runs bypass permission prompts, keep the
tracker project's write access short.

Each run's environment is the poller's plus the config's `run.env` (and `run.plan_env` in the
plan lane), with `QM_CONFIG`, `QM_LANE` and `QM_ISSUE` set. Its system prompt says it is
headless, names the tracker's markup, says never to push (unless `run.allow_push`), and adds
`run.note` / `run.plan_note`.

A run that has written a `result` event, has no background task pending, and has not exited
`exit_grace` seconds (120) later is ended with everything under it. A result written while
background tasks are pending waits up to `bg_max` seconds (5400). Logs: `STATE/logs/`, one
stream-json file per run (`quartermaster tail [-f] [--lane code|plan]`).

## 7. Jira specifics

- REST v2 for issues and comments (wiki-markup bodies on Cloud and Server alike); search uses
  `/rest/api/3/search/jql` on Cloud, `/rest/api/2/search` on Server/Data Center.
- Jira has no "all recent comments" call, so each poll reads the open-issue list and fetches
  comments only for issues whose `updated` time moved since the last poll.
- The open-issue query is `tracker.jql`, default
  `project = "<project>" AND statusCategory != Done ORDER BY created ASC`. Comments on issues
  outside it are not seen.
- No reactions on Jira: the poller's "started / finished" signals are its comments and the
  status labels.
- Labels are created on first use; they cannot contain spaces.
