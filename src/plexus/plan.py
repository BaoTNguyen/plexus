"""Plan phase: one planner agent call turns goal + context + ground truth into
an ordered feature list; human sign-off (`plexus approve`) arms the loop —
warren's gate, kept because the plan is where scope errors are cheapest.
"""
from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

from heart.env import Workspace
from heart.runner import run_agent

from . import ledger

PLAN_PROMPT = """\
You are planning a software goal that will be built one feature at a time by a
coding agent, each feature verified before the next starts.

Goal: {text}

Context: {context}

Manual checks: {manual_checks}

Project overview — the standing description of this project, agreed with a
human. Plan within it; do not re-decide what it settles.
{overview}

Definition of done — this must pass when all features are built: {suite}

Reply with ONLY a JSON array of features, in build order. Each feature:
{{"id": "<short-slug>", "title": "<one line>",
  "spec": "<what to implement, self-contained>",
  "acceptance": "<shell command that exits 0 iff this feature works>",
  "touches": ["<path glob>", ...],
  "contract": ["<public symbol this feature adds or changes>", ...],
  "priority": 0,
  "depends_on": ["<feature id>", ...],
  "needs_upstream": ["<symbol from another project>", ...],
  "skills": ["<capability>", ...],
  "difficulty": "easy|medium|hard",
  "effort": "low|medium|high",
  "manual_checks": ["<human validation step>", ...]}}

`touches` is a closed allowlist of every path the feature may create or modify.
A diff outside it is refused and the run stops, so keep it tight: a feature that
needs half the tree is two features.
`contract` names each public symbol added or changed, as "module.func(sig)",
"class Name", "<cli> <subcommand>", "<config> key: <k>" or "ledger kind: <kind>".
Use [] when the feature adds no public surface.
If the feature changes what a command prints, end `spec` with an `expect:` block:
a line `expect:`, then a line `$ <command>`, then the literal lines that command
must print. The run executes it and refuses the feature if any of those lines is
missing from the output, so write the mockup you actually want to see.
`priority` is an integer where 0 is highest. `depends_on` names features in this
plan; dependencies always run first, with priority breaking ties between ready
features. Use `needs_upstream` only for public symbols from another project.
Keep each feature small enough to land in one agent session."""


def matches(path: str, glob: str) -> bool:
    """Does `path` fall inside one `touches` glob?

    Lives here because `touches` is a plan field: run.py enforces it and
    review.py classifies from it, and neither should own the semantics.

    Plain fnmatch is wrong in both directions — its `*` spans `/`, so `src/*`
    would authorise a file three levels down, while `tests/**` would not match
    `tests/a/b.py` at all. Compare segment by segment instead, with `**` as the
    only wildcard allowed to cross a separator."""
    if glob == "**":
        return True
    pparts, gparts = path.split("/"), glob.split("/")
    if gparts[-1] == "**":
        return len(pparts) >= len(gparts) and all(
            fnmatch.fnmatch(p, g) for p, g in zip(pparts, gparts[:-1]))
    return len(pparts) == len(gparts) and all(
        fnmatch.fnmatch(p, g) for p, g in zip(pparts, gparts))


def parse_expect(spec_text: str) -> tuple[str, list[str]] | None:
    """The `expect:` mockup at the end of a feature spec, as (command, lines).

    Homed here for the same reason as `matches`: `spec` is a plan field, and
    run.py executing the block should not make run.py the owner of its grammar.

    Returns None when there is no usable block. Most features have none, and a
    spec that merely says the word "expect:" in prose is not a mockup — silently
    ignoring a malformed block beats failing a feature over its own docstring.
    The last block wins, since the format asks for it at the end."""
    lines = spec_text.splitlines()
    idx = [i for i, l in enumerate(lines) if l.strip().lower() == "expect:"]
    if not idx:
        return None
    body = lines[idx[-1] + 1:]
    if not body or not body[0].strip().startswith("$ "):
        return None
    cmd = body[0].strip()[2:].strip()
    # blank lines carry no signal in a terminal mockup and are the first thing a
    # model gets wrong, so match on content lines only
    want = [l.strip() for l in body[1:] if l.strip()]
    return (cmd, want) if cmd and want else None


def plan_path(root: str | Path = ".", task_id: str = "") -> Path:
    """Where a plan lives. One file per task, because a plan belongs to a piece
    of work rather than to the project — the project has an overview, not a
    feature list. The unsuffixed path is what repos written before tasks used,
    and is still what `plexus plan` with no task writes."""
    base = Path(root) / ".plexus"
    return base / "plans" / f"{task_id}.jsonl" if task_id else base / "plan.jsonl"


def _parse_features(raw: str) -> list[dict]:
    # Prefer a fenced block: it is an explicit delimiter, where the outermost
    # [..] span is a guess that any stray bracket in surrounding prose defeats.
    # Planners routinely add a sentence after the JSON.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fenced:
        return _validated(json.loads(fenced.group(1)))
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no JSON array in planner output")
    return _validated(json.loads(raw[start:end + 1]))


