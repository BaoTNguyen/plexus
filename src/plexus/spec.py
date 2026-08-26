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
# pipeline = true # build each feature with heart's implement/test/review roles
#                 # instead of one solo turn; a reviewer REJECT blocks the land

[review]
hold = ["spine", "boundary"]
# risk classes that escalate for your sign-off instead of auto-landing, even
# when acceptance is green — the software-factory boundary. leaf/mechanical
# features still land unattended; the classes you list here wait for you.
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
    # Risk classes that wait for a human sign-off before landing even when
    # acceptance is green. On by default for the two classes that can break
    # something no test covers: an unattended factory whose riskiest commits
    # land unread is not a factory, it is a liability.
    review_hold: tuple[str, ...] = ("spine", "boundary")
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
        pipeline=bool(agent.get("pipeline", False)),
        timeout=int(agent.get("timeout", 300)),
        spec_hash=hashlib.sha256(raw).hexdigest()[:12],
        review_hold=tuple(review.get("hold", ("spine", "boundary"))),
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


def init(root: str | Path = ".") -> Path:
    p = spec_path(root)
    if p.exists():
        raise SystemExit(f"{p} already exists")
    p.write_text(TEMPLATE)
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
        '[agent]\nname = "claude"\ntimeout = 300\n')
    _exclude_plexus_state(root)
    return True
