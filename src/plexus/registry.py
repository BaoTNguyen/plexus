"""Module -> repo registry for cross-repo goal seeding.

A downstream feature's `needs_upstream` names a symbol it depends on
(`heart.taskspec:TaskSpec`). When that symbol isn't importable yet, run.py
escalates the downstream goal — and, if the registry knows which checkout
provides the module, seeds a goal in that upstream repo so the work gets
queued instead of silently blocking a sibling project.

The map is fleet-level config, the one place that knows which local checkout
owns which top-level package. Default location
`$XDG_CONFIG_HOME/plexus/registry.json` (override with `PLEXUS_REGISTRY`):

    {"heart": "/home/me/Coding/Projects/heart",
     "arteries": "/home/me/Coding/Projects/arteries"}

Best-effort throughout: no registry, no matching entry, or an unwritable repo
just means no seed — the escalation on the downstream side still tells you.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from . import ledger
from .spec import scaffold_goal


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "plexus"


def _registry_path() -> Path:
    return Path(os.environ.get("PLEXUS_REGISTRY") or _config_dir() / "registry.json")


def _workspace_path() -> Path:
    return Path(os.environ.get("PLEXUS_WORKSPACE") or _config_dir() / "workspace.json")


def _overrides() -> dict[str, str]:
    try:
        data = json.loads(_registry_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_ws() -> dict:
    try:
        data = json.loads(_workspace_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ws(data: dict) -> None:
    wp = _workspace_path()
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_text(json.dumps(data, indent=2) + "\n")


def workspace_roots() -> list[Path]:
    """Directories plexus tracks — each a single repo or a parent of several.
    The IDE 'workspace': `plexus add` appends here, and the menu + the derived
    registry both read it. Empty (no file) falls back to registry.json alone,
    so nothing here is required to keep the older hand-edited flow working."""
    return [Path(p).expanduser() for p in _load_ws().get("roots", [])]


def add_workspace_root(path: str | Path) -> Path:
    """Register a project directory in the workspace — the 'Add Folder' move.
    Idempotent; returns the resolved path. Preserves any project metadata."""
    p = Path(path).expanduser().resolve()
    data = _load_ws()
    roots = data.setdefault("roots", [])
    if str(p) not in {str(Path(r).expanduser().resolve()) for r in roots}:
        roots.append(str(p))
        _save_ws(data)
    return p


def project_meta() -> dict[str, dict]:
    """Per-project view state — {resolved_path: {label, pinned}} — the grouping
    and pinning the dashboard grid renders. Empty is the flat, ungrouped menu."""
    return _load_ws().get("projects", {})


def set_project_meta(path: str | Path, **fields) -> dict:
    """Set/clear a project's label or pinned flag (a None value clears the key,
    and an empty metadata dict drops the project entry entirely). Grouping is
    view state, so it lives in workspace.json beside the roots, never in the
    goal repo's own ledger."""
    p = str(Path(path).expanduser().resolve())
    data = _load_ws()
    projects = data.setdefault("projects", {})
    meta = projects.setdefault(p, {})
    for k, v in fields.items():
        if v in (None, ""):
            meta.pop(k, None)
        else:
            meta[k] = v
    if not meta:
        projects.pop(p, None)
    _save_ws(data)
    return meta


# Monthly USD per seat, by the plan slug each CLI already records locally.
# Public list prices, checked 2026-07-31. The two "pro"s are different products
# and different money: Claude Pro is $20, ChatGPT Pro is $200 — which is exactly
# why this belongs in a table and not in a number a human types twice a year.
_SEAT_USD = {
    "claude": {"free": 0.0, "pro": 20.0, "max": 100.0, "max_5x": 100.0,
               "max_20x": 200.0, "team": 30.0, "enterprise": 0.0},
    "codex": {"free": 0.0, "plus": 20.0, "pro": 200.0, "business": 30.0,
              "team": 30.0, "enterprise": 0.0},
}


