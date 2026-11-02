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
        sql: Query returning the shape's expected columns. Receives
            ``{schema}`` for the schema being checked, so the same check runs
            against `publish_candidate`, `sandbox` or `reporting` unchanged --
            which is what lets the failure demo run safely in Phase 6. May
            also bind ``%(batch_id)s``, which every check that reads the audit
            trail must use: a control assesses the run being published, not
            every run ever recorded.
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
            from {schema}.fct_goods_movements
            group by posting_period
        ),

        revaluations as (
            select posting_period, sum(amount_usd) as amount_usd
            from {schema}.fct_inventory_revaluations
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
            from {schema}.fct_gl_postings
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
            (select count(*) from {schema}.fct_goods_movements)::numeric as actual_value
        union all
        select
            'gl_postings',
            (select count(*) from raw.gl_postings)::numeric,
            (select count(*) from {schema}.fct_gl_postings)::numeric
        union all
        select
            'inventory_revaluations',
            (select count(*) from raw.inventory_revaluations)::numeric,
            (select count(*) from {schema}.fct_inventory_revaluations)::numeric
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
        from {schema}.mart_inventory_balance
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
            from {schema}.mart_inventory_balance
        )

        select
            p.material_number || ' in ' || p.posting_period || ': value '
                || p.closing_value_usd || ' but ' || p.closing_quantity || ' x '
                || m.standard_cost_usd || ' = '
                || round(p.closing_quantity * m.standard_cost_usd, 2) as exception_detail
        from period_ends as p
        inner join {schema}.dim_material as m
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
        from {schema}.fct_gl_postings
        group by accounting_document, fiscal_year
        having sum(signed_amount_usd) <> 0
        order by 1
    """,
)


#: Checks that run in Phase 1, in the order they are reported.
#:
#: CTRL-006 (closed-period integrity) is absent on purpose. Detecting a
#: posting made *into* a closed period needs an entry timestamp distinct from
#: the posting date, which the generator does not yet emit. Adding a check that
#: cannot distinguish a backdated entry from a normal one would be worse than
#: having none -- it lands with the defect that exercises it (BTB-27).
PHASE_1_CHECKS: tuple[Check, ...] = (
    SOURCE_TO_RAW_COMPLETENESS,
    RAW_TO_MART_COMPLETENESS,
    GL_DOCUMENTS_BALANCE,
    SUBLEDGER_TO_GL,
    INVENTORY_VALUE_AGREES_WITH_COST,
    NO_NEGATIVE_ON_HAND,
)


def critical_checks(checks: tuple[Check, ...] = PHASE_1_CHECKS) -> tuple[Check, ...]:
    return tuple(check for check in checks if check.blocks_publish)
