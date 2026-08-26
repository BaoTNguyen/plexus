# The goal ledger

The contract for goal-level facts and training labels. This file is the canon,
the peer of heart's `SPINE.md`. It exists because label schemas lock in: a
goal run recorded under a weak schema can never be re-labeled, so what gets
written here was decided before v0 was built.

## The law: one fact, one owner

Every fact in the stack has exactly one authoritative store. Everything else
is a derived view and may be regenerated or lost without losing truth.

| Fact domain | System of record | Derived views | Degraded fallback |
|---|---|---|---|
| Prompt corpus, retrieval | capillaries Postgres | — | — |
| Turns, memories, gate/reward decisions | arteries Postgres | spine tee | repo-local JSONL (`store: jsonl` flags it) |
| Episode execution (diff, verifiers, reward) | heart `runs/<id>/episode.json` | spine events, `episodes.jsonl` export | — |
| Goals, plans, features, acceptance, escalations | plexus `.plexus/ledger.jsonl` | spine events, `labels.jsonl`/`plans.jsonl` exports | none — fsynced, must succeed |
| Training runs, checkpoints | marrow | spine events | — |
| Live cross-stack view | — (derived only) | the spine journal | — |

Rules that keep the stores from conflicting:

1. **The spine is telemetry, never a system of record.** It is written
   best-effort after the authoritative write and is never read back as state.
   Its only state-adjacent use is the staleness signal in `plexus status`.
2. **Exports derive from systems of record, never from the spine.**
   `episodes.jsonl` comes from heart's runs dir; `labels.jsonl` comes from
   the ledger. A dropped spine event can never corrupt a training set.
3. **Reference, don't copy.** The ledger stores `episode_id`s, hashes, and
   commit SHAs — never diffs, prompts, or verifier output that heart already
   owns (short failure tails for retry context are the one bounded exception).
4. **Joins use recorded ids, never parsed strings.** The
   `<goal_id>-<feature_id>-a<attempt>` task_id convention is a grep
   convenience for the journal; correctness always joins through the
   `task_id`/`episode_id` fields recorded in ledger records.
5. **Two rewards never merge.** Heart's verifier reward and plexus's
   acceptance label are different judgments of the same episode and are kept
   in separate fields forever. An episode that passed heart but failed
   acceptance is a hard negative — collapsing the two destroys exactly that
   signal.
6. **Additive-only, tolerant readers** — same two anti-drift laws as SPINE.md.
   Fields and kinds are added, never renamed or removed; readers skip unknown
   fields, unknown kinds, and torn lines.

## Ledger record catalog (v1)

Append-only JSONL at `<repo>/.plexus/ledger.jsonl`. Envelope on every record:
`ts` (UTC ISO-8601), `kind`, `goal_id`, and `feature_id` when applicable.
Written fsynced *before* the corresponding spine event.

| Kind | Fields beyond the envelope | Notes |
|---|---|---|
| `goal.started` | repo, base_commit, spec_hash | spec_hash keys runs to the exact `plexus.toml`; goals with different hashes are not comparable training data |
| `plan.created` | plan_id, features `[{feature_id, title, acceptance}]`, rejected `[]` | `rejected` holds discarded plan candidates once the planner runs best-of-N — counterfactuals for planner training |
| `plan.approved` | plan_id, approver (`human`\|`auto`), waived `[]` | waived = unverifiable criteria the human accepted |
| `feature.started` | attempt, task_id, retry_context | attempt numbering is monotonic per feature, never reused; retry_context is the failure tail the prompt carried — the retry-training input |
| `feature.failed` | attempt, task_id, episode_id?, failure_class, reason | one per failed attempt |
| `acceptance.round` | attempt, task_id, episode_id, passed, check | plexus's judgment in the real tree — heart cannot see this |
| `feature.landed` | attempt, task_id, episode_id, commit | |
| `escalation.raised` | reason_class, reason, episode_ids | a question for the human, with the evidence attached |
| `escalation.resolved` | resolution | resets the feature's attempt *budget* (not its numbering) |
| `goal.finished` | outcome (`scope_satisfied`\|`abandoned`), episodes_total | |

`failure_class` (growing enum): `verify_failed`, `review_rejected`,
`acceptance_failed`, `apply_failed`, `no_change`, `path_violation`,
`episode_error`, `timeout`.

`reason_class` (growing enum): `attempts_exhausted`, `budget_exhausted`,
`regression`, `unverifiable_ground_truth`, `destructive_action`,
`blocked_on_decision` (the agent asked for a decision mid-run instead of
guessing; `escalation.resolved.resolution` carries the answer, injected into
the next attempt).

## Export contract (what marrow reads)

`plexus export` derives two files from the ledger. Marrow joins them against
heart's `episodes.jsonl` on `episode_id` (or `task_id`) and never reads the
ledger or the journal directly.

`labels.jsonl` — one record per attempt:

```json
{"goal_id": "g1", "feature_id": "f2", "attempt": 3, "task_id": "g1-f2-a3",
 "episode_id": "20260705-...", "plan_id": "plan-...",
 "episode_outcome": "pass", "failure_class": null, "accepted": true,
 "retry_context": "verifier 'acceptance' failed:...",
 "rescued_by_attempt": null, "goal_outcome": "scope_satisfied"}
```

The training streams this feeds: `accepted=false` on `episode_outcome=pass`
rows are hard negatives; same-feature rows with different outcomes are DPO
pairs; `retry_context` on rescued rows is retry-prompt training data.

`plans.jsonl` — one record per plan:

```json
{"plan_id": "plan-...", "goal_id": "g1", "approved": true,
 "features_planned": 4, "features_landed": 4, "episodes_used": 7,
 "waived": [], "goal_outcome": "scope_satisfied"}
```

Plan outcomes are the delayed reward for planner training: plans whose
features landed within budget vs plans that escalated.
