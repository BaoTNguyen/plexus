"""Review phase: what landed, what the plan said, and which commits you must read.

The whole report is derived from the ledger and git. Nothing here is written by
hand on purpose — a conformance record a human maintains is one more thing that
goes stale, and the point of this surface is to be trustworthy on the day you
did not have time to maintain anything.

Risk class comes from the plan, not the diff, which is what makes it useful: you
know at `plexus approve` which features will need your eyes, before a single
episode burns. Two of the four classes are enforced rather than trusted — a
feature cannot be planned as `mechanical` and then land code, because `touches`
refuses the diff first (see run.py's _stray_paths).
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from . import ledger
from .plan import load_plan, matches as _matches

# Paths where a mistake is unrecoverable, from LEDGER.md's system-of-record
# table: the ledger schema, the spec that keys records to it, and the exports
# marrow trains on. A goal recorded under a wrong schema can never be relabelled,
# so these have no tripwire and never leave human hands. Five entries, kept in
# sync with LEDGER.md by hand — a generated list would just move the staleness.
SPINE = ("src/plexus/ledger.py", "src/plexus/spec.py", "src/plexus/export.py",
         "src/plexus/events.py", "LEDGER.md")

# a feature confined to these ships no behaviour, so nothing can regress
MECHANICAL = ("*.md", "docs/*", "docs/**", "tests/*", "tests/**")

# the sibling-checkout seam heart/plexus share; touching the pin means the
# cross-repo contract moved (see tests/test_heart_api_pin.py)
PIN_TEST = "tests/test_heart_api_pin.py"

CLASSES = ("spine", "boundary", "leaf", "mechanical")


def classify(feat: dict) -> str:
    """Risk class from plan fields alone. No git, no diff, no network."""
    touches = feat.get("touches") or ["**"]  # no allowlist: assume the worst
    contract = [c.lower().strip() for c in feat.get("contract") or []]
    if any(_matches(s, g) for s in SPINE for g in touches) or any(
            c.startswith("ledger kind:") for c in contract):
        return "spine"
    if any(_matches(PIN_TEST, g) for g in touches) or any(
            c.startswith(("plexus ", "plexus.toml key:")) for c in contract):
        return "boundary"
    if all(any(_matches(g, m) for m in MECHANICAL) for g in touches):
        return "mechanical"
    return "leaf"


def _show(repo: str | Path, ref: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "show", ref, *args],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _public_defs(src: str) -> set[str]:
    """Top-level public names. A syntax error means the file is not python we
    can reason about (a template, a fixture); silence beats a false alarm."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not n.name.startswith("_")}


def added_symbols(repo: str | Path, commit: str, paths: list[str]) -> set[str]:
    """Public top-level names this commit introduced, as `module.name`. Compares
    each touched file against its own parent revision, so a name that merely
    moved within a file does not read as new."""
    added: set[str] = set()
    for p in paths:
        if not p.endswith(".py"):
            continue
        after = _public_defs(_show(repo, f"{commit}:{p}"))
        before = _public_defs(_show(repo, f"{commit}~1:{p}"))
        added |= {f"{Path(p).stem}.{n}" for n in after - before}
    return added


def _declared(feat: dict) -> set[str]:
    """Contract entries reduced to bare symbol names: `review.report(x) -> str`
    and `class Foo` both have to match what the AST actually sees."""
    out: set[str] = set()
    for c in feat.get("contract") or []:
        name = c.split("(")[0].strip()
        name = name.removeprefix("class ").strip()
        out.add(name)
        out.add(name.split(".")[-1])
    return out


def rows(spec, root: str | Path = ".", repo: str | Path | None = None) -> list[dict]:
    repo = repo or root
    plan = {f["id"]: f for f in load_plan(root)}
    out: list[dict] = []
    for r in ledger.read(root):
        if r["kind"] != "feature.landed" or r.get("goal_id") != spec.goal_id:
            continue
        feat = plan.get(r["feature_id"], {})
        commit = r.get("commit", "")
        paths = _show(repo, commit, "--name-only", "--format=").split()
        stray = _stray(paths, feat)
        declared = _declared(feat)
        unplanned = sorted(s for s in added_symbols(repo, commit, paths)
                           if s not in declared and s.split(".")[-1] not in declared)
        cls = classify(feat)
        out.append({
            "feature_id": r["feature_id"], "commit": commit, "class": cls,
            "stray_paths": stray, "unplanned_symbols": unplanned,
            # spine and boundary are read because of what they are; leaf and
            # mechanical are read only when the diff broke its own promise
            "verdict": "READ" if cls in ("spine", "boundary")
                       else ("FLAG" if stray or unplanned else "ok"),
        })
    return out


def _stray(paths: list[str], feat: dict) -> list[str]:
    touches = feat.get("touches")
    if not touches:
        return []
    return sorted(p for p in paths if not any(_matches(p, g) for g in touches))


def report(spec, root: str | Path = ".", repo: str | Path | None = None) -> str:
    data = rows(spec, root, repo)
    if not data:
        return "nothing landed yet for this goal"
    lines = []
    for d in data:
        paths = "ok" if not d["stray_paths"] else f"+{len(d['stray_paths'])} stray"
        syms = ("ok" if not d["unplanned_symbols"]
                else "+" + ",".join(d["unplanned_symbols"]))
        lines.append(f"{d['class']:<11}{d['feature_id']:<22}{d['commit'][:7]:<9}"
                     f"paths {paths:<12}symbols {syms:<28}{d['verdict']}")
    need = sum(1 for d in data if d["verdict"] != "ok")
    lines.append(f"\n{need} of {len(data)} landed features need your eyes.")
    for d in data:
        for p in d["stray_paths"]:
            lines.append(f"  {d['feature_id']}: unplanned path {p}")
    return "\n".join(lines)


def preview(root: str | Path = ".") -> str:
    """Classes for a plan that has not run yet — the approve-time half of the
    surface, and the only one that can still change the outcome cheaply."""
    plan = load_plan(root)
    lines = [f"{classify(f):<11}{f['id']:<22}{f['title'][:44]}" for f in plan]
    heavy = sum(1 for f in plan if classify(f) in ("spine", "boundary"))
    lines.append(f"\n{heavy} of {len(plan)} features will need line-by-line review.")
    return "\n".join(lines)
