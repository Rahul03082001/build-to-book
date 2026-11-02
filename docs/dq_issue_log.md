# Data quality issue log

One entry per data quality issue found, with root cause, fix, and what prevents
a recurrence. This is the record that a control finding led to an action rather
than a shrug.

Phase 4 (BTB-55) requires at least three real entries from the full
defect-detection run. An entry is written when a control fires and the cause is
understood — not when a defect is planted, which is expected behaviour.

## How to write an entry

```markdown
## DQ-0NN: <short title>

- Date found: YYYY-MM-DD
- Found by: <control ID, or how it was noticed>
- Severity: Critical | High | Medium | Low
- Status: Open | Fixed | Accepted
- Jira: BTB-NN

**Symptom.** What looked wrong, in business terms. What a leader would have
seen on the dashboard.

**Root cause.** Why it happened. Not "bad data" -- the mechanism.

**Impact.** Which metrics and periods were affected, and whether anything was
published before it was caught.

**Fix.** What was changed, with the commit or PR.

**Prevention.** The control, test, or design change that stops a recurrence.
If nothing prevents it, say so and explain why that is acceptable.
```

## Entries

*None yet. The first entries come from the Phase 4 detection run (BTB-55).*

## Open questions carried forward

| Question | Raised | Where it will be answered |
| --- | --- | --- |
| Should the subledger-to-GL check use a materiality threshold rather than an exact match? | ADR-010 | Phase 4 (BTB-48), as a tolerance per check in the control matrix |
| Does a rerun that re-lands an identical file count as a data quality event or a routine retry? | ADR-017 | Phase 1 (BTB-15), by distinguishing "already loaded" from "duplicated at source" |
