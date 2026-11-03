# Metric definitions

Business definitions for every certified KPI, in finance and operations
language. A metric change requires **both** an entry here and an ADR
([DECISIONS.md](DECISIONS.md)), so a definition cannot drift without a recorded
reason.

> All data is synthetic and all controls are simulated, SOX-style. Nothing here
> describes a real company's results ([ADR-019](DECISIONS.md#adr-019-label-all-data-synthetic-and-all-controls-simulated)).

## How to read an entry

Each certified KPI gets: a plain-language definition, the grain it is reported
at, its assumptions, the model and column that implement it, and the
reconciliation that proves it.

---

## Global assumptions

These apply to every metric unless an entry overrides them. They are settled
now because they change every number downstream.

| Assumption | Choice | Why it matters |
| --- | --- | --- |
| **Date basis** | **Posting date**, not document date | A document created on the 31st and posted on the 1st belongs to the new period. Finance reports on when a transaction hit the books, so posting date is what reconciles to the GL. |
| **Currency** | **USD only** | No FX translation is modelled, so no rate-date policy is needed. A multi-currency version would need one and would be a separate decision. |
| **Inventory valuation** | **Standard cost**, from the SCD Type 2 snapshot in force on the posting date | Standard cost makes period-over-period movement explainable; an as-of join to the snapshot means a past period revalues at the cost that was actually in force, not today's. |
| **Period** | Calendar month | The close calendar locks a period `close_lag_days` business days after month end (`generator/config.yaml`). |
| **Closed periods** | No postings may be dated into a closed period | Enforced by CTRL-006. A posting that violates this blocks the publish. |

---

## Inventory value

**Definition.** The value of material on hand at the end of a period, valued at
the standard cost in force on each movement's posting date.

- **Grain:** material × period. Also reported by material group.
- **Basis:** posting date; USD; standard cost (see global assumptions).
- **Movements included:** goods receipts increase it, goods issues decrease it.
  Movement type determines the sign.
- **Excluded:** material in transit (not yet received) and scrapped units
  written off in the period.
- **Reconciliation:** CTRL-003 compares this against the GL inventory account
  balance per period. With defects off the difference is zero.
- **Implemented by:** *Phase 1 (BTB-21)* — inventory balance mart.
- **Certified:** `meta: {certified: true, owner: Finance}`

**The subledger is movements *plus* revaluations.** Standard costing values
each movement at the standard in force on its posting date, so stock received
at an old cost and issued at a new one leaves a value residue after the
quantity has netted out. A standard cost change therefore posts an
`inventory_revaluations` document — on-hand quantity times the cost delta,
offset to price difference (account 231000) — and CTRL-003 compares
**movements + revaluations** against GL account 130000.

Omitting revaluations does not break the reconciliation, which is what makes it
dangerous: movements and GL postings share the same valuation, so both sides
carry the same error and the check still passes. It breaks the *meaning* of the
number instead. Before revaluation was modelled, 20 of 300 materials on the
full profile closed at a negative value with every physical quantity correct
([ADR-023](DECISIONS.md#adr-023-post-a-revaluation-when-a-standard-cost-changes)).

The invariant that holds it honest, and the one to test rather than the
reconciliation alone:

> closing inventory value = on-hand quantity × current standard cost

**Why it can disagree with the GL.** Three causes, all of them planted as
defects: a duplicate receipt inflates the subledger; a unit-of-measure error
mis-values one component; a backdated posting moves value into a closed period.
Each has a control that catches it.

---

## Metrics to be defined

These land with the models that implement them. Each needs the full treatment
above before it can be called certified.

| KPI | Phase | Story | The question it has to answer |
| --- | --- | --- | --- |
| First-pass yield | 3 | BTB-44 | Of units entering the route, what share passed every step on the first attempt? Does a reworked unit that later passes count as a first-pass success? (No.) |
| WIP units and cycle time | 3 | BTB-44 | At a point in time, how many units are between first and last step? Is cycle time measured per unit or per order, and does burn-in dwell count? |
| Inventory aging | 3 | BTB-44 | How long has on-hand material been held? Measured from receipt date, FIFO-assumed even though valuation is standard cost — an assumption to state explicitly. |
| Purchase price variance | 3 | BTB-44 | Invoice price versus standard or PO price? Recognised at receipt or at invoice? |
| Supplier on-time delivery | 3 | BTB-44 | Promised versus actual date — and is "on time" exact, or within a tolerance window? Measured by line or by shipment? Against the ASN's promise or the purchase order's agreed date? **The data to answer this now exists** (BTB-34): `int_inbound_delivery_performance` carries `days_late_versus_promise` and `days_late_versus_order` at delivery-line grain, so all four groupings are computable. It deliberately publishes no on-time flag. On the demo profile the choice is not cosmetic: exact gives 87.6% and a two-day window gives 90.2%. |
| Standard versus actual cost variance | 3 | BTB-44 | Split into price, quantity, and mix variance, or reported as a single figure? |
| COGS | 3 | BTB-44 | Recognised at goods issue or at shipment? This interacts with the conflict below. |

---

## The deliberate definition conflict: shipped versus complete

*To be resolved in Phase 7 (BTB-70), with the outcome recorded as an ADR.*

Two source systems disagree about when a unit is finished, and both are right
within their own frame:

- **ERP** counts a unit as **shipped at goods issue** — when it leaves the
  warehouse and the inventory and COGS postings are made.
- **MES** counts a unit as **complete at final test** — when it passes the last
  station and is physically a finished product.

A unit that passes final test on 31 March and ships on 2 April is complete in
March and shipped in April. The gap is normally a few days, which is invisible
mid-month and material at month end.

**Month-end impact.** Operations reports March output including that unit, so
its yield and completion figures include it. Finance reports March revenue and
COGS excluding it. Both are correct, and a single "units completed" tile on a
dashboard sourced from the wrong one will not tie to the other. The resolution
must therefore do three things: pick a definition for the certified KPI, name
the other view and where it is still used, and make the dashboard label which
one it shows.

This is also what makes CTRL-005 (MES to ERP) meaningful: the control measures
the gap rather than assuming it away, so an unexplained *growth* in the gap is a
finding — units that passed test but were never confirmed in ERP.

---

## Change log

| Date | Metric | Change | ADR |
| --- | --- | --- | --- |
| 2026-11-02 | — | Global assumptions settled: posting date, USD, standard cost | ADR-007 |
| 2026-11-02 | Inventory value | Subledger redefined as goods movements **plus** standard cost revaluations | ADR-023 |
