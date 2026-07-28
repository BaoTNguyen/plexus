"""plexus CLI: the loop (init/plan/approve/run) plus the observability read
side (status/insights/stack/tail), which observes the whole stack."""
from __future__ import annotations

import argparse
import sys



def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="plexus")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scaffold plexus.toml in the target repo")
    s.add_argument("--root", default=".")

    s = sub.add_parser("plan", help="plan the goal into an ordered feature list")
    s.add_argument("--root", default=".")

    s = sub.add_parser("approve", help="sign off the plan; arms the run loop")
    s.add_argument("--root", default=".")
    s.add_argument("--waive", action="store_true",
                   help="approve despite criteria that don't fail on the base commit")

    s = sub.add_parser("run", help="walk the approved plan; exit 0 progressed/done, 1 escalated")
    s.add_argument("--root", default=".")
    s.add_argument("--candidates", type=int, default=1, help="best-of-N per attempt")

    s = sub.add_parser("amend", help="fix a not-yet-landed feature's criterion/spec in the plan")
    s.add_argument("feature", help="feature id to amend")
    s.add_argument("--root", default=".")
    s.add_argument("--acceptance", default=None, help="new acceptance command")
    s.add_argument("--spec", default=None, dest="spec_text", help="new feature spec")
    s.add_argument("--title", default=None, help="new title")

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

    s = sub.add_parser("stack", help="factory-wide event rollup by source")
    s.add_argument("--hours", type=float, default=24)

    s = sub.add_parser("tail", help="live spine tail (delegates to heart's pulse)")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--source", default=None)
    s.add_argument("--no-follow", action="store_true")

    s = sub.add_parser("serve", help="local control-plane dashboard (plan/run/activity/escalations)")
    s.add_argument("--root", default=".", help="a goal repo, or a parent of several")
    s.add_argument("--port", type=int, default=8100)

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
        feats = make_plan(load_spec(args.root), args.root)
        print(f"planned {len(feats)} feature(s); review then `plexus approve`")
        for f in feats:
            print(f"  {f['id']}: {f['title']}")
        return 0
    if args.cmd == "approve":
        from .plan import approve
        from .spec import load_spec
        print(f"approved {approve(load_spec(args.root), args.root, waive=args.waive)}")
        return 0
    if args.cmd == "run":
        from .run import run
        from .spec import load_spec
        return run(load_spec(args.root), args.root, candidates=args.candidates)
    if args.cmd == "amend":
        from .plan import amend
        from .spec import load_spec
        print(amend(load_spec(args.root), args.feature, args.root,
                    acceptance=args.acceptance, spec_text=args.spec_text, title=args.title))
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
        from .spec import load_spec
        goal_id = load_spec(args.root).goal_id
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
        return serve(args.root, args.port)
    return 2


if __name__ == "__main__":
    sys.exit(main())
