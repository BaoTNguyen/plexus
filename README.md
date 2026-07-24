# plexus

Autonomous product loop on top of the heart/arteries/capillaries stack. Give it
goals, context, and ground truth; it decomposes the goal into features,
dispatches them to heart one at a time, verifies each against the ground truth,
and iterates until the scope is satisfied or a human needs to decide something.

## Why "plexus"

A plexus is a branching junction of nerves and vessels where signals are
integrated and redistributed to the organs — the solar plexus coordinates, it
doesn't pump or filter. That is exactly this repo's job: it decides *what* the
body builds and routes intent downward, while heart executes, arteries
remembers, capillaries retrieves, and marrow learns. No metaphor collision with
any sibling.

```text
plexus       goal decomposition + acceptance loop   <- this repo
heart        orchestration + environment + reward
arteries     memory + trace substrate
capillaries  prompt/skill retrieval
marrow       RL training on heart's episodes
```

## What sets it apart

Most autonomous-agent demos are a while-loop around a chat model that runs until
it declares victory. Plexus is built for the case where you actually leave it
running:

- **The ledger is the system of record, not the chat log.** State lives in a
  fsynced JSONL file that survives a crash; `plexus run` resumes at the next open
  feature instead of restarting the goal. The event spine is telemetry only, so a
  dropped event never re-runs a landed feature or skips one.
- **"Done" is defined before the run, not decided by the model.** You declare
  ground truth (tests, I/O examples, or prose the planner compiles into checks),
  and a feature lands only when its acceptance criterion passes and nothing in the
  existing suite regressed. The model never grades its own homework.
- **It tells you what kind of stuck.** `plexus why` attributes every failure to a
  phase — coding, testing, or intent — so a bad plan reads differently from a bad
  diff. Escalations arrive as decisions with options, not stack traces.
- **A human gate where errors are cheapest.** The plan requires sign-off before
  anything runs, and unverifiable or contradictory ground truth blocks that gate
  rather than getting silently guessed past.
- **It runs while you sleep.** A `notify_cmd` fires on escalation or completion,
  so unattended operation doesn't mean polling a terminal.
- **Every goal makes the stack smarter.** Retries become same-task pass/fail
  pairs, acceptance failures become hard negatives heart can't see alone, and
  plan outcomes become a trainable signal — all keyed by task_id for marrow, as a
  byproduct of running.

## plexus vs heart — the boundary

Both orchestrate agents, so the line must be sharp or the repos bleed into
each other:

| | heart | plexus |
| --- | --- | --- |
| Unit of work | episode: one TaskSpec → one scored diff | goal: many episodes → satisfied scope |
| State | stateless between invocations; runs dir is output, not state | durable state machine; `.plexus/` ledger survives crashes and resumes |
| Question answered | "was this task done correctly?" | "is the scope satisfied yet, and what's next?" |
| Time horizon | minutes | hours to days |
| Correctness source | verifiers + reviewer on the given task | ground truth on the whole goal |
| Second job | RL environment for marrow — must stay deterministic and repeatable | demand generator — its retries and failures become tasks for marrow's data engine |

Heart is mechanism, plexus is intent. Anything about *how* an agent runs
(worktrees, roles, fix rounds, scoring, candidates) belongs in heart; anything
about *what to build next and when to stop* belongs here. Episodes are
disposable; the plan and ledger are the durable spine of a goal. If a feature
in plexus ever wants to manage a worktree or parse test output, it is heart
code in the wrong repo.

## System summary

You write a goal spec: what the product should do, the context it lives in, and
the ground truth that defines "done." Plexus plans that goal into an ordered
list of features, then runs a loop: pick the next feature, build a heart
`TaskSpec` for it, run it as an episode, and check the result against the
feature's acceptance criterion. A passing feature lands and the loop advances;
a failing one retries within a budget; anything ambiguous, unsatisfiable, or
repeatedly regressing pauses the loop and lands in an escalation queue for you.
Every episode leaves traces in arteries, events on the spine, and training data
for marrow — for free, because it went through heart.

## Warren lineage

