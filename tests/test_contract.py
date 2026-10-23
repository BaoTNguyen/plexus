"""The black-box contract (F27, DIRECTION §13): contract/cli.json run against
the plexus executable ($PLEXUS_BIN), every ledger record checked against
contract/ledger.json. The cases are data so a v2 in another language can be
judged by the same file; this module is only the runner."""
import json
import os
import shutil
import subprocess
import sys
import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
LEDGER = json.loads((HERE / "contract" / "ledger.json").read_text())
CLI = json.loads((HERE / "contract" / "cli.json").read_text())
BIN = os.environ.get("PLEXUS_BIN") or shutil.which("plexus", path=str(Path(sys.executable).parent))


def problems(rec: dict) -> list[str]:
    missing = [f for f in LEDGER["common"] if f not in rec]
    kind = LEDGER["kinds"].get(rec.get("kind"), {"required": []})
    missing += [f for f in kind["required"] if f not in rec]
    out = [f"{rec.get('kind')}: missing {f}" for f in missing]
    try:
        if datetime.datetime.fromisoformat(rec["ts"]).utcoffset() is None:
            out.append(f"{rec.get('kind')}: ts has no offset")
    except (KeyError, TypeError, ValueError):
        out.append(f"{rec.get('kind')}: ts is not a timestamp")
    return out


def _ledger(repo: Path) -> list[dict]:
    p = repo / ".vascular" / "plexus" / "ledger.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.parametrize("case", CLI["cases"], ids=[c["name"] for c in CLI["cases"]])
def test_cli_case(case, tmp_path):
    assert BIN, "no plexus executable: set PLEXUS_BIN"
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "VASCULAR_HOME": str(tmp_path / "home"),
           "EVENT_JOURNAL_DIR": str(tmp_path / "journal"), "HEART_PULSE": "file",
           **case.get("env", {})}

    def run(*args, check=True):
        return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, check=check)

    run("git", "init", "-q")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    run(BIN, "init")
    setup = case.get("setup", {})
    state = repo / ".vascular" / "plexus"
    state.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(state / "ledger.jsonl", "a") as f:
        for rec in setup.get("ledger", []):
            f.write(json.dumps({"ts": now, "goal_id": CLI["goal_id"], **rec}) + "\n")
    if "plan" in setup:
        (state / "plan.jsonl").write_text("".join(json.dumps(p) + "\n" for p in setup["plan"]))
    before = len(_ledger(repo))

    for step in case["steps"]:
        got = run(BIN, *step["args"], check=False)
        where = f"plexus {' '.join(step['args'])}\nstdout: {got.stdout}\nstderr: {got.stderr}"
        assert got.returncode == step["exit"], where
        for want in step.get("stdout_has", []):
            assert want in got.stdout, where
        for want in step.get("stderr_has", []):
            assert want in got.stderr, where

    records = _ledger(repo)
    added = records[before:]
    assert [r["kind"] for r in added] == case["appended"]
    assert [r["kind"] for r in added if r.get("operator") is True] == case.get("operator", [])
    assert [p for r in records for p in problems(r)] == []


def test_the_real_ledgers_meet_the_contract():
    """Every ledger on this machine, when there are any (none in CI)."""
    paths = list(Path.home().glob("Coding/Projects/*/.vascular/plexus/ledger.jsonl"))
    if not paths:
        pytest.skip("no real ledgers here")
    bad = [f"{p.parent.parent.parent.name}: {x}" for p in paths
           for r in map(json.loads, p.read_text().splitlines()) for x in problems(r)]
    assert bad == []
