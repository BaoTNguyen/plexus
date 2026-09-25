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
import fnmatch
import re
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

# Files that run on the operator's machine without anyone choosing to run the
# change: agent and editor hooks, CI, pre-commit, direnv, pytest's bootstrap,
# install-time code in dependency manifests and lockfiles, interpreter startup.
# The sandbox contains an agent while it works; it cannot contain a diff that
# lands a SessionStart hook or a conftest.py, which then runs on the host the
# next time you open the repo or type `pytest`. A dependency an agent found on
# the web is the same shape, one `pip install` later. So these are read off the
# diff at land time -- not the plan, which is the agent's own prediction -- and
# land only with a sign-off. Directory globs for trees, basenames for files
# that execute wherever they sit.
EXEC_DIRS = (".claude/**", ".arteries/**", ".codex/**", ".github/**", ".vscode/**",
             ".devcontainer/**", ".idea/**", ".husky/**")
EXEC_NAMES = ("conftest.py", "setup.py", "setup.cfg", "pyproject.toml",
              "requirements*.txt", "uv.lock", "poetry.lock", "Pipfile*",
              "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
              ".npmrc", ".pre-commit-config.yaml", ".gitlab-ci.yml", ".envrc",
              "Makefile", "*.pth", "sitecustomize.py", "usercustomize.py")

CLASSES = ("spine", "boundary", "exec", "suspect", "leaf", "mechanical")


# Capabilities a backdoor needs and ordinary feature work rarely adds: a way
# out, a way to run something, a way to hide what it runs. Regexes over added
# code lines, so a string that merely mentions one can misfire -- acceptable
# for a gate whose failure mode is "a human looks", not "the work is lost".
_SUSPECT = (
    ("network", re.compile(
        r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib3|socket|smtplib|ftplib|"
        r"paramiko|websockets?)\b|urllib\.request|from\s+urllib\s+import\s+request|"
        r"http\.client|socket\.(socket|create_connection)\(|\bfetch\(|\baxios\b|"
        r"XMLHttpRequest|new WebSocket\(|\b(curl|wget|nc|ncat)\s+-", re.M)),
    ("process", re.compile(
        r"\b(subprocess|os\.system|os\.popen|os\.exec[lv]p?e?|pty\.spawn|child_process|"
        r"execSync|spawnSync)\b")),
    ("dynamic code", re.compile(
        r"(?<![\w.])(eval|exec|compile)\s*\(|__import__\s*\(|importlib\.import_module|"
        r"\bnew Function\(")),
    ("deserialisation", re.compile(r"\b(pickle|marshal|dill|shelve)\.loads?\b")),
    ("decoding", re.compile(r"\bb(64|32|16|85)decode\b|\batob\(|\bcodecs\.decode\b")),
    ("credential read", re.compile(
        r"\.ssh/|\.aws/|\.config/gcloud|\.credentials\.json|\bauth\.json|\.netrc|"
        r"\.docker/config\.json|\.kube/config|\.gnupg/")),
)
# a long unbroken base64/hex run is data someone did not want read
_BLOB = re.compile(r"[A-Za-z0-9+/]{160,}={0,2}|(?:\\x[0-9a-fA-F]{2}){24,}")
# a dotted name with a real TLD: `http://egress:8888` is a container, not a host
_URL = re.compile(r"\b(?:https?|wss?)://((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org"}
# Code is what runs. Prose, lockfiles and data mention hosts and modules all day;
# the manifests and lockfiles that matter are exec_surface's business.
_CODE = (".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash",
         ".zsh", ".rb", ".go", ".rs", ".php", ".pl", ".ps1", ".lua")
_COMMENT = ("#", "//", "/*", "*", "--", "<!--")
_TEST = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")


def _baseline(repo: str | Path, base: str, path: str) -> str:
    """What `path` could already do at the base commit: the file itself, or,
    for a new file, its directory's code -- a new module in a package that
    shells out everywhere is not the package's first shell-out."""
    if (before := _show(repo, f"{base}:{path}")):
        return before
    folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
    names = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", base, folder],
                           capture_output=True, text=True).stdout.split()
    return "\n".join(_show(repo, f"{base}:{n}") for n in names if n.endswith(_CODE))[:400_000]


