# What this project is, and the problem it solves

> **Synthetic data, simulated controls.** Every figure in this project comes
> from a data generator, not from a real company. The controls are modelled on
> SOX-style IT general controls but this project is not, and does not claim to
> be, SOX compliant.

---

## The problem

A public manufacturer closes its books every month. Before the numbers go out,
somebody has to answer a question that sounds simple and is not:

> **"Is this number right, and how do you know?"**

Consider one figure — *inventory value* — on a dashboard a CFO reads. To stand
behind it, you need to be able to say:

- Which files it came from, and that none of them arrived incomplete.
- That no transaction was counted twice, and none was dropped.
- That the detailed movements add up to the general ledger balance.
- That nothing was posted into a month that had already been closed and
  reported.
- That the costs used to value the inventory were approved by someone with the
  authority to approve them.
- That the number on screen was produced by a specific version of the code, on
  a specific day, and can be reproduced.

Most data pipelines answer none of these. They move data correctly almost all
of the time, and when they don't, nobody finds out until a number looks strange
in a meeting. By then, nobody can reconstruct what happened.

### Why this is hard in manufacturing specifically

The data comes from systems that disagree with each other by design:

| System | What it knows | How it counts |
| --- | --- | --- |
| **ERP** | Purchase orders, goods movements, costs, accounting documents | A unit is *shipped* when it leaves the warehouse |
| **MES** (factory floor) | Every serial number through every test station | A unit is *complete* when it passes final test |
| **Supply chain** | Shipments, promised dates, actual dates | — |
| **Finance** | Journal entries, trial balance | A period is closed when it is closed |

A server that passes final test on 31 March and ships on 2 April is finished in
March to the factory and sold in April to finance. **Both are right.** A
dashboard that shows one number labelled "units completed" is wrong for one of
them, and nobody notices until two reports disagree in a meeting.

Multiply that by a unit-of-measure mismatch on a component, an interface that
retried and posted the same receipt twice, and a standard cost somebody changed
mid-month — and the monthly close becomes an argument instead of a report.

### The failure mode that actually matters

The worst outcome is not a pipeline that breaks. A broken pipeline is obvious.

The worst outcome is a pipeline that **half-works**: it fails partway through,
leaves tables partly loaded, and the dashboard shows a number that is
internally inconsistent but looks completely normal. Nobody doubts it, because
there is nothing to doubt. That is the number that makes it into a board pack.

---

## What this project does

It builds the pipeline that *can* answer "how do you know?", for a simulated
rack-scale server manufacturer — the kind that builds racks of GPUs, CPUs, and
NVMe drives and burns them in for twelve hours before shipping.

Data flows from four simulated source systems into a star schema and into
dashboards, and **five things are true at every point along the way**:

### 1. Every number traces back to its source

Every row that lands keeps four stamps: which batch loaded it, which file it
came from, that file's SHA-256 checksum, and when. Those stamps survive all the
way into the dashboard tables.

So you can take a number off a chart and follow it back to a specific file,
verify that file's checksum, and see the exact commit of the code that
processed it. Column-level lineage then traces each certified metric through
every transformation to the raw source columns that feed it — and a CI test
**fails the build** if that trace ever breaks.

### 2. Known-bad data is planted on purpose, to prove the controls work

A control that has never caught anything has never been tested. So the
generator can inject **13 specific defects**, each behind its own switch, each
one modelled on something that genuinely goes wrong:

| | Defect | What it simulates in real life |
| --- | --- | --- |
| 1 | Duplicate goods receipts | An interface retried and posted the same receipt twice |
| 2 | Backdated GL posting | An entry posted into a month already closed and reported |
| 3 | Late-arriving MES events | Factory events landing after the period closed |
| 4 | Unit-of-measure mismatch | One component counted in boxes of 10 but recorded as singles |
| 5 | Orphaned PO lines | Line items whose header never arrived in the extract |
| 6 | Receipt without a PO | Goods received against nothing — breaks the three-way match |
| 7 | Invoice price outlier | A supplier invoiced far above the agreed price |
| 8 | Unapproved standard cost change | Somebody revalued inventory with no approval record |
| 9 | MES vs ERP completion gap | Units passed final test but were never confirmed in ERP |
| 10 | Missing supplier on shipment | A null foreign key from the supply chain feed |
| 11 | Truncated extract | A source file whose last rows were silently dropped |
| 12 | Negative on-hand inventory | An issue posted before the receipt that covers it |
| 13 | Stale extract | A feed stopped arriving and nobody noticed |

Every injected record is written to a manifest. That is the part that matters:
a test can then assert the control caught **exactly** those records — not
merely that it caught *something*. That is the difference between "the control
fired" and "the control works".

### 3. Bad numbers never reach the dashboard

Transformations build into a staging area called `publish_candidate` — never
directly into the tables the dashboards read. The reconciliations run against
that candidate. Only if **every critical check passes** does it get swapped
into `reporting`.