def _validated(feats: list[dict]) -> list[dict]:
    ids = [f.get("id") for f in feats]
    if len(ids) != len(set(ids)):
        raise ValueError("feature ids must be unique")
    for f in feats:
        missing = [k for k in ("id", "title", "spec", "acceptance", "touches")
                   if not f.get(k)]
        if missing:
            raise ValueError(f"feature missing {missing}: {f}")
        if not isinstance(f["touches"], list):
            raise ValueError(f"touches must be a list of globs: {f['touches']!r}")
        # `contract` may legitimately be empty, so it cannot be required the way
        # `touches` is — absent and "adds no public surface" look identical.
        f.setdefault("contract", [])
        if not isinstance(f["contract"], list):
            raise ValueError(f"contract must be a list: {f['contract']!r}")
        f.setdefault("priority", 0)
        if not isinstance(f["priority"], int) or isinstance(f["priority"], bool):
            raise ValueError(f"priority must be an integer: {f['priority']!r}")
        for key in ("depends_on", "needs_upstream", "skills", "manual_checks"):
            f.setdefault(key, [])
            if not isinstance(f[key], list) or any(not isinstance(v, str) for v in f[key]):
                raise ValueError(f"{key} must be a list of strings: {f[key]!r}")
        unknown = set(f["depends_on"]) - set(ids)
        if unknown:
            raise ValueError(f"{f['id']} depends on unknown feature(s): {sorted(unknown)}")
        if f["id"] in f["depends_on"]:
            raise ValueError(f"{f['id']} cannot depend on itself")
        f.setdefault("difficulty", "unknown")
        f.setdefault("effort", "")
    execution_order(feats)  # reject cycles before the plan can be approved
    return feats


def execution_order(feats: list[dict]) -> list[dict]:
    """Stable dependency order; priority only chooses among currently ready work."""
    remaining = list(enumerate(feats))
    complete: set[str] = set()
    ordered: list[dict] = []
    while remaining:
        ready = [(i, f) for i, f in remaining
                 if set(f.get("depends_on", ())) <= complete]
        if not ready:
            blocked = {f["id"]: f.get("depends_on", []) for _, f in remaining}
            raise ValueError(f"feature dependency cycle: {blocked}")
        chosen = min(ready, key=lambda item: (item[1].get("priority", 0), item[0]))
        remaining.remove(chosen)
        ordered.append(chosen[1])
        complete.add(chosen[1]["id"])
    return ordered


