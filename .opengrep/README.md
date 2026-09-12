# opengrep ruleset

Custom [opengrep](https://github.com/opengrep/opengrep) rules enforce this project's import-preview
invariants and coding guidelines.

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

CodeRabbit backs off from opengrep in two separate cases, and this repo avoids both.

1. **A recognised config name.** CodeRabbit auto-detects an opengrep config only when it is named
   `opengrep.yml` / `semgrep.yml` (and a few variants), and when it finds one it runs *that*
   **instead of** its default packs. The ruleset therefore lives at
   **`.opengrep/librenms-rules.yaml`**, so CodeRabbit keeps running its own default packs.
2. **An opengrep step in GitHub Actions.** When CodeRabbit sees CI already running opengrep, it
   skips its own opengrep analysis and leaves the finding to the workflow. There is therefore
   deliberately **no opengrep job** in `.github/workflows/`, even though one used to exist here.

The cost of (2) is that these rules have no CI gate. They are enforced by the pre-push hook below,
and CodeRabbit runs opengrep over the pull request itself. A push that bypasses the hook lands
unchecked until that review.

## Layout

| Path | Purpose |
| --- | --- |
| `.opengrep/librenms-rules.yaml` | The ruleset. **Single source of truth.** |
| `.opengrep/tests/*.py` | Annotated rule-test fixtures. |
| `scripts/opengrep-scan.sh` | Scan the source tree. Pre-push hook and manual use. Non-zero on any finding. |
| `scripts/opengrep-test.sh` | Run the rule-tests against the ruleset. |
| `netbox_librenms_plugin/tests/test_import_disclosure.py` | Check scan options and explicit targets with the real executable. |
| `scripts/opengrep-bin.sh` | Shared binary lookup, sourced by both scripts. |

## Rules

| Rule id | Severity | Catches |
| --- | --- | --- |
| `import-disclosure` | error | A `warnings`/`issues` message that names a NetBox object no `restrict()` call filtered. |
| `no-requests-outside-http-client` | error | Selected imported requests HTTP calls outside the package HTTP client and tests. |
| `url-numeric-pk-converter` | error | A `path()` route uses `<str:pk>` or `<pk>`, including local string constants. |
| `no-django-testcase-in-tests` | warning | A test directly imports or inherits Django `TestCase`. Dynamic bases are outside this check. |
| `no-unittest-assertions` | warning | A test calls a `self` method with a unittest assertion API name. |
| `no-selected-fuzzy-apis` | warning | Code calls selected approximate-selection APIs. This does not prove exact-only selection. |

## Scope

`import-disclosure` excludes tests and migrations. The requests rule covers
`netbox_librenms_plugin/`, except its root `librenms_api.py` and tests. The two test-convention
rules include only `netbox_librenms_plugin/tests/`. The remaining rules apply to Python files
in the scan target.

Opengrep 1.30.0 skips test directories during directory scans. The scan script expands the default
targets into the package directory and explicit Python test files. Options alone keep these defaults.
Pass options before the first `--`. The wrapper passes them to opengrep unchanged.
Pass explicit targets after `--` to replace the defaults. With no `--`, the defaults apply.

The test script stages fixtures in a flat temporary directory. In opengrep 1.30.0, `opengrep test`
ignores rule `paths` filters. A path-scoped fixture still runs there. A flipped `ruleid:` annotation
must fail with an unexpected finding on that line. Use separate scans at representative paths to
verify path inclusion and exclusion; rule-tests alone do not test that scope.

## Detection limits

The requests rule checks selected HTTP methods and session constructors with an import binding
in the same file. It accepts import aliases and imports from `requests.api`. It does not report
parameters or local variables that shadow the library. It does not follow clients passed between
functions. The package-wide ban keeps all HTTP in one client because rules cannot infer its destination.

The URL rule checks `<str:pk>` and `<pk>` in literal routes and local string constants.
It leaves `<str:id>` alone because external IDs can contain text. The `pk` name is a package
convention, not proof of a numeric type. Imported constants and dynamically built routes are outside
this check. Other converter names are outside this check.

The assertion rule checks `self` calls against explicit unittest assertion API names.
It allows custom names such as `assertResponseUnchanged`. It does not resolve the implementation
of a method that shares a unittest API name. The Django rule detects direct imports or inheritance,
including resolved aliases. It cannot see dynamic bases.

The fuzzy API rule checks calls to `difflib.get_close_matches`, `SequenceMatcher` similarity ratios,
and selected `fuzzywuzzy.fuzz` and `rapidfuzz.fuzz` scorers. Imports and diff rendering are allowed.
Symbolic propagation covers simple local constructor bindings. Other method calls can invalidate
those bindings. The rule does not track dynamically supplied scorers or prove exact-only selection.
Runtime tests must cover the exact-only invariant.

## `--taint-intrafile` is required

Both scripts pass it. Taint has to cross into a module-private helper: three warnings in
`_detect_serial_match_role` named their matched device from inside a helper, invisible to any
per-function analysis. Without the flag those sites are missed.

## Running locally

```bash
./scripts/opengrep-scan.sh   # scan (same as the pre-push hook)
./scripts/opengrep-test.sh   # run the rule-tests
./scripts/opengrep-scan.sh --json -- netbox_librenms_plugin/urls.py  # scan one target
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

1. Add fixture cases to `.opengrep/tests/<rule-id>.py` with the match and clean markers.
2. Run `./scripts/opengrep-test.sh`. Confirm it reports the expected missing findings.
3. Add the rule to `.opengrep/librenms-rules.yaml`. Run the rule-tests again and confirm they pass.
4. Flip one `ruleid:` marker to `ok:`. Confirm the rule-test reports that line, then restore it.
5. Run `./scripts/opengrep-scan.sh`. Report any findings before changing production code.
