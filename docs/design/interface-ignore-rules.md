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

## 6. Follow-up: interface type vs existing links (issue #185)

### Brief

**Decision.** How an interface sync decides whether the type it plans to write fits the
interface's persisted links, the same way for the row diff and for the writer, for legacy `ifType`
mappings and Set type rules alike. The writer locks the interface row and plans with the locked
state.

**Problem as a class.** The attribute writer (`update_interface_from_port`,
`interface_sync.py:86`) writes `rules.decide_interface_write(...).netbox_type` with no check against
the interface's links, then calls a full `save()` (`:198`) outside any interface row lock. The row
diff (`interface_diff._type_differs`, `:171`) compares the same decision type. Three failures follow:
a non-`lag` type on an aggregate with members; a type NetBox's own `clean()` rejects (for example
`virtual` on a LAG member) stored without validation; and a stale full save overwrites a concurrent
link write (lost update). The module that owns the rule is "the type a sync writes to an existing
interface", which today has no owner: the decision type flows straight into both the writer and the
diff.

**Operator decisions in force.** The plugin keeps no copy of NetBox's type rules (2026-09-23). Ignore
and Set type semantics, and the core promotion rule (section 4), are ratified and not open.

**Constraints.**

- Supported NetBox: `min_version = "4.4.0"`; the dev container runs 4.7.0. NetBox's
  `Interface.clean()` type rules differ by version (evidence below).
- The diff, the verify repaint (`views/object_sync/devices.py`) and the single-row sync response
  must reach the same verdict as the writer. The verify repaint builds a bounded candidate index,
  not the full tab maps (finding 4.1).
- A table render must not raise (finding 5.2). No per-row query in the steady state.
- No broad exception swallowing; fail closed on unexpected state.
- PR #180 is at 97/100 changed files. The main files are already in its diff.

**Observable acceptance conditions.**

1. An aggregate with at least one LAG member, whose planned type is not `lag`, keeps `lag` after a
   sync. The row shows why and does not show a type difference. Its members keep `lag_id`.
2. A LAG member whose planned type is `virtual` keeps its type after a sync; the stored pair is one
   NetBox's `clean()` accepts. Same for a child with a parent whose planned type is not `virtual`.
3. The verdict is the same for a legacy mapping and a Set type rule with the same type.
4. The table (GET), the sync (POST) and the table again (GET) agree for cases 1 and 2, including the
   verify repaint.
5. A concurrent link write between the table load and the sync is not lost, and the sync plans with
   the link it sees under the lock.
6. An interface with no links, or a create, behaves as today.
7. A table render on NetBox 4.4.0 with a cross-chassis parent does not raise.

**Evidence.**

- NetBox 4.7.0 `Interface.clean()` (`dcim/models/device_components.py`) and
  `InterfaceValidationMixin.clean()` (`dcim/models/mixins.py:142`): virtual types with a cable or
  `mark_connected`; a parent only on a `virtual` or channel subinterface; `virtual` with a LAG;
  channel rules; wireless-only fields. NetBox 4.4.0 L842-865: the same cable rules, "Only virtual
  interfaces may be assigned to a parent interface", and the parent-chassis dereference bug at L875.
  `clean()` raises on the first error only.
- The `lag` FK has no `limit_choices_to` in 4.7.0: no NetBox model rule ties an aggregate's type to
  its members. The relationship pass promotes the aggregate itself (`views/sync/interfaces.py:2210`).
- Writer call sites: `SyncInterfacesView.update_interface_attributes` (`views/sync/interfaces.py:1722`),
  `resolve_or_create_interface_from_port` (`interface_sync.py:289`). Owner locks, not interface
  locks: `views/sync/interfaces.py:1371`, `:2095`, `:2104`.
- The guarded `clean()` for the 4.4.0 bug: `_validate_relationship` (`views/sync/interfaces.py:2275`),
  `utils.netbox_clean_reads_parent_virtual_chassis`.
- Prior findings 2.4, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3 (section 4), and the two rejected shapes: a
  plugin-side role model (4.1, 4.2), and comparing `clean()` errors of a new-type copy against a
  current-type copy (5.1).

**Candidate shapes.**

- **S1, NetBox judges one copy, fail closed.** Validate an in-memory copy with the planned type
  through the guarded `clean()`. Any error keeps the current type, with NetBox's message. Add the
  plugin's own "aggregate with members stays `lag`" rule, which is not a NetBox rule.
- **S2, plugin-owned link facts and rules.** Read link facts from the DB and apply a plugin rule
  table. Needs the operator decision above to be reopened.
