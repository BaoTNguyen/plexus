"""Run phase: walk the approved plan serially. Per feature, dispatch a heart
episode, judge it, and either land it (apply + commit, advancing the base for
the next feature) or retry within the attempt budget. Budget exhausted ->
escalate and pause. Re-running resumes from the next open feature.

Two judgments, kept apart on purpose (LEDGER law 5): heart judges with the
repo's own suite (`detect_verifiers`) — its reward is a pure regression /
correctness signal — while plexus runs the feature's acceptance criterion in
its *own* worktree, entirely outside the episode. The two are recorded in
separate fields and can now disagree, which is the whole point: the
(regression, acceptance) 2x2 is what separates "wrong plan" from "wrong code",
and the "heart passed / acceptance failed" cell is the hard negative marrow
cannot otherwise see. Acceptance stays out of heart's reward on purpose —
heart's hidden verifiers dominate its reward (weight 0.45), so routing the
criterion through them would merge the two rewards, which LEDGER law 5 forbids.
Merging them is marrow's job, never plexus's.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

from heart.detect import detect_verifiers
from heart.env import Workspace
from heart.episode import DEFAULT_ROLES, best_episode, run_candidates
from heart.taskspec import TaskSpec

from . import events, ledger
from .plan import load_plan

# heart episode outcomes that are mechanical failures — no valid applied diff to
# judge a criterion against, so acceptance is skipped and it's a coding failure.
_MECHANICAL = {
    "no_change": "no_change",
    "path_violation": "path_violation",
    "apply_failed": "apply_failed",
    "episode_error": "episode_error",
    "timeout": "timeout",
    # a secret in the diff — heart zeroes reward and skips verify, same as a path
    # violation. Kept as its own class so `plexus why` names it instead of hiding
    # a leaked credential behind a generic episode_error.
    "guardrail_violation": "guardrail_violation",
}

# The agent's channel to ask for a decision instead of guessing. A line
# `PLEXUS_BLOCKED: <question>` anywhere in its diff means "I cannot proceed
# without this decided" — the one signal that splits genuine intent ambiguity
# from silent incompletion in the acceptance-fail/no-regression cell.
_BLOCKED_MARKER = "PLEXUS_BLOCKED:"

# ponytail: a block must not be a free escape hatch — cap blocks per feature so a
# lazy agent can't dodge work by asking forever. Exhausting it is itself a strong
# planning signal (the goal is underspecified). A spec knob if it needs tuning.
_BLOCKS_PER_FEATURE = 2


# open lock files, keyed by resolved root. Held for the life of the process: the
# OS drops them on exit, which is exactly the lifetime a run needs, and keeping
# the reference here stops the fd being garbage-collected (which would unlock).
_LOCKS: dict[str, object] = {}


def _lock_goal(root: Path) -> None:
    """One `plexus run` per goal repo. Two concurrent runs would interleave
    commits on the same branch and race `_land` — the second one must say so
    rather than quietly corrupt the goal's history. Re-locking a root this
    process already holds is a no-op; flock is per-open-file-description, so a
    second open() here would otherwise deny us our own lock."""
    key = str(root.resolve())
    if key in _LOCKS:
        return
    path = root / ".plexus" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SystemExit(f"another `plexus run` is already active in {root} "
                         f"(lock: {path}) — one run per goal repo")
    # stamp our pid so the control plane can stop this run regardless of who
    # started it; the flock (not the content) is still what enforces one-per-repo
    f.write(str(os.getpid()))
    f.flush()
    _LOCKS[key] = f


def _episode_cost(episodes: list[dict]) -> dict:
    """Sum usage across every candidate actually run this attempt — best-of-N
    pays for the losing candidates too, so cost must count them, not just the
    winner. A key is omitted when heart couldn't price the agent (usage None),
    so a missing field means 'unknown', never 'zero'. Feeds the durable ledger;
    the live factory-wide total lives on the spool (`plexus stack`)."""
    out: dict = {}
    for k in ("cost_usd", "tokens_in", "tokens_out"):
        vals = [e["usage"][k] for e in episodes
                if (e.get("usage") or {}).get(k) is not None]
        if vals:
            out[k] = round(sum(vals), 6) if k == "cost_usd" else sum(vals)
    return out


def _git(repo: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _head(repo: str | Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _feature_state(recs: list[dict], goal_id: str,
                   feature_id: str) -> tuple[str, int, int]:
    """Pure state derivation from the ledger — the resumable core of the loop.
    Returns (state, next_attempt, budget_used) where state is
    landed|escalated|open. Numbering is monotonic and never reused, but
    `escalation.resolved` resets the *budget* — so budget_used counts attempts
    since the last resolution, not since the feature began (LEDGER)."""
    landed = False
    max_attempt = 0
    open_escalations = 0
    budget_used = 0
    for r in recs:
        if r.get("goal_id") != goal_id or r.get("feature_id") != feature_id:
            continue
        if r["kind"] == "feature.landed":
            landed = True
        elif r["kind"] == "feature.started":
            max_attempt = max(max_attempt, int(r.get("attempt", 0)))
            budget_used += 1
        elif r["kind"] == "escalation.raised":
            open_escalations += 1
        elif r["kind"] == "escalation.resolved":
            open_escalations -= 1
            budget_used = 0  # resolving hands the feature a fresh budget
    if landed:
        return "landed", max_attempt, budget_used
    state = "escalated" if open_escalations > 0 else "open"
    return state, max_attempt + 1, budget_used


def _verifier_tail(ep: dict, limit: int = 1000) -> str:
    """Short failure tail from heart's verifiers for retry_context — the one
    bounded exception to 'reference, don't copy' (LEDGER law 3)."""
    for r in (ep.get("verifier_results") or {}).values():
        if not r.get("passed"):
            return (r.get("output") or "").strip()[-limit:]
    return ""


