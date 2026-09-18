---
description: Repeatable workflow for creating the develop → master prep PR ahead of a release (no version bump)
---

# Release Prep PR (develop → master)

This describes the repeatable workflow for creating the **prep merge PR** that brings all `develop` changes into `master` ahead of a release. It is separate from [release.instructions.md](release.instructions.md), which covers the version-bump PR (`release/X.Y.Z` → `develop`) and the final release PR/tag. This prep PR carries **no version bump** — that follows afterward in its own PR per the standard release workflow.

> Both `develop` and `master` have branch protection. This PR merges into the default branch (`master`) — treat it as a step that needs user review before creation, not something to auto-create/push.

## Steps

1. **Find the cutoff** — get the most recent published release/tag on `master` (e.g. `v0.4.7`) and its publish date:
   ```
   gh api repos/<owner>/<repo>/releases/tags/vX.Y.Z --jq '.published_at'
   ```
2. **List PRs merged into `develop` since that date**, chronologically:
   ```
   gh pr list --repo <owner>/<repo> --base develop --state merged --search "merged:>DATE" --json number,title,mergedAt
   ```
3. **Summarize each PR** — fetch title/body (`gh pr view <n> --json title,body`) and group into changelog-style categories: New Features, Fixes, Development (CI/dependency bumps), Documentation. Reference each by PR number.
4. **Determine issue closures — requires explicit user confirmation:**
   - Closing keywords (`Fixes`/`Closes`/`Resolves #N`) only auto-close an issue when the PR that contains them is merged into the repo's **default branch**. Since the underlying feature PRs merged into `develop` (non-default), any keyword references in their bodies did **not** auto-close the issues yet.
   - Search merged PR bodies for closing keywords (`grep -oiE '(close[sd]?|fix(es|ed)?|resolve[sd]?) #[0-9]+'`) and cross-check against the currently open issues list (`gh issue list --state open`) to find candidates.
   - **Do not add `Closes #N` lines automatically based on this search alone.** Present the candidate list (and any issues you couldn't verify via a keyword match) to the user and ask them to explicitly confirm which issues this prep PR should close.
   - Use one `Closes #N` per line — GitHub requires the keyword to immediately precede each issue number; a comma-separated list like `Closes #1, #2` only closes the first.
5. **Draft the PR body** with:
   - Title: `Release X.Y.Z prep` (no version number bump in this PR — note that it follows in a separate PR)
   - Summary / Motivation / Scope of Change
   - Changes, grouped by category from step 3, each bullet referencing PR numbers
   - Confirmed `Closes #N` lines from step 4
   - How Was This Tested / Risk Assessment / Backwards Compatibility
6. **Present the draft PR description to the user for review.** Only run `gh pr create` (or instruct the user to paste it into the GitHub UI) once they've confirmed the content, including the issue closures.
