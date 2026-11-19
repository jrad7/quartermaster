# Quartermaster

Drive Claude Code from your issue tracker. Quartermaster watches a project's Jira (Cloud or
Server/Data Center) or GitHub issues and turns the owner's comment commands into headless
`claude -p` runs on your machine. Claude triages each issue into a tasked plan and posts it;
when you comment `/approve`, it works the plan one task per session, posting progress, until
it is done. You test, then `/fix` or `/revise` for another round.

Python 3.10+ standard library only, no install step. Protocol: [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Set up a project

1. **Put the command on your PATH:**
   `ln -s ~/quartermaster/bin/quartermaster ~/.local/bin/quartermaster`
2. **Write the config** in the project's root (where Claude should run):
   `cd ~/src/myproject && quartermaster init jira` (or `github`), then edit
   `quartermaster.json`: the tracker URL and project key, `name`, `plan_dir`.
3. **Credentials** go in the environment, never in the file. Jira Cloud: `JIRA_EMAIL` and
   `JIRA_API_TOKEN` (an API token from id.atlassian.com). Jira Server/DC: a personal access
   token in `JIRA_API_TOKEN` with `"auth": "bearer"`. GitHub: `gh auth login`, or `GH_TOKEN`.
4. **Install the skills** the runs use: copy `templates/skills/qm-plan` and
   `templates/skills/qm-work` into the project's `.claude/skills/`, and fill in each one's
   **Project rules** section (rules file, check command, branch and merge rules).
5. **Check it:** `quartermaster check` reads the issues, prints who the credentials act as,
   and warns about a missing plan dir or skill. Create the plan dir it names.
6. **Try one poll by hand:** `quartermaster once`. The first poll only marks existing comments
   as seen. Then comment `/plan` on an issue and run `quartermaster once` again.

Then run `quartermaster watch`, or the systemd service below.

## Config (`quartermaster.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `name` | the directory name | project name: state dir, unit instance, logs |
| `tracker` | — | `{"type": "jira", "url", "project", "jql", "auth", "email_env", "token_env", "deployment"}` or `{"type": "github", "repo"}` |
| `owner` | `"me"` | the account whose comments are commands: Jira accountId (Cloud) or username (Server), GitHub login; `"me"` = the credentials' own account |
| `workspace` | `.` | where Claude runs (relative to the config file) |
| `plan_dir` | `planning/issues` | plan files (relative to the workspace) |
| `state_dir` | `~/.local/state/quartermaster/<name>` | state, status, run logs |
| `label_prefix` | `claude-` (Jira), `claude:` (GitHub) | status labels are prefix + status |
| `command_prefix` | `/` | what starts a command comment (`!` avoids Jira's slash menu) |
| `commands` | `/qm-plan {key}`, `... revise`, `... fix`, `/qm-work {key}` | the prompts runs start with; point them at your own skills |
| `transitions` | `{}` | Jira only: status → workflow transition, e.g. `{"in-progress": "In Progress"}` |
| `interval` | 120 | seconds between polls |
| `parallel` | true | run the code and plan lanes side by side |
| `auto_plan`, `auto_plan_any_author` | true, false | plan new owner-opened issues without `/plan` |
| `max_resumes` | 3 | runs in a row that tick no task before resumes stop |
| `exit_grace`, `bg_max` | 120, 5400 | stuck-run limits (PROTOCOL §6) |
| `claude`, `model`, `max_budget_usd`, `permission_mode` | `claude`, —, —, `bypassPermissions` | how runs are started |
| `run` | — | `env`, `plan_env` (extra env for each lane), `path_prepend`, `note`, `plan_note` (added to the system prompt), `allow_push` |
| `dashboard` | — | `port` (8770), `service` (the systemd unit name its buttons control) |

## Commands

```
quartermaster check | info | status
quartermaster once | watch
quartermaster tail [-f] [--lane code|plan] [FILE]
quartermaster dashboard [--port N]
quartermaster issue KEY | approval KEY | label KEY STATUS | marker KEY REV [--fix ID] | comment KEY FILE
quartermaster oldest [--any] | labels | init jira|github
```

The second-to-last line is what the skills call. The project is the nearest
`quartermaster.json` at or above the working directory, or `--config` / `$QM_CONFIG`
(runs get `QM_CONFIG` set, so they find it from any worktree).

## Run it as a service

Two systemd template units, one instance per project:

```bash
sed "s/YOUR_USER/$USER/g" systemd/quartermaster@.service | sudo tee /etc/systemd/system/quartermaster@.service
sed "s/YOUR_USER/$USER/g" systemd/quartermaster-dashboard@.service | sudo tee /etc/systemd/system/quartermaster-dashboard@.service
mkdir -p ~/.config/quartermaster && cp templates/project.env.example ~/.config/quartermaster/myproject.env
chmod 600 ~/.config/quartermaster/myproject.env   # then fill in QM_CONFIG and the tokens
sudo systemctl daemon-reload
sudo systemctl enable --now quartermaster@myproject quartermaster-dashboard@myproject
journalctl -u quartermaster@myproject -f
```

Several projects run side by side: each has its own config, env file, state dir, unit
instance and dashboard port. The poller restarts itself between runs when this checkout's
code changes.

## The dashboard

`quartermaster dashboard` serves a local page (http://127.0.0.1:8770/ by default): the poller's
state and countdown, the runs in each lane with live output, issues with their plans and task
progress, every run log, the `claude` processes on the machine, and the service log. Controls:
start/stop/restart the service (`sudo -n systemctl`, needs a NOPASSWD rule), poll now, stop a
run. It binds to 127.0.0.1 and refuses cross-site requests. It cannot post comments: approvals
stay comments you wrote on the issue.

## Tests

```bash
PYTHONPATH=.:tests python3 -m unittest discover -s tests
```

## Origin

A generic copy of the poller built for wow-dungeon-idle, where it still runs unchanged. The
differences: pluggable trackers (Jira added), string issue keys, a per-project config, no push
command, no disk checks, and project-neutral skills and prompts.