def _run_acceptance(repo: str | Path, base_commit: str, diff: str,
                    command: str, timeout: int) -> tuple[bool, str]:
    """Plexus's own judgment, in its own clean worktree: check out the base,
    apply the episode's diff, run the feature criterion. Deliberately outside
    heart's episode so heart's reward stays a pure regression signal (see module
    docstring). Reuses heart's Workspace — worktrees belong to heart."""
    ws = Workspace(str(repo), base_commit)
    try:
        try:
            ws.apply(diff)
        except RuntimeError as exc:
            # diff won't apply in plexus's tree either — acceptance can't pass
            return False, f"acceptance could not apply the diff: {exc}"[-1000:]
        try:
            r = subprocess.run(command, shell=True, cwd=str(ws.path),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "acceptance command timed out"
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-1000:]
    finally:
        ws.destroy()


def _resume_answer(recs: list[dict], goal_id: str, feature_id: str) -> str:
    """If the feature's most recent block was answered (escalation.resolved after
    a blocked_on_decision escalation), the text to inject into the next attempt —
    warren's question_answered, in batch form."""
    q = a = None
    for r in recs:
        if r.get("goal_id") != goal_id or r.get("feature_id") != feature_id:
            continue
        if r["kind"] == "escalation.raised":
            q = r.get("reason") if r.get("reason_class") == "blocked_on_decision" else None
            a = None
        elif r["kind"] == "escalation.resolved":
            a = r.get("resolution")
    if q and a:
        return (f"A decision you were blocked on has been answered.\n"
                f"Question: {q}\nAnswer: {a}\nProceed with this decided.")
    return ""


def _blocks_so_far(recs: list[dict], goal_id: str, feature_id: str) -> int:
    return sum(1 for r in recs
               if r.get("goal_id") == goal_id and r.get("feature_id") == feature_id
               and r["kind"] == "escalation.raised"
               and r.get("reason_class") == "blocked_on_decision")


def _diff_paths(repo: str | Path, diff: str) -> list[str]:
    """The files the episode's diff touches, per git itself. `--numstat` with a
    dry run parses the patch without changing anything; the third column is the
    path (binary files report `-`/`-` counts, still with a path)."""
    r = subprocess.run(["git", "-C", str(repo), "apply", "--numstat", "-"],
                       input=diff, text=True, capture_output=True, check=True)
    return [line.split("\t", 2)[2] for line in r.stdout.splitlines() if "\t" in line]


