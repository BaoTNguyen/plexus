---
status: accepted
date: 2026-10-13
approved-by: operator
drafted-by: operator
retire-evidence: a change in threat or requirement; replay is not valid evidence (no adversarial tasks)
switch: HEART_SANDBOX=off (operator env)
---
# plan, approve and run refuse unless HEART_SANDBOX is docker-sbx or off

## Context and Problem Statement

An unset `HEART_SANDBOX` meant "run on this host" without a word: four web-lane
episodes ran unsandboxed unnoticed (F71), and approve judged criteria in the
wrong environment (F79). heart contains only `docker-sbx` and runs any other
value on the host, so `1` or a typo was a host run too (PLEXUS-B14, 2026-10-22).

## Decision Drivers

* A forgotten setting must stop the run, not widen it
* Running on the host stays possible, as a choice made out loud

## Considered Options

* Refuse unless the value is `docker-sbx` or `off`; `off` warns
* Refuse only when unset (2026-10-13): `1` or a typo still ran on the host (B14)

Other options were not recorded at the time.

## Decision Outcome

Chosen option: "refuse unless `docker-sbx` or `off`", because the only way onto
the host is to say so.

### Consequences

* Good, because a misconfigured box stops instead of running agents unsandboxed.
* Bad, because every command needs the variable set, tests included.

### Confirmation

`src/plexus/sandbox.py::gate`, used by the CLI and the dashboard's approve.
Proven by `tests/test_sandbox_gate.py::test_only_docker_sbx_or_off_passes`.
