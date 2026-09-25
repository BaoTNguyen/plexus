"""Goal spec: plexus.toml in the target repo's root.

[ground_truth].suite is the scope-level definition of done; per-feature
acceptance commands come from the plan. spec_hash keys ledger records to the
exact spec version (see LEDGER.md).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE = '''\
[goal]
id = "my-goal"
text = """What the product should do, in prose."""
context = """Repo layout, constraints, anything the planner needs."""

[ground_truth]
suite = "python3 -m pytest -q"   # the executable definition of done
manual = []                      # claims no suite can make; you confirm these
# Everything the planner needs beyond this — problem, behaviour, architecture,
# program design, build order — is Markdown under .plexus/overview/. It lives
# there rather than here because architecture is a diagram and program design is
# a signature, and a TOML list of strings can hold neither.

[notify]
# cmd = "notify-send plexus \"$PLEXUS_REASON_CLASS: $PLEXUS_FEATURE\""
# fired on escalation.raised; PLEXUS_KIND/GOAL/FEATURE/REASON_CLASS/REASON in env

[budgets]
attempts_per_feature = 3
episodes_per_goal = 25

[agent]
name = "claude"   # any heart agent: claude|codex|gemini|opencode|api[:profile]|shell
timeout = 300
# cmd = "..."     # custom agent template, prompt in $HEART_PROMPT (overrides name)
# network = "web" # under HEART_SANDBOX: web (public internet, filtered) | api | model
# pipeline = true # build each feature with heart's implement/test/review roles
#                 # (default: on when network is "web", off otherwise)
#                 # instead of one solo turn; a reviewer REJECT blocks the land
# orchestrate = true
#                 # let heart split each feature into a dependency graph of
#                 # workers built in waves and merged with git, falling back to a
#                 # single build when the feature will not split. Costs one extra
#                 # planning call per feature; earns it back when features are
#                 # large enough to have independent parts.

[review]
hold = ["spine", "boundary", "exec", "suspect"]
# risk classes that escalate for your sign-off instead of auto-landing, even
# when acceptance is green — the software-factory boundary. exec is read off
# the diff: hooks, CI, manifests, conftest.py — files that run on your machine.
# suspect is too: a diff that newly adds network calls, processes, dynamic
# code, decoding, credential reads, unseen URL hosts or encoded blobs.
# leaf/mechanical features still land unattended; the classes you list here
# wait for you.
pr_base = "main"
# when the goal finishes green, push the branch and open a PR into this base,
# with `plexus review` as the body. Set to "" to land locally and never push.
'''


@dataclass
class GoalSpec:
    goal_id: str
    text: str
    context: str
    suite: str
    attempts_per_feature: int
    episodes_per_goal: int
    agent: str
    agent_cmd: str | None
    timeout: int
    spec_hash: str
    pipeline: bool = False  # implement/test/review roles instead of a solo turn
    # Let heart decompose each feature into a dependency graph of workers (its
    # Path B) instead of building it in one worktree. The two DAGs are separate
    # scales of the same idea and both stay: plexus orders the features of a
    # goal and walks them one at a time, and heart may order the subtasks
    # *within* whichever feature it was handed. A serial feature plan is not a
    # reason for serial subtasks.
    orchestrate: bool = False
    # Which heart network the goal's agent turns get under HEART_SANDBOX: "web"
    # (any public host via the egress-web proxy, which refuses private addresses
    # and filters DNS), "api" (the vendor allowlist only) or "model" (the local
    # model only). Web by default because build agents are expected to look
    # things up; a goal that never should can say "api". Verifiers and
    # acceptance checks get no network whatever this says.
    network: str = "web"
    # Risk classes that wait for a human sign-off before landing even when
    # acceptance is green. On by default for the classes that can break
    # something no test covers, or run on your machine once landed: an
    # unattended factory whose riskiest commits land unread is not a factory,
    # it is a liability.
    review_hold: tuple[str, ...] = ("spine", "boundary", "exec", "suspect")
    pr_base: str = "main"  # "" disables pushing/PR-opening entirely
    # The one human-judgement field that is not prose: claims no suite can
    # make, which gate delivery until you confirm them. Structured because
    # run.py records them and validation checks them off one by one.
    manual_checks: tuple[str, ...] = field(default_factory=tuple)


def spec_path(root: str | Path = ".") -> Path:
    return Path(root) / "plexus.toml"


def load_spec(root: str | Path = ".") -> GoalSpec:
    raw = spec_path(root).read_bytes()
    data = tomllib.loads(raw.decode())
    goal, gt = data["goal"], data["ground_truth"]
    budgets, agent = data.get("budgets", {}), data.get("agent", {})
    review = data.get("review", {})
    return GoalSpec(
        goal_id=goal["id"],
        text=goal["text"],
        context=goal.get("context", ""),
        suite=gt["suite"],
        attempts_per_feature=int(budgets.get("attempts_per_feature", 3)),
        episodes_per_goal=int(budgets.get("episodes_per_goal", 25)),
        agent=agent.get("name", "claude"),
        agent_cmd=agent.get("cmd"),
        # a web-lane goal gets a reviewer unless it says otherwise: its
        # implementer read pages nobody vetted
        pipeline=bool(agent.get("pipeline", agent.get("network", "web") == "web")),
        orchestrate=bool(agent.get("orchestrate", False)),
        timeout=int(agent.get("timeout", 300)),
        network=str(agent.get("network", "web")),
        spec_hash=hashlib.sha256(raw).hexdigest()[:12],
        review_hold=tuple(review.get("hold", ("spine", "boundary", "exec", "suspect"))),
        pr_base=review.get("pr_base", "main"),
        # `[scope].manual_checks` is where these lived before the overview split
        # the prose out; read it so an existing repo keeps its checks.
        manual_checks=tuple(gt.get("manual")
                            or data.get("scope", {}).get("manual_checks", ())),
    )


def install_integration(root: str | Path = ".") -> str:
    """Wire arteries (and through it capillaries) into the goal repo.

    The stack is opt-in per repo: heart copies `.arteries` and
    `.claude/settings.local.json` into every worktree, so a repo missing them
    runs episodes with memory and retrieval silent — and the goal still finishes
    green, which is why this has to happen at init rather than be noticed later.
    Best effort: arteries may not be installed, and a goal repo without it works.
    """
    if (Path(root) / ".arteries").is_dir():
        return "arteries: already wired"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "arteries.setup_cli", "claude", "--cwd", str(root)],
            capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001 — never block init on the optional layer
        return f"arteries: not wired ({exc}) — episodes will run without memory"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()
        return (f"arteries: not wired ({detail[-1][:120] if detail else 'failed'}) "
                f"— episodes will run without memory/retrieval")
    return "arteries: wired (memory + retrieval active inside episodes)"


def _exclude_plexus_state(root: str | Path) -> None:
    """Keep plexus state out of the goal's git history: the ledger is repo-local
    state, and committing it would dirty the tree on every append. Idempotent."""
    exclude = Path(root) / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        existing = exclude.read_text() if exclude.exists() else ""
        if ".plexus/" not in existing:
            with open(exclude, "a", encoding="utf-8") as f:
                f.write("\n.plexus/\nplexus.toml\nruns/\n")


def default_network(root: str | Path = ".") -> str:
    """The lane a new goal starts on: "web" only for a repo GitHub says is
    public, "api" for everything else.

    On the web lane an agent can send the repo anywhere, which costs nothing
    when the repo is already public and everything when it is not. Unknown --
    no remote, no `gh`, no answer -- is treated as private: the mistake worth
    avoiding is a private repo that starts on the open web because nobody
    thought to change a default. Decided once, here, and written into the
    file, so it is visible and yours to change.
    """
    try:
        r = subprocess.run(["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"],
                           cwd=str(root), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "api"
    return "web" if r.returncode == 0 and r.stdout.strip() == "PUBLIC" else "api"


def init(root: str | Path = ".") -> Path:
    p = spec_path(root)
    if p.exists():
        raise SystemExit(f"{p} already exists")
    net = default_network(root)
    p.write_text(TEMPLATE.replace('# network = "web" #', f'network = "{net}"   #', 1))
    _exclude_plexus_state(root)
    return p


def scaffold_goal(root: str | Path, goal_id: str, text: str, context: str = "") -> bool:
    """Drop a concrete, ready-to-plan plexus.toml into a repo that has none —
    the write side of cross-repo seeding (see registry.py). Never clobbers an
    existing goal: a repo already carrying its own plexus.toml keeps it, and the
    seed lives only as an `upstream.requested` ledger record instead. Returns
    True if a spec was written."""
    p = spec_path(root)
    if p.exists():
        return False
    esc = text.replace('"""', '\\"\\"\\"')
    ctx = f'context = """{context}"""\n' if context else ""
    p.write_text(
        f'[goal]\nid = "{goal_id}"\ntext = """{esc}"""\n{ctx}\n'
        '[source]\nkind = "manual"\nurl = ""\ntitle = ""\nbody = ""\n\n'
        '[scope]\nconfirmed = true\noutcome = ""\nin_scope = []\n'
        'out_of_scope = []\nconstraints = []\nsuccess_criteria = []\n'
        'open_questions = []\nmanual_checks = []\n\n'
        '[ground_truth]\nsuite = "python3 -m pytest -q"\n\n'
        "[budgets]\nattempts_per_feature = 3\nepisodes_per_goal = 25\n\n"
        f'[agent]\nname = "claude"\ntimeout = 300\nnetwork = "{default_network(root)}"\n')
    _exclude_plexus_state(root)
    return True
