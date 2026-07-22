"""The system of record for goals.

Write order is fixed: ledger first (must succeed, fsynced), spine second
(best effort — heart's emit() swallows everything by design). State is
decided from the ledger alone; the spool is only ever a staleness signal.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from pathlib import Path

from . import events

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
    return Path(root) / ".plexus" / "ledger.jsonl"


def record(
    kind: str,
    *,
    goal_id: str,
    feature_id: str | None = None,
    root: str | Path = ".",
    **detail,
) -> dict:
    rec: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": kind,
        "goal_id": goal_id,
    }
    if feature_id:
        rec["feature_id"] = feature_id
    rec.update(detail)
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    events.emit(kind, goal_id=goal_id, feature_id=feature_id, **detail)
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
