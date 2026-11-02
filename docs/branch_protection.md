# Branch protection and change control

Steps to apply by hand in GitHub, plus the self-review checklist.

These are the compensating controls for the segregation-of-duties gap recorded
in [ADR-011](DECISIONS.md#adr-011-document-segregation-of-duties-as-a-known-limitation).
A one-person project cannot have a second approver, so the controls that *can*
work with one person are made as strong as possible — and the remaining gap is
stated rather than disguised.

## What this does and does not achieve

| | Status |
| --- | --- |
| No direct pushes to `main` | Enforced |
| Every change arrives by pull request | Enforced |
| Automated checks must pass before merge | Enforced |
| Every change traces to a story | Enforced by CI |
| History is linear and auditable | Enforced |
| **A second person reviewed the change** | **Not achieved — see ADR-011** |

A reviewer should test the enforced rows and read ADR-011 for the last one.

## Apply the protection

Repository → **Settings** → **Branches** → **Add branch protection rule**.

1. **Branch name pattern**: `main`
2. Tick **Require a pull request before merging**
   - **Require approvals**: 1
   - Tick **Dismiss stale pull request approvals when new commits are pushed**
   - Leave **Require review from Code Owners** off until a second reviewer
     exists; with one account it cannot be satisfied.
3. Tick **Require status checks to pass before merging**
   - Tick **Require branches to be up to date before merging**
   - Add these checks by name:
     - `PR title carries a Jira key`
     - `Lint`
     - `Tests`

   There is deliberately no separate `Pipeline` check. `dbt build` and the
   reconciliations run **inside** the `Tests` job against a freshly generated
   dataset, so the three checks above cover the whole pipeline. An earlier
   version had a standalone `Pipeline` job that reported success while running
   nothing -- a green tick claiming coverage that did not exist.
4. Tick **Require conversation resolution before merging**
5. Tick **Require linear history**
6. Tick **Do not allow bypassing the above settings**
   This is the important one. Leaving it unticked lets an administrator — which
   is the repository owner here — merge past every check, so the control would
   exist on paper only.
7. Leave **Allow force pushes** and **Allow deletions** unticked.

> **Note on approvals with a single account.** GitHub does not let you approve
> your own pull request, so with step 2's "Require approvals: 1" you will not
> be able to merge. Choose one:
>
> - **Recommended:** set **Require approvals** to 0 and rely on the status
>   checks plus the self-review checklist below. The limitation is already
>   documented in ADR-011, and a zero-approval rule is honest about it.
> - Or keep 1 approval and accept that merging needs the admin bypass, which
>   contradicts step 6.
>
> Do **not** create a second account to approve your own work. That fabricates
> the control's evidence while skipping its substance, which is the specific
> option ADR-011 rejected.

## Also set

- **Settings → General → Pull Requests**: allow squash merging only, and tick
  **Always suggest updating pull request branches**. Squash-only keeps one
  commit per story in `main`, so the Jira key in the merge commit is the
  traceability record.
- **Settings → Actions → General**: set workflow permissions to
  **Read repository contents**. The workflows request anything extra
  explicitly.
- **Settings → Environments**: create `hosted-demo` and put the hosted database
  credentials there as environment secrets rather than repository secrets, so
  only the nightly pipeline and the failure demo can read them.

## Verify it

```bash
# Should be rejected.
git checkout main && git commit --allow-empty -m "BTB-0: test direct push" && git push
```

Expected: `protected branch hook declined`. Then `git reset --hard origin/main`.

## Pull request self-review checklist

Copy into the PR description and tick every box before merging. The value is in
doing it as a separate, deliberate pass — ideally not immediately after writing
the code.

```markdown
- [ ] The PR title starts with the Jira key, e.g. `BTB-12: ...`
- [ ] Acceptance criteria from the story are all met, or the gap is stated below
- [ ] Tests cover the change, including at least one failure case
- [ ] `make check` passes locally
- [ ] Any decision with real alternatives has an ADR, and it is linked here
- [ ] A metric change updates both an ADR and docs/metric_definitions.md
- [ ] A new or changed control has a row in docs/control_matrix.csv
- [ ] No credentials, real data, or personal data in the diff
- [ ] Every number added to the README or docs names the command that reproduces it
- [ ] I have read the diff line by line, not just the summary
```

## Emergency changes

When a hotfix must bypass the normal path (built out in Phase 4, BTB-51):

1. Branch from `main` as `hotfix/BTB-NN-short-description`.
2. Fix, with a test that reproduces the failure.
3. Merge once the status checks pass — the checks are never bypassed, even for a
   hotfix.
4. Within one business day: a written after-the-fact review in the PR, an ADR
   if the fix changed a decision, and an entry in `docs/dq_issue_log.md` if data
   quality was affected.

The checks are deliberately not bypassable for emergencies. An emergency is
when a control is most likely to be needed, not least.
