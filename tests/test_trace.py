import hashlib
import json
import re

import pytest

from plexus import events, ledger

HEX = re.compile(r"^[0-9a-f]+$")
SPAN_KEYS = ("span_id", "trace_id", "parent_span_id")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    monkeypatch.chdir(tmp_path)


def _journal_line(tmp_path, kind):
    for f in (tmp_path / "journal").glob("**/*"):
        if f.is_file():
            for line in f.read_text().splitlines():
                if line.strip():
                    ev = json.loads(line)
                    if ev.get("kind") == kind:
                        return ev
    raise AssertionError(f"no {kind} event in journal")


def test_ids_deterministic():
    t = events.trace_for("план")
    s = events.attempt_span("план", "f1", 1)
    assert t == events.trace_for("план")
    assert s == events.attempt_span("план", "f1", 1)
    assert t == hashlib.sha256("plexus:план".encode("utf-8")).hexdigest()[:32]
    assert s == hashlib.sha256(
        "plexus:план:f1:1".encode("utf-8")
    ).hexdigest()[:16]
    assert len(t) == 32 and len(s) == 16
    assert HEX.fullmatch(t) and HEX.fullmatch(s)
    assert t != events.trace_for("p2")
    assert s != events.attempt_span("p2", "f1", 1)
    assert s != events.attempt_span("план", "f2", 1)
    assert s != events.attempt_span("план", "f1", 2)


def test_emit_span_fields_top_level(tmp_path):
    events.emit("x.started", span_id="s", trace_id="t", parent_span_id="p")
    ev = _journal_line(tmp_path, "x.started")
    assert (ev["span_id"], ev["trace_id"], ev["parent_span_id"]) == ("s", "t", "p")
    payload = ev.get("payload") or {}
    assert not any(k in payload for k in SPAN_KEYS)


def test_ledger_record_forwards_span(tmp_path):
    rec = ledger.record("feature.started", goal_id="g", feature_id="f",
                        root=tmp_path, attempt=1, span_id="s", trace_id="t")
    assert rec["span_id"] == "s" and rec["trace_id"] == "t"
    ev = _journal_line(tmp_path, "feature.started")
    assert ev["span_id"] == "s" and ev["trace_id"] == "t"
    payload = ev.get("payload") or {}
    assert not any(k in payload for k in SPAN_KEYS)
