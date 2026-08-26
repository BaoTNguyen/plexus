"""Control plane: a local, single-user dashboard over the ledger.

Warren shape (plan / run / activity / answer cards) mapped onto plexus's real
surface, but built on plexus's stack, not warren's: stdlib http.server + one
HTML file, no bearer auth, no SQLite, no build step. State still lives in
`.plexus/*.jsonl` — this is a lens over the system of record plus three write
paths (approve, resolve, run/stop) that call the same code the CLI does.

The README rejected a web UI for the autonomous loop; this is the deliberate
reversal for the *supervision* surface — deciding which goal to advance,
answering blocks, and stopping a run — which a CLI genuinely does not cover.

Read side reuses observe/diagnose/plan verbatim. Write side:
  approve, resolve  -> synchronous (fast; approve spins a worktree, seconds)
  plan, run         -> spawned subprocess (minutes); tracked so run can be stopped
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import tomllib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from urllib.parse import parse_qs, urlparse

from heart.runner import CACHE_MULTIPLIERS, speed_multiplier

from . import diagnose, ledger, observe, overview, review
from .plan import load_plan
from .run import _feature_state
from .spec import load_spec

# root(resolved str) -> Popen we spawned, kept only so we can reap it (avoid
# zombies). Liveness and stop are decided from the flock, not this table, so a
# run started from a terminal is detected and stopped just the same.
# Popen, or term.TmuxJob when a run went into a tmux window. Both answer
# poll(), which is all the reaper below ever asks of them.
_PROCS: dict = {}
_PROC_KINDS: dict[str, str] = {}
_PROC_RESULTS: dict[str, dict] = {}
_STATIC = Path(__file__).with_name("static")


def _reap() -> None:
    for k, p in list(_PROCS.items()):
        code = p.poll()
        if code is not None:  # poll() reaps a finished child
            _PROC_RESULTS[k] = {
                "kind": _PROC_KINDS.get(k, "unknown"),
                "exit_code": code,
                "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            _PROCS.pop(k, None)
            _PROC_KINDS.pop(k, None)


def _scan_roots(base: Path) -> list[Path]:
    """A goal repo is a dir with a plexus.toml. `base` may be one goal, or a
    parent holding several (one level deep) — the factory view `plexus stack`
    implies but never had a UI for."""
    base = base.resolve()
    roots = []
    if (base / "plexus.toml").exists():
        roots.append(base)
    roots += [p.parent for p in base.glob("*/plexus.toml")]
    return sorted(set(roots))


def menu_roots(base: Path) -> list[Path]:
    """The full project menu: the CLI `--root` plus every workspace root the
    user has added (`plexus add`), each expanded to its goal repos. This is how
    a project living outside the `--root` parent still shows up — the multi-root
    workspace, versus `_scan_roots`' single base."""
    from . import registry
    roots = set(_scan_roots(base))
    for w in registry.workspace_roots():
        roots.update(_scan_roots(w))
    return sorted(roots)


def _goal_id(root: Path) -> str:
    try:
        return load_spec(root).goal_id
    except Exception:
        recs = ledger.read(root)
        return recs[0]["goal_id"] if recs else root.name


def _project_id(root: Path) -> str:
    """Stable-enough opaque URL id without exposing a filesystem path."""
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def _current_plan(root: Path) -> tuple[str, list[dict]]:
    try:
        plan = load_plan(root)
    except SystemExit:
        return "", []
    return (str(plan[0].get("plan_id", "")), plan) if plan else ("", [])


def _plan_approved(recs: list[dict], goal_id: str, plan_id: str) -> bool:
    """Only an approval for the current goal and plan arms execution."""
    if not plan_id:
        return False
    return any(r.get("kind") == "plan.approved"
               and r.get("goal_id") == goal_id
               and r.get("plan_id") == plan_id for r in recs)


def _job_status(root: Path) -> dict:
    _reap()
    key = str(root.resolve())
    proc = _PROCS.get(key)
    if proc is not None:
        return {"kind": _PROC_KINDS.get(key, "unknown"), "running": True,
                "exit_code": None, "finished": ""}
    result = _PROC_RESULTS.get(key) or {}
    return {"kind": result.get("kind", ""), "running": False,
            "exit_code": result.get("exit_code"), "finished": result.get("finished", "")}


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _goal_lifecycle(root: Path) -> dict:
    """Derive the singular current goal's UI state from existing artifacts."""
    spec = load_spec(root)
    recs = ledger.read(root)
    plan_id, plan = _current_plan(root)
    approved = _plan_approved(recs, spec.goal_id, plan_id)
    job = _job_status(root)
    goal_recs = [r for r in recs if r.get("goal_id") == spec.goal_id]
    automated_passed = any(r.get("kind") == "validation.automated_passed"
                           for r in goal_recs)
    manual_passed = any(r.get("kind") == "validation.manual_passed"
                        for r in goal_recs)
    delivery = next((str(r.get("result", "")) for r in reversed(goal_recs)
                     if r.get("kind") == "delivery.requested"), "")
    open_escalations = _open_escalations(recs, spec.goal_id)
    if _running(root):
        state = "running"
    elif job["running"] and job["kind"] == "plan":
        state = "planning"
    elif open_escalations:
        state = "blocked"
    elif any(r.get("kind") == "goal.finished" for r in goal_recs):
        state = "done"
    elif automated_passed and spec.manual_checks and not manual_passed:
        state = "validating"
    elif plan and not approved:
        state = "awaiting_approval"
    elif approved:
        state = "ready"
    else:
        spec_mtime = (root / "plexus.toml").stat().st_mtime
        log = root / ".plexus" / "plan.log"
        failed_job = (job["kind"] == "plan" and job["exit_code"] not in (None, 0))
        recent_error_log = log.exists() and log.stat().st_mtime >= spec_mtime
        placeholder = (spec.goal_id == "my-goal"
                       or spec.text.startswith("What the product should do"))
        if placeholder:
            state = "intake"
        elif failed_job or recent_error_log:
            state = "plan_failed"
        else:
            state = "draft"
    return {
        "state": state,
        "approved": approved,
        "plan_id": plan_id,
        "plan_exists": bool(plan),
        "features": len(plan),
        "job": job,
        "editable": state in ("intake", "clarify", "draft", "plan_failed"),
        "validation": {
            "automated_passed": automated_passed,
            "manual_passed": manual_passed,
            "checks": list(spec.manual_checks),
        },
        "delivery": delivery,
    }


