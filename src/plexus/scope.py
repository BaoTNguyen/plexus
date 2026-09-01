"""What an episode's sandbox refused, distilled into the ledger.

Heart emits `sandbox.denied` when a container refuses writes the task spec had
permitted, and `guardrail.hit` when an agent probes ground the spec forbade.
Both reach the journal and, until this module, nothing read them: the correction
they carry was discarded with the journal's day-scale rotation.

Read once, soon, then keep the distillation -- following observe.py's rule that
"insights come from the ledger, not the journal". A journal scan at plexus's
multi-week horizon silently returns less the older the question gets, which is
the worst way for a learning loop to fail: it looks like the system stopped
making mistakes.

What is recorded is what the kernel refused, never what an agent asked for.
Deriving scope from a request teaches an agent that asking for more is cheaper
than working within less; deriving it from a refusal measures what happened.
"""
from __future__ import annotations

import re
from pathlib import Path

from .ledger import read, record

#: journal events that say something about scope, and what each one means
_DENIED = "sandbox.denied"      # the sandbox refused ground the spec permitted
_PROBED = "guardrail.hit"       # the agent reached for ground the spec forbade

#: paths named inside a refusal line, quoted or bare
_PATH = re.compile(r"""['"]([^'"\n]{1,200})['"]|(?<![\w/])([\w.-]+/[\w./-]+)""")


def _paths_in(text: str) -> list[str]:
    return [(a or b).lstrip("/") for a, b in _PATH.findall(text) if (a or b)]


def _directory(path: str) -> str:
    """The directory a refused file sits in.

    A spec granting `src/app.py` grants one file and refuses the next one the
    same task needs. The directory is the smallest widening that is likely to
    hold, and it is still far narrower than the unrestricted default plexus
    dispatches with today.
    """
    parent = str(Path(path).parent)
    return "" if parent in (".", "/") else parent


def observe(task_id: str, *, goal_id: str, feature_id: str | None = None,
            root: str | Path = ".") -> dict | None:
    """Distil one episode's scope events into the ledger. Returns what it found.

    Call this while the episode's events are still in the journal -- directly
    after the run, not on a later query. Best-effort: heart may be absent, or
    the journal may have rotated, and neither should fail a goal.
    """
    try:
        from heart.pulse import load_events
        events = load_events(task=task_id)
    except Exception:
        return None  # no journal to read; the ledger keeps what it already had

    needed: set[str] = set()
    probed: set[str] = set()
    for event in events:
        payload = event.get("payload") or {}
        if event.get("kind") == _DENIED:
            for line in payload.get("evidence") or []:
                needed.update(d for p in _paths_in(line) if (d := _directory(p)))
        elif event.get("kind") == _PROBED:
            probed.update(payload.get("paths") or [])

    if not needed and not probed:
        return None

    found = {"task_id": task_id, "needed": sorted(needed), "probed": sorted(probed)}
    record("scope.observed", goal_id=goal_id, feature_id=feature_id, root=root, **found)
    return found


def for_task(task_id: str, *, root: str | Path = ".") -> dict[str, list[str]]:
    """Everything the ledger has learned about this task's scope.

    `allow` accumulates across attempts because a task can be refused twice in
    different places, and the second refusal does not retract the first.

    `deny` is separate and never merges into `allow`: a path the agent was
    caught probing is evidence the prohibition is working, not evidence the
    scope was too tight. Folding the two would let an agent widen its own
    boundary by repeatedly reaching past it.
    """
    allow: set[str] = set()
    deny: set[str] = set()
    for rec in read(root):
        if rec.get("kind") != "scope.observed" or rec.get("task_id") != task_id:
            continue
        allow.update(rec.get("needed") or [])
        deny.update(rec.get("probed") or [])
    return {"allow": sorted(allow), "deny": sorted(deny)}
