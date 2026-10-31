---
status: accepted
date: 2026-07-21
approved-by: operator
drafted-by: operator
retire-evidence: a second user, or a remote operator, on the same stack
switch: none
---
# plexus keeps warren's goal spine and drops its sandbox and HTTP control plane

## Context and Problem Statement

plexus is modelled on warren's Plot: shape, plan, sign-off, serial run,
activity log. warren also ships its own container sandbox and a bearer-authed
HTTP control plane. plexus runs on a single-user local stack whose episode
runner already owns isolation.

## Decision Drivers

* One owner per concern: isolation already has one
* A single local user needs no authenticated remote surface

## Considered Options

* Adopt the durable spine per goal, serial dispatch gated on the previous child
  landing, resume from the next open child, and the sign-off gate that arms
  execution; drop the sandbox and the HTTP control plane
* Adopt warren whole, sandbox and bearer-authed control plane included
* A web UI that drives the autonomous loop itself

## Decision Outcome

Chosen option: "adopt the spine, drop the sandbox and control plane", because
isolation belongs to the episode runner and nothing here is remote. The web UI
came back for supervision only (`plexus serve`): it reads the ledger and calls
the same code the CLI does.

### Consequences

* Good, because state stays in `.vascular/plexus/*.jsonl`, and the dashboard is a lens on it.
* Bad, because a second user or a remote operator would need the control plane rebuilt.

### Confirmation

`src/plexus/run.py` (serial loop, resume), `src/plexus/plan.py` (sign-off gate),
`src/plexus/serve.py` (supervision surface over the ledger).