def detect_subscriptions() -> dict[str, float]:
    """Monthly seat cost per provider, read from what the CLIs already wrote.

    Both tools store the signed-in plan on disk, so the fleet can price a
    subscription turn without anyone retyping it: Claude Code keeps
    `claudeAiOauth.subscriptionType` in ~/.claude/.credentials.json, and Codex
    carries `chatgpt_plan_type` inside the ChatGPT access token in
    ~/.codex/auth.json. Only the plan claim is read — never the token, which
    stays on disk and is not logged, copied or sent anywhere.

    A provider is omitted when it can't be determined: signed out, an API key
    instead of a seat, or a plan slug newer than the table. Omitted means
    unknown, never zero — the caller keeps whatever was configured."""
    import base64
    out: dict[str, float] = {}

    try:
        creds = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
        plan = str((creds.get("claudeAiOauth") or {}).get("subscriptionType", "")).lower()
        if plan in _SEAT_USD["claude"]:
            out["claude"] = _SEAT_USD["claude"][plan]
    except Exception:
        pass

    try:
        auth = json.loads((Path.home() / ".codex" / "auth.json").read_text())
        # an API key is metered per token, not a seat — heart already prices
        # those turns from models.json, so a seat cost would double-count
        if auth.get("auth_mode") == "chatgpt":
            token = (auth.get("tokens") or {}).get("access_token") or ""
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(
                payload + "=" * (-len(payload) % 4)))
            plan = str((claims.get("https://api.openai.com/auth") or {}).get(
                "chatgpt_plan_type", "")).lower()
            if plan in _SEAT_USD["codex"]:
                out["codex"] = _SEAT_USD["codex"][plan]
    except Exception:
        pass

    return out


def accounting_config() -> dict:
    """Fleet cost inputs that providers do not expose in per-turn telemetry.

    A seat cost of 0 means "not set", not "free", so a detected plan fills it
    in. An explicitly saved non-zero number always wins — detection is the
    default, not an override, or a negotiated or grandfathered rate would be
    silently reset to list price on every read."""
    raw = _load_ws().get("accounting", {})
    subscriptions = raw.get("subscriptions", {}) if isinstance(raw, dict) else {}
    pricing = raw.get("pricing", {}) if isinstance(raw, dict) else {}
    detected = detect_subscriptions()
    return {"subscriptions": {
        provider: (max(0.0, float(subscriptions.get(provider, 0)))
                   or detected.get(provider, 0.0))
        for provider in ("claude", "codex")
    }, "detected_subscriptions": detected, "pricing": {
        provider: {
            "input": max(0.0, float((pricing.get(provider) or {}).get("input", 0))),
            "output": max(0.0, float((pricing.get(provider) or {}).get("output", 0))),
            "models": _clean_models((pricing.get(provider) or {}).get("models")),
        } for provider in ("claude", "codex")
    }}


