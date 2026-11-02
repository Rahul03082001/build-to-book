# Build-to-Book

**Manufacturing data pipeline and inventory reconciliation, with end-to-end
lineage and simulated SOX-style controls.**

[![CI](https://github.com/Rahul03082001/build-to-book/actions/workflows/ci.yml/badge.svg)](https://github.com/Rahul03082001/build-to-book/actions/workflows/ci.yml)

> ### Synthetic data, simulated controls
> All data in this project is **synthetic**, produced by the generator in
> [`generator/`](generator/). All controls are **simulated, SOX-style**
> controls, modelled on IT general controls and on how auditors test
> information produced by the entity.
> **This project is not SOX compliant and does not claim to be**
> ([ADR-019](docs/DECISIONS.md#adr-019-label-all-data-synthetic-and-all-controls-simulated)).

---

## Live demo

| | Link |
| --- | --- |
| Data trust console (Streamlit) | *Phase 1 — [BTB-24](backlog/jira_backlog.csv)* |
| BI dashboard (Tableau Public) | *Phase 6 — BTB-63* |
| Data dictionary (dbt docs) | *Phase 5 — BTB-57* |

Free tiers sleep when idle. **Open every link five minutes before a demo** —
see [`docs/demo_script.md`](docs/demo_script.md) for the warm-up procedure and
the backup plan.

---

## The problem

A public manufacturer closes its books monthly, and before the numbers go out
somebody must answer: *"Is this number right, and how do you know?"*

For one figure — inventory value — that means showing that no extract arrived
incomplete, that nothing was counted twice, that the detailed movements tie to
the general ledger, that nothing was posted into a closed period, that the costs
used were approved, and that the number can be reproduced from a specific
commit on a specific day.

Most pipelines answer none of that. And the failure that matters most is not a
pipeline that breaks — it is one that **half-works**, leaves tables partly
loaded, and shows a number that is internally inconsistent but looks entirely
normal.

**[Read the full overview →](PROJECT_OVERVIEW.md)** — what the project does,
the 13 planted defects, and what it deliberately does not claim.

---

## How it works, in five sentences

1. Four simulated source systems (ERP, MES, supply chain, finance) emit nightly
   extracts, each with a control-totals trailer giving its row count and value
   total.
2. Ingestion validates every file against a declared schema, quarantines bad
   rows with reasons, and stamps each landed row with its batch, source file,
   file checksum, and load time.
3. dbt builds a documented star schema into a `publish_candidate` schema —
   never straight into the tables the dashboards read.
4. Reconciliations run against that candidate, and it is swapped into
   `reporting` **only if every critical check passes**; otherwise the dashboard
   keeps the last known-good dataset with an honest refresh timestamp.
5. Column-level lineage traces every certified metric back to its raw source
   columns, and CI fails if that trace ever breaks.

[Architecture diagram and the full data flow →](docs/architecture.md)

---

## Quickstart

Requires Docker and Python 3.11+ (or [`uv`](https://docs.astral.sh/uv/), which
will fetch 3.11 for you).

```bash
git clone https://github.com/Rahul03082001/build-to-book.git
cd build-to-book

cp .env.example .env     # then edit the passwords
make venv                # create .venv and install dependencies
make init                # start PostgreSQL 16 and apply roles and grants
make check               # lint + tests
```

`make help` lists every target and the phase it belongs to. Targets for
unbuilt phases exit non-zero naming the phase, so a partial pipeline can never
report success.

```bash
make all                 # rebuild everything from scratch
make data PROFILE=full   # the large local profile: 5M+ MES events
```

---

## Project status

Built in vertical slices, so there is always something runnable
([ADR-013](docs/DECISIONS.md#adr-013-build-in-vertical-slices-starting-with-inventory)).

| Phase | Scope | Status |
| --- | --- | --- |
| **0** | Repo skeleton, 19 ADRs, backlog, CI, warehouse roles | **Complete** |
| 1 | Inventory slice end to end, deployed live | **In progress** — pipeline, controls and planted defects done (BTB-10…BTB-23, BTB-26, BTB-27, BTB-76); live deployment outstanding |
| 2 | All four source systems, 13 planted defects at volume | **In progress** — BOMs and the streaming writer done (BTB-30) |
| 3 | Full star schema in dbt, plus a performance tuning study | Not started |
| 4 | Reconciliations, evidence packs, access review, control matrix | Not started |
| 5 | Column-level lineage with a CI gate | Not started |
| 6 | Tableau dashboard, trust console, failure demo | Not started |
| 7 | Metric definitions, demo script, interview notes | Not started |

67 stories across 8 epics in
[`backlog/jira_backlog.csv`](backlog/jira_backlog.csv), ready for Jira's CSV
importer.

### What Phase 0 delivered

- **[19 architecture decision records](docs/DECISIONS.md)** — every tool, model
  shape, and control design, with the alternatives considered and why they lost.
  Free-tier hosting limits were checked against provider documentation on
  2026-11-02 and recorded with that date
  ([ADR-015](docs/DECISIONS.md#adr-015-free-tier-hosting-for-the-live-demo)).
- **[17 controls specified](docs/control_matrix.csv)** — 9 preventive, 8
  detective, each with its risk, owner, evidence location, and test procedure.
- **[Four least-privilege database roles](warehouse/sql/roles.sql)** — with
  `PUBLIC` privileges revoked and `INSERT`-only access to the audit trail.
- **[13 defects registered](generator/defects.py)** — each mapped to the control
  expected to catch it and whether that control is preventive or detective.
- **CI that gates every change** — lint, tests, and a check that fails a pull
  request whose title carries no Jira key.
- **[Branch protection runbook](docs/branch_protection.md)** — applied and
  verified on `main`: pull requests required, all three CI checks required,
  admins included so there is no bypass, and the segregation-of-duties
  limitation stated plainly rather than disguised.

### Verified on this machine

Phase 0 numbers, each from an actual run on 2026-11-02 (macOS, Docker 29.8.2,
PostgreSQL 16.15):

| Result | Command |
| --- | --- |
| 313 unit tests pass | `make test` |
| 37 role-permission integration tests pass | `make init && .venv/bin/pytest -m integration` |
| Lint clean: ruff check, ruff format, sqlfluff | `make lint` |
| Warehouse starts healthy, 9 schemas created | `make up` |
| `make roles` is idempotent — clean exit on a second run | `make roles && make roles` |
| `reporter` is refused on `raw`, permitted on `reporting` | `.venv/bin/pytest -m integration -k reporter` |
| 8 synthetic extracts generated, byte-identical across runs | `make data` |
| 14,727 rows loaded into `raw` with full provenance | `make load` |
| Rerun inserts 0 rows and reports 14,727 skipped | `make load && make load` |
| Audit trail refuses UPDATE, DELETE **and** TRUNCATE | `.venv/bin/pytest -k refuses_modification` |
| Source-side duplicate rejected while a rerun is skipped | `.venv/bin/pytest -k in_file_duplicate` |
| A one-cent trailer mismatch is caught with the row count intact | `.venv/bin/pytest -k value_total` |
| 219 dbt nodes build and pass: 18 models, 201 tests | `make build` |
| Closing inventory = quantity × cost in force, every material and period | `make build` (singular test) |
| Subledger ties to GL account 130000 to the cent, in SQL | `make build` (singular test) |
| An as-of join to the Type 2 dimension returns exactly one row per fact | `make build` (singular test) |
| 10 control results recorded per run, passes included | `make controls` |
| A forced failure blocks the publish and `reporting` keeps its previous version, unchanged | `.venv/bin/pytest -k blocks_and_leaves_reporting` |
| Repeated publishes work; exactly 2 versions retained per mart | `make publish && make publish` |
| 2 of 13 defects planted and detected, counts matching the manifest exactly | `make data-defects && make load && make build && make controls` |
| BOM explosion matches an independent two-level calculation | `make build` (singular test) |
| Writer memory flat at 91 MB from 100k to 5M rows | `.venv/bin/pytest -k streaming_memory` |
| Subledger ties to GL to the cent, every period | `.venv/bin/pytest -k subledger_ties` |
| Closing inventory = quantity × current standard cost, per material | `.venv/bin/pytest -k closing_value` |
| CI green on `main`: lint + all 403 tests, roles applied | [Actions](https://github.com/Rahul03082001/build-to-book/actions/workflows/ci.yml) |
| Direct push to `main` rejected: `GH006 ... protected branch hook declined` | `git commit --allow-empty -m "BTB-0: test" && git push origin main` |

The role tests were also confirmed to **fail** when a privilege is deliberately
leaked (`grant usage on schema raw to btb_reporter`) and to pass again once it
is revoked — a control that cannot fail is not evidence of anything.

---

## Results

> Every number in this section comes from an actual run, with the command that
> reproduces it. Sections below are empty because the runs that produce them
> have not happened yet — placeholder numbers would make this whole document
> untrustworthy.

### Defect detection

Which control caught each of the 13 planted defects.

Two of the thirteen are planted and detected so far. The rest arrive with
their source systems in Phase 2.

| # | Defect | Caught by | Type | Detected / planted |
| --- | --- | --- | --- | --- |
| 1 | Duplicate goods receipts | CTRL-001 (zero rejected rows) | Preventive — rejected at load | 9 / 9 |
| 2 | Backdated GL posting | CTRL-006 (closed-period integrity) | Preventive — blocks the publish | 4 / 4 |

Reproduce with:

```bash
make clean-db && make init && make data && make load && make build && make publish && make data-defects && make load && make build && make publish
```

The second `make publish` is blocked by CTRL-001 and CTRL-006, and `reporting`
keeps the dataset the first one published. Counts come from
`defects_manifest.json`, which lists every injected record — so the tests
assert that each control caught *exactly* what was planted, in both
directions.

### Performance tuning

Before-and-after `EXPLAIN (ANALYZE, BUFFERS)` on the slowest mart query, run on
the `full` profile.

*Produced by Phase 3 (BTB-46). Method and results:*
[`docs/tuning/`](docs/tuning/)

---

## Repository layout

```text
build-to-book/
├── PROJECT_OVERVIEW.md       the problem and what this does about it
├── Makefile                  data, load, build, controls, publish, lineage,
│                             export, evidence, test, all
├── docker-compose.yml        PostgreSQL 16, local warehouse
├── backlog/                  Jira-importable epics and stories
├── generator/                synthetic data + defect injection
│   ├── config.yaml           demo and full profiles, seed, 13 defect switches
│   ├── dataset.py            composes the source-system generators
│   ├── bom.py                multi-level bills of materials
│   ├── defects.py            defect registry and manifest contract
│   └── injectors.py          the defect injectors
├── ingestion/                validated, incremental, idempotent loaders
├── warehouse/
│   ├── sql/roles.sql         four least-privilege roles and grants
│   └── dbt/                  staging, intermediate, marts, snapshots, tests
├── controls/                 reconciliations, publish gate, IPE evidence
├── lineage/                  column-level lineage via sqlglot
├── app/                      Streamlit data trust console
├── exports/                  certified datasets for Tableau and Power BI
├── dashboards/               workbooks and screenshots
├── evidence/                 generated evidence packs
├── tests/
├── .github/workflows/        ci · pipeline · docs · demo_failure
└── docs/
    ├── DECISIONS.md          19 ADRs
    ├── architecture.md       diagram and end-to-end lineage path
    ├── control_matrix.csv    17 controls
    ├── metric_definitions.md business definitions per KPI
    ├── branch_protection.md  change control runbook
    ├── demo_script.md        five-minute walkthrough
    ├── dq_issue_log.md       data quality issues, root cause, prevention
    └── tuning/               performance case study
```

---

## Documentation

| Document | What it answers |
| --- | --- |
| [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | What problem this solves, and what it does not claim |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why every choice was made, and what lost |
| [docs/architecture.md](docs/architecture.md) | How the pieces fit, and the lineage path |
| [docs/control_matrix.csv](docs/control_matrix.csv) | Each control's risk, type, owner, evidence, and test |
| [docs/metric_definitions.md](docs/metric_definitions.md) | What each KPI means in business terms |
| [docs/branch_protection.md](docs/branch_protection.md) | Change control, and the limitation it does not fix |
| [docs/demo_script.md](docs/demo_script.md) | The five-minute walkthrough |

---

## Licence

Synthetic data and simulated controls, built as a portfolio project. No real
company, supplier, price, or person appears anywhere in this repository.
