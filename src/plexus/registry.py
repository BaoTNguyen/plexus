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

import base64
import json
import os
import secrets
import tempfile
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
    out: dict[str, float] = {}

    try:
        creds = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
        plan = str((creds.get("claudeAiOauth") or {}).get("subscriptionType", "")).lower()
        if plan in _SEAT_USD["claude"]:
            out["claude"] = _SEAT_USD["claude"][plan]
    except Exception:
        pass

    plan = str((codex_claims().get("https://api.openai.com/auth") or {}).get(
        "chatgpt_plan_type", "")).lower()
    if plan in _SEAT_USD["codex"]:
        out["codex"] = _SEAT_USD["codex"][plan]
    return out


def codex_claims() -> dict:
    """The claims of Codex's ChatGPT access token, or {} for an API key, a
    signed-out CLI or anything unreadable. Claims only -- the plan, the
    expiry -- never the token itself, which is not logged, copied or sent.

    An API key is metered per token, not a seat: heart already prices those
    turns from models.json, so a seat reading here would double-count."""
    try:
        auth = json.loads((Path.home() / ".codex" / "auth.json").read_text())
        if auth.get("auth_mode") != "chatgpt":
            return {}
        payload = ((auth.get("tokens") or {}).get("access_token") or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


# The file each signed-in CLI authenticates from. Paths, never contents --
# this is the same fact detect_subscriptions reads a plan claim out of, used a
# second time for a different purpose.
_SEAT_FILES = {
    "claude": ("~/.claude/.credentials.json", "~/.claude.json"),
    "codex": ("~/.codex/auth.json",),
}


# The dev environment a contained agent should share with the host one: user
# settings (model, hooks such as tagore's), global instructions, skills and
# plugins. Read-only in the container, config not credentials, and the reason a
# contained planner still plans on Opus in your house style rather than on the
# account default with no conventions.
_DEV_FILES = ("~/.claude/settings.json", "~/.claude/CLAUDE.md")
_DEV_DIRS = ("~/.claude/skills", "~/.claude/plugins")


def seat_secrets() -> Path:
    """Where seat tokens for the egress proxy's injector live: one file per
    route (`anthropic`), mode 0600, mounted read-only into the proxy and
    nowhere else. Beside heart's models.json because heart's proxy reads it."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return cfg / "heart" / "secrets"


def sentinel_seed() -> Path:
    """This box's sentinel seed, created on first use: 32 random bytes in the
    secrets dir, 0600 in a 0700 directory.

    What an agent holds instead of a seat is derived from it (heart
    sandbox.sentinels), so it is worth something only to a container that was
    handed it. It used to be a constant in heart's source -- anyone reading it
    could use the injector, including a run whose seats were withheld. To
    rotate: delete the file and run `plexus doctor --fix`.
    """
    path = seat_secrets() / "sentinel"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return path


def injected_seats() -> list[str]:
    """Routes whose seat is injected by the proxy rather than mounted.

    anthropic once the operator saves a `claude setup-token` for the proxy;
    chatgpt whenever Codex is signed in with a ChatGPT seat, because its
    auth.json is already the file the proxy reads -- there is nothing to save,
    and the alternative is that file inside every container. The host's own
    `codex` stays the one thing that refreshes it."""
    routes = [r for r in ("anthropic",) if (seat_secrets() / r).is_file()]
    if codex_claims():
        routes.append("chatgpt")
    return routes


def seat_env() -> dict[str, str]:
    """The sandbox credential env for a run: which subscription seats exist on
    this machine, named as paths for heart to mount.

    Why the control plane owns this. Whether a goal may spend a seat is fleet
    policy and belongs here; how a credential reaches a container -- read-only,
    files and never directories, agent roles only, never a verifier -- is
    sandbox mechanics and stays in heart. Plexus decides whether there is
    anything to mount; heart decides what mounting means.

    Paths, not material, and the distinction is the whole design. A control
    plane that reads the token turns it into a string, then an env var, then a
    line in episode.json, `ps` output and the run artifacts -- four durable
    places, none of them credential stores. Worse for OAuth specifically: a
    refresh rotates the token at the provider, so N copies handed down means
    the first sandbox to refresh logs out the other N-1 and your own shell with
    them. One file on disk, one owner, mounted read-only N times.

    Nothing here is invented. A seat that isn't signed in contributes no path,
    and `PLEXUS_SEAT=off` withholds every seat for a run that should have none.
    The dev environment (_DEV_FILES, _DEV_DIRS) is named alongside, and
    `PLEXUS_DEV_ENV=off` withholds it.
    An operator who set HEART_SANDBOX_HOME_FILES already answered the question
    and is left alone.

    Better than a path is no credential at all. A seat token saved under
    seat_secrets() is injected by the egress proxy: the container is handed a
    sentinel and the proxy swaps in the real token on the way out, so that
    seat's files are not mounted. HEART_SANDBOX_INJECT tells heart which seats
    arrive that way.

    API keys are deliberately not handled: heart's HEART_SANDBOX_ENV already
    forwards named ones, and guessing which of a shell's variables is a real
    credential is how a placeholder like ANTHROPIC_API_KEY=x ends up beating a
    working seat inside the container. Name them or don't have them.
    """
    off = ("off", "0", "none")
    seats = os.environ.get("PLEXUS_SEAT", "").strip().lower() not in off
    injected = injected_seats() if seats else []
    env = {}
    if injected:
        sentinel_seed()  # heart derives the stand-ins from it; must exist first
        env["HEART_SANDBOX_INJECT"] = ",".join(injected)
    # the dev environment rides along unless the operator says otherwise --
    # PLEXUS_DEV_ENV=off, or naming the dirs themselves
    dev = os.environ.get("PLEXUS_DEV_ENV", "").strip().lower() not in off
    dirs = [str(Path(d).expanduser()) for d in _DEV_DIRS
            if dev and Path(d).expanduser().is_dir()]
    if dirs and not os.environ.get("HEART_SANDBOX_HOME_DIRS", "").strip():
        env["HEART_SANDBOX_HOME_DIRS"] = ",".join(dirs)
    if os.environ.get("HEART_SANDBOX_HOME_FILES", "").strip():
        return env
    files = [str(Path(f).expanduser()) for f in _DEV_FILES
             if dev and Path(f).expanduser().is_file()]
    if "chatgpt" in injected:
        # the plan is the one true fact the stand-in auth.json carries
        plan = (codex_claims().get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
        if plan:
            env["HEART_SANDBOX_CODEX_PLAN"] = str(plan)
    for provider, group in _SEAT_FILES.items() if seats else ():
        if (provider, True) in (("claude", "anthropic" in injected),
                                ("codex", "chatgpt" in injected)):
            continue  # the proxy holds it; the container gets a sentinel
        for name in group:
            path = Path(name).expanduser()
            # ponytail: ~/.claude.json goes in whole because the CLI wants it
            # there and heart derives the container path from the host one, so a
            # sanitized stub has nowhere to land. It is config and history for
            # every project on the box, not a credential store. An injected
            # seat skips it entirely, which is the real upgrade path.
            if path.is_file():
                files.append(str(path))
    if files:
        env["HEART_SANDBOX_HOME_FILES"] = ",".join(files)
    return env


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
    try:  # lazy: heart may be absent; an empty card is the documented fallback
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
