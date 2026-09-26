# Stack identity and interface ownership

## Stack serial identity

Stack detection, deduplication and creation must classify the same serial as
identity evidence. Generic serial normalization preserves strings such as `N/A`,
but these placeholders do not identify a stack member.

`normalize_stack_serial()` owns the existing placeholder set. Bulk stack identity
and all virtual-chassis consumers call it. The former `_norm_serial()` helper is
removed. Generic `normalize_serial()` keeps its existing behavior. Manufacturer
rules run before stack placeholder filtering. Real serial spelling and numeric
zero remain valid evidence.

The alternative was to copy placeholder filtering into the creator. That leaves
two definitions which can disagree. Both independent proposals selected one
shared helper instead.

The independent designer ratified the merged draft with the deletion and coverage
amendments. Real HTTP-to-ORM regressions first demonstrated incorrect master flags
and collapsed members. The repaired cases cover every placeholder, case and space
variants, unrelated placeholder-serial devices, numeric zero, and manufacturer
rules that produce placeholders. Existing decorated-master behavior remains
covered. The shared helper prevents divergent placeholder tables; the tests check
the data-dependent identity behavior.

## Out-of-band interface ownership

The interfaces table and writer must attribute an out-of-band row to the same
owner. An existing binding takes precedence, followed by physical stack-member
inference, then the page-device fallback. Explicit posted selections take
precedence over these defaults. An out-of-band row cannot adopt a host interface
by name.

The writer uses the existing row-owner resolver for host and out-of-band rows.
Host automatic relationship resolution remains strict. This change does not add
rows to automatic relationship selection. One default-owner map drives both
collision checks and selected-row writes over the complete snapshot.

Inference keeps complete current membership evidence. It substitutes locked
permitted member instances where available, then refuses destinations outside
the permitted locked set. Removing hidden members before inference would turn a
hidden destination into a page-device fallback.

Both independent proposals selected the shared resolver. The merged draft added
complete-member inference before authorization and received an independent
ratification. Five real table-to-POST cases failed before implementation. Seven
cases now cover inferred ownership, existing bindings, explicit overrides, hidden
members, and an unselected colliding host row. Same-member host and out-of-band
name collisions refuse the out-of-band write without creating a page-device copy.

## Review record and remaining work

The root agent and a second agent drafted each design independently from the
problem, constraints and code pointers. The second agent received the root draft
only after returning its proposal, then reviewed the merged design. It used the
inherited model and reasoning effort without an override. Exact runtime model
metadata was not exposed in the review record.

The combined affected request and stack suite passed 316 tests after both
implementations. These verdicts ratify the designs; they do not replace review and
full test gates for the final propagated commits. Cross-request port-binding
claims are a separate unresolved concurrency decision.

## Concurrent port binding

A binding identity is the validated server key and normalized port ID. It is shared
across Interface and VMInterface and does not depend on the owning device or VM.
The operator selected a nonblocking, retryable conflict. The independent design
review ratified the second revision, which also covers module inventory binding.

`claim_librenms_port_binding` requires an open transaction. It hashes an unambiguous
JSON identity with a namespace and uses `pg_try_advisory_xact_lock`. The claim lasts
until commit or rollback. Reentry in the same transaction succeeds. Contention
raises a typed, generic refusal without naming the other owner. It adds no blocking
wait edge to the existing owner and row locks.

Interface sync, IP interface resolution, rebind, remote cable creation and module
inventory binding claim before checking ownership or creating a row. The shared
field writer claims and reads current ownership before changing fields or MACs.
It cannot accept a precomputed absent owner. Bulk sync rolls back its attempt and
resets counters on contention. Rebind and remote creation deliver the retry message
through the existing response helpers: HTTP 302 for an ordinary request and HTTP
200 with a rendered fragment for HTMX. Module
binding reports a conflict through its existing partial-binding result. The IP
path keeps its existing per-row rollback and error ledger.

Real transaction tests cover cross-model claims, canonical numeric IDs, independent
servers, rollback and HTTP actions without partial writes. Existing scope and
permission checks still apply. Manual custom-field edits are outside this plugin
writer protocol. The later row-concurrency layer must retain its fresh attempt and
version checks when it handles this typed refusal.
