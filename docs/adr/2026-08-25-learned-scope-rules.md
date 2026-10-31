---
status: accepted
date: 2026-08-25
approved-by: operator
drafted-by: operator
retire-evidence: a change in threat or requirement; replay is not valid evidence
switch: none
---
# Learned scope comes from refusals, widens to a directory, and never from probes

## Context and Problem Statement

A sandboxed task is refused paths its spec didn't grant. plexus records those
refusals so later attempts can be given the scope they need. How it learns
decides whether an agent can widen its own boundary.

## Decision Drivers

* An agent must not be able to grant itself access
* The learned scope must hold for the next attempt, not just the next file

## Considered Options

* Derive scope from kernel refusals only; widen a refused file to its directory; record `needed` and `probed` apart and never merge them
* Derive scope from what the agent says it needs: optimizing that produces an agent that asks for root
* Merge probes into permissions: an agent could widen its boundary by reaching past it repeatedly
* Grant the refused file only: the next file the task needs is refused too

## Decision Outcome

Chosen option: "refusals, directory widening, probes kept apart".

### Consequences

* Good, because a probe stays evidence that the prohibition works.
* Bad, because nothing enforces the learned scope yet: `run.py` dispatches
  without `allowed_paths`, and how to opt in is undecided.

### Confirmation

`src/plexus/scope.py` (`for_task`). Proven by `tests/test_scope.py`.
