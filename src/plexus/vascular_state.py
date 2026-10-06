"""Where plexus's own state lives under .vascular, via vascular_paths.

The only place src/plexus is allowed to spell '.plexus' or build that path by
hand. Everything else should call these.
"""
from __future__ import annotations

from pathlib import Path

from . import vascular_paths


def repo_dir(root: str | Path, component: str) -> Path:
    return vascular_paths.repo_dir(root, component)


def plexus_dir(root: str | Path) -> Path:
    return repo_dir(root, "plexus")


def tasks_path(root: str | Path) -> Path:
    return plexus_dir(root) / "tasks.jsonl"


def ledger_path(root: str | Path) -> Path:
    return plexus_dir(root) / "ledger.jsonl"


def plans_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "plans"


def plan_jsonl_path(root: str | Path) -> Path:
    return plexus_dir(root) / "plan.jsonl"


def plan_log_path(root: str | Path) -> Path:
    return plexus_dir(root) / "plan.log"


def plan_raw_log_path(root: str | Path) -> Path:
    return plexus_dir(root) / "plan.raw.log"


def overview_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "overview"


def labels_path(root: str | Path) -> Path:
    return plexus_dir(root) / "labels.jsonl"


def transcripts_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "transcripts"


def llm_runs_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "llm_runs"


def jobs_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "jobs"


def held_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "held"


def runs_dir(root: str | Path) -> Path:
    return plexus_dir(root) / "runs"


def lock_path(root: str | Path) -> Path:
    return plexus_dir(root) / "lock"


def verifiers_probed_path(root: str | Path) -> Path:
    return plexus_dir(root) / "verifiers-probed"


def discuss_path(root: str | Path, view: str) -> Path:
    return plexus_dir(root) / f"discuss-{view}.md"


def transcript_path(root: str | Path, name: str) -> Path:
    return transcripts_dir(root) / name
