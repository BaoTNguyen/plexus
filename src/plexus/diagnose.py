"""The judgment surface: tell what went wrong per feature in terms of
intent / logs / traces / bugs, and roll failures up by phase.

This is the read side that makes semi-autonomous runs reviewable — it turns a
failed feature into an evidence bundle a human can apply taste to, and it
never holds the only copy of anything: every attempt points back to a
`pulse episode <id>` for the raw logs/traces/diff (LEDGER's reference-don't-copy
law), so manual verification is always one command deeper.

Phase attribution maps heart/plexus failure classes onto the 40/20/40 phases:

  coding   — the agent fumbled producing or landing a valid change
  testing  — a check caught a defect (a bug), or in v0 a possible intent gap
  intent   — the goal/criterion itself was wrong (plan-time or scope-level)

Acceptance is un-collapsed from heart's verifier (run.py runs the criterion in
its own worktree), so an `acceptance_failed` record carries the episode's
regression outcome alongside plexus's acceptance bit, and the 2x2 splits
"wrong plan" from "wrong code":

  acceptance fail + no regression  -> intent   (code coherent, criterion unmet)
  acceptance fail + regression     -> coding   (broadly broken)
  acceptance pass + regression     -> testing  (works but broke existing tests)

Records written before the un-collapse (or by a collapsed path) carry no pair;
they fall back to the failure-class-only map and stay `testing`.
"""
from __future__ import annotations

from collections import Counter

from .ledger import read

# failure_class (LEDGER's growing enum) -> phase, when the 2x2 pair is absent.
_PHASE: dict[str, str] = {
    "no_change": "coding",
    "apply_failed": "coding",
    "path_violation": "coding",
    "episode_error": "coding",
    "timeout": "coding",
    "guardrail_violation": "coding",
    "review_rejected": "coding",
    "verify_failed": "testing",
    "acceptance_failed": "testing",
}

# escalation reason_class -> phase, for the goal/plan-level failures that never
# attach to a single attempt. None means "inherit from the feature's attempts".
_ESC_PHASE: dict[str, str | None] = {
    "unverifiable_ground_truth": "intent",
    "blocked_on_decision": "intent",  # the agent confirmed the goal is ambiguous
    "scope_violation": "intent",  # plan and diff disagree on the feature's width
    "regression": "testing",
    "destructive_action": "coding",
    "attempts_exhausted": None,
    "budget_exhausted": None,
}

# what a human should go look at, per phase — the point of the whole surface
_ADVICE = {
    "coding": "the agent could not produce/land a valid change; read the run "
              "logs before blaming the criterion",
    "testing": "the code met the criterion but regressed the existing suite — "
               "a bug in what already worked; read the run logs",
    "intent": "the code is coherent and breaks nothing, it just doesn't meet the "
              "criterion — check whether the criterion itself is right",
}


def classify_phase(failure_class: str, episode_outcome: str | None = None,
                   acceptance_passed: bool | None = None) -> str:
    """Place a failed attempt in the 40/20/40 phases. Coding-class failures are
    unambiguous; the testing-class ones (acceptance_failed / verify_failed)
    refine through the (regression, acceptance) 2x2 when the pair is recorded."""
    base = _PHASE.get(failure_class, "coding")
    if base != "testing":
        return base
    if acceptance_passed is False and episode_outcome in ("pass", "unverified"):
        return "intent"   # coherent, no regression, criterion unmet
        # `unverified` (repo ships no verifier) lands here too: nothing detected a
        # regression, so the criterion is still the thing that went unmet. The
        # certainty note in why() is what flags how much to trust that.
    if acceptance_passed is False and episode_outcome == "fail":
        return "coding"   # both fail -> broken
    if acceptance_passed is True and episode_outcome == "fail":
        return "testing"  # criterion met but regressed the suite
    return "testing"      # no pair (collapsed/old record) — cannot split


def _classify_rec(r: dict) -> str:
    return classify_phase(r.get("failure_class", ""), r.get("episode_outcome"),
                          r.get("acceptance_passed"))