Adopted: the durable spine per goal (warren's Plot: shape → plan → sign-off →
serial run → activity log), serial dispatch gated on the previous child
landing, resume-from-next-open-child, and the sign-off gate that arms
execution. Adapted: warren's prompt libraries and agent memory become
capillaries and arteries; its run sandbox becomes heart's worktrees. Rejected:
the container/bwrap sandbox, HTTP control plane, and web UI — this is a
single-user local stack and heart already owns isolation; a CLI and a ledger
file cover it.

## What plexus owns vs. delegates

| Component | Owner | Note |
| --- | --- | --- |
| Goal spec format (`plexus.toml`) | plexus | goals, context, ground truth, budgets |
| Feature planner | plexus logic, LLM via a heart episode | planning is itself a solo episode; no new model plumbing |
| Loop state machine + ledger (`.plexus/`) | plexus | JSONL, repo-local, stdlib-only like heart |
| Escalation queue | plexus | `.plexus/escalations.jsonl` + `plexus status` |
| Worktrees, verify-fix loop, review, scoring | **heart** | imported as a library, like marrow does |
| Prompt/skill selection | **capillaries** | rides along via arteries hooks |
| Memory, traces, run telemetry | **arteries** | rides along via CLI hooks |
| Event spine + pulse tooling | **heart** | plexus emits to it, never reinvents it |
| Training data | **marrow** | consumes heart's `episodes.jsonl`; plexus does nothing |

Anything not in this table that later looks necessary should first be checked
against heart's flags before being built here.

## Contract consumed: heart as a library

Marrow set the precedent; plexus follows it. Heart is stdlib-only, so
`pip install -e ../heart` is the whole dependency story:

```python
from heart.taskspec import TaskSpec, Verifier
from heart.detect import detect_verifiers
from heart.episode import run_candidates, best_episode

task = TaskSpec(task_id=f"{goal_id}-{feature_id}-a{attempt}",
                repo_path=repo, base_commit=head, prompt=feature_spec,
                public_verifiers=verifiers)
ep = best_episode(run_candidates(task, n=1))
ep["outcome"], ep["reward"], ep["review_verdict"]   # full dict, no stdout parsing
```

This is strictly richer than shelling out to `heart work` (which prints a
four-field JSON summary): plexus gets the whole episode record, controls the
TaskSpec (base commit, verifiers, path allow/deny lists), and inherits heart's
apply semantics instead of reimplementing them. The dependency direction stays
one-way: plexus imports heart; heart never knows plexus exists.

## The loop

1. **Shape.** `plexus init` scaffolds `plexus.toml`. Goals in prose, ground
   truth declared explicitly (see below), budgets defaulted.
2. **Plan.** `plexus plan` runs one solo heart episode that turns goal +
   context into an ordered feature list, each with its own acceptance
   criterion referencing the ground truth. Plan is written to
   `.plexus/plan.jsonl` and **requires human sign-off** (`plexus approve`)
   before anything runs — warren's gate, kept because the plan is where scope
   errors are cheapest to catch.
3. **Run.** `plexus run` walks the plan serially. Per feature: build the
   TaskSpec, run the episode, then run the feature's acceptance check on the
   clean result.
   - Pass → apply, record in ledger, next feature.
   - Fail → retry with the failure appended to the feature spec, up to the
     per-feature attempt budget (default 3).
   - Budget exhausted → escalate, loop pauses.
   Re-running `plexus run` resumes from the next open feature.
4. **Verify scope.** After the last feature, run the *full* ground-truth suite
   on a clean checkout. All pass = scope satisfied, loop ends, `plexus status`
   says so. Any regression → one repair feature is synthesized and dispatched;
   if it fails, escalate.

## Ground truth

Three kinds, each reduced to an executable check before the run phase starts:

- **Executable tests** — used as-is (a `Verifier` per feature or suite).
- **Input/output examples** — compiled into a test file by a dedicated heart
  episode during `plexus plan`; the generated tests are part of the sign-off
  review, because a wrong test silently redefines the goal.
- **Prose spec** — the planner extracts checkable criteria and maps each to
  one of the above. Criteria it cannot make executable are listed in the plan
  as **unverifiable**; they block sign-off until the human either supplies a
  check or explicitly waives them. Contradictory ground truth is surfaced at
  plan time, never resolved by silent guessing.

## Stopping and escalation