def _import_github_issue(url: str) -> dict:
    """Read one GitHub issue through the user's existing gh authentication."""
    if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/issues/\d+/?", url):
        raise ValueError("use a full GitHub issue URL")
    try:
        result = subprocess.run(
            ["gh", "issue", "view", url, "--json", "number,title,body,url"],
            capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ValueError("GitHub CLI (gh) is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("GitHub issue import timed out") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(detail or "GitHub issue import failed")
    issue = json.loads(result.stdout)
    title, body = issue["title"].strip(), (issue.get("body") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or f"issue-{issue['number']}"
    return {
        "goal_id": slug,
        "text": title + (f"\n\n{body}" if body else ""),
        "source_kind": "github",
        "source_url": issue["url"],
        "source_title": title,
        "source_body": body,
    }


def _running(root: Path) -> bool:
    """Authoritative: is a `plexus run` holding this goal's flock right now?
    Probe by trying the same non-blocking lock the run holds — if we get it,
    nobody's running (release immediately); if we're blocked, one is. Works for
    terminal-started runs too, and is immune to PID reuse (unlike reading the
    stamped pid), because the kernel drops the flock the instant the holder dies."""
    lock = Path(root) / ".plexus" / "lock"
    if not lock.exists():
        return False
    try:
        f = open(lock, "r")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()


def _open_escalations(recs: list[dict], goal_id: str) -> list[dict]:
    """Per feature, raised minus resolved > 0 -> open, carrying the latest
    reason. Mirrors observe.status's counting so the two never disagree."""
    by_feat: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("goal_id") == goal_id and r.get("feature_id"):
            by_feat.setdefault(r["feature_id"], []).append(r)
    out = []
    for fid, rs in by_feat.items():
        raised = [r for r in rs if r["kind"] == "escalation.raised"]
        if len(raised) - sum(r["kind"] == "escalation.resolved" for r in rs) > 0:
            last = raised[-1]
            out.append({"feature_id": fid,
                        "reason_class": last.get("reason_class", "?"),
                        "reason": last.get("reason", "")})
    return out


def _fleet_state(server) -> dict:
    from . import term
    return {"local_slots": server.local_slots,
            "global_agents": server.global_agents,
            "max_goals": server.max_goals,
            # no tmux, no persistent shell — the UI drops the terminal tab
            # rather than offering one that loses everything on reload
            "terminal": term.available()}


def _pull_requests(root: Path, base: str) -> list[dict]:
    """Open PRs for this repo, through the user's existing gh auth.

    Best effort by design: no gh, no network, or not a GitHub remote all mean
    the rest of the validation tab still renders. A missing PR list is a gap in
    one panel, not a broken page.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--json",
             "number,title,headRefName,baseRefName,url,isDraft,mergeable,statusCheckRollup",
             "--limit", "20"],
            cwd=str(root), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    try:
        listed = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for pr in listed:
        checks = pr.get("statusCheckRollup") or []
        states = [str(c.get("conclusion") or c.get("state") or "").upper()
                  for c in checks]
        rollup = ("failing" if any(s in ("FAILURE", "ERROR", "TIMED_OUT") for s in states)
                  else "pending" if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "") for s in states)
                  else "passing" if states else "none")
        out.append({
            "number": pr.get("number", 0), "title": pr.get("title", ""),
            "head": pr.get("headRefName", ""), "base": pr.get("baseRefName", base),
            "url": pr.get("url", ""), "draft": bool(pr.get("isDraft")),
            "mergeable": str(pr.get("mergeable") or ""), "checks": rollup,
        })
    return out


def _validation(root: Path) -> dict:
    """The validation tab: what the machines say, what still needs your eyes,
    and which PRs are waiting on you.

    Automated and manual are kept apart on purpose. A green suite is evidence
    that nothing regressed; it is not evidence that the thing was built right,
    and collapsing the two is how a factory ships something that passes every
    test and does the wrong job.
    """
    from . import tasks as tasklib
    spec = load_spec(root)
    recs = ledger.read(root)
    goal_recs = [r for r in recs if r.get("goal_id") == spec.goal_id]
    automated = next((r for r in reversed(goal_recs)
                      if r.get("kind") in ("validation.automated_passed",
                                           "validation.automated_failed")), {})
    manual = next((r for r in reversed(goal_recs)
                   if r.get("kind") == "validation.manual_passed"), {})
    landed = [r for r in recs if r.get("kind") == "feature.landed"]
    board = tasklib.group(root)
    # tasks that shipped code but nobody has signed off on yet — the queue this
    # tab exists to drain
    awaiting = [t for t in board["done"] if not t.get("reason")]
    try:
        rows = review.rows(spec, root)
    except (Exception, SystemExit):
        # load_plan exits rather than raising when there is no plan yet, and an
        # unplanned project must still be able to open this tab
        rows = []
    return {
        "suite": spec.suite,
        "automated": {
            "state": ("passed" if automated.get("kind", "").endswith("passed")
                      else "failed" if automated else "unknown"),
            "ts": automated.get("ts", ""),
        },
        "manual": {"checks": list(spec.manual_checks),
                   "done": bool(manual), "ts": manual.get("ts", "")},
        "landed": [{"feature_id": r.get("feature_id", ""), "ts": r.get("ts", ""),
                    "commit": str(r.get("commit", ""))[:12]} for r in landed[-25:]],
        "awaiting_signoff": awaiting,
        "review_rows": rows,
        "pull_requests": _pull_requests(root, spec.pr_base),
        "pr_base": spec.pr_base,
    }


def _tasks_discuss_prompt(root: Path) -> str:
    """Opening turn for breaking the overview down into tasks.

    Seeded with the board as it stands and the overview it has to satisfy, so
    the model proposes the gap rather than re-proposing what is already queued.
    """
    from . import tasks as tasklib
    board = tasklib.group(root)
    lines = ["Help me break this project's work into tasks.", ""]
    for bucket in ("active", "blocked", "planned", "done"):
        rows = board.get(bucket) or []
        lines.append(f"{bucket.title()} ({len(rows)}):")
        lines += [f"  - {t['id']}: {t['title']}"
                  + (f"  [blocked by {', '.join(t['blocked_by'])}]" if t.get("blocked_by") else "")
                  for t in rows] or ["  (none)"]
        lines.append("")
    lines += [
        "The project overview these have to satisfy:",
        "",
        _overview_or_note(root),
        "",
        f"Tasks live in {tasks_file(root)}, one JSON object per line. Fields: "
        "id, title, body, blocked_by (list of task ids), requires_plan (bool), "
        "state (open|planning|ready|running|blocked|landed|closed), order.",
        "",
        "Read the repo and the overview, then tell me what is missing from this "
        "board and in what order it should be built. Ask before writing. When we "
        "agree, append the new tasks to that file — keep each one a single piece "
        "of work with a clear finish, and use blocked_by to express the order "
        "rather than relying on the list position.",
    ]
    return "\n".join(lines)


def _overview_or_note(root: Path) -> str:
    text = overview.as_context(root).strip()
    return text or "(the overview is empty — say so rather than inventing one)"


def tasks_file(root: Path) -> Path:
    from . import tasks as tasklib
    return tasklib.tasks_path(root)


def _term_windows(root: Path, roots: list[Path]) -> dict:
    """What the build tab lists: live tmux windows plus the transcripts of runs
    whose windows have already closed. One switcher over both, because 'the run
    that just finished' is the thing you most want to read and it stops being a
    window the moment it ends."""
    from . import term
    session = _term_name(root, roots)
    live = term.windows(session)
    live_names = {w["name"] for w in live}
    folder = Path(root) / ".plexus" / "transcripts"
    past = []
    for path in sorted(folder.glob("*.log"), reverse=True)[:40]:
        if path.stem not in live_names:
            past.append({"name": path.stem, "file": path.name,
                         "bytes": path.stat().st_size,
                         "finished": datetime.datetime.fromtimestamp(
                             path.stat().st_mtime,
                             datetime.timezone.utc).isoformat()})
    return {"session": session, "windows": live, "transcripts": past}


def _term_name(root: Path, roots: list[Path]) -> str:
    """tmux session name for a project. Named for the directory, because the
    whole point is that you can type `tmux attach -t plexus-plexus` in a real
    shell — a hash would be correct and useless. Two roots sharing a basename
    get the short project id appended so they can't collide onto one shell."""
    base = re.sub(r"[^A-Za-z0-9_-]", "-", root.name)
    if sum(r.name == root.name for r in roots) > 1:
        base += "-" + _project_id(root)[:6]
    return f"plexus-{base}"


def _list_goals(roots: list[Path]) -> list[dict]:
    from . import registry
    meta = registry.project_meta()
    out = []
    for root in roots:
        lines, code = observe.status(str(root))
        lifecycle = _goal_lifecycle(root)
        m = meta.get(str(root)) or meta.get(str(Path(root).resolve())) or {}
        goal_id = _goal_id(root)
        status = lifecycle["state"].replace("_", " ").upper()
        if goal_id != "my-goal":
            status += f"  {goal_id}"
        if lifecycle["state"] not in ("intake", "clarify", "draft", "planning", "plan_failed",
                                      "awaiting_approval", "ready"):
            status = lines[0] if lines else status
        out.append({"root": str(root), "name": root.name, "goal_id": goal_id,
                    "project_id": _project_id(root),
                    "goal_state": lifecycle["state"], "status": status,
                    "code": code, "running": _running(root),
                    "label": m.get("label", ""), "pinned": bool(m.get("pinned")),
                    "term_session": _term_name(root, roots)})
    return out


def _goal_detail(root: Path) -> dict:
    """Everything the tabs render, from the ledger alone. Pure enough to test
    without HTTP."""
    root = Path(root)
    recs = ledger.read(root)
    goal_id = _goal_id(root)
    try:
        plan = load_plan(root)
    except SystemExit:
        plan = []
    plan_id = str(plan[0].get("plan_id", "")) if plan else ""
    approved = _plan_approved(recs, goal_id, plan_id)

    features = []
    for feat in plan:
        state, next_attempt, budget_used = _feature_state(recs, goal_id, feat["id"])
        features.append({"id": feat["id"], "title": feat["title"],
                         "acceptance": feat["acceptance"], "state": state,
                         "attempt": next_attempt - 1, "budget_used": budget_used,
                         "priority": feat.get("priority", 0),
                         "depends_on": feat.get("depends_on", []),
                         "manual_checks": feat.get("manual_checks", [])})

    activity = [{"ts": r.get("ts", ""), "kind": r.get("kind", ""),
                 "feature_id": r.get("feature_id", ""),
                 "reason": r.get("reason", "") or r.get("outcome", "")}
                for r in recs[-40:]][::-1]

    return {"goal_id": goal_id, "root": str(root), "approved": approved,
            "lifecycle": _goal_lifecycle(root),
            "running": _running(root),
            "status": " · ".join(observe.status(str(root))[0]),
            "insights": observe.insights(str(root)),
            "features": features,
            "escalations": _open_escalations(recs, goal_id),
            "activity": activity,
            "why": diagnose.why(str(root))}


def _short(v, n: int = 70) -> str:
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def _event_detail(kind: str, payload: dict) -> str:
    """The salient bit of a spine event for the live log — the fields you'd want
    to see scroll by while an agent works, else a compact head of the payload."""
    salient = [k for k in ("role", "tool", "command", "outcome", "reward", "passed",
                           "verifier", "reason_class", "reason", "message", "status",
                           "outcome", "cost_usd", "attempt", "chosen", "mode")
               if k in payload]
    items = [(k, payload[k]) for k in dict.fromkeys(salient)] or list(payload.items())[:4]
    return "  ".join(f"{k}={_short(v)}" for k, v in items)


def _live(root: Path, limit: int = 60) -> list[dict]:
    """The goal's live activity: spine events across the whole stack, filtered to
    this goal's lineage (plexus stamps task_id `<goal>-<feature>-a<n>`, and heart
    stamps payload.goal_id), newest last so it reads like a tail. This is the
    'watch it work' surface — role turns, verifier rounds, rewards, retrieval —
    that the ledger (coarse, durable) deliberately doesn't carry.
    ponytail: full-journal scan per poll; prune by day-file if it ever drags."""
    goal_id = _goal_id(root)
    try:
        from heart.pulse import load_events
    except Exception:
        return []
    pref = goal_id + "-"
    out = []
    for e in load_events():
        task = str(e.get("task_id") or "")
        if task.startswith(pref) or (e.get("payload") or {}).get("goal_id") == goal_id:
            # role/duration live top-level on the event, not in payload; fold them
            # in so the detail line shows which role is working
            detail = dict(e.get("payload") or {})
            for k in ("role", "duration_ms"):
                if e.get(k) is not None:
                    detail.setdefault(k, e[k])
            out.append({"ts": e.get("ts", ""), "source": e.get("source", "?"),
                        "kind": e.get("kind", "?"),
                        "detail": _event_detail(e.get("kind", ""), detail)})
    return out[-limit:]


def _spine_events(root: Path) -> list[dict]:
    """Raw spine events belonging to one goal, oldest first."""
    goal_id = _goal_id(root)
    try:
        from heart.pulse import load_events
    except Exception:
        return []
    pref = goal_id + "-"
    return [e for e in load_events()
            if str(e.get("task_id") or "").startswith(pref)
            or (e.get("payload") or {}).get("goal_id") == goal_id]


def _task_parts(task_id: str, goal_id: str) -> tuple[str, int]:
    match = re.match(rf"^{re.escape(goal_id)}-(.+)-a(\d+)$", task_id)
    return (match.group(1), int(match.group(2))) if match else ("", 0)


def _episodes(root: Path, limit: int = 50) -> list[dict]:
    """Group the retained event journal into compact episode summaries."""
    goal_id = _goal_id(root)
    grouped: dict[str, list[dict]] = {}
    for event in _spine_events(root):
        episode_id = str(event.get("episode_id") or
                         (event.get("payload") or {}).get("episode_id") or "")
        if episode_id:
            grouped.setdefault(episode_id, []).append(event)
    out = []
    for episode_id, events in grouped.items():
        events.sort(key=lambda e: e.get("ts", ""))
        first, last = events[0], events[-1]
        task_id = str(first.get("task_id") or
                      next((e.get("task_id") for e in events if e.get("task_id")), ""))
        feature_id, attempt = _task_parts(task_id, goal_id)
        route = next((e for e in events if e.get("kind") == "route.decided"), {})
        route_data = {**route, **(route.get("payload") or {})}
        terminal = next((e for e in reversed(events)
                         if e.get("kind") in ("episode.finished", "episode.failed")), None)
        state = ("running" if terminal is None else
                 "failed" if terminal.get("kind") == "episode.failed" else
                 str((terminal.get("payload") or {}).get("outcome") or "finished"))
        costs = [{**e, **(e.get("payload") or {})} for e in events
                 if e.get("kind") == "role.finished"]
        out.append({
            "episode_id": episode_id,
            "task_id": task_id,
            "feature_id": feature_id,
            "attempt": attempt,
            "state": state,
            "started": first.get("ts", ""),
            "finished": terminal.get("ts", "") if terminal else "",
            "duration_ms": ((terminal or {}).get("duration_ms")
                            or ((terminal or {}).get("payload") or {}).get("duration_ms")),
            "tier": route_data.get("tier", ""),
            "agent": route_data.get("agent") or route_data.get("chosen", ""),
            "cost_usd": round(sum(float(e.get("cost_usd") or 0) for e in costs), 6),
            "verify_rounds": sum(e.get("kind") == "verify.round" for e in events),
            "outcome": state,
            "_last": last.get("ts", ""),
        })
    out.sort(key=lambda e: e["_last"], reverse=True)
    for episode in out:
        episode.pop("_last", None)
    return out[:max(1, min(limit, 200))]


def _episode_detail(root: Path, episode_id: str) -> dict | None:
    events = [e for e in _spine_events(root)
              if str(e.get("episode_id") or
                     (e.get("payload") or {}).get("episode_id") or "") == episode_id]
    if not events:
        return None
    summary = next((e for e in _episodes(root, 200)
                    if e["episode_id"] == episode_id), None)
    steps, memory, route, verify = [], [], [], []
    for event in sorted(events, key=lambda e: e.get("ts", "")):
        payload = event.get("payload") or {}
        row = {
            "ts": event.get("ts", ""),
            "source": event.get("source", "?"),
            "kind": event.get("kind", "?"),
            "role": event.get("role", ""),
            "duration_ms": event.get("duration_ms"),
            "detail": _event_detail(event.get("kind", ""), {
                **payload,
                **({"role": event["role"]} if event.get("role") else {}),
            }),
            "payload": payload,
        }
        steps.append(row)
        if event.get("source") in ("arteries", "capillaries"):
            memory.append(row)
        if event.get("kind") == "route.decided":
            route.append(row)
        if str(event.get("kind", "")).startswith("verify."):
            verify.append(row)
    return {"meta": summary or {"episode_id": episode_id},
            "steps": steps, "memory": memory, "route": route, "verify": verify}


def _dashboard(roots: list[Path], window_h: float = 24.0) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    # window_h 0 means all time. An empty cutoff sorts below every ISO
    # timestamp, so every `ts >= cutoff` filter below keeps working unchanged.
    cutoff = "" if not window_h else (now - datetime.timedelta(hours=window_h)).isoformat()
    cutoff_7d = (now - datetime.timedelta(days=7)).isoformat()
    projects = _list_goals(roots)
    alerts = []
    plexus_spend_24h = plexus_spend_7d = 0.0
    landed_24h = landed_7d = tokens_in = tokens_out = 0
    for root, project in zip(roots, projects):
        recs = ledger.read(root)
        project_24h = 0.0
        for rec in recs:
            ts = rec.get("ts", "")
            cost = float(rec.get("cost_usd") or 0)
            if ts >= cutoff:
                project_24h += cost
                tokens_in += int(rec.get("tokens_in") or 0)
                tokens_out += int(rec.get("tokens_out") or 0)
                landed_24h += rec.get("kind") == "feature.landed"
            if ts >= cutoff_7d:
                plexus_spend_7d += cost
                landed_7d += rec.get("kind") == "feature.landed"
        plexus_spend_24h += project_24h
        for escalation in _open_escalations(recs, project["goal_id"]):
            alerts.append({**escalation, "severity": "blocked",
                           "project_id": project["project_id"],
                           "goal_id": project["goal_id"]})
        if project["code"] == 2:
            alerts.append({"severity": "stalled",
                           "project_id": project["project_id"],
                           "goal_id": project["goal_id"],
                           "reason": project["status"]})
    episodes = []
    for root in roots:
        episodes.extend({**episode, "project_id": _project_id(root),
                         "goal_id": _goal_id(root)}
                        for episode in _episodes(root, 10))
    episodes.sort(key=lambda e: e.get("started", ""), reverse=True)
    telemetry = _fleet_telemetry(roots, cutoff, cutoff_7d, window_h)
    return {
        "cost": {**telemetry["cost"], "window_h": window_h,
                 "plexus_attributed": {
                     "cost_usd": round(plexus_spend_24h, 6),
                     "seven_day": round(plexus_spend_7d, 6),
                     "tokens_in": tokens_in, "tokens_out": tokens_out,
                 }},
        "runs": {
            "running": sum(p["running"] for p in projects),
            "blocked": sum(p["code"] == 1 for p in projects),
            "stalled": sum(p["code"] == 2 for p in projects),
            "landed": landed_24h, "landed_7d": landed_7d,
            "projects": len(projects),
        },
        "alerts": alerts,
        "recent_episodes": episodes[:12],
        "activity": telemetry["activity"],
        "stack_health": observe.stack(hours=window_h),
    }


def _elapsed_hours(events: list[dict]) -> float:
    """Hours from the oldest event to now — the real span of an all-time window."""
    first = min((event.get("ts", "") for event in events if event.get("ts")),
                default="")
    try:
        start = datetime.datetime.fromisoformat(first.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    now = datetime.datetime.now(start.tzinfo or datetime.timezone.utc)
    return max(0.0, (now - start).total_seconds() / 3600)


def _fleet_telemetry(roots: list[Path], cutoff: str, cutoff_7d: str,
                     window_h: float) -> dict:
    """Real-time activity from the shared spine, including ordinary CLI turns.

    Tokens remain exact-only: subscription hooks currently expose turn activity
    but not provider usage, while Heart role completions include tokens when the
    underlying CLI/API returns them. Never estimate tokens from character counts.
    """
    # keep whichever reaches further back: the selected window may be a year,
    # and the 7-day figures still have to come out of the same list
    retain_cutoff = min(cutoff, cutoff_7d)
    try:
        from heart.pulse import load_events
        history = load_events()
        retained = [event for event in history
                    if event.get("ts", "") >= retain_cutoff]
        history_h = _elapsed_hours(history)
    except Exception:
        retained, history_h = [], 0.0
    events = [event for event in retained if event.get("ts", "") >= cutoff]
    sources = Counter(str(event.get("source", "unknown")) for event in events)
    turns = {str(event.get("turn_id")) for event in events
             if event.get("kind") == "turn.observed" and event.get("turn_id")}
    responses = {str(event.get("turn_id")) for event in events
                 if event.get("kind") == "assistant.response" and event.get("turn_id")}
    # role.finished is an episode's agent invocation, turn.observed an
    # interactive CLI turn — disjoint sources, so both count without overlap.
    # episode.finished is excluded: it aggregates its own roles.
    metered = [(event.get("payload") or {}) for event in events
               if event.get("kind") in ("role.finished", "turn.observed")]
    metered = [payload for payload in metered
               if payload.get("tokens_in") is not None
               or payload.get("tokens_out") is not None
               or payload.get("cache_read") is not None]
    activity = {
        "events": len(events),
        "turns": len(turns),
        "responses": len(responses),
        "retrievals": sum(event.get("kind") == "prompt.retrieved" for event in events),
        "active_5m": sum(event.get("ts", "") >= (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=5)).isoformat() for event in events),
        "last_event": max((event.get("ts", "") for event in events), default=""),
        "tokens_in": sum(int(payload.get("tokens_in") or 0) for payload in metered),
        "tokens_out": sum(int(payload.get("tokens_out") or 0) for payload in metered),
        # reported separately, never added into tokens_in: they are the same
        # kind of thing at a tenth (or five times) the price, and the split is
        # the only way to see that a prompt was mostly cache
        "cache_read": sum(int(payload.get("cache_read") or 0) for payload in metered),
        "cache_write": sum(int(payload.get("cache_write_5m") or 0)
                           + int(payload.get("cache_write_1h") or 0)
                           for payload in metered),
        "metered_turns": len(metered),
        # No dollars here on purpose. This panel counts volume; the Cost panel
        # owns money and prices these same tokens from the rate card. Summing
        # payload cost_usd would report $0.00 beside it, because an interactive
        # turn carries counts and no price — arteries measures, plexus values.
        "by_source": [{"source": source, "events": count}
                      for source, count in sources.most_common()],
    }
    from . import registry
    accounting = registry.accounting_config()
    subscriptions, pricing = accounting["subscriptions"], accounting["pricing"]
    root_map = {str(root.resolve()): {
        "project_id": _project_id(root), "name": root.name,
    } for root in roots}

    def project_for(payload: dict) -> tuple[str, str]:
        candidate = payload.get("repo") or payload.get("cwd")
        if candidate:
            try:
                path = Path(str(candidate)).expanduser().resolve()
                matches = [(repo, info) for repo, info in root_map.items()
                           if path == Path(repo) or Path(repo) in path.parents]
                if matches:
                    _, info = max(matches, key=lambda item: len(item[0]))
                    return info["project_id"], info["name"]
            except OSError:
                pass
        project = str(payload.get("project_id") or "")
        for info in root_map.values():
            if project == info["name"]:
                return info["project_id"], info["name"]
        return "", "Unassigned"

    def provider(payload: dict) -> str:
        value = str(payload.get("cli") or payload.get("agent") or "").lower()
        return value.partition(":")[0]

    def _cached(payload: dict) -> int:
        """Cache tokens on a turn. A turn whose whole prompt was a cache hit
        reports tokens_in 0 and is still real money, so it must not be skipped
        for looking empty."""
        return sum(int(payload.get(b) or 0) for b in CACHE_MULTIPLIERS)

    def costs(window_events: list[dict], hours: float) -> dict:
        # Two ledgers, never summed into one bar. `marginal` is money that exists
        # because a turn ran — metered API calls. `seat` is a prorated slice of a
        # monthly subscription, which is owed whether or not the fleet did
        # anything, so attributing it to a project says "this project used the
        # seat most", not "this project cost you this". Mixing them makes a
        # project on a seat you already bought look expensive.
        project_marginal: Counter = Counter()
        project_seat: Counter = Counter()
        provider_projects: dict[str, Counter] = {
            "claude": Counter(), "codex": Counter(),
        }
        metered_api = equivalent = 0.0
        # per provider too: a pooled ratio is dominated by whichever
        # subscription costs most, which is not necessarily the one doing the
        # work. Claude at $20 and Codex at $200 share a denominator that is 91%
        # Codex, so one blended number hides both that Claude returns ~80x and
        # that Codex has no measured usage at all.
        equivalent_by: Counter = Counter()
        # tokens per provider, so the panel's dropdown can filter to one plan
        tokens_by: dict[str, Counter] = {}
        local_turns = local_tokens = local_duration = 0
        tokens_in_total = tokens_out_total = cache_total = 0
        # Turns we could not price, kept apart from turns that cost nothing.
        # Both render as $0.00 if you only sum money, which is how a provider
        # with no adapter looks free forever.
        #   unmeasured  arteries told us it had no way to read this turn's usage
        #   unattributed  real tokens, but no `cli` to pick a rate card with
        #   unpriced    known provider, but the rate card has no usable row
        gaps: Counter = Counter()
        models_seen: dict[str, Counter] = {}
        # premium speed tiers, counted so a 2x bill is explainable rather than
        # just a larger number than last week
        fast_turns: Counter = Counter()
        # read heart's card once, not once per event: it is a file read, and a
        # busy window is thousands of turns
        model_rates = registry.heart_model_rates()
        for event in window_events:
            payload = event.get("payload") or {}
            if event.get("kind") == "turn.observed":
                p = provider(payload)
                if p in provider_projects:
                    provider_projects[p][project_for(payload)] += 1
            # An interactive CLI turn and an episode role are disjoint: arteries
            # emits the first, heart the second, and no turn produces both. So
            # pricing them in the same pass adds no double count — while leaving
            # turn.observed out priced a subscription seat's entire real
            # workload at zero. `episode.finished` stays excluded on purpose: it
            # is the *sum* of its role.finished events, so counting it too would
            # double every episode. tests/test_plexus.py pins that.
            if event.get("kind") not in ("role.finished", "turn.observed"):
                continue
            p = provider(payload)
            # arteries stamps this when it had no transcript and no declared
            # counts. Recording it is the whole point: silence here is why the
            # panel could read $0.00 through a month of real work.
            if payload.get("usage_source") == "unavailable":
                gaps["unmeasured"] += 1
            tin, tout = int(payload.get("tokens_in") or 0), int(payload.get("tokens_out") or 0)
            tokens_in_total += tin
            tokens_out_total += tout
            cached = _cached(payload)
            cache_total += cached
            bucket = tokens_by.setdefault(p, Counter())
            bucket["tokens_in"] += tin
            bucket["tokens_out"] += tout
            bucket["cache_tokens"] += cached
            value = payload.get("cost_usd")
            if p == "api" and float(value or 0) == 0:
                local_turns += 1
                local_tokens += tin + tout
                local_duration += int(event.get("duration_ms") or 0)
            elif p == "api" and value is not None:
                amount = float(value)
                metered_api += amount
                project_marginal[project_for(payload)] += amount
            elif p in ("claude", "codex") and value is not None:
                equivalent += float(value)
                equivalent_by[p] += float(value)
            elif p in ("claude", "codex") and (tin or tout or _cached(payload)):
                # Bill the model, not just the vendor. arteries reports it on
                # every turn it can measure; without it a Haiku turn and an
                # Opus turn on the same CLI price identically, which is wrong
                # by more than an order of magnitude in whichever direction you
                # use less. Falls back to the provider rate when the rate card
                # has no row for this model, so an unconfigured stack prices
                # exactly as it did before.
                model = str(payload.get("model") or "")
                rates = registry.rates_for(pricing, p, model, model_rates)
                if rates is None:
                    gaps["unpriced"] += 1
                    continue
                if model:
                    models_seen.setdefault(p, Counter())[model] += 1
                # Fast mode bills at 2x across the whole window, and the cache
                # multipliers stack on top of it — so it scales the base rates
                # before anything else is worked out, not the total afterwards.
                # A turn with no `speed` is standard: absent means the host
                # predates the field, and every one of those ran standard.
                speed = str(payload.get("speed") or "standard")
                boost = speed_multiplier(speed)
                if boost != 1.0:
                    fast_turns[speed] += 1
                in_rate = rates["input"] * boost
                out_rate = rates["output"] * boost
                # cache buckets price off the base input rate at heart's
                # multipliers — the same constant heart bills with, imported
                # rather than copied so the two can't drift apart silently
                cache_cost = sum(
                    int(payload.get(bucket) or 0) * in_rate * mult
                    for bucket, mult in CACHE_MULTIPLIERS.items())
                priced = (tin * in_rate + tout * out_rate
                          + cache_cost) / 1_000_000
                equivalent += priced
                equivalent_by[p] += priced
            elif tin or tout or cached:
                # real tokens, but `cli` was absent so no rate card applies.
                # Counted rather than dropped: a turn falling off the bottom of
                # this chain used to leave no trace at all.
                gaps["unattributed"] += 1
        subscription = 0.0
        by_provider = []
        month_hours = 365.2425 / 12 * 24
        for p, monthly in subscriptions.items():
            accrued = float(monthly) * hours / month_hours
            subscription += accrued
            tok = tokens_by.get(p) or Counter()
            by_provider.append({
                "provider": p, "monthly": float(monthly),
                # what this plan actually costs you in the window. A subscription
                # accrues whether or not it is used, so that *is* its cost —
                # unlike `equivalent`, which is what the same work would have
                # cost on metered API and is a comparison, not a bill.
                "cost": round(accrued, 6),
                "equivalent": round(equivalent_by.get(p, 0.0), 6),
                "tokens_in": tok["tokens_in"], "tokens_out": tok["tokens_out"],
                "cache_tokens": tok["cache_tokens"],
                "tokens": tok["tokens_in"] + tok["tokens_out"] + tok["cache_tokens"],
            })
            weights = provider_projects[p]
            total_weight = sum(weights.values())
            if total_weight:
                for project, weight in weights.items():
                    project_seat[project] += accrued * weight / total_weight
            elif accrued:
                project_seat[("", "Unassigned")] += accrued
        api_tok = tokens_by.get("api") or Counter()
        if metered_api or api_tok:
            by_provider.append({
                "provider": "api", "monthly": 0.0,
                "cost": round(metered_api, 6), "equivalent": round(metered_api, 6),
                "tokens_in": api_tok["tokens_in"], "tokens_out": api_tok["tokens_out"],
                "cache_tokens": api_tok["cache_tokens"],
                "tokens": api_tok["tokens_in"] + api_tok["tokens_out"] + api_tok["cache_tokens"],
            })
        return {
            "total": round(subscription + metered_api, 6),
            "subscription": round(subscription, 6),
            "metered_api": round(metered_api, 6),
            "equivalent_api": round(equivalent, 6),
            # what those subscription turns would have cost at API rates, over
            # what the seats accrued. >1 means the seats are paying for
            # themselves; it is the only reading either number supports alone.
            # None, not 0.0, when nothing was measured: a seat that ran real
            # work we failed to price is not a seat at 0% utilisation, and
            # showing the second is worse than showing nothing.
            "seat_utilisation": (round(equivalent / subscription, 4)
                                 if subscription and equivalent else None),
            "tokens_in": tokens_in_total, "tokens_out": tokens_out_total,
            "cache_tokens": cache_total,
            "local": {"turns": local_turns, "tokens": local_tokens,
                      "duration_ms": local_duration},
            "by_provider": by_provider,
            # turns money could not be attached to, by reason. A zero here is a
            # real claim; an absent field never was.
            "gaps": dict(gaps),
            # which models actually ran, per provider — the input you need to
            # decide whether a per-model rate is worth configuring
            "models": {p: dict(c) for p, c in models_seen.items()},
            # turns billed above the standard rate, by tier
            "premium_speed": dict(fast_turns),
            "project_marginal": project_marginal, "project_seat": project_seat,
        }

    # Seat cost is prorated by elapsed hours, and a window can reach back
    # further than the data does. Capping at the real span of history is what
    # stops a one-year window from billing twelve months of subscription
    # against one month of observed work — the figure looked plausible and was
    # pure fiction. All time is the same rule with no window to cap against.
    hours = min(window_h, history_h) if window_h else history_h
    current = costs(events, hours)
    week = costs([event for event in retained
                  if event.get("ts", "") >= cutoff_7d], min(24 * 7, history_h))
    marginal, seat = current.pop("project_marginal"), current.pop("project_seat")
    project_rows = [
        {"project_id": project_id, "name": name,
         "marginal_usd": round(marginal.get(key, 0.0), 6),
         "seat_usd": round(seat.get(key, 0.0), 6),
         "cost_usd": round(marginal.get(key, 0.0) + seat.get(key, 0.0), 6),
         "unassigned": not bool(project_id)}
        for key in dict.fromkeys([*marginal, *seat])
        for project_id, name in [key]
    ]
    # marginal first: real money outranks an allocation of money already spent
    project_rows.sort(key=lambda row: (row["marginal_usd"], row["seat_usd"]),
                      reverse=True)
    week.pop("project_marginal")
    week.pop("project_seat")
    week.pop("by_provider")
    return {"activity": activity, "cost": {
        **current, "seven_day": week["total"], "by_project": project_rows,
        "subscriptions": subscriptions, "pricing": pricing,
        # what the signed-in CLI plans imply, so the settings form can say the
        # seat numbers were read rather than guessed
        "detected_subscriptions": accounting.get("detected_subscriptions", {}),
    }}


def _run_env(local_slots: int, global_agents: int = 0) -> dict[str, str]:
    """Child env for a spawned plan/run. The dashboard's fleet caps are
    authoritative, so they're always stamped in (0 disables — heart treats <=0
    as off for both), overriding whatever the shell that launched `plexus serve`
    had set. Two resources, two caps: HEART_LOCAL_SLOTS bounds the one local
    model server, HEART_MAX_AGENTS_GLOBAL bounds every agent (the knob that
    matters when goals share a paid key or a throttled subscription seat)."""
    return {**os.environ,
            "HEART_LOCAL_SLOTS": str(max(0, local_slots)),
            "HEART_MAX_AGENTS_GLOBAL": str(max(0, global_agents))}


def _spawn(root: Path, *args: str, local_slots: int = 0,
           global_agents: int = 0, roots: list[Path] | None = None) -> bool:
    """Run a plexus subcommand detached so Stop can kill the whole tree (heart's
    agent children included), not just the python parent. The fleet caps ride
    into the child's env so every goal the menu launches shares one bound on the
    shared model resources.

    Where it runs matters as much as that it runs. With tmux present the job
    gets a window in the project's session, which means the agent CLI has a real
    TTY: watchable in the browser, attachable from your own shell, and able to
    stop on a prompt you can actually answer. Without tmux it falls back to a
    bare Popen, which is what this always did — and where a run that pauses for
    input hangs somewhere nobody can see.
    """
    key = str(root.resolve())
    _reap()
    if _running(root) or key in _PROCS:
        return False
    kind = args[0] if args else "unknown"
    cmd = [sys.executable, "-m", "plexus.cli", *args, "--root", str(root)]
    env = _run_env(local_slots, global_agents)
    job = None
    from . import term
    if term.available():
        stamp = datetime.datetime.now().strftime("%H%M%S")
        work = Path(root) / ".plexus"
        job = term.run_window(
            _term_name(root, roots or [root]), Path(root), f"{kind}-{stamp}",
            cmd, env,
            work / "transcripts" / f"{kind}-{stamp}.log",
            work / "jobs" / f"{kind}-{stamp}.exit")
    if job is None:
        job = subprocess.Popen(cmd, start_new_session=True, env=env)
    _PROCS[key] = job
    _PROC_KINDS[key] = kind
    _PROC_RESULTS.pop(key, None)
    return True


def _approved(root: Path) -> bool:
    try:
        plan_id, _ = _current_plan(root)
        return _plan_approved(ledger.read(root), _goal_id(root), plan_id)
    except Exception:
        return False


def _startable(roots: list[Path], cap: int) -> list[Path]:
    """Approved, not-currently-running goals to launch on 'Run all', capped so
    the fleet doesn't oversubscribe. cap<=0 means no goal-count cap (the local
    slot semaphore is still the real throttle on the GPU). Runs already in
    flight count against the cap, so 'Run all' tops up rather than piling on."""
    idle = [r for r in roots if _approved(r) and not _running(r)]
    if cap and cap > 0:
        free = max(0, cap - sum(_running(r) for r in roots))
        idle = idle[:free]
    return idle


def _stop(root: Path) -> bool:
    """Stop whatever run holds the lock — ours or a terminal's — via the pid it
    stamped into the lock file. SIGTERM the process group so heart's agent
    children die with it. Between features this is clean (state is fsynced per
    record, resume-from-next-open-child picks it up); mid-episode leaves a
    worktree for heart to clean. ponytail: getpgid(pid) is the run's own group
    for both a Popen(start_new_session) child and a shell-foreground run."""
    if not _running(root):
        return False
    try:
        pid = int((Path(root) / ".plexus" / "lock").read_text().strip())
        # guard against PID reuse: the flock says *a* run holds the lock, but the
        # stamped pid could have been recycled by an unrelated process. Only
        # signal if the pid still looks like a plexus run. On platforms without
        # /proc we can't check — the flock gate above is the fallback.
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"plexus" not in cmdline:
                return False
        except OSError:
            pass
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return False  # empty/torn lock (truncate window) or already gone
    _reap()
    return True


# --- headless fleet scheduler (`plexus fleet run`) -------------------------
# The dashboard's Run-all needs a human at the browser; this is its unattended
# twin, meant to be fired by a systemd --user timer or cron. It advances every
# approved, idle, unblocked goal once (concurrency capped), respects a rolling
# cost ceiling, and exits when the fleet is drained — so a oneshot timer never
# overlaps itself and never re-runs a goal a human still owns.


def _goal_run_state(root: Path) -> str:
    """'done' (all planned features landed), 'escalated' (a block a human owns),
    or 'open' (has work the scheduler may advance). Derived from the ledger, the
    same source `plexus status` reads."""
    recs = ledger.read(root)
    gid = _goal_id(root)
    if _open_escalations(recs, gid):
        return "escalated"
    try:
        plan = load_plan(root)
    except SystemExit:
        plan = []
    if plan and all(_feature_state(recs, gid, f["id"])[0] == "landed" for f in plan):
        return "done"
    return "open"


def _fleet_cost(roots: list[Path], window_h: float) -> float:
    """Non-local spend across the fleet in the last window_h hours. Local models
    price to 0.0 (heart), so summing cost_usd already counts only metered APIs
    and subscription seats billed at API rates — exactly the ceiling's scope."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=window_h)).isoformat()
    total = 0.0
    for r in roots:
        for rec in ledger.read(r):
            if rec.get("cost_usd") is not None and rec.get("ts", "") >= cutoff:
                total += rec["cost_usd"]
    return round(total, 6)


def _in_window(window: str, now: datetime.time | None = None) -> bool:
    """window is 'HH:MM-HH:MM' in local time; handles overnight wrap (22:00-08:00)."""
    a, _, b = window.partition("-")
    now = now or datetime.datetime.now().time()
    start = datetime.datetime.strptime(a.strip(), "%H:%M").time()
    end = datetime.datetime.strptime(b.strip(), "%H:%M").time()
    return start <= now < end if start <= end else (now >= start or now < end)


def fleet_run(root: str = ".", max_goals: int = 3, cost_ceiling: float = 0.0,
              cost_window_h: float = 24.0, local_slots: int = 0,
              global_agents: int = 0, run_window: str | None = None) -> int:
    roots = menu_roots(Path(root))
    if not roots:
        print(f"no plexus.toml under {Path(root).resolve()}")
        return 2
    if run_window and not _in_window(run_window):
        print(f"fleet: outside run window {run_window}, nothing started")
        return 0
    env = _run_env(local_slots, global_agents)
    attempted: set = set()          # started once per invocation — no busy-loop
    procs: dict[Path, subprocess.Popen] = {}
    while True:
        spent = _fleet_cost(roots, cost_window_h)
        over = bool(cost_ceiling) and spent >= cost_ceiling
        if not over:
            for r in roots:
                if len(procs) >= max_goals:
                    break
                if r in attempted or r in procs or _running(r) or not _approved(r):
                    continue
                if _goal_run_state(r) != "open":
                    continue
                cmd = [sys.executable, "-m", "plexus.cli", "run",
                       "--candidates", "1", "--root", str(r)]
                procs[r] = subprocess.Popen(cmd, start_new_session=True, env=env)
                attempted.add(r)
        if not procs:
            if over:
                print(f"fleet: paused — ${spent:.4f} >= ${cost_ceiling:.4f} "
                      f"ceiling over {cost_window_h:g}h")
            break
        time.sleep(2)
        for r, p in list(procs.items()):
            if p.poll() is not None:
                procs.pop(r)
    print(f"fleet: advanced {len(attempted)} goal(s); "
          f"spent ${_fleet_cost(roots, cost_window_h):.4f} over {cost_window_h:g}h")
    return 0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet; this is a local tool
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._json({"error": "not found"}, 404)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if path.name != "index.html":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            # Explicit, not merely absent: with no cache header at all a browser
            # applies its own heuristic freshness and can keep serving a stale
            # index — which pins it to the old hashed assets, so a rebuilt
            # frontend never arrives and a fixed bug looks unfixed.
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _root(self, qs) -> Path:
        return Path(qs["root"][0])

    def _guard(self) -> bool:
        """Reject cross-origin / rebinding requests. This server runs commands,
        SIGTERMs process groups and approves plans, so a page the user happens to
        visit must not be able to drive it via localhost. Require a loopback Host
        (blocks DNS-rebinding through an attacker hostname) and, when a browser
        sends Origin, require it to be loopback too (blocks plain CSRF). curl and
        same-origin fetches (no cross-origin Origin) pass."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host and host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def _allowed_root(self, root: Path) -> bool:
        """A request may only act on a goal repo this server actually scanned —
        never an arbitrary path from the request body."""
        try:
            return root.resolve() in set(self.server.roots)
        except OSError:
            return False

    def _term_view(self, root: Path, view: str) -> str:
        """tmux session name for one view of a project.

        `shell` is the project's own terminal; the others are drafting
        conversations, one per tab. Separate sessions rather than windows so a
        tab can show its own without the tabs fighting over a shared current
        window, and so closing one is a kill-session."""
        base = _term_name(root, self.server.roots)
        return base if view in ("", "shell") else f"{base}-{re.sub(r'[^a-z]', '', view)}"

    def _term_session(self, root: Path, view: str = "shell",
                      cols: int = 120, rows: int = 32):
        from . import term
        if not term.available():
            return None
        return term.get(self._term_view(root, view), root, cols, rows)

    def _term_ws(self, qs) -> None:
        """The terminal, over one WebSocket.

        This replaced SSE-out plus a POST per keystroke. That shape could not
        keep input in order — `fetch` promises nothing about ordering and each
        POST landed on its own server thread — so typing quickly arrived
        scrambled, with characters missing. A socket is ordered by
        construction, carries bytes without base64, and costs one connection
        rather than one per key.
        """
        from . import term
        root = self._root(qs)
        if not self._allowed_root(root):
            return self._json({"error": "unknown root"}, 403)
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json({"error": "not a websocket request"}, 400)
        try:
            cols = max(20, min(int(qs.get("cols", ["120"])[0]), 500))
            rows = max(5, min(int(qs.get("rows", ["32"])[0]), 200))
        except ValueError:
            return self._json({"error": "invalid size"}, 400)
        session = self._term_session(root, qs.get("view", ["shell"])[0], cols, rows)
        if session is None:
            return self._json({"error": "tmux not installed"}, 503)

        # Written by hand rather than via send_response: an upgrade needs an
        # HTTP/1.1 status line and this handler speaks 1.0 everywhere else.
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + term.ws_accept(key).encode() + b"\r\n\r\n")
        self.wfile.flush()

        send_lock = threading.Lock()

        def send(payload: bytes, opcode: int = term.OP_BINARY) -> None:
            with send_lock:  # frames must not interleave on the wire
                self.wfile.write(term.ws_frame(payload, opcode))
                self.wfile.flush()

        stop = threading.Event()

        def pump() -> None:
            queue, backlog = session.subscribe()
            try:
                if backlog:
                    send(backlog)
                while not stop.is_set():
                    try:
                        chunk = queue.get(timeout=20)
                    except Empty:
                        send(b"", term.OP_PING)
                        continue
                    if chunk is None:
                        break
                    send(chunk)
            except (OSError, ValueError):
                pass
            finally:
                session.unsubscribe(queue)

        writer = threading.Thread(target=pump, daemon=True)
        writer.start()
        try:
            while True:
                frame = term.ws_read(self.rfile)
                if frame is None:
                    break
                opcode, data = frame
                if opcode == term.OP_CLOSE:
                    break
                if opcode == term.OP_PING:
                    send(data, term.OP_PONG)
                elif opcode == term.OP_TEXT:
                    # control channel: resize only. Input is always binary, so
                    # a keystroke can never be read as a command.
                    try:
                        message = json.loads(data or b"{}")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if "cols" in message and "rows" in message:
                        session.resize(max(20, min(int(message["cols"]), 500)),
                                       max(5, min(int(message["rows"]), 200)))
                elif opcode == term.OP_BINARY and data:
                    session.write(data)
        except (OSError, ValueError, struct.error):
            pass
        finally:
            stop.set()

    def do_GET(self):
        if not self._guard():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            index = _STATIC / "index.html"
            if index.exists():
                self._static(index)
            else:  # source checkout before the optional frontend build
                body = _HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif u.path.startswith("/assets/"):
            asset = (_STATIC / u.path.removeprefix("/")).resolve()
            if _STATIC.resolve() not in asset.parents:
                return self._json({"error": "not found"}, 404)
            self._static(asset)
        elif u.path == "/api/goals":
            self._json(_list_goals(self.server.roots))
        elif u.path == "/api/dashboard":
            try:
                # 0 is all time; anything else clamps to [1h, 1 year]
                requested = float(qs.get("window_h", ["24"])[0])
                window_h = 0.0 if requested <= 0 else max(1.0, min(requested, 24 * 366))
            except ValueError:
                return self._json({"error": "invalid window_h"}, 400)
            self._json(_dashboard(self.server.roots, window_h))
        elif u.path == "/api/fleet":
            self._json(_fleet_state(self.server))
        elif u.path == "/api/accounting":
            from . import registry
            self._json(registry.accounting_config())
        elif u.path == "/api/goal":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            self._json(_goal_detail(root))
        elif u.path == "/api/live":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            try:
                limit = max(60, min(int(qs.get("limit", ["500"])[0]), 2000))
            except ValueError:
                return self._json({"error": "invalid limit"}, 400)
            events = _live(root, limit=limit)
            if "since" in qs:
                since = qs["since"][0]
                events = [event for event in events if event.get("ts", "") > since]
                cursor = max((event.get("ts", "") for event in events), default=since)
                self._json({"events": events, "cursor": cursor})
            else:
                self._json(events)  # compatibility with the old embedded UI
        elif u.path == "/api/episodes":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except ValueError:
                return self._json({"error": "invalid limit"}, 400)
            self._json(_episodes(root, limit))
        elif u.path == "/api/episode":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            detail = _episode_detail(root, qs.get("id", [""])[0])
            self._json(detail) if detail else self._json({"error": "episode not found"}, 404)
        elif u.path == "/api/term/ws":
            self._term_ws(qs)
        elif u.path == "/api/overview":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            self._json({"sections": overview.read(root),
                        "assets": overview.assets(root)})
        elif u.path == "/api/overview-asset":
            # images referenced from a section, served out of the repo. Resolved
            # and re-checked against the root: a section is written by a model,
            # so its paths are untrusted input like any other.
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            rel = qs.get("path", [""])[0]
            try:
                target = (root / rel).resolve()
            except OSError:
                return self._json({"error": "bad path"}, 400)
            if root.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "not found"}, 404)
            self._static(target)
        elif u.path == "/api/validation":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            self._json(_validation(root))
        elif u.path == "/api/tasks":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            from . import tasks
            self._json(tasks.group(root))
        elif u.path == "/api/term/sessions":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            from . import term
            base = _term_name(root, self.server.roots)
            out = []
            for view in ("shell", "overview", "tasks"):
                name = base if view == "shell" else f"{base}-{view}"
                if term.exists(name):
                    out.append({"view": view, "name": name})
            self._json({"sessions": out, "base": base})
        elif u.path == "/api/term/windows":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            self._json(_term_windows(root, self.server.roots))
        elif u.path == "/api/term/transcript":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            name = Path(qs.get("name", [""])[0]).name  # basename only: no ../
            path = Path(root) / ".plexus" / "transcripts" / name
            if not name or not path.is_file():
                return self._json({"error": "no such transcript"}, 404)
            self._json({"name": name, "text": _log_tail(path, 400_000)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        # Fleet-wide routes own no goal, so they're handled before root
        # extraction. /fleet sets the caps applied to future spawns (partial
        # update — only the keys sent). /run_all launches the approved, idle
        # goals up to the goal cap, each carrying the current caps.
        if u.path == "/api/fleet":
            for k in ("local_slots", "global_agents", "max_goals"):
                if k in data:
                    setattr(self.server, k, max(0, int(data[k])))
            return self._json(_fleet_state(self.server))
        if u.path == "/api/accounting":
            from . import registry
            try:
                return self._json(registry.set_accounting_config(
                    data.get("subscriptions", {}), data.get("pricing", {})))
            except (TypeError, ValueError, OSError) as exc:
                return self._json({"error": str(exc)}, 400)
        if u.path == "/api/add":
            # 'Add Folder to Workspace' — register a project dir, then rescan so
            # its goals join the menu immediately. Loopback-guarded like every
            # write path; only ever touches the user's own workspace file.
            from . import registry
            path = Path(data.get("path", "")).expanduser()
            if not path.is_dir():
                return self._json({"error": f"not a directory: {path}"}, 400)
            registry.add_workspace_root(path)
            self.server.roots = menu_roots(self.server.base)
            return self._json({"roots": [str(r) for r in self.server.roots]})
        if u.path == "/api/project":
            # grid view state: label (group) / pinned flag for one project.
            from . import registry
            root = Path(data["root"])
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            fields = {k: data[k] for k in ("label", "pinned") if k in data}
            try:
                registry.set_project_meta(root, **fields)
            except OSError as exc:
                return self._json({"error": f"workspace metadata is not writable: {exc}"}, 500)
            return self._json({"ok": True})
        if u.path == "/api/run_all":
            # optional `roots` restricts the launch to one group/project — the
            # 'run this project' button — otherwise the whole idle fleet.
            want = set(data.get("roots") or [])
            started = _startable(self.server.roots, self.server.max_goals)
            if want:
                started = [r for r in started
                           if str(r) in want or str(r.resolve()) in want]
            for r in started:
                _spawn(r, "run", "--candidates", "1",
                       local_slots=self.server.local_slots,
                       global_agents=self.server.global_agents,
                       roots=self.server.roots)
            return self._json({"started": [str(r) for r in started]})
        root = Path(data["root"])
        if not self._allowed_root(root):
            return self._json({"error": "unknown root"}, 403)
        try:
            if u.path == "/api/task":
                from . import tasks
                if data.get("id"):
                    fields = {k: v for k, v in data.items()
                              if k not in ("root", "id")}
                    tasks.update(root, str(data["id"]), **fields)
                else:
                    tasks.create(
                        root, str(data.get("title", "")),
                        body=str(data.get("body", "")),
                        source_kind=str(data.get("source_kind", "manual")),
                        source_url=str(data.get("source_url", "")),
                        blocked_by=list(data.get("blocked_by") or []),
                        requires_plan=bool(data.get("requires_plan", True)))
                return self._json(tasks.group(root))
            elif u.path == "/api/term/close":
                from . import term
                return self._json({"ok": term.kill(
                    self._term_view(root, str(data.get("view", ""))))})
            elif u.path == "/api/overview":
                key = str(data.get("key", ""))
                if key not in overview.KEYS:
                    return self._json({"error": "unknown section"}, 400)
                return self._json(overview.write(root, key, str(data.get("text", ""))))
            elif u.path == "/api/discuss":
                # One conversation per tab, not one per field. It opens with
                # everything that tab currently holds, so the agent starts from
                # what is written rather than asking you to paste it back.
                from . import term
                view = str(data.get("view", "overview"))
                if view not in ("overview", "tasks"):
                    return self._json({"error": "no conversation for that view"}, 400)
                if not term.available():
                    return self._json({"error": "tmux is required to open a session"}, 503)
                spec = load_spec(root)
                name = self._term_view(root, view)
                if term.exists(name):
                    # already talking — rejoin rather than stacking another
                    return self._json({"ok": True, "session": name, "rejoined": True})
                prompt = (overview.discuss_prompt(root) if view == "overview"
                          else _tasks_discuss_prompt(root))
                # The prompt goes to a file and the agent is told to read it.
                # Sending a few thousand characters through send-keys means
                # every quote and newline in it becomes an escaping problem, and
                # a paste that large trips the bracketed-paste handling of some
                # CLIs. One short line has neither failure.
                brief = root / ".plexus" / f"discuss-{view}.md"
                brief.parent.mkdir(parents=True, exist_ok=True)
                brief.write_text(prompt, encoding="utf-8")
                started = term.start(
                    name, root, spec.agent,
                    f"Read {brief} and follow it.",
                    _run_env(self.server.local_slots, self.server.global_agents))
                if not started:
                    return self._json({"error": "could not open a session"}, 500)
                return self._json({"ok": True, "session": name, "rejoined": False})
            elif u.path == "/api/pr-merge":
                # Explicit, human-initiated, one PR at a time. Squash because a
                # goal's history is episode noise and main should read as one
                # decision per task. Never automatic: this is the last gate
                # before code reaches the branch everything else builds on.
                number = int(data.get("number") or 0)
                if number <= 0:
                    return self._json({"error": "which PR?"}, 400)
                merged = subprocess.run(
                    ["gh", "pr", "merge", str(number), "--squash"],
                    cwd=str(root), capture_output=True, text=True, timeout=90)
                if merged.returncode:
                    detail = (merged.stderr or merged.stdout).strip()
                    return self._json({"error": detail or "merge failed"}, 400)
                ledger.record("delivery.merged", goal_id=_goal_id(root), root=root,
                              pr=number, result=merged.stdout.strip())
                return self._json({"ok": True, "pr": number})
            elif u.path == "/api/task-from-issue":
                # One GitHub issue becomes one task. This is the only place a
                # source now enters the system — the charter never has one.
                from . import tasks
                issue = _import_github_issue(str(data.get("url", "")).strip())
                tasks.create(root, issue["source_title"] or issue["goal_id"],
                             body=issue["source_body"],
                             source_kind="github", source_url=issue["source_url"])
                return self._json(tasks.group(root))
            elif u.path == "/api/term/select":
                from . import term
                if not term.available():
                    return self._json({"error": "tmux not installed"}, 503)
                ok = term.select(self._term_view(root, str(data.get("view", "shell"))),
                                 str(data.get("window", "")))
                return self._json({"ok": ok})
            elif u.path == "/api/plan":
                if not overview.as_context(root).strip():
                    return self._json(
                        {"error": "write the overview first — the planner plans "
                                  "within it, and there is nothing there yet"}, 409)
                task_id = str(data.get("task", ""))
                if task_id:
                    from . import tasks as tasklib
                    tasklib.update(root, task_id, state="planning", error="")
                started = _spawn(root, "plan", *(["--task", task_id] if task_id else []),
                                 local_slots=self.server.local_slots,
                                 global_agents=self.server.global_agents,
                                 roots=self.server.roots)
                if not started:
                    return self._json({"error": "a plan or run job is already active"}, 409)
                return self._json({"ok": True, "state": "planning"})
            elif u.path == "/api/approve":
                from .plan import approve
                approve(load_spec(root), root, waive=data.get("waive", False),
                        task_id=str(data.get("task", "")))
            elif u.path == "/api/run":
                if not _approved(root):
                    return self._json({"error": "the current plan is not approved"}, 409)
                task_id = str(data.get("task", ""))
                started = _spawn(
                    root, "run", "--candidates", str(data.get("candidates", 1)),
                    *(["--task", task_id] if task_id else []),
                    local_slots=self.server.local_slots,
                    global_agents=self.server.global_agents,
                    roots=self.server.roots)
                if not started:
                    return self._json({"error": "a plan or run job is already active"}, 409)
                return self._json({"ok": True, "state": "running"})
            elif u.path == "/api/validate":
                spec = load_spec(root)
                lifecycle = _goal_lifecycle(root)
                if lifecycle["state"] != "validating":
                    return self._json({"error": "the goal is not awaiting manual validation"}, 409)
                confirmed = data.get("checks") or []
                if set(confirmed) != set(spec.manual_checks):
                    return self._json({"error": "confirm every manual check"}, 400)
                ledger.record("validation.manual_passed", goal_id=spec.goal_id,
                              root=root, checks=confirmed,
                              notes=str(data.get("notes", "")))
                ledger.record("goal.finished", goal_id=spec.goal_id, root=root,
                              outcome="scope_satisfied")
                from .run import _open_pr
                note = _open_pr(spec, root, str(root))
                if note:
                    ledger.record("delivery.requested", goal_id=spec.goal_id,
                                  root=root, result=note)
                return self._json({"ok": True, "delivery": note})
            elif u.path == "/api/stop":
                _stop(root)
            elif u.path == "/api/resolve":
                ledger.record("escalation.resolved", goal_id=_goal_id(root),
                              feature_id=data["feature"], root=root,
                              resolution=data.get("answer", "resolved"))
            else:
                return self._json({"error": "not found"}, 404)
        except (ValueError, TypeError, OSError, tomllib.TOMLDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        except SystemExit as e:  # approve rejects unusable criteria this way
            return self._json({"error": str(e)}, 400)
        self._json({"ok": True})


def serve(root: str = ".", port: int = 8100, local_slots: int = 0,
          global_agents: int = 0, max_goals: int = 0) -> int:
    base = Path(root)
    roots = menu_roots(base)
    if not roots:
        print(f"no plexus.toml under {base.resolve()} or the workspace — "
              f"run `plexus init` first, or `plexus add <path>`")
        return 2
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.base = base
    httpd.roots = roots
    # live fleet knobs, adjusted from the dashboard; plain ints, mutated by one
    # handler thread and read by others — the GIL makes the assignment atomic
    httpd.local_slots = max(0, local_slots)
    httpd.global_agents = max(0, global_agents)
    httpd.max_goals = max(0, max_goals)
    slots = f", local slots {httpd.local_slots}" if httpd.local_slots else ""
    print(f"plexus control plane: http://127.0.0.1:{port}  "
          f"({len(roots)} goal{'s' if len(roots) != 1 else ''}{slots})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


_HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>plexus control plane</title><style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;
gap:14px;align-items:center}header b{color:var(--accent)}
.wrap{display:flex;height:calc(100vh - 49px)}
.side{width:300px;border-right:1px solid var(--line);overflow:auto}
.g{padding:12px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.g:hover{background:var(--panel)}.g.sel{background:var(--panel);
border-left:3px solid var(--accent)}
.g .id{font-weight:600}.g .st{color:var(--dim);font-size:12px;margin-top:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.c0{background:var(--ok)}.c1{background:var(--warn)}.c2{background:var(--bad)}
.main{flex:1;overflow:auto;padding:18px}
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent}
.tab.on{color:var(--fg);border-bottom-color:var(--accent)}
button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
padding:6px 14px;border-radius:6px;cursor:pointer;font:inherit}
button:hover{border-color:var(--accent)}button.p{background:var(--accent);color:#0d1117;border:0}
button:disabled{opacity:.4;cursor:not-allowed}
.bar{display:flex;gap:8px;margin-bottom:16px;align-items:center}
.row{padding:10px 14px;border:1px solid var(--line);border-radius:6px;margin-bottom:8px;
background:var(--panel)}.row .t{font-weight:600}.row .m{color:var(--dim);font-size:12px;margin-top:4px}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line)}
.landed{color:var(--ok);border-color:var(--ok)}.escalated{color:var(--bad);border-color:var(--bad)}
.open{color:var(--dim)}
pre{white-space:pre-wrap;color:var(--dim);margin:0}
.act{font-size:12px;padding:5px 0;border-bottom:1px solid var(--line);display:flex;gap:10px}
.act .k{color:var(--accent);min-width:150px}.act .ts{color:var(--dim);min-width:60px}
.livelog{height:calc(100vh - 190px);overflow:auto;border:1px solid var(--line);
border-radius:6px;padding:4px 12px;background:var(--panel)}
textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:8px;font:inherit;margin:6px 0}
.hint{color:var(--dim);padding:40px;text-align:center}
.fleet{color:var(--dim);font-size:12px}.fleet input{width:3.2em;margin-left:4px;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:3px 6px;font:inherit}
.q{width:100%;padding:8px 12px;background:var(--bg);color:var(--fg);border:0;
border-bottom:1px solid var(--line);font:inherit;outline:none}
.grp{display:flex;align-items:center;justify-content:space-between;
padding:9px 16px 3px;color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.05em}
.mini{padding:1px 8px;font-size:11px;border-radius:5px}
.g .tools{float:right;opacity:0}.g:hover .tools{opacity:.7}
.g .tools span{cursor:pointer;margin-left:7px}.g .tools span:hover{opacity:1}
</style></head><body>
<header><b>plexus</b> control plane<span id=hdr style=color:var(--dim)></span>
<span class=fleet title="Add a project to the menu: a repo, or a parent of several. Like 'Add Folder to Workspace'.">+
<input id=addp type=text placeholder="path to add" style="width:12em"
 onkeydown="if(event.key==='Enter')addProject()"></span>
<span class=fleet style="margin-left:auto"
 title="local slots: cap on agents hitting the local model server (0=off).">local
<input id=slots type=number min=0 onchange="setFleet('local_slots',this.value)"></span>
<span class=fleet title="global agents: cap on ALL concurrent agents across goals — the knob for a shared paid key or throttled subscription seat (0=off).">global
<input id=global type=number min=0 onchange="setFleet('global_agents',this.value)"></span>
<span class=fleet title="max goals: how many goals 'Run all' launches at once (0=no cap; the local-slots semaphore still throttles the GPU).">goals
<input id=maxg type=number min=0 onchange="setFleet('max_goals',this.value)"></span>
<button class=p onclick="runAll()" title="Start every approved, idle goal, up to the goals cap.">▶▶ Run all</button></header>
<div class=wrap><div class=side>
<input id=q class=q placeholder="search projects…" oninput="goals()">
<div id=goallist></div></div><div class=main id=main>
<div class=hint>select a goal</div></div></div>
<script>
let sel=null, tab='run', query='';
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(p,b)=>j('/api'+p,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b)});
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

