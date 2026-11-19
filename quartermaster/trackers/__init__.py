"""Issue tracker backends. `make(cfg)` returns the one the config names."""

from __future__ import annotations

from .base import Tracker, TrackerError, norm_time


def make(cfg) -> Tracker:
    kind = cfg.tracker["type"]
    if kind == "jira":
        from .jira import Jira
        return Jira(cfg.tracker, cfg.workspace)
    if kind == "github":
        from .github import GitHub
        return GitHub(cfg.tracker, cfg.workspace)
    raise TrackerError(f"unknown tracker type {kind!r}")


__all__ = ["Tracker", "TrackerError", "make", "norm_time"]
