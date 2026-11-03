"""The reconciliation checks, declared as data.

Each check is a SQL query plus the metadata an auditor needs: which control it
implements, how severe a failure is, and what tolerance applies. Declaring
them as data rather than as functions means the control matrix, the runner and
the publish gate all read one list, so a check cannot exist in code without a
control ID or run without its severity being decided.

Two shapes of check:

* **Comparison** -- two figures that must agree, per period. Returns expected,
  actual and the difference, because "the reconciliation failed" is not
  something anyone can act on; the difference is what gets investigated.
* **Exception** -- a count of rows that should not exist. Returns the count and
  a sample, because the rows themselves are the finding.

Severity decides whether a failure blocks the publish (ADR-010). Only
`critical` blocks. The distinction exists so that a minor oddity cannot stop
the pipeline and teach everyone to disable checks -- and so that a real
integrity failure cannot be waved through.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


class CheckShape(enum.StrEnum):
    """How a check's result is interpreted."""

    #: Two figures per period that must agree within tolerance.
    COMPARISON = "comparison"
    #: A count of rows that should not exist.
    EXCEPTION = "exception"


@dataclasses.dataclass(frozen=True)
class Check:
    """One reconciliation or integrity check.

    Attributes:
        check_id: Control ID from docs/control_matrix.csv. A check with no
            control is a claim with nothing behind it; a control with no check
            is coverage that does not exist. Tests assert both directions.
        name: Short human name, used in the publish log and the console.
        shape: How to read the result.
        severity: Only `critical` blocks a publish (ADR-010).
        sql: Query returning the shape's expected columns. Names the layer
            it reads as ``{marts}``, ``{intermediate}`` or ``{staging}``, so
            the same check runs against a sandbox build without reading
            production. Hardcoding a layer was a real hole: two checks read
            `intermediate.` and `staging.` directly, which would have made the
            Phase 6 failure demo assess live data while claiming not to.

            May also bind ``%(batch_id)s``, which every check reading the
            audit trail must use: a control assesses the run being published,
            not every run ever recorded.
        tolerance: Permitted absolute difference. Zero everywhere in Phase 1:
            both sides of every comparison are two-decimal-place decimals by
            construction, so any difference is real. A materiality-based
            tolerance is a separate decision (ADR-010).
        rationale: Why the check exists, for the evidence pack.
    """

    check_id: str
    name: str
    shape: CheckShape
    severity: str
    sql: str
    rationale: str
    tolerance: decimal.Decimal = decimal.Decimal("0")

    @property
    def blocks_publish(self) -> bool:
        return self.severity == SEVERITY_CRITICAL


