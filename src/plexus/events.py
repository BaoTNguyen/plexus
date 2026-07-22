"""Spine emission for plexus, conforming to heart/SPINE.md.

Plexus already imports heart as a library (marrow's precedent), so it uses
heart's emitter rather than duplicating the ~40 lines. "plexus" is an
additive source value, which SPINE rule 1 permits.

Correlation: the spine's hierarchy tops out at episode_id; plexus sits above
it. Goal lineage rides two paths: the task_id naming convention
<goal_id>-<feature_id>-a<attempt> (threads through heart/arteries events with
zero changes there), and explicit goal_id/feature_id payload fields on
plexus's own events.
"""
from __future__ import annotations

from heart.events import emit as _emit

SOURCE = "plexus"


def make_task_id(goal_id: str, feature_id: str, attempt: int) -> str:
    return f"{goal_id}-{feature_id}-a{attempt}"


def emit(
    kind: str,
    *,
    goal_id: str | None = None,
    feature_id: str | None = None,
    task_id: str | None = None,
    episode_id: str | None = None,
    duration_ms: int | None = None,
    **payload,
) -> None:
    if goal_id:
        payload["goal_id"] = goal_id
    if feature_id:
        payload["feature_id"] = feature_id
    _emit(SOURCE, kind, task_id=task_id, episode_id=episode_id,
          duration_ms=duration_ms, **payload)
