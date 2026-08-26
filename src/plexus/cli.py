"""plexus CLI: the loop (init/plan/approve/run) plus the observability read
side (status/insights/stack/tail), which observes the whole stack."""
from __future__ import annotations

import argparse
import os
import sys



def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="plexus")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scaffold plexus.toml in the target repo")
    s.add_argument("--root", default=".")

    s = sub.add_parser("plan", help="plan a task into an ordered feature list")
    s.add_argument("--root", default=".")
    s.add_argument("--task", default="", help="task id; omit to plan the project itself")

    s = sub.add_parser("approve", help="sign off the plan; arms the run loop")
    s.add_argument("--root", default=".")
    s.add_argument("--task", default="", help="task whose plan to approve")
    s.add_argument("--waive", action="store_true",
                   help="approve despite criteria that don't fail on the base commit")

    s = sub.add_parser("run", help="walk the approved plan; exit 0 progressed/done, 1 escalated")
    s.add_argument("--root", default=".")
    s.add_argument("--candidates", type=int, default=1, help="best-of-N per attempt")
    s.add_argument("--task", default="",
                   help="task to run; omit to take the next ready one off the queue")

    s = sub.add_parser("amend", help="fix a not-yet-landed feature's criterion/spec in the plan")
    s.add_argument("feature", help="feature id to amend")
    s.add_argument("--root", default=".")
    s.add_argument("--acceptance", default=None, help="new acceptance command")
    s.add_argument("--spec", default=None, dest="spec_text", help="new feature spec")
    s.add_argument("--title", default=None, help="new title")
    s.add_argument("--touches", default=None,
                   help="new path allowlist, comma-separated globs")

    s = sub.add_parser("review", help="plan-vs-landed conformance: which commits you must read")
    s.add_argument("--root", default=".")
    s.add_argument("--plan", action="store_true",
                   help="classify the plan before it runs, instead of what landed")

    s = sub.add_parser("status", help="symptom check; exit 0 ok, 1 escalations, 2 stalled")
    s.add_argument("--root", default=".")
    s.add_argument("--stale-minutes", type=float, default=30)

    s = sub.add_parser("insights", help="goal-level metrics from the ledger")
    s.add_argument("--root", default=".")

    s = sub.add_parser("why", help="what went wrong per feature: intent/logs/traces/bugs")
    s.add_argument("feature", nargs="?", default=None, help="feature id; omit for all failures")
    s.add_argument("--root", default=".")

    s = sub.add_parser("resolve", help="answer a blocked feature / clear an escalation, then resume")
    s.add_argument("feature", help="feature id with an open escalation")
    s.add_argument("answer", nargs="?", default="resolved",
                   help="the decision (injected into the next attempt for a block)")
    s.add_argument("--root", default=".")

    s = sub.add_parser("export", help="labels.jsonl for marrow: acceptance joined to heart reward")
    s.add_argument("--root", default=".")
    s.add_argument("-o", "--out", default=None, help="default .plexus/labels.jsonl")

    s = sub.add_parser("prune", help="drop old episode dumps that nothing references")
    s.add_argument("--root", default=".")
    s.add_argument("--days", type=float, default=14)
    s.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    s.add_argument("--force", action="store_true",
                   help="delete even episodes not yet exported to labels.jsonl")

    s = sub.add_parser("add", help="register a project directory in the menu "
                                   "(repo or a parent of several) — like 'Add Folder'")
    s.add_argument("path", help="path to the project or parent directory")

    s = sub.add_parser("report", help="fleet digest: spend / lead time / escalation "
                                      "rate per goal across the menu")
    s.add_argument("--root", default=".", help="a goal repo, or a parent of several")

    s = sub.add_parser("stack", help="factory-wide event rollup by source")
    s.add_argument("--hours", type=float, default=24)

    s = sub.add_parser("tail", help="live spine tail (delegates to heart's pulse)")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--source", default=None)
    s.add_argument("--no-follow", action="store_true")

    s = sub.add_parser("serve", help="local control-plane dashboard (plan/run/activity/escalations)")
    s.add_argument("--root", default=".", help="a goal repo, or a parent of several")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--local-slots", type=int,
                   default=int(os.environ.get("HEART_LOCAL_SLOTS", "0") or 0),
                   help="fleet cap on concurrent agents hitting the local model "
                        "server (0=off); adjustable live from the dashboard")
    s.add_argument("--global-agents", type=int,
                   default=int(os.environ.get("HEART_MAX_AGENTS_GLOBAL", "0") or 0),
                   help="fleet cap on ALL concurrent agents across goals (0=off)")
    s.add_argument("--max-goals", type=int, default=0,
                   help="how many goals 'Run all' launches at once (0=no cap)")

    f = sub.add_parser("fleet", help="headless scheduler: advance approved goals "
                                     "unattended (drive from a systemd timer or cron)")
    f.add_argument("action", nargs="?", default="run", choices=["run"])
    f.add_argument("--root", default=".", help="a goal repo, or a parent of several")
    f.add_argument("--max-goals", type=int, default=3,
                   help="max goals advanced concurrently per invocation (default 3)")
    f.add_argument("--cost-ceiling", type=float, default=0.0,
                   help="pause launching when non-local spend in the window hits this "
                        "many dollars (0=off); subscription seats count at API rates")
    f.add_argument("--cost-window-hours", type=float, default=24.0,
                   help="rolling window the cost ceiling measures over (default 24h)")
    f.add_argument("--run-window", default=None,
                   help="only launch inside this local-time window, e.g. 22:00-08:00")
    f.add_argument("--local-slots", type=int,
                   default=int(os.environ.get("HEART_LOCAL_SLOTS", "0") or 0),
                   help="per-GPU cap on local-model agents, stamped into each run")
    f.add_argument("--global-agents", type=int,
                   default=int(os.environ.get("HEART_MAX_AGENTS_GLOBAL", "0") or 0),
                   help="cap on ALL agents across goals, stamped into each run")

    args = p.parse_args(argv)
    if args.cmd == "init":
        from .spec import init, install_integration
        path = init(args.root)
        print(install_integration(args.root))
        print(f"wrote {path} — edit it, then `plexus plan`")
        return 0
    if args.cmd == "plan":
        from .plan import make_plan
        from .spec import load_spec
        from .review import classify
        feats = make_plan(load_spec(args.root), args.root, task_id=args.task)
        scope = f" for {args.task}" if args.task else ""
        print(f"planned {len(feats)} feature(s){scope}; review then `plexus approve`")
        for f in feats:
            # the class is the review budget, and this is the last moment it can
            # be changed for free — narrow a `touches` list here, not after
            print(f"  [{classify(f)}] {f['id']}: {f['title']}")
        return 0
    if args.cmd == "approve":
        from .plan import approve
        from .spec import load_spec
        print(f"approved {approve(load_spec(args.root), args.root, waive=args.waive, task_id=args.task)}")
        return 0
    if args.cmd == "run":
        from .run import run
        from .spec import load_spec
        return run(load_spec(args.root), args.root, candidates=args.candidates,
                   task_id=args.task)
    if args.cmd == "amend":
        from .plan import amend
        from .spec import load_spec
        touches = ([t.strip() for t in args.touches.split(",") if t.strip()]
                   if args.touches is not None else None)
        print(amend(load_spec(args.root), args.feature, args.root,
                    acceptance=args.acceptance, spec_text=args.spec_text,
                    title=args.title, touches=touches))
        return 0
    if args.cmd == "review":
        from . import review
        from .spec import load_spec
        print(review.preview(args.root) if args.plan
              else review.report(load_spec(args.root), args.root))
        return 0
    if args.cmd == "status":
        from . import observe
        lines, code = observe.status(args.root, stale_minutes=args.stale_minutes)
        print("\n".join(lines))
        return code
    if args.cmd == "insights":
        from . import observe
        print("\n".join(observe.insights(args.root)))
        return 0
    if args.cmd == "why":
        from . import diagnose
        print("\n".join(diagnose.why(args.root, args.feature)))
        return 0
    if args.cmd == "resolve":
        from . import ledger
        from .serve import _open_escalations
        from .spec import load_spec
        goal_id = load_spec(args.root).goal_id
        # guard against a typo'd feature id silently recording a resolution for a
        # block that isn't open — which leaves the real escalation in place and
        # the run still paused (found in the end-to-end shakeout).
        open_ids = {e["feature_id"] for e in _open_escalations(ledger.read(args.root), goal_id)}
        if args.feature not in open_ids:
            print(f"no open escalation for {args.feature!r} in {goal_id}."
                  + (" Open: " + ", ".join(sorted(open_ids)) if open_ids
                     else " Nothing is blocked."))
            return 1
        ledger.record("escalation.resolved", goal_id=goal_id, feature_id=args.feature,
                      root=args.root, resolution=args.answer)
        print(f"resolved {args.feature}; re-run `plexus run` to resume")
        return 0
    if args.cmd == "export":
        from .export import export
        path, rows, counts = export(args.root, args.out)
        print(f"wrote {path}: {rows} row(s)")
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {label:20} {n}")
        return 0
    if args.cmd == "prune":
        from .prune import prune
        print("\n".join(prune(args.root, days=args.days, apply=args.apply, force=args.force)))
        return 0
    if args.cmd == "add":
        from pathlib import Path
        from . import registry
        from .serve import _scan_roots
        p = registry.add_workspace_root(args.path)
        pkg = registry.derive_package(p)
        goals = len(_scan_roots(p))
        print(f"added {p} to the workspace"
              + (f" (package '{pkg}' -> registry)" if pkg else "")
              + (f"; {goals} goal(s) found" if goals else
                 "; no plexus.toml yet — `plexus init` there to add a goal"))
        return 0
    if args.cmd == "report":
        from pathlib import Path
        from . import observe
        from .serve import menu_roots
        print("\n".join(observe.report(menu_roots(Path(args.root)))))
        return 0
    if args.cmd == "stack":
        from . import observe
        print("\n".join(observe.stack(args.hours)))
        return 0
    if args.cmd == "tail":
        from heart.pulse import tail
        tail(n=args.n, source=args.source, follow=not args.no_follow)
        return 0
    if args.cmd == "serve":
        from .serve import serve
        return serve(args.root, args.port, args.local_slots,
                     args.global_agents, args.max_goals)
    if args.cmd == "fleet":
        from .serve import fleet_run
        return fleet_run(args.root, args.max_goals, args.cost_ceiling,
                         args.cost_window_hours, args.local_slots,
                         args.global_agents, args.run_window)
    return 2


if __name__ == "__main__":
    sys.exit(main())
