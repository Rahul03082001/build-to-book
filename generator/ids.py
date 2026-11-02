"""Document and master-data number ranges.

Loosely modelled on SAP number ranges (ADR-002), because the shape of these
keys is what makes the join paths and the uniqueness rules transfer to real
ERP work:

* Purchase orders start `45`, accounting documents `01`, material documents
  `49` -- the leading digits are how an ERP analyst recognises a document type
  at a glance.
* A material document number is unique **within a fiscal year**, not globally.
  That is a genuine ERP gotcha: the natural key is (document, year, item), and
  a loader keyed on the document alone silently collides across years.
* Line items increment in tens (10, 20, 30), as SAP purchase orders do, so
  that a line can be inserted later without renumbering.

Counters are explicit objects rather than globals so the generator stays
deterministic: the same seed produces the same numbers in the same order.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterator


@dataclasses.dataclass
class NumberRange:
    """A monotonic counter formatted to a fixed width.

    Args:
        prefix: Leading digits identifying the document type.
        width: Total digits including the prefix.
        start: First sequence value.
    """

    prefix: str
    width: int
    start: int = 1
    _counter: Iterator[int] = dataclasses.field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._counter = itertools.count(self.start)
        body = self.width - len(self.prefix)
        if body <= 0:
            raise ValueError(f"width {self.width} leaves no room after prefix {self.prefix!r}")
        self._body_width = body

    def next(self) -> str:
        return f"{self.prefix}{next(self._counter):0{self._body_width}d}"


def purchase_order_numbers() -> NumberRange:
    """SAP purchase orders occupy the 45xxxxxxxx range."""
    return NumberRange(prefix="45", width=10)


def material_document_numbers() -> NumberRange:
    """Material documents occupy 49xxxxxxxx, unique within a fiscal year.

    Callers create **one range per fiscal year**. Reusing a number in a later
    year is correct ERP behaviour, not a bug, so the natural key is
    (document, fiscal_year, item) -- a loader keyed on the document alone
    collides silently across years.
    """
    return NumberRange(prefix="49", width=10)


def accounting_document_numbers() -> NumberRange:
    """Accounting documents occupy 01xxxxxxxx, unique within a fiscal year."""
    return NumberRange(prefix="01", width=10)


def revaluation_document_numbers() -> NumberRange:
    """Standard cost revaluation documents occupy 52xxxxxxxx, unique per year."""
    return NumberRange(prefix="52", width=10)


def material_numbers() -> NumberRange:
    """Numeric material numbers from 1000000, as an SAP material master uses."""
    return NumberRange(prefix="", width=7, start=1_000_000)


def supplier_numbers() -> NumberRange:
    """Numeric supplier (vendor) numbers from 100000."""
    return NumberRange(prefix="", width=6, start=100_000)


def line_items(count: int, step: int = 10) -> list[int]:
    """Line item numbers in increments of ten, as SAP purchase orders use."""
    return [(i + 1) * step for i in range(count)]