const PAL=['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#39c5cf','#db61a2','#e3b341'];
const dotColor=l=>{if(!l)return 'var(--dim)';let h=0;
  for(const c of l)h=(h*31+c.charCodeAt(0))>>>0;return PAL[h%PAL.length];};
let GROUPS={};
async function goals(){
  const gs=await j('/api/goals');
  const q=(document.getElementById('q')||{}).value||'';query=q.toLowerCase();
  const shown=gs.filter(g=>!query||g.goal_id.toLowerCase().includes(query)
    ||(g.label||'').toLowerCase().includes(query));
  const pinned=shown.filter(g=>g.pinned), rest=shown.filter(g=>!g.pinned);
  const groups={}, ungrouped=[];
  for(const g of rest){if(g.label)(groups[g.label]=groups[g.label]||[]).push(g);
    else ungrouped.push(g);}
  const row=g=>`<div class="g${g.root===sel?' sel':''}" onclick="pick('${g.root}')">
     <div class=id><span class=dot style="background:${dotColor(g.label)}"></span>${esc(g.goal_id)}
     ${g.running?'▶':''}<span class=tools>
       <span title="pin / unpin" onclick="event.stopPropagation();togglePin('${g.root}',${g.pinned?0:1})">${g.pinned?'★':'☆'}</span>
       <span title="set group label" onclick="event.stopPropagation();editLabel('${g.root}','${esc(g.label)}')">🏷</span>
     </span></div><div class=st>${esc(g.status)}</div></div>`;
  GROUPS={};let gi=0;
  const section=(title,list,run)=>{let k='';
    if(run){k='grp'+(gi++);GROUPS[k]=list.map(g=>g.root);}
    return `<div class=grp><span>${esc(title)}</span>`+
      (run?`<button class=mini title="Run this group's idle goals" onclick="runGroup('${k}')">▶▶</button>`:'')+
      `</div>`+list.map(row).join('');};
  let h='';
  if(pinned.length)h+=section('★ pinned',pinned,true);
  for(const label of Object.keys(groups).sort())h+=section(label,groups[label],true);
  if(ungrouped.length)h+=section('ungrouped',ungrouped,false);
  document.getElementById('goallist').innerHTML=h||'<div class=hint>no goals</div>';
}
async function togglePin(root,val){await post('/project',{root,pinned:val?true:null});goals();}
async function editLabel(root,cur){const l=prompt('group label (blank to clear):',cur||'');
  if(l===null)return;await post('/project',{root,label:l});goals();}
