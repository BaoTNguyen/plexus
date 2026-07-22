"""Export the ledger as training rows, joined to heart's episode records.

This is the bridge marrow needs. LEDGER law 5 keeps heart's verifier reward and
plexus's acceptance label in separate fields forever, which is right — but it
means the interesting cell of the 2x2 (heart green, acceptance red: the agent
built the wrong thing correctly) exists only as a *join*, and nothing performs
that join. Anything reading heart alone sees a clean pass.

One row per episode, because the episode is what marrow trains on. Reward comes
from `runs/<episode_id>/episode.json` rather than the ledger — the ledger
records ids and points at heart for the rest (law 3, reference don't copy), so
export is where the two are finally brought together. That also fixes the order
of operations: export before `plexus prune`, or the rewards are already gone.

Blocked episodes are exported too, with a null reward and `label="blocked"`.
They are not noise to be dropped: whether an abstention was warranted is the
training signal for asking-instead-of-guessing, and the answer text is right
there in the resolution.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import ledger
from .diagnose import _ESC_PHASE, classify_phase


def _episode_facts(root: Path, runs_dir: str, episode_id: str) -> dict:
    """outcome/reward/diff size from heart's own record of the episode. Missing
    is normal (pruned, or a run from another machine) — the ledger row still
    stands on its own, it just carries no reward."""
    path = Path(root) / runs_dir / episode_id / "episode.json"
    if not path.exists():
        return {}
    try:
        ep = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "episode_outcome": ep.get("outcome"),
        "heart_reward": (ep.get("reward") or {}).get("total"),
        "diff_lines": ep.get("diff_lines"),
        "agent": ep.get("agent"),
        "blocked_reason": ep.get("blocked_reason"),
        "task_id": ep.get("task_id"),
    }


def _attempt_from_task_id(task_id: str | None) -> int | None:
    """`<goal>-<feature>-a<n>` (events.make_task_id). Blocked episodes are only
    ever named by escalation.raised, which records ids but no attempt number —
    heart's copy of the task_id is the one place it survives."""
    if not task_id:
        return None
    head, _, tail = str(task_id).rpartition("-a")
    if not head or not tail.isdigit():
        return None
    return int(tail)


def _criteria(recs: list[dict]) -> dict[tuple[str, str], str]:
    """(goal_id, feature_id) -> acceptance command, from plan.created."""
    out: dict[tuple[str, str], str] = {}
    for r in recs:
        if r["kind"] == "plan.created":
            for f in r.get("features", []):
                out[(r["goal_id"], f.get("feature_id"))] = f.get("acceptance", "")
    return out


def _label(row: dict) -> str:
    """The one-word verdict marrow sorts on. `blocked` is deliberately not
    folded into a failure: the agent choosing not to guess is a different act
    from an attempt that fell short, and merging them relearns the bug that
    scored a block like a cheap win."""
    if row.get("episode_outcome") == "blocked":
        return "blocked"
    if row.get("landed"):
        return "landed"
    if row.get("acceptance_passed") is False and row.get("episode_outcome") == "pass":
        return "wrong_thing_built"  # the hard negative: heart green, criterion unmet
    if row.get("acceptance_passed") is False:
        return "failed"
    if row.get("acceptance_passed") is True:
        return "regressed"  # criterion met, existing suite broke
    return "unscored"


def build_rows(root: str | Path = ".", runs_dir: str = "runs") -> list[dict]:
    root = Path(root)
    recs = ledger.read(root)
    criteria = _criteria(recs)
    spec_hash = next((r.get("spec_hash") for r in recs if r["kind"] == "goal.started"), None)

    rows: dict[str, dict] = {}

    def row_for(episode_id: str, r: dict) -> dict:
        row = rows.setdefault(episode_id, {
            "episode_id": episode_id,
            "task_id": r.get("task_id"),
            "goal_id": r.get("goal_id"),
            "feature_id": r.get("feature_id"),
            "attempt": r.get("attempt"),
            "spec_hash": spec_hash,
            "criterion": criteria.get((r.get("goal_id"), r.get("feature_id")), ""),
            "acceptance_passed": None,
            "landed": False,
            "commit": None,
            "failure_class": None,
        })
        # task_id/attempt only appear on some record kinds; fill as they show up
        for k in ("task_id", "attempt"):
            if row.get(k) is None and r.get(k) is not None:
                row[k] = r[k]
        return row

    for r in recs:
        kind = r["kind"]
        if kind == "acceptance.round" and r.get("episode_id"):
            row_for(r["episode_id"], r)["acceptance_passed"] = r.get("passed")
        elif kind == "feature.landed" and r.get("episode_id"):
            row = row_for(r["episode_id"], r)
            row["landed"] = True
            row["commit"] = r.get("commit")
        elif kind == "feature.failed" and r.get("episode_id"):
            row = row_for(r["episode_id"], r)
            row["failure_class"] = r.get("failure_class")
            if r.get("acceptance_passed") is not None:
                row["acceptance_passed"] = r["acceptance_passed"]
        elif kind == "escalation.raised":
            for ep_id in r.get("episode_ids") or []:
                row_for(ep_id, r)["escalation_class"] = r.get("reason_class")

    out = []
    for episode_id, row in rows.items():
        facts = _episode_facts(root, runs_dir, episode_id)
        row["task_id"] = row.get("task_id") or facts.pop("task_id", None)
        facts.pop("task_id", None)
        row.update(facts)
        if row.get("attempt") is None:
            row["attempt"] = _attempt_from_task_id(row.get("task_id"))
        # phase answers "where did this go wrong", so it belongs only to episodes
        # that went wrong. Defaulting it for a landing would put every success in
        # the `coding` bucket and quietly poison the 40/20/40 rollup.
        if row.get("failure_class"):
            row["phase"] = classify_phase(row["failure_class"],
                                          row.get("episode_outcome"),
                                          row.get("acceptance_passed"))
        elif row.get("escalation_class"):
            row["phase"] = _ESC_PHASE.get(row["escalation_class"])
        else:
            row["phase"] = None
        row["label"] = _label(row)
        out.append(row)
    out.sort(key=lambda r: (str(r.get("feature_id")), r.get("attempt") or 0))
    return out


def export(root: str | Path = ".", out_path: str | Path | None = None,
           runs_dir: str = "runs") -> tuple[Path, int, dict]:
    """Write labels.jsonl. Returns (path, rows, label counts)."""
    rows = build_rows(root, runs_dir)
    path = Path(out_path) if out_path else Path(root) / ".plexus" / "labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    return path, len(rows), counts
