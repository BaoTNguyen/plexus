"""Goal-level read side, per pulse's division of labor: `plexus status` is
the symptom check (exit code as the alert primitive), `pulse episode <id>` is
the cause drill-down. Insights come from the ledger, not the spool — pulse's
full-spool rescan is fine at heart's one-day horizon and wrong at plexus's
multi-week one. `stack` is the factory-wide rollup no single organ has.
"""
from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from pathlib import Path

from .ledger import read


def _pct(values: list[float], q: float) -> float:
    # mirrors heart.pulse._pct; percentiles, never averages
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _last_activity(goal_id: str, goal_records: list[dict]) -> datetime.datetime:
    latest = max(r["ts"] for r in goal_records)
    try:  # the spool adds heart/arteries activity between ledger writes
        from heart.pulse import load_events  # ponytail: full-spool scan, prune by day file if slow
        for e in load_events():
            if ((e.get("payload") or {}).get("goal_id") == goal_id
                    or str(e.get("task_id", "")).startswith(goal_id + "-")):
                if e.get("ts", "") > latest:
                    latest = e["ts"]
    except Exception:
        pass  # spool is best-effort; the ledger alone still answers
    return datetime.datetime.fromisoformat(latest)


def _silent_layers(recs: list[dict], root: str, sample: int = 10) -> str | None:
    """Layers that emitted nothing for the recent episodes.

    A goal repo missing `.arteries` runs to completion looking perfectly healthy
    while memory and retrieval never fire — the failure is invisible in every
    other signal, so it gets its own line. Checked against the spine rather than
    the filesystem so a present-but-broken hook is caught too."""
    eps = {r["episode_id"] for r in recs if r.get("episode_id")}
    if not eps:
        return None
    recent = set(sorted(eps)[-sample:])
    try:
        from heart.pulse import load_events
        events = [e for e in load_events() if e.get("episode_id") in recent]
    except Exception:
        return None  # no spool to judge by; say nothing rather than cry wolf
    seen = {e.get("source") for e in events}
    if not seen:
        return None  # episodes older than the spool's retention — not evidence
    # capillaries emits a gate decision on every turn now (retrieve or skip), so
    # it is genuinely silent only when unwired or its DB is down — the same two
    # causes as arteries, distinguished below.
    missing = [s for s in ("arteries", "capillaries") if s not in seen]
    if not missing:
        return None
    line = (f"NOTE     {'/'.join(missing)} emitted nothing across the last "
            f"{len(recent)} episode(s) — ")
    # silence has two causes with opposite fixes, and telling them apart matters:
    # an unwired repo needs installing, a wired one that went quiet is usually
    # Postgres being unreachable (capillaries retrieval cannot run without it).
    if not (Path(root) / ".arteries").is_dir():
        return line + f"repo not wired; run `arteries setup claude --cwd {root}`"
    return line + "repo is wired, so check `plexus stack` for degraded writes"


def status(root: str = ".", stale_minutes: float = 30) -> tuple[list[str], int]:
    """Symptom check. Exit codes: 0 progressing/done/idle, 1 escalations
    waiting on a human, 2 stalled (ledger says running, no activity)."""
    recs = read(root)
    if not recs:
        return ["OK  no goals recorded"], 0
    goals: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        goals[r["goal_id"]].append(r)
    lines, code = [], 0
    for goal_id, rs in goals.items():
        kinds = [r["kind"] for r in rs]
        if "goal.finished" in kinds:
            outcome = next((r.get("outcome") for r in reversed(rs)
                            if r["kind"] == "goal.finished"), None)
            lines.append(f"DONE     {goal_id}" + (f": {outcome}" if outcome else ""))
            continue
        open_esc = kinds.count("escalation.raised") - kinds.count("escalation.resolved")
        if open_esc > 0:
            last = [r for r in rs if r["kind"] == "escalation.raised"][-1]
            line = (f"BLOCKED  {goal_id}: {open_esc} escalation(s) open, latest "
                    f"{last.get('feature_id', '?')} — {last.get('reason', 'no reason recorded')}")
            if last.get("episode_ids"):
                line += f"  drill down: pulse episode {last['episode_ids'][-1]}"
            lines.append(line)
            code = max(code, 1)
            continue
        age_min = (_now() - _last_activity(goal_id, rs)).total_seconds() / 60
        if age_min > stale_minutes:
            lines.append(f"STALLED  {goal_id}: ledger says running, "
                         f"no activity for {age_min:.0f}m")
            code = max(code, 2)
        else:
            lines.append(f"RUNNING  {goal_id}: last activity {age_min:.0f}m ago")
    # a wiring gap is a note, not an alert: the run is still valid, it is just
    # blind — so it must not change the exit code monitors key off
    gap = _silent_layers(recs, root)
    if gap:
        lines.append(gap)
    return lines, code