async function runGroup(k){const r=await post('/run_all',{roots:GROUPS[k]||[]});
  alert(r.started.length?'started '+r.started.length+' goal(s)':'nothing idle to start in this group');
  goals();detail();}
function pick(r){sel=r;detail();}
let liveTimer=null;
function setTab(t){tab=t;detail();
  if(liveTimer){clearInterval(liveTimer);liveTimer=null;}
  if(t==='live')liveTimer=setInterval(refreshLive,2000);}

async function detail(){
  if(!sel){return}
  const d=await j('/api/goal?root='+encodeURIComponent(sel));
  document.getElementById('hdr').textContent=' · '+d.goal_id+(d.running?' · running':'');
  const T=['run','live','plan','activity','escalations'];
  let h=`<div class=tabs>`+T.map(t=>`<div class="tab${t===tab?' on':''}"
    onclick="setTab('${t}')">${t}${t==='escalations'&&d.escalations.length?
    ' ('+d.escalations.length+')':''}</div>`).join('')+`</div>`;
  h+=`<div class=bar>`+
    (d.running?`<button class=p onclick="act('/stop')">■ Stop</button>`:
     d.approved?`<button class=p onclick="act('/run')">▶ Run</button>`:
     `<button onclick="act('/plan')">Plan</button>
      <button class=p onclick="act('/approve')">✓ Approve &amp; arm</button>`)+
    `<span style=color:var(--dim)>${esc(d.status)}</span></div>`;

  if(tab==='run') h+=d.features.map(f=>
    `<div class=row><div class=t>${esc(f.id)} · ${esc(f.title)}
     <span class="badge ${f.state}">${f.state}</span></div>
     <div class=m>acceptance: ${esc(f.acceptance)} · attempt ${f.attempt}</div></div>`
    ).join('')||'<div class=hint>no plan yet — hit Plan</div>';
  else if(tab==='live') h+=`<div id=livelog class=livelog><div class=hint>connecting…</div></div>`;
  else if(tab==='plan') h+=`<div class=row><pre>`+
    d.features.map(f=>`${f.id}: ${esc(f.title)}\n    ${esc(f.acceptance)}`).join('\n')+
    `</pre></div>`+(d.approved?'':'<div class=hint>plan not armed — Approve to enable Run</div>');
  else if(tab==='activity') h+=d.activity.map(a=>
    `<div class=act><span class=ts>${esc(a.ts.slice(11,19))}</span>
     <span class=k>${esc(a.kind)}</span><span>${esc(a.feature_id)} ${esc(a.reason)}</span></div>`
    ).join('')+`<div class=row style=margin-top:16px><b>insights</b><pre>${
    esc(d.insights.join('\n'))}</pre></div>`;
  else h+=d.escalations.map(e=>
    `<div class=row><div class=t>${esc(e.feature_id)}
     <span class="badge escalated">${esc(e.reason_class)}</span></div>
     <div class=m>${esc(e.reason)}</div>
     <textarea id="a_${e.feature_id}" placeholder="answer / decision…"></textarea>
     <button onclick="resolve('${e.feature_id}')">Resolve &amp; resume</button></div>`
    ).join('')||'<div class=hint>no open escalations</div>';
  document.getElementById('main').innerHTML=h;
  if(tab==='live')refreshLive();
}
const SRC={heart:'#58a6ff',plexus:'#bc8cff',arteries:'#3fb950',
  capillaries:'#d29922',marrow:'#db61a2'};
