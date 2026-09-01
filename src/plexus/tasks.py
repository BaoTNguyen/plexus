"""Tasks: the unit of work between the charter and an episode.

The charter (plexus.toml) is standing — it says what the project is and how it
is built, and it never closes. Tasks come and go: one arrives from a GitHub
issue or by hand, gets planned into features, runs, and lands. Keeping them
apart is the whole point of this file. Before it, `[source]` lived on the goal,
so the project's charter and one closable work item shared a slot and the UI
could not tell you which one you were editing.

Storage is one JSON object per line in `.plexus/tasks.jsonl`, append-only, last
write per id wins — the same habit as `ledger.py`, for the same reasons: it is
diffable in git, it survives a torn tail, and a concurrent append under
PIPE_BUF is atomic without a lock.

Execution is sequential and gated: a task is ready only when every task it is
blocked by has landed. One in flight per project, which is what makes a human
review of every line tractable.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

STATES = ("open", "planning", "ready", "running", "blocked", "landed", "closed")
#: terminal states — a blocker in one of these no longer blocks
DONE = ("landed", "closed")


def tasks_path(root: str | Path = ".") -> Path:
    return Path(root) / ".plexus" / "tasks.jsonl"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def slug(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def read(root: str | Path = ".") -> list[dict]:
    """Every task, folded to its latest version, in insertion order.

    A torn last line is skipped rather than fatal: the file is appended to by a
    process that can be killed mid-write, and losing the whole board because of
    one truncated record would be the worse failure.
    """
    path = tasks_path(root)
    folded: dict[str, dict] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            folded[rec["id"]] = {**folded.get(rec["id"], {}), **rec}
    return list(folded.values())


def _append(root: str | Path, rec: dict) -> dict:
    """Append one record, healing a torn tail first.

    A process killed mid-write leaves a line with no newline on it. Appending
    straight onto that fuses the two into one unparseable line, so the torn
    record takes a good one down with it — `read` skips junk, but only if the
    junk ends. Writing a separator when the file does not end in a newline
    keeps the damage to the record that was actually lost.
    """
    path = tasks_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        if fh.tell():
            fh.seek(fh.tell() - 1)
            if fh.read(1) != "\n":
                fh.write("\n")
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def create(root: str | Path, title: str, *, body: str = "",
           source_kind: str = "manual", source_url: str = "",
           blocked_by: list[str] | None = None,
           requires_plan: bool = True) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("a task needs a title")
    existing = read(root)
    taken = {t["id"] for t in existing}
    unknown = set(blocked_by or []) - taken
    if unknown:
        raise ValueError(f"unknown blocker(s): {', '.join(sorted(unknown))}")
    now = _now()
    return _append(root, {
        "id": slug(title, taken), "title": title, "body": body,
        "source_kind": source_kind, "source_url": source_url,
        "state": "open", "blocked_by": list(blocked_by or []),
        # a task big enough to need decomposition stays out of `ready` until it
        # has a plan; a one-step task runs as a single feature. Seeds draws the
        # same line between an epic and a bug.
        "requires_plan": bool(requires_plan), "plan_id": "",
        # Program design lives here, not on the charter. Types and signatures
        # are decided per piece of work; putting them on the project meant one
        # task's shape was recorded as if it governed every other.
        "design_types": [], "design_interfaces": [], "design_call_paths": [],
        # why a task is stuck, and which PR carried it out. A PR is usually
        # several landed tasks, so the number lives on the task, not the goal.
        "error": "", "pr": 0,
        "order": len(existing), "created": now, "updated": now, "reason": "",
    })


def update(root: str | Path, task_id: str, **fields) -> dict:
    current = {t["id"]: t for t in read(root)}
    task = current.get(task_id)
    if task is None:
        raise ValueError(f"no task {task_id!r}")
    allowed = {"title", "body", "state", "blocked_by", "requires_plan",
               "plan_id", "order", "reason", "error", "pr",
               "design_types", "design_interfaces", "design_call_paths"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot set: {', '.join(sorted(unknown))}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"unknown state {fields['state']!r}")
    if "blocked_by" in fields:
        blockers = list(fields["blocked_by"])
        if task_id in blockers:
            raise ValueError("a task cannot block itself")
        missing = set(blockers) - set(current)
        if missing:
            raise ValueError(f"unknown blocker(s): {', '.join(sorted(missing))}")
        if _cycles({**current, task_id: {**task, "blocked_by": blockers}}):
            raise ValueError("that dependency would make a cycle")
    return _append(root, {**task, **fields, "id": task_id, "updated": _now()})


def _cycles(by_id: dict[str, dict]) -> bool:
    """Depth-first cycle check. Without it a pair of tasks can block each other
    and `ready` goes quietly empty forever, which reads as 'nothing to do'."""
    colour: dict[str, int] = {}

    def visit(node: str) -> bool:
        if colour.get(node) == 1:
            return True
        if colour.get(node) == 2:
            return False
        colour[node] = 1
        for nxt in by_id.get(node, {}).get("blocked_by", []):
            if nxt in by_id and visit(nxt):
                return True
        colour[node] = 2
        return False

    return any(visit(t) for t in by_id)


def blockers(root: str | Path, task: dict, by_id: dict[str, dict] | None = None
             ) -> list[str]:
    """Which of this task's blockers are not done yet."""
    if by_id is None:
        by_id = {t["id"]: t for t in read(root)}
    return [b for b in task.get("blocked_by", [])
            if by_id.get(b, {}).get("state") not in DONE]


