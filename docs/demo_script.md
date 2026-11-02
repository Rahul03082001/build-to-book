# Five-minute demo script

*Filled in after Phase 6, when every step has something live to show (BTB-73).*
The running order below is settled; the per-step detail and the rehearsed timing
come later.

## Before you start — the warm-up

Free tiers sleep. All three hosted components must be woken, and this is the
step most likely to be skipped and most likely to matter:

| Component | Sleeps after | Wake-up |
| --- | --- | --- |
| Neon (database) | 5 min idle | Resumes by itself on the first query; the first page load is slow |
| Streamlit console | 12 h without traffic | Visiting the URL wakes it; can take a minute |
| Tableau Public | Does not sleep | — |

**Open every live link five minutes before the demo.** Not one minute: a
Streamlit cold start plus a Neon resume can take longer than a silent pause is
comfortable.

## The seven steps, about 45 seconds each

1. **The problem.** Leaders at a public manufacturer need inventory and
   production numbers they can trust, and auditors need to be able to test them.
   Say plainly: the data is synthetic, the controls are simulated, SOX-style.

2. **Tableau dashboard.** Inventory value, yield, supplier delivery, cost
   variance. The business view first, so the controls have something to be
   about.

3. **Trust console.** Take one KPI from that dashboard and show its last
   successful refresh time and whether its reconciliations passed in that run.
   This is the "can I trust this number?" moment.

4. **Lineage lookup.** Trace the same KPI back to its source columns. Same
   number, now with a provenance chain.

5. **Failure demo.** Trigger a duplicate receipt in the **sandbox** schema.
   Show the blocked publish, and `reporting` still holding the last good
   dataset with its honest refresh time. Say explicitly that this runs against
   sandbox and the live data is untouched.

6. **Governance.** A pull request with its Jira key and CI checks, one ADR, and
   the control matrix. Mention the segregation-of-duties limitation here rather
   than waiting to be asked — it reads as judgement, not as a gap.

7. **Close.** What changes in production: a real ERP connection, a second
   approver, and an actual auditor testing the controls.

## Backup plan

Free tiers go down, and an interview is exactly when it will happen.

- A three-minute screen recording, dated, linked from the README.
- Dated screenshots of every dashboard page in `dashboards/`.
- `make all` runs the whole pipeline locally, so a laptop with Docker can
  demonstrate everything except the public URLs.

If a service is down, say so once, switch to the recording, and carry on. Do not
spend demo time debugging someone else's free tier.

## Likely questions, and where the answer lives

| Question | Answer |
| --- | --- |
| "Is this real data?" | No — synthetic, with deliberately planted defects so each control can be proven to catch them (ADR-001) |
| "Who approves changes?" | Nobody else — a documented limitation with compensating controls (ADR-011) |
| "What if a check is wrong?" | A false positive blocks the refresh, not the dashboard; `reporting` keeps the last good dataset (ADR-010, ADR-014) |
| "Why Postgres and not Snowflake?" | Cost, and Postgres has the role model the access controls need (ADR-004) |
| "Why not just warn instead of blocking?" | A warning next to a number on screen gets ignored (ADR-010) |

Fuller answers in `docs/interview_notes.md` (Phase 7, BTB-74).
