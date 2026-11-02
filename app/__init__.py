"""Streamlit data trust console (ADR-018).

Operational views that BI tools handle poorly: run history, reconciliation
results, lineage lookup, and downloadable evidence packs. This is deliberately
*not* the BI dashboard -- that is Tableau (ADR-012).

Connects only through the read-only `reporter` role, against certified
reporting tables.
"""