When a check fails, the publish is blocked and the dashboard keeps showing the
**last known-good dataset**, with an honest "last refreshed" timestamp.

This is the key design choice. A leader looking at yesterday's inventory value,
clearly labelled as yesterday's, can still make a decision. A leader looking at
today's *wrong* value cannot — and has no way to tell.

### 4. The audit trail cannot be edited

Every run writes one row per file to an append-only table: run ID, batch ID,
git commit, file checksum, rows in the file, rows loaded, rows rejected, start
and end time, status, and which database role ran it.

`UPDATE` and `DELETE` are revoked from every role, and a database trigger
raises an exception on any attempt — two independent layers, so a bypass leaves
an error rather than a silent change. Tests prove both layers hold.

### 5. Access is least-privilege, and it is tested

Four database roles, each able to do only its job:

| Role | Can | Cannot |
| --- | --- | --- |
| `loader` | Write raw data; add audit records | Read the certified dataset; edit audit history |
| `transformer` | Read raw; build the warehouse | Edit audit history |
| `reporter` | Read the certified dataset | See raw, sandbox, or half-built data |
| `auditor` | Read the audit trail and evidence | See business data at all |

The tests assert what each role is **denied**, not just what it is allowed —
which is the half that usually goes untested.

---

## What you can actually look at

Four things, once the phases land:

1. **A BI dashboard** (Tableau Public) — inventory value, first-pass yield,
   supplier on-time delivery, cost variance. What a business leader would
   actually open.
2. **A data trust console** (Streamlit) — for every metric on that dashboard:
   when it last refreshed successfully, whether its reconciliations passed,
   where its data came from, and a downloadable evidence pack.
3. **A live failure demonstration** — trigger a planted defect against an
   isolated sandbox schema and watch the publish get blocked while the live
   dashboard keeps its last good numbers. The live data is never touched.
4. **The data dictionary** (dbt docs) — every model and column described,
   generated from the same source as the code so it cannot drift.

---

## How it is built

| Layer | Tool | Why |
| --- | --- | --- |
| Data generation | Python, NumPy, Faker | Deterministic from a seed, so results reproduce exactly |
| Ingestion | pandas + pandera | Validates each file, quarantines bad rows with reasons |
| Warehouse | PostgreSQL 16 | Free, and it has the real role/grant model the access controls need |
| Transformations | dbt Core | Reviewable SQL in Git, with tests, docs, and a dependency graph |
| Lineage | sqlglot | Column-level tracing, parsed from the SQL that actually ran |
| Controls | Python + SQL | Reconciliations, publish gate, evidence packs |
| Trust console | Streamlit | Operational views that BI tools handle badly |
| BI | Tableau Public | A real BI tool, with a free public link |
| CI | GitHub Actions | Lint, tests, dbt build, and reconciliations on every change |

The whole thing runs on a laptop with `docker compose up` and `make all`, and
on free hosting tiers at no cost.

Every one of those choices — and eighteen others — is recorded in
[`docs/DECISIONS.md`](docs/DECISIONS.md) with the alternatives that were
considered and why they lost.

---

## What this project is honest about

Three things are deliberately *not* claimed, because claiming them would be
false and a reviewer would find out:

**There is no second approver.** Real change control requires that the person
who writes a change is not the person who approves it. This is a one-person
project, so that is impossible. It is documented as a known limitation with the
compensating controls that *do* work — branch protection, required CI checks, a
self-review checklist, and full traceability from every commit to a story. The
alternative, a second account approving its own work, would fabricate the
evidence while skipping the substance. That is the exact failure an auditor
exists to catch.

**The data is synthetic.** Labelled as such everywhere — in the README, in the
dashboards, in the export manifests, in the config file.

**The controls are simulated.** They are modelled on SOX IT general controls
and on how auditors test information produced by the entity. SOX compliance is
an attestation a real company's management makes about real financial
reporting. A portfolio project cannot have it, and saying otherwise would
demonstrate not understanding what SOX is.

---

## Why build this at all

Most data engineering portfolios show that data can be *moved*. Extract,
transform, load, chart. That is the easy half.

The hard half — the half that makes a BI team at a public company trust you
with the numbers that go in the board pack — is proving the data is *right*,
and being able to show your working when somebody asks. That means governed
pipelines, end-to-end lineage, reconciled metrics, and controls that have been
tested by being made to fail.

This project is the second half.

---

## Where to look next

| If you want | Read |
| --- | --- |
| Why each choice was made | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| How the pieces fit together | [`docs/architecture.md`](docs/architecture.md) |
| What each control does and how to test it | [`docs/control_matrix.csv`](docs/control_matrix.csv) |
| What each metric means in business terms | [`docs/metric_definitions.md`](docs/metric_definitions.md) |
| The planned work, story by story | [`backlog/jira_backlog.csv`](backlog/jira_backlog.csv) |
| How to run it | [`README.md`](README.md) |
