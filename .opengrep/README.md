# opengrep ruleset

Custom [opengrep](https://github.com/opengrep/opengrep) rules that encode this project's
import-preview invariants as machine-checked gates, so the same class of defect stops coming back
review after review.

## Why opengrep (and not ruff or a hand-written checker)

- **ruff** covers generic Python lint. It has no plugin system and no user-defined rules, so a
  project-specific invariant cannot be expressed there at all.
- **CodeQL** (`.github/workflows/codeql.yml`) covers broad dataflow SAST.
- **opengrep** fills the gap: taint rules for *our* invariants, in YAML, and it is the same engine
  CodeRabbit runs.

`import-disclosure` replaces `tools/lint_import_disclosure.py`, a 595-line AST taint checker. The
rule reproduces that checker's verdicts on 39 of its 40 test cases and finds all 14 disclosure sites
it originally found in the pre-fix tree, in a fraction of the code. It also closes the two shapes
that checker documented as an unfixable limitation (see **Known limitation** below).

## Relationship to CodeRabbit (these run *on top* of CR's defaults)

CodeRabbit auto-detects an opengrep config **only** when it is named `opengrep.yml` / `semgrep.yml`
(and a few variants), and when it finds one it runs *that* **instead of** its default packs. We
deliberately do **not** use those names: the ruleset lives at **`.opengrep/librenms-rules.yaml`**, so
CodeRabbit keeps running its own default packs and these rules are enforced *additionally* by the
pre-push hook and the CI job.

## Layout

| Path | Purpose |
| --- | --- |
| `.opengrep/librenms-rules.yaml` | The ruleset. **Single source of truth.** |
| `.opengrep/tests/*.py` | Annotated rule-test fixtures. |
| `scripts/opengrep-scan.sh` | Scan the source tree. Pre-push hook and CI. Non-zero on any finding. |
| `scripts/opengrep-test.sh` | Run the rule-tests against the ruleset. |
| `scripts/opengrep-bin.sh` | Shared binary lookup, sourced by both scripts. |

## Rules

| Rule id | Severity | Catches |
| --- | --- | --- |
| `import-disclosure` | error | A `warnings`/`issues` message that names a NetBox object no `restrict()` call filtered. |

## Scope

The scan covers production code, not tests: the rule sets `paths.exclude` for `**/tests/**` and
`**/migrations/**`, and opengrep's own default ignores skip test directories too. The invariant is
about what a user is shown, so a fixture that names an object is not a finding. The checker this
replaced scanned tests as well and was clean there; both scopes report zero today.

## `--taint-intrafile` is required

Both scripts pass it. Taint has to cross into a module-private helper: three warnings in
`_detect_serial_match_role` named their matched device from inside a helper, invisible to any
per-function analysis. Without the flag those sites are missed.

## Running locally

```bash
./scripts/opengrep-scan.sh   # scan (same as the pre-push hook)
./scripts/opengrep-test.sh   # run the rule-tests
```

Both find opengrep via `$OPENGREP_BIN`, then `PATH`, then `~/.local/opt/opengrep/bin`. Install it
from <https://github.com/opengrep/opengrep> (there is no PyPI package), or set `OPENGREP_BIN`.

## Known limitation

A keyword argument read back out of a `**kwargs` dict is not followed:

```python
_describe(value=device)          # def _describe(**values): return str(values["value"])
```

The reverse shape, a caller-side `**{...}` unpacking, **is** reported. This is the one case out of 40
where the rule is less precise than the AST checker it replaced; that checker had the mirror-image
hole. Both fixtures are recorded in `.opengrep/tests/import-disclosure.py`.

## Suppressing a true exception

Add an inline `# nosemgrep: import-disclosure` on the offending line, with a short reason comment
above it.

## Adding a rule

1. Add the rule to `.opengrep/librenms-rules.yaml`.
2. Add fixture cases to `.opengrep/tests/<rule-id>.py` with the match / clean markers.
3. `./scripts/opengrep-test.sh` — confirm it passes.
4. `./scripts/opengrep-scan.sh` — confirm the tree is clean (or fix it).
