"""Reconciliations, the publish gate, IPE evidence, and the access review.

Each check writes one row to `audit.reconciliation_results` with run ID,
check ID, expected, actual, difference, tolerance, and pass/fail. A failed
*critical* check blocks publishing (ADR-010) and `reporting` keeps the last
good dataset (ADR-014) -- a check that only warns still lets bad numbers
reach leaders.

Phase 1 builds the first reconciliation and the gate; Phase 4 builds the rest.
"""

# Severity decides whether a failure blocks the publish or only records a
# finding. Only CRITICAL blocks (ADR-010).
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_WARNING)
