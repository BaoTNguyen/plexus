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
import importlib
import os
import re
import subprocess
from pathlib import Path

from heart.detect import detect_verifiers
from heart.env import Workspace
from heart.episode import DEFAULT_ROLES, best_episode, run_candidates
from heart.taskspec import TaskSpec

from . import events, ledger, scope
from .plan import matches as _matches
from .plan import execution_order, load_plan, parse_expect

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
    # the sandbox refused writes the spec had permitted: a misconfiguration on
    # our side, not a coding failure. Named so `plexus why` says so, and so the
    # retry can widen the scope instead of asking the agent to try harder.
    "scope_denied": "scope_denied",
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
    the live factory-wide total lives on the journal (`plexus stack`)."""
    out: dict = {}
    # cache buckets ride alongside tokens_in rather than inside it: heart prices
    # them at different multipliers, so a consumer that summed them would bill a
    # cache read at ten times its rate. cost_usd already has them folded in.
    for k in ("cost_usd", "tokens_in", "tokens_out",
              "cache_read", "cache_write_5m", "cache_write_1h"):
        vals = [e["usage"][k] for e in episodes
                if (e.get("usage") or {}).get(k) is not None]
        if vals:
            out[k] = round(sum(vals), 6) if k == "cost_usd" else sum(vals)
    return out


def _goal_spend(recs: list[dict]) -> dict:
    """Roll up what a goal has spent: episode count plus summed usage across every
    attempt (feature.landed/failed carry per-attempt cost). Uses distinct
    `spend_*` keys, never `cost_usd`/`tokens_*`, so the per-attempt cost summers
    (observe.insights/report, the fleet cost ceiling) don't double-count this
    aggregate. A usage key is omitted when nothing priced the goal — unknown, not
    zero, same convention as _episode_cost."""
    out: dict = {"episodes_total": sum(1 for r in recs if r["kind"] == "feature.started")}
    for src, dst in (("cost_usd", "spend_usd"), ("tokens_in", "spend_tokens_in"),
                     ("tokens_out", "spend_tokens_out")):
        vals = [r[src] for r in recs if r.get(src) is not None]
        if vals:
            out[dst] = round(sum(vals), 6) if src == "cost_usd" else sum(vals)
    return out


def _missing_upstream(specs: list[str]) -> list[str]:
    """Which of a feature's declared `needs_upstream` symbols aren't importable
    yet. Each spec is 'module.path' or 'module.path:Symbol'. Repos are otherwise
    independent (no cross-repo orchestration), so this is how a downstream goal
    detects that an upstream change it depends on hasn't landed — checked against
    the live editable installs, the same surface `test_*_api_pin.py` guards by
    hand. A miss escalates rather than burning attempts on a doomed run."""
    missing = []
    for spec in specs:
        mod, _, sym = spec.partition(":")
        try:
            m = importlib.import_module(mod)
        except Exception:
            missing.append(spec)
            continue
        if sym and not hasattr(m, sym):
            missing.append(spec)
    return missing


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
                    command: str, timeout: int,
                    expect: tuple[str, list[str]] | None = None,
                    ) -> tuple[bool, str, str]:
    """Plexus's own judgment, in its own clean worktree: check out the base,
    apply the episode's diff, run the feature criterion. Deliberately outside
    heart's episode so heart's reward stays a pure regression signal (see module
    docstring). Reuses heart's Workspace — worktrees belong to heart.

    Returns (passed, tail, stage) where stage names which check said no —
    "acceptance" or "expect". The two are different failures: an acceptance
    failure means the code does not work, an expect failure means it works and
    does not look like what was approved, and only the second is a plan-time
    problem. Both run in the one worktree because building a second to re-run
    the mockup would double the setup cost of every feature that has one."""
    ws = Workspace(str(repo), base_commit)
    try:
        try:
            ws.apply(diff)
        except RuntimeError as exc:
            # diff won't apply in plexus's tree either — acceptance can't pass
            return False, f"acceptance could not apply the diff: {exc}"[-1000:], "acceptance"
        try:
            r = subprocess.run(command, shell=True, cwd=str(ws.path),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "acceptance command timed out", "acceptance"
        if r.returncode != 0:
            return False, (r.stdout + r.stderr).strip()[-1000:], "acceptance"
        if expect:
            ok, tail = _check_expect(str(ws.path), expect, timeout)
            if not ok:
                return False, tail, "expect"
        return True, (r.stdout + r.stderr).strip()[-1000:], ""
    finally:
        ws.destroy()


def _check_expect(cwd: str, expect: tuple[str, list[str]],
                  timeout: int) -> tuple[bool, str]:
    """Run the spec's `expect:` mockup and check the promised lines appear.

    Containment, not equality: a mockup written at plan time cannot predict
    timings, paths or ids, and demanding an exact transcript would fail every
    honest feature. Each promised line must show up somewhere in the output,
    which is enough to catch the case this exists for — the command ran, and
    printed something other than what was signed off."""
    cmd, want = expect
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"expect command timed out: {cmd}"
    got = r.stdout + r.stderr
    missing = [l for l in want if l not in got]
    if not missing:
        return True, ""
    return False, (f"`{cmd}` did not print what the spec promised.\n"
                   + "missing line(s):\n"
                   + "\n".join(f"  {l}" for l in missing[:10])
                   + f"\nactual output:\n{got.strip()[-600:]}")


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


def _held_before(recs: list[dict], goal_id: str, feature_id: str) -> bool:
    """This feature already hit the review hold once. Since a still-open
    escalation would have paused the run before we reached the land branch, a
    prior hold means it was resolved — the operator signed off, so land it now
    instead of holding forever."""
    return any(r.get("goal_id") == goal_id and r.get("feature_id") == feature_id
               and r["kind"] == "escalation.raised"
               and r.get("reason_class") == "held_for_review" for r in recs)


def _diff_paths(repo: str | Path, diff: str) -> list[str]:
    """The files the episode's diff touches, per git itself. `--numstat` with a
    dry run parses the patch without changing anything; the third column is the
    path (binary files report `-`/`-` counts, still with a path)."""
    r = subprocess.run(["git", "-C", str(repo), "apply", "--numstat", "-"],
                       input=diff, text=True, capture_output=True, check=True)
    return [line.split("\t", 2)[2] for line in r.stdout.splitlines() if "\t" in line]


def _stray_paths(paths: list[str], touches: list[str] | None) -> list[str]:
    """Paths the diff touched that the plan never authorised.

    A plan with no `touches` — every plan made before this field existed — is
    unenforced rather than blocked: retroactively refusing to land a resumed
    goal would strand it with no way forward but hand-editing the ledger."""
    if not touches:
        return []
    return [p for p in paths if not any(_matches(p, g) for g in touches)]


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


def _open_pr(spec, root: Path, repo: str) -> str:
    """Push the goal branch and open (or refresh) a PR into `spec.pr_base`, with
    the review report as the body.

    Runs only after the goal is green, and is best-effort throughout: the work is
    already committed locally, so a missing remote, a missing `gh`, or no network
    costs a line of output and never the run's exit code. Nothing about the PR is
    written to the ledger — GitHub is the system of record for a pull request,
    and the ledger is telemetry (LEDGER law 2).

    Why the PR is per-goal and not per-feature: features are ordered and each one
    builds on the commit before it, so parking a risky feature on a side branch
    would strand every feature after it. Risk is gated earlier instead, by
    `review_hold` refusing to land the commit at all until you sign off."""
    if not spec.pr_base:
        return ""
    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == spec.pr_base or branch == "HEAD":
            return f"on {branch}: no PR (nothing to merge into {spec.pr_base})"
        if not _git(repo, "remote"):
            return "no git remote: landed locally, no PR"
        from .review import report
        subprocess.run(["git", "-C", repo, "push", "-u", "origin", branch],
                       capture_output=True, text=True, check=True, timeout=120)
        body = ("Opened by `plexus run` for goal `" + spec.goal_id + "`.\n\n"
                "## What to read\n\n```\n" + report(spec, root, repo) + "\n```\n")
        view = subprocess.run(["gh", "pr", "view", branch, "--json", "url",
                               "-q", ".url"], cwd=repo, capture_output=True,
                              text=True, timeout=60)
        if view.returncode == 0 and view.stdout.strip():
            subprocess.run(["gh", "pr", "edit", branch, "--body", body], cwd=repo,
                           capture_output=True, text=True, timeout=60)
            return f"PR updated: {view.stdout.strip()}"
        made = subprocess.run(
            ["gh", "pr", "create", "--base", spec.pr_base, "--head", branch,
             "--title", f"plexus: {spec.goal_id}", "--body", body],
            cwd=repo, capture_output=True, text=True, timeout=120)
        if made.returncode != 0:
            return f"could not open PR: {(made.stderr or made.stdout).strip()[-300:]}"
        return f"PR opened: {made.stdout.strip().splitlines()[-1]}"
    except FileNotFoundError:
        return "gh CLI not installed: pushed nothing, landed locally"
    except Exception as exc:
        return f"could not open PR: {exc}"


def _feature_prompt(spec, feat: dict, retry_context: str) -> str:
    parts = [feat["spec"]]
    if spec.context:
        parts.append(f"Repo context: {spec.context}")
    if feat.get("touches"):
        # tell the agent the allowlist rather than letting it discover the wall
        # by hitting it — a refused land costs a whole episode
        parts.append(
            "Change ONLY these paths: " + ", ".join(feat["touches"])
            + ". A diff touching anything else is refused and the run stops. "
              "If the feature genuinely cannot be built inside them, do not "
              "widen the diff — report it as a blocked decision instead.")
    if feat.get("contract"):
        parts.append(
            "Public surface this feature is planned to add or change: "
            + "; ".join(feat["contract"])
            + ". Anything else you export publicly gets flagged for review.")
    if parse_expect(feat["spec"]):
        parts.append("The `expect:` block above is not documentation — that "
                     "command is executed after the acceptance check passes and "
                     "every line under it must appear in its output.")
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


def _mark_blocked(root, task_id: str, recs: list[dict],
                  goal_id: str, feature_id: str = "") -> None:
    """Put the escalation's reason on the task.

    A board that shows `blocked` without saying why is a board you have to go
    read a ledger to use, and the whole point of the column is to be the thing
    that tells you what needs you.
    """
    if not task_id:
        return
    from . import tasks as _tasks
    why = next((str(r.get("reason") or r.get("reason_class") or "escalated")
                for r in reversed(recs)
                if r.get("kind") == "escalation.raised"
                and (not feature_id or r.get("feature_id") == feature_id)), "escalated")
    try:
        _tasks.update(root, task_id, state="blocked", error=why[:500])
    except ValueError:
        pass


def run(spec, root: str | Path = ".", runs_dir: str | Path = "runs",
        candidates: int = 1, task_id: str = "") -> int:
    """Run the next ready task, or the one named.

    Tasks are sequential and gated, so the queue decides what starts: with
    nothing named this takes `tasks.next_task()`, which is empty while anything
    is in flight. A repo with no tasks at all still runs its own plan, which is
    how projects that predate tasks keep working.

    The walk is wrapped rather than edited because it exits on escalation from
    seven different places; putting the state transition here means every one
    of them lands on the board instead of the six I would have remembered.
    """
    root = Path(root)
    from . import tasks as _tasks
    if not task_id and _tasks.read(root):
        nxt = _tasks.next_task(root)
        if nxt is None:
            print("no task is ready: everything is blocked, in flight, or done")
            return 0
        task_id = nxt["id"]
    if task_id:
        _tasks.update(root, task_id, state="running", error="")
        print(f"task {task_id}")
    code = _walk(spec, root, runs_dir, candidates, task_id)
    if code == 1 and task_id:
        _mark_blocked(root, task_id, ledger.read(root), spec.goal_id)
    return code


def _walk(spec, root: Path, runs_dir, candidates: int, task_id: str) -> int:
    """Walk the plan feature by feature. 0 progressed or done, 1 escalated."""
    from . import tasks as _tasks
    _lock_goal(root)
    repo = str(root)
    # reclaim any worktrees a previously killed run leaked (safe now: the lock we
    # just took means no live episode for this repo exists)
    try:
        from heart.env import prune_repo_worktrees
        prune_repo_worktrees(repo)
    except Exception:
        pass
    plan = load_plan(root, task_id)
    recs = ledger.read(root)
    plan_id = str(plan[0].get("plan_id", "")) if plan else ""
    if not any(r.get("kind") == "plan.approved"
               and r.get("goal_id") == spec.goal_id
               and r.get("plan_id") == plan_id for r in recs):
        raise SystemExit("current plan is not approved; run `plexus approve` first")

    if not any(r["kind"] == "goal.started" for r in recs):
        ledger.record("goal.started", goal_id=spec.goal_id, root=root,
                      repo=repo, base_commit=_head(repo), spec_hash=spec.spec_hash)
    _probe_regression_signal(repo, _head(repo), spec.timeout, spec.goal_id)

    for feat in execution_order(plan):
        fid = feat["id"]
        recs = ledger.read(root)
        state, next_attempt, budget_used = _feature_state(recs, spec.goal_id, fid)
        if state == "landed":
            continue
        if state == "escalated":
            return 1  # a human owns this one; do not run past it

        # upstream gate: if this feature declares symbols from another repo that
        # haven't landed yet, escalate instead of dispatching a run that can't
        # succeed. The reason names the missing module so the operator (or the
        # router) knows which sibling project's goal to activate first.
        missing = _missing_upstream(feat.get("needs_upstream", []))
        if missing:
            # seed a goal in whichever sibling repo owns the missing symbol, so
            # the upstream work is queued rather than left for the operator to
            # remember. Best-effort: no registry entry -> the escalation stands
            # alone. (See registry.py.)
            try:
                from .registry import seed_upstream
                seeded = seed_upstream(missing, spec.goal_id)
            except Exception:
                seeded = []
            tail = (" — seeded upstream goal(s): "
                    + "; ".join(f"{s} in {r}" for s, r in seeded)) if seeded else \
                   " — land it in the providing project first"
            ledger.record("escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                          root=root, reason_class="upstream_not_ready",
                          reason="depends on upstream not yet available: "
                                 + ", ".join(missing) + tail,
                          episode_ids=[])
            return 1

        # per-goal episode ceiling — exhaustion escalates, never truncates scope
        episodes_used = sum(1 for r in recs if r["kind"] == "feature.started")
        if episodes_used >= spec.episodes_per_goal:
            spend = _goal_spend(recs)
            usd = spend.get("spend_usd")
            ledger.record("escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                          root=root, reason_class="budget_exhausted",
                          reason=f"goal hit {spec.episodes_per_goal}-episode ceiling"
                                 f" — {spend['episodes_total']} episode(s)"
                                 + (f", ${usd:.4f} spent" if usd is not None else ""),
                          episode_ids=[], **spend)
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
                skills=feat.get("skills", []),
                difficulty=feat.get("difficulty", "unknown"),
                effort=feat.get("effort", ""),
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
            # Distil the sandbox's refusals now, while the episode's events are
            # still in the journal. The journal rotates on a day scale and
            # plexus reasons on a multi-week one, so a later scan would return
            # less the older the question gets -- which looks exactly like the
            # system having stopped making mistakes.
            scope.observe(task_id, goal_id=spec.goal_id, feature_id=fid, root=root)
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
            acc_passed, acc_tail, acc_stage = _run_acceptance(
                repo, base, diff, feat["acceptance"], spec.timeout,
                parse_expect(feat["spec"]))
            ledger.record("acceptance.round", goal_id=spec.goal_id, feature_id=fid,
                          root=root, attempt=attempt, task_id=task_id,
                          episode_id=ep_id, passed=acc_passed,
                          episode_outcome=ep_outcome, check=feat["acceptance"],
                          # `passed` stays the round's single verdict — the
                          # expect block is part of plexus's acceptance, not a
                          # second reward — and this names which half said no
                          failed_stage=acc_stage or None)

            # land only when the criterion passes AND nothing regressed AND review ok.
            # `unverified` counts as "nothing regressed": there was no suite to
            # regress, and refusing to land would deadlock every test-less repo.
            if acc_passed and ep_outcome in ("pass", "unverified") and review != "reject":
                # Software-factory boundary: a green feature whose risk class is
                # on the goal's review-hold list waits for a human sign-off
                # instead of auto-landing. Only on the first pass — a prior hold
                # means it was resolved, so land it now (see _held_before).
                from .review import classify
                cls = classify(feat)
                if cls in spec.review_hold and not _held_before(
                        ledger.read(root), spec.goal_id, fid):
                    ledger.record(
                        "escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                        root=root, reason_class="held_for_review",
                        reason=f"{cls}-class feature passed acceptance; policy holds "
                               f"'{cls}' for your sign-off before landing — resolve "
                               f"to land, or amend the plan",
                        episode_ids=[ep_id])
                    return 1
                # Scope gate, last thing before the commit exists. Not a retry:
                # the agent and the plan disagree about how wide the feature is,
                # and only a human can say which of the two is wrong.
                stray = _stray_paths(_diff_paths(repo, diff), feat.get("touches"))
                if stray:
                    ledger.record(
                        "escalation.raised", goal_id=spec.goal_id, feature_id=fid,
                        root=root, reason_class="scope_violation",
                        reason="diff touched paths the plan did not authorise: "
                               + ", ".join(sorted(stray)[:10])
                               + f" (allowed: {', '.join(feat['touches'])})",
                        episode_ids=[ep_id])
                    return 1
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
            elif acc_stage == "expect":
                # the criterion passed and the suite is clean: the code works,
                # it just isn't what the approved mockup showed
                fclass = "expect_mismatch"
            elif not acc_passed:
                fclass = "acceptance_failed"
            else:  # criterion passed but the existing suite regressed
                fclass = "verify_failed"
            ledger.record("feature.failed", goal_id=spec.goal_id, feature_id=fid,
                          root=root, attempt=attempt, task_id=task_id,
                          episode_id=ep_id, failure_class=fclass,
                          episode_outcome=ep_outcome, acceptance_passed=acc_passed,
                          reason=("acceptance=pass but the expect block did not match"
                                  if acc_stage == "expect" else
                                  f"acceptance={'pass' if acc_passed else 'fail'}")
                                 + f" regression={'FAIL' if ep_outcome == 'fail' else 'ok'}",
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
    final = ledger.read(root)
    spend = _goal_spend(final)
    if suite.returncode == 0:
        if spec.manual_checks:
            ledger.record("validation.automated_passed", goal_id=spec.goal_id,
                          root=root, task=task_id,
                          checks=list(spec.manual_checks), **spend)
            if task_id:
                _tasks.update(root, task_id, state="landed")
            return 0
        ledger.record("goal.finished", goal_id=spec.goal_id, root=root,
                      outcome="scope_satisfied", task=task_id, **spend)
        note = _open_pr(spec, root, repo)
        if note:
            ledger.record("delivery.requested", goal_id=spec.goal_id, root=root,
                          task=task_id, result=note)
            print(note)
        if task_id:
            # the PR number, so validation can say which tasks a PR carries
            found = re.search(r"/pull/(\d+)", note or "")
            _tasks.update(root, task_id, state="landed",
                          **({"pr": int(found.group(1))} if found else {}))
        return 0
    # ponytail: v0 escalates on regression; repair-feature synthesis is roadmap #2
    usd = spend.get("spend_usd")
    last_landed = [r.get("episode_id") for r in final
                   if r["kind"] == "feature.landed" and r.get("episode_id")]
    ledger.record("escalation.raised", goal_id=spec.goal_id, root=root,
                  reason_class="regression",
                  reason=f"scope suite failed after all {spend['episodes_total']} "
                         f"episode(s) landed"
                         + (f", ${usd:.4f} spent" if usd is not None else "")
                         + f":\n{(suite.stdout + suite.stderr)[-1000:]}",
                  episode_ids=last_landed[-1:], **spend)
    return 1
