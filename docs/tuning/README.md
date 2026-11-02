# Performance tuning study

*Phase 3 (BTB-46).* Run on the **`full`** profile — 12 months, 300 materials,
20,000 PO lines, 8,000 production orders, and 5,000,000+ MES events. The `demo`
profile is far too small for any of this to be meaningful.

## Method

Every number here must come from an actual run, with the command to reproduce
it. No estimates, no "roughly".

1. Identify the slowest mart query. Expected to be inventory valuation over
   goods movements, because it scans the largest fact and joins an SCD Type 2
   dimension as-of.
2. Record `EXPLAIN (ANALYZE, BUFFERS)` **before** any change. Note the plan
   shape, the row estimates versus actuals, and where time is actually spent.
3. Try at least two remedies and measure each separately:
   - indexing on the join and filter columns,
   - partitioning by posting period,
   - converting the model to incremental.
4. Record **after** numbers using the same method, on the same data, on the same
   machine. State the machine and the Postgres settings.
5. Log the chosen fix as an ADR, including what it made worse.

## Why before-and-after must use the same method

A faster second run can be the buffer cache rather than the change. Each
measurement states whether the cache was warm, and the headline comparison uses
like-for-like runs. A cold-versus-warm comparison would make any change look
like a win.

## Results

*To be filled in by BTB-46.*

| Query | Change | Before | After | Command |
| --- | --- | --- | --- | --- |
| | | | | |