const srcColor=s=>SRC[s]||'var(--dim)';
async function refreshLive(){
  if(!sel||tab!=='live')return;
  const evs=await j('/api/live?root='+encodeURIComponent(sel));
  const el=document.getElementById('livelog');if(!el)return;
  const near=el.scrollHeight-el.scrollTop-el.clientHeight<50;
  el.innerHTML=evs.map(e=>`<div class=act><span class=ts>${esc((e.ts||'').slice(11,19))}</span>
    <span class=k style="color:${srcColor(e.source)};min-width:190px">${esc(e.source)}·${esc(e.kind)}</span>
    <span>${esc(e.detail)}</span></div>`).join('')
    ||'<div class=hint>no events yet for this goal — start a run to watch it work</div>';
  if(near)el.scrollTop=el.scrollHeight;
}
async function act(p){const r=await post(p,{root:sel});
  if(r.error)alert(r.error);goals();detail();}
async function resolve(f){const a=document.getElementById('a_'+f).value;
  await post('/resolve',{root:sel,feature:f,answer:a||'resolved'});detail();}
function showFleet(f){document.getElementById('slots').value=f.local_slots;
  document.getElementById('global').value=f.global_agents;
  document.getElementById('maxg').value=f.max_goals;}
async function fleet(){showFleet(await j('/api/fleet'));}
async function setFleet(k,v){showFleet(await post('/fleet',{[k]:+v||0}));}
async function addProject(){const p=document.getElementById('addp').value.trim();
  if(!p)return;const r=await post('/add',{path:p});
  if(r.error){alert(r.error);return}
  document.getElementById('addp').value='';goals();}
