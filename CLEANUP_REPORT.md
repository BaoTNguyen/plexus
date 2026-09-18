# plexus cleanup: import placement and duplication

Branch `dev`. Two commits: the import audit, then the dedup. Both sit on top
of `e8d5719`, which was already ahead of `main`.

## Counts

| | |
|---|---|
| function-local imports audited | 103 |
| hoisted to module top | 13 |
| kept local | 17 (plus docstring examples, which are not imports) |

plexus hoisted the least of any repo, and that is the right outcome rather
than a shortfall. Almost everything the scan flagged was `from heart ...`,
and all of it stays.

## KEEP categories

| category | count | files |
|---|---|---|
| `from heart ...` | 17 | `observe.py`, `sandbox.py`, `serve.py`, `registry.py`, `cli.py`, `run.py`, `scope.py` |

That is the whole table. plexus declares no dependencies, and its README says
`pip install -e ../heart` is the entire dependency story — which means heart
is available in a working install and absent in plenty of others. Every one
of these seventeen sites exists so that `import plexus` succeeds either way,
and several are inside best-effort probes where an ImportError is supposed to
stay silent.

Six carry an explicit comment. The other eleven do not, and I left them that
way on purpose: seventeen copies of "lazy: keeps plexus importable without
heart" is noise, not documentation. The uniformity is the documentation —
there is exactly one reason, and the rule is that a `from heart` import in
this codebase is never at module scope. `observe.py:239` states it once in
full.

## Within-repo duplication

### Extracted

**`_spend()` in `observe.py`** — `insights` and `stack` each summed
`cost_usd`, `tokens_in` and `tokens_out` off the same three keys, walking the
same records. The goal bill and the factory bill, counted twice. They can no
longer disagree about what a run cost, which matters because a wrong number
here is one nothing would alert on.

**`_send()` in `serve.py`** — the same five-line response preamble appeared in
`_json` and again in the no-frontend-build fallback.

`_static` keeps its own copy. It interleaves `Cache-Control` between the
length header and the terminator, and bending `_send` to accommodate that
costs more than the four lines it saves.

## Verification

| check | result |
|---|---|
| `PYTHONPATH=src:../heart/src pytest -q` | 27 passed |
| imports with `heart` stubbed out | passes — `plexus` and `plexus.cli` both import clean |
| frontend | untouched; not built, not modified |

## Cross-repo duplication candidates

Reported, not extracted.

| # | logic | files | ~lines | worth it? |
|---|---|---|---|---|
| 1 | The loopback guard on the control-plane HTTP server: reject any request whose `Host` or `Origin` is not `127.0.0.1`/`localhost`. Byte-identical in both servers. | `plexus/serve.py:1262` vs `heart/serve.py:41` | 7 | **No — keep both copies, deliberately.** This is the DNS-rebinding guard. Importing it from heart would put a security boundary behind plexus's lazy-import convention, where an ImportError is routinely swallowed. A guard that can silently fail to load is worse than a guard written twice. Seven self-contained stdlib lines in each server is the correct shape. |
| 2 | Deciding which model endpoints are local. `sandbox.local_model_hosts` opens heart's `models.json` by hand and re-implements the loopback test heart exports as `agents_api.is_local_endpoint`. | `plexus/sandbox.py:50` vs `heart/agents_api.py:49` | ~14 | **Yes.** The two disagree: heart also accepts private LAN ranges, plexus does not, so on a box with a model server on the LAN they classify it differently. Unlike #1 this is not a security boundary and plexus already imports heart nearby — `registry.heart_model_rates` does exactly this correctly. |
| 3 | The shape of `runs/<episode_id>/episode.json`. `export._episode_facts` reads `outcome`, `reward.total`, `diff_lines`, `agent` and `blocked_reason` by string key out of a file heart writes, with no shared definition. | `plexus/export.py:29` vs `heart/episode.py:1090` | ~15 | **Not as a dedup — as a contract.** The logic is not duplicated; the schema is. A `heart.export.load_episode()` would give the join one place to break loudly instead of returning `{}`. Low priority. |
| 4 | Building `claude` / `codex` command lines. | `plexus` shares this with `marrow` and `heart` | ~16 | **Marginal, and marrow's problem more than plexus's.** See heart's report, item 3 — the flag sets genuinely differ and only the model-pinning rule is worth sharing. |

Worth recording as the counter-example: `plexus/events.py` and
`registry.py:276` already do this right. They import `heart.events.emit` and
`heart.runner.model_pricing` instead of copying, and `registry.py:276` says
why in a comment about two rate cards drifting in two repos. The pattern
exists here. Item 2 is where it was not applied.

## Suspected bugs

None found in plexus itself. One inherited: heart's `sandbox.image_is_stale`,
which plexus calls from `sandbox.py:266`, compares the Dockerfile's *checkout*
mtime rather than its commit time. On a fresh clone every file gets today's
mtime, so a correctly built image reads as stale and every sandboxed episode
raises. Details in heart's report.