- S3 (the second designer's).

### 6A. Claude design (Opus 5.5, from the brief; drafted before reading 6B)

**Owner.** `interface_diff.py` already owns "what the writer writes" (the shared field schema,
`synced_description`, `interface_enabled_from_port`) and the writer imports it. It gains one function:

    planned_interface_type(interface, decision) -> PlannedType(value: str | None, kept_reason: str | None)

- `interface` is a persisted `Interface` (a `VMInterface` has no type: returns `(None, None)`, no queries).
- `decision.netbox_type is None` (UNMAPPED): today's rule. Current type, or `other` when empty. No check.
- `decision.netbox_type == interface.type`: `(interface.type, None)`. No query.
- Otherwise, in this order, and the first refusal wins:
  1. **Plugin rule (not a NetBox rule):** `Interface.objects.filter(lag_id=interface.pk).exists()`
     and the planned type is not `lag` -> kept, "type kept: the interface has LAG members; <rule> sets <T>".
     NetBox has no model rule for this (the `lag` FK has no `limit_choices_to`), so it does not
     conflict with "no copy of NetBox's type rules".
  2. **NetBox judges one copy:** `copy.copy(interface)`, set `type = T`, run the guarded clean
     (below). A `ValidationError` -> kept, "type kept: NetBox refuses <T>: <first message>".
  3. Else `(T, None)`.
- **No comparison with a current-type copy.** Any `clean()` error keeps the current type. This closes
  5.1 by construction: an error that hides the type conflict still keeps the type. The price is that
  an unrelated pre-existing NetBox error on the interface also holds a type change. The reason shows
  NetBox's message, so the operator sees what to fix. Fail closed, as the brief requires.
- It reads persisted links through NetBox and one `exists()`, never the page maps, so it does not
  depend on page scope or the verify repaint's candidate index (closes 4.1). It needs no role model
  (closes 4.2: NetBox's own `virtual`-with-LAG and parent rules run on the copy, per version).

**Guarded clean, shared.** `_validate_relationship`'s 4.4.0 tolerance moves to
`utils.netbox_interface_clean(interface)`, used by the relationship pass and by the planner. Fix for
5.2: when the `AttributeError` is the known 4.4.0 `virtual_chassis` dereference, the answer is known.
Same chassis: rerun without the parent (today). Different or no chassis: raise the `ValidationError`
NetBox meant ("belongs to <device>, which is not part of virtual chassis <vc>"). Any other
`AttributeError` still propagates (a real defect, fail fast). So a table render never raises for the
4.4.0 bug.

**Writer (`update_interface_from_port`).** For an existing interface (`pk` set):

1. Require an open transaction (`transaction.get_connection().in_atomic_block`, else raise).
   Callers already lock the owner device or VM first; the interface row is locked after it, which
   keeps the owner -> interface order the relationship pass uses.
2. `type(interface).objects.select_for_update(of=("self",)).filter(pk=interface.pk)` and
   `interface.refresh_from_db()`, so the plan reads the locked links (closes 5.3's stale read).
3. `planned_interface_type(interface, decision)` sets the type. A kept reason is logged at info.
4. Save with `update_fields` = the tracked fields that changed plus `custom_field_data`, not a full
   `save()`. A concurrent write to `lag`, `parent`, `bridge` or any untracked column is never
   overwritten (closes the lost update, for every field, not only type).

A create (no `pk`) has no links: today's behaviour.

**Diff.** `compute_row_sync_state` calls `planned_interface_type` once per existing row and puts
`value` in `DiffContext.netbox_type`. `_type_differs` compares it. `RowSyncState` gains
`type_kept_reason`; the Type cell renders it as a note. A kept type is not a difference, so a row
whose only difference is a kept type is in sync and offers no Sync. The table, the verify repaint and
the single-row sync response all go through `compute_row_sync_state`, so they agree with the writer.

**Cost.** Queries only for a row whose decision type differs from its current type: one `exists()`
and the queries `clean()` makes (cable, lag, parent, bridge, untagged VLAN; 2 to 5). A kept row pays
this on every render. Kept rows are a misconfiguration signal and few; the cost is stated, not hidden.

**Validation.**

- DB tests on `planned_interface_type`: aggregate with members + non-`lag` (legacy mapping and Set
  rule, AC1, AC3); member + `virtual` (AC2); child with parent + `1000base-t` (AC2); unrelated
  pre-existing error holds the type (the 5.1 disposition); no links (AC6).
- End to end GET -> POST -> GET on the interface tab for AC1 and AC2, plus the verify repaint (AC4).
- Lost update (AC5): load the tab, set `lag_id` on the row in the DB, POST; `lag_id` survives and the
  type is planned with it. Mutation check: revert `update_fields` to a full save, the test goes red.
- 4.4.0 guard (AC7): version patched to 4.4.0 (the version read is the one mocked boundary), a
  cross-chassis parent on a real VC, table render -> kept note, no exception.

### 6B. Codex design (`gpt-6-astra`, effort high, read-only, blind)

Input: the brief above only (constraints, acceptance conditions, evidence), fresh context, told not to read "Candidate shapes". It reports reading sections 1-5 only. Caveat: S1's one-paragraph summary was in the file it could open, so independence is not guaranteed. Full text: kept in the session scratchpad; the load-bearing points are in the divergence table.

Summary: new module `interface_type_plan.py`; `load_type_evidence(interfaces)` batch-loads persisted facts, and a pure `plan_interface_type(decision, interface, evidence) -> TypePlan(value, status accepted|unchanged|kept|blocked, reason)` does no queries. A private materialized relation reader answers NetBox 4.7's channel `aggregate(Max)` from batched data. Members `Exists` rule; one copy through the real `clean()`; any error keeps the type; no two-copy comparison. 4.4.0 fault -> `kept` always, no retry. Writer reselects under a row lock, recomputes the decision, full-saves the fresh instance and returns `InterfaceWriteResult(interface, changed, type_plan)`. Batch paths lock every interface row in pk order before mutation. `blocked` for missing evidence or an unexpected validator `AttributeError`. Unlinked fast path. AST guard so no type write bypasses the module.

### 6C. Divergence table

| # | decision | 6A (Claude) | 6B (codex) | evidence | disposition | consequence |
|---|---|---|---|---|---|---|
| D1 | who reads the evidence | the planner queries per row, only when the decision type differs from the current type | batch `load_type_evidence`; pure planner with zero queries; a fake ORM reader for 4.7's channel `aggregate(Max)` | 4.7 `clean()` makes its own DB reads (`cable_terminations.exists()`, channel max); a zero-query planner has to answer them from fakes | **6A.** The fake relation reader re-implements a Django related manager to feed NetBox, and codex names it the highest-risk element. Its only gain is query count on refused rows. The cost of 6A is stated: 2-5 queries per row whose type differs, every render. | a query-count test pins 0 extra queries for rows whose type matches |
| D2 | module | `planned_interface_type` in `interface_diff.py` (already the shared writer/diff schema, imported by the writer) | new `interface_type_plan.py` | #180 is at 97/100 files; `interface_diff.py` is in the diff | **6A.** Same seam, no new file. | none at call sites |
| D3 | 4.4.0 parent-chassis fault | shared guard: same chassis, rerun without the parent; different chassis, raise NetBox's intended `ValidationError` | always `kept` ("this NetBox version could not validate"), no retry | 4.4.0 L864 (the virtual-only-parent rule) runs before the L868-880 device/chassis block, so the fault at L875 means L842-865 already passed; the rerun keeps the later LAG/bridge/wireless/VLAN checks | **6A.** The retry loses no type rule. Codex's version holds a correct type change on 4.4.0 forever. | AC7 test: same-VC child accepts a valid type; mismatched VC keeps with NetBox's message |
| D4 | writer freshness | lock the row, `refresh_from_db()` the passed instance, then decide and plan | reselect a new instance, full save it, return `InterfaceWriteResult` so callers switch objects | callers pass shared-index objects that the relationship pass reuses (`_apply_interface_relationship` docstring) | **6A.** Refreshing in place keeps one object per row, so the relationship pass sees fresh values too, with no new return type. Codex's point adopted: the decision is recomputed after the lock (owner platform is re-read). Under the row lock a full save is safe; `update_fields` is dropped from 6A. | mutation check: remove the refresh -> AC5 test red |
| D5 | lock order | per row, inside the writer, after the caller's owner locks | lock every interface row of the batch in pk order before any mutation | every plugin path that locks interfaces takes owner locks first (`views/sync/interfaces.py:1371`, `:2095`; `cables.py:632` then `:648`), so plugin writers on one device serialize at the owner | **6A, contested.** Round 1 must check every interface-locking path (including `migrate.py:520`, `:838`) for an owner-first order. | a counterexample path makes this a finding |
| D6 | unexpected validator `AttributeError` | propagates (defect, fail fast) | `blocked` row status | user rule: fail fast, no broad swallowing | **6A.** Only the known 4.4.0 fault is handled. | none |
| D7 | already-invalid interface (unrelated `clean()` error) | type change held, NetBox's message shown | unlinked fast path keeps today's write; linked ones held | AC6 wording "no links behaves as today" | **Operator decision (2026-09-24): hold the type (6A).** The fast path lists clean()'s inputs (cable, wireless, channel), which is close to a copy of NetBox's rule inputs. | AC6 reads: an interface whose new-type copy passes `clean()`, or a create, behaves as today |
| D8 | kept message on POST | info log; the swapped table shows the note | warning log plus a collected sync message | the htmx swap re-renders the row with its note | **6A**, log at warning. | none |
| D9 | AST guard | none | no attribute type write or type comparison bypasses the planner; ratified promotions allowed | user rule: prevent the class mechanically | **6B adopted.** | a new test in the existing AST guard file |
| D10 | tests for AC5 | sequential stale-object test | threaded, independent connections, barriers | `transaction=True` flushes seeded rows (known harness trap) | **both:** sequential test required; one threaded lock-wait test if the harness allows it | |

Both designs agree: members `exists()` as the one plugin rule; one copy through the real `clean()`;
any error keeps the type; no two-copy comparison; the diff compares the planned value; a kept type is
not a difference.

### r1 (merged candidate)

- **Planner.** `interface_diff.planned_interface_type(interface, decision) -> PlannedType(value,
  kept_reason)`, as in 6A. UNMAPPED, create, VMInterface and unchanged type: today's behaviour, no
  query. A changed type: (1) members `exists()` -> kept if not `lag`; (2) guarded `clean()` on
  `copy.copy(interface)` with the new type -> kept on `ValidationError`, reason carries NetBox's first
  message and the rule names; (3) else the new type.
- **Guard.** `utils.netbox_interface_clean(interface)`, shared with `_validate_relationship`, as D3.
- **Writer.** For an existing interface: require an atomic block; `select_for_update(of=("self",))`
  on the row after the caller's owner locks; `refresh_from_db()`; decide the rule with the re-read
  owner platform; plan; set fields; full `save()`. Kept reason logged at warning.
- **Diff.** `compute_row_sync_state` plans once per existing row; `_type_differs` compares
  `value`; `RowSyncState.type_kept_reason`; the Type cell renders the note. Table, verify repaint and
  single-row response share it.
- **AST guard** (D9).
- **D7 (operator, 2026-09-24):** an already-invalid interface holds the type change, with NetBox's message.
- **Tests:** 6A's list plus D10.

### Round 1 (codex `gpt-6-astra` high, read-only): r1 NOT RATIFIED

D3 held (the reviewer ran real 4.4.0 `clean()`: physical-parent rejection before the fault,
same-VC acceptance after the retry, virtual-with-LAG rejection on the retry). Writer callers are
atomic and owner-locked. Render parity has one seam. D1 stands.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 1.1 | MAJOR | D5 premise false: module type apply locks interfaces in `(name, pk)` order with no device lock, so it can deadlock against a per-row writer; module adoption and migrate reconciliation also lock interfaces before (or without) the owner | confirmed: `views/sync/modules.py:2518` (no Device lock in the view), `:483`; `views/sync/migrate.py:321` locks an interface that can sit on a third device | accepted, r2-1 |
| 1.2 | MAJOR | `refresh_from_db()` does not reset NetBox 4.7's `_original_name`, so a restored name skips the channel-child rename cascade | confirmed: `dcim/models/mixins.py:274` sets it in `__init__`; `save()` reads it at `:288` | accepted, r2-2 |
| 1.3 | MAJOR | module template type apply writes `type` directly (`full_clean(exclude=...)`), so an aggregate with members can lose `lag` | confirmed: `views/sync/modules.py:2391` | accepted, r2-3 |
| 1.4 | MAJOR | "create = no pk" is wrong: both resolvers persist a skeleton with `get_or_create` before the writer | confirmed: `views/sync/interfaces.py:1670`, `interface_sync.py:275` | accepted, r2-4 |
| 1.5 | MINOR | patching only the version does not run 4.4.0's faulty `clean()` | confirmed: CI matrix has a `v4.4.0` leg (`.github/workflows/test.yaml:29`) | accepted, r2-5 |

Stated assumption (from the review, not a finding): `copy.copy` shares related objects, prefetch
caches and `custom_field_data` with the original. Stock NetBox `clean()` only reads them.

### r2 (changes from r1, verbatim)

- **r2-1. Owner-first lock order is an invariant.** Every plugin transaction that locks interface
  rows first locks their owners (virtual chassis, then devices in pk order, or the VM). Fix the three
  sites: module type apply locks the target device first; module adoption locks `module.device`
  first; migrate reconciliation reads the interface's `device_id`, locks that device, then locks the
  interface and re-checks `device_id`. Mechanical guard: an AST test finds every `select_for_update`
  call in `netbox_librenms_plugin/` whose queryset is an interface model (or a device component
  manager); in each enclosing function an owner lock call must come first, or the site is in an
  explicit allowlist with a reason. A new unclassified site fails the test.
- **r2-2. The writer reselects, it does not refresh.** `update_interface_from_port` starts with
  `type(interface).objects.select_for_update(of=("self",)).get(pk=interface.pk)`, decides the rule
  with the locked owner platform, plans, writes and full-saves that fresh instance, and returns
  `InterfaceWriteResult(interface, changed)`. Both callers (`SyncInterfacesView.update_interface_attributes`
  and `resolve_or_create_interface_from_port`) continue with the returned instance (MAC, VLAN, the
  IP-driven create). It asserts an atomic block. D4 is reversed on this evidence.
- **r2-3. One type-change check, two users.** `interface_diff.type_change_refusal(interface, new_type)
  -> str | None` holds the members rule and the guarded `clean()` on a copy. `planned_interface_type`
  calls it, and `_apply_module_interface_type` calls it in place of its own `full_clean(exclude=...)`
  (a refusal returns `validation_failed` with the reason). The D9 AST guard allows exactly these type
  writers: the attribute writer via the planner, module apply via the check, the ratified
  promotions, and the relationship pass's restore callbacks.
- **r2-4. "No type yet" is the create case.** An interface with an empty `type` takes the planned
  type without the check. This covers the skeleton both resolvers create, with no provenance flag.
  A type-less row cannot be held at `""` (NetBox requires a type), and today's unmapped rule already
  treats "no type yet" as "fill".
- **r2-5. AC7 runs NetBox 4.4.0's real `clean()`** on CI's `v4.4.0` leg (skipped on other versions),
  and locally through the old-NetBox simulation. Cases: same VC accepts a valid type; different VC
  keeps the type with NetBox's intended message; a retry that then hits virtual-with-LAG keeps the
  type. Mutation check: remove the guard, the test goes red on 4.4.0.

### Round 2 (codex `gpt-6-astra` high, read-only): r2 NOT RATIFIED

1.2, 1.3, 1.5 CLOSED. 1.1 and 1.4 NOT CLOSED. The reviewer enumerated 52 `select_for_update` and 12
`relock_scoped_row` calls. Fresh-instance propagation: no stale write left if both callers use the
returned instance; relationship indexes are rebuilt after the attribute pass (`views/sync/interfaces.py:619`).

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 2.1 | MAJOR | r2-1's reconciliation fix locks a newly found third device while donor/winner are held, so a device-lock cycle with cable sync (`cables.py:632`) appears; `TransferDeviceIPView` (`migrate.py:960`) and `MergeNetBoxDevicesView` (`imports/actions.py:4012`) have the same lookup | accepted as a defect of r2-1, not of today's code | r3-1: out of core |
| 2.2 | MAJOR | a device lock inside module adoption conflicts with the bay/module locks its callers already hold and with NetBox's module-move order (device before bay) | accepted as a defect of r2-1 | r3-1: out of core |
| 2.3 | MAJOR | "an owner lock comes first in the function" is not a sound AST predicate (wrapper calls, lazy querysets, branches, wrong owner) | accepted | r3-1: out of core |
| 2.4 | MAJOR | replacing module apply's `full_clean(exclude=...)` with model `clean()` drops `type` field validation (choices) | confirmed: `full_clean` runs `clean_fields` on the non-excluded `type` | accepted, r3-2 |
| 2.5 | MAJOR | "empty type = create" is unsafe: a skeleton synced with Type excluded can gain a LAG in the relationship pass, and a later `virtual` fill would skip the check | accepted: the path is `views/sync/interfaces.py:1670` -> `interface_sync.py:139` -> `:770` | accepted, r3-3 |

**Clustering.** 1.1, 2.1, 2.2 and 2.3 sit in one mechanism: **plugin-wide lock order for interface
rows**. It predates #185. The relationship pass already takes owner locks and then one pk-ordered
interface batch (`interface_relationships.py:160`), while module type apply (`modules.py:2518`,
`(name, pk)`, no owner), module adoption (`:483`, no order), and the migrate/transfer/merge owner
lookups do not. Today's relationship pass is exposed to the same cycles.

### r3 (changes from r2, verbatim)

- **r3-1. Split: core A and mechanism B.** r2-1 is withdrawn.
  - *Core A (issue #185):* the writer locks its row with `select_for_update(of=("self",))` per row,
    after the owner locks every caller already holds (verified in round 1). That is the relationship
    pass's own discipline (owner first, then interface rows). Module type apply, which the core
    already changes (r2-3), locks the target device before its interfaces and orders its interface
    lock by pk. No other path changes its lock order in the core.
  - *Mechanism B (follow-up issue, filed after the core):* a plugin-wide interface-row lock order:
    module adoption vs. install/replace/move order (2.2), migrate/transfer/merge owner lookups
    constrained to the locked winner (2.1), and an inventory guard over every locking primitive with
    caller contracts and negative fixtures (2.3). Evidence: 1.1, 2.1, 2.2, 2.3.
  - Standalone check: the core adds no new kind of lock-order exposure. Its per-row locks sit under
    the owner lock, so plugin writers that take the owner lock serialize there. A path that skips the
    owner lock can already deadlock against the relationship pass today; PostgreSQL detects and
    aborts that. B removes that class.
- **r3-2. The check keeps field validation.** `type_change_refusal(interface, new_type)` runs, on the
  copy: `clean_fields(exclude=<every field except type>)`, then the members rule, then the guarded
  `clean()`. The first failure is the reason.
- **r3-3. Creation provenance, not empty type.** Both resolvers already have `get_or_create`'s
  `created`. They pass `created` to the writer (required keyword). Only `created=True` takes the
  planned type unchecked. A pre-existing type-less row is checked like any other; on a refusal it
  keeps its type and the row shows the reason. r2-4 is withdrawn.

### Round 3 (codex `gpt-6-astra` high, read-only): A NOT RATIFIED, B NOT RATIFIED

1.4, 2.4, 2.5 CLOSED. 1.1 NOT CLOSED. r3-3 provenance: exactly two production writer calls and
three `get_or_create` sites (`views/sync/interfaces.py:1670`, `:1710`, `interface_sync.py:275`); OOB
rows use the device resolver; existing-id/name branches carry `created=False`.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 3.1 | MAJOR | per-row writer locks in snapshot order (`views/sync/interfaces.py:1102`) cycle with migrate reconciliation, which locks third-device interfaces one by one before its owner check (`migrate.py:298`, `:321`); even unchanged rows now take locks; the sync catches `IntegrityError`, not a deadlock `OperationalError` (`:250`) | accepted. Note: reconciliation's field order is arbitrary, so today's pk-ordered relationship batch deadlocks with it too for the mirrored input. Any new lock adds schedules against such a counterparty, so the counterparties must be fixed in A. | accepted, r4-1, r4-2 |
| 3.2 | MAJOR | module type apply resolves the module before the transaction; after a move it would lock the old device and the new device's interfaces | confirmed: `views/sync/modules.py:2505`, `:2520` | accepted, r4-2 |

Verdict B: the clustering is valid, but A cannot defer the compatibility of its own new locks.

### r4 (changes from r3, verbatim)

- **r4-1. One interface acquisition per sync transaction.** After the owner locks and before the
  first row mutation, the bulk interface sync locks every interface row of the locked owner scope
  (the device, every member of its virtual chassis, or the VM) in one
  `select_for_update(of=("self",)).order_by("pk")` statement. The writer's per-row reselect then
  re-reads rows the transaction already holds. Relationship targets sit in the same owner scope
  (NetBox requires the same device or virtual chassis for `lag`, `parent`, `bridge`), so the
  relationship pass's later batch takes no new row locks. Rows the transaction creates are private
  to it. The IP-driven create locks its one row under the owner lock it already holds. Cost: one
  statement per sync, one row lock per interface of the owner scope.
- **r4-2. Counterparties that lock interface rows without the owner lock are fixed in A.**
  - Migrate reconciliation (`migrate.py:321`), `TransferDeviceIPView` (`migrate.py:960`) and
    `MergeNetBoxDevicesView` (`imports/actions.py:4012`): the interface lock query is constrained
    to the already-locked winner device (`device=winner`); no row means skip or refuse, as the
    post-lock owner check does today. They then lock only rows of an owner they hold.
  - Module type apply: lock the target device, re-read the module scoped to that device (refuse a
    moved module), scope the interface lock query to that device, order by pk, and derive template
    targets from the re-read state. No Module lock after the Device lock (NetBox 4.7 order is Module
    first).
  - Module adoption (`modules.py:483`): add `.order_by("pk")`; no device lock (2.2).
  - Cable remote-create keeps its explicit exception (round 2: an existing row aborts it).
  - **Invariant:** every transaction that locks interface rows of an owner either holds that
    owner's lock (so it serializes with the sync) or takes them in one ascending statement while it
    holds no other interface row lock.
- **Mechanism B (follow-up):** the inventory guard over every locking primitive (2.3), and lock
  orders that do not touch interface rows. r3-1's per-row-lock standalone claim is withdrawn.
- **File budget.** r4 touches three files not yet in #180's diff (`views/sync/modules.py`,
  `views/sync/migrate.py`, `views/imports/actions.py`): 100/100. Tests go to files already in the
  diff.

### Round 4 (codex `gpt-6-astra` high, read-only): A NOT RATIFIED, B NOT SEPARABLE

3.2 CLOSED. 1.1 and 3.1 NOT CLOSED. The reviewer classified every production interface-locking
site (52 `select_for_update`, 12 `relock_scoped_row`, plus implicit NetBox writes) and explored all
interleavings of each pair against the r4 sync.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 4.1 | BLOCKER | module adoption locks interface `I`, then NetBox's component counter `UPDATE`s the Device: `I -> D` against the sync's `D -> I`; one interface is enough, ordering cannot fix it | accepted (4.4.0 and 4.7.0 counter receivers) | mechanism B |
| 4.2 | MAJOR | repeated or unordered interface UPDATE/DELETE in the same outer transaction: template adoption fallback, install loops, VC normalization, replacement cascades, interface delete (+ owner counters) | accepted | mechanism B |
| 4.3 | MAJOR | NetBox 4.7's channel-rename `on_commit` callback opens a new owner-free transaction and saves children in name order | accepted; upstream behaviour | mechanism B |
| 4.4 | MAJOR | cable replacement retraces paths and updates path-origin interfaces beyond the locked owner set | accepted; upstream behaviour | mechanism B |

Also: r4-1 locks every interface of a VC for a one-row sync, and the initial owner set is
permission-restricted while the relationship scope locks all VC members.

**Clustering.** 1.1, 2.1-2.3, 3.1, 4.1-4.4 are one mechanism: **there is no global acquisition order
for interface rows, and NetBox itself takes interface and owner locks in orders the plugin does not
control** (counters, on-commit renames, path retrace). Most listed cycles already exist against
today's relationship pass (owner, then a pk-ordered interface batch) and today's attribute
`UPDATE`. An ordering rule cannot be closed at plugin level. Four rounds patched variants of it.

### r5

**Operator decision (2026-09-24): split.** The r5 core goes to one counted verdict round; a blocker
there reports the design blocked. Target: PR #180 (98/100 files).

**General fix: the attribute writer never waits on an interface row lock.** A cycle needs every
member to wait. A transaction that never waits for a lock adds no edge to any cycle.

- **r5-1.** The writer opens its own savepoint and takes the row with
  `select_for_update(of=("self",), nowait=True)`. `LockNotAvailable` (SQLSTATE `55P03`, caught
  narrowly) refuses that row: "NetBox interface <name> is being changed by another operation. Try
  again." The bulk sync reports it like any other row refusal; the IP-driven create raises the
  `ValueError` its callers already handle.
- **r5-2.** It plans and writes the fresh locked instance (r2-2). When nothing changes (fields,
  custom fields, MAC), it rolls its savepoint back with `transaction.set_rollback(True)`.
  PostgreSQL releases row locks taken inside a rolled-back savepoint. The rows the writer holds at the
  end are then exactly the rows today's `UPDATE` holds: changed rows only.
- **r5-3.** r4-1 (whole-scope prelock) and r4-2 (counterparty changes) are withdrawn. Module type
  apply keeps today's lock order and only routes through `type_change_refusal` (r2-3, r3-2). 3.2 is
  then moot: the core adds no device lock there.
- **Standalone check.** Against today, the writer drops wait edges (today's `save()` waits on a
  locked row; r5 refuses it) and holds no extra rows. Every remaining cycle needs a wait the core
  does not add. Those cycles are today's.
- **Mechanism B (follow-up issue):** deadlock exposure of the relationship pass, module
  install/adoption/replace, interface delete, cable replacement, and NetBox's own owner-free writes.
  Candidate direction: the same no-wait rule for every plugin interface-row acquisition, plus the
  inventory guard (2.3). Evidence: 1.1, 2.1-2.3, 3.1, 4.1-4.4.

### Core (split scope, for the verdict round)

1. **Check.** `interface_diff.type_change_refusal(interface, new_type) -> str | None` on a
   `copy.copy` with the new type: `clean_fields(exclude=<all but type>)`; then "has LAG members
   (`Interface.objects.filter(lag_id=pk).exists()`) and new type is not `lag`"; then the guarded
   `clean()`. First failure is the reason (with the rule names). No two-copy comparison. An
   already-invalid interface holds the type change (D7).
2. **Planner.** `planned_interface_type(interface, decision, *, created) -> PlannedType(value,
   kept_reason)`: UNMAPPED, VMInterface, `created=True`, or unchanged type -> today's behaviour, no
   query; otherwise `type_change_refusal` decides.
3. **Guard.** `utils.netbox_interface_clean(interface)` shared with `_validate_relationship`: the
   4.4.0 `virtual_chassis` fault reruns without the parent on the same chassis and raises NetBox's
   intended `ValidationError` on a different one (D3); any other `AttributeError` propagates.
4. **Writer.** `update_interface_from_port(..., created)`: asserts an atomic block; own savepoint;
   `select_for_update(of=("self",), nowait=True)` reselect (r5-1); decide the rule with the locked
   owner platform; plan; write; full save of the fresh instance; returns
   `InterfaceWriteResult(interface, changed)`; nothing changed -> `set_rollback(True)` (r5-2);
   `55P03` -> row refused "being changed by another operation". Callers pass `created` from their
   `get_or_create` (`False` on id/name branches) and continue with the returned instance.
5. **Diff.** `compute_row_sync_state` plans once per existing row; `_type_differs` compares `value`;
   `RowSyncState.type_kept_reason` renders as a Type-cell note; a kept type is not a difference.
   Table, verify repaint and single-row response share it.
6. **Module type apply** calls `type_change_refusal` (refusal -> `validation_failed` with the
   reason); lock order unchanged.
7. **AST guard (D9):** `Interface.type` is assigned only by the writer via the planner, module apply
   via the check, the ratified promotions and their restore callbacks, and creates.
8. **Tests:** AC1-AC7 as in 6A with r2-5 (real 4.4.0 on CI's leg), D10, the 1.4/2.5 creation cases,
   a NOWAIT refusal test (a second connection holds the row lock; the sync refuses that row and
   commits the others), and a savepoint-release test (an unchanged row's lock is not held after the
   writer returns: a second connection can take it with NOWAIT).

**Deferred (follow-up issue, filed after the core is implemented):** plugin-wide interface-row
deadlock exposure (mechanism B), evidence 1.1, 2.1-2.3, 3.1, 4.1-4.4.

### Round 5 = core verdict round (codex `gpt-6-astra` high, read-only): r5 core NOT RATIFIED — DESIGN BLOCKED

Checked and holding: the savepoint mechanism (PostgreSQL releases row locks taken after a savepoint
on rollback to it; Django's nested `set_rollback(True)` gives `SAVEPOINT -> ROLLBACK TO -> RELEASE`
with the outer transaction usable); planner and module apply take no row locks; NOWAIT refusal
leaves no partial state; skeleton and IP paths; no CLOSED finding reopened.

| # | sev | finding | my check | status |
|---|---|---|---|---|
| 5c.1 | BLOCKER | while it holds the NOWAIT-locked interface, the writer still waits on other rows: `assign_interface_mac` calls `mac_addresses.add()` unconditionally, which `UPDATE`s the MAC row even when nothing changes; `BaseInterface.save()` clears tagged-VLAN relations before the row save. Today an unchanged row takes no interface lock, so r5 adds `I -> M` wait edges | confirmed: `interface_sync.py:79` (unconditional `add`) | open |
| 5c.2 | MAJOR | after an unchanged writer releases its lock, the VLAN helper full-saves the returned instance and overwrites a concurrent `lag_id` (AC5 unmet) | confirmed: `views/sync/interfaces.py:1585` -> `views/mixins.py:1712` full `save()` | open |

**Clustering.** Both come from the same place: the writer's lock boundary is smaller than the row
operation. The row operation is the attribute writer, the MAC assignment and the VLAN sync, plus
NetBox's implicit writes inside `save()`. Any early lock on `I` changes what is held while those
run.

**Status: BLOCKED** under the agreed rule (a blocker in the split core's verdict round). The
operator decides the next step.

**Candidate r6 (not reviewed): late lock.** No lock until the row has a change. Plan on a fresh
unlocked read, then run the MAC step (MAC before `I`, as today). If any field changes, take
`select_for_update` on `I` at the point where today's `UPDATE` already takes and waits for that row
lock, re-read, re-plan the type against the locked state, then `save(update_fields=<changed>)`. The
VLAN helper saves with `update_fields` too. The wait edges and held rows are then the same as today.
The redundant MAC `add()` becomes a no-op when the MAC is already attached. Known open point: the
members rule reads other rows (`lag_id` on members), which a lock on `I` does not stabilise under
any scheme so far.

**Operator decision (2026-09-24): rescope to the check only.** No new interface row lock in #185.
The row lock, the plan-vs-concurrent-link race, and plugin-wide deadlock exposure go to one
follow-up issue (evidence 1.1, 2.1-2.3, 3.1, 4.1-4.4, 5c.1, 5c.2, and the members-rule note above).
The rescoped core gets one verdict round.

### Rescoped core (check only, for its verdict round)

Kept unchanged from "Core (split scope)": 1 (check), 2 (planner with `created`), 3 (guard),
5 (diff), 6 (module type apply), 7 (AST guard). Changed:

- **Writer.** No new lock and no reselect: it plans against the instance its caller's resolver
  just read in the same transaction. Callers pass `created` from `get_or_create` (`False` on id/name
  branches). The save becomes `save(update_fields=<tracked fields that changed> + custom_field_data
  when it changed>)`, so a stale instance never writes `lag`, `parent`, `bridge` or any other column
  the writer does not own. It returns the bool it returns today. r2-2, r5-1, r5-2 are withdrawn.
- **VLAN helper** (`views/mixins.py:1712`): `save(update_fields=["mode", "untagged_vlan"])` for the
  same reason (5c.2).
- **MAC** (`interface_sync.py:79`): skip `mac_addresses.add()` when the MAC is already attached
  (a redundant `UPDATE`, found in 5c.1).
- **AC5 reworded:** a concurrent link write between the table load and the sync is not lost. The
  planner reads the link state current at planning time. A link written by another transaction
  between planning and the `UPDATE` is a known race, deferred with the row lock.
- **Tests:** as Core 8, minus the NOWAIT and savepoint tests; plus a lost-update test for the writer
  and for the VLAN helper (stale instance, concurrent `lag_id` in the DB, sync, `lag_id` survives;
  mutation: full `save()` goes red).
- **File budget:** `views/mixins.py` is new to #180's diff: 98/100.

### Rescoped core verdict round (codex `gpt-6-astra` high, read-only): NOT RATIFIED on the partial saves only

| # | sev | finding | status |
|---|---|---|---|
| 6.1 | MAJOR | `update_fields` without `_name` leaves NetBox's natural-ordering key stale after a rename (`NaturalOrderingField.pre_save` runs only for saved fields) | withdrawn with the partial saves |
| 6.2 | MAJOR | `update_fields` without `last_updated` stops advancing the `auto_now` timestamp | withdrawn with the partial saves |

No finding on items 1-3 and 5-7. The reviewer executed 4.4.0's real `Interface.clean()` with the
guard (same chassis accepted; different chassis, physical child and virtual-with-LAG refused),
checked the MAC no-op (identical primary assignment and change result in all three cases), and found
no stale-object defect from dropping the reselect.

**Operator decision (2026-09-24): defer the lost update.** The writer and the VLAN helper keep
today's full `save()`. The stale-save lost update (5.3, 5c.2) moves to the concurrency follow-up
with the row lock. **Closed on the reviewer's own executed evidence at the cap**: the rescoped core
minus the partial saves has no open finding.

## 7. Ratified scope for issue #185 (2026-09-24)

1. `interface_diff.type_change_refusal(interface, new_type) -> TypeRefusal | None`: on `copy.copy`
   with the new type, `clean_fields(exclude=<all but type>)`, then the members rule
   (`Interface.objects.filter(lag_id=pk).exists()` and the new type is not `lag`), then the guarded
   `clean()`. The first failure is the reason. No two-copy comparison. `TypeRefusal` holds the
   message, its `ValidationError` field and the objects that the message can name (item 9).
2. `interface_diff.planned_interface_type(interface, decision, *, created) -> PlannedType(value,
   kept)`. UNMAPPED, VMInterface, `created=True` or an unchanged type: today's behaviour, no
   query. Otherwise `type_change_refusal` decides; a refusal keeps the current type, and `kept`
   holds the rule names, the planned type and the refusal.
3. `utils.netbox_interface_clean(interface)`, shared with `_validate_relationship`: the 4.4.0
   `virtual_chassis` fault reruns without the parent on the same chassis, and raises NetBox's
   intended `ValidationError` on a different one. Any other `AttributeError` propagates.
4. Writer: `update_interface_from_port(..., created)` sets the planned type and logs the kept note,
   with NetBox's full message, at warning. Callers pass `created` from `get_or_create` (`False` on the id and name branches).
   Save, lock and return value are unchanged.
5. Diff: `compute_row_sync_state` plans once per existing row; `_type_differs` compares `value`;
   `RowSyncState.type_kept` renders as a Type-cell note for the table's user; a kept type is not a
   difference. Table, verify repaint and single-row response share it.
6. Module type apply calls `type_change_refusal`; a refusal returns `validation_failed` with the
   refusal, and the warning shows it through the rule in item 9. Lock order unchanged.
7. AST guard: `Interface.type` is assigned only by the writer via the planner, module apply via the
   check, the ratified promotions and their restore callbacks, and creates. The guard also finds ORM
   writes: `update(type=...)`, `bulk_update` with `type`, and `type` in `*_or_create` defaults.
8. `assign_interface_mac` skips `mac_addresses.add()` when the MAC is already attached.
9. Disclosure: NetBox's message can name linked objects (a parent, bridge or LAG on another device,
   that device, the virtual chassis, the untagged VLAN). A page shows the message only when the
   viewer may view every such object (`utils.object_is_visible`, the check that `PortDisclosure`
   batches). A field that this plugin does not know can name any object, so only a superuser gets
   its message. An admin `CUSTOM_VALIDATORS` entry for `dcim.interface` can put any text under any
   field, so while one applies, every NetBox message is treated as unknown. Otherwise the note names
   the field and hides the message. The log keeps it. The members rule message always shows.

**Acceptance conditions:** AC1, AC2, AC3, AC4, AC6 (as in D7), AC7 (real 4.4.0 `clean()` on CI's
`v4.4.0` leg; mutation: remove the guard -> red). Plus: the D7 case (an unrelated `clean()` error
holds the type), the 1.4/2.5 creation cases (a skeleton created with a `channel` mapping writes it;
a pre-existing type-less row is checked), module apply refusing a non-`lag` template type on an
aggregate with members, and a query-count test (no extra query for a row whose type matches). AC5
is deferred.

**Follow-up issue (file after this scope is implemented):** interface-row concurrency. It covers the
row lock, the plan-vs-concurrent-link race, the stale full-save lost update (5.3, 5c.2), and
plugin-wide deadlock exposure. Evidence: 1.1, 2.1-2.3, 3.1, 4.1-4.4, 5c.1, 5c.2, 6.1, 6.2, and the
members-rule note. Candidate directions on record: late lock (r6), no-wait acquisition (r5).