def _clean_models(raw: object) -> dict:
    """Per-model overrides under a provider: {"claude-opus-5": {...}}.

    Optional, and empty for an existing config — a provider with no overrides
    prices exactly as it did before. It exists because one rate per vendor is
    the wrong granularity: arteries reports the model on every turn, and Opus
    and Haiku on the same CLI differ by more than an order of magnitude, so a
    provider-wide rate misprices whichever model you use less.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for model, rates in raw.items():
        if not isinstance(rates, dict):
            continue
        name = str(model).strip()[:120]
        if not name:
            continue
        out[name] = {
            "input": max(0.0, float(rates.get("input", 0) or 0)),
            "output": max(0.0, float(rates.get("output", 0) or 0)),
        }
    return out


def heart_model_rates() -> dict[str, dict[str, float]]:
    """Per-model rates from heart's `models.json`, or {} when unavailable.

    heart owns the verified card (`_note` in that file dates it against the
    vendors' own pricing pages) and prices episodes from it. Reading it here
    rather than copying the numbers is the same call the CACHE_MULTIPLIERS
    import makes: two rate cards in two repos drift silently, and a wrong cost
    is still a plausible number, so nothing ever alerts.
    """
    try:
        from heart.runner import model_pricing
        return model_pricing()
    except Exception:
        return {}


def rates_for(pricing: dict, provider: str, model: str | None,
              model_rates: dict | None = None) -> dict | None:
    """The rate card row to bill a turn against, most specific first:

    1. a per-model override in this workspace — a negotiated or grandfathered
       rate has to beat the published one
    2. heart's verified per-model card, keyed by the model id arteries reports
    3. the provider-wide rate

    Returns None when none of those is usable, so the caller counts the turn as
    unpriced rather than billing it at zero.
    """
    entry = pricing.get(provider) or {}
    if model:
        override = (entry.get("models") or {}).get(str(model))
        if override and (override.get("input") or override.get("output")):
            return override
        card = (heart_model_rates() if model_rates is None else model_rates)
        known = card.get(str(model))
        # a genuinely free local model reports 0/0 and must stay priced at zero,
        # so this tests for presence, not for a truthy rate
        if known is not None:
            return known
    if entry.get("input") or entry.get("output"):
        return entry
    return None


def set_accounting_config(subscriptions: dict, pricing: dict | None = None) -> dict:
    if not isinstance(subscriptions, dict):
        raise ValueError("subscriptions must be an object")
    clean = {provider: max(0.0, float(subscriptions.get(provider, 0)))
             for provider in ("claude", "codex")}
    pricing = pricing or {}
    clean_pricing = {provider: {
        "input": max(0.0, float((pricing.get(provider) or {}).get("input", 0))),
        "output": max(0.0, float((pricing.get(provider) or {}).get("output", 0))),
        "models": _clean_models((pricing.get(provider) or {}).get("models")),
    } for provider in ("claude", "codex")}
    data = _load_ws()
    data["accounting"] = {"subscriptions": clean, "pricing": clean_pricing}
    _save_ws(data)
    return data["accounting"]


def derive_package(repo: str | Path) -> str | None:
    """The top-level import package a repo provides, so the registry wires itself
    from the same pyproject that already declares it. Handles both layouts in
    this stack: a src/<pkg> tree (packages.find where=['src']) and a flat
    packages=['name'] list. None when it can't tell — the explicit registry.json
    is the override for those."""
    repo = Path(repo)
    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    # explicit flat package list (marrow): first top-level entry
    listed = (data.get("tool", {}).get("setuptools", {}) or {}).get("packages")
    if isinstance(listed, list):
        tops = [p for p in listed if isinstance(p, str) and "." not in p]
        if tops:
            return tops[0]
    # src layout: the one importable package dir under src/
    src = repo / "src"
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if (child / "__init__.py").exists():
                return child.name
    # flat layout named after the project
    name = data.get("project", {}).get("name")
    if name and (repo / name / "__init__.py").exists():
        return name
    return None


def _repos_with_pyproject(base: Path) -> list[Path]:
    """A base is a single repo (has pyproject) or a parent of several (one level
    down) — matching how the goal menu expands a root, but keyed on pyproject so
    an upstream repo counts even before it has a goal."""
    base = Path(base)
    if not base.exists():
        return []
    if (base / "pyproject.toml").exists():
        return [base.resolve()]
    return sorted({p.parent.resolve() for p in base.glob("*/pyproject.toml")})


def load_registry(extra_roots: tuple[str | Path, ...] = ()) -> dict[str, str]:
    """package -> repo path. Derived from every repo under the workspace roots
    (zero-maintenance: a new project wires itself from its pyproject), with
    registry.json entries layered on top as explicit overrides that win."""
    reg: dict[str, str] = {}
    for base in list(workspace_roots()) + [Path(p) for p in extra_roots]:
        for repo in _repos_with_pyproject(base):
            pkg = derive_package(repo)
            if pkg:
                reg.setdefault(pkg, str(repo))
    reg.update(_overrides())  # explicit pins override derivation
    return reg


def resolve_repo(spec: str, registry: dict[str, str]) -> Path | None:
    """Longest-prefix module match -> repo path. `spec` is 'module.path' or
    'module.path:Symbol'; a prefix matches the module itself or any dotted
    child, so 'heart' owns 'heart.taskspec' but not 'heartbeat'."""
    mod = spec.partition(":")[0]
    best: tuple[str, str] | None = None
    for prefix, repo in registry.items():
        if mod == prefix or mod.startswith(prefix + "."):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, repo)
    return Path(best[1]) if best else None


def _open_request(recs: list[dict], symbol: str) -> bool:
    """An upstream request for this symbol already stands (requested, not yet
    resolved) — so seeding again would just duplicate it."""
    net = 0
    for r in recs:
        if r.get("symbol") == symbol:
            if r["kind"] == "upstream.requested":
                net += 1
            elif r["kind"] == "upstream.fulfilled":
                net -= 1
    return net > 0


def _goal_id(symbol: str) -> str:
    mod, _, sym = symbol.partition(":")
    return "upstream-" + (sym or mod.split(".")[-1]).lower()


def seed_upstream(missing: list[str], downstream_goal: str,
                  registry: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Write a goal request into each repo that owns a missing upstream symbol.
    Idempotent: an already-open request for the same symbol is not duplicated.
    Returns the (symbol, repo) pairs actually seeded."""
    registry = load_registry() if registry is None else registry
    seeded: list[tuple[str, str]] = []
    for spec in missing:
        repo = resolve_repo(spec, registry)
        if not repo or not repo.is_dir():
            continue
        recs = ledger.read(repo)
        if _open_request(recs, spec):
            continue
        gid = _goal_id(spec)
        # scaffold a plannable goal if the repo carries none; otherwise the
        # request rides only as a ledger record the operator merges into their
        # own plan (we never overwrite a repo's own active goal).
        scaffold_goal(repo, gid,
                      text=f"Add {spec}, required by downstream goal "
                           f"'{downstream_goal}'. Land the public symbol it "
                           f"names so the dependent project can proceed.",
                      context=f"Seeded by plexus: {downstream_goal} declared "
                              f"needs_upstream = {spec!r} but it is not importable.")
        ledger.record("upstream.requested", goal_id=gid, root=repo, symbol=spec,
                      requested_by=downstream_goal,
                      reason=f"{downstream_goal} needs {spec} — land it here")
        seeded.append((spec, str(repo)))
    return seeded


def demo() -> None:
    """Self-check: prefix resolution, idempotent seeding, no-clobber of an
    existing spec.

    A temp root is not enough isolation. `ledger.record` writes twice on
    purpose — the repo-local ledger *and* the shared spine — so a self-check
    that seeds fixture goals leaves `upstream-taskspec` and friends sitting in
    the operator's real journal, where the dashboard reads them as activity. The
    spine has no per-run scoping, so the only lever is the env var, and owning
    it here means the caller cannot forget."""
    import os
    import tempfile
    old_journal = os.environ.get("EVENT_JOURNAL_DIR")
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        os.environ["EVENT_JOURNAL_DIR"] = str(base / "journal")
        up = base / "heart"
        up.mkdir()
        reg = {"heart": str(up), "arteries": str(base / "nope")}

        assert resolve_repo("heart.taskspec:TaskSpec", reg) == up
        assert resolve_repo("heartbeat.x", reg) is None       # prefix, not substring
        assert resolve_repo("unknown.mod", reg) is None

        seeded = seed_upstream(["heart.taskspec:TaskSpec", "arteries.x:Y"], "g-down", reg)
        assert seeded == [("heart.taskspec:TaskSpec", str(up))], seeded  # arteries repo absent
        assert (up / "plexus.toml").exists()                  # goal written into the repo
        recs = ledger.read(up)
        assert any(r["kind"] == "upstream.requested" for r in recs)

        # idempotent: a standing request is not seeded twice
        assert seed_upstream(["heart.taskspec:TaskSpec"], "g-down", reg) == []

        # no-clobber: existing spec stays; only the ledger record is added
        before = (up / "plexus.toml").read_text()
        seed_upstream(["heart.other:Thing"], "g-down2", reg)
        assert (up / "plexus.toml").read_text() == before
        assert any(r.get("symbol") == "heart.other:Thing" for r in ledger.read(up))

        # --- derive_package: both layouts this stack uses ---
        srcrepo = base / "srclay"
        (srcrepo / "src" / "wibble").mkdir(parents=True)
        (srcrepo / "src" / "wibble" / "__init__.py").touch()
        (srcrepo / "pyproject.toml").write_text(
            '[project]\nname="wibble"\n[tool.setuptools.packages.find]\nwhere=["src"]\n')
        assert derive_package(srcrepo) == "wibble", derive_package(srcrepo)

        flatrepo = base / "flatlay"
        (flatrepo / "flatpkg").mkdir(parents=True)
        (flatrepo / "pyproject.toml").write_text(
            '[project]\nname="flatpkg"\n[tool.setuptools]\npackages=["flatpkg"]\n')
        assert derive_package(flatrepo) == "flatpkg", derive_package(flatrepo)
        assert derive_package(base / "nopyproject") is None

        # --- workspace auto-derive: a parent root wires every repo under it,
        #     overrides win, `add` is idempotent ---
        ws = base / "workspace.json"
        reg_over = base / "reg.json"
        reg_over.write_text(json.dumps({"flatpkg": "/pinned/elsewhere"}))
        old_ws = os.environ.get("PLEXUS_WORKSPACE")
        old_reg = os.environ.get("PLEXUS_REGISTRY")
        os.environ["PLEXUS_WORKSPACE"] = str(ws)
        os.environ["PLEXUS_REGISTRY"] = str(reg_over)
        try:
            ws.write_text(json.dumps({"roots": [str(base)]}))
            derived = load_registry()
            assert derived["wibble"] == str(srcrepo), derived     # auto-derived
            assert derived["flatpkg"] == "/pinned/elsewhere"      # override wins
            p = add_workspace_root(flatrepo)
            assert p == flatrepo.resolve()
            add_workspace_root(flatrepo)                          # idempotent
            assert json.loads(ws.read_text())["roots"].count(str(flatrepo.resolve())) == 1

            # project metadata: label/pin set, per-field clear, entry drop
            fr = str(flatrepo.resolve())
            set_project_meta(flatrepo, label="grp-a", pinned=True)
            assert project_meta()[fr] == {"label": "grp-a", "pinned": True}
            set_project_meta(flatrepo, pinned=None)               # clear one field
            assert project_meta()[fr] == {"label": "grp-a"}
            set_project_meta(flatrepo, label="")                  # clear last -> drop
            assert fr not in project_meta()
            # metadata survives a later add (roots + projects don't clobber)
            set_project_meta(srcrepo, label="keep")
            add_workspace_root(base / "another")
            assert project_meta()[str(srcrepo.resolve())]["label"] == "keep"
            saved = set_accounting_config(
                {"claude": 100, "codex": 20},
                {"claude": {"input": 3, "output": 15}})
            assert saved["subscriptions"] == {"claude": 100.0, "codex": 20.0}
            assert accounting_config()["pricing"]["claude"] == {
                "input": 3.0, "output": 15.0, "models": {}}
            # per-model overrides: exact match wins, everything else falls back
            # to the provider rate, so an unconfigured stack prices unchanged
            set_accounting_config(
                {"claude": 100, "codex": 20},
                {"claude": {"input": 3, "output": 15,
                            "models": {"claude-haiku-4-5": {"input": 1, "output": 5}}}})
            pricing = accounting_config()["pricing"]
            card = {"claude-opus-5": {"input": 15.0, "output": 75.0},
                    "qwen3.6-27b": {"input": 0.0, "output": 0.0}}
            # 1. a workspace override beats everything — a negotiated rate has
            #    to win over the published one
            assert rates_for(pricing, "claude", "claude-haiku-4-5", card) == {
                "input": 1.0, "output": 5.0}
            # 2. heart's verified card beats the provider-wide rate
            assert rates_for(pricing, "claude", "claude-opus-5", card)["input"] == 15.0
            # 3. a model in neither falls back to the provider rate, so adding
            #    one model's rate never stops the others from being billed
            assert rates_for(pricing, "claude", "claude-unknown-9", card)["input"] == 3.0
            assert rates_for(pricing, "claude", None, card)["input"] == 3.0
            # a local model is genuinely free: 0/0 must survive as zero rather
            # than falling through to the provider rate for being falsy
            assert rates_for(pricing, "claude", "qwen3.6-27b", card) == {
                "input": 0.0, "output": 0.0}
            # a provider with no rate at all is unpriceable, not free
            assert rates_for({"claude": {"input": 0, "output": 0}}, "claude", "x", {}) is None
        finally:
            for k, v in (("PLEXUS_WORKSPACE", old_ws), ("PLEXUS_REGISTRY", old_reg),
                         ("EVENT_JOURNAL_DIR", old_journal)):
                # restore, never just pop: the test suites scope the journal before
                # importing plexus, and popping would hand the rest of the run
                # the production journal instead of their temp one
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("ok")


if __name__ == "__main__":
    demo()
