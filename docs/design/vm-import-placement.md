# VM Import Placement

## 0. Review record

No claims have been refuted.

## 1. Factual brief

NetBox 4.6 accepts a virtual machine that is assigned to a site, a cluster, or a host device. The corrected importer will offer all three placement choices.

The current import workflow overloads `cluster_<device_id>` as both a cluster selection and the decision to import a LibreNMS row as a virtual machine. Search-result validation skips site matching for new virtual machines, adds a mandatory-cluster issue, and marks readiness from cluster state alone. VM creation writes a cluster but not a site. The same assumption reaches the HTMX row updates, confirmation form, synchronous dispatcher, background job payload, and bulk VM importer.

The corrected workflow must:

- Use an explicit object-type choice that is independent from placement.
- Accept a new VM when its matched site, selected cluster, or selected host device supplies placement.
- When a selected host belongs to a cluster, assign both the host and that cluster as NetBox requires.
- Preserve device import, existing-object detection, collision checks, permission checks, and naming behavior.
- Keep synchronous and background imports equivalent.
- Reject malformed object-type and placement input without changing the target model.
- Use NetBox-native controls and semantic colors in light and dark themes.
- Test the public import request with the real Django request, ORM, validation, and rendered response. Mock only the LibreNMS network boundary.

Evidence:

- Installed NetBox `VirtualMachine.clean()` rejects a row only when site, cluster, and device are all absent.
- `netbox_librenms_plugin/import_utils/device_operations.py` currently adds the mandatory-cluster issue and bypasses site matching for VM rows.
- `netbox_librenms_plugin/views/imports/actions.py` derives VM mode from a truthy cluster in row refresh, confirmation, and execution.
- `netbox_librenms_plugin/import_utils/vm_operations.py` writes only the validated cluster during VM creation.

## 2. Candidate designs

### Candidate A: explicit request fields over the existing import split

Add `object_type_<source_id>` with the values `device` and `virtualmachine`. Add
`vm_placement_<source_id>` with the values `site`, `cluster`, and `host`. Keep the existing
role, rack, and cluster inputs, and add `host_device_<source_id>`. A strict parser rejects
unknown, incomplete, or contradictory combinations.

Keep the existing Device and VM bulk import functions and job collections. Derive those
collections from the parsed object type, never from cluster presence. VM validation always
matches the source location to a site. Its readiness rule uses the selected placement method.
VM creation writes the matched site, selected cluster, or selected host. A host that belongs to a
cluster also supplies that cluster.

This is the smaller change. It removes the incorrect request inference, but it leaves model and
placement decisions copied across confirmation, permissions, collision checks, synchronous
dispatch, job serialization, and job execution.

### Candidate B: discriminated row plans

Introduce an import-plan module with two immutable layers. `ImportRowIntent` represents the
possibly incomplete state of a search row. `ImportRowPlan` represents an executable import after
all required choices resolve.

```text
ImportRowIntent(
    source_device_id,
    object_type,
    vm_placement_method,
    optional role_id,
    optional rack_id,
    optional cluster_id,
    optional host_device_id,
)
```

The intent can represent `Virtual machine + Cluster + no cluster selected` and similar states.
This lets an HTMX refresh render a precise blocker without bypassing the semantic module or
reading raw placement fields elsewhere.

The executable variants are:

```text
DeviceTarget(role_id, optional rack_id)
VirtualMachineTarget(
    MatchedSitePlacement
    or ClusterPlacement(cluster_id)
    or HostPlacement(host_device_id),
    optional role_id,
)
ImportRowPlan(source_device_id, target)
```

One HTTP adapter parses row controls into intents. Search and HTMX validation consume intents.
Before confirmation, the module validates each intent against resolved NetBox objects and turns
it into an executable plan. One serializer carries executable plans through the background job.
Permission derivation, collision mode, confirmation, and dispatch consume target variants. They
do not inspect placement fields to decide the model. The existing Device and VM bulk operations
remain separate implementation details behind this semantic interface.

Validation keeps requested intent separate from the kind of an existing object that a search
finds. It always performs site matching. For a new VM, it applies the plan's placement and blocks
only when that placement is unavailable. Host resolution is object-permission scoped and fails
closed if the Device is missing or no longer visible. A clustered host supplies its cluster.

VM creation constructs the object, calls `full_clean()`, saves it, and assigns the LibreNMS
identity in the existing transaction. It writes exactly these fields:

- Matched site: `site=<matched site>`.
- Cluster: `cluster=<selected cluster>`.
- Standalone host: `device=<selected device>`.
- Clustered host: `device=<selected device>, cluster=<device.cluster>`.

The search-row UI keeps Proposal A's compact layout. Device rows show the required role inline.
VM rows show the active placement inline. The options menu contains object type, placement method,
optional VM role, and optional rack for Devices. The host control uses NetBox's remote Device
selector so a large inventory does not create a wide or unbounded table cell. All controls use
NetBox and Bootstrap semantic classes, so NetBox owns light and dark rendering.

