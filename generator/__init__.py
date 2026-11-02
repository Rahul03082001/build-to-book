"""Synthetic source-data generation for Build-to-Book.

Everything this package emits is synthetic (ADR-001, ADR-019). It simulates a
rack-scale server manufacturer (ADR-003) using SAP-style ERP structures
(ADR-002) so that join paths and grains transfer to real ERP work.
"""

# Importing `injectors` is what registers them against the defect
# registry, so it must happen when the package loads -- otherwise
# `injector_for()` reports a Phase that has, in fact, been built.
from generator import injectors as _injectors  # noqa: F401

__all__ = ["config", "defects", "injectors"]
