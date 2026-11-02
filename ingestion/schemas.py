"""Declared schemas for every extract, validated with pandera (ADR-006).

Validation must do two things that a plain type cast does not. It has to report
**which rows** failed and **why**, because a rejected row is logged with its
reason rather than dropped (BTB-17). And it has to be declarative enough that a
reviewer can read the contract without reading the loader.

Each extract declares:

* column names, types and nullability
* value constraints that encode a business rule, not just a type
* its natural key, which the anti-join loader uses (ADR-020)

The constraints are deliberately the kind that catch real faults. `quantity`
cannot be zero, because a movement of nothing is meaningless and usually means
a parsing error. `fiscal_year` must be plausible, so a mis-parsed date surfaces
here instead of as a missing period in a report. `debit_credit_indicator` is
restricted to S and H, because a third value would silently unbalance the
trial balance.

Validation does **not** enforce cross-row or cross-extract rules -- that a
receipt references a real PO line, or that the subledger ties to the GL. Those
are reconciliations, and they belong in `controls/` where a failure is a
recorded control result rather than a rejected row.
"""

from __future__ import annotations

import datetime as dt

import pandera.pandas as pa
from pandera.pandas import Column, DataFrameSchema

# Plausible fiscal years. Narrow on purpose: a mis-parsed date usually lands
# far outside this range, and catching it at load is much cheaper than
# explaining a missing period later.
_MIN_YEAR, _MAX_YEAR = 2020, 2035

_SHA256 = r"^[0-9a-f]{64}$"

# Movement types this slice knows about (ADR-002). An unknown type would get a
# sign of zero in the mart and silently drop out of the balance.
_MOVEMENT_TYPES = ["101", "261"]

_DEBIT_CREDIT = ["S", "H"]


def _text(nullable: bool = False, **kwargs: object) -> Column:
    return Column(str, nullable=nullable, coerce=True, **kwargs)


def _code(allowed: list[str] | None = None, nullable: bool = False) -> Column:
    checks = [pa.Check.isin(allowed)] if allowed else [pa.Check.str_length(min_value=1)]
    return Column(str, checks=checks, nullable=nullable, coerce=True)


def _is_iso_date(value: object) -> bool:
    """Whether a value parses as an ISO calendar date."""
    try:
        dt.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return False
    return True


def _date(nullable: bool = False) -> Column:
    """An ISO date, validated as text rather than coerced to datetime64[ns].

    pandas' nanosecond datetime tops out at **2262-04-11**, and SCD Type 2
    windows use 9999-12-31 as the open-ended sentinel -- which is SAP's own
    convention (ADR-002) and overflows the coercion. The first load attempt
    failed on exactly that.

    Three ways out, and why this one:

    * Coerce to datetime64[ns] and change the sentinel. Rejected: the sentinel
      would have to be 2262-04-11, a magic number with no meaning to a
      reviewer.
    * Represent an open window as NULL. This is a common convention, but then
      every as-of join must handle NULL explicitly, and a forgotten
      ``OR valid_to IS NULL`` silently drops the current version of every
      row -- the exact class of bug ADR-007 warns about.
    * Coerce to ``datetime64[us]``, which pandas 3 infers for out-of-range
      dates and which holds year 9999 comfortably. Tested and rejected:
      pandera normalises the declared dtype back to ``datetime64[ns]`` before
      coercing, so it fails identically.
    * Validate the text and let Postgres parse it. Chosen. Postgres' ``date``
      reaches year 5874897, so the sentinel stores natively, and
      ``posting_date BETWEEN valid_from AND valid_to`` needs no NULL handling.

    The check is real validation, not a bypass: a malformed date is rejected
    here, and Postgres rejects anything that slips past.
    """
    return Column(
        str,
        checks=[pa.Check(_is_iso_date, element_wise=True, name="iso_date")],
        nullable=nullable,
        coerce=True,
    )


def _money(nullable: bool = False, allow_negative: bool = True) -> Column:
    checks = [] if allow_negative else [pa.Check.ge(0)]
    return Column(float, checks=checks, nullable=nullable, coerce=True)


def _positive_int(nullable: bool = False) -> Column:
    return Column(int, checks=[pa.Check.gt(0)], nullable=nullable, coerce=True)


def _nullable_positive_int() -> Column:
    """A nullable integer, as pandas' Int64 rather than float64.

    A nullable float64 column renders 10 as ``10.0``, which Postgres rejects
    for an integer column -- the first full load failed on exactly that, at
    ``po_item``. Int64 keeps missing values as pd.NA and whole numbers whole.
    """
    return Column(
        "Int64",
        checks=[pa.Check.gt(0)],
        nullable=True,
        coerce=True,
    )


def _year() -> Column:
    return Column(
        int,
        checks=[pa.Check.in_range(_MIN_YEAR, _MAX_YEAR)],
        coerce=True,
    )