- **Done** means: every feature's acceptance check and the full ground-truth
  suite pass on a clean checkout. Nothing else counts.
- **Budgets**: per-feature attempts (3), per-goal total episodes (default 25),
  optional cost ceiling if the agent reports spend. Exhaustion always
  escalates; it never silently truncates scope.
- **Escalate immediately** (pause loop, write to queue) on: unverifiable or
  contradictory ground truth at plan time, the same feature failing on the
  same check twice with materially identical diffs, a feature that would
  require destructive action outside the worktree, or full-suite regression
  that the single repair attempt doesn't fix.
- Escalations are questions with options, not stack traces: what failed, what
  was tried, what decision is needed.

## Observability

Heart already implements the SRE-book stack for this ecosystem: an append-only
NDJSON event spine (`heart/SPINE.md` is the wire canon — additive-only kinds,
tolerant readers, emission never raises) read by `pulse health` (symptom
check, exit code as the alert primitive), `pulse episode <id>` (cause
drill-down), and `pulse insights` (golden-signal dashboard, percentiles never
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
its spool loses nothing; plexus cannot, because a dropped event read back as
state would skip a feature or re-run a landed one. So the write order is
fixed: ledger first (must succeed), spine second (best effort), and `plexus
status` decides state from the ledger alone — the spool only supplies the
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
episode_ids so the next command is `pulse episode <id>`, not a plexus-specific
viewer. Goal-level insights (lead times, escalation rate, rescue rate after
retry) come from the **ledger**, not the spool: pulse's read side rescans the
entire spool history per query, which is fine at heart's one-day horizon and
wrong at plexus's multi-week one. Ledger for history, spool for the live
window — `plexus insights` stays a small read-side function either way.

### What went wrong: the judgment surface

`plexus status` tells you *that* a feature is stuck; `plexus why [feature]`
tells you *what kind* of stuck, so a semi-autonomous run stays reviewable and
your attention lands where taste is actually needed. It reads the ledger and
prints, per failed or escalated feature: the **intent** it was judged against
(the acceptance criterion), each attempt's failure attributed to a phase, the
escalation, a phase verdict, and — because it stores pointers, never copies —
a `pulse episode <id>` drill-down to the raw **logs, traces, and diff** for
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
repo's own suite (`detect_verifiers`) — its reward stays a pure
regression/correctness signal — while `run.py` runs the feature's acceptance
criterion in its *own* worktree, outside the episode. The two judgments are
recorded separately (`acceptance.round.passed` + `episode_outcome`) and can
disagree, which is the whole point of the 2×2 above. Landing requires *both*:
the criterion passes and nothing regressed — a diff that meets the feature but
breaks the existing suite is caught as `testing` and never lands. Acceptance is
deliberately kept out of heart's reward: heart's hidden verifiers dominate its
reward (weight 0.45), so routing the criterion through them would merge the two
rewards, which LEDGER law 5 forbids. That separation is also what manufactures
marrow's hard negative — an episode that earned heart's reward but failed
acceptance — which cannot exist while the two are one command.

Heart's alerting assumes someone at the terminal (exit code as the alert
primitive); plexus's premise is unattended operation, so an escalation that
waits to be polled defeats autonomy. Plexus therefore owns one push channel:
`notify_cmd` in `plexus.toml` (e.g. `notify-send`, `mail`, a webhook curl),
fired on escalation and goal completion. This deliberately stays out of
heart — heart runs while you watch; plexus runs while you sleep.

Finally, because plexus is the integrator, it owns the factory-wide vantage
no single organ has: `plexus stack` rolls up the shared spool by source —
event volume, failures, and store degradation across heart, arteries,
capillaries, marrow, and plexus in one view. Per-source depth stays with each
repo's own tools; stack answers only "which organ is unhealthy," then hands
off.

## Code layout (current)

v0 loop and observability are both implemented.