def _land(repo: str | Path, diff: str, feature_id: str) -> str:
    """Commit exactly the paths the diff touched — never `add -A`. The goal repo
    is a real working tree: it holds the user's unrelated edits and plexus's own
    `runs/` episode dumps, and a blanket add would sweep both into the feature
    commit and into the goal's history."""
    paths = _diff_paths(repo, diff)
    subprocess.run(["git", "-C", str(repo), "apply", "--whitespace=nowarn"],
                   input=diff, text=True, capture_output=True, check=True)
    _git(repo, "add", "--", *paths)  # tracks new files so the pathspec commit sees them
    _git(repo, "commit", "-m", f"plexus: land {feature_id}", "--no-verify", "--", *paths)
    return _head(repo)


def _feature_prompt(spec, feat: dict, retry_context: str) -> str:
    parts = [feat["spec"]]
    if spec.context:
        parts.append(f"Repo context: {spec.context}")
    if retry_context:
        parts.append("A previous attempt did not satisfy the acceptance check. "
                     f"Its output tail:\n{retry_context}\nFix the cause.")
    parts.append(
        "If a decision is genuinely missing or the requirements are ambiguous or "
        "contradictory, do not guess. Write a single line "
        f"`{_BLOCKED_MARKER} <the exact decision you need>` to a file named "
        "PLEXUS_BLOCKED and stop — asking is not failing.")
    return "\n\n".join(parts)


def _probe_regression_signal(repo: str, base: str, timeout: int, goal_id: str) -> None:
    """Run the repo's own auto-detected suite twice at HEAD before the loop. The
    regression axis of plexus's coding-vs-intent split *is* heart's pass/fail on
    this suite, so if it's flaky the split can't be trusted. Warn (once per repo,
    via a marker), never block — plenty of real repos have some flake, and the
    operator should decide, not plexus. Best-effort: any error here is silent, the
    run proceeds. Also flags a suite that already fully passes at base (nothing to
    regress → the regression axis is vacuous, not wrong)."""
    marker = Path(repo) / ".plexus" / "verifiers-probed"
    if marker.exists():
        return
    try:
        verifiers = detect_verifiers(repo)
        if not verifiers:
            marker.write_text("no verifiers\n")
            return
        from heart.verify import run_verifiers
        runs = []
        for _ in range(2):
            ws = Workspace(repo, base)
            try:
                runs.append({n: r["passed"]
                             for n, r in run_verifiers(verifiers, str(ws.path), timeout).items()})
            finally:
                ws.destroy()
        marker.write_text("probed\n")
        if runs[0] != runs[1]:
            flaky = sorted(k for k in runs[0] if runs[0].get(k) != runs[1].get(k))
            ledger.record("verifiers.flaky", goal_id=goal_id, root=repo,
                          verifiers=flaky,
                          reason="repo suite gave different results on two identical "
                                 "runs at HEAD — the regression signal is noise, so "
                                 "coding-vs-intent verdicts may be wrong")
        elif all(runs[0].values()):
            ledger.record("verifiers.pass_at_base", goal_id=goal_id, root=repo,
                          reason="repo suite fully passes at HEAD — nothing to regress, "
                                 "so the regression axis is vacuous (landing rides on "
                                 "acceptance alone)")
    except Exception:
        pass


