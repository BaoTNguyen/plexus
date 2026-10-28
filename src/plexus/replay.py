"""`plexus replay`: re-run landed features from their base commit (F28,
DIRECTION §5). One command, one number: pass rate, cost and lead time.

Each task in the set (evals/replay.json) names a registered repo, the commit
its feature landed on top of, and the feature as planned. A sample clones the
repo at that base, writes the one-feature plan, approves it and runs it with
the very same `plexus run` a real goal gets, then reads the clone's own
ledger. Nothing about the original repo changes. `--check` spends nothing: it
confirms each acceptance command fails at the base and passes where the
feature landed, so a task that can't discriminate never costs an episode.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from . import vascular_paths
from .registry import load_registry, workspace_roots

PLEXUS = [sys.executable, "-c", "import sys; from plexus.cli import main; sys.exit(main())"]
# Commits from before heart dropped its silent file fallback (B11) have tests
# that read a tmp journal and expect it to just work; they are run as they
# were written, with the journal files read on purpose.
OLD_TESTS_ENV = "HEART_PULSE=file"


def _scratch(prefix: str) -> Path:
    """Under cache/plexus/replay: docker can mount it, unlike /tmp, and a
    sandboxed verifier has to see the clone."""
    root = vascular_paths.path("cache", "plexus", "replay")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def load(path: str | Path, holdout: bool = False, only: list[str] | None = None) -> list[dict]:
    tasks = json.loads(Path(path).read_text())["tasks"]
    if only:
        return [t for t in tasks if t["id"] in only]
    return [t for t in tasks if bool(t.get("holdout")) == holdout]


def _repo(task: dict) -> Path:
    """The registered checkout, or one named so under a workspace root: the
    registry derives from pyproject.toml, and pulse is a Rust crate."""
    found = [load_registry().get(task["repo"])] + [str(r / task["repo"]) for r in workspace_roots()]
    for path in found:
        if path and (Path(path) / ".git").exists():
            return Path(path)
    raise SystemExit(f"{task['id']}: no checkout named {task['repo']!r}")


def _clone(task: dict, into: Path, at: str) -> Path:
    work = into / task["repo"]
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(_repo(task)), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-B", "replay", at], check=True)
    # no remote: a finished goal delivers by pushing to origin, and origin here
    # is the real checkout. Without one, plexus says "landed locally" and stops
    subprocess.run(["git", "-C", str(work), "remote", "remove", "origin"], check=True)
    return work


def _acceptance(task: dict, work: Path) -> tuple[int, str]:
    """Exit code and the output's last lines, run as a verifier runs it: in the
    sandbox image, no network. On the host, tests that probe the live docker
    daemon decide the result instead of the feature."""
    image = os.environ.get("HEART_SANDBOX_IMAGE", "heart-agent:vascular")
    got = subprocess.run(["docker", "run", "--rm", "--network", "none", "-e", OLD_TESTS_ENV,
                          "-v", f"{work}:/work", "-w", "/work", image,
                          "bash", "-c", task["feature"]["acceptance"]],
                         capture_output=True, text=True, timeout=1800)
    return got.returncode, "\n".join((got.stdout + got.stderr).strip().splitlines()[-6:])


def check(task: dict) -> str | None:
    """None when the task discriminates, else why not."""
    # a clean clone for each side: what the base run leaves behind must not
    # decide the landed one
    tmp = _scratch("check-")
    try:
        at_base, _ = _acceptance(task, _clone(task, tmp / "base", task["base"]))
        if at_base == 0:
            return "acceptance already passes at the base"
        if at_base == 127:
            return "acceptance names a tool that is not there (exit 127)"
        landed, tail = _acceptance(task, _clone(task, tmp / "landed", task["landed"]))
        if landed != 0:
            return f"acceptance fails where the feature landed (exit {landed}):\n{tail}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


def goal_toml(toml: str, task_id: str) -> str:
    """Today's goal file, fitted to a replay. Its own goal id, so replay events
    stay out of the real goal's feed; the old-tests env on the suite; and no
    dependents. Those are today's sibling checkouts, not the ones the feature
    landed beside: plexus's tests import heart.observe, so every heart task
    failed its regression on a heart from before it (2026-10-28). The repo's
    own suite still runs."""
    goal = "replay-" + re.sub(r"[^a-z0-9-]+", "-", task_id.lower())
    toml = re.sub(r'(?m)^id = ".*"', f'id = "{goal}"', toml, count=1)
    toml = re.sub(r'(?m)^suite = "', f'suite = "{OLD_TESTS_ENV} ', toml, count=1)
    return re.sub(r'(?m)^dependents = .*$', "dependents = []", toml)


def run_env(environ: dict) -> dict:
    """The env `plexus run` gets in a replay: the old-tests var set, and named
    for passthrough so the agent's sandbox shell has it too. Without it the
    agent sees the base's suite fail on untouched code and stops to ask
    (escalation-cost, 2026-10-28). It only picks how journals are read."""
    name, value = OLD_TESTS_ENV.split("=")
    named = [n for n in environ.get("HEART_SANDBOX_ENV", "").split(",") if n.strip()]
    return {**environ, name: value,
            "HEART_SANDBOX_ENV": ",".join(dict.fromkeys([*named, name]))}


def sample(task: dict, keep: bool = False) -> dict:
    """One replay of one task through plexus run; the clone's ledger says how it went."""
    tmp = _scratch("sample-")
    try:
        work = _clone(task, tmp, task["base"])
        (work / "plexus.toml").write_text(
            goal_toml((_repo(task) / "plexus.toml").read_text(), task["id"]))
        state = work / ".vascular" / "plexus"
        state.mkdir(parents=True, exist_ok=True)
        feature = {"plan_id": f"replay-{task['id'].replace(':', '-')}",
                   "id": task["id"].split(":")[-1], **task["feature"],
                   "acceptance": f"export {OLD_TESTS_ENV}; {task['feature']['acceptance']}"}
        (state / "plan.jsonl").write_text(json.dumps(feature) + "\n")
        out = []
        for argv in (["approve"], ["run"]):
            got = subprocess.run([*PLEXUS, *argv, "--root", str(work)], cwd=work,
                                 env=run_env(dict(os.environ)), capture_output=True, text=True)
            out.append(got.stdout[-2000:] + got.stderr[-2000:])
            if argv[0] == "approve" and got.returncode:
                return {"id": task["id"], "outcome": "not-approved", "detail": out[-1]}
        return {"id": task["id"], **outcome([json.loads(line) for line in
                                             (state / "ledger.jsonl").read_text().splitlines()])}
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def outcome(recs: list[dict]) -> dict:
    """landed / escalated / unfinished, attempts, dollars and lead time, from a ledger."""
    started = [r for r in recs if r["kind"] == "feature.started"]
    landed = next((r for r in recs if r["kind"] == "feature.landed"), None)
    raised = [r for r in recs if r["kind"] == "escalation.raised"]
    cost = sum(float(r.get("cost_usd") or 0) for r in recs
               if r["kind"] in ("feature.failed", "feature.landed", "escalation.raised"))
    lead = None
    if landed and started:
        lead = (datetime.datetime.fromisoformat(landed["ts"])
                - datetime.datetime.fromisoformat(started[0]["ts"])).total_seconds()
    return {"outcome": "landed" if landed else "escalated" if raised else "unfinished",
            "reason_class": raised[-1].get("reason_class") if raised and not landed else None,
            "attempts": len(started), "cost_usd": round(cost, 4), "lead_s": lead}