# ---------------------------------------------------------------------------
# CTRL-003: inventory subledger to the GL inventory account.
#
# The Phase 1 reconciliation. Subledger is goods movements PLUS standard cost
# revaluations (ADR-023) -- omitting revaluations would let the inventory
# account drift away from quantity x standard cost while this still passed.
# ---------------------------------------------------------------------------
SUBLEDGER_TO_GL = Check(
    check_id="CTRL-003",
    name="Inventory subledger ties to GL account 130000",
    shape=CheckShape.COMPARISON,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "Inventory on the balance sheet must agree with the movements that produced it, "
        "or the reported inventory value is unsupported."
    ),
    sql="""
        with movements as (
            select posting_period, sum(amount_usd) as amount_usd
            from {marts}.fct_goods_movements
            group by posting_period
        ),

        revaluations as (
            select posting_period, sum(amount_usd) as amount_usd
            from {marts}.fct_inventory_revaluations
            group by posting_period
        ),

        subledger as (
            select posting_period, sum(amount_usd) as amount_usd
            from (
                select posting_period, amount_usd from movements
                union all
                select posting_period, amount_usd from revaluations
            ) as combined
            group by posting_period
        ),

        ledger as (
            select posting_period, sum(signed_amount_usd) as amount_usd
            from {marts}.fct_gl_postings
            where gl_account = '130000'
            group by posting_period
        )

        select
            coalesce(l.posting_period, s.posting_period) as period,
            coalesce(l.amount_usd, 0)                    as expected_value,
            coalesce(s.amount_usd, 0)                    as actual_value
        from ledger as l
        full outer join subledger as s on s.posting_period = l.posting_period
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-001: source-to-raw completeness, re-tested from the audit trail.
#
# The loader already checks this per file at load time. This re-derives it from
# audit.pipeline_runs, which is the point: an auditor can test completeness
# from the recorded evidence alone, without rerunning the pipeline or trusting
# that the loader's own check ran.
# ---------------------------------------------------------------------------
SOURCE_TO_RAW_COMPLETENESS = Check(
    check_id="CTRL-001",
    name="Every source row is accounted for in the audit trail",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "An extract that lands incomplete produces totals that are wrong while looking "
        "entirely plausible. Re-derived from the audit trail so completeness is testable "
        "from recorded evidence rather than from the loader's own report."
    ),
    sql="""
        select
            extract_name || ': '
                || rows_inserted || ' inserted + ' || rows_skipped || ' skipped + '
                || rows_rejected || ' rejected <> ' || control_total_rows || ' declared'
                as exception_detail
        from audit.pipeline_runs
        -- Scoped to the batch being published. Assessing the whole audit
        -- history would mean one historical failure -- including a
        -- deliberately broken test fixture -- blocks every future publish
        -- for ever, which makes the gate useless rather than strict.
        where batch_id = %(batch_id)s
          and status = 'success'
          and control_total_rows is not null
          and rows_inserted + rows_skipped + rows_rejected <> control_total_rows
    """,
)

# ---------------------------------------------------------------------------
# CTRL-002: raw-to-mart completeness.
#
# A join that changes grain shows up here as a count mismatch rather than as a
# quietly inflated metric several models downstream.
# ---------------------------------------------------------------------------
RAW_TO_MART_COMPLETENESS = Check(
    check_id="CTRL-002",
    name="No rows lost or duplicated between raw and the marts",
    shape=CheckShape.COMPARISON,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "Rows silently lost or multiplied between layers -- usually by a join that changes "
        "grain -- make a metric wrong in a way that is invisible at the point of use."
    ),
    sql="""
        select
            'goods_movements'                                       as period,
            (select count(*) from raw.goods_movements)::numeric      as expected_value,
            (select count(*) from {marts}.fct_goods_movements)::numeric as actual_value
        union all
        select
            'gl_postings',
            (select count(*) from raw.gl_postings)::numeric,
            (select count(*) from {marts}.fct_gl_postings)::numeric
        union all
        select
            'inventory_revaluations',
            (select count(*) from raw.inventory_revaluations)::numeric,
            (select count(*) from {marts}.fct_inventory_revaluations)::numeric
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-010: no negative on-hand inventory.
# ---------------------------------------------------------------------------
NO_NEGATIVE_ON_HAND = Check(
    check_id="CTRL-010",
    name="No material shows a negative on-hand balance",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "Negative on-hand is physically impossible, so it always indicates a sequencing "
        "or duplication error rather than a real position."
    ),
    sql="""
        select
            material_number || ' in ' || posting_period || ': closing quantity '
                || closing_quantity as exception_detail
        from {marts}.mart_inventory_balance
        where closing_quantity < 0
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# Inventory value must equal quantity x the cost in force.
#
# Deliberately independent of CTRL-003. That reconciliation compares two
# sources which derive from the same valuation, so a valuation error appears
# identically on both sides and passes -- which is exactly how a negative
# inventory value survived it before ADR-023. This check cannot be satisfied
# by a shared error.
# ---------------------------------------------------------------------------
INVENTORY_VALUE_AGREES_WITH_COST = Check(
    check_id="CTRL-003",
    name="Inventory value equals quantity times the standard cost in force",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "The subledger-to-GL reconciliation cannot detect a valuation error shared by both "
        "of its sources. This asserts the inventory balance independently, against the "
        "standard cost in force at each period end."
    ),
    sql="""
        with period_ends as (
            select
                material_number,
                posting_period,
                closing_quantity,
                closing_value_usd,
                (
                    date_trunc('month', to_date(posting_period, 'YYYY-MM'))
                    + interval '1 month - 1 day'
                )::date as period_end
            from {marts}.mart_inventory_balance
        )

        select
            p.material_number || ' in ' || p.posting_period || ': value '
                || p.closing_value_usd || ' but ' || p.closing_quantity || ' x '
                || m.standard_cost_usd || ' = '
                || round(p.closing_quantity * m.standard_cost_usd, 2) as exception_detail
        from period_ends as p
        inner join {marts}.dim_material as m
            on m.material_number = p.material_number
            and p.period_end between m.valid_from and m.valid_to
        where p.closing_value_usd <> round(p.closing_quantity * m.standard_cost_usd, 2)
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# Every accounting document balances.
# ---------------------------------------------------------------------------
GL_DOCUMENTS_BALANCE = Check(
    check_id="CTRL-002",
    name="Every accounting document balances to zero",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "A document whose debits and credits do not agree breaks double-entry and makes "
        "the trial balance meaningless."
    ),
    sql="""
        select
            'document ' || accounting_document || '/' || fiscal_year
                || ' nets to ' || sum(signed_amount_usd) as exception_detail
        from {marts}.fct_gl_postings
        group by accounting_document, fiscal_year
        having sum(signed_amount_usd) <> 0
        order by 1
    """,
)