def phase_counts(recs: list[dict]) -> Counter:
    """Failures rolled up by phase across a goal — the 40/20/40 signal: where
    are defects actually being caught? Intent-heavy means under-planned."""
    counts: Counter = Counter()
    for r in recs:
        if r["kind"] == "feature.failed":
            counts[_classify_rec(r)] += 1
        elif r["kind"] == "escalation.raised":
            phase = _ESC_PHASE.get(r.get("reason_class", ""))
            if phase:
                counts[phase] += 1
    return counts


def _feature_meta(recs: list[dict], goal_id: str, feature_id: str) -> tuple[str, str]:
    """(title, acceptance criterion) from the plan — the intent this feature was
    judged against. Lives in plan.created's features[], not on the per-attempt
    records."""
    for r in recs:
        if r["kind"] == "plan.created" and r.get("goal_id") == goal_id:
            for f in r.get("features", []):
                if f.get("feature_id") == feature_id:
                    return f.get("title", feature_id), f.get("acceptance", "")
    return feature_id, ""


def _went_wrong(recs: list[dict]) -> list[tuple[str, str]]:
    """(goal_id, feature_id) pairs that failed at least once or escalated,
    in first-seen order."""
    seen: list[tuple[str, str]] = []
    for r in recs:
        if r["kind"] in ("feature.failed", "escalation.raised") and r.get("feature_id"):
            key = (r["goal_id"], r["feature_id"])
            if key not in seen:
                seen.append(key)
    return seen


def why(root: str = ".", feature_id: str | None = None) -> list[str]:
    """Evidence bundle for what went wrong. With no feature_id, one section per
    feature that failed or escalated; otherwise just that feature."""
    recs = read(root)
    if not recs:
        return ["no ledger records"]
    targets = _went_wrong(recs)
    if feature_id:
        targets = [t for t in targets if t[1] == feature_id]
        if not targets:
            return [f"feature {feature_id}: no failures or escalations recorded"]

    out: list[str] = []
    for goal_id, fid in targets:
        frecs = [r for r in recs
                 if r.get("goal_id") == goal_id and r.get("feature_id") == fid]
        title, crit = _feature_meta(recs, goal_id, fid)
        out.append(f"feature {fid}: {title}")
        # intent — the criterion this feature was judged against
        if crit:
            out.append(f"  intent (acceptance criterion): {crit}")

        # per-attempt: outcome, phase, and the drill-down to raw logs/traces/diff
        phases: Counter = Counter()
        for r in frecs:
            if r["kind"] != "feature.failed":
                continue
            phase = _classify_rec(r)
            phases[phase] += 1
            ep = r.get("episode_id", "?")
            out.append(f"  attempt {r.get('attempt', '?')}: {r.get('failure_class')} "
                       f"[{phase}]  logs/traces/diff: pulse episode {ep}")

        landed = next((r for r in frecs if r["kind"] == "feature.landed"), None)
        if landed:
            out.append(f"  -> later landed on attempt {landed.get('attempt', '?')}")

        # a block is the agent confirming the goal is ambiguous — the signal that
        # turns the intent lean into certainty
        block = next((r for r in reversed(frecs)
                      if r["kind"] == "escalation.raised"
                      and r.get("reason_class") == "blocked_on_decision"), None)
        if block:
            out.append(f"  BLOCKED — agent asked: {block.get('reason', '')}")
            ans = next((r.get("resolution") for r in reversed(frecs)
                        if r["kind"] == "escalation.resolved"), None)
            out.append(f"  answered: {ans}" if ans else "  awaiting an answer")
            phases["intent"] += 1

        esc = next((r for r in reversed(frecs) if r["kind"] == "escalation.raised"
                    and r.get("reason_class") != "blocked_on_decision"), None)
        if esc:
            out.append(f"  escalation [{esc.get('reason_class', '?')}]: "
                       f"{esc.get('reason', '')}")

        # the verdict: which phase, how certain, and what the human should evaluate
        if phases:
            phase = phases.most_common(1)[0][0]
            certainty = ""
            if phase == "intent":
                certainty = (" — confirmed: the agent blocked on this decision" if block
                             else " — unconfirmed: the agent did not block, so this may "
                                  "be incomplete code, not a bad criterion")
            out.append(f"  phase verdict: {phase} ({dict(phases)}){certainty}")
            out.append(f"  look at: {_ADVICE[phase]}")
        out.append("")
    return out