def make_plan(spec, root: str | Path = ".", task_id: str = "") -> list[dict]:
    out = Path(root) / ".plexus"
    out.mkdir(parents=True, exist_ok=True)
    log = out / "plan.log"
    task = None
    if task_id:
        from . import tasks as _tasks
        task = next((t for t in _tasks.read(root) if t["id"] == task_id), None)
        if task is None:
            raise SystemExit(f"no task {task_id!r}")
    shown = lambda values: "; ".join(values) if values else "(none)"
    from . import overview as _overview
    # A task's plan is planned against the task, inside the project's overview.
    # Without this every task would be planned against the whole project and
    # each one would propose rebuilding it.
    goal_text = spec.text if task is None else (
        f"{task['title']}\n\n{task.get('body') or ''}".strip()
        + f"\n\n(This is one task in the project: {spec.text})")
    prompt = PLAN_PROMPT.format(
        text=goal_text, context=spec.context or "(none)", suite=spec.suite,
        overview=_overview.as_context(root) or "(not written yet)",
        manual_checks=shown(spec.manual_checks))
    # The planner is the one turn in the loop that is genuinely open: how to
    # decompose, how big a feature should be, what makes a criterion executable —
    # and the only turn where retrieval scores well. No override needed: the
    # prompt is short and low-density, so it lands inside capillaries' complexity
    # band and retrieves naturally, while the long feature prompts fall outside it.
    #
    # Retry: this is the highest-leverage turn in the whole loop, so one flaky
    # call or one unparseable reply must not abort planning. A malformed reply is
    # re-asked (the log is overwritten each try), and only an exhausted budget
    # raises. Attempts are cheap relative to a wrong or missing plan.
    attempts = max(1, int(os.getenv("PLEXUS_PLAN_ATTEMPTS", "3")))
    feats = None
    last_err = ""
    for i in range(attempts):
        res = run_agent(spec.agent, prompt, cwd=str(root), extra_env={},
                        timeout=spec.timeout, log_path=log, agent_cmd=spec.agent_cmd)
        if res["exit_code"] != 0:
            last_err = f"planner agent failed (exit {res['exit_code']})"
            continue
        try:
            feats = _parse_features(log.read_text(encoding="utf-8", errors="replace"))
            break
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = f"planner output unparseable: {exc}"
    if feats is None:
        raise SystemExit(f"{last_err} after {attempts} attempt(s); see {log}")
    plan_id = "plan-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if task_id:
        # Feature ids are namespaced by task so two tasks can both plan a
        # feature called "api" without their ledger histories merging — every
        # per-feature query in run.py is keyed on (goal_id, feature_id).
        for feat in feats:
            feat["id"] = f"{task_id}:{feat['id']}"
            feat["depends_on"] = [f"{task_id}:{d}" for d in feat.get("depends_on", [])]
    target = plan_path(root, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        for feat in feats:
            f.write(json.dumps({"plan_id": plan_id, "task_id": task_id, **feat}) + "\n")
    if task_id:
        from . import tasks as _tasks
        _tasks.update(root, task_id, plan_id=plan_id, state="ready", error="")
    ledger.record(
        "plan.created", goal_id=spec.goal_id, root=root, plan_id=plan_id,
        task=task_id, spec_hash=spec.spec_hash,
        features=[{"feature_id": f["id"], "title": f["title"],
                   "acceptance": f["acceptance"]} for f in feats],
        rejected=[],  # populated once the planner runs best-of-N
    )
    return feats


def load_plan(root: str | Path = ".", task_id: str = "") -> list[dict]:
    p = plan_path(root, task_id)
    if not p.exists() and task_id:
        raise SystemExit(f"no plan for task {task_id}; run `plexus plan --task {task_id}`")
    if not p.exists():
        raise SystemExit("no plan; run `plexus plan` first")
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def check_criteria(spec, root: str | Path = ".", task_id: str = "") -> list[tuple[str, str]]:
    """Every acceptance command must FAIL on the base commit. A criterion that
    already passes is vacuous (`true`, `echo ok`) or describes work already done,
    and would land a feature for an empty diff; one that exits 127 names a tool
    that isn't there, so it can never be executable ground truth.

    This is the cheapest possible planning gate: three wasted episodes and a
    misfiled `intent` verdict, traded for one question asked before the run."""
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    bad: list[tuple[str, str]] = []
    # A fresh worktree per criterion: run in a shared tree and one criterion's
    # side effects (files it writes, a port it binds, a DB row it inserts) leak
    # into the next, which can mask or trip a later check. Each criterion is
    # judged against a clean base and nothing else.
    for feat in load_plan(root, task_id):
        ws = Workspace(str(root), base)
        try:
            r = subprocess.run(feat["acceptance"], shell=True, cwd=str(ws.path),
                               capture_output=True, text=True, timeout=spec.timeout)
        except subprocess.TimeoutExpired:
            bad.append((feat["id"], "acceptance command hung on the base commit"))
            continue
        finally:
            ws.destroy()
        if r.returncode == 0:
            bad.append((feat["id"], "already passes on the base commit — vacuous "
                                    "criterion, or the feature is already built"))
        elif r.returncode == 127:
            bad.append((feat["id"], "command not found — not executable ground truth"))
    return bad


def amend(spec, feature_id: str, root: str | Path = ".",
          acceptance: str | None = None, spec_text: str | None = None,
          title: str | None = None, touches: list[str] | None = None) -> str:
    """Fix one not-yet-landed feature's plan in place.

    The plan is otherwise immutable once armed, so a criterion discovered wrong
    mid-run used to mean hand-editing .plexus/plan.jsonl. This rewrites the one
    feature's fields and records plan.amended. A landed feature is refused — its
    commit already shipped, so amending it would be a lie. Re-run `plexus run`
    after amending; the feature reopens against the new criterion."""
    from . import ledger
    plan = load_plan(root)
    feat = next((f for f in plan if f["id"] == feature_id), None)
    if feat is None:
        raise SystemExit(f"no feature {feature_id!r} in the plan")
    recs = ledger.read(root)
    if any(r["kind"] == "feature.landed" and r.get("feature_id") == feature_id
           and r.get("goal_id") == spec.goal_id for r in recs):
        raise SystemExit(f"{feature_id} already landed — its commit shipped; amend refused")

    changes = {}
    if acceptance is not None:
        changes["acceptance"] = acceptance
    if spec_text is not None:
        changes["spec"] = spec_text
    if title is not None:
        changes["title"] = title
    if touches is not None:
        # the resolution path for a scope_violation escalation where the plan,
        # not the agent, was wrong about how wide the feature really is
        changes["touches"] = touches
    if not changes:
        raise SystemExit(
            "nothing to amend — pass --acceptance / --spec / --title / --touches")
    feat.update(changes)

    with open(plan_path(root), "w", encoding="utf-8") as f:
        for p in plan:
            f.write(json.dumps(p) + "\n")
    ledger.record("plan.amended", goal_id=spec.goal_id, feature_id=feature_id,
                  root=root, changed=sorted(changes))
    return f"amended {feature_id}: {', '.join(sorted(changes))} — re-run `plexus run`"


def approve(spec, root: str | Path = ".", approver: str = "human",
            waive: bool = False, task_id: str = "") -> str:
    plan_id = load_plan(root, task_id)[0]["plan_id"]
    bad = check_criteria(spec, root, task_id)
    if bad and not waive:
        raise SystemExit(
            "plan not approved — these acceptance criteria are not usable ground truth:\n"
            + "\n".join(f"  {fid}: {why}" for fid, why in bad)
            + "\nFix them in .plexus/plan.jsonl, or `plexus approve --waive` to accept.")
    ledger.record("plan.approved", goal_id=spec.goal_id, root=root,
                  plan_id=plan_id, task=task_id, approver=approver,
                  waived=[fid for fid, _ in bad])
    return plan_id
