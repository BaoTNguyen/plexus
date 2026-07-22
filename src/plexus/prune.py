"""Retention for `runs/` — the episode dumps heart writes per attempt.

Not a cleanup script, because the dumps are not scratch. Two things hold
references to them: `plexus why` prints `pulse episode <id>` instead of copying
evidence (LEDGER law 3), and `plexus export` reads reward out of episode.json.
Deleting freely would silently break the drill-down for old failures and throw
away training data that was never exported.

So the rule keeps what is still owed to somebody:

  * episodes cited by an open escalation — a human is mid-decision on those
  * episodes of features that failed and never landed — that is what `why` shows
  * anything newer than the age cutoff

and drops the rest, which is dominated by successful landings whose value has
already been captured in the commit and in labels.jsonl.

Dry run by default. `--apply` deletes.
"""
from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from . import ledger


def _referenced(recs: list[dict]) -> set[str]:
    """Episodes something still points at. Failures are kept only while their
    feature has not landed: once it lands, the failed attempts stop being the
    live explanation for anything and become history the ledger already tells."""
    landed_features = {(r.get("goal_id"), r.get("feature_id")) for r in recs
                       if r["kind"] == "feature.landed"}
    open_escalations: set[str] = set()
    resolved: set[tuple] = set()
    for r in recs:
        if r["kind"] == "escalation.resolved":
            resolved.add((r.get("goal_id"), r.get("feature_id")))

    keep: set[str] = set()
    for r in recs:
        if r["kind"] == "escalation.raised":
            if (r.get("goal_id"), r.get("feature_id")) not in resolved:
                open_escalations.update(r.get("episode_ids") or [])
        elif r["kind"] == "feature.failed" and r.get("episode_id"):
            if (r.get("goal_id"), r.get("feature_id")) not in landed_features:
                keep.add(r["episode_id"])
    return keep | open_escalations


def plan_prune(root: str | Path = ".", days: float = 14,
               runs_dir: str = "runs") -> tuple[list[Path], list[Path], int]:
    """(prunable, kept, bytes_freed). Pure — deletes nothing."""
    root = Path(root)
    base = root / runs_dir
    if not base.is_dir():
        return [], [], 0
    keep_ids = _referenced(ledger.read(root))
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    prunable: list[Path] = []
    kept: list[Path] = []
    freed = 0
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if d.name in keep_ids or d.stat().st_mtime > cutoff:
            kept.append(d)
            continue
        prunable.append(d)
        freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return prunable, kept, freed


def prune(root: str | Path = ".", days: float = 14, apply: bool = False,
          runs_dir: str = "runs") -> list[str]:
    prunable, kept, freed = plan_prune(root, days, runs_dir)
    mb = freed / 1_000_000
    if not prunable:
        return [f"nothing to prune: {len(kept)} episode dir(s) kept "
                f"(referenced or newer than {days:g}d)"]
    lines = [f"{'pruned' if apply else 'prunable'}: {len(prunable)} episode dir(s), "
             f"{mb:.1f} MB   kept: {len(kept)}"]
    if apply:
        for d in prunable:
            shutil.rmtree(d, ignore_errors=True)
        lines.append("run `plexus export` before pruning if you have not — "
                     "reward lives in episode.json, and it is gone now")
    else:
        lines += [f"  {d.name}" for d in prunable[:10]]
        if len(prunable) > 10:
            lines.append(f"  ... and {len(prunable) - 10} more")
        lines.append("re-run with --apply to delete")
    return lines