async function runAll(){const r=await post('/run_all',{});
  alert(r.started.length?'started '+r.started.length+' goal(s)':
    'nothing to start — no approved, idle goals (or the cap is full)');
  goals();detail();}

fleet();goals();detail();
setInterval(()=>{goals();if(sel&&tab!=='live')detail();},5000);
</script></body></html>"""


def demo() -> None:
    """Self-check: _goal_detail derives tab state from a hand-written ledger."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "plexus.toml").write_text(
            '[goal]\nid="g1"\ntext="t"\n[ground_truth]\nsuite="true"\n')
        (root / ".plexus").mkdir()
        recs = [
            {"kind": "plan.created", "goal_id": "g1", "ts": "2026-01-01T00:00:00+00:00",
             "features": [{"feature_id": "f1", "title": "one", "acceptance": "true"}]},
            {"kind": "plan.approved", "goal_id": "g1", "plan_id": "p1",
             "ts": "2026-01-01T00:01:00+00:00"},
            {"kind": "feature.landed", "goal_id": "g1", "feature_id": "f1",
             "attempt": 1, "ts": "2026-01-01T00:02:00+00:00"},
            {"kind": "escalation.raised", "goal_id": "g1", "feature_id": "f2",
             "reason_class": "blocked_on_decision", "reason": "which db?",
             "ts": "2026-01-01T00:03:00+00:00"},
        ]
        (root / ".plexus" / "plan.jsonl").write_text(
            json.dumps({"plan_id": "p1", "id": "f1", "title": "one",
                        "spec": "s", "acceptance": "true"}) + "\n")
        (root / ".plexus" / "ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n")

        det = _goal_detail(root)
        assert det["goal_id"] == "g1", det["goal_id"]
        assert len(_project_id(root)) == 16
        assert det["approved"] is True
        assert det["features"][0]["state"] == "landed", det["features"]
        assert len(det["escalations"]) == 1, det["escalations"]
        assert det["escalations"][0]["feature_id"] == "f2"
        assert det["lifecycle"]["state"] == "blocked"
        assert _scan_roots(root) == [root.resolve()]

        # goal workspace: an unplanned spec is a draft (not "no goals"), can be
        # edited, and approval is scoped to the exact current plan.
        draft_root = root / "draft"
        draft_root.mkdir()
        (draft_root / "plexus.toml").write_text(
            '[goal]\nid="my-goal"\ntext="placeholder"\n'
            '[ground_truth]\nsuite="true"\n')
        assert _goal_lifecycle(draft_root)["state"] == "intake"
        # A real spec reads as a draft, and manual checks survive from either
        # spelling: [ground_truth].manual is where they live now, and
        # [scope].manual_checks is where an existing repo still has them.
        (draft_root / "plexus.toml").write_text(
            '[goal]\nid="real-goal"\ntext="Build the real thing"\n'
            '[ground_truth]\nsuite="python3 -m pytest -q"\nmanual=["click it"]\n'
            '[agent]\nname="codex"\npipeline=true\n')
        saved = load_spec(draft_root)
        assert (saved.goal_id == "real-goal" and saved.pipeline is True
                and saved.manual_checks == ("click it",)), saved
        assert _goal_lifecycle(draft_root)["state"] == "draft"
        (draft_root / "plexus.toml").write_text(
            '[goal]\nid="real-goal"\ntext="t"\n[ground_truth]\nsuite="true"\n'
            '[scope]\nmanual_checks=["legacy"]\n')
        assert load_spec(draft_root).manual_checks == ("legacy",), "old spelling dropped"
        assert _plan_approved(
            [{"kind": "plan.approved", "goal_id": "real-goal", "plan_id": "old"}],
            "real-goal", "new") is False

        # flock probe: a held lock (any process) reads as running; released -> not.
        # Two open fds conflict under flock even within one process.
        lock = root / ".plexus" / "lock"
        lock.write_text("12345")
        assert _running(root) is False
        held = open(lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _running(root) is True
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
        assert _running(root) is False

        # fleet caps ride into the spawned child's env; both keys, clamped >=0
        env = _run_env(3, 5)
        assert env["HEART_LOCAL_SLOTS"] == "3" and env["HEART_MAX_AGENTS_GLOBAL"] == "5"
        assert _run_env(0)["HEART_LOCAL_SLOTS"] == "0"
        assert _run_env(-5, -2)["HEART_MAX_AGENTS_GLOBAL"] == "0"

        # run-all picks approved + idle goals only, honoring the goal cap
        root2 = root / "sub_g2"
        (root2 / ".plexus").mkdir(parents=True)
        (root2 / ".plexus" / "ledger.jsonl").write_text(
            json.dumps({"kind": "plan.approved", "goal_id": "g2",
                        "plan_id": "p2"}) + "\n")
        (root2 / ".plexus" / "plan.jsonl").write_text(
            json.dumps({"plan_id": "p2", "id": "f1"}) + "\n")
        assert _approved(root) and _approved(root2)
        assert _startable([root, root2], 0) == [root, root2]   # no cap: both idle
        held = open(lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _startable([root, root2], 0) == [root2]         # root running -> skipped
        assert _startable([root, root2], 1) == []              # 1 running fills cap 1
        assert _startable([root, root2], 2) == [root2]         # cap 2 -> one slot free
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

        # menu_roots merges workspace roots with the CLI base, so a project
        # outside the --root parent still lands in the menu (isolated env)
        wsdir = root / "elsewhere"
        (wsdir / "plexus.toml").parent.mkdir(parents=True)
        (wsdir / "plexus.toml").write_text('[goal]\nid="w"\ntext="t"\n[ground_truth]\nsuite="true"\n')
        wsfile = root / "ws.json"
        wsfile.write_text(json.dumps({"roots": [str(wsdir)]}))
        old_ws = os.environ.get("PLEXUS_WORKSPACE")
        os.environ["PLEXUS_WORKSPACE"] = str(wsfile)
        try:
            m = menu_roots(root)
            assert root.resolve() in m and wsdir.resolve() in m, m  # both, deduped
            # grid metadata flows into the goal list: label + pinned round-trip
            from . import registry
            registry.set_project_meta(wsdir, label="scrapers", pinned=True)
            g = next(x for x in _list_goals([wsdir]))
            assert g["label"] == "scrapers" and g["pinned"] is True, g
        finally:
            os.environ.pop("PLEXUS_WORKSPACE", None) if old_ws is None \
                else os.environ.__setitem__("PLEXUS_WORKSPACE", old_ws)

        # live view: spine events filtered to this goal's lineage, both paths
        # (task_id prefix and payload.goal_id), isolated journal
        import heart.events as he
        old_sp = os.environ.get("EVENT_JOURNAL_DIR")
        os.environ["EVENT_JOURNAL_DIR"] = str(root / "livejournal")
        try:
            he.emit("heart", "role.started", task_id="g1-f1-a1", role="implement")
            he.emit("heart", "verify.round", task_id="other-z-a1", passed=True)  # other goal
            he.emit("plexus", "feature.started", goal_id="g1")                   # payload lineage
            live = _live(root)
            kinds = [e["kind"] for e in live]
            assert "role.started" in kinds and "feature.started" in kinds, live
            assert "verify.round" not in kinds, live       # different goal, filtered out
            assert any("role=implement" in e["detail"] for e in live), live
        finally:
            os.environ.pop("EVENT_JOURNAL_DIR", None) if old_sp is None \
                else os.environ.__setitem__("EVENT_JOURNAL_DIR", old_sp)

        # scheduler: goal state, cost rollup, run-window (overnight wrap + daytime)
        assert _goal_run_state(root) == "escalated"   # demo has an open f2 block
        assert _fleet_cost([root], 24) == 0.0         # no costed records in the ledger
        assert _in_window("22:00-08:00", datetime.time(23, 0))
        assert not _in_window("22:00-08:00", datetime.time(12, 0))
        assert _in_window("09:00-17:00", datetime.time(12, 0))
        assert not _in_window("09:00-17:00", datetime.time(20, 0))
        dash = _dashboard([root])
        assert dash["runs"]["projects"] == 1
        assert dash["alerts"][0]["goal_id"] == "g1"
    print("ok")


if __name__ == "__main__":
    demo()
