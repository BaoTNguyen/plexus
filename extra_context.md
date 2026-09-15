# Extra context — plexus

Session handoff, written 2026-08-26. `LEDGER.md` is the fact-ownership law,
`SCOPE.md` the learned-scope design. This file holds current state and the
things that will waste your afternoon.

## State

Branch `dev`, in sync with origin, at `2dd6874` ("Learn scope from what the
sandbox refused"). Plexus never carried an `event-journal-and-sandbox` branch —
it was already on `dev` and needed no merge when the other four repos came down.

Tests: 15 passed, well under a second.

## The tests do not run on their own

`tests/test_heart_api_pin.py` imports `heart.detect`. Heart is not installed
into the interpreter, so **collection aborts before a single test runs**:

```
ModuleNotFoundError: No module named 'heart'
Interrupted: 1 error during collection
```

It looks like a broken test file. It is a missing sibling. Run it as:

```
PYTHONPATH=../heart/src:../arteries/src:../capillaries/src python3 -m pytest -q
```

or `pip install -e ../heart` once. Worth fixing properly with a `conftest.py`
that inserts the sibling `src` paths — the current shape means a fresh checkout
fails at import, which reads as a much scarier problem than it is.

## Learned scope is the live thread

`src/plexus/scope.py`. Heart can now refuse an episode's writes at the kernel,
and a refusal is evidence: it says the spec's prediction of what the work needed
was wrong. Plexus turns that into a better next attempt.

The rule `observe.py` states, and that `scope.py` follows: **insights come from
the ledger, not the journal.** Pulse's full-journal rescan is fine at heart's
one-day horizon and wrong at plexus's multi-week one. So `observe()` distils
once, immediately, called from `run.py` right after the episode returns while
its events are still in the journal.

Open and unbuilt: a two-phase scope strategy — assign a relaxed scope first so
the run can self-correct, then spawn a secondary sandboxed run for the
correction itself. Related unsolved case: review agents produce insights needing
execution scopes wider than the least-privilege profile predicted for them.

## Ownership doctrine

Plexus's question is *"what should be built next; is the scope satisfied?"* —
goals, plans, acceptance, escalation, retry budgets. Not verifiers, not memory,
not retrieval.

Plexus **names episodes** and passes the name down the stack. Heart orchestrates
each into subtasks with dedicated context and memory via arteries; runs execute
in docker-sbx.

Two deliberate near-duplications, not redundancies:

- **plexus acceptance vs heart review/verify.** Heart judges the task with its
  verifiers; plexus judges the goal against ground truth. The "heart passed /
  acceptance failed" cell is a hard negative marrow cannot otherwise see
  (LEDGER law 5). Keep both.
- **capillaries feedback vs arteries rewards.** Local relevance signal versus
  episode outcome, joined by `episode_id`.

## Ledger rules worth memorizing

1. The spine is telemetry, **never** a system of record. Best-effort, written
   after the authoritative write, never read back as state. Its one
   state-adjacent use is the staleness signal in `plexus status`.
2. Exports derive from systems of record, never from the spine. `labels.jsonl`
   comes from the ledger; a dropped spine event can never corrupt a training set.
3. Reference, don't copy — store `episode_id`s and hashes.
4. `.plexus/ledger.jsonl` is fsynced and has **no degraded fallback**. It must
   succeed.

Label schemas lock in: a goal run recorded under a weak schema can never be
re-labeled. That's why LEDGER.md was written before v0 was built, and why
changing it is expensive rather than merely annoying.

## Traps

- `.arteries/hooks/` is generated and gitignored. Capillaries lost its whole
  hook install that way and ran blind for a week — verify the directory exists,
  not just that `settings.local.json` points at it.
- Plexus has no persistent arteries memory of its own (0 rows as of today). It
  reads the `harness` scope, so it sees arteries' memory, which is
  arteries-flavored. Don't assume a fresh session here knows plexus's reasoning.
