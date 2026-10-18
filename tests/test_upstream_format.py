"""needs_upstream must be `module.path` or `module.path:Symbol` (F89).

A planner wrote a call signature there; the run's import check can never
satisfy one, so the feature escalated upstream_not_ready forever.
"""
from plexus.plan import upstream_problems


def test_import_forms_are_fine():
    assert upstream_problems(["heart.events", "heart.events:emit", "a_b.c:D_e"]) == []


def test_signatures_and_junk_are_refused():
    bad = ["heart.events.emit(source, kind, *, span_id=None)", "heart.events:emit()",
           "heart events", "", "heart.events:", ":emit"]
    assert len(upstream_problems(bad)) == len(bad)
