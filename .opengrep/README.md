# opengrep ruleset

Custom [opengrep](https://github.com/opengrep/opengrep) rules enforce this project's import-preview and
caught-error text invariants, and its coding guidelines.

## Why opengrep

- **ruff** covers generic Python lint. It has no plugin system and no user-defined rules, so a
  project-specific invariant cannot be expressed there at all.
- **CodeQL** (`.github/workflows/codeql.yml`) covers broad dataflow SAST.
- **opengrep** fills the gap: taint rules for *our* invariants, in YAML, and it is the same engine
  CodeRabbit runs.

## Relationship to CodeRabbit (these run *on top* of CR's defaults)

CodeRabbit backs off from opengrep in two separate cases, and this repo avoids both.

1. **A recognised config name.** CodeRabbit auto-detects an opengrep config only when it is named
   `opengrep.yml` / `semgrep.yml` (and a few variants), and when it finds one it runs *that*
   **instead of** its default packs. The ruleset therefore lives at
   **`.opengrep/librenms-rules.yaml`**, so CodeRabbit keeps running its own default packs.
2. **An opengrep step in GitHub Actions.** When CodeRabbit sees CI already running opengrep, it
   skips its own opengrep analysis and leaves the finding to the workflow. There is therefore
   deliberately **no opengrep job** in `.github/workflows/`.

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
| `import-disclosure-sanitizer-shadow` | error | A local definition that impersonates a permission API trusted by `import-disclosure`. |
| `caught-error-text` | error | A read of a caught error that can be a ValidationError, a database error or an `AbortRequest`, other than through `exception_text_for`, the module logger or a `raise`. |
| `caught-error-text-shadow` | error | A binding of a name that `caught-error-text` trusts, a logging change, a risky error class under another name, or `except*`. |
| `no-requests-outside-http-client` | error | Selected imported requests HTTP calls outside the package HTTP client and tests. |
| `ipaddress-address-needs-netaddr` | error | An ipam `IPAddress` gets an address that is not a `netaddr.IPNetwork(...)` call, or a `**` expansion. |
| `url-numeric-pk-converter` | error | A `path()` route uses `<str:pk>` or `<pk>`, including local string constants. |
| `no-direct-htmx-request-header-read` | error | Code reads the `HX-Request` header (or `HTTP_HX_REQUEST`) instead of `request.htmx`. |
| `no-django-testcase-in-tests` | warning | A test directly imports or inherits Django `TestCase`. Dynamic bases are outside this check. |
| `no-unittest-assertions` | warning | A test calls a `self` method with a unittest assertion API name. |
| `xfail-needs-raises` | error | A test marks an expected failure with `pytest.mark.xfail` and does not name the exception with `raises=`, or gives `raises=None`. An alias of the marker is not followed. |
| `no-selected-fuzzy-apis` | warning | Code calls selected approximate-selection APIs. This does not prove exact-only selection. |

## Scope

`import-disclosure` and its sanitizer-shadow guard exclude tests and migrations. Canonical helper
definitions carry explicit suppressions. All other local bindings of these names are blocked.
`caught-error-text` and its shadow rule also exclude tests and migrations. The canonical
definitions of the trusted names, and each reviewed read of a caught error, carry explicit
suppressions. The requests rule covers
`netbox_librenms_plugin/`, except its root `librenms_api.py` and tests. The IPAddress rule covers
`netbox_librenms_plugin/`, except tests and migrations. The htmx header rule excludes tests,
because a test sends the header to build an htmx request. The two test-convention
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
in the same file. It accepts import aliases and imports from `requests.api` and
`requests.sessions`. It does not report parameters or local variables that shadow the library. It
does not follow clients passed between functions. The package-wide ban keeps all HTTP in one client
because rules cannot infer its destination.

The htmx header rule checks `.get()`, subscript and `in` reads whose key is the header name or its
`META` name, in any letter case, also through a local string constant. It does not see a key built
at run time.

The IPAddress rule checks the `address` keyword of the constructor and of `create`,
`get_or_create` and `update_or_create` at the end of any `IPAddress.objects` queryset chain, and
`address` in `defaults` or `create_defaults`. A `**` expansion into these calls hides the address,
so the rule reports it outright. Filters such as `filter(address=...)` are allowed. The rule cannot
infer the type of an assigned object, so it checks `.address =` and `setattr(..., "address", ...)`
only in files that import `IPAddress` from `ipam.models` or import `ipam.models` itself. A value
passed in as a `netaddr.IPNetwork` variable is also reported; wrap the value at the call.

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

The disclosure rule accepts only manager calls through `.objects` and the exact fixed-wording
linkage rendering. It does not trust a helper name. Its companion rule rejects local definitions,
assignments, and callable parameters that can obscure these taint flows.

The caught-error rule denies by default. Its source is the name of a handler whose class can catch
a ValidationError, a Django or psycopg database error or an `AbortRequest`, or is `Exception`,
`BaseException` or an exception group. A class that the rule cannot name, such as `type(exc)`,
also counts. A chained error (`__cause__`, `__context__`, a group's `exceptions`) and a call that
returns the current exception (`sys.exc_info()`, `traceback.format_exc()`, `locals()` and others)
are sources in any function. Each argument or receiver of a call, and each store, return, yield,
loop or assert message, is a finding. The exceptions are the log methods of the module `logger`, a `raise`,
and the trusted calls: `exception_text_for`, `classify_conflict`, `database_error_sqlstate`,
`isinstance`, `hasattr`, `type`, `validation_error_detail`, and a `TypeRefusal` that the reviewed
factory `_first_refusal` builds. A trusted call hides the arguments that it gets. A call of a
helper is a finding at the call, and intrafile analysis also reports the reads inside a helper of
the same file. Its limits:

- The rule knows a risky class by name: its import path, also through an import alias, or its bare
  name. It cannot follow a class in a variable, a tuple constant or an attribute of a variable,
  such as `except model.WriteFailed`. The shadow rule reports an assignment of a risky class and a
  subclass of one. The two rules keep the same class list: add a new subclass to both, with a
  fixture case for each rule.
- A reader of the current exception that code keeps as a function object, such as
  `format_error = traceback.format_exc`, is not a source.
- A slice drops taint in opengrep. The rule reads a slice of a caught name or of its attributes as
  a source, but not a slice of another tainted value, such as an attribute of a chained error.
- Opengrep 1.30.0 cannot parse `except*`. The shadow rule reports it, and `--strict` fails the
  scan on the parse warning.
- A read in a module logger call goes to the server log and is not a finding. The rule trusts the
  deployment's logging configuration. The shadow rule reports logging changes in the package only.
- The rule trusts the `TypeRefusal` that `_first_refusal` returns: it keeps the message, and only
  `TypeRefusal.text_for` may show it. A read of its `message`, or a serialization of it, is outside
  the rule. A call of `_first_refusal`, and a `TypeRefusal` built anywhere else from a caught error,
  are findings.
- The shadow rule does not see a binding by a computed name, such as `setattr(module, name, value)`.
  It reads loop, `as`, `case` and walrus targets as text, so it can report a comment that looks
  like one.
- Taint analysis does not read a lambda default, a decorator or a `match` pattern. The shadow rule
  reports a lambda default that calls a function anywhere, and a lambda default, a decorator or a
  `match` statement in a `try` statement that has a named handler. Use `functools.partial` in place
  of a lambda default.
- The rule follows a closure or a lambda that the function defines before the handler only when
  the `try` statement is at the top level of the function body.

## `--taint-intrafile` is required

Both scripts pass it. Taint has to cross into a module-private helper: three warnings in
`_detect_serial_match_role` named their matched device from inside a helper, invisible to any
per-function analysis. Without the flag those sites are missed.

## A timeout or a parse failure fails the scan

The scan script passes `--strict` and `--timeout 60`. By default, a rule that runs longer than 5 s
on a file, or a file that opengrep cannot parse, is a warning, and the scan passes without the
findings of that file. With `--strict`, each such warning fails the scan, and the 60 s limit stops a
stalled analysis. The scan still skips a file that the `.semgrepignore` rules or the size limit
exclude. `caught-error-text` needs about 5 s on `views/sync/modules.py`. CodeRabbit's own opengrep
run can time out on that file, so the pre-push hook is the gate.

## Running locally

```bash
./scripts/opengrep-scan.sh   # scan (same as the pre-push hook)
./scripts/opengrep-test.sh   # run the rule-tests
./scripts/opengrep-scan.sh --json -- netbox_librenms_plugin/urls.py  # scan one target
```

The devcontainer installs opengrep 1.30.0. Outside the devcontainer, both scripts find opengrep via
`$OPENGREP_BIN`, then `PATH`, then `~/.local/opt/opengrep/bin`. Install it from
<https://github.com/opengrep/opengrep> (there is no PyPI package), or set `OPENGREP_BIN`.

## Known limitation

A keyword argument read back out of a `**kwargs` dict is not followed:

```python
_describe(value=device)          # def _describe(**values): return str(values["value"])
```

The reverse shape, a caller-side `**{...}` unpacking, **is** reported. Both fixtures are recorded in
`.opengrep/tests/import-disclosure.py`.

## Suppressing a true exception

Add `# nosemgrep: <rule-id>` on its own line directly above the first line of the finding, with a
short reason comment above it. A finding in a multi-line call starts on the line of the argument,
so put the comment directly above that argument. `# nosemgrep: caught-error-text` parses as Python,
so ruff's ERA001 reads it as commented-out code: add `# noqa: ERA001` after the rule id.
A suppression covers each finding of its rule on its line. Keep a reviewed call in a statement of
its own, and build the message on the next line, where the rule still checks it.

## Adding a rule

1. Add fixture cases to `.opengrep/tests/<rule-id>.py` with the match and clean markers.
2. Run `./scripts/opengrep-test.sh`. Confirm it reports the expected missing findings.
3. Add the rule to `.opengrep/librenms-rules.yaml`. Run the rule-tests again and confirm they pass.
4. Flip one `ruleid:` marker to `ok:`. Confirm the rule-test reports that line, then restore it.
5. Run `./scripts/opengrep-scan.sh`. Report any findings before changing production code.
