# Interface ignore rules

Status: **RATIFIED (split core + cable mechanism), 2026-09-23.** Target branch:
`feat/interface-member-badges`. Deferred: type-vs-links validation and module port binding.

## 1. Brief

### Decision

Operators want to keep some LibreNMS ports out of interface sync on some platforms (for example
VLAN SVIs, loopbacks, or internal ports on one vendor). They also want to set a NetBox interface
type from the port name and the platform, not only from `ifType` and speed. A planned background
sync job must apply the same rules as the UI.

### Settled by the operator (not open)

1. The rules extend `InterfaceTypeMapping`. There is no second table and no saved search.
2. A rule gets an optional NetBox `Platform` foreign key. It is not a LibreNMS OS string.
3. A rule gets an optional name regex, matched against `ifName` or `ifDescr`.
4. A rule gets an action: **Set type** (today's behaviour) or **Ignore**.
5. Ignore always wins over Set type.
6. Ignore never deletes or changes an interface that exists in NetBox.
7. The interfaces sync table hides ignored ports by default. An "N ignored" toggle shows them
   greyed out, with the matching rule and no sync checkbox.

### Open decisions

- A. The specificity order between Set-type rules, and what happens at a tie.
- B. The uniqueness constraints once platform and pattern are part of a rule's key.
- C. Which consumers must honour Ignore: the interfaces-tab sync writer, the IP tab
  resolve-or-create, the cables tab far end, VC member rows, OOB rows, LAG members and parents
  whose partner is ignored, the future job.
- D. The shape of the one shared matcher (the module, its interface, where it is loaded once per
  request), and how the table's type-mapping cache changes.
- E. Which platform a row reads: the synced device, the VC member that owns the port, or a VM.

### Constraints

- One definition read by the table, the writer and a future job (repo rule: the row sync diff is
  one definition, `interface_diff.py`).
- Fail closed on ambiguity; no silent fallback.
- No backwards-compatibility layer. A migration is allowed. The model keeps working for existing
  `ifType` + speed rows without edits.
- Regexes are Python `re`, validated at save time.
- The PR must stay at or below 100 changed files (now 71).

### Observable acceptance conditions

1. With an enabled Ignore rule `name ^Vlan` for platform P, a device on P shows no `Vlan*` rows
   in the interfaces tab by default. The toggle shows them greyed out with the rule name and no
   checkbox. A device on another platform still shows them.
2. A sync POST that names an ignored port does not create or change a NetBox interface.
3. A Set-type rule `platform P, name ^Te` makes a `Te1/1` port on P sync as that rule's type,
   and the table's Type column shows the same type.
4. Existing `ifType` + speed rows give the same result as before for every port that no new rule
   matches.
5. Two equal-rank Set-type rules that both match one port show the row as ambiguous, and the
   writer does not set a type.

### Evidence pointers

- Model: `netbox_librenms_plugin/models.py` (`InterfaceTypeMapping`; compare `InventoryIgnoreRule`
  for an existing action/skip rule and `PortStackLagPattern` for per-OS regexes).
- Selection rule: `utils.select_interface_type_mapping`.
- Writer: `interface_sync.get_netbox_interface_type`, `update_interface_from_port`,
  `resolve_or_create_interface_from_port`.
- Table: `tables/interfaces.py` (`render_type`, `_interface_type_mapping_cache`).
- Tab view: `views/base/interfaces_view.py` (`get_context_data`, `get_table`).
- Sync POST: `views/sync/interfaces.py`.
- Other consumers: `views/sync/ip_addresses.py` (resolve-or-create), `views/sync/cables.py`
  (far-end type), `interface_diff.py`.
- Mapping UI and API: `forms.py`, `filters.py`, `tables/mappings.py`, `views/mapping_views.py`,
  `api/serializers.py`.

## 2. Designs

Blindness: design A (Claude Opus 5.5, main session) was written from the brief and the code alone.
Design B (codex `gpt-6-astra`, effort xhigh, `-s read-only`, fresh context) got only section 1 and
a fixed list of required sections. Neither saw the other before both were complete.

### 2A. Claude design

**Model.** On `InterfaceTypeMapping`:

- `action`: `CharField`, choices `set_type` / `ignore`, default `set_type`. The migration gives
  existing rows `set_type`.
- `platform`: `ForeignKey(dcim.Platform, null=True, blank=True, on_delete=CASCADE)`. Never
  `SET_NULL`: that would turn a platform-scoped Ignore into a global one. NetBox's delete
  confirmation lists the rules it removes.
- `name_pattern`: `CharField(max_length=200, blank=True, default="")`. Python `re`, compiled in
  `clean()`, matched with `re.search` against `ifName` and against `ifDescr`. Either match counts.
- `librenms_type` becomes `blank=True`; `""` means any type.
- `netbox_type` becomes `blank=True`, no default. `clean()`: Set type requires it; Ignore requires
  it blank.
- `librenms_speed` stays. `clean()` rejects a speed on an Ignore rule (a speed threshold has no
  meaning for a filter) and a speed without a type (today's semantics).
- `clean()` requires at least one of platform, name pattern, type.
- Uniqueness is over the match fields, not the action: one rule per
  `(platform, name_pattern, librenms_type, librenms_speed)`. With `""` for the text fields, only
  `platform` and `librenms_speed` can be NULL, so four conditional `UniqueConstraint`s (platform
  null/set x speed null/set) cover it on every PostgreSQL version NetBox supports.
  `nulls_distinct=False` needs PostgreSQL 15, so it is not used. `clean()` gives the readable
  error.

**Matching.** One rule set, loaded once (all rows, patterns compiled). For a port and a platform id:

1. Candidates: rules whose platform is NULL or equals the row's platform, whose type is `""` or
   equals `ifType`, whose pattern is `""` or matches `ifName` or `ifDescr`, and (Set type only)
   whose speed is NULL or at or below the port speed.
2. Any Ignore candidate: the port is ignored. The decision names every matching Ignore rule.
3. Otherwise rank Set-type candidates by the tuple (platform set, pattern set, type set, speed or
   -1). The top rank wins. The speed part keeps today's rule: the highest threshold at or below the
   port speed beats the any-speed row.
4. Two or more candidates on the top rank: ambiguous. The writer treats the port as unmapped (a
   new interface gets `other`, an existing type is kept). The Type cell names the rules.

Worked examples: global `ethernetCsmacd -> 1000base-t` and `P, ^Te -> 10gbase-x+`: a `Te1/1`
`ethernetCsmacd` port on P gets `10gbase-x+`; on platform Q it gets `1000base-t`. Global `^Te` and
`P, ethernetCsmacd`: on P the platform rule wins (platform outranks pattern). `P, ^Te` and
`P, /1$`: `Te1/1` on P is ambiguous.

Row platform: the `platform_id` of the NetBox object the row writes to: the resolved chassis
member for a VC row (`resolve_interface_row_device`), the device, or the VM. An OOB row reads the
device it writes to. A device with no platform matches only global rules.

**Shared matcher.** New module `interface_rules.py`:

- `InterfaceRules.load()` reads every rule once and compiles patterns.
- `rules.decide(port, platform_id) -> RuleDecision` with `ignored_by: tuple[rule, ...]`,
  `type_rule: rule | None`, `ambiguous: tuple[rule, ...]`, and the property `netbox_type`.
- It replaces `utils.select_interface_type_mapping`, `interface_sync.get_netbox_interface_type`
  and the table's `_interface_type_mapping_cache`. All three are deleted. Each request loads the
  rule set once and threads it to its consumers (the table, the sync POST, the IP tab, the cables
  far-end create).

**Consumers.**

| path | ignored port |
|---|---|
| interfaces tab render (`interfaces_view.get_context_data`) | Enrichment runs on every port, so an ignored port bound to an interface still counts as matched and that interface never shows as NetBox-only. Name decisions (`synced_interface_names`, `reported_name_owners`) keep ignored ports as input, so hiding a port never changes another row's name. The row gets `ignored_by`; the table data drops it unless the toggle is on. |
| sync POST loop (`sync_selected_interfaces`) | Skip with a reason naming the rule. A crafted POST cannot sync it. |
| `auto_select_lag_members` closure | Never adds an ignored port and does not walk through one. |
| relationship edges (`_sync_interface_relationships`) | An ignored source is not written. An edge from a synced source to an ignored target that exists in NetBox is written, because only the source interface changes. |
| single-row verify, Rebind, single relationship views | Refuse an ignored port with the same reason. |
| IP tab `resolve_or_create_interface_from_port` | Return an interface already bound to the port. Refuse to adopt by name or create. Adopting writes the port id, which changes the interface. |
| cables far-end create | Refuse and do not offer the action for an ignored far-end port (the far device's platform). |
| row sync diff (`compute_row_sync_state`) | Unchanged; its `netbox_type` input comes from the decision. |
| future job | Calls `decide()` per port. |

**Table UI.** The tab view counts ignored rows and passes the count to the template. A `show_ignored`
GET/POST flag (carried like `interfaces_page`) decides whether ignored rows are in the table data.
Pagination therefore counts only the rows it shows. Shown ignored rows are greyed, carry the rule
names, and have no checkbox, so select-all and off-page selection never include them.

**Mapping UI and API.** The form, filter set, filter form, list table, bulk import, YAML export
and REST serializer gain `action`, `platform` (by slug in import) and `name_pattern`. The list
page title becomes "Interface Mappings".

**Failure behaviour.** An invalid regex fails `clean()`. A deleted platform removes its rules.
Ambiguity is visible in the table and never writes a type.

**Tests.** Real DB, no mocks: model `full_clean()` cases; `decide()` against real rows for
every rank and the tie; interfaces tab GET with a real Ignore rule (row hidden, count shown,
toggle shows it without a checkbox); sync POST that names an ignored port (nothing created, skip
message); IP tab create refused; migration test that existing rows keep their result.

**Files.** About 14 new to the PR: models, migration, forms, filters, tables/mappings,
serializers, the matcher module, 1-2 templates, 3 test files, this record. The PR goes from 71 to
about 85.

### 2B. Codex design

The full text is not kept. Section 3 records every divergence between 2A and 2B and how each was
settled.

## 3. Divergence table

| # | decision | A (Claude) | B (codex) | evidence | disposition |
|---|---|---|---|---|---|
| 1 | extra fields | none | `name` label, `enabled` flag | not in the brief; `description` and `__str__` identify a rule | A. Reopen if the operator asks for a toggle. |
| 2 | platform on delete | `CASCADE` | `PROTECT` | `PlatformMapping.netbox_platform` is `CASCADE` (`models.py:806`); NetBox's delete confirmation lists dependants | A. Never `SET_NULL`. |
| 3 | Ignore output type | `""` | `NULL` | `netbox_type` has choices; NULL reads as "no output" without a sentinel | B. |
| 4 | unique key | match fields only (Ignore and Set with the same selectors are rejected) | includes `action` (both allowed; Ignore wins) | with Ignore always winning, a Set rule on the same selectors never takes effect | A. A rule that can never apply is rejected at save time. |
| 5 | speed | only with a type, never on Ignore | a selector on its own, allowed on Ignore | today speed only refines a type (`select_interface_type_mapping`) | A. Fewer meanings. |
| 6 | DB checks | `clean()` only | check constraints for action/type pairing and "at least one selector" | fail fast at the DB as well | B. |
| 7 | ambiguity | treated as unmapped, row syncs | row operation blocked | global rule: no silent fallbacks | B. Brief condition 5 tightened: no interface write for the row. |
| 8 | edges to an ignored endpoint | written when only the source changes | skipped if either end is ignored or ambiguous | `_prepare_bulk_lag_aggregate` promotes the aggregate (`views/sync/interfaces.py:736`), so the target changes too | B. |
| 9 | VC owner unresolved | read the platform from `resolve_interface_row_device` | block the row, no fallback | that helper returns the page device on failure by default (`utils.py:1133`) | B. Call with `return_device_on_failure=False`. |
| 10 | where the gate lives | in the view loops | inside `update_interface_from_port` and `resolve_or_create_interface_from_port`, which require `rules` | a caller cannot bypass a gate in the shared writer | B. |
| 11 | load once | threaded by callers | cached on the request (`interface_rules_for_request`), `load()` for a job | the sync POST renders its response via a second view (`views/sync/interfaces.py:321`); `_remember_interface_name_per_platform` already caches on the request (`utils.py:1942`) | B. |
| 12 | invalid regex that bypassed `clean()` | not handled | `load()` raises; the request shows a configuration error | fail fast | B. |
| 13 | row diff | unchanged | new `ignored` / `ambiguous` states | ignored and ambiguous rows render with no actions, so the diff must know | B. |
| 14 | stale off-page selections | server gate refuses them with a skip message | JS prunes them using eligibility metadata | the server gate already refuses every ignored port | A. Round 1 attacks this. |
| 15 | NetBox-only delete revalidation | not in scope | revalidate presence on delete | a stale selection deleting an interface is not specific to Ignore | A. Out of scope. |
| 16 | AST guard | none | a test that no module outside the matcher selects mappings at run time | it keeps the one-definition rule mechanical | B. |
| 17 | IP assignment to an existing interface | allowed (the IP is another object) | blocked | a product decision about what Ignore means | **Operator.** |
| 18 | cable sync between existing interfaces | allowed | blocked | as 17 | **Operator.** |
| 19 | module port binding (`_bind_interface_librenms_id`) | missed | gated; needs port selectors carried into module enrichment | the binder writes `librenms_id` and `module_id` (`views/sync/modules.py:734`) | Split candidate, **operator.** |
| 20 | files | about 14 new | 51 touched, 93 total | the difference is almost all rows 17 to 19 plus JS pruning | Follows 14 and 17 to 19. |

Agreed by both (not re-litigated): the lexicographic rank (platform, pattern, type, speed); ties
never broken by id or order; Ignore evaluated first; `re.search` on `ifName` or `ifDescr`, and a
missing name cannot match; four partial unique constraints, not `nulls_distinct`; the owner's
platform, never a VM's host; presence computed before rows are hidden; filtering before
pagination; no checkbox on ignored rows; delete `select_interface_type_mapping`,
`get_netbox_interface_type` and the table cache.

Operator dispositions (2026-09-23): rows 17 and 18 go to **B** (Ignore blocks IP assignment and
cable sync on the port). Row 19 (module port binding) is **deferred** to a follow-up. Row 1: no
`enabled` field.

## 4. Merged design and ratify rounds

### r1 (merged candidate)

**Meaning.** An Ignore rule makes a LibreNMS port inert for the plugin's port-driven writes. The
plugin does not create, update, bind, relink or promote the port's interface. It does not assign
an IP to that interface, and it does not create, tag or replace a cable on it. It deletes nothing.
Ambiguous Set rules block the same writes for that port. Module port binding is out of scope
(follow-up).

**Model** (`InterfaceTypeMapping`, migration `0022`, irreversible):

- `action` (`set_type` / `ignore`, default `set_type`); `platform` FK to `dcim.Platform`,
  `null=True`, `on_delete=CASCADE`; `name_pattern` (`""` = any); `librenms_type` now `blank`
  (`""` = any); `netbox_type` now `null=True` (NULL on Ignore); `librenms_speed` unchanged.
- `clean()`: strip `librenms_type`; keep `name_pattern` verbatim; compile it with
  `validate_regex_field()`; at least one of platform / type / pattern; Set type requires
  `netbox_type`, Ignore requires NULL; speed requires a type and is rejected on Ignore; readable
  uniqueness error.
- Unique over `(platform, librenms_type, name_pattern, librenms_speed)`, action excluded, as four
  partial `UniqueConstraint`s. Check constraints: action/type pairing, at least one selector.

**Matching** (`interface_rules.py`):

- `InterfaceRuleMatcher.load()`: one query, compiled patterns; raises `RuleConfigurationError`
  on a pattern that does not compile.
- `matcher.decide(port, *, platform_id) -> RuleDecision(kind, netbox_type, rules)`, `kind` in
  `UNMAPPED`, `SET_TYPE`, `IGNORE`, `AMBIGUOUS`. No DB access, no side effects.
- `interface_rules_for_request(request)` caches one matcher on the request. A job calls `load()`.
- Selectors are AND. The pattern is `re.search` on `ifName` or `ifDescr`, raw values; a
  non-`str` name cannot match. Type is exact equality with `ifType`. Speed is a minimum in Kbps via
  `convert_speed_to_kbps`.
- Any matching Ignore gives `IGNORE` (all matches listed). Otherwise the top Set rank by
  `(has_platform, has_pattern, has_type, has_speed, speed)` wins. Two at the top rank give
  `AMBIGUOUS`, even when their outputs agree.
- Platform: the owner of the interface. A standalone device, the VM itself, or the VC member from
  `resolve_interface_row_device(..., return_device_on_failure=False)`. An unresolved VC owner
  blocks the row. An OOB row reads its target device. A cable far end reads the remote device.

**Gate.** `update_interface_from_port` and `resolve_or_create_interface_from_port` take a required
`rules` argument and raise `PortSyncBlocked(decision)` before any write. The callers turn it into
their per-row skip or error result. Deleted: `utils.select_interface_type_mapping`,
`interface_sync.get_netbox_interface_type` and its view wrapper, and the table's
`_interface_type_mapping_cache` / `get_interface_mapping`. An AST test fails if code outside
`interface_rules.py`, the model, forms, filters, serializers, tables/mappings and migrations
queries `InterfaceTypeMapping`.

**Consumers.**

| path | for an `IGNORE` or `AMBIGUOUS` port |
|---|---|
| interfaces tab render | Every port is enriched first, so a bound ignored port still counts as matched and never makes its interface NetBox-only. Name decisions keep ignored ports as input. Then rows are filtered before pagination. `interface_diff` gets `ignored` / `ambiguous` states with no actions. Ambiguous rows stay visible and name the rules. |
| bulk and single-row sync POST | Skip the row with a reason naming the rules. Column exclusions do not bypass it. |
| auto-select of related ports | Never adds an ignored port and does not walk through one. |
| bulk and single LAG / parent / bridge edges | Skip the edge when either end is ignored or ambiguous. The existing link stays. |
| single-row verify (VC member switch) | Re-evaluates with the new owner's platform. |
| Rebind | Refuses an ignored or ambiguous port. |
| IP tab | Create: refused. Assign or reassign to an existing interface bound to, or matched from, an ignored port: skipped with the reason, including force and primary-IP requests. Existing IPs stay. |
| cables tab | Create, tag or replace a cable where the local or the remote port is ignored or ambiguous: skipped with the reason. The far-end create is not offered and its POST is refused. Existing cables stay. |
| module port binding | Out of scope, follow-up. |

**Table UI.** `interfaces_show_ignored=1` includes ignored rows, greyed, with the rule, no
checkbox and no actions. The "N ignored" count covers the whole snapshot. The paginator links carry
the flag. The toggle resets to page 1. Off-page selections are not pruned in JS: the server gate
refuses them and reports the skip.

**Mapping UI and API.** The form, import (platform by slug), filter set and form, list table,
detail template, YAML export and serializer gain `action`, `platform` and `name_pattern`, with
`select_related("platform")`. The docs page and the contrib YAML examples are updated.

**Tests** (real DB, real requests, no mocks): model and constraint cases; `decide()` matrix
including the legacy speed matrix (condition 4); tab GET hide/toggle/count on P and Q devices
(condition 1); forged bulk and single POST (condition 2); name/platform Set GET+POST agreement
(condition 3); tie, both orders (condition 5); IP create/assign skip; cable create/replace skip;
edge skip both directions; one mapping query per request; the AST guard; an e2e toggle check.

### Round 1 (codex `gpt-6-astra` xhigh, read-only, HEAD `4345c07671`): NOT RATIFIED

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 1 | BLOCKER | `sync_interface` runs its own resolvers (`get_or_create`) before the gated updater, so the gate fires after creation | confirmed: `views/sync/interfaces.py:1479` (device), `:1519` (VM), updater at `:1547` | accepted, r2-1 |
| 2 | BLOCKER | cable rows carry only IDs and names; IP port record can be `None` | confirmed: `cables_view.py:867` row keys, `:975` reduces records to id + names; `ip_addresses_view.py:372` | accepted, r2-2 |
| 3 | MAJOR | a forced cable replacement deletes a cable whose other end is an ignored port | `cables.py:591` deletes every cable in `to_remove` | accepted, r2-3 |
| 4 | MAJOR | LAG/parent promotion overwrites a winning Set type | `_promote_lag_aggregate` / `_promote_parent_child` persist outside the attribute writer (`interfaces.py:2011`, `:2033`) | accepted, r2-4 |
| 5 | MINOR | AST allowlist misses mapping CRUD and API views | 9 queries in `views/mapping_views.py`, 1 in `api/views.py` | accepted, r2-5 |

Reviewer checks that passed: filtering before pagination is compatible (`interfaces_view.py:808`,
`:895`, `:913`); 1,024 pure-Python comparisons show the rank tuple matches the legacy speed
selector; the four partial constraints are sound; the module-binding deferral does not affect the
core's correctness or authorization; the file projection is 93 including this record.

### r2 (changes from r1, verbatim)

- **r2-1.** The gate is a rule, not two call sites: *every port-driven write path evaluates the
  decision after it resolves the owner and before its first write.* Paths: `sync_interface` (before
  `_resolve_device_interface` / `_resolve_vm_interface`), `resolve_or_create_interface_from_port`,
  `update_interface_from_port` (a second line of defence), Rebind (`interfaces.py:1763`), bulk and
  single relationship persistence (`:2102`), cable far-end create (`cables.py:1384`), cable apply
  (`cables.py:274`), IP assign (`ip_addresses.py:1141`).
- **r2-2.** Evidence and ambiguity scope.
  - `AMBIGUOUS` blocks only interface writes (create, update, promote, far-end create, IP-driven
    create). It does not block an IP assignment or a cable on an existing interface: the type is
    not written there.
  - New `matcher.may_ignore(platform_id) -> bool`: true when any Ignore rule is global or scoped
    to that platform.
  - A consumer without the full port record (`ifName`, `ifDescr`, `ifType`, `ifSpeed`) refuses
    the write with a "refresh" reason when `may_ignore` is true, and proceeds when it is false.
    Missing evidence never becomes `UNMAPPED` when an Ignore rule could apply.
  - The cables tab keeps `ifType` and `ifSpeed` next to the names in the local and remote port
    records it caches (`cables_view.py:975` and the local port map), and the rows and replay
    whitelist (`cables_view.py:316`) carry them.
  - The IP tab already has full records. A `None` record falls under the refusal rule.
- **r2-3.** Before a confirmed cable replacement deletes a cable, it checks each far termination
  of every cable in `to_remove` that is an interface bound to a LibreNMS port on this server. The
  port record is fetched by ID on this confirmed path, before the transaction. Ignored: the
  replacement is refused. Fetch failure and `may_ignore(owner platform)`: refused. An unbound
  interface is not a LibreNMS port, so no rule applies.
- **r2-4.** A promotion never overrides a `SET_TYPE` decision. If the winning type is not
  compatible with the edge (an aggregate not `lag`, a child the parent pass would promote), the
  edge is refused with a reason naming the rule. `UNMAPPED` ends keep today's promotion.
- **r2-5.** The AST guard allows `views/mapping_views.py` and `api/views.py` as management
  consumers and still forbids run-time selection anywhere else.
- **r2-6** (from the reviewer's notes). Nullable optional fields also get `blank=True`. The
  matcher's one speed reader replaces the table's `ifSpeed` default of 0 and the writer's None, so
  the two readers can no longer disagree. The browser's related-row traversal
  (`librenms_sync.js:1275`) skips rows marked ignored.

### Round 2 (codex `gpt-6-astra` xhigh, read-only): NOT RATIFIED

Round-1 findings: 1 CLOSED, 2 NOT CLOSED, 3 NOT CLOSED, 4 CLOSED (original overwrite), 5 CLOSED.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 2.1 | MAJOR | `may_ignore=False` lets an interface write go ahead on partial evidence, which can hide a Set tie (a type selector fails to match on a missing `ifType`) | follows from the r2-2 rule; the far-end create falls back to `other` today (`cables.py:1289`, `:1304`) | accepted, r3-1 |
| 2.2 | MAJOR | a manual cable pick targets an interface bound to another port, but the evidence stays the advertised port's | `cables_view.py:3553` stores only `manual_remote_id`; `:2531` keeps the advertised port identity | accepted, r3-2 |
| 2.3 | MAJOR | the displaced endpoints of a replacement are not locked; their binding or owner platform can change after the check | `_group_terminations` locks only the proposed pair (`cables.py:420`); the fingerprint has no binding or platform (`cables.py:73`) | accepted, r3-2 |
| 2.4 | MAJOR | refusing a promotion does not undo the attribute pass's type write, so an aggregate with members can lose `lag` | attribute pass runs first (`interfaces.py:219`); the updater writes type with no role check (`interface_sync.py:149`) | accepted, r3-3 |

The cable mechanism has now produced findings in both rounds (1.2, 1.3, 2.2, 2.3). r3 briefs the
general fix, not the instances, and round 3 asks for two verdicts.

### r3 (changes from r2, verbatim)

- **r3-1. Evidence rule by write kind.**
  - *Interface writes* (create, update, promote, IP-driven create, far-end create) need the
    complete record (`ifName`, `ifDescr`, `ifType`, `ifSpeed`) for the port. Without it they refuse
    with a "refresh" reason. `may_ignore` is never used to allow an interface write. The far-end
    create refuses when its port fetch returns nothing. Today it falls back to `other`.
  - *Writes on existing interfaces* (IP assign, cable create/tag/replace) can only be blocked by
    Ignore. Without the record they refuse when `may_ignore(platform)` is true and proceed when it
    is false.
- **r3-2. Cable evidence identity (general rule).** A cable write is allowed only when every
  LibreNMS port it touches is decided "not ignored". The touched ports are:
  - the row's local and remote port IDs;
  - the port bound (`librenms_id` on this server) to each NetBox termination the write attaches,
    including a manual pick;
  - the port bound to each far termination of every cable it removes.

  Records come from the cache or a fetch by ID before the transaction. Inside the transaction, the
  apply path locks every touched interface and its owner (`select_for_update`). It re-reads each
  binding and owner platform under that lock and decides with those values. When a bound port has
  no pre-fetched record, the r3-1 rule for existing interfaces applies. An interface with no
  binding on this server adds no port.
- **r3-3. A Set type never breaks an existing role.** Before `update_interface_from_port` writes a
  `SET_TYPE` result, it checks the interface's current role. An aggregate with members must stay
  `lag`. A child that has a parent keeps the plugin's promoted type. On a conflict it keeps the
  current type and reports "type kept: rule R conflicts with <role>", in the same way as the
  existing "kept its current name" message. Other fields still sync. Together with r2-4,
  promotion never overrides a Set type, and a Set type never breaks a relationship.

### Round 3 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED, B RATIFIED

Round-2 findings 2.1 to 2.4: all CLOSED.

**Verdict B: RATIFY r3-2.** Acceptance predicate: before a cable create, tag or replace, every
advertised, attached and displaced port is decided using the binding and owner platform read under
lock, with the r3-1 missing-evidence rule. First increment: the shared pre-write gate. DB tests
prove that an ignored manual or displaced endpoint stops the whole operation, including when a
binding or platform changes concurrently.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 3.1 | MAJOR | r3-3's keep-type branch lives only in the writer. The table's diff still compares the requested type, so it offers a Sync that can never clear the difference | `_type_differs` compares `context.netbox_type` directly (`interface_diff.py:157`); the table feeds the requested type (`tables/interfaces.py:254`) | accepted, r4-1 |

The reviewer states that NetBox's own `Interface.clean()` LAG/parent rules are unverified here.
r3-3 does not depend on them: it protects the plugin's own role convention.

### r4 (changes from r3, verbatim)

- **r4-1. One planned-type function.** `interface_rules.planned_type(decision, role) ->
  PlannedType(value, kept_reason)` decides the type a sync writes. `role` is
  `InterfaceRole(current_type, has_members, has_parent)`. The writer (`update_interface_from_port`)
  and the row diff (`interface_diff`) both call it. The diff compares `value` with the current
  type. A `kept_reason` renders as a note on the Type cell ("type kept: rule R conflicts with
  <role>"). It is not a difference, so it does not make the row differ or offer Sync. Other fields
  keep their own verdicts.
  - Role inputs, table: `has_members` comes from the lookup maps the tab already builds. Every
    member's interfaces are loaded, and a LAG never spans devices outside a VC, so it is the set of
    loaded `lag_id`s. `has_parent` comes from the loaded `parent_id`. No extra query per row.
  - Role inputs, writer: read from the interface under the existing lock.
  - Tests: GET, then POST, then GET for an aggregate with members and for a promoted child. After
    the POST the row shows no difference and the note.

### Round 4 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED

3.1 NOT CLOSED. Both new findings belong to the role-preservation mechanism (r3-3, r4-1), which
also produced 3.1.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 4.1 | MAJOR | `has_members` derived from loaded `lag_id`s is incomplete: a member left on a device removed from the VC, and the verify repaint's bounded candidate index (`interface_relationships.py:392`) | the verify path builds a bounded index, not the full tab maps (`views/object_sync/devices.py:299`) | accepted, r5-1 |
| 4.2 | MAJOR | `InterfaceRole` cannot express "is a LAG member"; NetBox rejects `virtual` with a LAG (upstream `device_components.py`, 4.4.0 L856, 4.6.5 L1002) and the writer saves without validation (`interface_sync.py:197`) | writer save path confirmed; NetBox source cited by the reviewer from upstream | accepted, r5-1 |

**Operator decision (2026-09-23):** NetBox's own `Interface.clean()` decides whether a planned type
fits the interface's existing links. The plugin keeps no copy of NetBox's type rules.

### r5 (changes from r4, verbatim)

- **r5-1. `planned_type` asks NetBox.** Replaces r3-3's role model and r4-1's `InterfaceRole`.
  `interface_rules.planned_type(decision, interface) -> PlannedType(value, kept_reason)`:
  - No interface (a create), or a decision that is not `SET_TYPE`: today's behaviour (the rule's
    type; an unmapped create gets `other`; an unmapped existing interface keeps its type).
  - `SET_TYPE` equal to the current type: `value` is the current type.
  - `SET_TYPE` different from the current type: validate an in-memory copy with the new type
    through the plugin's existing guarded `clean()` path (the NetBox 4.4.0 parent-chassis
    tolerance, `utils.netbox_clean_reads_parent_virtual_chassis`, `views/sync/interfaces.py:2071`).
    If it passes: `value` is the new type. If it fails, the copy is validated again with the
    current type. Only errors that the new type adds count. Then `value` is the current type and
    `kept_reason` is NetBox's message. Errors that exist with the current type too do not block
    the type.
  - It reads the persisted relationships through NetBox, so the result does not depend on page
    scope or on the verify repaint's candidate set. The writer, the full-tab diff, the verify
    repaint and the single-row sync response all call it. The writer calls it under the existing
    row lock.
  - Cost: `clean()` runs only for rows with an existing interface whose Set type differs from the
    current type. In a steady state there are few such rows.
- **r5-2. Promotion (replaces r2-4).** A relationship pass never changes an end whose decision is
  `SET_TYPE`. It links without promotion when NetBox's `clean()` accepts the link with that type,
  and refuses the edge with NetBox's reason when it does not. `UNMAPPED` ends keep today's
  promotion.
- r3-3 and r4-1 are withdrawn. 3.1, 4.1 and 4.2 are to be judged against r5-1.

### Round 5 (codex `gpt-6-astra` xhigh, read-only): A NOT RATIFIED — blocker cap reached

3.1 CLOSED, 4.1 CLOSED (withdrawn model), 4.2 NOT CLOSED (5.1). r5-2 promotion coverage: no
omission (four sites confirmed by AST).

| # | sev | finding | status |
|---|---|---|---|
| 5.1 | MAJOR | NetBox's `clean()` stops at its first error. An unrelated bridge error found first hides the virtual-with-LAG error, so comparing the errors of the two copies lets an invalid type through (NetBox 4.4.0 L837-858, 4.6.5 L983-1004) | open |
| 5.2 | MAJOR | the 4.4.0 guard re-raises `AttributeError` when the chassis no longer matches (`views/sync/interfaces.py:2078`), so a table render can fail | open |
| 5.3 | MAJOR | the attribute writer locks owners, not the interface row (`:1039`, `:1238`, resolver `:1445`); a concurrent LAG assignment is overwritten by a stale save (`interface_sync.py:198`). The gap exists today | open |

Clustering: 2.4, 3.1, 4.1, 4.2, 5.1, 5.2 and 5.3 all sit in one mechanism: **checking an
attribute-pass type write against the interface's existing links**. Today's writer has the same
gap for legacy `ifType` mappings: it writes a mapped type with no check against the links.

**Proposed split line (operator decision pending):**

- *Core:* everything ratified or not contested, plus a minimal promotion rule. The relationship
  pass does not promote an end whose decision is `SET_TYPE`. It tries the link, and the `clean()`
  that the relationship pass already runs today (`views/sync/interfaces.py:2019`, `:2071`) accepts
  or refuses it. The attribute pass writes the planned type as today's writer does. No new
  type-vs-links check.
- *Deferred mechanism:* validating an attribute-pass type write against existing links, for
  legacy and new rules alike, including the interface row lock (5.3). This goes to a follow-up
  issue with findings 2.4 to 5.3 as its evidence.
- Standalone check: the core's acceptance conditions 1 to 5 do not need the deferred part. The
  core leaves today's gap as it is; it does not add a new kind of write.

**Operator decision (2026-09-23): split.** The core goes to one counted verdict round. A blocker
there reports the design blocked. The design does not split again.

### Core (split scope, for the verdict round)

The core is r1 with these changes: r2-1, r2-2 as narrowed by r3-1, r2-5, r2-6, r3-1, and r3-2
(already RATIFIED as verdict B). r2-3 is replaced by r3-2. r2-4, r3-3, r4-1 and r5-1 are
withdrawn. r5-2 is replaced by this rule:

- **Core promotion rule.** The relationship pass (bulk LAG `:755`, bulk parent `:760`, single LAG
  `:2527`, single parent `:2548`) never promotes an end whose decision is `SET_TYPE`. It tries the
  link with that end's current type. The `clean()` it already runs accepts or refuses the link, and
  a refusal is reported with NetBox's reason. The exclusion and permission checks tied to promotion
  apply only when a promotion is actually planned. `UNMAPPED` ends keep today's promotion.
- **Attribute pass.** A `SET_TYPE` result is written as today's writer writes a mapped type: no
  check against existing links, no interface row lock. That is today's behaviour for legacy
  mappings, and it is the deferred mechanism.
- **Diff.** `interface_diff` compares the decision's type (the value the writer writes) with the
  current type. Since the writer applies it unconditionally, the two cannot disagree.

**Deferred (follow-up issue, filed after the core is implemented):** validate an attribute-pass
type write against the interface's existing links, for legacy and new rules alike, with the
interface row lock. Evidence: findings 2.4, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3. Module port binding is
a second follow-up.

### Core verdict round (codex `gpt-6-astra` xhigh, read-only): RATIFY core

No findings. The core stands alone. The promotion rule covers all four sites (AST). Acceptance
conditions 3 and 5 stay consistent. No finding outside the deferred cluster reopens. 7,168
in-memory cases agree with the legacy selector.

Ratified scope: r1 + r2-1, r2-2 as narrowed by r3-1, r2-5, r2-6, r3-1, r3-2, and the core
promotion, attribute-pass and diff rules. r2-3 is superseded. r2-4, r3-3, r4-1 and r5-1 are
withdrawn. r5-2 is replaced.

## 5. Next action

1. **Increment 1:** the model extension and migration `0022`, `interface_rules.py` (matcher,
   `RuleDecision`, `may_ignore`, request cache), mapping form, filter, table, serializer and
   import/export. Acceptance: legacy selections unchanged; platform and raw-name matching; Ignore
   precedence; explicit equal-rank ambiguity; invalid rules rejected; one rule query per request
   and none per decision.
   *Implemented (uncommitted), codex implementation review CLEAN after 4 rounds.* Decisions made
   during review:
   - A CSV import that has a `name_pattern` column is refused as a whole. NetBox's `parse_csv`
     strips every cell, so whitespace in a regex cannot survive CSV. Re-reading the raw CSV was
     tried and dropped: a second parser kept diverging from NetBox's (trailing space, sniffed
     `skipinitialspace`, normalized headers). YAML, JSON, the form and the API keep patterns
     verbatim.
   - `utils.REGEX_COMPILE_ERRORS = (re.error, OverflowError)`; a pattern such as `a{4294967295}`
     is a validation error, not a crash.
   - A background-job CSV *upload* fails upstream in NetBox 4.4-4.6 for every model (the file is
     read before the job replays it). Out of scope.
2. **Increment 2:** interfaces tab and its writers (gate before the resolvers, hide + toggle,
   auto-select, relationship promotion rule, Rebind, verify repaint). Acceptance conditions 1 to 5.
   *Implementation finding (increment 2):* the core promotion rule assumed the relationship pass's
   `clean()` refuses a LAG link to a non-`lag` aggregate. NetBox 4.4.0 and 4.7 do not check the
   aggregate's type, and migration 0020 seeds `ieee8023adLag -> lag`, so every aggregate decides
   `SET_TYPE(lag)`. Applied literally, no aggregate would ever be promoted. **Revised rule:** a
   promotion runs only when its target type equals the end's own decision type. When an end's
   decision is `SET_TYPE` with a different type, the edge is refused and the existing link stays.
   `UNMAPPED` ends keep today's promotion. "Promotion never overrides a Set type" still holds.
   *Operator decision (2026-09-24), from increment-2 review round 4:* when a stored VC-member
   choice for a rendered row differs from the member the page renders, the browser does not
   restore it. It drops that stored selection and shows a notice. The async "pending restore" was
   removed: its lifecycle produced findings in three rounds in a row (dependency selection,
   deselect-all, htmx swap, navigation). Off-page selections still submit with their stored member,
   and the server decides.
3. **Increment 3:** IP tab and cables tab gates (r3-1, r3-2).
   *Implemented (uncommitted).* Decisions made during implementation:
   - One check for writes on existing interfaces: `matcher.check_existing_interface_write`
     returns the blocking IGNORE or INCOMPLETE decision, or None. The IP writer, the IP table, the
     cable gate and the cable table all call it.
   - IP: the assignment touches the IP row's source port and the port bound to the target
     interface, re-read from the locked row (a name match can reach an interface bound to another
     port). Both are decided with the platform of the owner that `_lock_target_interface` locked.
     The IP table uses the same port list (`ip_assignment_ports`) with the snapshot records. Both
     resolve rows against one scope (`ip_interface_scope`): the object and the chassis members
     the caller may view, and their interfaces the caller may view. The sync locks the same
     owners, so the table and the sync agree, and neither reaches a hidden member.
   - The IP snapshot keeps `bound_ports_by_id`: the records of the ports bound to the interfaces
     in scope, read from the same device-ports payload. They are evidence for the rules only and
     never become IP rows or name candidates (`ports_by_id` is unchanged).
   - One disclosure rule for every refusal and pill that can name a port (`utils.PortDisclosure`,
     used by the cable table and writer and the IP table and writer): a port's id, name and rule
     label are shown only when its owner is in the caller's view scope. For a port bound on this
     server, the owner is the bound interface's own device or VM, read from the binding, and the
     interface must be visible too; the owner a caller passes counts only for an unbound port.
     An owner that did not resolve is not visible, an ambiguous binding is not visible, and no
     port is visible by default, the row's own ports included. A port that is not visible still
     blocks, with `HIDDEN_PORT_REASON`, which names no port, name or rule.
   - A table preloads the rule once for every port it may ask about (one binding query per
     interface model, one view-scope query per model), so a render costs the same queries for
     2 or for 20 blocking rows. A writer asks one row at a time through the same rule.
   - Cables: the snapshot keeps `local_port_record` (from the device ports payload) and
     `remote_port_record` (from the neighbour's port index, now cached under a new key so an entry
     without records is never read). The row's remote port is `remote_port_key`, and also
     `remote_port_id` when it differs. Each port is decided with its own owner's platform
     (`port_owner_id`): the local port with the local owner, and a bound port with its
     interface's device. The advertised remote port uses the resolved neighbour (its chassis
     member for a VC); a manual pick onto another device never changes that. The gate locks these
     owners with the others.
   - *Operator decision (2026-09-24):* when the advertised port's owner does not resolve, the port
     is decided with no platform (`platform_id=None`), so only global rules match it, as for a
     device with no platform (r1). A manual pick does not change this; the picked interface's own
     bound port is decided separately with the picked device's platform. A missing record follows
     r3-1 with no platform, so only a global Ignore rule refuses it.
   - The far ends of the cables on both endpoints are candidate evidence before the lock. Their
     interfaces and owners are locked in the same statements as the endpoints and owners, so the
     lock order does not change. A removed cable that ends on an interface the lock did not take
     makes the row stale.
   - Records are fetched by id before the row's transaction, and only when the snapshot has an
     Ignore rule. The table decides only the ports whose records the snapshot holds (a render
     makes no LibreNMS call); the sync gate decides every touched port. A blocked row loses Sync
     Cable and the far-end create, and shows the rule.
   *Known limitation (increment-3 review round 5):* `PortDisclosure` finds bindings through
   `build_librenms_id_qs`, which matches only ASCII-digit ids. A row whose stored `librenms_id`
   is a reader-only form (for example `"7_101"`, accepted by `get_librenms_device_id`) is not
   found, so its port counts as unbound and its refusal can be named. Every guard built on
   `find_by_librenms_id` shares this split. It is deliberate (narrowing the reader wipes mappings);
   the only lossless fix is a heal path that normalises such values. That is an open follow-up.
4. After the core is implemented: file follow-ups for type-vs-links validation (findings 2.4 to
   5.3) and module port binding.

## 0. Refuted claims

None. Every reviewer finding was confirmed and accepted. Withdrawn design statements are
listed in the ratified scope above.
