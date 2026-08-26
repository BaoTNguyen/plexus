"""Self-check for the scope/conformance surface, same style as test_plexus.py:
stdlib asserts, no frameworks, no network. Run: python3 tests/test_review.py

Covers the three things that can silently stop working:
  1. the glob matcher, where a wrong `*` either blocks honest work or waves
     through a diff that escaped its slice
  2. the classifier, which decides how much of your attention a feature costs
  3. the landed-vs-planned report, end to end against a real git repo
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="plexus-review-test-"))
os.environ["EVENT_JOURNAL_DIR"] = str(tmp / "journal")

here = Path(__file__).resolve()
for p in (here.parents[1] / "src", here.parents[2] / "heart" / "src"):
    sys.path.insert(0, str(p))
from plexus import ledger, review  # noqa: E402
from plexus.plan import matches  # noqa: E402
from plexus.run import _stray_paths  # noqa: E402

# --- glob matcher: one segment per `*`, `**` is the only one that spans ---
assert matches("src/plexus/review.py", "src/plexus/*")
assert matches("src/plexus/review.py", "src/plexus/review.py")
assert not matches("src/plexus/sub/deep.py", "src/plexus/*"), "* must not span /"
assert matches("src/plexus/sub/deep.py", "src/plexus/**")
assert matches("tests/test_review.py", "tests/**")
assert matches("tests/a/b/c.py", "tests/**")
assert not matches("src/other.py", "tests/**")
assert matches("anything/at/all.py", "**")
assert matches("README.md", "*.md")
assert not matches("docs/README.md", "*.md"), "*.md is one segment only"

# --- stray paths: absent allowlist is unenforced, never retro-blocking ---
assert _stray_paths(["src/a.py", "src/b.py"], None) == []
assert _stray_paths(["src/a.py", "src/b.py"], []) == []
assert _stray_paths(["src/a.py", "LEDGER.md"], ["src/*"]) == ["LEDGER.md"]
assert _stray_paths(["src/a.py"], ["src/*", "tests/*"]) == []

# --- classifier: risk from plan fields alone, no git involved ---
def cls(touches, contract=None):
    return review.classify({"touches": touches, "contract": contract or []})


assert cls(["src/plexus/ledger.py"]) == "spine"
assert cls(["src/plexus/*"]) == "spine", "a glob covering a spine file is spine"
assert cls(["LEDGER.md"]) == "spine"
assert cls(["src/plexus/run.py"], ["ledger kind: scope.checked"]) == "spine"
assert cls(["src/plexus/cli.py"], ["plexus review"]) == "boundary"
assert cls(["tests/test_heart_api_pin.py"]) == "boundary", "the cross-repo seam"
assert cls(["src/plexus/prune.py"], ["plexus.toml key: budgets.x"]) == "boundary"
assert cls(["src/plexus/prune.py"], ["prune.sweep(days) -> int"]) == "leaf"
assert cls(["README.md", "tests/test_prune.py"]) == "mechanical"
assert cls(["README.md", "src/plexus/prune.py"]) == "leaf", "one code path is enough"
# A wide allowlist is judged by what it *permits*, not by what the agent
# intended: `tests/*` can reach the pin test, so it buys a boundary review.
# Same reason `src/plexus/*` reads as spine. Narrow the glob to buy it back.
assert cls(["tests/*"]) == "boundary"
assert cls([]) == "spine", "no allowlist must assume the worst, not the best"
assert review.classify({}) == "spine"

# --- contract parsing tolerates the shapes the planner actually writes ---
d = review._declared({"contract": ["review.report(spec, root) -> str", "class Row",
                                   "plexus review"]})
assert {"review.report", "report", "Row", "class Row"} <= d | {"class Row"}
assert "report" in d and "Row" in d

# --- end to end: a real repo, two commits, one obedient and one not ---
repo = tmp / "repo"
repo.mkdir()
git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
subprocess.run([*git[:3], "init", "-q"], check=True)
(repo / "src").mkdir()
(repo / "seed.txt").write_text("seed\n")
subprocess.run([*git, "add", "-A"], check=True)
subprocess.run([*git, "commit", "-qm", "seed"], check=True)


def commit(files: dict[str, str], msg: str) -> str:
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    # pathspec, not `add -A`: run.py's _land commits exactly the diff's paths,
    # and once .plexus/ exists a blanket add would sweep the ledger into the
    # feature commit and make every later feature look like a scope violation
    subprocess.run([*git, "add", "--", *files], check=True)
    subprocess.run([*git, "commit", "-qm", msg, "--", *files], check=True)
    return subprocess.run([*git[:3], "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


# f1 keeps its promise; f2 exports a helper it never declared and edits a file
# outside its slice — the two failure modes the report exists to name
c1 = commit({"src/one.py": "def alpha():\n    return 1\n"}, "plexus: land f1")
c2 = commit({"src/two.py": "def beta():\n    return 2\n\n\ndef gamma():\n    return 3\n",
             "src/one.py": "def alpha():\n    return 99\n"}, "plexus: land f2")

plan_dir = repo / ".plexus"
plan_dir.mkdir(exist_ok=True)
plan = [
    {"plan_id": "p1", "id": "f1", "title": "alpha", "spec": "s", "acceptance": "true",
     "touches": ["src/one.py"], "contract": ["one.alpha() -> int"]},
    {"plan_id": "p1", "id": "f2", "title": "beta", "spec": "s", "acceptance": "true",
     "touches": ["src/two.py"], "contract": ["two.beta() -> int"]},
]
(plan_dir / "plan.jsonl").write_text("".join(json.dumps(f) + "\n" for f in plan))

ledger.record("feature.landed", goal_id="g1", feature_id="f1", root=repo, commit=c1)
ledger.record("feature.landed", goal_id="g1", feature_id="f2", root=repo, commit=c2)



# --- the scope gate, composed the way run.py composes it -------------------
# _stray_paths is only correct if it is fed git's own path format. Unit-testing
# the matcher against hand-written strings would not catch a `git apply
# --numstat` that returned `b/src/one.py`, so compose the two for real.
from plexus.run import _diff_paths  # noqa: E402

(repo / "src" / "three.py").write_text("x = 1\n")
(repo / "LEDGER.md").write_text("law\n")
subprocess.run([*git, "add", "-AN"], check=True)  # intent-to-add: new files show in diff
real_diff = subprocess.run([*git[:3], "diff"], capture_output=True, text=True,
                           check=True).stdout
paths = _diff_paths(repo, real_diff)
assert {"src/three.py", "LEDGER.md"} <= set(paths), paths
assert not any(p.startswith(("a/", "b/")) for p in paths), f"raw diff prefixes: {paths}"
stray = _stray_paths(paths, ["src/*"])
assert "LEDGER.md" in stray and "src/three.py" not in stray, stray
assert _stray_paths(["src/three.py", "LEDGER.md"], ["src/*", "LEDGER.md"]) == []
subprocess.run([*git, "reset", "-q"], check=True)
(repo / "src" / "three.py").unlink()
(repo / "LEDGER.md").unlink()

# a scope violation is a planning failure, not a coding one — `plexus why` has
# to say so, or the whole point of routing it to a human is lost
from plexus.diagnose import _ESC_PHASE  # noqa: E402

assert _ESC_PHASE["scope_violation"] == "intent"


class FakeSpec:
    goal_id = "g1"


rows = {r["feature_id"]: r for r in review.rows(FakeSpec, repo, repo)}
assert set(rows) == {"f1", "f2"}
assert rows["f1"]["class"] == "leaf"
assert rows["f1"]["stray_paths"] == [] and rows["f1"]["unplanned_symbols"] == []
assert rows["f1"]["verdict"] == "ok", "a feature that kept its promise costs no attention"
assert rows["f2"]["stray_paths"] == ["src/one.py"], rows["f2"]
assert rows["f2"]["unplanned_symbols"] == ["two.gamma"], rows["f2"]
assert rows["f2"]["removed_symbols"] == [], "f2 edited alpha, it did not delete it"
assert rows["f2"]["verdict"] == "FLAG"

text = review.report(FakeSpec, repo, repo)
assert "1 of 2 landed features need your eyes." in text, text
assert "two.gamma" in text and "unplanned path src/one.py" in text

# a spine feature is read for what it is, even when the diff behaved perfectly
plan[0]["touches"] = ["src/one.py", "LEDGER.md"]
(plan_dir / "plan.jsonl").write_text("".join(json.dumps(f) + "\n" for f in plan))
rows2 = {r["feature_id"]: r for r in review.rows(FakeSpec, repo, repo)}
assert rows2["f1"]["class"] == "spine" and rows2["f1"]["verdict"] == "READ"
assert rows2["f1"]["stray_paths"] == []

assert "2 of 2 features will need line-by-line review." not in review.preview(repo)
assert "1 of 2 features will need line-by-line review." in review.preview(repo)

# --- a deletion nobody declared -------------------------------------------
# The half `added_symbols` alone cannot see. A commit that quietly drops a
# public name leaves callers unresolved, and in this stack the caller is often
# in the next repo up, so this is the pin test's failure one step earlier.
c3 = commit({"src/two.py": "def beta():\n    return 2\n"}, "plexus: land f3")
plan.append({"plan_id": "p1", "id": "f3", "title": "tidy", "spec": "s",
             "acceptance": "true", "touches": ["src/two.py"], "contract": []})
(plan_dir / "plan.jsonl").write_text("".join(json.dumps(f) + "\n" for f in plan))
ledger.record("feature.landed", goal_id="g1", feature_id="f3", root=repo, commit=c3)

rows3 = {r["feature_id"]: r for r in review.rows(FakeSpec, repo, repo)}
assert rows3["f3"]["removed_symbols"] == ["two.gamma"], rows3["f3"]
assert rows3["f3"]["unplanned_symbols"] == []
assert rows3["f3"]["verdict"] == "FLAG", "an undeclared deletion must not read as ok"
assert "removed public two.gamma" in review.report(FakeSpec, repo, repo)

# declaring the removal in `contract` is how you say you meant it
plan[-1]["contract"] = ["two.gamma"]
(plan_dir / "plan.jsonl").write_text("".join(json.dumps(f) + "\n" for f in plan))
rows4 = {r["feature_id"]: r for r in review.rows(FakeSpec, repo, repo)}
assert rows4["f3"]["removed_symbols"] == [] and rows4["f3"]["verdict"] == "ok"

# --- expect blocks: the mockup is executed, not filed ----------------------
from plexus.plan import parse_expect  # noqa: E402
from plexus.run import _check_expect  # noqa: E402

assert parse_expect("just a spec") is None
assert parse_expect("does what it says\n\nexpect:\nno command here") is None, \
    "a block with no `$ ` line is prose, not a mockup"
cmd, want = parse_expect(
    "print two lines\n\nexpect:\n$ echo hi\nhi\n\nthere\n")
assert cmd == "echo hi" and want == ["hi", "there"], (cmd, want)
# the format asks for the block at the end, so a spec that mentions one earlier
# must not shadow the real one
assert parse_expect("expect:\n$ false\nold\n\nexpect:\n$ true\nnew")[0] == "true"

ok, tail = _check_expect(str(repo), ("printf 'a\\nb\\n'", ["a", "b"]), 30)
assert ok and tail == ""
ok, tail = _check_expect(str(repo), ("printf 'a\\n'", ["a", "zzz"]), 30)
assert not ok and "zzz" in tail and "actual output" in tail, tail
# containment, not equality: a mockup cannot predict every line around it
ok, _ = _check_expect(str(repo), ("printf 'noise\\nwanted\\nmore\\n'", ["wanted"]), 30)
assert ok, "extra output around the promised lines is not a mismatch"
# a command that fails still counts if it printed the promised lines — the
# exit code is the acceptance criterion's job, and it already ran
ok, _ = _check_expect(str(repo), ("echo boom; false", ["boom"]), 30)
assert ok

# an expect mismatch is an intent failure: the code works, it just isn't what
# was signed off, so `plexus why` must not file it under coding
from plexus.diagnose import _PHASE, classify_phase  # noqa: E402

assert _PHASE["expect_mismatch"] == "intent"
assert classify_phase("expect_mismatch", "pass", False) == "intent"

# --- review_hold is on out of the box --------------------------------------
# The whole surface is decoration if the riskiest commits still auto-land, so
# the default is the policy, not the empty tuple.
from plexus.spec import GoalSpec  # noqa: E402

assert set(GoalSpec.review_hold) == {"spine", "boundary"}

# --- opening the PR never costs the run ------------------------------------
# Every way this can be unavailable has to return a string, not raise: the goal
# is already committed by the time it runs, so a missing remote must not turn a
# finished goal into a failed one. No network is touched by any of these paths.
from plexus.run import _open_pr  # noqa: E402


class PRSpec:
    goal_id = "g1"
    pr_base = "main"


assert "no git remote" in _open_pr(PRSpec, repo, str(repo))
PRSpec.pr_base = ""
assert _open_pr(PRSpec, repo, str(repo)) == "", "pr_base='' opts out entirely"
PRSpec.pr_base = "main"
subprocess.run([*git, "checkout", "-qb", "main"], check=True)
assert "nothing to merge into main" in _open_pr(PRSpec, repo, str(repo))

print("plexus review self-check ok")
