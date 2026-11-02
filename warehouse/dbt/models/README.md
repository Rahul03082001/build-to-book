# Model layers

| Layer | Rule | Materialisation |
| --- | --- | --- |
| `staging/` | One model per raw table. Rename to business names, cast types, standardise units of measure, deduplicate with explicit rules. **No joins.** | view |
| `intermediate/` | Business logic: BOM explosion, receipt-to-PO matching, unit genealogy, running balances. | ephemeral |
| `marts/` | Facts and dimensions. **Grain declared at the top of every YAML.** | table |

Every model carries `_batch_id` and its source IDs through, so any row traces
back to the file that produced it.

Certified models carry:

```yaml
meta:
  certified: true
  owner: Finance
```

The lineage report and the CI lineage gate are driven by that tag
([ADR-009](../../../docs/DECISIONS.md#adr-009-sqlglot-for-column-level-lineage)),
so tagging a model brings it into scope automatically — and a certified model
with no owner fails a test.

Built out in Phase 1 (inventory: BTB-19..BTB-21) and Phase 3 (everything else:
BTB-40..BTB-45).
