# Decision log

Why every meaningful choice in Build-to-Book was made. Two purposes: audit-style
evidence that changes were deliberate rather than accidental, and a study guide
for defending each choice.

**Rules.** Entries are append-only and numbered. A past entry's reasoning is
never rewritten; it is superseded by a new entry and marked
`Superseded by ADR-0YY`. Each entry links its Jira key and the commit or PR
that implemented it. The first paragraph of every entry is written so a finance
manager or auditor can follow it; technical detail comes after. A metric change
needs both an ADR here and an update to [metric_definitions.md](metric_definitions.md).

All data in this project is synthetic and all controls are simulated,
SOX-style controls. Nothing here claims SOX compliance (ADR-019).

## Index

| ID | Date | Title | Status |
| --- | --- | --- | --- |
| [ADR-001](#adr-001-generate-synthetic-data-instead-of-using-a-public-dataset) | 2026-11-02 | Generate synthetic data instead of using a public dataset | Accepted |
| [ADR-002](#adr-002-model-erp-extracts-on-sap-style-structures) | 2026-11-02 | Model ERP extracts on SAP-style structures | Accepted |
| [ADR-003](#adr-003-simulate-a-rack-scale-server-manufacturer) | 2026-11-02 | Simulate a rack-scale server manufacturer | Accepted |
| [ADR-004](#adr-004-postgresql-as-the-warehouse) | 2026-11-02 | PostgreSQL as the warehouse | Accepted |
| [ADR-005](#adr-005-dbt-core-for-transformations) | 2026-11-02 | dbt Core for transformations | Accepted |
| [ADR-006](#adr-006-python-and-pandas-for-ingestion-and-reconciliation) | 2026-11-02 | Python and pandas for ingestion and reconciliation | Accepted |
| [ADR-007](#adr-007-scd-type-2-snapshots-on-material-master-and-standard-cost) | 2026-11-02 | SCD Type 2 snapshots on material master and standard cost | Accepted |
| [ADR-008](#adr-008-append-only-audit-table-for-every-pipeline-run) | 2026-11-02 | Append-only audit table for every pipeline run | Accepted |
| [ADR-009](#adr-009-sqlglot-for-column-level-lineage) | 2026-11-02 | sqlglot for column-level lineage | Accepted |
| [ADR-010](#adr-010-a-failed-critical-check-blocks-publishing) | 2026-11-02 | A failed critical check blocks publishing | Accepted |
| [ADR-011](#adr-011-document-segregation-of-duties-as-a-known-limitation) | 2026-11-02 | Document segregation of duties as a known limitation | Accepted |
| [ADR-012](#adr-012-tableau-public-for-the-live-bi-dashboard) | 2026-11-02 | Tableau Public for the live BI dashboard | Accepted |
| [ADR-013](#adr-013-build-in-vertical-slices-starting-with-inventory) | 2026-11-02 | Build in vertical slices, starting with inventory | Accepted |
| [ADR-014](#adr-014-publish-by-swapping-a-validated-build-into-reporting) | 2026-11-02 | Publish by swapping a validated build into reporting | Accepted |
| [ADR-015](#adr-015-free-tier-hosting-for-the-live-demo) | 2026-11-02 | Free-tier hosting for the live demo | Accepted |
| [ADR-016](#adr-016-two-data-profiles-demo-and-full) | 2026-11-02 | Two data profiles: demo and full | Accepted |
| [ADR-017](#adr-017-incremental-idempotent-loading-keyed-on-source-and-batch-ids) | 2026-11-02 | Incremental, idempotent loading keyed on source and batch IDs | Accepted |
| [ADR-018](#adr-018-streamlit-as-the-data-trust-console-not-the-bi-dashboard) | 2026-11-02 | Streamlit as the data trust console, not the BI dashboard | Accepted |
| [ADR-019](#adr-019-label-all-data-synthetic-and-all-controls-simulated) | 2026-11-02 | Label all data synthetic and all controls simulated | Accepted |
| [ADR-020](#adr-020-idempotent-loading-by-staging-table-and-anti-join) | 2026-11-02 | Idempotent loading by staging table and anti-join | Accepted |
| [ADR-021](#adr-021-publish-by-pointing-views-at-versioned-tables) | 2026-11-02 | Publish by pointing views at versioned tables | Accepted |
| [ADR-022](#adr-022-control-totals-as-a-json-sidecar-per-extract) | 2026-11-02 | Control totals as a JSON sidecar per extract | Accepted |
| [ADR-023](#adr-023-post-a-revaluation-when-a-standard-cost-changes) | 2026-11-02 | Post a revaluation when a standard cost changes | Accepted |

---

## ADR-001: Generate synthetic data instead of using a public dataset

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-3 | PR/commit: initial commit
- Area: Data

### Context

The project's whole claim is that its controls work. A control that has never
failed has never been tested, so we need data containing known, deliberate
errors — and we need to know exactly which records are wrong, so we can prove a
control caught all of them and nothing else. Real ERP data from a manufacturer
is not available, and would not come with a list of its own defects.

### Options considered

1. **Generate synthetic data with planted defects**: full control over what is
   wrong and where. Con: we must build and maintain a generator, and reviewers
   may discount results from data we made up.
2. **AdventureWorks or a similar sample database**: realistic schema shapes and
   zero generation effort. Con: no planted defects, so there is nothing for a
   control to detect; its retail/bicycle domain also does not match
   manufacturing.
3. **A public manufacturing dataset (for example a Kaggle production log)**:
   real-world messiness. Con: the defects are unlabelled, so "the control found
   40 issues" cannot be checked for completeness — we could never show the
   control missed nothing.

### Decision

Generate all source data synthetically, with a deterministic seed and a defect
injector controlled by per-defect switches.

### Reasons

Planting defects is the only way to measure a control's *completeness*, not
just that it fires. Each run writes `defects_manifest.json` listing every
injected record, so a test asserts the exact set a control caught — which is
how an auditor tests a control, rather than taking the operator's word for it.
Determinism matters as much: a fixed seed means a reviewer re-running the
project sees the same numbers in the README.

The cost — reviewers discounting synthetic data — is reduced by making the
generator's rules explicit and the data internally consistent: receipts
reference POs, GL entries post from goods movements at standard cost, and the
subledger ties to the GL when defects are off.

### Consequences

Easier: testing controls precisely, demonstrating a failure on demand, and
keeping the repo free of anyone's real data.

Harder: the generator becomes a real piece of software needing its own tests.
Its internal consistency rules are now load-bearing — a bug there looks like a
control failure. Mitigated by testing the clean case (defects off ⇒ subledger
equals GL) separately from the defect cases.

### How a reviewer can verify it

`generator/config.yaml` holds the seed and the 13 defect switches;
`generator/defects.py` holds the registry mapping each defect to the control
expected to catch it. `make data` then `make controls` reproduces the
detection table in the README.

---

## ADR-002: Model ERP extracts on SAP-style structures

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-3 | PR/commit: initial commit
- Area: Modeling

### Context

The extracts have to be shaped like something. Shape determines which join
paths, grains, and gotchas the project teaches — and therefore whether the
experience transfers to a real BI team's ERP data.

### Options considered

1. **SAP-style structures** (PO header and line, goods movements, production
   orders, accounting documents): the most common manufacturing ERP, so the
   patterns transfer. Con: more tables and more verbose keys; a steeper start.
2. **Flat custom tables** (one wide table per business concept): fastest to
   build and easiest to query. Con: hides the header/line split, document
   numbering, and movement-type semantics that make real ERP work hard — it
   would teach nothing transferable.
3. **Oracle EBS or Dynamics-style structures**: also realistic. Con: less
   common in manufacturing than SAP, so a narrower payoff.

### Decision

Model the ERP extracts loosely on SAP-style structures: purchase order header
and line, goods movements (material documents), production orders, and
accounting documents.

### Reasons

The hard parts of ERP reporting are structural: a header/line split that makes
every count ambiguous until you state a grain, movement types that decide
whether a quantity adds or subtracts, and document numbers that are unique only
within a year. Reproducing those means the join paths and grain decisions here
are the same ones a real engagement needs. "Loosely" is deliberate — we copy
the shapes that teach, not SAP's actual column names, which would add opacity
without insight.

### Consequences

Easier: cross-team conversations, because the vocabulary matches what ERP
analysts already use; and declaring grain honestly, because the structure
forces it.

Harder: more tables to generate and document, and every fact model must state
its grain explicitly. Accepted — stating grain is the point.

### How a reviewer can verify it

Each mart model's YAML declares its grain at the top. `fct_goods_movements` is
one row per material document line, not per document.

---

## ADR-003: Simulate a rack-scale server manufacturer

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-3 | PR/commit: initial commit
- Area: Data

### Context

The simulated business decides which metrics matter and which failure modes are
realistic. It should map onto the kind of manufacturer this project is meant to
demonstrate fitness for.

### Options considered

1. **Rack-scale server manufacturer** (GPUs, CPUs, DIMMs, NVMe, PSUs, NICs,
   chassis; burn-in testing): high-value components, serialised units, and a
   long test process that produces rich MES data. Con: requires learning the
   domain's vocabulary.
2. **Generic retail**: familiar and simple. Con: no production process, so no
   yield, WIP, cycle time, or MES-versus-ERP reconciliation — most of the
   interesting controls disappear.
3. **Bicycle manufacturer** (the AdventureWorks domain): has a BOM and
   production orders. Con: low-value components make inventory valuation
   errors uninteresting, and there is no burn-in-style test stage.

### Decision

Simulate a rack-scale server manufacturer with 4 to 6 rack configurations built
from GPU, CPU, DIMM, NVMe, PSU, NIC, and chassis components, routed through
assembly, firmware flash, burn-in, and final test.

### Reasons

The domain generates exactly the metrics a manufacturing BI team certifies:
first-pass yield and rework loops from the test route, WIP and cycle time from
burn-in's long dwell, and material inventory value that is large enough that a
unit-of-measure error on one component moves the balance sheet. High component
values are what make the inventory reconciliation worth building — a $40,000 GPU
tray mis-valued is a reportable error, where a mis-valued bicycle chain is not.

Serialised units also enable unit genealogy across MES steps, which is what
makes the MES-versus-ERP completion gap a realistic and detectable defect.

### Consequences

Easier: every KPI has an obvious business meaning, and the planted defects are
plausible rather than contrived.

Harder: the generator must keep serial-level MES events consistent with
order-level ERP confirmations, which is where the deliberate
"shipped versus complete" definition conflict in Phase 7 comes from.

### How a reviewer can verify it

`generator/config.yaml` under `domain`: the material groups, the four-step MES
route with per-step failure rates, and the failure codes.

---

## ADR-004: PostgreSQL as the warehouse

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-7 | PR/commit: initial commit
- Area: Tooling

### Context

The warehouse must run free on a laptop and free on a public URL, support a
real role-and-grant model for the access-control story, and expose good query
plans for the performance study.

### Options considered

1. **PostgreSQL 16**: free, runs in Docker locally and on managed free tiers,
   `EXPLAIN (ANALYZE, BUFFERS)` is excellent for tuning, and it has a genuine
   role/grant/default-privileges model. Con: not a columnar MPP warehouse, so
   analytical scans are slower than Snowflake or BigQuery at volume.
2. **Snowflake or BigQuery**: what many BI teams actually use, and far faster on
   wide scans. Con: trials expire and costs are unpredictable, which breaks the
   "demo at short notice, at no cost" requirement. A demo that has run out of
   credits is worse than no demo.
3. **DuckDB**: superb single-node analytics, zero setup. Con: no meaningful role
   or grant model, so the least-privilege and access-review controls — a
   substantial part of this project — could not be built or tested at all.

### Decision

PostgreSQL 16, in Docker locally and on a managed free tier for the live demo.

### Reasons

The role model decided it. Four roles with least privilege (`loader`,
`transformer`, `reporter`, `auditor`), tests that prove each role is *denied*
what it should not reach, and an access review that reads real `pg_catalog`
grants — none of that is possible on DuckDB, and it is the part of the project
closest to IT general controls. Postgres also gives append-only enforcement
through `REVOKE` plus a trigger, which is what makes the audit trail credible
rather than merely conventional.

Free and local-first keeps the demo dependable. The cost is analytical speed,
which turns into an asset: the Phase 3 tuning study needs a query that is
genuinely slow before indexing.

### Consequences

Easier: zero cost, identical local and hosted major version, strong tuning
tooling, and real access control.

Harder: the full profile's 5M+ MES events will not be fast without deliberate
indexing or partitioning — which is the study, not a surprise. It also means
the hosted demo must run the small profile (ADR-016).

### How a reviewer can verify it

`docker-compose.yml` pins `postgres:16-alpine`;
`warehouse/sql/roles.sql` defines the four roles and their grants.

---

## ADR-005: dbt Core for transformations

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-3 | PR/commit: initial commit
- Area: Tooling

### Context

Transformations must be reviewable in Git, tested, documented, and traceable.
Three capabilities that often come from three separate tools — tests, docs, and
a dependency graph — are all requirements here.

### Options considered

1. **dbt Core with dbt-postgres**: tests, docs, snapshots, and a lineage graph
   in one tool; models stay as reviewable SQL in version control; it is the
   industry standard, so the skill transfers. Con: another tool to learn, and
   its lineage is model-level only (see ADR-009).
2. **pandas-only transforms**: one language for the whole pipeline. Con: no
   built-in lineage, no generated documentation, and transformations become
   Python that a SQL-literate analyst or auditor cannot review. It also pulls
   the whole dataset through memory.
3. **Stored procedures in Postgres**: runs next to the data, no new dependency.
   Con: hard to review in diffs, hard to unit test, and no generated docs or
   graph — the opposite of what an audit trail needs.

### Decision

dbt Core with dbt-postgres for all warehouse transformations, in
`warehouse/dbt/`.

### Reasons

Reviewability is the deciding factor. A model is a SQL file in a pull request,
so a reviewer — or an auditor — reads the transformation itself rather than
inferring it from Python. `dbt docs` doubles as the data dictionary, which
means documentation is generated from the same source as the code and cannot
drift. Snapshots give SCD Type 2 (ADR-007) without hand-written merge logic,
and `dbt build` runs models and their tests together, so a model that breaks a
business rule fails the build rather than quietly publishing.

### Consequences

Easier: generated docs and graph, declarative tests, and a structure
(staging → intermediate → marts) that enforces where logic belongs.

Harder: lineage stops at the model boundary, which is why Phase 5 adds
column-level tracing with sqlglot (ADR-009). dbt also needs a profile and
target configuration that differs between local, CI, and hosted runs.

### How a reviewer can verify it

`make build` runs `dbt build`; `warehouse/dbt/` holds the project, and every
model has a YAML entry describing it.

---

## ADR-006: Python and pandas for ingestion and reconciliation

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-3 | PR/commit: initial commit
- Area: Tooling

### Context

Ingestion has to do more than move bytes: validate each file against a declared
schema, reject and log bad rows rather than dropping them, compute row counts
and checksums, and write run metadata. Reconciliations must compare numbers
across systems and record expected-versus-actual with a tolerance.

### Options considered

1. **Python with pandas plus a schema-validation library**: expressive
   validation, straightforward hashing and run metadata, and reconciliation
   logic that reads like the business rule it implements. Con: pandas holds data
   in memory, so the full profile needs chunked reads.
2. **Pure SQL `COPY` loading**: fastest possible load, no Python. Con: `COPY`
   either accepts a file or rejects it — there is no natural place to quarantine
   individual bad rows, compute a file checksum, or record run metadata. The
   validation and audit story, which is the point, would be missing.
3. **A managed ingestion tool (Airbyte, Meltano)**: less code to own. Con: heavy
   infrastructure for flat-file ingestion, and it hides exactly the validation
   and provenance logic this project exists to demonstrate.

### Decision

Python 3.11 with pandas for ingestion and reconciliation, using **pandera** for
declarative schema validation.

### Reasons

The evidence requirements drive this. Each load must stamp `_batch_id`,
`_source_file`, `_file_sha256`, and `_loaded_at` on every row, count rows in
versus loaded versus rejected, and write one audit row per file per run. That is
natural in Python and awkward in `COPY`.

pandera is chosen over hand-written checks or Great Expectations because
schemas are declared as code next to the loader, validate a DataFrame in one
call, and return the failing rows — which is what the rejected-row log needs. It
is a library rather than a platform, so it adds no services to operate. Great
Expectations is more capable but brings its own store and configuration layer,
which is more than a flat-file pipeline needs.

### Consequences

Easier: precise validation, per-row rejection with reasons, and reconciliations
that read like their business definitions.

Harder: memory discipline on the full profile — loaders must read in chunks
rather than whole files. Recorded as follow-up work in Phase 2.

### How a reviewer can verify it

`pyproject.toml` lists pandas and pandera; `ingestion/__init__.py` names the
four provenance columns stamped on every landed row.

---

## ADR-007: SCD Type 2 snapshots on material master and standard cost

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-43 | PR/commit: initial commit
- Area: Modeling

### Context

Inventory is valued at standard cost. If a standard cost changes mid-period, the
reported inventory value changes with it — so the question "who changed this
cost, to what, and when?" has to be answerable months later. The same applies to
material master attributes such as material group and unit of measure.

### Options considered

1. **SCD Type 2 snapshots** (`valid_from` / `valid_to` rows): full history, so
   any past valuation can be reconstructed and any change attributed. Con: joins
   must be as-of rather than simple equality, which is easy to get wrong.
2. **SCD Type 1, overwrite in place**: simplest, and always shows current truth.
   Con: history is destroyed. Last month's inventory value becomes impossible to
   reproduce, and an unapproved cost change leaves no trace — the planted
   `unapproved_standard_cost_change` defect would be undetectable.
3. **Keep every raw extract and reconstruct history on demand**: no modelling
   work up front. Con: pushes the hard part onto every consumer, and every
   analyst reinvents the as-of logic differently.

### Decision

dbt snapshots with SCD Type 2 on the material master and on standard cost.

### Reasons

Auditors ask who changed a standard cost and when, so history must be
reconstructible — that is the first-order reason. Type 2 also makes a control
possible that is otherwise impossible: every cost change in the snapshot must
have a matching approval record, which is how the unapproved-change defect gets
caught. And it makes restatement honest — a prior period can be revalued at the
cost that was actually in force, not at today's cost.

### Consequences

Easier: point-in-time valuation, change attribution, and the cost-approval
control.

Harder: every join to these dimensions must be as-of (`posting_date between
valid_from and valid_to`), which is a common source of silent duplication. Phase
3 adds a dbt test asserting that an as-of join does not multiply fact rows.

### How a reviewer can verify it

`warehouse/dbt/snapshots/` holds the snapshot definitions; the Phase 4
cost-approval control joins changes to approval records.

---

## ADR-008: Append-only audit table for every pipeline run

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-18 | PR/commit: initial commit
- Area: Controls

### Context

Every certified number needs run-level evidence behind it: which file produced
it, what the file's checksum was, how many rows went in and came out, which code
version ran, and who ran it. That evidence is only worth something if it cannot
be edited after the fact.

### Options considered

1. **A database table with `UPDATE` and `DELETE` revoked, plus a trigger that
   raises on any attempt**: queryable, joinable to the data it describes, and
   tamper-evident at two layers. Con: `superuser` can still bypass it, so the
   guarantee is strong but not absolute.
2. **Application log files**: nearly free to produce. Con: logs rotate and
   expire, cannot be joined to the data, and are hard to query — "show me the
   checksum of the file behind this number" becomes a grep across archives.
3. **An external append-only service or object storage with
   write-once policies**: a genuinely immutable store. Con: more
   infrastructure and cost than a solo free-tier project can carry, and it
   separates the evidence from the data it describes.

### Decision

`audit.pipeline_runs` as an append-only table: one row per file per run, with
`UPDATE` and `DELETE` revoked from every role and a trigger that raises on any
attempt.

### Reasons

Run-level evidence is the core of the audit trail, and it has to be queryable to
be useful — a reviewer should be able to join a reported number to the file
checksum that produced it in one SQL statement. Defence in depth matters here:
revoking the privilege stops the ordinary path, and the trigger catches anything
that gets a privilege it should not have, so a bypass leaves an error rather
than a silent edit. Both layers are tested in Phase 1.

Recording the git commit SHA alongside counts and checksums ties the data to the
exact code that produced it, which is what makes a past run reproducible.

### Consequences

Easier: reproducing any run, proving completeness at load, and answering
"when did this number last change, and why".

Harder: corrections cannot be edited in; a mistake is fixed by a new run with a
new batch ID, and the wrong row stays visible. That is the intended behaviour
for an audit trail, and it means the table only grows — Phase 4 notes retention
as production follow-up. A `superuser` connection can still bypass both layers,
documented as a known limitation.

### How a reviewer can verify it

`warehouse/sql/roles.sql` grants the loader `INSERT` only on `audit`; the Phase
1 tests attempt an `UPDATE` and a `DELETE` and assert both are refused.

---

## ADR-009: sqlglot for column-level lineage

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-58 | PR/commit: initial commit
- Area: Lineage

### Context

The project promises that any dashboard number can be traced to the exact source
columns that produced it. dbt's graph shows that one model depends on another,
which does not answer "which source column feeds `inventory_value_usd`?"

### Options considered

1. **sqlglot over dbt's compiled SQL**: parses the actual SQL that ran and
   resolves column references through it, so lineage reflects reality rather
   than documentation. Runs as a library in CI with no services. Con: we write
   the traversal ourselves, and unusual SQL may need special handling.
2. **OpenLineage with Marquez**: a proper lineage platform with a UI and an
   event standard. Con: requires running a server and a database for a solo
   project whose hosting budget is zero — heavy infrastructure for one report.
3. **Hand-maintained lineage documentation**: no tooling at all. Con: it is
   wrong the moment a model changes, and nothing detects the drift. The one
   thing lineage must not do is silently go stale.

### Decision

sqlglot over dbt's compiled SQL, emitting `lineage/kpi_lineage.json` and a
readable `lineage/kpi_lineage.md`, driven by models tagged
`meta: {certified: true}`.

### Reasons

Parsing compiled SQL means the lineage is derived from what actually executed,
so it cannot disagree with the pipeline. Because it is a library, it runs inside
the existing CI job — which enables the control that matters most: a CI test
that **fails when a certified KPI's lineage cannot be fully resolved to raw
source columns**. That turns lineage from a document into an enforced property.
OpenLineage would give a nicer UI but its operational cost is not justified
when the deliverable is a report plus a CI gate.

### Consequences

Easier: KPI-to-source-column tracing, a lineage lookup page in the trust
console, and protection against lineage breaking silently.

Harder: we own the resolver. Certain SQL constructs (`SELECT *`, some window
functions, lateral joins) need explicit handling, and the CI gate will fail
until they are handled — which is the correct, if occasionally inconvenient,
behaviour.

### How a reviewer can verify it

`make lineage` writes `lineage/kpi_lineage.json`; the Phase 5 CI test asserts
every certified KPI resolves to raw source columns.

---

## ADR-010: A failed critical check blocks publishing

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-23 | PR/commit: initial commit
- Area: Controls

### Context

When a reconciliation fails, there are two possible behaviours: publish the
numbers anyway and raise a warning, or refuse to publish. The choice decides
whether the dashboard can ever show a number the controls rejected.

### Options considered

1. **A failed critical check blocks the publish**: wrong numbers never reach a
   leader's screen. Con: a false positive, or a check that is too strict, stops
   a refresh that would have been fine — availability is traded for integrity.
2. **Warn-only checks**: the dashboard always refreshes, and failures appear as
   a banner. Con: warnings get ignored. A number that is on screen is treated as
   true regardless of what a banner says, so this is integrity theatre.
3. **Block only on a large variance, warn on a small one**: materiality-based,
   like real financial reporting. Con: requires defensible materiality
   thresholds per metric, which is a bigger exercise than this project can
   justify at the moment — kept as follow-up.

### Decision

Checks are classified `critical` or `warning`. A failed `critical` check blocks
the publish; a failed `warning` is recorded and surfaced but does not block.

### Reasons

A check that only warns still lets bad numbers reach leaders, and the cost of a
wrong certified number is much higher than the cost of a stale one — a leader
who sees yesterday's inventory value with an honest timestamp can still decide,
while one who sees today's wrong value cannot. Combined with the publish gate
(ADR-014), blocking is also *cheap*: the previous good dataset stays live, so
blocking degrades freshness rather than availability.

Keeping a `warning` class avoids the opposite failure, where every minor data
oddity stops the pipeline and the team learns to disable checks.

### Consequences

Easier: trusting any number the dashboard shows, because it passed every
critical check in the run that produced it.

Harder: a false positive on a critical check stops the refresh, so check
tolerances must be set deliberately and each failure needs triage. Every check's
severity and tolerance is recorded in `docs/control_matrix.csv` so the choice is
reviewable rather than buried in code.

### How a reviewer can verify it

`controls/__init__.py` defines the two severities; the Phase 1 failure scenarios
show a critical failure blocking the publish while `reporting` keeps its last
good dataset.

---

## ADR-011: Document segregation of duties as a known limitation

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-6 | PR/commit: initial commit
- Area: Controls

### Context

Real SOX change control requires that the person who writes a change is not the
person who approves it. This is a one-person project, so genuine segregation of
duties is impossible. The question is what to do about that.

### Options considered

1. **Document it honestly as a known limitation, and list compensating
   controls**: accurate, and it shows the reviewer that the gap is understood
   rather than overlooked. Con: the project cannot claim a complete change
   control.
2. **Self-approve pull requests from a second GitHub account**: the repository
   would show two approvers and look compliant. Con: it misrepresents the
   control. The whole value of segregation of duties is that a second *person*
   looked; faking the artefact while skipping the substance is the kind of
   control failure an auditor exists to find, and discovering it would destroy
   credibility.
3. **Skip change control entirely**: no claim, no misrepresentation. Con:
   throws away branch protection, CI gates, and traceability, which are real
   controls that do work with one person.

### Decision

Segregation of duties is documented as a known limitation of a one-person
project. No second approver is simulated.

### Compensating controls

These are real and in place, and they are what a reviewer should test instead:

- **Branch protection on `main`**: no direct pushes; every change arrives by
  pull request. Steps to apply are in [branch_protection.md](branch_protection.md).
- **Required CI status checks**: lint, tests, `dbt build` with tests, and the
  reconciliations must pass before a merge is possible. A human cannot wave
  these through.
- **A pull request self-review checklist**: an explicit, recorded pass over the
  change against a fixed list, so review is a deliberate step rather than a
  habit of merging one's own work.
- **Linear, traceable history**: every commit message and PR title carries its
  Jira key, enforced by a CI check, so every change in `main` traces to a story
  and an ADR.
- **An append-only audit trail** (ADR-008) that records which code version
  produced which data, independent of who approved the change.

In production this control would be completed by requiring a second approver
with `CODEOWNERS` and a minimum of one approving review from someone other than
the author, plus a separate release approver for production deploys. The gap is
the approver, not the mechanism — the mechanism is already here.

### Reasons

Overclaiming a control is worse than declaring a gap: a reviewer who finds a
fabricated approval stops trusting everything else in the project, while one who
finds a documented limitation sees judgement. Naming the compensating controls
also makes the limitation testable — a reviewer can verify branch protection and
the CI gates directly.

### Consequences

Easier: the project's control claims survive scrutiny, and the limitation
becomes a good interview answer about preventive versus detective controls.

Harder: the change-management control is genuinely incomplete, and that must be
stated in the README and the control matrix rather than mentioned only here.

### How a reviewer can verify it

[branch_protection.md](branch_protection.md) holds the settings to apply and
the self-review checklist; `.github/workflows/ci.yml` holds the required checks;
`docs/control_matrix.csv` records this control as partially implemented with
this ADR as its reference.

---

## ADR-012: Tableau Public for the live BI dashboard

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-63 | PR/commit: initial commit
- Area: Tooling

### Context

The project needs a dashboard in a tool a BI team actually uses, reachable from
a public URL at no cost, and demonstrable on short notice.

### Options considered

1. **Tableau Public** (plus Power BI Desktop locally if on Windows): a real BI
   tool, free, and it gives a public URL that works on a phone. Con: everything
   published is public, and file-based extracts have no automated refresh.
2. **Power BI Service only**: closer to many manufacturers' stacks. Con: the
   free tier cannot share a report publicly with an anonymous viewer, so there
   is no link to send; Power BI Desktop is also Windows-only.
3. **Streamlit as the only dashboard**: already in the stack, connects live to
   Postgres, and refreshes automatically. Con: it is not a BI tool this kind of
   team uses, so it does not demonstrate the skill the role asks for. Streamlit
   keeps a different job instead (ADR-018).

### Decision

Tableau Public hosts the live BI dashboard. On Windows, the same pages are also
rebuilt in Power BI Desktop, with the `.pbix` and screenshots committed.

**Open question for BTB-63:** whether a Windows machine is available. The answer
is recorded here as an amendment to this ADR before the Phase 6 dashboard work
starts.

### Reasons

Tableau Public is the only option that is simultaneously a real BI tool, free,
and publicly linkable — which the "demo at short notice" requirement makes
non-negotiable. Keeping Power BI Desktop as a parallel deliverable covers the
other half of the usual job requirement without depending on a paid service.

Two consequences of Tableau Public shaped the architecture and were checked
before deciding:

- **Everything published is public.** This is only acceptable because all data
  is synthetic (ADR-001, ADR-019). With real data it would be disqualifying, and
  it is worth saying so in an interview.
- **Automated refresh is limited to Google Sheets** (refreshed roughly daily);
  file-based extracts such as CSV or Parquet have to be republished by hand.
  So the nightly pipeline cannot refresh Tableau on its own. The trust console
  (ADR-018) connects live to Postgres and therefore shows the true refresh
  state, while the Tableau workbook carries the batch ID and refresh timestamp
  of the extract it was built from, so a stale dashboard is visibly stale rather
  than silently wrong.

Published workbooks are also capped (around 15 million rows per data source),
which is comfortably above the demo profile and another reason the hosted demo
runs the small profile (ADR-016).

### Consequences

Easier: a free public dashboard link that works on a phone, and credible
coverage of the BI-tool requirement.

Harder: Tableau refresh is a manual step after a pipeline change, so the demo
script must account for it, and the workbook must display the batch ID it was
built from. No row-level security is possible, which is fine for synthetic data
and must not be presented as a production pattern.

### How a reviewer can verify it

`dashboards/` holds the workbooks and screenshots; the README links the live
Tableau URL; each dashboard page footer shows the batch ID and extract
timestamp.

---

## ADR-013: Build in vertical slices, starting with inventory

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-9 | PR/commit: initial commit
- Area: Tooling

### Context

The project has seven phases and many layers: generation, ingestion, warehouse,
controls, lineage, BI. They can be built layer by layer or slice by slice.

### Options considered

1. **Vertical slices**: one thin end-to-end path first (inventory), then widen.
   A working demo exists within days, and integration problems surface
   immediately. Con: some early components get revisited as later slices arrive.
2. **Layer by layer**: finish all generation, then all ingestion, then all
   modelling. Each layer is coherent and built once. Con: nothing is demoable
   until the last layer lands, and every integration assumption stays untested
   until the end — the classic way to discover at week six that the grain was
   wrong from week one.
3. **Breadth-first on sources, depth later**: all extracts first, then one
   pipeline over them. Con: same late-integration risk, plus a large volume of
   generator code written before any of it is validated by a downstream
   consumer.

### Decision

Build in vertical slices. Phase 1 is a thin inventory path — generate, ingest,
model, reconcile, gate, publish, dashboard — deployed live. Later phases widen
it without breaking the live demo.

### Reasons

A thin end-to-end path is demoable within days, which matters when the project
must be shown at short notice; from Phase 1 onwards there is always something
live to show. It also front-loads the integration risk: the publish gate, the
audit table, and the role model are all exercised against real data in the first
slice, so a mistake in any of them is found while the surface area is small.

Inventory is the right first slice because it is the shortest path to a
reconciliation that actually means something — goods movements against the GL
inventory account — so the slice proves the control pattern, not just the
plumbing.

### Consequences

Easier: continuous demonstrability, early validation of the hardest
architectural choices, and a natural review checkpoint at each phase.

Harder: the generator and the staging layer must be designed to extend rather
than be rewritten when Phase 2 adds MES, supply chain, and finance sources. The
`demo`/`full` profile split and the per-extract schema declarations exist partly
to make that extension cheap.

### How a reviewer can verify it

`backlog/jira_backlog.csv` orders stories by slice rather than by layer; Phase 1
closes with a live URL.

---

## ADR-014: Publish by swapping a validated build into reporting

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-23 | PR/commit: initial commit
- Area: Controls

> **Amendment 2026-11-02.** The decision below stands: build into a candidate,
> swap only if every critical check passes. The *mechanism* described under
> Consequences — renaming schemas inside a transaction — is superseded by
> [ADR-021](#adr-021-publish-by-pointing-views-at-versioned-tables), which
> found that `ALTER DEFAULT PRIVILEGES` is keyed to the schema name and would
> silently strip the reporter grants from each new build. The original
> reasoning is left intact rather than rewritten.

### Context

A pipeline run can fail halfway. What a leader sees while that is happening, and
afterwards, is a design decision — not an accident.

### Options considered

1. **Build into `publish_candidate`, run critical checks, then swap into
   `reporting`**: `reporting` only ever holds a complete, validated dataset, and
   a failure leaves the previous good one in place. Con: roughly double the
   storage, and the swap must be atomic.
2. **Build directly into `reporting`**: simplest, no extra schema. Con: a
   mid-run failure leaves half-loaded tables, so the dashboard shows a dataset
   that is internally inconsistent — the worst outcome, because it looks fine.
3. **Block the refresh on failure but leave tables as they are**: no swap
   machinery. Con: if the failure happened mid-load, "as they are" is already
   half-loaded. This only works if loading is atomic, which it is not.

### Decision

Build into `publish_candidate`, run the critical checks, and swap into
`reporting` only if all pass. On failure, `reporting` keeps the last good
dataset and `audit.publish_log` records what blocked the publish.

### Reasons

When a run fails, leaders still see trusted numbers with an honest refresh time.
That sentence is the whole control: the dashboard's trust tiles show the last
*successful* refresh, so a stale number is visibly stale, while a half-loaded
number would be invisibly wrong. It also makes blocking on a failed check
(ADR-010) a cheap decision rather than an outage, which is what keeps the
critical checks from being disabled under pressure.

The swap is done by renaming schemas inside a transaction, so readers see either
the old dataset or the new one and never a mixture.

### Consequences

Easier: safe failures, a believable trust indicator, and a failure demo that is
genuinely reassuring to watch.

Harder: about twice the storage, which interacts with the free-tier cap
(ADR-015) and is another reason the hosted demo runs the small profile. Long-
running reader transactions can also block a schema rename; the gate takes a
lock with a timeout and records a failed publish rather than waiting
indefinitely.

### How a reviewer can verify it

`warehouse/sql/init/00_schemas.sql` creates both `publish_candidate` and
`reporting`; the Phase 1 forced-failure test asserts `reporting` still holds the
previous dataset and `audit.publish_log` holds the reason.

---

## ADR-015: Free-tier hosting for the live demo

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-24 | PR/commit: initial commit
- Area: Tooling

### Context

The demo must open on a public URL at no cost, from a phone, at short notice,
and without depending on a laptop being awake. Free tiers have caps and sleep
behaviour that shape what can be hosted, so the limits were checked before
choosing rather than discovered later.

### Options considered

1. **Managed free-tier Postgres + Streamlit Community Cloud + Tableau Public +
   GitHub Pages**: zero cost, four public URLs, nothing depends on local
   hardware. Con: every component has caps and sleeps when idle.
2. **A paid cloud VM running everything**: no caps, full control, no sleeping.
   Con: ongoing cost and real operational work (patching, backups, TLS) that
   adds nothing to what the project demonstrates.
3. **Local-only, demonstrated over screen share**: no limits at all and the full
   data profile. Con: fails the "open it on a phone at short notice"
   requirement, and a laptop problem becomes a failed interview.

### Decision

Host on free tiers: **Neon** for Postgres, **Streamlit Community Cloud** for the
trust console, **Tableau Public** for the BI dashboard, and **GitHub Pages** for
dbt docs.

### Free-tier limits, checked 2026-11-02

**Neon (Postgres).** Free plan: 1 GB storage per project and 20 GB across all
projects; writes that would grow storage fail once a cap is hit, and overages are
not billed. 100 CU-hours of compute per project per month, resetting monthly,
autoscaling to 2 CU. Compute **suspends after 5 minutes of inactivity and this
cannot be disabled on the free plan**; it resumes on the next connection.
10 branches per project, 5 GB of egress per project per month. No documented
maximum connection count, but connections drop when the compute budget is
exhausted.

*Chosen over Supabase*, whose free plan gives 500 MB per database and — decisively
— **pauses a project after one week of inactivity, requiring a manual restore
from the dashboard** before the app works again. A demo that needs a manual
un-pause before it can be opened fails the short-notice requirement. Neon's
5-minute suspend resumes by itself on the next query, so the worst case is a slow
first page load.

**Streamlit Community Cloud.** Apps with no traffic for 12 hours go to sleep and
wake when visited. Roughly 1 GB of memory per app. Public apps are unlimited;
the free tier allows one private app. Streamlit's documentation states these
limits may change without notice.

**Tableau Public.** Data sources are capped at about 15 million rows. Automated
refresh covers Google Sheets only (roughly daily); file-based extracts must be
republished manually. Everything published is publicly visible, and no row-level
security is available. See ADR-012.

**GitHub Pages.** Published sites up to 1 GB, a soft bandwidth limit of 100 GB
per month, and a soft limit of 10 builds per hour that **does not apply when
publishing from a custom Actions workflow** — which is how `docs.yml` deploys,
so dbt docs can be republished on every merge.

### Reasons

Zero cost, public links, and no dependence on a laptop during an interview. The
limits confirm the architecture rather than fight it: Neon's 1 GB cap is why the
hosted demo runs the `demo` profile and the `full` profile stays local
(ADR-016), and the publish gate's duplicate schemas (ADR-014) make that cap
tighter still. Tableau Public's lack of automated refresh for file extracts is
why the trust console, not Tableau, is the authoritative view of refresh state.

The sleep behaviour is the main operational risk, and it is handled by procedure
rather than by hope: the demo script's first instruction is to open every live
link five minutes beforehand, and a dated screen recording plus screenshots are
linked from the README for when a service is unavailable anyway.

### Consequences

Easier: four public URLs at no cost, and a demo that survives a dead laptop.

Harder: three sleep behaviours to warm up before a demo; a hard 1 GB data
ceiling; manual Tableau refresh; and limits that providers may change without
notice — so these numbers carry their check date and should be re-verified
before relying on them. The nightly `pipeline.yml` run also keeps the Neon
project active, which incidentally reduces cold starts.

### How a reviewer can verify it

The README lists all four live URLs; `.github/workflows/pipeline.yml` runs
nightly against the hosted database; `docs/demo_script.md` holds the warm-up
procedure and the backup plan.

---

## ADR-016: Two data profiles: demo and full

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-36 | PR/commit: initial commit
- Area: Data

### Context

Two requirements pull in opposite directions. The hosted demo must fit inside
1 GB of free-tier storage (ADR-015). The performance tuning study needs enough
data that a query is genuinely slow — millions of MES events — which does not
fit.

### Options considered

1. **Two profiles from one generator**: `demo` small and hosted, `full` large
   and local. Both requirements met from the same code. Con: two configurations
   to keep consistent, and some bugs only appear at volume.
2. **One mid-sized profile for both**: a single number to reason about. Con: it
   would be simultaneously too big for the free tier and too small for tuning —
   failing both requirements rather than compromising between them.
3. **Generate large data locally and host a pre-aggregated extract**: smallest
   hosted footprint. Con: the hosted demo would no longer run the real pipeline,
   so the live trust console could not show genuine run history or
   reconciliations.

### Decision

Two profiles in `generator/config.yaml`. `demo`: 3 months, 60 materials, 12
suppliers, 1,200 PO lines, 400 production orders, 60,000 MES events. `full`: 12
months, 300 materials, 40 suppliers, 20,000 PO lines, 8,000 production orders,
5,000,000 MES events.

### Reasons

Free hosting has a hard storage limit while the tuning study needs real volume,
and one size cannot satisfy both. Keeping both profiles in one generator, driven
by the same seed and the same consistency rules, means the hosted demo runs the
*same pipeline* as the local full run — only the row counts differ. That keeps
the live trust console honest: its run history and reconciliation results come
from real runs, not from a fixture.

### Consequences

Easier: a hosted demo that fits, and a local profile big enough for `EXPLAIN
ANALYZE` to be interesting.

Harder: performance problems that only appear at 5M rows will not be caught by
the hosted run, so the tuning study must be run deliberately on `full`. CI runs
the `demo` profile for speed, which means a volume regression could reach `main`
unnoticed — accepted, with a periodic `full` run as follow-up work.

### How a reviewer can verify it

`generator/config.yaml` declares both profiles; `make data PROFILE=full`
switches between them; `docs/tuning/` records the full-profile measurements.

---

## ADR-017: Incremental, idempotent loading keyed on source and batch IDs

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-15 | PR/commit: initial commit
- Area: Data

### Context

Pipelines fail and get rerun. If a rerun loads the same goods receipts again,
inventory is overstated and the GL reconciliation breaks — and the cause looks
like a data problem rather than a pipeline problem. Reruns must be safe by
construction.

### Options considered

1. **Incremental loading keyed on source IDs plus batch IDs**: a rerun inserts
   only genuinely new rows, so running twice has the same effect as running
   once. Con: requires a reliable natural key per extract and a merge path
   rather than a plain insert.
2. **Truncate and reload everything each run**: trivially idempotent and simple
   to reason about. Con: it discards the history and provenance the audit trail
   depends on, scales badly as volume grows, and — the deciding objection —
   hides duplicate handling entirely. The project could never demonstrate that
   duplicates are detected, which is one of the planted defects.
3. **Append everything and deduplicate downstream**: the fastest load. Con: raw
   becomes untrustworthy, every consumer must remember to deduplicate, and the
   row counts in the audit trail stop matching the source control totals.

### Decision

Incremental, idempotent loading keyed on each extract's natural source ID, with
every row stamped with its `_batch_id`.

### Reasons

A rerun after a failure must never double-count inventory or postings — that is
the requirement, and keying on source IDs is what makes it true regardless of
how many times a job runs. Keeping the batch ID alongside preserves the audit
story: the same logical row can be traced to the run that first landed it, so
"how many rows did this file contribute?" stays answerable.

Rejecting truncate-and-reload is also what makes the duplicate-receipt defect
meaningful. A genuine duplicate *within a source extract* is a real control
finding, and only a loader that distinguishes "already loaded" from "duplicated
at source" can tell the difference between a harmless rerun and a broken
upstream interface.

### Consequences

Easier: safe reruns, honest row counts, and a detectable difference between a
retry and a source-side duplicate.

Harder: every extract needs a declared natural key, and a source that reuses IDs
across periods would break the assumption — so keys are documented per extract
and tested. Phase 1 proves the property directly: two consecutive reruns must
add zero rows.

### How a reviewer can verify it

The Phase 1 idempotency test runs the loader twice and asserts the second run
adds no rows; `ingestion/__init__.py` names the provenance columns.

---

## ADR-018: Streamlit as the data trust console, not the BI dashboard

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-65 | PR/commit: initial commit
- Area: Tooling

### Context

Two different audiences need two different views. A business leader wants
inventory value and yield. An engineer, auditor, or reviewer wants run history,
reconciliation results, lineage, and downloadable evidence. One tool serving
both would serve neither well.

### Options considered

1. **Streamlit for operational trust views, Tableau for BI**: each tool does
   what it is good at. Con: two applications to deploy and keep alive.
2. **Build the trust views inside Tableau too**: one tool, one link. Con:
   Tableau is built for aggregated visual analysis, not for run logs, text-heavy
   check results, lineage trees, or file downloads. An evidence pack download is
   not something a BI tool does.
3. **Streamlit for everything**: one application, live database connection, no
   manual refresh. Con: it is not a BI tool a manufacturing BI team uses, so it
   would not demonstrate the dashboard skill the role requires (ADR-012).

### Decision

Streamlit is the data trust console: pipeline runs, reconciliation results,
data-quality issues, lineage lookup, and evidence pack downloads. Tableau Public
is the BI dashboard. Streamlit connects only through the read-only `reporter`
role against certified reporting tables.

### Reasons

Run history, check results, lineage, and evidence are operational views that BI
tools handle poorly — they are tabular, text-heavy, interactive in a
drill-to-detail way, and sometimes they are file downloads. Streamlit does all of
that in Python, next to the code that produced the evidence.

The split also strengthens the architecture: because Streamlit reads the
database live, it is the authoritative view of refresh state, which is exactly
what Tableau Public cannot be (ADR-012). And restricting it to the `reporter`
role makes the least-privilege model observable — the console physically cannot
read `raw` or `sandbox`.

### Consequences

Easier: operational transparency, a lineage lookup that would be impractical in
Tableau, and a live demonstration that the `reporter` role is sufficient for all
reporting.

Harder: two hosted applications, each with its own sleep behaviour to warm up
before a demo. The trust console must also be clear that it is not the certified
BI layer, so a viewer does not mistake an operational view for a certified
number.

### How a reviewer can verify it

`app/` holds the console; its database connection uses `btb_reporter_svc`; the
Phase 1 role tests prove that role cannot read `raw`.

---

## ADR-019: Label all data synthetic and all controls simulated

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-8 | PR/commit: initial commit
- Area: Controls

### Context

The project implements controls modelled on SOX IT general controls and produces
auditor-style evidence. How that is described decides whether a reviewer reads
it as competent or as misleading.

### Options considered

1. **Label the data synthetic and the controls "simulated, SOX-style"
   everywhere**: accurate, and it still communicates the design intent. Con:
   slightly less impressive at a glance than an unqualified claim.
2. **Describe the project as SOX compliant**: stronger-sounding. Con: it is
   false. SOX compliance is an attestation about a real company's financial
   reporting, made by its management and tested by external auditors — a
   portfolio project cannot have it. Anyone who knows what SOX is would read the
   claim as evidence that the author does not.
3. **Avoid the word SOX entirely**: no risk of overclaiming. Con: it also hides
   the point. The control design *is* derived from ITGC and IPE concepts, and
   naming that framework is what makes the work legible to a BI or audit team.

### Decision

Every surface — README, dashboards, trust console, exports, and documentation —
states that the data is synthetic and the controls are simulated, SOX-style.
The project never claims SOX compliance.

### Reasons

Overclaiming compliance would mislead reviewers and damage credibility, and it
is the easiest claim in the project to falsify. The honest framing loses nothing:
"simulated, SOX-style controls" tells a reader precisely what was built and what
was not, and shows familiarity with the distinction between designing a control
and attesting to one — which is itself the thing being assessed.

Labelling the data synthetic is also a practical necessity, because Tableau
Public publishes everything publicly (ADR-012). Synthetic data makes that
acceptable; real data would not be.

### Consequences

Easier: claims that survive scrutiny, and a clear answer to "what would change
in production?" — a real ERP connection, a second approver, and an actual
auditor.

Harder: every published surface needs the label, which means it must be built
into templates (dashboard footers, export manifests, console headers) rather
than remembered each time.

### How a reviewer can verify it

The README states it in the opening section; `generator/config.yaml` carries the
notice at the top of the file; export manifests and dashboard footers carry it
too.

---

## ADR-020: Idempotent loading by staging table and anti-join

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-15 | PR/commit: BTB-9-phase-1-decisions
- Area: Data

### Context

[ADR-017](#adr-017-incremental-idempotent-loading-keyed-on-source-and-batch-ids)
settled that loading is incremental and idempotent on each extract's natural
source ID. It did not settle *how*, and the choice turns out to decide whether
one of the planted defects is detectable at all.

Two situations look identical to a naive loader. A pipeline rerun after a
failure re-presents rows that are already in `raw` — harmless, and the loader
should skip them. An upstream interface retry posts the same goods receipt
twice *within one extract* — a real control finding, and the loader must
surface it. Both appear as "a row whose key already exists".

### Options considered

1. **Load into a per-batch staging table, then `INSERT ... SELECT` with a
   `LEFT JOIN` anti-join**: the anti-join's skipped count is a real number the
   loader can report and record. Con: an extra table write per file, and the
   staging tables need cleaning up.
2. **`INSERT ... ON CONFLICT DO NOTHING` against a unique index on the natural
   key**: one statement, fastest, least code. Con: Postgres silently discards
   the collision. `ON CONFLICT` reports only how many rows were inserted, not
   which were dropped or why — so a rerun and a source-side duplicate are
   indistinguishable, and the duplicate-receipt control has nothing to work
   with.
3. **A watermark on load timestamp or batch sequence**: no key comparisons at
   all, and it scales past both. Con: it assumes rows never arrive out of
   order, which Phase 2 deliberately violates with `late_arriving_mes_events`.
   Anything dated before the watermark would never load, silently.

### Decision

Each file lands in a per-batch staging table in `raw`. Rows are then inserted
into the target with a `LEFT JOIN` anti-join on the declared natural key, and
the loader records three counts separately: rows in the file, rows inserted,
and rows skipped as already present. A *within-file* duplicate on the natural
key is detected before the anti-join and rejected, not skipped.

### Reasons

The anti-join turns "how many rows did this load skip, and why" into a
reportable number instead of a side effect. That distinction is the whole
reason this option wins: `audit.pipeline_runs` needs honest counts per file,
and CTRL-001 needs to compare them against the control totals. `ON CONFLICT`
would make both approximate.

Separating "duplicated inside the source file" from "already present in raw"
is what makes the duplicate-receipt defect a finding rather than a quiet
no-op. An interface that posts a receipt twice is broken upstream and someone
should hear about it; a rerun of yesterday's file is routine. Collapsing them
would mean the control can never fire.

Rejecting the watermark now rather than later also avoids building something
Phase 2 is designed to break.

### Consequences

Easier: trustworthy per-file counts, a detectable difference between a retry
and an upstream fault, and reruns that are safe by construction.

Harder: one extra write per file, and the per-batch staging tables must be
dropped on both the success and the failure path or `raw` accumulates
litter — the loader drops them in a `finally`. Each extract must also declare
a natural key, so a source that reuses IDs across periods would break the
assumption; keys are documented and tested per extract.

### How a reviewer can verify it

The Phase 1 idempotency test runs the loader twice and asserts the second run
inserts zero rows while reporting a non-zero skip count — not merely that the
row total is unchanged. BTB-26 asserts a within-file duplicate is rejected and
reported, with the count matching `defects_manifest.json`.

---

## ADR-021: Publish by pointing views at versioned tables

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-23 | PR/commit: BTB-9-phase-1-decisions
- Area: Controls

### Context

[ADR-014](#adr-014-publish-by-swapping-a-validated-build-into-reporting)
decided that a validated build is swapped into `reporting` only when every
critical check passes, and described the swap as renaming schemas inside a
transaction. Implementing it exposed a problem with that mechanism serious
enough to change it.

`ALTER DEFAULT PRIVILEGES` is keyed to the schema **name**, not to the schema
object. Rename `publish_candidate` to `reporting` and the default privileges
attached to the name `reporting` no longer apply to it — so every table the
next build creates silently loses the `reporter` role's `SELECT`. The live
dashboard would break one run after the change, with nothing in the logs
pointing at the rename.

### Options considered

1. **Build versioned tables inside `reporting`, and publish by recreating a
   stable view over the newest version**: the schema is never renamed, so
   grants and default privileges never move. The swap is a `CREATE OR REPLACE
   VIEW` in one transaction — atomic and sub-second. The previous version
   stays queryable, so rollback is pointing the view back. Con: old versions
   accumulate and must be pruned, which matters against Neon's 1 GB cap.
2. **Rename schemas in a transaction** (the original mechanism): one statement
   moves the whole dataset, and it reads simply. Con: the default-privileges
   footgun above, plus `reporting_old` has to be dropped or rotated, and dbt's
   notion of where it built things no longer matches reality.
3. **Rename each mart table individually inside `reporting`**: no schema
   churn, grants stay put. Con: renaming N tables in one transaction holds
   `ACCESS EXCLUSIVE` on all of them simultaneously. A dashboard query holding
   a read lock blocks the publish, and once the publish is waiting it blocks
   every subsequent reader — the exact stall the gate's lock timeout exists to
   avoid, made more likely.

### Decision

Marts build as versioned tables in `reporting` (`inv_balance_v<batch_id>`).
Publishing recreates a stable view per mart over the new version, inside one
transaction. `reporting` keeps the previous version until it is pruned, and a
rollback repoints the view.

### Reasons

Keeping the schema name fixed is what makes the access-control model hold
across runs. The `reporter` role's grants are the live demo's only
authorisation boundary, and a mechanism that strips them on a future build is
not acceptable in the component whose entire job is to publish safely.

The swap being a view definition change also makes it genuinely atomic and
fast: readers see the old version or the new one, and the transaction touches
catalogue rows rather than taking exclusive locks on large tables. That keeps
blocking a failed publish cheap, which is what stops the critical checks from
being disabled under pressure (ADR-010).

Retaining the previous version is a bonus the other options do not offer: the
"last good dataset" is not merely conceptual, it is still a table that can be
queried and diffed against the rejected candidate.

### Consequences

Easier: grants that survive every publish, a sub-second atomic swap, rollback
by repointing a view, and a last-good dataset that is directly inspectable.

Harder: version pruning becomes a real responsibility — the gate keeps the
current and previous versions and drops older ones, because two copies of
every mart against a 1 GB free tier is already most of the budget
(ADR-015, ADR-016). Consumers must also query the view, never a versioned
table directly; the `reporter` role is granted on the views, which enforces it
rather than relying on discipline.

### How a reviewer can verify it

BTB-23's forced-failure test asserts `reporting`'s views still resolve to the
previous version, `audit.publish_log` records the reason, and the dashboard
shows its earlier refresh time. A second test runs two successful publishes
and asserts the `reporter` role can still read every view afterwards — the
regression this ADR exists to prevent.

---

## ADR-022: Control totals as a JSON sidecar per extract

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-13 | PR/commit: BTB-9-phase-1-decisions
- Area: Controls

### Context

Each extract needs a control total — row count and value total — produced by
the source so completeness can be checked at load (CTRL-001). Real ERP and
mainframe extracts usually carry this as a trailer record in the file itself.
The format decides how awkward validation is, and whether one file can be
checked on its own.

### Options considered

1. **A JSON sidecar per extract** (`goods_movements.control_totals.json`): the
   data file stays clean CSV that pandas and pandera read in one call, and the
   totals are typed and extensible. Con: two files per extract, and a missing
   sidecar must be treated as a hard failure rather than a skip, or the
   control can be bypassed by deleting a file.
2. **A trailer row inside the data file**: what real source systems do, and
   the totals cannot be separated from the data. Con: every reader must strip
   the last row before parsing. Schema validation sees a malformed row,
   numeric columns have to tolerate a text marker, and the row count is
   off by one everywhere until it is handled.
3. **One manifest covering the whole run**: a single file, easy to diff across
   runs. Con: it couples the extracts. `audit.pipeline_runs` records one row
   *per file per run*, so validating a single extract in isolation is the unit
   of work — a run-level manifest fights that grain.

### Decision

A JSON sidecar beside each extract, named by the
`output.control_totals_suffix` setting in `generator/config.yaml`. A missing
or unparseable sidecar fails the load for that file.

### Reasons

The per-file grain decides it. The audit trail, the completeness control, and
the rejected-row log all work one file at a time, and a sidecar matches that
exactly while a run-level manifest does not. Keeping the CSV free of a trailer
row also means pandera validates the real schema rather than a schema plus one
special case — and the UoM and type checks are load-bearing controls, so
weakening them to accommodate a marker row is a bad trade.

The realism argument for a trailer row is genuine and worth saying out loud in
an interview: this is the one place the project chooses convenience over
fidelity to how source systems actually behave. The mitigation is that the
sidecar carries the same information and is validated just as strictly, and a
missing sidecar is a hard failure — so the *control* is as strong, even though
the file layout is tidier than reality.

### Consequences

Easier: clean CSV parsing, typed and extensible totals, and per-file
validation that matches the audit grain.

Harder: two files to keep together, and the "missing sidecar is a failure"
rule must be tested, or the control becomes optional in practice. A future
source that genuinely ships trailer records would need a reader that strips
them into the same sidecar shape — noted as follow-up rather than built now.

### How a reviewer can verify it

BTB-13's test asserts the sidecar matches the extract it accompanies. A
further test deletes a sidecar and asserts the load fails rather than skipping
the completeness check.

---

## ADR-023: Post a revaluation when a standard cost changes

- Date: 2026-11-02
- Status: Accepted
- Jira: BTB-12 | PR/commit: BTB-10-inventory-generator
- Area: Metrics

### Context

Inventory is valued at standard cost, and each movement is valued at the
standard in force on its posting date
([ADR-007](#adr-007-scd-type-2-snapshots-on-material-master-and-standard-cost),
docs/metric_definitions.md). Building the generator exposed a consequence that
had not been thought through.

Stock received at an old standard cost and issued at a new one leaves a value
residue after the quantity has netted out. A worked example from the generated
data: material 1000056 was received at \$435.00, the standard cost rose to
\$452.53, and the stock was then issued at the new cost. Closing quantity was
zero; closing *value* was **minus \$87.65**.

On the demo profile (three months) the drift was small enough to miss. On the
full profile (twelve months) **20 of 300 materials closed at a negative
value** while every physical quantity was correct, and the inventory account in
total went negative. The inventory balance sheet figure had quietly stopped
meaning anything, and no existing control would have caught it: the subledger
still tied to the GL, because both sides carried the same wrong number.

### Options considered

1. **Post a revaluation when the standard cost changes**: revalue stock on
   hand by the cost delta and post the difference to a price-difference
   account, which is what standard costing actually does. The invariant
   becomes exact — inventory balance equals on-hand quantity times current
   standard cost. Con: an eighth extract, and the subledger for CTRL-003 is
   now movements *plus* revaluations rather than movements alone.
2. **Value issues at the cost in force when the stock was received**: the
   quantity and value net out together, so no residue and no extra extract.
   Con: that is moving-average or FIFO costing, not standard costing. It would
   contradict ADR-007 and the metric definitions, and it would remove the
   price variance that is the entire point of a standard cost system.
3. **Allow no standard cost changes inside the reporting window**: move every
   change before `period_start` and the drift cannot arise. Con: it makes the
   SCD Type 2 snapshot decorative — an as-of join bug would become invisible,
   and the unapproved-cost-change defect would have nothing to act on. It
   avoids the problem by removing the thing being demonstrated.

### Decision

A standard cost change posts an `inventory_revaluations` document: on-hand
quantity times the cost delta, debiting inventory and crediting price
difference (account 231000) when the cost rises, and the reverse when it
falls. The revaluation carries no quantity, because nothing physically moved.

The inventory subledger is therefore **goods movements plus revaluations**.
CTRL-003 compares that sum against GL account 130000.

### Reasons

Standard costing produces price variances by design; a model that values
movements at a changing standard but never revalues the stock on hand is not
standard costing, it is an error. Option 2 would have made the arithmetic tidy
by abandoning the costing method the project claims to use, and option 3 by
abandoning the SCD Type 2 story — both trade away something the project exists
to demonstrate.

What makes this worth an ADR rather than a quiet fix is how the fault behaved.
It did not break the reconciliation. Movements and GL postings derive from the
same valuation, so both sides moved together and CTRL-003 passed throughout. A
reconciliation that two sources satisfy by sharing a common error is weaker
evidence than it looks, and the only thing that caught this was asserting an
*independent* invariant: closing value must equal quantity times current
standard cost. That is a lesson worth keeping in the log.

### Consequences

Easier: the inventory balance is now exactly on-hand quantity times current
standard cost, per material, which is what an inventory valuation report
shows. Price variance becomes visible as its own account instead of
contaminating inventory. The approver on each cost change flows through to the
revaluation, so CTRL-007 can trace a revaluation to its authorisation.

Harder: an eighth extract to ingest, model and document. CTRL-003's definition
is now a sum of two sources, so the control must be written to include
revaluations — omitting them would reintroduce exactly the drift this fixes.
A reviewer reading the older ADR-014 and ADR-022 text will not see this
mentioned; it is recorded here and in docs/metric_definitions.md.

### How a reviewer can verify it

`test_closing_value_equals_quantity_times_current_standard_cost` asserts the
invariant per material against the generator's own output.
`test_no_material_closes_at_a_negative_value` covers the symptom that exposed
it. Both were confirmed to fail when the revaluation step is removed, and
`docs/metric_definitions.md` records the inventory value definition that
depends on it.
