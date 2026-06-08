---
applyTo: "**/changelog.md,**/pyproject.toml,**/__init__.py"
---

# Release Workflow

This describes the standard release process for the NetBox LibreNMS Plugin. Follow these steps exactly when asked to create a new release.

## Branch Strategy

> **Both `develop` and `master` have branch protection.** All changes must go through pull requests — never push directly.

### Standard Flow (used for all releases)

1. **Create `release/X.Y.Z` branch** from develop
2. **Version bump commit** on the release branch
3. **PR `release/X.Y.Z` → develop** (version bump PR)
4. **PR `develop` → master** (release PR) — GitHub will auto-include any commits master is missing
5. **Tag `vX.Y.Z`** on master and create GitHub release
6. **Post-release:** master may now be ahead of develop (e.g. the merge commit). This resolves naturally — step 1 of the next release starts from the current develop, and the release PR (step 4) will reconcile any divergence.

## Files to Update

Three files must be updated in a single commit with message `Bump version to X.Y.Z and update changelog`:

### `netbox_librenms_plugin/__init__.py`
```python
__version__ = "X.Y.Z"
```

### `pyproject.toml`
```toml
version = "X.Y.Z"
```

### `docs/changelog.md`
Prepend a new section at the top (after `# Changelog`). Use the current date in `YYYY-MM-DD` format. Categories used (pick only those that apply):
- `### New Features`
- `### Improvements`
- `### Fixes`
- `### Development`
- `### Documentation`

Example:
```markdown
## X.Y.Z (YYYY-MM-DD)

### Fixes
* Description of fix (#PR_NUMBER)
```

## Version Bump PR (release/X.Y.Z → develop)

**Title:** `Bump version to X.Y.Z and update changelog`

**Body template:**
```markdown
## Summary
Bump version to X.Y.Z and update changelog for release.

## Motivation / Problem
- Maintenance / cleanup

Prepare release X.Y.Z with <brief description of what's included>.

## Scope of Change

- Config / settings
- Docs only

## How Was This Tested?

- Not tested: version bump and changelog only

## Risk Assessment
- No impact on existing users
- No code logic changes

## Backwards Compatibility
- No breaking changes
```

## Release PR (develop → master)

**Title:** `Release X.Y.Z`

**Body template:**
```markdown
## Summary
Release X.Y.Z — merge develop into master for PyPI release.

## Motivation / Problem
- <Bug | Feature | Maintenance / cleanup>

<One-line description of what this release contains, referencing PR numbers.>

## Scope of Change

<List applicable scopes from the PR template>

## Changes
<Bullet list of changes, each referencing PR numbers>
- Bump version to X.Y.Z

## How Was This Tested?

<Summarize testing from the included PRs>

## Risk Assessment
<Brief risk assessment>

## Backwards Compatibility
- No breaking changes
```

## GitHub Release Text

**Tag:** `vX.Y.Z` (create on master)
**Release title:** `vX.Y.Z`

**Body template:**
```markdown
## <Bug Fix Release | Feature Release | Maintenance Release>

<One-paragraph summary of the release. Explain what was wrong and what changed.>

### <Fixes | New Features | Improvements>

- <Change description> (#PR_NUMBER)

> ### Upgrade note
> - <Migration/collectstatic notes, or "No database migrations in this release."> Standard [update process](https://github.com/bonzo81/netbox-librenms-plugin#update) applies.

---

## All Changes
* <commit title> by @<author> in https://github.com/bonzo81/netbox-librenms-plugin/pull/<PR_NUMBER>
```

The "All Changes" section lists only the feature/fix PRs included in the release — not version bump or merge PRs.

## Checklist

- [ ] Version bumped in `__init__.py` and `pyproject.toml`
- [ ] Changelog updated in `docs/changelog.md`
- [ ] Version bump PR merged to develop
- [ ] Release PR merged to master
- [ ] Tag created on master
- [ ] GitHub release published