```text
src/plexus/spec.py      plexus.toml load + `plexus init` scaffold, spec_hash
src/plexus/plan.py      planner episode -> plan.jsonl, sign-off gate
src/plexus/run.py       the serial acceptance loop: dispatch, judge, land/retry/escalate, resume
src/plexus/events.py    spine emission via heart.events, task_id convention
src/plexus/ledger.py    system of record: fsynced JSONL, ledger-first write order
src/plexus/observe.py   status (symptom check), insights (ledger), stack (spool rollup)
src/plexus/diagnose.py  why (per-feature intent/logs/traces/bugs), phase attribution
src/plexus/cli.py       plexus init | plan | approve | run | status | insights | why | stack | tail
tests/test_plexus.py    self-check: python3 tests/test_plexus.py (stdlib, no network)
```

Install mirrors marrow: `pip install -e ../heart && pip install -e .`.

## Marrow and the self-improvement flywheel

Marrow already trains on two streams: heart's `episodes.jsonl` (SFT/DPO/GRPO,
stages 0–3) and arteries' decision ledger (reranker + gate, stage 4). Plexus
adds a third stream that no other layer can produce, and it closes the loop:

- **Acceptance labels as a second reward.** An episode that passes heart's
  verifiers but fails plexus's acceptance check is a hard negative heart
  cannot see on its own — the diff satisfied the task but not the goal.
  Joined to episodes via the task_id convention, these sharpen marrow's
  reward beyond per-task verification.
- **Retry pairs for free.** Attempts `a1..aN` on the same feature are
  same-task pass/fail pairs — exactly what marrow's DPO stage needs 20+ of.
  Plexus manufactures them as a side effect of its retry budget.
- **Planner training.** Plans are themselves episodes, and the ledger gives
  each plan an outcome label: did its features land within budget, or
  escalate? That turns planning into a trainable task with delayed reward —
  a data stream that exists only because plexus records goal outcomes.
- **Rescue labels.** The ledger records which failure-appended retry prompt
  rescued a failing feature, which is training signal for failure-aware
  retry prompting and, eventually, the gate models.

The flywheel: plexus runs goals unattended → episodes, acceptance labels, and
plan outcomes accumulate → marrow trains the implementer (GRPO), the planner
(DPO on plan outcomes), and the gate/reranker (arteries ledger) → better
checkpoints serve heart's `api` agent → plexus lands more scope per budget.
The contracts stay flat: marrow reads `episodes.jsonl` plus plexus's ledger,
joins on task_id, and never calls plexus — the same arms-length relationship
it keeps with heart.

## v0 — smallest slice that proves the loop

One toy repo, one `plexus.toml` with executable-test ground truth, two planned
features. Acceptance checks:

1. `plexus plan` produces `.plexus/plan.jsonl` with 2 features, each carrying a
   runnable acceptance command; `plexus approve` arms it.
2. `plexus run` executes feature 1 as a heart episode, applies on pass, then
   feature 2; `plexus status` exits 0 and says `done`; the full suite passes
   on a clean checkout; `pulse tail` shows the plexus goal/feature events
   interleaved with heart's episode events, correlated by task_id.
3. Kill feature 2's test so it can't pass: after 3 attempts `plexus status`
   exits 1 with one escalation naming the failing check and the episode_ids
   behind it, and re-running `plexus run` after a manual fix resumes at
   feature 2, not feature 1.

Not in v0: I/O-example compilation, prose-spec extraction, budgets beyond the
attempt counter, repair-feature synthesis, `notify_cmd`, any UI.

## Roadmap after v0

1. Ground-truth compiler: I/O examples and prose criteria → generated tests
   with sign-off review.
2. Unattended operation: `notify_cmd` push on escalation/completion,
   failure-aware retry prompts, `--candidates N` best-of-N on retries,
   repair-feature synthesis after full-suite regression.
3. Fleet mode: `plexus run` across multiple goals/repos on a schedule, cost
   ledger, and a `plexus report` digest.
4. Flywheel export: `plexus export` emits acceptance labels, retry pairs, and
   plan outcomes keyed by task_id for marrow's training stages.

## Open questions

1. Should plan sign-off ever be skippable (`--auto-approve`) for low-stakes
   goals, or is the gate unconditional? Default: unconditional until the
   planner has earned trust.
2. Where do multi-repo products live — one `plexus.toml` per repo, or one goal
   spanning repos? v0 assumes one repo; the answer changes the spec format.
3. ~~Should plexus event kinds be added to SPINE.md's catalog?~~ Done —
   SPINE.md now lists the plexus source, its kinds, and the task_id
   correlation convention.
