<!-- Title must start with the Jira key, e.g. "BTB-12: add goods receipt staging model" -->

## What this changes

<!-- One or two sentences in plain language. -->

## Story

Jira: BTB-NN

## Test evidence

<!-- Paste the command and its output. "Tests pass" on its own is not evidence. -->

```
$ make check
```

## Decisions

<!-- Link any ADR this change adds or supersedes, or write "none". -->

## Self-review checklist

See [docs/branch_protection.md](../docs/branch_protection.md). Segregation of
duties is a known limitation of this project ([ADR-011](../docs/DECISIONS.md#adr-011-document-segregation-of-duties-as-a-known-limitation));
this checklist is one of its compensating controls.

- [ ] The PR title starts with the Jira key
- [ ] Acceptance criteria from the story are all met, or the gap is stated above
- [ ] Tests cover the change, including at least one failure case
- [ ] `make check` passes locally
- [ ] Any decision with real alternatives has an ADR, and it is linked above
- [ ] A metric change updates both an ADR and docs/metric_definitions.md
- [ ] A new or changed control has a row in docs/control_matrix.csv
- [ ] No credentials, real data, or personal data in the diff
- [ ] Every number added to the README or docs names the command that reproduces it
- [ ] I have read the diff line by line, not just the summary
