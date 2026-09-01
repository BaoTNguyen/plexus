"""Self-check, mirroring heart's tests/test_heart.py style: stdlib asserts,
no frameworks, no network. Run: python3 tests/test_plexus.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="plexus-test-"))
os.environ["EVENT_JOURNAL_DIR"] = str(tmp / "journal")

here = Path(__file__).resolve()
for p in (here.parents[1] / "src", here.parents[2] / "heart" / "src"):
    sys.path.insert(0, str(p))
from plexus import events, ledger, observe  # noqa: E402

root = tmp / "repo"

# --- write order: record() lands in the ledger and on the spine ---
ledger.record("goal.started", goal_id="g1", root=root)
ledger.record("feature.started", goal_id="g1", feature_id="f1", root=root)
ledger.record("feature.failed", goal_id="g1", feature_id="f1", root=root,
              reason="tests failed")
ledger.record("feature.landed", goal_id="g1", feature_id="f1", root=root,
              episode_ids=["ep-1", "ep-2"])
recs = ledger.read(root)
assert [r["kind"] for r in recs] == [
    "goal.started", "feature.started", "feature.failed", "feature.landed"]

journal_files = list((tmp / "journal").glob("*.ndjson"))
assert journal_files, "spine events not written"
journal = [json.loads(l) for l in journal_files[0].read_text().splitlines()]
assert all(e["source"] == "plexus" for e in journal)
assert journal[1]["payload"]["goal_id"] == "g1"
assert journal[1]["payload"]["feature_id"] == "f1"

# --- task_id convention threads goal lineage through heart's events ---
assert events.make_task_id("g1", "f1", 2) == "g1-f1-a2"

# --- status: running goal with recent activity -> 0 ---
lines, code = observe.status(root)
assert code == 0 and any("RUNNING" in l for l in lines), (lines, code)

# --- stalled goal (zero tolerance makes any running goal stale) -> 2 ---
lines, code = observe.status(root, stale_minutes=0)
assert code == 2 and any("STALLED" in l for l in lines), (lines, code)

# --- open escalation -> 1, with the pulse drill-down pointer ---
ledger.record("escalation.raised", goal_id="g1", feature_id="f2", root=root,
              reason="acceptance cannot be made executable",
              episode_ids=["ep-3"])
lines, code = observe.status(root)
assert code == 1 and any("BLOCKED" in l for l in lines), (lines, code)
assert any("pulse episode ep-3" in l for l in lines), lines

# --- resolved + finished -> 0 ---
ledger.record("escalation.resolved", goal_id="g1", feature_id="f2", root=root)
ledger.record("goal.finished", goal_id="g1", root=root, outcome="scope_satisfied")
lines, code = observe.status(root)
assert code == 0 and any("DONE" in l for l in lines), (lines, code)

# --- insights read the ledger: lead time and retry rescue ---
out = observe.insights(root)
assert any("lead time" in l for l in out), out
assert any("rescued=1" in l for l in out), out

# --- torn tail line is skipped, not fatal ---
with open(ledger.ledger_path(root), "a") as f:
    f.write('{"ts": "2026-07-05T00:00:00+00:00", "kind": "goal.st')
assert len(ledger.read(root)) == len(recs) + 3  # escalations + finish, junk skipped

# --- stack rollup sees plexus on the shared journal ---
out = observe.stack(hours=1)
assert any("plexus" in l for l in out), out

# --- diagnose: phase classification + evidence bundle ---
from plexus import diagnose  # noqa: E402

assert diagnose.classify_phase("no_change") == "coding"
assert diagnose.classify_phase("apply_failed") == "coding"
assert diagnose.classify_phase("acceptance_failed") == "testing"  # no pair -> stays testing
assert diagnose.classify_phase("verify_failed") == "testing"
assert diagnose.classify_phase("unknown_future_class") == "coding"  # tolerant default
# the un-collapsed 2x2: acceptance fail + regression outcome splits intent vs code
assert diagnose.classify_phase("acceptance_failed", "pass", False) == "intent"
assert diagnose.classify_phase("acceptance_failed", "fail", False) == "coding"
assert diagnose.classify_phase("verify_failed", "fail", True) == "testing"

# a fresh goal with one feature that fails on mechanics then a bug, then escalates
droot = tmp / "diag"
ledger.record("plan.created", goal_id="d1", root=droot, plan_id="p",
              features=[{"feature_id": "fx", "title": "widget", "acceptance": "make test"}],
              rejected=[])
ledger.record("feature.failed", goal_id="d1", feature_id="fx", root=droot,
              attempt=1, failure_class="no_change", episode_id="e1", reason="outcome=no_change")
ledger.record("feature.failed", goal_id="d1", feature_id="fx", root=droot,
              attempt=2, failure_class="acceptance_failed", episode_id="e2", reason="outcome=fail")
ledger.record("escalation.raised", goal_id="d1", feature_id="fx", root=droot,
              reason_class="attempts_exhausted", reason="2 attempts failed", episode_ids=["e1", "e2"])
w = diagnose.why(droot)
blob = "\n".join(w)
assert "feature fx: widget" in blob, blob
assert "intent (acceptance criterion): make test" in blob, blob
assert "[coding]" in blob and "[testing]" in blob, blob          # both phases attributed
assert "pulse episode e2" in blob, blob                          # drill-down to raw traces
assert "escalation [attempts_exhausted]" in blob, blob
# phase rollup surfaces in insights
from plexus import observe as _obs  # noqa: E402
assert any("failures by phase" in l for l in _obs.insights(droot)), _obs.insights(droot)
# targeting an unknown feature is explained, not silent
assert "no failures or escalations" in "\n".join(diagnose.why(droot, "nope"))

# --- blocked-on-decision channel: detection, budget, resume-answer injection ---
from heart.episode import _blocked_reason  # noqa: E402
from plexus.run import _BLOCKED_MARKER, _blocks_so_far, _resume_answer  # noqa: E402

# detection lives in heart now (outcome="blocked"); plexus only supplies the word
M = _BLOCKED_MARKER
assert _blocked_reason("diff --git a/PLEXUS_BLOCKED b/PLEXUS_BLOCKED\n"
                       "+PLEXUS_BLOCKED: sync or async API?", M) == "sync or async API?"
assert _blocked_reason("+PLEXUS_BLOCKED:   trimmed?  ", M) == "trimmed?"
assert _blocked_reason("+def add(a, b):\n+    return a + b", M) is None  # normal diff
# a marker with no text still blocks: the agent declared it cannot proceed, and
# silently scoring the episode would lose that
assert _blocked_reason("+PLEXUS_BLOCKED:", M) == "(no reason given)"
assert _blocked_reason("+PLEXUS_BLOCKED: x", None) is None  # no marker configured

# blocks are counted per feature for the budget guard
broot = tmp / "block"
ledger.record("escalation.raised", goal_id="b1", feature_id="bf", root=broot,
              reason_class="blocked_on_decision", reason="q1")
ledger.record("escalation.resolved", goal_id="b1", feature_id="bf", root=broot,
              resolution="use async")
assert _blocks_so_far(ledger.read(broot), "b1", "bf") == 1
# a resolved block yields the answer to inject into the next attempt
inj = _resume_answer(ledger.read(broot), "b1", "bf")
assert "use async" in inj and "q1" in inj, inj
# an unresolved block yields nothing to inject yet
ledger.record("escalation.raised", goal_id="b1", feature_id="bf", root=broot,
              reason_class="blocked_on_decision", reason="q2")
assert _resume_answer(ledger.read(broot), "b1", "bf") == ""
assert _blocks_so_far(ledger.read(broot), "b1", "bf") == 2

# diagnose promotes a block to a certain intent verdict
ledger.record("plan.created", goal_id="b1", root=broot, plan_id="p",
              features=[{"feature_id": "bf", "title": "the widget", "acceptance": "make test"}],
              rejected=[])
bw = "\n".join(diagnose.why(broot, "bf"))
assert "BLOCKED — agent asked: q2" in bw, bw
assert "phase verdict: intent" in bw and "confirmed" in bw, bw

# an intent lean WITHOUT a block is flagged unconfirmed
uroot = tmp / "unconf"
ledger.record("plan.created", goal_id="u1", root=uroot, plan_id="p",
              features=[{"feature_id": "uf", "title": "w", "acceptance": "make test"}], rejected=[])
ledger.record("feature.failed", goal_id="u1", feature_id="uf", root=uroot, attempt=1,
              failure_class="acceptance_failed", episode_outcome="pass",
              acceptance_passed=False, episode_id="eu")
uw = "\n".join(diagnose.why(uroot, "uf"))
assert "phase verdict: intent" in uw and "unconfirmed" in uw, uw

# --- run loop: resumable state derivation, the pure core, no agent needed ---
from plexus.run import _feature_state  # noqa: E402

R = lambda **k: {"goal_id": "g", **k}  # noqa: E731
# open feature, first attempt, no budget spent
assert _feature_state([], "g", "f1") == ("open", 1, 0)
# one failed attempt -> next attempt 2, one unit of budget spent
recs = [R(kind="feature.started", feature_id="f1", attempt=1),
        R(kind="feature.failed", feature_id="f1", attempt=1)]
assert _feature_state(recs, "g", "f1") == ("open", 2, 1)
# landed -> skip, whatever the attempt count
assert _feature_state(recs + [R(kind="feature.landed", feature_id="f1", attempt=2)],
                      "g", "f1")[0] == "landed"
# three failed attempts then escalation -> a human owns it; the loop stops
esc = [R(kind="feature.started", feature_id="f1", attempt=n) for n in (1, 2, 3)]
esc += [R(kind="escalation.raised", feature_id="f1")]
assert _feature_state(esc, "g", "f1")[0] == "escalated"
# resolving reopens it: numbering stays monotonic (next=4) but budget resets to 0,
# so the loop gets a fresh attempts_per_feature — the bug the smoke test caught
assert _feature_state(esc + [R(kind="escalation.resolved", feature_id="f1")],
                      "g", "f1") == ("open", 4, 0)
# another goal's records never bleed in
assert _feature_state([{"goal_id": "other", "kind": "feature.landed",
                        "feature_id": "f1"}], "g", "f1") == ("open", 1, 0)

# --- _land commits only the diff's paths, not the whole dirty tree ---
import subprocess  # noqa: E402

from plexus.run import _diff_paths, _land  # noqa: E402

g = tmp / "landrepo"
g.mkdir()
G = lambda *a: subprocess.run(["git", "-C", str(g), *a], check=True,  # noqa: E731
                              capture_output=True, text=True).stdout
G("init", "-q", "-b", "main")
G("config", "user.email", "t@t"); G("config", "user.name", "t")
(g / "seed.txt").write_text("seed\n")
G("add", "-A"); G("commit", "-qm", "seed")

# the episode's diff touches one file
diff = subprocess.run(["git", "-C", str(g), "diff", "--", "seed.txt"],
                      capture_output=True, text=True).stdout
(g / "seed.txt").write_text("seed\nfeature\n")
diff = subprocess.run(["git", "-C", str(g), "diff"], capture_output=True, text=True).stdout
G("checkout", "--", "seed.txt")
assert _diff_paths(g, diff) == ["seed.txt"], _diff_paths(g, diff)

# meanwhile the tree is dirty the way a real repo is: plexus's own episode dumps
# and an unrelated edit the user had open
(g / "runs").mkdir()
(g / "runs" / "episode.json").write_text("{}")
(g / "mine.txt").write_text("my unrelated work\n")

_land(g, diff, "f1")
committed = G("show", "--name-only", "--format=", "HEAD").split()
assert committed == ["seed.txt"], f"land swept in extra files: {committed}"
assert (g / "runs" / "episode.json").exists() and (g / "mine.txt").exists()
assert "mine.txt" in G("status", "--porcelain"), "unrelated edit was consumed"

# --- approve gate: criteria must fail on the base commit ---
from plexus import plan as planmod  # noqa: E402
from plexus.spec import GoalSpec  # noqa: E402

# dependencies are hard ordering constraints; priority breaks ties only among
# work whose prerequisites have landed
unordered = [
    {"id": "ui", "priority": 0, "depends_on": ["api"]},
    {"id": "docs", "priority": 5, "depends_on": []},
    {"id": "api", "priority": 2, "depends_on": []},
]
assert [f["id"] for f in planmod.execution_order(unordered)] == ["api", "ui", "docs"]
try:
    planmod.execution_order([
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ])
    raise AssertionError("dependency cycle should be rejected")
except ValueError as exc:
    assert "cycle" in str(exc)

gspec = GoalSpec(goal_id="lg", text="t", context="", suite="true",
                 attempts_per_feature=3, episodes_per_goal=25, agent="shell",
                 agent_cmd=None, timeout=30, spec_hash="deadbeef")
(g / ".plexus").mkdir(exist_ok=True)
planmod.plan_path(g).write_text("\n".join(json.dumps(f) for f in [
    {"plan_id": "p", "id": "ok", "title": "t", "spec": "s", "acceptance": "test -f built.txt"},
    {"plan_id": "p", "id": "vacuous", "title": "t", "spec": "s", "acceptance": "true"},
    {"plan_id": "p", "id": "missing", "title": "t", "spec": "s", "acceptance": "no_such_tool_xyz"},
]) + "\n")
bad = dict(planmod.check_criteria(gspec, g))
assert "ok" not in bad, bad                     # legitimately fails on base -> usable
assert "vacuous" in bad and "vacuous" in bad["vacuous"], bad
assert "missing" in bad and "not found" in bad["missing"], bad
# approval refuses until the plan is fixed, and --waive records what was accepted
try:
    planmod.approve(gspec, g)
    raise AssertionError("approve should refuse an unusable plan")
except SystemExit as e:
    assert "vacuous" in str(e) and "missing" in str(e), e
planmod.approve(gspec, g, waive=True)
appr = [r for r in ledger.read(g) if r["kind"] == "plan.approved"][-1]
assert sorted(appr["waived"]) == ["missing", "vacuous"], appr

# --- notify fires on escalation only, and a broken notifier is not fatal ---
(g / "plexus.toml").write_text(
    '[goal]\nid="lg"\ntext="t"\n[ground_truth]\nsuite="true"\n'
    f'[notify]\ncmd = "echo $PLEXUS_REASON_CLASS/$PLEXUS_FEATURE > {g}/notified"\n')
ledger.record("feature.failed", goal_id="lg", feature_id="f1", root=g)
assert not (g / "notified").exists(), "notify fired on a non-escalation kind"
ledger.record("escalation.raised", goal_id="lg", feature_id="f9", root=g,
              reason_class="blocked_on_decision", reason="q")
assert (g / "notified").read_text().strip() == "blocked_on_decision/f9"
(g / "plexus.toml").write_text('[notify]\ncmd = "exit 3"\n')   # notifier that fails
ledger.record("escalation.raised", goal_id="lg", feature_id="f9", root=g)  # must not raise

# --- one run per goal repo: concurrent runs would race _land on one branch ---
import fcntl  # noqa: E402

from plexus.run import _lock_goal  # noqa: E402

lroot = tmp / "lockrepo"
lroot.mkdir(parents=True, exist_ok=True)
_lock_goal(lroot)
_lock_goal(lroot)  # same process, same root: no-op, must not deny us our own lock

# a *separate* open file description conflicts, which is what a second `plexus
# run` process is. Simulated here rather than forked: same flock semantics.
other = tmp / "lockrepo2"
other.mkdir(parents=True, exist_ok=True)
(other / ".plexus").mkdir(parents=True, exist_ok=True)
held = open(other / ".plexus" / "lock", "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
try:
    _lock_goal(other)
    raise AssertionError("second run should have been refused the lock")
except SystemExit as exc:
    assert "already active" in str(exc), exc
fcntl.flock(held, fcntl.LOCK_UN)
held.close()

# --- silent-layer detection: a repo that runs blind must say so ---
from plexus.observe import _silent_layers  # noqa: E402

# no episodes recorded yet -> nothing to judge, stay quiet
assert _silent_layers([], ".") is None
assert _silent_layers([{"kind": "goal.started", "goal_id": "g"}], ".") is None
# episodes present but absent from the journal (aged out) is not evidence of a gap
assert _silent_layers([{"kind": "acceptance.round", "episode_id": "no-such-episode"}],
                      ".") is None

# --- export: the join that makes the 2x2 visible to anything downstream ---
from plexus.export import build_rows, export  # noqa: E402
from plexus.prune import plan_prune, prune  # noqa: E402

xr = tmp / "exportrepo"
(xr / "runs").mkdir(parents=True, exist_ok=True)


def _episode(ep_id: str, outcome: str, reward, task_id: str) -> None:
    d = xr / "runs" / ep_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "episode.json").write_text(json.dumps({
        "episode_id": ep_id, "task_id": task_id, "outcome": outcome,
        "reward": {"total": reward, "components": {}}, "diff_lines": 12,
        "agent": "claude"}))


ledger.record("goal.started", goal_id="xg", root=xr, spec_hash="abc123")
ledger.record("plan.created", goal_id="xg", root=xr, plan_id="p1", features=[
    {"feature_id": "f1", "title": "one", "acceptance": "pytest tests/f1.py"},
    {"feature_id": "f2", "title": "two", "acceptance": "pytest tests/f2.py"}])

# f1 attempt 1: heart green, criterion unmet — the hard negative marrow needs
_episode("ep-a", "pass", 0.97, "xg-f1-a1")
ledger.record("acceptance.round", goal_id="xg", feature_id="f1", root=xr,
              attempt=1, task_id="xg-f1-a1", episode_id="ep-a", passed=False,
              episode_outcome="pass", check="pytest tests/f1.py")
ledger.record("feature.failed", goal_id="xg", feature_id="f1", root=xr, attempt=1,
              task_id="xg-f1-a1", episode_id="ep-a", failure_class="acceptance_failed",
              episode_outcome="pass", acceptance_passed=False, reason="x")
# f1 attempt 2 lands
_episode("ep-b", "pass", 0.99, "xg-f1-a2")
ledger.record("acceptance.round", goal_id="xg", feature_id="f1", root=xr, attempt=2,
              task_id="xg-f1-a2", episode_id="ep-b", passed=True,
              episode_outcome="pass", check="pytest tests/f1.py")
ledger.record("feature.landed", goal_id="xg", feature_id="f1", root=xr, attempt=2,
              task_id="xg-f1-a2", episode_id="ep-b", commit="deadbeef")
# f2 blocked, still open — no acceptance ever ran, attempt only in the task_id
_episode("ep-c", "blocked", None, "xg-f2-a1")
ledger.record("escalation.raised", goal_id="xg", feature_id="f2", root=xr,
              reason_class="blocked_on_decision", reason="sync or async?",
              episode_ids=["ep-c"])

rows = {r["episode_id"]: r for r in build_rows(xr)}
assert rows["ep-a"]["label"] == "wrong_thing_built", rows["ep-a"]
assert rows["ep-a"]["phase"] == "intent"          # coherent code, criterion wrong
assert rows["ep-a"]["heart_reward"] == 0.97       # heart's number survives the join
assert rows["ep-b"]["label"] == "landed" and rows["ep-b"]["commit"] == "deadbeef"
assert rows["ep-b"]["phase"] is None              # a landing has no failure phase
assert rows["ep-c"]["label"] == "blocked"
assert rows["ep-c"]["heart_reward"] is None       # abstention carries no reward
assert rows["ep-c"]["attempt"] == 1               # recovered from the task_id
assert rows["ep-c"]["criterion"] == "pytest tests/f2.py"
assert all(r["spec_hash"] == "abc123" for r in rows.values())

path, n, counts = export(xr)
assert n == 3 and counts["wrong_thing_built"] == 1, counts
assert len(path.read_text().strip().splitlines()) == 3

# --- prune: keeps what `why` and an open escalation still need ---
prunable, kept, _ = plan_prune(xr, days=0)
names = {d.name for d in prunable}
assert "ep-c" not in names, "episode behind an open escalation must survive"
assert "ep-b" in names, "a landed feature's episode is prunable"
# ep-a failed but f1 later landed, so it is history the ledger already tells
assert "ep-a" in names, names
assert "--apply" in "\n".join(prune(xr, days=0))          # dry run by default
assert (xr / "runs" / "ep-b" / "episode.json").exists()   # ...and deleted nothing
prune(xr, days=0, apply=True)
assert not (xr / "runs" / "ep-b").exists() and (xr / "runs" / "ep-c").exists()

# prune refuses to delete an episode whose reward was never exported (no
# labels.jsonl), and --force overrides
ur = tmp / "unexported-repo"
(ur / "runs" / "ep-x").mkdir(parents=True)
(ur / "runs" / "ep-x" / "episode.json").write_text("{}")
ledger.record("feature.landed", goal_id="g", feature_id="f", root=ur, episode_id="ep-x")
refused = "\n".join(prune(ur, days=0, apply=True))
assert "refusing to prune" in refused, refused
assert (ur / "runs" / "ep-x").exists(), "unexported reward must survive without --force"
prune(ur, days=0, apply=True, force=True)
assert not (ur / "runs" / "ep-x").exists(), "--force deletes anyway"

# --- planner parsing: prose around the JSON must not defeat it ---
from plexus.plan import _parse_features  # noqa: E402

FEAT = ('[{"id": "a", "title": "t", "spec": "s", "acceptance": "pytest -q",'
        ' "touches": ["src/*"], "contract": []}]')
# a fenced block wins over bracket-slicing: the trailing sentence here contains
# brackets, which is exactly what the outermost-[..] heuristic gets wrong
assert _parse_features(f"```json\n{FEAT}\n```\n\nSkipped a [scaffold] step.")[0]["id"] == "a"
assert _parse_features(f"here you go:\n{FEAT}")[0]["acceptance"] == "pytest -q"
assert _parse_features(f"```\n{FEAT}\n```")[0]["title"] == "t"
try:
    _parse_features("no array at all")
    raise AssertionError("should have rejected output with no JSON array")
except ValueError:
    pass
try:
    _parse_features('```json\n[{"id": "a"}]\n```')   # missing required keys
    raise AssertionError("should have rejected an incomplete feature")
except ValueError:
    pass
# a plan with no path allowlist cannot be scope-enforced, so it never gets made
NO_TOUCHES = '[{"id": "a", "title": "t", "spec": "s", "acceptance": "pytest -q"}]'
try:
    _parse_features(f"```json\n{NO_TOUCHES}\n```")
    raise AssertionError("should have rejected a feature with no touches")
except ValueError:
    pass
# contract is genuinely optional — absent and "adds no public surface" are the
# same statement, so it defaults rather than failing the plan
assert _parse_features(f"```json\n{NO_TOUCHES[:-2]}, \"touches\": [\"src/*\"]}}]"
                       f"\n```")[0]["contract"] == []

# --- cost: candidate spend sums across all N, skips unpriced, surfaces in insights ---
from plexus.run import _episode_cost  # noqa: E402

# two priced candidates + one heart couldn't price (usage None) → sum the priced
cands = [{"usage": {"cost_usd": 0.10, "tokens_in": 100, "tokens_out": 50}},
         {"usage": {"cost_usd": 0.05, "tokens_in": 40, "tokens_out": 20}},
         {"usage": {"cost_usd": None, "tokens_in": None, "tokens_out": None}}]
c = _episode_cost(cands)
assert c == {"cost_usd": 0.15, "tokens_in": 140, "tokens_out": 70}, c
assert _episode_cost([{"usage": None}]) == {}, "no usage → no cost keys, not zeros"

cr = tmp / "cost-repo"
ledger.record("feature.landed", goal_id="g", feature_id="f1", root=cr,
              cost_usd=0.15, tokens_in=140, tokens_out=70)
ledger.record("feature.failed", goal_id="g", feature_id="f2", root=cr,
              cost_usd=0.05, tokens_in=40, tokens_out=20)
ins = "\n".join(observe.insights(str(cr)))
assert "cost: $0.2000" in ins and "180 in / 90 out" in ins, ins

# --- _goal_spend: aggregate rollup for budget_exhausted/regression, distinct
#     spend_* keys so the per-attempt cost summers never double-count it
from plexus.run import _goal_spend  # noqa: E402
gs = [{"kind": "feature.started"}, {"kind": "feature.started"},
      {"kind": "feature.failed", "cost_usd": 0.05, "tokens_in": 40, "tokens_out": 20},
      {"kind": "feature.landed", "cost_usd": 0.15, "tokens_in": 140, "tokens_out": 70}]
assert _goal_spend(gs) == {"episodes_total": 2, "spend_usd": 0.2,
                           "spend_tokens_in": 180, "spend_tokens_out": 90}, _goal_spend(gs)
assert _goal_spend([{"kind": "feature.started"}]) == {"episodes_total": 1}  # unpriced
# the rollup must NOT collide with cost_usd: insights on cr still reads $0.2000,
# not doubled, because _goal_spend emits spend_usd (proven by the assert above +
# that cr's records carry only per-attempt cost_usd, summed once here)
assert "cost_usd" not in _goal_spend(gs), "rollup must avoid the cost_usd key"

# --- amend: rewrites an unlanded feature's criterion, refuses a landed one ---
from types import SimpleNamespace  # noqa: E402
from plexus.plan import amend, load_plan, plan_path  # noqa: E402

ar = tmp / "amend-repo"
(ar / ".plexus").mkdir(parents=True)
plan_path(ar).write_text("\n".join(json.dumps(p) for p in [
    {"plan_id": "p1", "id": "f1", "title": "one", "spec": "s1", "acceptance": "false"},
    {"plan_id": "p1", "id": "f2", "title": "two", "spec": "s2", "acceptance": "false"},
]) + "\n")
sp = SimpleNamespace(goal_id="g")

amend(sp, "f2", ar, acceptance="pytest tests/f2.py", title="Two!")
plan = {f["id"]: f for f in load_plan(ar)}
assert plan["f2"]["acceptance"] == "pytest tests/f2.py", plan["f2"]
assert plan["f2"]["title"] == "Two!" and plan["f1"]["acceptance"] == "false"
assert any(r["kind"] == "plan.amended" and r.get("feature_id") == "f2"
           for r in ledger.read(ar)), "amend must record plan.amended"

# a landed feature is refused
ledger.record("feature.landed", goal_id="g", feature_id="f1", root=ar, attempt=1)
try:
    amend(sp, "f1", ar, acceptance="true")
    raise AssertionError("amend must refuse a landed feature")
except SystemExit:
    pass

# --- control plane: serve.py carries its own demo(); run it here so its proof
# (ledger->tab state, goal discovery, flock liveness) guards on the normal check ---
import contextlib
import io

from plexus import serve  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):  # demo() prints "ok"; keep our line clean
    serve.demo()

# --- #4 upstream gate: importable symbol passes, missing module/symbol reported
from plexus.run import _missing_upstream  # noqa: E402

assert _missing_upstream([]) == []
assert _missing_upstream(["json", "json:loads"]) == []          # stdlib, present
assert _missing_upstream(["json:nonesuch"]) == ["json:nonesuch"]  # module ok, symbol gone
assert _missing_upstream(["no_such_module_xyz:Thing"]) == ["no_such_module_xyz:Thing"]

# --- report digest: per-goal spend / lead-time / escalation rate + fleet total
rr = tmp / "report-repo"; rr.mkdir()
(rr / "plexus.toml").write_text('[goal]\nid="rg"\ntext="t"\n[ground_truth]\nsuite="true"\n')
ledger.record("feature.started", goal_id="rg", feature_id="f1", root=rr)
ledger.record("feature.landed", goal_id="rg", feature_id="f1", root=rr, cost_usd=0.30)
ledger.record("feature.started", goal_id="rg", feature_id="f2", root=rr)
ledger.record("escalation.raised", goal_id="rg", feature_id="f2", root=rr,
              reason_class="attempts_exhausted", reason="x")
m = observe._metrics(ledger.read(rr), "rg")
assert m["spend"] == 0.30 and m["landed"] == 1, m
assert m["touched"] == 2 and m["esc_raised"] == 1 and m["esc_rate"] == 0.5, m
rep = "\n".join(observe.report([rr]))
assert "rg" in rep and "$  0.3000" in rep and "fleet: $0.3000" in rep, rep
assert observe.report([tmp / "nonexistent"]) == ["no goals recorded"]

# --- registry: prefix resolution + idempotent cross-repo seeding
from plexus import registry  # noqa: E402
import contextlib as _c, io as _io  # noqa: E402
with _c.redirect_stdout(_io.StringIO()):
    registry.demo()

# seed writes a plannable goal into the owning repo, then load_spec reads it back
from plexus.spec import load_spec  # noqa: E402
up = tmp / "up-repo"; up.mkdir()
seeded = registry.seed_upstream(["heart.x:Y"], "g-down", {"heart": str(up)})
assert seeded == [("heart.x:Y", str(up))], seeded
assert load_spec(up).goal_id == "upstream-y"       # scaffolded spec is valid
assert registry.seed_upstream(["heart.x:Y"], "g-down", {"heart": str(up)}) == []  # idempotent

# --- review hold-gate: held classes escalate before landing, then land once resolved
from plexus.run import _held_before  # noqa: E402
hr = tmp / "hold-repo"; (hr / ".plexus").mkdir(parents=True)
assert not _held_before([], "g", "f1")
ledger.record("escalation.raised", goal_id="g", feature_id="f1", root=hr,
              reason_class="held_for_review", reason="sign off")
assert _held_before(ledger.read(hr), "g", "f1")   # a prior hold means it was resolved -> land
# the two classes that can break something no test covers hold by default — a
# spec that says nothing must not mean "land the ledger schema unread"
assert load_spec(rr).review_hold == ("spine", "boundary")
assert load_spec(rr).pr_base == "main"
# and an explicit empty list still opts out
(rr / "plexus.toml").write_text((rr / "plexus.toml").read_text()
                                + '\n[review]\nhold = []\npr_base = ""\n')
assert load_spec(rr).review_hold == () and load_spec(rr).pr_base == ""

# --- resolve guard: a wrong feature id must not silently no-op (shakeout finding)
from plexus import cli  # noqa: E402
rg = tmp / "resolve-repo"; rg.mkdir()
(rg / "plexus.toml").write_text('[goal]\nid="rgoal"\ntext="t"\n[ground_truth]\nsuite="true"\n')
ledger.record("escalation.raised", goal_id="rgoal", feature_id="realf", root=rg,
              reason_class="attempts_exhausted", reason="x")
assert cli.main(["resolve", "wrongf", "--root", str(rg)]) == 1      # typo -> refused
assert not any(r["kind"] == "escalation.resolved" for r in ledger.read(rg))
assert cli.main(["resolve", "realf", "ok", "--root", str(rg)]) == 0  # correct -> resolved
assert any(r["kind"] == "escalation.resolved" and r["feature_id"] == "realf"
           for r in ledger.read(rg))

# --- cache tokens: priced, never folded into tokens_in ---------------------
# A cached agent turn reports almost no `tokens_in` while sending tens of
# thousands of cached tokens. Counting only tokens_in understated every turn;
# adding cache into it would overcharge reads tenfold. Both are wrong, so the
# buckets stay separate all the way from heart's event to the dashboard.
from heart.runner import CACHE_MULTIPLIERS  # noqa: E402
from plexus.run import _episode_cost  # noqa: E402

eps = [{"usage": {"cost_usd": 0.10, "tokens_in": 15, "tokens_out": 2692,
                  "cache_read": 48_000, "cache_write_5m": 9_000, "cache_write_1h": 3_000}},
       {"usage": {"cost_usd": 0.05, "tokens_in": 10, "tokens_out": 100,
                  "cache_read": 1_000, "cache_write_5m": 0, "cache_write_1h": 0}}]
agg = _episode_cost(eps)
assert agg["cache_read"] == 49_000 and agg["cache_write_5m"] == 9_000, agg
assert agg["cache_write_1h"] == 3_000 and agg["tokens_in"] == 25, agg
assert agg["cost_usd"] == 0.15, agg
# an episode heart could not price contributes nothing rather than a zero
assert "cache_read" not in _episode_cost([{"usage": {}}])

# end to end through the dashboard: a synthetic journal, real arithmetic
import shutil  # noqa: E402
journal = tmp / "cache-journal"; journal.mkdir()
prior_journal = os.environ["EVENT_JOURNAL_DIR"]
os.environ["EVENT_JOURNAL_DIR"] = str(journal)
try:
    croot = tmp / "cache-repo"; croot.mkdir()
    (croot / "plexus.toml").write_text(
        '[goal]\nid="cg"\ntext="t"\n[ground_truth]\nsuite="true"\n')
    import datetime as _dt  # noqa: E402
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    (journal / "20260731.ndjson").write_text(json.dumps({
        "ts": now, "source": "heart", "kind": "role.finished",
        "payload": {"agent": "claude", "cli": "claude", "repo": str(croot),
                    "tokens_in": 1_000_000, "tokens_out": 0,
                    "cache_read": 1_000_000, "cache_write_5m": 1_000_000,
                    "cache_write_1h": 1_000_000}}) + "\n")
    from plexus import registry, serve  # noqa: E402
    old_ws = os.environ.get("PLEXUS_WORKSPACE")
    os.environ["PLEXUS_WORKSPACE"] = str(tmp / "ws.json")
    # 1M uncached @ $5 + 1M read @ 0.1x + 1M 5m write @ 1.25x + 1M 1h write @ 2x
    want = 5.0 + 0.5 + 6.25 + 10.0
    try:
        registry.set_accounting_config(
            {"claude": 0, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0}})
        cost = serve._dashboard([croot], 24)["cost"]

        # --- no double count: heart emits BOTH the per-role event and an
        #     episode.finished carrying the sum of those roles. Every consumer
        #     must price role.finished only; adding the aggregate doubles the
        #     bill, and the failure is invisible because the result is still a
        #     plausible number. Append the aggregate and assert nothing moves.
        #
        # Inside the try, because the prices this asserts against live in the
        # workspace PLEXUS_WORKSPACE points at. Outside it, the lookup fell back
        # to ~/.config/plexus/workspace.json -- so the assertion passed on a
        # developer machine that happened to have one and read 0.0 on a fresh
        # CI runner, which is the least useful place to learn it.
        with open(journal / "20260731.ndjson", "a") as fh:
            fh.write(json.dumps({
                "ts": now, "source": "heart", "kind": "episode.finished",
                "payload": {"agent": "claude", "cli": "claude", "repo": str(croot),
                            "outcome": "pass",
                            "tokens_in": 1_000_000, "tokens_out": 0,
                            "cache_read": 1_000_000, "cache_write_5m": 1_000_000,
                            "cache_write_1h": 1_000_000, "cost_usd": want}}) + "\n")
        again = serve._dashboard([croot], 24)["cost"]
        # the same trap in the factory-wide rollup
        stack = "\n".join(observe.stack(hours=24))

        # --- an interactive CLI turn is priced too. arteries emits
        #     turn.observed and heart emits role.finished for disjoint work, so
        #     both count; this is ~all of a subscription seat's real workload
        #     and used to price at 0.
        with open(journal / "20260731.ndjson", "a") as fh:
            fh.write(json.dumps({
                "ts": now, "source": "arteries", "kind": "turn.observed",
                "turn_id": "t1",
                "payload": {"cli": "claude", "repo": str(croot),
                            "tokens_in": 1_000_000, "tokens_out": 0,
                            "cache_read": 1_000_000, "cache_write_5m": 1_000_000,
                            "cache_write_1h": 1_000_000}}) + "\n")
        withturn = serve._dashboard([croot], 24)["cost"]
    finally:
        os.environ.pop("PLEXUS_WORKSPACE", None) if old_ws is None \
            else os.environ.__setitem__("PLEXUS_WORKSPACE", old_ws)
    assert abs(cost["equivalent_api"] - want) < 1e-6, (cost["equivalent_api"], want)
    assert cost["cache_tokens"] == 3_000_000, cost["cache_tokens"]
    assert cost["tokens_in"] == 1_000_000, "cache must not inflate tokens_in"
    assert abs(again["equivalent_api"] - want) < 1e-6, \
        f"episode.finished double-counted: {again['equivalent_api']} != {want}"
    assert again["cache_tokens"] == 3_000_000, again["cache_tokens"]
    assert again["tokens_in"] == 1_000_000, again["tokens_in"]
    assert "2 priced role-turn(s)" not in stack, stack
    assert abs(withturn["equivalent_api"] - 2 * want) < 1e-6, \
        f"interactive turn not priced: {withturn['equivalent_api']} != {2 * want}"
    assert withturn["cache_tokens"] == 6_000_000, withturn["cache_tokens"]

    # --- a turn is billed against its model, not just its vendor. Opus and
    #     Haiku on one CLI differ by more than 10x, so one rate per provider
    #     misprices whichever you use less. arteries reports `model`; a rate
    #     card row for it must win over the provider rate.
    with open(journal / "20260731.ndjson", "a") as fh:
        fh.write(json.dumps({
            "ts": now, "source": "arteries", "kind": "turn.observed",
            "turn_id": "t2",
            "payload": {"cli": "claude", "repo": str(croot),
                        "model": "claude-haiku-4-5",
                        "tokens_in": 1_000_000, "tokens_out": 0}}) + "\n")
    old_ws = os.environ.get("PLEXUS_WORKSPACE")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["PLEXUS_WORKSPACE"] = str(tmp / "ws.json")
    # heart's rate card is read from $XDG_CONFIG_HOME/heart/models.json. Point
    # it at a fixture: without this the assertions below pass or fail according
    # to what the developer running the suite happens to have configured.
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
    (tmp / "cfg" / "heart").mkdir(parents=True, exist_ok=True)
    (tmp / "cfg" / "heart" / "models.json").write_text(json.dumps({
        "profiles": {"haiku": {"model": "claude-haiku-4-5"}},
        "pricing": {"claude:haiku": {"in_per_mtok": 1.0, "out_per_mtok": 5.0}},
    }))
    try:
        # heart owns the verified card; plexus bills from it rather than
        # keeping a second copy, so a provider-only workspace config still
        # prices Haiku at the Haiku rate.
        registry.set_accounting_config(
            {"claude": 0, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0}})
        bymodel = serve._dashboard([croot], 24)["cost"]
        assert abs(bymodel["equivalent_api"] - (2 * want + 1.0)) < 1e-6, \
            f"heart model rate ignored: {bymodel['equivalent_api']}"
        assert bymodel["models"]["claude"]["claude-haiku-4-5"] == 1, bymodel["models"]

        # a workspace override outranks heart's card — a negotiated rate has to
        # beat the published one
        registry.set_accounting_config(
            {"claude": 0, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0,
                        "models": {"claude-haiku-4-5": {"input": 4.0, "output": 20.0}}}})
        override = serve._dashboard([croot], 24)["cost"]
        assert abs(override["equivalent_api"] - (2 * want + 4.0)) < 1e-6, \
            f"workspace override ignored: {override['equivalent_api']}"

        # a model in neither card still bills, at the provider rate: adding one
        # model's rate must not silently stop the others being counted
        (tmp / "cfg" / "heart" / "models.json").write_text(json.dumps({
            "profiles": {}, "pricing": {}}))
        registry.set_accounting_config(
            {"claude": 0, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0}})
        fallback = serve._dashboard([croot], 24)["cost"]
        assert abs(fallback["equivalent_api"] - (2 * want + 5.0)) < 1e-6, \
            f"provider fallback broken: {fallback['equivalent_api']}"
    finally:
        for key, prior in (("PLEXUS_WORKSPACE", old_ws), ("XDG_CONFIG_HOME", old_xdg)):
            os.environ.pop(key, None) if prior is None \
                else os.environ.__setitem__(key, prior)

    # --- fast mode bills at 2x across the whole window, cache included. The
    #     multiplier scales the rates before the cache buckets are worked out,
    #     because the vendor stacks caching on top of fast-mode pricing; naively
    #     doubling the final total gets the same answer only when there is no
    #     cache traffic, which is never true of a real agent turn.
    with open(journal / "20260731.ndjson", "a") as fh:
        fh.write(json.dumps({
            "ts": now, "source": "arteries", "kind": "turn.observed",
            "turn_id": "tfast",
            "payload": {"cli": "claude", "repo": str(croot),
                        "speed": "fast",
                        "tokens_in": 1_000_000, "tokens_out": 1_000_000,
                        "cache_read": 1_000_000}}) + "\n")
    old_ws = os.environ.get("PLEXUS_WORKSPACE")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["PLEXUS_WORKSPACE"] = str(tmp / "ws.json")
    # empty card again, so every turn in the window prices off the provider
    # rate and the only variable under test is the speed multiplier
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "cfg")
    try:
        registry.set_accounting_config(
            {"claude": 0, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0}})
        fast = serve._dashboard([croot], 24)["cost"]
        # the earlier Haiku turn now bills at the $5 provider rate, plus
        # 1M in @ $10 + 1M out @ $50 + 1M cache read @ 0.1 x $10 = $61
        assert abs(fast["equivalent_api"] - (2 * want + 5.0 + 61.0)) < 1e-6, \
            f"fast mode not billed at 2x: {fast['equivalent_api']}"
        assert fast["premium_speed"] == {"fast": 1}, fast["premium_speed"]
    finally:
        for key, prior in (("PLEXUS_WORKSPACE", old_ws), ("XDG_CONFIG_HOME", old_xdg)):
            os.environ.pop(key, None) if prior is None \
                else os.environ.__setitem__(key, prior)

    # --- turns money could not be attached to are counted, not dropped. Both
    #     of these used to fall off the pricing chain leaving no trace, so a
    #     provider with no adapter read as free rather than as unmeasured.
    with open(journal / "20260731.ndjson", "a") as fh:
        fh.write(json.dumps({
            "ts": now, "source": "arteries", "kind": "turn.observed",
            "payload": {"repo": str(croot), "usage_source": "unavailable"}}) + "\n")
        fh.write(json.dumps({
            "ts": now, "source": "arteries", "kind": "turn.observed",
            "payload": {"repo": str(croot), "tokens_in": 999}}) + "\n")
    gaps = serve._dashboard([croot], 24)["cost"]["gaps"]
    assert gaps.get("unmeasured") == 1, gaps
    assert gaps.get("unattributed") == 1, gaps

    # utilisation: a ratio only when both halves are real. Seat detection reads
    # the signed-in CLI plans out of $HOME, so point HOME at an empty dir —
    # otherwise this assertion passes or fails according to whose machine runs
    # the suite.
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp / "empty-home")
    try:
        nc = serve._dashboard([croot], 24)["cost"]
        assert nc["subscription"] == 0.0, nc["subscription"]
        assert nc["seat_utilisation"] is None, \
            "no seat cost -> None, never a 0% that reads as waste"
    finally:
        os.environ.pop("HOME", None) if real_home is None \
            else os.environ.__setitem__("HOME", real_home)

    # --- the time window actually filters. A turn three days old must be
    #     invisible at 1h, visible at 7d, and visible at all time (window 0).
    #     The failure this pins is a metric that silently keeps its own 24h
    #     cutoff while the picker says something else.
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)).isoformat()
    with open(journal / "20260731.ndjson", "a") as fh:
        fh.write(json.dumps({
            "ts": old_ts, "source": "arteries", "kind": "turn.observed",
            "turn_id": "old1",
            "payload": {"cli": "claude", "repo": str(croot),
                        "tokens_in": 2_000_000, "tokens_out": 0}}) + "\n")
    # a real seat price, or the accrual assertions below compare 0.0 to 0.0
    old_ws = os.environ.get("PLEXUS_WORKSPACE")
    os.environ["PLEXUS_WORKSPACE"] = str(tmp / "ws.json")
    try:
        registry.set_accounting_config(
            {"claude": 200, "codex": 0},
            {"claude": {"input": 5.0, "output": 25.0}})
        hour = serve._dashboard([croot], 1)
        week = serve._dashboard([croot], 24 * 7)
        year = serve._dashboard([croot], 24 * 365)
        forever = serve._dashboard([croot], 0)
    finally:
        os.environ.pop("PLEXUS_WORKSPACE", None) if old_ws is None \
            else os.environ.__setitem__("PLEXUS_WORKSPACE", old_ws)
    assert forever["cost"]["subscription"] > 0, "seat price not applied"
    assert week["cost"]["tokens_in"] - hour["cost"]["tokens_in"] == 2_000_000, \
        (hour["cost"]["tokens_in"], week["cost"]["tokens_in"])
    assert week["activity"]["turns"] > hour["activity"]["turns"], "turn counts ignore the window"
    assert forever["cost"]["tokens_in"] == week["cost"]["tokens_in"], "all time must not drop events"
    # Seat accrual caps at the span of observed history. Without the cap a
    # one-year window prorated twelve months of subscription over three days of
    # data and reported it as spend — plausible-looking and entirely invented.
    # not exact equality: each call measures elapsed history from its own
    # now(), so the two land microseconds apart and can straddle the rounding
    # step. A cent of tolerance still catches the regression this pins, which
    # was a year window accruing $2400 against three days of data.
    assert abs(year["cost"]["subscription"]
               - forever["cost"]["subscription"]) < 0.01, \
        ("a window longer than the data must not accrue seat cost past it",
         year["cost"]["subscription"], forever["cost"]["subscription"])
    assert forever["cost"]["subscription"] < 24 * 30, forever["cost"]["subscription"]
    assert "all time" in "\n".join(observe.stack(hours=0))
finally:
    os.environ["EVENT_JOURNAL_DIR"] = prior_journal

print("plexus self-check ok")
