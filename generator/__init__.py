"""Synthetic source-data generation for Build-to-Book.

Everything this package emits is synthetic (ADR-001, ADR-019). It simulates a
rack-scale server manufacturer (ADR-003) using SAP-style ERP structures
(ADR-002) so that join paths and grains transfer to real ERP work.
"""

__all__ = ["config", "defects"]