def run(spec, root: str | Path = ".", runs_dir: str | Path = "runs",
        candidates: int = 1) -> int:
    """Walk the plan. Returns 0 (progressed or done) or 1 (escalated, paused)."""
    root = Path(root)
    _lock_goal(root)
    repo = str(root)
    # reclaim any worktrees a previously killed run leaked (safe now: the lock we
    # just took means no live episode for this repo exists)
    try:
        from heart.env import prune_repo_worktrees
        prune_repo_worktrees(repo)
    except Exception:
        pass
    plan = load_plan(root)
    recs = ledger.read(root)

    if not any(r["kind"] == "goal.started" for r in recs):
        ledger.record("goal.started", goal_id=spec.goal_id, root=root,
                      repo=repo, base_commit=_head(repo), spec_hash=spec.spec_hash)
    _probe_regression_signal(repo, _head(repo), spec.timeout, spec.goal_id)

    for feat in plan:
        fid = feat["id"]
        recs = ledger.read(root)
        state, next_attempt, budget_used = _feature_state(recs, spec.goal_id, fid)
        if state == "landed":
            continue
        if state == "escalated":
            return 1  # a human owns this one; do not run past it

        # per-goal episode ceiling — exhaustion escalates, never truncates scope
        episodes_used = sum(1 for r in recs if r["kind"] == "feature.started")
        if episodes_used >= spec.episodes_per_goal:
            ledger.record("escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                          root=root, reason_class="budget_exhausted",
                          reason=f"goal hit {spec.episodes_per_goal}-episode ceiling",
                          episode_ids=[])
            return 1

        # if a prior block was just answered, carry the answer into the next attempt
        retry_context = _resume_answer(recs, spec.goal_id, fid)
        landed = False
        last_episode_ids: list[str] = []
        attempt = next_attempt
        budget = spec.attempts_per_feature - budget_used
        for i in range(budget):
            task_id = events.make_task_id(spec.goal_id, fid, attempt)
            ledger.record("feature.started", goal_id=spec.goal_id, feature_id=fid,
                          root=root, attempt=attempt, task_id=task_id,
                          retry_context=retry_context)
            base = _head(repo)
            task = TaskSpec(
                task_id=task_id, repo_path=repo, base_commit=base,
                prompt=_feature_prompt(spec, feat, retry_context),
                public_verifiers=detect_verifiers(repo),  # heart: regression/correctness
                timeout_seconds=spec.timeout,
                # heart owns the mechanism (outcome="blocked", reward withheld);
                # plexus owns the vocabulary and what a block costs
                blocked_marker=_BLOCKED_MARKER,
            )
            # goal lineage: heart's emit() stamps these into every event of the
            # dispatched episode, so `heart pulse goal <id>` can trace
            # goal -> feature -> episode -> reward (plexus runs serially, so
            # process-global env is safe here)
            os.environ["PLEXUS_GOAL_ID"] = spec.goal_id
            os.environ["PLEXUS_FEATURE_ID"] = fid
            try:
                # retrieval is left on: a feature prompt is fully specified and
                # long, so capillaries' gate skips it on its own (out of the
                # complexity band). No plexus-side declaration needed — the gate
                # separates planner from feature on their inherent characteristics.
                # pipeline: build with heart's implement/test/review roles so a
                # reviewer REJECT blocks the land (run.py already reads
                # review_verdict below). Solo turn otherwise.
                roles = DEFAULT_ROLES if spec.pipeline else None
                cands = run_candidates(
                    task, candidates, agent=spec.agent, agent_cmd=spec.agent_cmd,
                    runs_dir=str(root / runs_dir), roles=roles)
                ep = best_episode(cands)
                attempt_cost = _episode_cost(cands)  # best-of-N pays for all N
            finally:
                os.environ.pop("PLEXUS_GOAL_ID", None)
                os.environ.pop("PLEXUS_FEATURE_ID", None)
            ep_id = ep["episode_id"]
            last_episode_ids.append(ep_id)
            ep_outcome = ep["outcome"]
            review = ep.get("review_verdict")
            diff = (root / runs_dir / ep_id / "diff.patch").read_text()

            # the agent asked for a decision instead of guessing — route it, don't
            # fail it. This is the one signal that makes the intent verdict certain.
            question = ep.get("blocked_reason") if ep_outcome == "blocked" else None
            if question:
                if _blocks_so_far(ledger.read(root), spec.goal_id, fid) >= _BLOCKS_PER_FEATURE:
                    ledger.record(
                        "escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                        root=root, reason_class="attempts_exhausted",
                        reason=f"still blocked after {_BLOCKS_PER_FEATURE} answered "
                               f"decisions — the goal is likely underspecified: {question}",
                        episode_ids=last_episode_ids)
                else:
                    ledger.record(
                        "escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                        root=root, reason_class="blocked_on_decision",
                        reason=question, episode_ids=[ep_id])
                return 1  # a human (later, the router) owes an answer before we go on

            # mechanical failure: no valid applied diff to judge — a coding failure,
            # acceptance never runs. `unverified` is not one of these: the diff
            # applied cleanly, the repo simply ships no verifier for heart to run,
            # and plexus's own criterion is still perfectly judgeable.
            if ep_outcome not in ("pass", "fail", "unverified"):
                ledger.record("feature.failed", goal_id=spec.goal_id, feature_id=fid,
                              root=root, attempt=attempt, task_id=task_id,
                              episode_id=ep_id,
                              failure_class=_MECHANICAL.get(ep_outcome, "episode_error"),
                              episode_outcome=ep_outcome, acceptance_passed=None,
                              reason=f"outcome={ep_outcome}", **attempt_cost)
                retry_context = _verifier_tail(ep)
                attempt += 1
                continue

            # plexus's own judgment, in its own worktree, never in heart's reward
            acc_passed, acc_tail = _run_acceptance(
                repo, base, diff, feat["acceptance"], spec.timeout)
            ledger.record("acceptance.round", goal_id=spec.goal_id, feature_id=fid,
                          root=root, attempt=attempt, task_id=task_id,
                          episode_id=ep_id, passed=acc_passed,
                          episode_outcome=ep_outcome, check=feat["acceptance"])

            # land only when the criterion passes AND nothing regressed AND review ok.
            # `unverified` counts as "nothing regressed": there was no suite to
            # regress, and refusing to land would deadlock every test-less repo.
            if acc_passed and ep_outcome in ("pass", "unverified") and review != "reject":
                commit = _land(repo, diff, fid)
                ledger.record("feature.landed", goal_id=spec.goal_id, feature_id=fid,
                              root=root, attempt=attempt, task_id=task_id,
                              episode_id=ep_id, commit=commit, **attempt_cost)
                landed = True
                break

            # the (regression, acceptance) quadrant → failure_class; diagnose.py
            # reads episode_outcome + acceptance_passed to place the phase
            if review == "reject":
                fclass = "review_rejected"
            elif not acc_passed:
                fclass = "acceptance_failed"
            else:  # criterion passed but the existing suite regressed
                fclass = "verify_failed"
            ledger.record("feature.failed", goal_id=spec.goal_id, feature_id=fid,
                          root=root, attempt=attempt, task_id=task_id,
                          episode_id=ep_id, failure_class=fclass,
                          episode_outcome=ep_outcome, acceptance_passed=acc_passed,
                          reason=f"acceptance={'pass' if acc_passed else 'fail'} "
                                 f"regression={'FAIL' if ep_outcome == 'fail' else 'ok'}",
                          **attempt_cost)
            retry_context = acc_tail if not acc_passed else _verifier_tail(ep)
            attempt += 1

        if not landed:
            ledger.record("escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                          root=root, reason_class="attempts_exhausted",
                          reason=f"{spec.attempts_per_feature} attempts failed "
                                 f"acceptance: {feat['acceptance']}",
                          episode_ids=last_episode_ids)
            return 1

    # scope gate: the full ground-truth suite on the built-up tree
    suite = subprocess.run(spec.suite, shell=True, cwd=repo,
                           capture_output=True, text=True)
    if suite.returncode == 0:
        episodes = sum(1 for r in ledger.read(root) if r["kind"] == "feature.started")
        ledger.record("goal.finished", goal_id=spec.goal_id, root=root,
                      outcome="scope_satisfied", episodes_total=episodes)
        return 0
    # ponytail: v0 escalates on regression; repair-feature synthesis is roadmap #2
    ledger.record("escalation.raised", goal_id=spec.goal_id, root=root,
                  reason_class="regression",
                  reason=f"scope suite failed after all features landed:\n"
                         f"{(suite.stdout + suite.stderr)[-1000:]}",
                  episode_ids=[])
    return 1
