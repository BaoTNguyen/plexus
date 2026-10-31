# plexus

Give plexus a goal and the test suite that defines "done". It plans the goal
into small features, has a coding agent build each one in a sandbox, lands a
feature only when its own check and your whole suite pass, and stops to ask you
when it can't tell what you want. It can run for hours unattended and picks up
where it left off after a crash.

plexus is one repo of a six-repo agent stack and runs its agents through
[heart](https://github.com/BaoTNguyen/heart). The
[vascular README](https://github.com/BaoTNguyen/vascular#readme) shows how the
pieces fit.

## Why use it

Most autonomous coding loops run until the model says it's finished. plexus
decides what finished means before the run starts, and checks it with your
code rather than the model's opinion.

- **You define done, up front.** Every feature carries an acceptance check, and
  it lands only when that check passes and nothing in your suite regressed.
- **You sign off where mistakes are cheapest.** Nothing runs until you approve
  the plan. A check that already passes, can't run, or is rigged to pass
  (`|| true`) is refused at that gate.
- **Risky work waits for you; routine work doesn't.** Each feature is classed by
  the paths it may touch and the diff it produced. Tests, docs and self-contained
  changes land on their own. Changes to the ledger, a seam another repo calls,
  credentials, or anything that runs on your machine (hooks, CI, manifests) hold
  for your signature, and the diff you approve is the one that lands.
- **A crash costs nothing.** State lives in an fsynced ledger under
  `.vascular/plexus/`. Run `plexus run` again and it resumes at the next open
  feature.
- **It tells you what kind of stuck.** `plexus why` files each failure under
  coding, testing or intent, so a bad plan reads differently from a bad diff.
- **It runs while you sleep.** A notify command fires when it needs you, and
  `plexus fleet run` advances several goals from a timer under one cost ceiling.

## Get started

### Install

plexus needs heart checked out beside it, and neither is on PyPI. The easiest
route is the whole stack: `git clone --recursive https://github.com/BaoTNguyen/vascular`.
On its own:

```bash
git clone https://github.com/BaoTNguyen/heart
git clone https://github.com/BaoTNguyen/plexus && cd plexus
uv sync && uv run pytest -q
uv tool install --editable . --with-editable ../heart   # `plexus` on your PATH
plexus doctor          # what this machine still needs; --fix provisions it
```

### Choose where agents run

`plan`, `approve` and `run` start agents or agent-written checks, so they refuse
until you say where:

```bash
export HEART_SANDBOX=docker-sbx HEART_SANDBOX_IMAGE=heart-agent:latest   # containers
export HEART_SANDBOX=off                                                 # this machine, with a warning
```

### Your first goal

```bash
cd your-repo
plexus init       # writes plexus.toml, every option commented
$EDITOR plexus.toml
plexus plan       # an agent turns the goal into features, each with a check
plexus approve    # read the plan, then sign it off
plexus run        # build, check and land, one feature at a time
plexus status     # 0 progressing or done, 1 waiting on you, 2 stalled
```

The two parts of `plexus.toml` you have to write:

```toml
[goal]
id = "csv-export"
text = """Add a --csv flag to `report` that writes the same rows as CSV."""
context = """The CLI is src/app/cli.py; tests live in tests/."""

[ground_truth]
suite = "python3 -m pytest -q"   # the executable definition of done
```

Everything else has a default. Longer context (the problem, architecture,
program design) goes in Markdown under `.vascular/plexus/overview/`, where
diagrams and code fences fit.

When `run` stops for you:

```bash
plexus why                         # what failed, in which phase, with episode ids
plexus resolve f3 "use the v2 API" # answer it; the answer reaches the next attempt
plexus amend f3 --touches 'src/app/*,tests/*'   # the plan was wrong, not the code
plexus run                         # resume
```

When the whole goal is green, `run` pushes the branch and opens a PR into
`[review] pr_base` with the review table as its description. `plexus serve`
puts the same controls in a browser at http://127.0.0.1:8100.

## How a goal runs

1. **Plan.** One agent run turns the goal into ordered features. Each gets an
   acceptance check, `touches` (the paths it may change) and `contract` (the
   public names it adds or changes).
2. **Approve.** You read it and sign it off. Claims no suite can check go in
   `[ground_truth] manual`, and you confirm those yourself.
3. **Run.** For each feature, heart builds it in an isolated worktree, then
   plexus runs the acceptance check against the result.
   - Pass, suite green, inside `touches` → it lands and the loop moves on.
   - Fail → it retries with the failure added to the prompt, up to
     `attempts_per_feature` (3).
   - Out of attempts, out of scope, or a decision only you can make → it
     escalates and the loop pauses.
4. **Verify scope.** After the last feature the full suite runs on a clean
   checkout. A regression gets one repair feature, then escalates.

A repo that other repos import can list them in `[ground_truth] dependents`.
Their suites then run against every candidate, so a change that breaks a caller
fails before it lands.

## What you have to read

| class | when | read |
| --- | --- | --- |
| `spine` | it can touch the ledger, the spec or the event code | every line |
| `boundary` | it changes a CLI command, a config key, or a name a sibling repo calls | the seam and every caller |
| `exec` | it touches files that run on your machine: hooks, CI, manifests, `conftest.py` | every line |
| `credentials` | it touches TLS, seat or secret code, or names a token or key | every line |
| `suspect` | the diff newly adds network calls, processes, dynamic code, decoding or encoded blobs | every line |
| `leaf` | one repo, nothing outside calls what it changes | the report line |
| `mechanical` | docs and tests only | nothing |

The classes in `[review] hold` wait for you even when green; the rest land on
their own. `plexus review --plan` shows the classes before the run, while
narrowing `touches` is still free. `plexus review` shows plan against landed
afterward.

A feature that changes what a command prints can end its spec with an `expect:`
block, the command and the lines it must print. That turns a mockup written
before the code into a check.

## Commands

| command | what it does |
| --- | --- |
| `init` · `plan` · `approve` · `run` | the loop above |
| `status` · `why` · `insights` | is it moving, what went wrong, goal metrics |
| `resolve` · `amend` | answer an escalation, fix a feature in the plan |
| `review` | which landed commits you must read |
| `replay` | re-run landed features from their base: pass rate, cost, lead time |
| `serve` · `fleet run` | browser control plane; headless runner for a timer |
| `add` · `remove` · `report` | the project menu and its fleet digest |
| `stack` · `tail` | factory-wide event rollup; live event tail |
| `export` · `prune` | training labels for marrow; drop unreferenced episode dumps |
| `doctor` | sandbox readiness; `--fix` provisions it |

`plexus <command> --help` has the flags.

## Extending plexus

<!-- TODO(operator): what this section should cover is still to be decided. -->

## Learn more

- [`docs/adr/`](docs/adr/): why plexus is built the way it is
- [`LEDGER.md`](LEDGER.md): the ledger's record kinds and the laws that keep it the system of record
- [`AGENTS.md`](AGENTS.md): conventions for agents working in this repo
