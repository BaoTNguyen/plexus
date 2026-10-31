# Decisions

Why plexus is shaped the way it is, one file per decision in trimmed MADR 4.
Format and rules: [vascular/docs/adr](https://github.com/BaoTNguyen/vascular/tree/main/docs/adr).
The "In force" table is generated from each record's front matter: run
`python3 docs/adr/index.py` after adding or superseding one. CI fails when the
table is stale.

## In force

<!-- adr-index:start -->
| Decision | Date |
|---|---|
| [plan, approve and run refuse unless HEART_SANDBOX is docker-sbx or off](2026-10-13-refuse-unsandboxed-runs.md) | 2026-10-13 |
| [Learned scope comes from refusals, widens to a directory, and never from probes](2026-08-25-learned-scope-rules.md) | 2026-08-25 |
<!-- adr-index:end -->

## Stack decisions that bind plexus

| Decision | Date |
|---|---|
| [GitHub enforces PR plus CI on main](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-10-30-protect-main.md) | 2026-10-30 |
| [Plan text reaches pulse as artifacts](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-10-30-planning-data-in-pulse.md) | 2026-10-30 |
| [A spec is a proposal; at landing it splits](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-10-29-spec-model.md) | 2026-10-29 |
| [A feature lives in the repo whose question it answers](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-07-20-ownership-by-question.md) | 2026-07-20 |
| [Seams are pinned by contract tests in each repo](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-07-20-contract-tests.md) | 2026-07-20 |
