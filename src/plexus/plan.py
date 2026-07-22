"""Plan phase: one planner agent call turns goal + context + ground truth into
an ordered feature list; human sign-off (`plexus approve`) arms the loop —
warren's gate, kept because the plan is where scope errors are cheapest.
"""
from __future__ import annotations

import datetime
import json
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

Definition of done — this must pass when all features are built: {suite}

Reply with ONLY a JSON array of features, in build order. Each feature:
{{"id": "<short-slug>", "title": "<one line>",
  "spec": "<what to implement, self-contained>",
  "acceptance": "<shell command that exits 0 iff this feature works>"}}
Keep each feature small enough to land in one agent session."""


def plan_path(root: str | Path = ".") -> Path:
    return Path(root) / ".plexus" / "plan.jsonl"


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
    for f in feats:
        missing = [k for k in ("id", "title", "spec", "acceptance") if not f.get(k)]
        if missing:
            raise ValueError(f"feature missing {missing}: {f}")
    return feats


def make_plan(spec, root: str | Path = ".") -> list[dict]:
    out = Path(root) / ".plexus"
    out.mkdir(parents=True, exist_ok=True)
    log = out / "plan.log"
    prompt = PLAN_PROMPT.format(text=spec.text, context=spec.context or "(none)",
                                suite=spec.suite)
    # The planner is the one turn in the loop that is genuinely open: how to
    # decompose, how big a feature should be, what makes a criterion executable —
    # and the only turn where retrieval scores well. No override needed: the
    # prompt is short and low-density, so it lands inside capillaries' complexity
    # band and retrieves naturally, while the long feature prompts fall outside it.
    res = run_agent(spec.agent, prompt, cwd=str(root), extra_env={},
                    timeout=spec.timeout, log_path=log, agent_cmd=spec.agent_cmd)
    if res["exit_code"] != 0:
        raise SystemExit(f"planner agent failed (exit {res['exit_code']}); see {log}")
    feats = _parse_features(log.read_text(encoding="utf-8", errors="replace"))
    plan_id = "plan-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(plan_path(root), "w", encoding="utf-8") as f:
        for feat in feats:
            f.write(json.dumps({"plan_id": plan_id, **feat}) + "\n")
    ledger.record(
        "plan.created", goal_id=spec.goal_id, root=root, plan_id=plan_id,
        features=[{"feature_id": f["id"], "title": f["title"],
                   "acceptance": f["acceptance"]} for f in feats],
        rejected=[],  # populated once the planner runs best-of-N
    )
    return feats


def load_plan(root: str | Path = ".") -> list[dict]:
    p = plan_path(root)
    if not p.exists():
        raise SystemExit("no plan; run `plexus plan` first")
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def check_criteria(spec, root: str | Path = ".") -> list[tuple[str, str]]:
    """Every acceptance command must FAIL on the base commit. A criterion that
    already passes is vacuous (`true`, `echo ok`) or describes work already done,
    and would land a feature for an empty diff; one that exits 127 names a tool
    that isn't there, so it can never be executable ground truth.

    This is the cheapest possible planning gate: three wasted episodes and a
    misfiled `intent` verdict, traded for one question asked before the run."""
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    bad: list[tuple[str, str]] = []
    ws = Workspace(str(root), base)
    try:
        for feat in load_plan(root):
            try:
                r = subprocess.run(feat["acceptance"], shell=True, cwd=str(ws.path),
                                   capture_output=True, text=True, timeout=spec.timeout)
            except subprocess.TimeoutExpired:
                bad.append((feat["id"], "acceptance command hung on the base commit"))
                continue
            if r.returncode == 0:
                bad.append((feat["id"], "already passes on the base commit — vacuous "
                                        "criterion, or the feature is already built"))
            elif r.returncode == 127:
                bad.append((feat["id"], "command not found — not executable ground truth"))
    finally:
        ws.destroy()
    return bad


def approve(spec, root: str | Path = ".", approver: str = "human",
            waive: bool = False) -> str:
    plan_id = load_plan(root)[0]["plan_id"]
    bad = check_criteria(spec, root)
    if bad and not waive:
        raise SystemExit(
            "plan not approved — these acceptance criteria are not usable ground truth:\n"
            + "\n".join(f"  {fid}: {why}" for fid, why in bad)
            + "\nFix them in .plexus/plan.jsonl, or `plexus approve --waive` to accept.")
    ledger.record("plan.approved", goal_id=spec.goal_id, root=root,
                  plan_id=plan_id, approver=approver,
                  waived=[fid for fid, _ in bad])
    return plan_id