SCHEMAS: dict[str, DataFrameSchema] = {
    "materials": DataFrameSchema(
        {
            "material_number": _code(),
            "material_description": _text(),
            "material_group": _code(),
            "base_uom": _code(),
            "plant": _code(),
            "created_on": _date(),
        },
        strict=True,
        coerce=True,
        name="materials",
    ),
    "suppliers": DataFrameSchema(
        {
            "supplier_number": _code(),
            "supplier_name": _text(),
            "country": _code(),
            "payment_terms": _code(),
            "created_on": _date(),
        },
        strict=True,
        coerce=True,
        name="suppliers",
    ),
    "standard_costs": DataFrameSchema(
        {
            "material_number": _code(),
            "valid_from": _date(),
            "valid_to": _date(),
            # A standard cost of zero or less would value inventory at nothing.
            "standard_cost_usd": _money(allow_negative=False),
            "currency": _code(["USD"]),
            # CTRL-007 joins cost changes to approvals, so a blank approver is
            # a rejected row rather than a control finding to chase later.
            "approved_by": _code(),
            "approved_on": _date(),
        },
        strict=True,
        coerce=True,
        name="standard_costs",
    ),
    "purchase_order_headers": DataFrameSchema(
        {
            "po_number": _code(),
            "supplier_number": _code(),
            "po_date": _date(),
            "currency": _code(["USD"]),
            "created_by": _code(),
            "company_code": _code(),
        },
        strict=True,
        coerce=True,
        name="purchase_order_headers",
    ),
    "purchase_order_lines": DataFrameSchema(
        {
            "po_number": _code(),
            "po_item": _positive_int(),
            "material_number": _code(),
            "quantity_ordered": _positive_int(),
            "unit_price_usd": _money(allow_negative=False),
            "net_value_usd": _money(allow_negative=False),
            "uom": _code(),
            "delivery_date": _date(),
            "plant": _code(),
        },
        strict=True,
        coerce=True,
        name="purchase_order_lines",
    ),
    "goods_movements": DataFrameSchema(
        {
            "material_document": _code(),
            "material_document_item": _positive_int(),
            "fiscal_year": _year(),
            "movement_type": _code(_MOVEMENT_TYPES),
            "material_number": _code(),
            "plant": _code(),
            # Signed by movement type, but never zero: a movement of nothing
            # is meaningless and usually means a parsing fault.
            "quantity": Column(int, checks=[pa.Check.ne(0)], coerce=True),
            "uom": _code(),
            "amount_usd": _money(),
            "posting_date": _date(),
            "document_date": _date(),
            # Null on goods issues.
            "po_number": _text(nullable=True),
            "po_item": _nullable_positive_int(),
            "entered_by": _code(),
        },
        strict=True,
        coerce=True,
        name="goods_movements",
    ),
    "inventory_revaluations": DataFrameSchema(
        {
            "revaluation_document": _code(),
            "fiscal_year": _year(),
            "material_number": _code(),
            "posting_date": _date(),
            "old_standard_cost_usd": _money(allow_negative=False),
            "new_standard_cost_usd": _money(allow_negative=False),
            # Revaluing zero stock would post a zero entry; the generator skips
            # those, so seeing one means a logic fault.
            "quantity_on_hand": _positive_int(),
            "amount_usd": Column(float, checks=[pa.Check.ne(0)], coerce=True),
            "currency": _code(["USD"]),
            "approved_by": _code(),
        },
        strict=True,
        coerce=True,
        name="inventory_revaluations",
    ),
    "gl_postings": DataFrameSchema(
        {
            "accounting_document": _code(),
            "line_item": _positive_int(),
            "fiscal_year": _year(),
            "company_code": _code(),
            "gl_account": _code(),
            # A third value here would silently unbalance the trial balance.
            "debit_credit_indicator": _code(_DEBIT_CREDIT),
            "amount_usd": _money(allow_negative=False),
            "signed_amount_usd": _money(),
            "currency": _code(["USD"]),
            "posting_date": _date(),
            "document_date": _date(),
            # When the document was entered, as distinct from the period it
            # posts to. CTRL-006 compares this against the period close date;
            # without it a backdated posting is indistinguishable from a
            # normal one.
            "entered_on": _date(),
            "reference_document": _code(),
            # Null on the offset line.
            "material_number": _text(nullable=True),
            "entered_by": _code(),
        },
        strict=True,
        coerce=True,
        name="gl_postings",
    ),
}


# ---------------------------------------------------------------------------
# Natural keys, declared here as well as in each extract's control-totals
# sidecar. The loader asserts the two agree before loading anything.
#
# Declaring it twice is deliberate. The sidecar is the source's claim about its
# own key; this is the loader's contract. If they disagree, one of them is
# wrong and loading on either would be a guess -- the anti-join's correctness
# depends entirely on the key being right (ADR-020).
# ---------------------------------------------------------------------------
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "materials": ("material_number",),
    "suppliers": ("supplier_number",),
    "standard_costs": ("material_number", "valid_from"),
    "purchase_order_headers": ("po_number",),
    "purchase_order_lines": ("po_number", "po_item"),
    # Material document numbers repeat across fiscal years (ADR-020).
    "goods_movements": ("material_document", "fiscal_year", "material_document_item"),
    "inventory_revaluations": ("revaluation_document", "fiscal_year"),
    "gl_postings": ("accounting_document", "fiscal_year", "line_item"),
}


def natural_key_for(extract_name: str) -> tuple[str, ...]:
    if extract_name not in NATURAL_KEYS:
        raise KeyError(f"no declared natural key for {extract_name!r}")
    return NATURAL_KEYS[extract_name]


def schema_for(extract_name: str) -> DataFrameSchema:
    if extract_name not in SCHEMAS:
        raise KeyError(
            f"no declared schema for {extract_name!r}; known extracts: {sorted(SCHEMAS)}"
        )
    return SCHEMAS[extract_name]
