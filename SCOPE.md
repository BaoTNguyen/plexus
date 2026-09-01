# Learned scope

How a sandbox's refusals become a better next attempt. The peer of `LEDGER.md`
for one narrow fact domain: what an episode was *permitted* to touch versus what
it *needed* to touch.

## The problem

Heart can run an episode inside a container whose mount table is derived from
the task spec — `allowed_paths` become the writable mounts, `denied_paths` are
layered back read-only on top. That turns a path rule from something audited
after the fact into something the kernel enforces.

A mount table derived from a spec is a **prediction** of what the work needs.
Predictions are wrong. When one is too tight the agent's writes are refused, it
produces nothing, and the diff comes back empty — which heart's outcome ladder
would read as `no_change`, meaning "the agent did nothing".

That is a lie about what happened, and the expensive kind: the task looks
unproductive rather than misconfigured, and the fix is invisible.

## What heart does about it

Heart names the failure instead of misattributing it.

| Event | Meaning |
|---|---|
| `sandbox.denied` | the sandbox refused ground the spec **permitted** — our misconfiguration |
| `guardrail.hit` with `rules: ["denied_path_probe"]` | the agent reached for ground the spec **forbade** — its violation |

The distinction is load-bearing. Without it, `scope_denied` is a reward hack: an
agent heading for a bad score writes one byte into a denied path, produces an
empty diff plus a refusal, and escapes being scored at all. Heart classifies the
refusal by the path it names, so forbidden ground yields `path_violation` while
permitted ground yields `scope_denied`.

`scope_denied` carries **no reward** — `None`, not `0.0`. Zero would blame the
model for a sandbox we drew too tight, and training on it teaches that the task
is impossible.

## What plexus does about it

`src/plexus/scope.py`.

**Distil once, immediately, into the ledger.** `observe()` is called from
`run.py` right after the episode returns, while its events are still in the
journal. This follows the rule `observe.py` already states:

> Insights come from the ledger, not the journal — pulse's full-journal rescan
> is fine at heart's one-day horizon and wrong at plexus's multi-week one.

A journal scan at plexus's horizon returns less the older the question gets.
That failure mode is indistinguishable from the system having stopped making
mistakes, which is the worst way for a learning loop to break.

```
episode → sandbox.denied → scope.observe() → ledger: scope.observed
                                           → scope.for_task() at the next attempt
```

### API

| Function | Does |
|---|---|
| `observe(task_id, *, goal_id, feature_id, root)` | reads this episode's scope events, records one `scope.observed`, returns what it found or `None` |
| `for_task(task_id, *, root)` | `{"allow": [...], "deny": [...]}` accumulated from the ledger |

### Ledger record

```json
{"kind": "scope.observed", "goal_id": "...", "feature_id": "...",
 "task_id": "...", "needed": ["src/adapters"], "probed": ["src/secrets/key.pem"]}
```

## Three rules that keep the loop honest

**Refusals widen to the directory, not the file.** A spec granting `src/app.py`
grants one file and refuses the next one the same task needs. The directory is
the smallest widening likely to hold, and still far narrower than the
unrestricted default `run.py` dispatches with today.

**A probe never becomes a permission.** `needed` and `probed` are recorded
separately and `for_task` never merges them. A path the agent was caught
reaching for is evidence the prohibition is *working*. Folding the two would let
an agent widen its own boundary by repeatedly reaching past it — the same reward
hack heart closes at the classification layer, closed again here.

**Scope is derived from refusals, never from requests.** A refusal is the kernel
reporting a fact. Anything the agent says about what it needs is an account of
its own interests, and optimising against that produces an agent that asks for
root.

## Not wired yet — the decision that is yours

`run.py` builds its `TaskSpec` with **no `allowed_paths` at all**. Plexus
dispatches unrestricted today, so `for_task()` is recorded and read by nothing.

This is deliberate. Feeding learned paths straight into `allowed_paths` would
take a task that was unrestricted and restrict it to whatever a single refusal
happened to name — turning a working task into a broken one on its first
observation. Enforcement has to be opt-in, and how it is expressed is a charter
question, not a mechanism one.

Rough shapes, in the order I would consider them:

1. **A goal-level flag** in `plexus.toml` (`[sandbox] enforce_scope = true`), so
   a whole goal opts in once its scope has stabilised.
2. **A warm-up count** — enforce only after N observations for a task, so the
   first attempts run wide and teach, and later ones run narrow.
3. **Per-feature**, from the plan, for goals where one feature touches secrets
   and the rest do not.

Whichever is chosen, `allowed_paths` should be seeded from `for_task()["allow"]`
and `denied_paths` should union `for_task()["deny"]` with whatever the charter
already forbids.

## Also open

- **Generalisation.** Corrections are per `task_id`. A new task in the same repo
  starts the cycle again. Whether "tasks touching `src/` here also need
  `tests/`" is worth learning is a real question and not bookkeeping.
- **`scope_denied` retries.** `_MECHANICAL` now names it, so `plexus why` says
  "misconfiguration" rather than "episode error". Whether the retry should widen
  the scope automatically — and how hard to cap that — is undecided.
- **Nothing reads `review.notes` yet.** Heart emits the reviewer's reasoning on
  every verdict, not just rejections. It reaches the journal and dies there, the
  same way scope facts did before this module.

## Files

| Path | What |
|---|---|
| `src/plexus/scope.py` | the consumer |
| `tests/test_scope.py` | 7 tests, including one that deletes the journal before querying |
| `src/plexus/run.py` | calls `scope.observe()` after each episode; `_MECHANICAL["scope_denied"]` |
| heart `src/heart/sandbox.py` | profile derivation, `_git_mounts`, `verifier_profile_for` |
| heart `src/heart/episode.py` | `_scope_denials`, `_probed_forbidden`, `UNSCOREABLE` |
