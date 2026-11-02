"""Defect injection registry.

Thirteen known defects, each behind its own switch in config.yaml. The point
of planting them is to prove that each control catches its defect (ADR-001):
a control that has never failed has never been tested.

Every injected record is recorded in `defects_manifest.json` so a test can
assert that a control caught exactly what was planted. Without the manifest a
test can only check that *something* was flagged, which is much weaker
evidence.

Phase 0 fixes the registry and the manifest contract. The injectors themselves
land with the extracts they corrupt: two in Phase 1, the rest in Phase 2.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Control identifiers, as they appear in docs/control_matrix.csv. Each defect
# declares which control is expected to catch it, so Phase 4's detection test
# is a lookup rather than a judgement call.
# ---------------------------------------------------------------------------
CTRL_SOURCE_TO_RAW = "CTRL-001"
CTRL_RAW_TO_MART = "CTRL-002"
CTRL_SUBLEDGER_TO_GL = "CTRL-003"
CTRL_THREE_WAY_MATCH = "CTRL-004"
CTRL_MES_TO_ERP = "CTRL-005"
CTRL_CLOSED_PERIOD = "CTRL-006"
CTRL_COST_APPROVAL = "CTRL-007"
CTRL_FRESHNESS = "CTRL-008"
CTRL_REFERENTIAL = "CTRL-009"
CTRL_NON_NEGATIVE_INVENTORY = "CTRL-010"
CTRL_UOM_CONSISTENCY = "CTRL-011"


@dataclasses.dataclass(frozen=True)
class DefectSpec:
    """One plantable defect.

    Attributes:
        key: Switch name in config.yaml. Must match exactly.
        title: Human-readable name used in the README detection table.
        simulates: The real-world failure this stands in for.
        target_extract: Which source extract gets corrupted.
        expected_control: The control that must catch it.
        preventive: True if the control should stop the bad row at the door,
            False if it is detected after landing. Auditors care about this
            distinction, so the registry records it explicitly.
        phase: The phase that implements the injector.
    """

    key: str
    title: str
    simulates: str
    target_extract: str
    expected_control: str
    preventive: bool
    phase: int


# ---------------------------------------------------------------------------
# The registry. Order matches the defect table in the project spec.
# ---------------------------------------------------------------------------
REGISTRY: dict[str, DefectSpec] = {
    spec.key: spec
    for spec in [
        DefectSpec(
            key="duplicate_goods_receipt",
            title="Duplicate goods receipts",
            simulates="An interface retry posting the same receipt twice.",
            target_extract="erp_goods_movements",
            expected_control=CTRL_SOURCE_TO_RAW,
            preventive=True,
            phase=1,
        ),
        DefectSpec(
            key="backdated_gl_posting",
            title="Backdated GL posting",
            simulates="An entry posted into a closed period.",
            target_extract="finance_gl_postings",
            expected_control=CTRL_CLOSED_PERIOD,
            preventive=True,
            phase=1,
        ),
        DefectSpec(
            key="late_arriving_mes_events",
            title="Late-arriving MES events",
            simulates="Events landing after the period closed.",
            target_extract="mes_unit_events",
            expected_control=CTRL_MES_TO_ERP,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="uom_mismatch",
            title="Unit-of-measure mismatch",
            simulates="Each versus box-of-10 on one component.",
            target_extract="erp_goods_movements",
            expected_control=CTRL_UOM_CONSISTENCY,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="orphaned_po_lines",
            title="Orphaned PO lines",
            simulates="Lines whose header is missing.",
            target_extract="erp_purchase_orders",
            expected_control=CTRL_REFERENTIAL,
            preventive=True,
            phase=2,
        ),
        DefectSpec(
            key="receipt_without_po",
            title="Receipt without a PO",
            simulates="An unmatched receipt that breaks the three-way match.",
            target_extract="erp_goods_movements",
            expected_control=CTRL_THREE_WAY_MATCH,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="invoice_price_outlier",
            title="Invoice price outlier",
            simulates="Invoice price far above the PO price.",
            target_extract="erp_supplier_invoices",
            expected_control=CTRL_THREE_WAY_MATCH,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="unapproved_standard_cost_change",
            title="Unapproved standard cost change",
            simulates="A mid-period cost change with no approval record.",
            target_extract="erp_standard_costs",
            expected_control=CTRL_COST_APPROVAL,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="mes_erp_completion_gap",
            title="MES versus ERP completion gap",
            simulates="Units passed final test but never confirmed in ERP.",
            target_extract="erp_production_orders",
            expected_control=CTRL_MES_TO_ERP,
            preventive=False,
            phase=2,
        ),
        DefectSpec(
            key="missing_supplier_on_shipment",
            title="Missing supplier on shipment",
            simulates="A null foreign key from the supply chain feed.",
            target_extract="supply_chain_shipments",
            expected_control=CTRL_REFERENTIAL,
            preventive=True,
            phase=2,
        ),
        DefectSpec(
            key="truncated_extract",
            title="Truncated extract",
            simulates="A source file with rows silently dropped.",
            target_extract="erp_goods_movements",
            expected_control=CTRL_SOURCE_TO_RAW,
            preventive=True,
            phase=2,
        ),
        DefectSpec(
            key="stale_extract",
            title="Stale extract",
            simulates="A source file older than its expected refresh window.",
            target_extract="mes_unit_events",
            expected_control=CTRL_FRESHNESS,
            preventive=True,
            phase=2,
        ),
        DefectSpec(
            key="negative_on_hand",
            title="Negative on-hand inventory",
            simulates="An issue posted before its receipt.",
            target_extract="erp_goods_movements",
            expected_control=CTRL_NON_NEGATIVE_INVENTORY,
            preventive=False,
            phase=2,
        ),
    ]
}


@dataclasses.dataclass
class InjectedRecord:
    """One record a defect injector corrupted or added.

    `natural_key` must identify the row in the *source* extract, so that a
    control's finding can be matched back to the plant without relying on
    surrogate keys assigned at load time.
    """

    defect_key: str
    extract: str
    natural_key: dict[str, Any]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "defect_key": self.defect_key,
            "extract": self.extract,
            "natural_key": self.natural_key,
            "note": self.note,
        }


@dataclasses.dataclass
class DefectManifest:
    """Everything that was planted in one generation run."""

    seed: int
    profile: str
    generated_at: dt.datetime
    records: list[InjectedRecord] = dataclasses.field(default_factory=list)

    def add(self, record: InjectedRecord) -> None:
        if record.defect_key not in REGISTRY:
            raise KeyError(f"{record.defect_key!r} is not in the defect registry")
        self.records.append(record)

    def count_for(self, defect_key: str) -> int:
        return sum(1 for r in self.records if r.defect_key == defect_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "profile": self.profile,
            "generated_at": self.generated_at.isoformat(),
            "defects_enabled": sorted({r.defect_key for r in self.records}),
            "total_injected": len(self.records),
            "counts_by_defect": {
                key: self.count_for(key) for key in sorted({r.defect_key for r in self.records})
            },
            "records": [r.to_dict() for r in self.records],
        }

    def write(self, path: pathlib.Path) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def read(cls, path: pathlib.Path) -> dict[str, Any]:
        """Read a manifest back as a plain dict, for assertions in tests."""
        return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Injectors. Registered per defect key as the extracts they corrupt land.
# ---------------------------------------------------------------------------
Injector = Callable[..., list[InjectedRecord]]

INJECTORS: dict[str, Injector] = {}


def register(defect_key: str) -> Callable[[Injector], Injector]:
    """Attach an injector to a registry entry."""
    if defect_key not in REGISTRY:
        raise KeyError(f"{defect_key!r} is not in the defect registry")

    def decorator(fn: Injector) -> Injector:
        INJECTORS[defect_key] = fn
        return fn

    return decorator


def injector_for(defect_key: str) -> Injector:
    """Return the injector for a defect, or explain which phase builds it."""
    spec = REGISTRY[defect_key]
    if defect_key not in INJECTORS:
        raise NotImplementedError(
            f"{spec.title!r} ({defect_key}) is injected in Phase {spec.phase}; "
            f"no injector is registered yet."
        )
    return INJECTORS[defect_key]


def unimplemented() -> list[str]:
    """Registry keys with no injector yet, for the Phase 0 status report."""
    return [key for key in REGISTRY if key not in INJECTORS]