def insights(root: str = ".") -> list[str]:
    """Goal-level golden signals from the ledger."""
    recs = read(root)
    if not recs:
        return ["no ledger records"]
    kinds = Counter(r["kind"] for r in recs)
    lines = [
        f"goals: started={kinds['goal.started']} finished={kinds['goal.finished']}  "
        f"features: landed={kinds['feature.landed']} failed-attempts={kinds['feature.failed']}  "
        f"escalations: raised={kinds['escalation.raised']} resolved={kinds['escalation.resolved']}"
    ]
    started: dict[tuple, str] = {}
    failed_first: set[tuple] = set()
    lead: list[float] = []
    rescued = 0
    for r in recs:
        key = (r["goal_id"], r.get("feature_id"))
        if r["kind"] == "feature.started" and key not in started:
            started[key] = r["ts"]
        elif r["kind"] == "feature.failed":
            failed_first.add(key)
        elif r["kind"] == "feature.landed" and key in started:
            dt = (datetime.datetime.fromisoformat(r["ts"])
                  - datetime.datetime.fromisoformat(started[key])).total_seconds()
            lead.append(dt)
            if key in failed_first:
                rescued += 1
    if lead:
        lines.append(f"feature lead time: p50={_pct(lead, .5):.0f}s "
                     f"p95={_pct(lead, .95):.0f}s n={len(lead)}")
    if failed_first:
        lines.append(f"retry rescue: first-attempt-failed={len(failed_first)} rescued={rescued}")
    # durable per-goal spend: run.py stamps each landed/failed attempt with the
    # cost of every candidate it ran. Summed here so a multi-week goal's bill
    # survives the spool's day-scale retention (the spool's live total is `stack`).
    costed = [r for r in recs if r.get("cost_usd") is not None]
    if costed:
        cost = sum(r["cost_usd"] for r in costed)
        tin = sum(r.get("tokens_in") or 0 for r in costed)
        tout = sum(r.get("tokens_out") or 0 for r in costed)
        lines.append(f"cost: ${cost:.4f}  tokens: {tin:,} in / {tout:,} out  "
                     f"over {len(costed)} attempt(s)")
    from .diagnose import phase_counts  # where defects land across plan/code/test
    phases = phase_counts(recs)
    if phases:
        lines.append("failures by phase: " + " ".join(
            f"{p}={phases[p]}" for p in ("intent", "coding", "testing") if phases[p]))
    return lines


def stack(hours: float = 24) -> list[str]:
    """Factory-wide rollup of the shared spool by source — event volume,
    failures, store degradation across heart/arteries/capillaries/marrow/plexus."""
    from heart.pulse import load_events
    cutoff = (_now() - datetime.timedelta(hours=hours)).isoformat()
    events = [e for e in load_events() if e.get("ts", "") >= cutoff]
    lines = [f"window: last {hours:g}h  events={len(events)}"]
    by_source = Counter(e.get("source", "?") for e in events)
    fails = Counter(e.get("source", "?") for e in events
                    if str(e.get("kind", "")).endswith(".failed"))
    for src, n in by_source.most_common():
        line = f"  {src:<12} events={n}"
        if fails.get(src):
            line += f"  failed={fails[src]}"
        lines.append(line)
    degraded = sum(1 for e in events
                   if (e.get("payload") or {}).get("store") in ("jsonl", "lost"))
    if degraded:
        lines.append(f"  DEGRADED: {degraded} ledger write(s) fell back from Postgres")
    # factory-wide spend: sum cost off role.finished only — the atomic
    # per-invocation event. Summing episode.finished as well would
    # double-count: its cost is the sum of its roles.
    priced = [(e.get("payload") or {}) for e in events
              if e.get("kind") == "role.finished"]
    priced = [p for p in priced if p.get("cost_usd") is not None]
    if priced:
        cost = sum(p["cost_usd"] for p in priced)
        tin = sum(p.get("tokens_in") or 0 for p in priced)
        tout = sum(p.get("tokens_out") or 0 for p in priced)
        lines.append(f"  cost: ${cost:.4f}  tokens: {tin:,} in / {tout:,} out  "
                     f"({len(priced)} priced role-turn(s))")
    return lines
