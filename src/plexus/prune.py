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
import json
import shutil
from pathlib import Path

from . import ledger


def _exported_ids(root: str | Path) -> set[str]:
    """Episode ids already captured in labels.jsonl — their reward is safe to
    delete because marrow has it. Missing file = nothing exported yet."""
    p = Path(root) / ".plexus" / "labels.jsonl"
    if not p.exists():
        return set()
    out: set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            eid = json.loads(line).get("episode_id")
        except json.JSONDecodeError:
            continue
        if eid:
            out.add(eid)
    return out


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
          runs_dir: str = "runs", force: bool = False) -> list[str]:
    prunable, kept, freed = plan_prune(root, days, runs_dir)
    mb = freed / 1_000_000
    if not prunable:
        return [f"nothing to prune: {len(kept)} episode dir(s) kept "
                f"(referenced or newer than {days:g}d)"]
    lines = [f"{'pruned' if apply else 'prunable'}: {len(prunable)} episode dir(s), "
             f"{mb:.1f} MB   kept: {len(kept)}"]
    if apply:
        # Refuse before deleting, not after: an episode's reward in episode.json
        # is marrow's training signal, captured only once `plexus export` writes
        # it to labels.jsonl. Deleting an un-exported episode discards that data
        # for good, so block it unless the operator forces it.
        exported = _exported_ids(root)
        unexported = [d for d in prunable if d.name not in exported]
        if unexported and not force:
            return [f"refusing to prune: {len(unexported)} of {len(prunable)} episode(s) "
                    f"are not in .plexus/labels.jsonl — their reward is training data "
                    f"that deletion would lose.",
                    "run `plexus export` first, or `plexus prune --apply --force` to "
                    "delete anyway."]
        for d in prunable:
            shutil.rmtree(d, ignore_errors=True)
        lines.append(f"deleted {len(prunable)} dir(s)"
                     + (f" ({len(unexported)} not exported — forced)" if unexported else ""))
    else:
        lines += [f"  {d.name}" for d in prunable[:10]]
        if len(prunable) > 10:
            lines.append(f"  ... and {len(prunable) - 10} more")
        lines.append("re-run with --apply to delete")
    return lines
