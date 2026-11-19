"""Per-project settings: `quartermaster.json`, found by `--config`, `$QM_CONFIG`, or by looking
up from the working directory. JSON, not TOML, so Python 3.10's standard library reads it.

Every path in the file is relative to the file's own directory. Secrets never go in the file:
the tracker section names the environment variables that hold them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "quartermaster.json"

DEFAULT_COMMANDS = {
    "plan": "/qm-plan {key}",
    "revise": "/qm-plan {key} revise",
    "fix": "/qm-plan {key} fix",
    "work": "/qm-work {key}",
}


class ConfigError(SystemExit):
    pass


@dataclass
class Config:
    path: Path
    name: str
    workspace: Path
    plan_dir: Path
    state_dir: Path
    tracker: dict
    owner: str = "me"
    label_prefix: str = "claude-"
    command_prefix: str = "/"
    commands: dict = field(default_factory=lambda: dict(DEFAULT_COMMANDS))
    interval: int = 120
    parallel: bool = True
    auto_plan: bool = True
    auto_plan_any_author: bool = False
    max_resumes: int = 3
    exit_grace: int = 120
    bg_max: int = 5400
    claude: str = "claude"
    model: str | None = None
    max_budget_usd: float | None = None
    permission_mode: str = "bypassPermissions"
    run_env: dict = field(default_factory=dict)
    plan_env: dict = field(default_factory=dict)
    path_prepend: list = field(default_factory=list)
    run_note: str = ""
    plan_note: str = ""
    transitions: dict = field(default_factory=dict)
    dashboard_port: int = 8770
    service: str | None = None
    raw: dict = field(default_factory=dict)

    def label(self, status: str) -> str:
        """The tracker label for a logical status (`planning` -> `claude-planning`)."""
        return self.label_prefix + status


def find_config(explicit: str | Path | None = None, start: Path | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise ConfigError(f"quartermaster: no config file at {p}")
        return p
    if os.environ.get("QM_CONFIG"):
        return find_config(os.environ["QM_CONFIG"])
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        p = d / CONFIG_NAME
        if p.is_file():
            return p
    raise ConfigError(f"quartermaster: no {CONFIG_NAME} in {here} or above it; "
                      "pass --config or set QM_CONFIG")


def _rel(base: Path, value: str | None, default: str) -> Path:
    p = Path(os.path.expanduser(value or default))
    return p if p.is_absolute() else (base / p).resolve()


def load(explicit: str | Path | None = None, start: Path | None = None) -> Config:
    path = find_config(explicit, start)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"quartermaster: {path}: {e}") from None
    return from_dict(data, path)


def from_dict(data: dict, path: Path) -> Config:
    base = path.parent
    name = data.get("name") or base.name
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError(f"quartermaster: name {name!r} must be letters, digits, '.', '_' or '-'")
    tracker = data.get("tracker")
    if not isinstance(tracker, dict) or tracker.get("type") not in ("jira", "github"):
        raise ConfigError("quartermaster: config needs a tracker section with type \"jira\" or \"github\"")
    workspace = _rel(base, data.get("workspace"), ".")
    run = data.get("run") or {}
    dash = data.get("dashboard") or {}
    default_prefix = "claude:" if tracker["type"] == "github" else "claude-"
    cfg = Config(
        path=path,
        name=name,
        workspace=workspace,
        plan_dir=_rel(workspace, data.get("plan_dir"), "planning/issues"),
        state_dir=_rel(base, data.get("state_dir"), str(Path.home() / ".local/state/quartermaster" / name)),
        tracker=tracker,
        owner=str(data.get("owner", "me")),
        label_prefix=data.get("label_prefix", default_prefix),
        command_prefix=data.get("command_prefix", "/"),
        commands={**DEFAULT_COMMANDS, **(data.get("commands") or {})},
        interval=int(data.get("interval", 120)),
        parallel=bool(data.get("parallel", True)),
        auto_plan=bool(data.get("auto_plan", True)),
        auto_plan_any_author=bool(data.get("auto_plan_any_author", False)),
        max_resumes=int(data.get("max_resumes", 3)),
        exit_grace=int(data.get("exit_grace", 120)),
        bg_max=int(data.get("bg_max", 5400)),
        claude=data.get("claude", "claude"),
        model=data.get("model"),
        max_budget_usd=data.get("max_budget_usd"),
        permission_mode=data.get("permission_mode", "bypassPermissions"),
        run_env={k: str(v) for k, v in (run.get("env") or {}).items()},
        plan_env={k: str(v) for k, v in (run.get("plan_env") or {}).items()},
        path_prepend=[os.path.expanduser(p) for p in run.get("path_prepend") or []],
        run_note=run.get("note", ""),
        plan_note=run.get("plan_note", ""),
        transitions=dict(data.get("transitions") or {}),
        dashboard_port=int(dash.get("port", 8770)),
        service=dash.get("service"),
        raw=data,
    )
    for k in ("plan", "revise", "fix", "work"):
        if "{key}" not in cfg.commands[k]:
            raise ConfigError(f"quartermaster: commands.{k} must contain {{key}}")
    if not cfg.command_prefix or cfg.command_prefix.isalnum():
        raise ConfigError("quartermaster: command_prefix must be a symbol such as '/' or '!'")
    return cfg