def suspicious(repo: str | Path, base: str, diff: str) -> list[str]:
    """What a diff newly lets the code do that a backdoor would need.

    Deterministic on purpose: the pipeline reviewer is a model reading a diff
    an agent wrote, and a diff can carry instructions for its reviewer. A regex
    cannot be talked out of what it matches.

    *Newly* is the noise filter. A capability counts only when the added code
    uses it and the code it lands beside did not (_baseline) -- more subprocess
    calls in a module that already shells out are ordinary work; the first one
    in a date parser is a question. A URL host counts when it is new and the
    file can reach the network at all -- otherwise it is a docs link in a
    string. A long encoded blob counts wherever it appears. Tests may start
    processes and touch the network without comment: they mock and spawn as a
    matter of course. Measured over the last 40 commits of heart, plexus,
    arteries and capillaries before settling on this.
    """
    added: dict[str, list[str]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None
            if current and not current.endswith(_CODE):
                current = None
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            if not line[1:].lstrip().startswith(_COMMENT):
                added.setdefault(current, []).append(line[1:])
    found = []
    for path, lines in added.items():
        before = _baseline(repo, base, path) if base else ""
        text = "\n".join(lines)
        for name, pattern in _SUSPECT:
            if name in ("process", "network") and _TEST.search(path):
                continue
            if pattern.search(text) and not pattern.search(before):
                hit = next(l for l in lines if pattern.search(l))
                found.append(f"{path}: new {name}: {hit.strip()[:80]}")
        network = _SUSPECT[0][1]
        if network.search(text) or network.search(before):
            known = set(_URL.findall(before)) | _LOCAL_HOSTS
            for host in sorted(set(_URL.findall(text)) - known):
                found.append(f"{path}: new host {host}")
        if _BLOB.search(text):
            found.append(f"{path}: encoded blob")
    return found


def exec_surface(paths: list[str]) -> list[str]:
    """The paths in a diff that would execute on the operator's machine."""
    return [p for p in paths
            if any(_matches(p, g) for g in EXEC_DIRS)
            or any(fnmatch.fnmatch(p.rsplit("/", 1)[-1], n) for n in EXEC_NAMES)]


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


def _symbol_delta(repo: str | Path, commit: str,
                  paths: list[str]) -> tuple[set[str], set[str]]:
    """(added, removed) public top-level names, as `module.name`. Compares each
    touched file against its own parent revision, so a name that merely moved
    within a file reads as neither."""
    added: set[str] = set()
    removed: set[str] = set()
    for p in paths:
        if not p.endswith(".py"):
            continue
        after = _public_defs(_show(repo, f"{commit}:{p}"))
        before = _public_defs(_show(repo, f"{commit}~1:{p}"))
        added |= {f"{Path(p).stem}.{n}" for n in after - before}
        removed |= {f"{Path(p).stem}.{n}" for n in before - after}
    return added, removed


def added_symbols(repo: str | Path, commit: str, paths: list[str]) -> set[str]:
    """Public top-level names this commit introduced."""
    return _symbol_delta(repo, commit, paths)[0]


def removed_symbols(repo: str | Path, commit: str, paths: list[str]) -> set[str]:
    """Public top-level names this commit deleted or renamed away.

    The mirror of `added_symbols`, and the more urgent half: an unplanned export
    is clutter, an unplanned deletion is a caller somewhere that no longer
    resolves. In this stack that caller is often in another repo — plexus imports
    heart by name — so a removal nothing declared is the failure mode the pin
    test exists to catch, seen one step earlier."""
    return _symbol_delta(repo, commit, paths)[1]


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
        added, gone = _symbol_delta(repo, commit, paths)
        undeclared = lambda ss: sorted(
            s for s in ss
            if s not in declared and s.split(".")[-1] not in declared)
        unplanned, dropped = undeclared(added), undeclared(gone)
        cls = classify(feat)
        out.append({
            "feature_id": r["feature_id"], "commit": commit, "class": cls,
            "stray_paths": stray, "unplanned_symbols": unplanned,
            "removed_symbols": dropped,
            # spine and boundary are read because of what they are; leaf and
            # mechanical are read only when the diff broke its own promise
            "verdict": "READ" if cls in ("spine", "boundary")
                       else ("FLAG" if stray or unplanned or dropped else "ok"),
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
        syms = ", ".join(["+" + s for s in d["unplanned_symbols"]]
                         + ["-" + s for s in d["removed_symbols"]]) or "ok"
        lines.append(f"{d['class']:<11}{d['feature_id']:<22}{d['commit'][:7]:<9}"
                     f"paths {paths:<12}symbols {syms:<28}{d['verdict']}")
    need = sum(1 for d in data if d["verdict"] != "ok")
    lines.append(f"\n{need} of {len(data)} landed features need your eyes.")
    for d in data:
        for p in d["stray_paths"]:
            lines.append(f"  {d['feature_id']}: unplanned path {p}")
        for s in d["removed_symbols"]:
            lines.append(f"  {d['feature_id']}: removed public {s} "
                         f"— check who called it")
    return "\n".join(lines)


def preview(root: str | Path = ".") -> str:
    """Classes for a plan that has not run yet — the approve-time half of the
    surface, and the only one that can still change the outcome cheaply."""
    plan = load_plan(root)
    lines = [f"{classify(f):<11}{f['id']:<22}{f['title'][:44]}" for f in plan]
    heavy = sum(1 for f in plan if classify(f) in ("spine", "boundary"))
    lines.append(f"\n{heavy} of {len(plan)} features will need line-by-line review.")
    return "\n".join(lines)