def summary(results: list[dict]) -> list[str]:
    n = len(results)
    landed = [r for r in results if r["outcome"] == "landed"]
    cost = [r.get("cost_usd") or 0 for r in results]
    lead = [r["lead_s"] for r in landed if r.get("lead_s") is not None]
    lines = [f"pass rate: {len(landed)}/{n}" + (f" ({len(landed) / n:.0%})" if n else ""),
             f"cost: ${sum(cost):.2f} total, ${statistics.median(cost):.2f} median per sample"
             if cost else "cost: none"]
    if lead:
        lines.append(f"lead time (landed): p50 {statistics.median(lead) / 60:.1f} min, "
                     f"max {max(lead) / 60:.1f} min")
    return lines


def main(set_path: str, samples: int = 1, holdout: bool = False, only: list[str] | None = None,
         check_only: bool = False, keep: bool = False) -> int:
    tasks = load(set_path, holdout, only)
    if check_only:
        bad = 0
        for t in tasks:
            why = check(t)
            bad += why is not None
            print(f"{'ok  ' if why is None else 'BAD '} {t['id']}" + (f": {why}" if why else ""))
        return int(bad > 0)
    results_dir = Path(set_path).parent / "results"
    results_dir.mkdir(exist_ok=True)
    log = results_dir / f"{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    results = []
    for t in tasks:
        for i in range(samples):
            r = {**sample(t, keep), "sample": i + 1,
                 "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            results.append(r)
            with open(log, "a") as f:
                f.write(json.dumps(r) + "\n")
            print(f"{r['outcome']:<12} {t['id']}  attempts={r.get('attempts')} "
                  f"${r.get('cost_usd') or 0:.2f}", flush=True)
    print("\n".join(summary(results)))
    print(f"results: {log}")
    return 0