### Candidate C: shared execution pipeline

Use Candidate B and also replace the two orchestration paths with one executor that owns
permission derivation, collision preflight, and Device/VM dispatch. The view and background job
become adapters around the same executor.

This is the deepest seam, but it combines the placement correction with a broad rewrite of the
existing collision interstitial, job progress, cancellation, and response rendering. That extra
replacement is not necessary to remove cluster-derived model selection.

## 3. Divergences

| Decision | Candidate A | Candidate B | Candidate C | Resolution |
|---|---|---|---|---|
| Semantic owner | Request helper | Typed import-plan module | Plan module plus executor | Choose B. It removes distributed inference without rewriting unrelated orchestration. |
| Job contract | Existing parallel collections | Serialized row plans | Serialized row plans | Choose serialized plans. Synchronous and queued requests use one meaning. |
| Import operations | Existing Device and VM operations | Existing operations consume typed partitions | One new executor | Keep the operations. Their model-specific work is a valid seam. |
| Placement model | Strings and mutable validation dicts | Incomplete intent plus exclusive executable variants | Exclusive placement variants | Choose the two-layer model. Incomplete UI state is representable, but invalid state cannot execute. |
| Host support | Add an ID field | Add `HostPlacement` | Add `HostPlacement` | Offer it. A clustered host supplies its own cluster. |
| Existing matches | Continue mutating `import_as_vm` | Separate requested target from actual match kind | Same as B | Separate the facts. An existing match must not rewrite creation intent. |
| Scope | Smallest diff | Coordinated contract replacement | Broad workflow rewrite | Choose B. C changes more behavior than this defect requires. |

The blind candidate preferred Candidate C before host placement became a requirement. The local
candidate preferred Candidate A. The merged design chooses Candidate B: it takes the blind
candidate's semantic plan and serialization boundary, adds host placement, and keeps the current
model-specific import operations to limit risk.

## 4. Gate inventory and acceptance conditions

The replacement covers these gates:

- Initial search-result validation and cached row refresh.
- Object-type, placement, role, rack, platform, and device-type HTMX refreshes.
- Details and existing-match modal requests.
- Confirmation rendering and its hidden form.
- Direct synchronous POST parsing.
- Permission derivation and collision-only validation.
- Background-job serialization, permission recheck, collision scan, and dispatch.
- VM bulk validation and creation.
- Post-import row refresh and documentation.

Acceptance conditions:

1. A new search row defaults to Device. Cluster or host input alone cannot change its type.
2. A row with a matched site becomes a ready VM with matched-site placement and no cluster.
3. Cluster placement works without a matched site.
4. A standalone host creates a VM with that host. A clustered host creates a VM with that host
   and its cluster.
5. A missing type retains the existing safe Device default. A malformed type, or an incomplete or
   malformed VM placement, fails closed. Placement never changes the target model or permissions.
6. Confirmation shows and preserves the explicit type and placement.
7. Synchronous and background paths consume the same serialized plans and create the same VM.
8. Existing site-only, cluster-only, and host-placed VMs render as existing VMs.
9. Device validation still requires site, device type, and role. Rack remains optional. Virtual
   chassis detection remains Device-only.
10. Existing ambiguity, collision, disclosure, multi-server, and permission behavior remains
    fail closed.
11. The selected host must be visible under NetBox object permissions when the plan is validated
    and when it executes.
12. Every partial HTMX request preserves the plan plus server and naming state.
13. The controls remain compact and use NetBox-native theme-aware styling.

## 5. Ratification

Round 1 found that the first merged design used one type for both incomplete UI state and an
executable import. It could not represent a VM row whose placement method was selected before its
cluster or host. The design now uses `ImportRowIntent` for search and HTMX state, and converts it
to `ImportRowPlan` only at the confirmation boundary.

Round 2 ratified the revised design: `RATIFY r2`. The intent-to-plan seam preserves incomplete
HTMX state while keeping malformed or incomplete rows out of permission derivation, collision
checks, serialization, and writes. Host re-resolution plus `VirtualMachine.full_clean()` covers
the NetBox placement constraints.

## 6. Test and implementation increments

After ratification, use vertical TDD slices:

1. Add a failing real request/parser matrix for explicit target and all three placements.
2. Add a failing search-row and HTMX test for matched-site VM readiness and state preservation.
3. Add a failing synchronous request-to-ORM test for site, cluster, standalone-host, and
   clustered-host VM creation.
4. Add a failing background-job test that deserializes the same plans and produces equivalent
   placement.
5. Add malformed-input, permissions, collision, and existing-VM regressions.
6. Add a small AST regression test that rejects model selection derived from cluster or host
   truthiness in production import code.
7. Update the import documentation and run the full Django, browser, Ruff, C901, formatting, and
   pre-commit checks.
