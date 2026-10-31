# Operating plexus

The parts of running plexus that a first goal doesn't need: seats, the fleet,
goals that need a change in another repo, the project menu, and what plexus
records about each run. Start with the [README](../README.md).

## Seats and the credential injector

Each seat's secrets live under `seat_secrets()`, rooted at
`~/.vascular/secrets/heart` (`VASCULAR_HOME` overrides the
`~/.vascular` root) — the credential injector reads from there rather than
trusting anything the sandboxed agent process can see or set.

The injector's proxy intercepts outbound TLS, so it needs its own CA and a
matching proxy cert; both live under `seat_secrets()/tls`. To rotate them,
delete that `tls/` directory and run `plexus doctor --fix` — it regenerates
the CA and cert and restarts the proxies to pick them up.

## Running several goals: the fleet

Because one dashboard (`plexus serve`) scans a parent of several goals, it is also where the
fleet shares scarce resources. Three live knobs stamp into every run it
launches: **local slots** (`HEART_LOCAL_SLOTS`, heart's cross-process cap on
agents hitting the one local model server), **global agents**
(`HEART_MAX_AGENTS_GLOBAL`, a cap on all agents for a shared paid key or
throttled subscription seat), and **max goals** (how many `Run all` starts at
once). The caps are heart's mechanism; the dashboard is just the control
surface, so a goal launched from a terminal with the same env is bounded the
same way.

To advance the fleet without a browser, `plexus fleet run` is the headless twin
of `Run all`: it starts every approved, idle, unblocked goal (skipping any a
human still owns via an open escalation), up to `--max-goals` (default 3),
carrying the same caps, and exits once the fleet is drained. It honors a rolling
`--cost-ceiling` (non-local spend over `--cost-window-hours`; local models are
free, subscription seats counted at API rates) and an optional `--run-window`.
Because it exits when drained and one run holds each goal's flock, a `oneshot`
timer never overlaps itself:

```ini
# ~/.config/systemd/user/plexus-fleet.service   (Type=oneshot)
[Service]
ExecStart=%h/.local/bin/plexus fleet run --root %h/projects \
          --max-goals 3 --cost-ceiling 5 --run-window 22:00-08:00
# ~/.config/systemd/user/plexus-fleet.timer  ->  OnCalendar=*:0/30  (every 30 min)
```

## Goals that need another repo

There is no cross-repo orchestration and there should not be: a goal runs in one
repo. A feature that needs a change upstream is split and ordered by hand.

1. Land the upstream change in its own repo, on `dev`.
2. Bump `tests/test_heart_api_pin.py` to pin the new surface. It fails in
   plexus's own suite the moment the seam moves, so it is the integration gate.
3. Only then run the downstream feature.

CI enforces the order rather than trusting it: a PR into `main` is tested against
heart's `main`, while `dev` is tested against heart's `dev`. A plexus change that
depends on unlanded heart work goes green on `dev` and red on the PR.

Every repo in the stack carries the same workflow and the same matrix rule, each
checking out the siblings it actually imports — capillaries alone, arteries with
capillaries, heart with both, plexus with heart. marrow imports nothing and runs
by itself. So the ordering constraint is enforced at every edge of the chain, not
only the one plexus happens to sit on.

What plexus automates is the *detection and the seeding*, not the orchestration.
A feature may declare `needs_upstream = ["heart.taskspec:TaskSpec.foo", …]` —
modules or `module:Symbol` names it depends on. Before dispatching, `run` checks
each against the live editable installs; a miss raises an `upstream_not_ready`
escalation naming the module, instead of burning attempts on a run that cannot
pass — the same discipline as the pin test, now caught at dispatch rather than
by a red suite.

Then it seeds the fix. A registry maps each top-level package to the repo that
provides it — and it wires itself. Every repo plexus tracks already names its
package in `pyproject.toml`, so the map is *derived* from the same workspace the
menu scans; `registry.json` is only for explicit pins and out-of-tree repos:

```json
// ~/.vascular/config/plexus/workspace.json  — the menu + the derived registry
{"roots": ["/path/to/projects"]}
// ~/.vascular/config/plexus/registry.json   — optional overrides (win over derived)
{"heart": "/path/to/projects/heart"}
```

