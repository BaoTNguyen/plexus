"""The system of record for goals.

Write order is fixed: ledger first (must succeed, fsynced), spine second
(best effort — heart's emit() swallows everything by design). State is
decided from the ledger alone; the journal is only ever a staleness signal.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from pathlib import Path

from . import events, vascular_state

# escalations pause the run; without this you only find out by polling `status`
_NOTIFY_ON = {"escalation.raised"}


def _notify(root: str | Path, rec: dict) -> None:
    """Best-effort hook for the kinds that need a human. Never raises: the
    ledger write already succeeded, and a broken notifier must not look like a
    failed record."""
    try:
        cfg = tomllib.loads((Path(root) / "plexus.toml").read_text())
        cmd = cfg.get("notify", {}).get("cmd")
        if not cmd:
            return
        subprocess.run(cmd, shell=True, timeout=30, capture_output=True, env={
            **os.environ,
            "PLEXUS_KIND": rec["kind"],
            "PLEXUS_GOAL": rec["goal_id"],
            "PLEXUS_FEATURE": rec.get("feature_id", ""),
            "PLEXUS_REASON_CLASS": rec.get("reason_class", ""),
            "PLEXUS_REASON": str(rec.get("reason", ""))[:2000],
        })
    except Exception:
        pass


def ledger_path(root: str | Path = ".") -> Path:
    return vascular_state.ledger_path(root)


def record(
    kind: str,
    *,
    goal_id: str,
    feature_id: str | None = None,
    root: str | Path = ".",
    operator: bool = False,
    **detail,
) -> dict:
    """`operator`: a person did this (approve, amend, resolve, merge, manual
    validation). Its event carries origin=operator, which is how pulse counts
    my attention per landed feature (F29)."""
    rec: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": kind,
        "goal_id": goal_id,
    }
    if feature_id:
        rec["feature_id"] = feature_id
    if operator:
        rec["operator"] = True
    rec.update(detail)
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    events.emit(kind, goal_id=goal_id, feature_id=feature_id,
                origin="operator" if operator else None, **detail)
    if kind in _NOTIFY_ON:
        _notify(root, rec)
    return rec


def read(root: str | Path = ".") -> list[dict]:
    path = ledger_path(root)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn tail line from a crash mid-append
    return out


#: feature_id for an escalation about the goal as a whole -- the final-suite
#: regression has no feature, and without a key it could be neither seen nor
#: resolved.
GOAL_LEVEL = "__goal__"


def open_escalations(recs: list[dict], goal_id: str) -> list[dict]:
    """The goal's open escalations, one per feature, latest raise last.

    The one rule for "is this blocked": status, the dashboard, resolve and
    prune all ask here. Records are walked in order with a per-feature count
    that never drops below zero, so resolving the same block twice does not
    bank a credit that silently cancels the next one. Each entry carries the
    latest raise's reason and the episodes raised since the feature was last
    clear -- the ones still explaining an open question.
    """
    count: dict[str, int] = {}
    last: dict[str, tuple[int, dict]] = {}
    episodes: dict[str, list[str]] = {}
    for i, r in enumerate(recs):
        if r.get("goal_id") != goal_id or r.get("kind") not in (
                "escalation.raised", "escalation.resolved"):
            continue
        fid = r.get("feature_id") or GOAL_LEVEL
        if r["kind"] == "escalation.raised":
            count[fid] = count.get(fid, 0) + 1
            last[fid] = (i, r)
            episodes.setdefault(fid, []).extend(r.get("episode_ids") or [])
        else:
            count[fid] = max(0, count.get(fid, 0) - 1)
            if not count[fid]:
                episodes.pop(fid, None)
    open_ids = sorted((f for f, n in count.items() if n > 0), key=lambda f: last[f][0])
    return [{"feature_id": f, "count": count[f],
             "reason_class": last[f][1].get("reason_class", "?"),
             "reason": last[f][1].get("reason", ""),
             "episode_ids": episodes.get(f, [])} for f in open_ids]


def resolve(root: str | Path, goal_id: str, feature_id: str, answer: str) -> list[str] | None:
    """Record an answer to an open escalation. Returns None when it was
    recorded, else the feature ids that *are* open: a typo'd id, or a second
    tab resolving what the first already did, must not write a resolution for
    a block that is not there."""
    open_ids = [e["feature_id"] for e in open_escalations(read(root), goal_id)]
    if feature_id not in open_ids:
        return open_ids
    record("escalation.resolved", goal_id=goal_id, feature_id=feature_id, root=root,
           resolution=answer, operator=True)
    return None
