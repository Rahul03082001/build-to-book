"""Supplier invoices (BTB-31).

The third leg of the three-way match. CTRL-004 compares PO line, goods receipt
and invoice on quantity and price; until now only two of the three existed, so
the control had nothing to match against.

Invoices reference the PO line and the goods receipt that preceded them, which
is what makes the match a join rather than a guess. With defects off, every
invoice agrees with its PO on price and with its receipts on quantity, so
CTRL-004 passes with no exceptions -- and the `invoice_price_outlier` and
`receipt_without_po` defects in BTB-37 then have something to break.

Shaped on SAP's invoice header and item (RBKP and RSEG), per ADR-002. An
invoice can cover several PO lines, so the header/item split is not decorative:
a count of invoices and a count of invoice lines are different numbers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from generator import ids
from generator.writers import ExtractSpec, money

#: Share of receipted PO lines that are invoiced inside the window. The rest
#: are goods-received-not-invoiced at period end, which is a real balance and
#: the reason the GR/IR clearing account exists.
INVOICED_FRACTION = 0.88

#: Days between the last receipt on a PO line and the supplier's invoice.
INVOICE_LAG_DAYS = (2, 25)

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "supplier_invoice_headers": ExtractSpec(
        name="supplier_invoice_headers",
        columns=[
            "invoice_number",
            "fiscal_year",
            "supplier_number",
            "invoice_date",
            "posting_date",
            "currency",
            "gross_amount_usd",
            "payment_terms",
            "entered_by",
        ],
        source_system="ERP",
        value_column="gross_amount_usd",
        natural_key=["invoice_number", "fiscal_year"],
    ),
    "supplier_invoice_lines": ExtractSpec(
        name="supplier_invoice_lines",
        columns=[
            "invoice_number",
            "fiscal_year",
            "invoice_item",
            "po_number",
            "po_item",
            "material_number",
            "quantity_invoiced",
            "unit_price_usd",
            "net_amount_usd",
            "uom",
        ],
        source_system="ERP",
        value_column="net_amount_usd",
        natural_key=["invoice_number", "fiscal_year", "invoice_item"],
    ),
}


@dataclasses.dataclass
class InvoiceData:
    headers: list[dict[str, Any]]
    lines: list[dict[str, Any]]

    def as_mapping(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "supplier_invoice_headers": self.headers,
            "supplier_invoice_lines": self.lines,
        }


class InvoiceGenerator:
    """Invoices against receipted purchase order lines."""

    def __init__(
        self,
        purchase_order_headers: Sequence[dict[str, Any]],
        purchase_order_lines: Sequence[dict[str, Any]],
        suppliers: Sequence[dict[str, Any]],
        goods_movements: Sequence[dict[str, Any]],
        rng: Any,
        uom: str,
        period_end: dt.date,
    ) -> None:
        self.rng = rng
        self.uom = uom
        self.period_end = period_end
        self._numbers: dict[int, ids.NumberRange] = {}

        self._supplier_by_po = {
            header["po_number"]: header["supplier_number"] for header in purchase_order_headers
        }
        # Payment terms come from the supplier master. An earlier version read
        # them from the PO header, which does not carry them -- so every
        # invoice silently got the "NET30" default and the column looked
        # populated while meaning nothing.
        self._terms_by_supplier = {
            supplier["supplier_number"]: supplier["payment_terms"] for supplier in suppliers
        }
        self._po_lines = {
            (line["po_number"], line["po_item"]): line for line in purchase_order_lines
        }

        # Receipts per PO line, so an invoice can match the quantity actually
        # delivered rather than the quantity ordered. Invoicing the ordered
        # quantity would make the three-way match trivially pass even when a
        # delivery was short.
        receipts: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for movement in goods_movements:
            if movement["movement_type"] != "101":
                continue
            key = (movement["po_number"], movement["po_item"])
            receipts.setdefault(key, []).append(movement)
        self._receipts = receipts

    def _range(self, fiscal_year: int) -> ids.NumberRange:
        if fiscal_year not in self._numbers:
            self._numbers[fiscal_year] = ids.invoice_numbers()
        return self._numbers[fiscal_year]

    def generate(self) -> InvoiceData:
        headers: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []

        # Group receipted PO lines by supplier and by the week the last receipt
        # landed, so one invoice can cover several lines -- as a real supplier
        # invoices a delivery run rather than a single item.
        groups: dict[tuple[str, dt.date], list[tuple[tuple[str, int], dt.date]]] = {}
        for key in sorted(self._receipts):
            if self.rng.random() > INVOICED_FRACTION:
                # Received but not yet invoiced at period end: a real GR/IR
                # balance, and the reason that clearing account exists.
                continue
            movements = self._receipts[key]
            last_receipt = max(m["posting_date"] for m in movements)
            supplier = self._supplier_by_po.get(key[0])
            if supplier is None:
                continue
            week = last_receipt - dt.timedelta(days=last_receipt.weekday())
            groups.setdefault((supplier, week), []).append((key, last_receipt))

        for (supplier, _week), members in sorted(groups.items()):
            latest = max(receipt_date for _key, receipt_date in members)
            lag = int(self.rng.integers(*INVOICE_LAG_DAYS))
            invoice_date = min(latest + dt.timedelta(days=lag), self.period_end)
            fiscal_year = invoice_date.year
            invoice_number = self._range(fiscal_year).next()

            gross = money(0)
            for position, (key, _receipt_date) in enumerate(members, start=1):
                po_line = self._po_lines[key]
                # Invoiced quantity is what was delivered, not what was
                # ordered -- a short delivery must show up in the match.
                delivered = sum(m["quantity"] for m in self._receipts[key])
                unit_price = po_line["unit_price_usd"]
                net = money(unit_price * delivered)
                gross = money(gross + net)
                lines.append(
                    {
                        "invoice_number": invoice_number,
                        "fiscal_year": fiscal_year,
                        "invoice_item": position * 10,
                        "po_number": key[0],
                        "po_item": key[1],
                        "material_number": po_line["material_number"],
                        "quantity_invoiced": delivered,
                        "unit_price_usd": unit_price,
                        "net_amount_usd": net,
                        "uom": self.uom,
                    }
                )

            headers.append(
                {
                    "invoice_number": invoice_number,
                    "fiscal_year": fiscal_year,
                    "supplier_number": supplier,
                    "invoice_date": invoice_date,
                    # Posting date can lag the supplier's own invoice date;
                    # reporting uses posting date (docs/metric_definitions.md).
                    "posting_date": min(
                        invoice_date + dt.timedelta(days=int(self.rng.integers(0, 4))),
                        self.period_end,
                    ),
                    "currency": "USD",
                    "gross_amount_usd": gross,
                    "payment_terms": self._terms_by_supplier[supplier],
                    "entered_by": "AP0" + str(int(self.rng.integers(1, 4))),
                }
            )

        return InvoiceData(headers=headers, lines=lines)