When a downstream goal declares a missing upstream symbol, `run` writes a goal
into the owning repo — a ready-to-plan `plexus.toml` if it has none, or an
`upstream.requested` ledger record if it already carries its own goal (never
clobbering it). The request is idempotent and names who asked for what. So the
menu's other projects light up with the upstream work a downstream goal needs,
without you remembering to file it. The goal still runs in one repo; only the
gate and the hand-off are automatic. (See `src/plexus/registry.py`.)

## Adding projects: the workspace

The menu is a workspace of project directories, the way an IDE opens folders. A
root is a single repo or a parent of several; `plexus serve`/`report`/`fleet`
scan all of them. Two ways to grow it:

- **Drop a repo under a workspace root** — `cd newthing && plexus init`. It
  appears on the next scan and its package wires into the registry from its
  pyproject. Nothing to edit.
- **`plexus add <path>`** — register a repo living anywhere else (the "Add
  Folder to Workspace" move); the dashboard has the same input in its header.

The derived registry means a new project is import-resolvable for cross-repo
seeding the moment it's in the workspace — no second step.

In `plexus serve`, the menu is a **grid** (the mission-control shape): a search
box, a **pinned** section, and projects **grouped by a label** you assign
(colored dot derived from the label). Each group and the pinned section carry a
**Run-all** that advances only that group's idle goals — so you can run one
project at a time, exactly the split you asked for. Labels and pins are view
state in `workspace.json` (`projects: {path: {label, pinned}}`), never in the
goal repo's ledger. Like mission-control, plexus only ever *references* a repo —
`add`/label/unpin touch the workspace file, never the project's files.

## Observability

Heart already implements the SRE-book stack for this ecosystem: an append-only
NDJSON event spine (`heart/SPINE.md` is the wire canon — additive-only kinds,
tolerant readers, emission never raises) read by `heart observe health` (symptom
check, exit code as the alert primitive), `heart observe episode <id>` (cause
drill-down), and `heart observe insights` (golden-signal dashboard, percentiles never
averages). Plexus builds none of that again. It joins the spine:

- **Emit via `heart.events.emit(source="plexus", ...)`** — plexus already
  imports heart, and "plexus" is an additive source value, which SPINE rule 1
  permits.
- **Event kinds** (additive to the catalog): `goal.started/finished`,
  `plan.created/approved`, `feature.started/finished/failed`,
  `acceptance.round` (attempt, passed — mirrors `verify.round`),
  `escalation.raised/resolved`, `budget.consumed`.
- **Correlation without touching heart**: the spine's hierarchy tops out at
  `episode_id`; plexus sits above it. Rather than adding fields to heart's
  emitter, plexus names task_ids deterministically —
  `<goal_id>-<feature_id>-a<attempt>` — so every heart/arteries event already
  carries the goal lineage in `task_id`, and the ledger maps feature →
  episode_ids for exact joins. If the naming convention ever proves clumsy,
  the upgrade is an additive `PLEXUS_GOAL_ID` env fallback in heart's
  `emit()`, four lines, allowed by the spine's rules — but not before.

One boundary heart's model must not blur: **the spine is telemetry, never a
system of record.** `emit()` swallows every exception by design — right for
observability, disqualifying for state. Heart can afford that because losing
its journal loses nothing; plexus cannot, because a dropped event read back as
state would skip a feature or re-run a landed one. So the write order is
fixed: ledger first (must succeed), spine second (best effort), and `plexus
status` decides state from the ledger alone — the journal only supplies the
staleness signal (running per ledger, but no episode events lately).

The golden signals move up one level, and that is the actual division of
observability labor between the two repos:

| Golden signal | heart (episode level) | plexus (goal level) |
| --- | --- | --- |
| Latency | role/episode durations, p50/p95 | feature lead time (dispatch → landed), episodes-per-feature |
| Traffic | episodes + turns per window | features landed, goals active per window |
| Errors | episode.failed, verify failures | escalations raised, acceptance failures, budget exhaustion |
| Saturation | verifier timeouts, fix-round exhaustion | budget consumed vs ceiling, retry pressure per feature |

Symptoms vs causes, same split as pulse: **`plexus status` is the symptom
check** — exit 0 (progressing or done), 1 (escalations waiting), 2 (stalled:
ledger says running but no episode events for N minutes — the goal-level
zombie, which heart's episode-level zombie rule cannot see). **Cause
drill-down is delegated**: `plexus status` prints the failing feature's
episode_ids so the next command is `heart observe episode <id>`, not a plexus-specific
viewer. Goal-level insights (lead times, escalation rate, rescue rate after
retry) come from the **ledger**, not the journal: the ledger is the system of
record for a multi-week goal, and the journal's retention is not. Ledger for history, journal for the live
window — `plexus insights` stays a small read-side function either way.

### What went wrong: the judgment surface

`plexus status` tells you *that* a feature is stuck; `plexus why [feature]`
tells you *what kind* of stuck, so a semi-autonomous run stays reviewable and
your attention lands where taste is actually needed. It reads the ledger and
prints, per failed or escalated feature: the **intent** it was judged against
(the acceptance criterion), each attempt's failure attributed to a phase, the
escalation, a phase verdict, and — because it stores pointers, never copies —
a `heart observe episode <id>` drill-down to the raw **logs, traces, and diff** for
every attempt. Manual verification is always one command deeper.

Failures attribute to the 40/20/40 phases (`src/plexus/diagnose.py`):

- **coding** — the agent fumbled producing or landing a valid change
  (`no_change`, `apply_failed`, `path_violation`, `review_rejected`, …).
- **testing** — a check caught a defect (`verify_failed`, `acceptance_failed`).
- **intent** — the goal or criterion itself was wrong (plan-time
  `unverifiable_ground_truth`, scope-level `regression`).

`plexus insights` rolls these up as *failures by phase* — the real signal
40/20/40 is after: defects clustered in `intent` mean the plan phase is
under-invested, not that the coder is bad.

**How intent gets separated from code.** Heart judges each episode with the
repo's own suite (`detect_verifiers`) — its outcome stays a pure
regression/correctness signal — while `run.py` runs the feature's acceptance
criterion in its *own* worktree, outside the episode. The two judgments are
recorded separately (`acceptance.round.passed` + `episode_outcome`) and can
disagree, which is the whole point of the 2×2 above. Landing requires *both*:
the criterion passes and nothing regressed — a diff that meets the feature but
breaks the existing suite is caught as `testing` and never lands. Acceptance is
deliberately kept out of heart's verifiers: routing the criterion through them
would merge the two judgments, which LEDGER law 5 forbids. That separation is
also what manufactures marrow's hard negative — an episode that passed heart
but failed acceptance — which cannot exist while the two are one command.

Heart's alerting assumes someone at the terminal (exit code as the alert
primitive); plexus's premise is unattended operation, so an escalation that
waits to be polled defeats autonomy. Plexus therefore owns one push channel:
`notify_cmd` in `plexus.toml` (e.g. `notify-send`, `mail`, a webhook curl),
fired on escalation and goal completion. This deliberately stays out of
heart — heart runs while you watch; plexus runs while you sleep.

Finally, because plexus is the integrator, it owns the factory-wide vantage
no single organ has: `plexus stack` rolls up the shared journal by source —
event volume, failures, and store degradation across heart, arteries,
capillaries, marrow, and plexus in one view. Per-source depth stays with each
repo's own tools; stack answers only "which organ is unhealthy," then hands
off.

The dashboard also carries a **live** tab per goal: while a run works, it
polls and virtualizes that goal's spine events — role turns, retrieval, verifier rounds,
land — filtered to the goal's lineage across heart/arteries/capillaries,
so supervision isn't blind between ledger writes. It's the structured,
event-sourced answer to "watch the agent," not a raw terminal (plexus has no PTY
to show); the ledger stays the coarse, durable record, the stream the live one.

Where `stack` reads the live journal (heart's day horizon), `plexus report` reads
the durable ledgers across the whole menu — one row per goal with the three
numbers a multi-project fleet is judged on: non-local spend, median feature
lead time, and escalation rate, plus a fleet total. `insights` is the
single-goal deep read; `report` is the across-projects scoreboard.