def ready(root: str | Path = ".") -> list[dict]:
    """Tasks that could run right now, in the order they should.

    Sequential and gated: every blocker must have landed or been closed. A task
    that still needs decomposition is excluded — it is not work yet, it is a
    planning job, and surfacing it here is how an agent ends up implementing an
    epic in one turn.
    """
    all_tasks = read(root)
    by_id = {t["id"]: t for t in all_tasks}
    out = [t for t in all_tasks
           if t.get("state") in ("open", "ready")
           and not blockers(root, t, by_id)
           and (not t.get("requires_plan") or t.get("plan_id"))]
    return sorted(out, key=lambda t: (t.get("order", 0), t["created"]))


def next_task(root: str | Path = ".") -> dict | None:
    """The one task to run next, or None. Nothing new starts while something is
    in flight — that is the gate, and it is what keeps review tractable."""
    if any(t.get("state") in ("running", "planning") for t in read(root)):
        return None
    queue = ready(root)
    return queue[0] if queue else None


def board(root: str | Path = ".") -> dict:
    """Everything the tasks tab renders, computed once."""
    all_tasks = read(root)
    by_id = {t["id"]: t for t in all_tasks}
    rows = []
    for task in sorted(all_tasks, key=lambda t: (t.get("order", 0), t["created"])):
        waiting = blockers(root, task, by_id)
        rows.append({
            **task,
            "waiting_on": waiting,
            "runnable": (task.get("state") in ("open", "ready") and not waiting
                         and (not task.get("requires_plan") or task.get("plan_id"))),
            "needs_plan": bool(task.get("requires_plan")) and not task.get("plan_id"),
        })
    nxt = next_task(root)
    return {"tasks": rows, "next": nxt["id"] if nxt else "",
            "in_flight": [t["id"] for t in all_tasks
                          if t.get("state") in ("running", "planning")]}


def group(root: str | Path = ".") -> dict:
    """The four buckets the tasks tab shows: done, active, blocked, planned.

    Named for what you want to know at a glance — what shipped, what is moving,
    what is stuck and needs me, what is queued — rather than for the underlying
    states, which are an implementation detail nobody asks a board about.
    """
    rows = board(root)
    buckets: dict[str, list] = {"active": [], "blocked": [], "planned": [], "done": []}
    for task in rows["tasks"]:
        state = task.get("state")
        if state in DONE:
            buckets["done"].append(task)
        elif state == "blocked" or task.get("error"):
            buckets["blocked"].append(task)
        elif state in ("planning", "running"):
            buckets["active"].append(task)
        else:
            buckets["planned"].append(task)
    return {**rows, **buckets}


def demo() -> None:
    """Self-check: ordering, gating, cycle refusal, and the plan requirement."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = create(root, "Add seeds store", requires_plan=False)
        b = create(root, "Wire tasks tab", blocked_by=[a["id"]], requires_plan=False)
        c = create(root, "Rework charter", requires_plan=True)
        assert a["id"] == "add-seeds-store", a["id"]

        # b is gated on a; c needs a plan first. Only a is runnable.
        assert [t["id"] for t in ready(root)] == [a["id"]], ready(root)
        assert next_task(root)["id"] == a["id"]

        # nothing new starts while one is in flight
        update(root, a["id"], state="running")
        assert next_task(root) is None, "gate let a second task start"

        # landing a unblocks b
        update(root, a["id"], state="landed")
        assert [t["id"] for t in ready(root)] == [b["id"]], ready(root)

        # c joins the queue once it has a plan
        update(root, c["id"], plan_id="p1")
        assert {t["id"] for t in ready(root)} == {b["id"], c["id"]}

        # cycles are refused rather than silently emptying the queue
        try:
            update(root, a["id"], blocked_by=[b["id"]])
            raise AssertionError("cycle accepted")
        except ValueError as exc:
            assert "cycle" in str(exc), exc

        # a duplicate title gets its own id, never collides
        d = create(root, "Add seeds store")
        assert d["id"] == "add-seeds-store-2", d["id"]

        # a torn tail line must not lose the board
        with open(tasks_path(root), "a") as fh:
            fh.write('{"id": "torn", "title": "hal')
        assert len(read(root)) == 4, read(root)

        assert board(root)["next"] == b["id"]

        # the four buckets sort by what you want to know, not by raw state
        update(root, b["id"], state="running")
        update(root, c["id"], state="blocked", error="needs an API key")
        g = group(root)
        assert [t["id"] for t in g["done"]] == [a["id"]], g["done"]
        assert [t["id"] for t in g["active"]] == [b["id"]], g["active"]
        assert [t["id"] for t in g["blocked"]] == [c["id"]], g["blocked"]
        assert [t["id"] for t in g["planned"]] == [d["id"]], g["planned"]
    print("tasks self-check ok")


if __name__ == "__main__":
    demo()