# ---------------------------------------------------------------------------
# CTRL-001, second half: no row was rejected.
#
# The accounting identity above proves every source row is *accounted for*. It
# passes when a row was rejected, because a reject is accounted for -- and that
# is not good enough. A rejected row means the certified dataset was computed
# from an extract the source got wrong, which is exactly what the gate exists
# to stop.
#
# This is what makes the duplicate-receipt defect preventive: the loader
# refuses the duplicate at the door (ADR-020), and this check stops the run
# rather than absorbing the loss quietly.
#
# Zero tolerance in Phase 1. A reject threshold is defensible for a noisy feed
# and is recorded as follow-up (BTB-17), but a threshold chosen now would be a
# number with no evidence behind it.
# ---------------------------------------------------------------------------
NO_REJECTED_ROWS = Check(
    check_id="CTRL-001",
    name="No source row was rejected during loading",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "A rejected row means the source sent something invalid, so the certified dataset "
        "is computed from an incomplete extract. The row counts still reconcile, which is "
        "why this has to be a separate check."
    ),
    sql="""
        select
            r.extract_name || ' row ' || r.source_row || ': '
                || r.rejection_rule || ' -- ' || r.rejection_detail as exception_detail
        from audit.rejected_rows as r
        where r.batch_id = %(batch_id)s
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-006: closed-period integrity.
#
# A posting whose entry date falls after its period's close date restates
# figures that were already reported. Preventive by necessity: detecting it
# after publishing means the restatement has already reached a leader's screen.
#
# The close lag is passed in from the generator config rather than hardcoded
# here, so the control and the data cannot disagree about when a period locks
# -- a disagreement would make this check either blind or permanently failing.
# Calendar days approximate business days; a real close calendar arrives with
# the finance extracts in Phase 2.
# ---------------------------------------------------------------------------
CLOSED_PERIOD_INTEGRITY = Check(
    check_id="CTRL-006",
    name="No posting was entered into a closed period",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "A posting entered after its period closed restates figures that have already been "
        "reported. Nothing about the amount or the accounts looks unusual -- only the pair "
        "of dates reveals it."
    ),
    sql="""
        with closes as (
            select
                accounting_document,
                fiscal_year,
                line_item,
                posting_period,
                entered_on,
                (
                    date_trunc('month', posting_date)
                    + interval '1 month - 1 day'
                    + (%(close_lag_days)s || ' days')::interval
                )::date as period_closed_on
            from {marts}.fct_gl_postings
        )

        select
            'document ' || accounting_document || '/' || fiscal_year
                || ' line ' || line_item || ' posts to ' || posting_period
                || ' but was entered ' || entered_on
                || ', after that period closed on ' || period_closed_on
                as exception_detail
        from closes
        where entered_on > period_closed_on
        order by 1
    """,
)


# ---------------------------------------------------------------------------
# CTRL-004: three-way match.
#
# A supplier paid for goods never ordered or never received, or at a price
# nobody agreed. One of the oldest controls in accounts payable, and the first
# one here that needed a whole source system (BTB-31) before it could exist.
#
# Only the genuine exceptions are findings. `ORDERED_NOT_RECEIVED` is an open
# commitment and `RECEIVED_NOT_INVOICED` is a real goods-received-not-invoiced
# balance at period end -- both are normal, and flagging them would bury the
# three statuses that matter in a hundred that do not.
# ---------------------------------------------------------------------------
THREE_WAY_MATCH = Check(
    check_id="CTRL-004",
    name="PO line, goods receipt and invoice agree on quantity and price",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "A supplier paid for goods that were never ordered or never received, or at a price "
        "nobody agreed. Open commitments and goods-received-not-invoiced balances are normal "
        "and deliberately not treated as exceptions."
    ),
    sql="""
        select
            'PO ' || po_number || '/' || po_item || ' (' || material_number || '): '
                || match_status
                || ' -- ordered ' || quantity_ordered
                || ', received ' || quantity_received
                || ', invoiced ' || quantity_invoiced
                || ', PO price ' || po_unit_price_usd
                || ', invoiced price ' || coalesce(invoiced_unit_price_usd::text, 'n/a')
                as exception_detail
        -- The match lives in `intermediate`, not in the mart schema: it is
        -- business logic over three sources rather than a published fact.
        from {intermediate}.int_three_way_match
        where match_status in (
            'INVOICED_NOT_RECEIVED', 'QUANTITY_MISMATCH', 'PRICE_MISMATCH'
        )
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-009: every invoice line references a real purchase order line.
#
# Found by testing CTRL-004 rather than by reading it. The three-way match is
# driven FROM purchase order lines, so an invoice line referencing a PO line
# that does not exist appears nowhere in the match -- it is excluded by the
# join rather than flagged by it. An orphan invoice line sat in raw and nothing
# reported it.
#
# The match answers "did this PO get received and invoiced correctly?", which
# is a different question from "is this invoice for anything we ordered?". The
# second needs its own check; widening the match to a full outer join would
# have conflated the two and made its status column mean two things.
# ---------------------------------------------------------------------------
INVOICE_REFERENCES_A_PURCHASE_ORDER = Check(
    check_id="CTRL-009",
    name="Every invoice line references a real purchase order line",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "An invoice line pointing at a purchase order line that does not exist is a payment "
        "for something nobody ordered. It is invisible to the three-way match, which is "
        "driven from purchase order lines and therefore excludes it rather than flagging it."
    ),
    sql="""
        select
            'invoice ' || l.invoice_number || '/' || l.fiscal_year
                || ' item ' || l.invoice_item
                || ' references PO ' || l.po_number || '/' || l.po_item
                || ', which does not exist' as exception_detail
        from {staging}.stg_supplier_invoice_lines as l
        where not exists (
            select 1
            from {staging}.stg_purchase_order_lines as p
            where p.po_number = l.po_number
              and p.po_item = l.po_item
        )
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-009, second check: the supply chain feed's foreign keys.
#
# A separate check under the same control rather than a widening of the one
# above, matching how CTRL-001, CTRL-002 and CTRL-003 are each several named
# checks. Each one then says what it actually looked at, and a report naming
# "every invoice line references a real purchase order line" cannot quietly
# mean "and also some shipment things".
#
# Both faults are reported by one check because they are one risk: a fact
# pointing at a dimension member that does not exist. A null supplier and an
# unknown supplier differ only in how the reference broke.
# ---------------------------------------------------------------------------
SHIPMENT_REFERENCES_RESOLVE = Check(
    check_id="CTRL-009",
    name="Every shipment names a real supplier and a real purchase order line",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "A shipment with no supplier cannot be attributed to anyone, so it silently "
        "drops out of supplier on-time delivery and spend by vendor -- the figures look "
        "complete and are not. A shipment line pointing at a purchase order line that "
        "does not exist is goods arriving against nothing anybody ordered."
    ),
    sql="""
        select
            'delivery ' || h.inbound_delivery
                || ' has no supplier'                            as exception_detail
        from {staging}.stg_shipment_headers as h
        where h.supplier_number is null
        union all
        select
            'delivery ' || h.inbound_delivery
                || ' names supplier ' || h.supplier_number
                || ', which does not exist'
        from {staging}.stg_shipment_headers as h
        where
            h.supplier_number is not null
            and not exists (
                select 1
                from {staging}.stg_suppliers as s
                where s.supplier_number = h.supplier_number
            )
        union all
        select
            'delivery ' || l.inbound_delivery
                || ' item ' || l.shipment_item
                || ' references PO ' || l.po_number || '/' || l.po_item
                || ', which does not exist'
        from {staging}.stg_shipment_lines as l
        where not exists (
            select 1
            from {staging}.stg_purchase_order_lines as p
            where
                p.po_number = l.po_number
                and p.po_item = l.po_item
        )
        order by 1
    """,
)

# ---------------------------------------------------------------------------
# CTRL-005: MES versus ERP completion.
#
# Production output reported to finance must match what the factory actually
# built and tested. The two systems count a finished unit at different moments
# -- MES at final test, ERP at confirmation -- so the gap is the finding.
#
# This only works because confirmations are generated FROM the MES outcome
# (BTB-33). Generating the two independently would leave a clean-run gap made
# of noise, and a control whose baseline is noise cannot detect a real finding.
# ---------------------------------------------------------------------------
MES_TO_ERP_COMPLETION = Check(
    check_id="CTRL-005",
    name="Units passing final test match the quantity confirmed in ERP",
    shape=CheckShape.EXCEPTION,
    severity=SEVERITY_CRITICAL,
    rationale=(
        "Production output reported to finance must match what the factory built and "
        "tested. A positive gap means units were built and never confirmed -- output "
        "missing from the figures finance reports."
    ),
    sql="""
        select
            'order ' || order_number || ' (' || material_number || '): '
                || completion_status
                || ' -- MES completed ' || mes_units_completed
                || ', ERP confirmed ' || erp_units_confirmed
                || ', gap ' || completion_gap
                as exception_detail
        from {intermediate}.int_mes_to_erp_completion
        where completion_status in ('MES_WITHOUT_CONFIRMATION', 'COMPLETION_GAP')
        order by abs(completion_gap) desc, order_number
    """,
)

#: Checks that run in Phase 1, in the order they are reported.
#:
#: CTRL-006 arrived with BTB-27, once the generator emitted `entered_on`. It
#: was deliberately absent before that: a check that cannot distinguish a
#: backdated entry from a normal one is worse than no check, because it looks
#: like coverage.
PHASE_1_CHECKS: tuple[Check, ...] = (
    SOURCE_TO_RAW_COMPLETENESS,
    NO_REJECTED_ROWS,
    RAW_TO_MART_COMPLETENESS,
    GL_DOCUMENTS_BALANCE,
    THREE_WAY_MATCH,
    INVOICE_REFERENCES_A_PURCHASE_ORDER,
    SHIPMENT_REFERENCES_RESOLVE,
    MES_TO_ERP_COMPLETION,
    SUBLEDGER_TO_GL,
    INVENTORY_VALUE_AGREES_WITH_COST,
    NO_NEGATIVE_ON_HAND,
    CLOSED_PERIOD_INTEGRITY,
)


def critical_checks(checks: tuple[Check, ...] = PHASE_1_CHECKS) -> tuple[Check, ...]:
    return tuple(check for check in checks if check.blocks_publish)
