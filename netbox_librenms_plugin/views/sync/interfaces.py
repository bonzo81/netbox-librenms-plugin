import logging
from urllib.parse import quote_plus

from dcim.models import Device, Interface, VirtualChassis
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.interface_relationships import (
    build_interface_index,
    filter_interface_index,
    interface_owner_for_object,
    interface_queryset_for_object,
    relationship_candidate_ids,
    relationship_candidate_q,
    resolve_interface_by_port_id,
)
from netbox_librenms_plugin.interface_sync import (
    assign_interface_mac,
    get_netbox_interface_type,
    update_interface_from_port,
)
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    build_migrated_context,
    convert_speed_to_kbps,
    validation_error_detail,
    find_by_librenms_id,
    get_interface_name_field,
    get_librenms_sync_device,
    get_interface_port_identity_sets,
    interface_name_fallback_matches_port,
    is_list_of_dicts,
    normalize_librenms_port_id,
    netbox_clean_reads_parent_virtual_chassis,
    normalize_relationship_maps,
    resolve_interface_row_device,
    interface_name_rejection_reason,
    syncable_interface_name,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
    relock_scoped_row,
)

logger = logging.getLogger(__name__)


class SyncInterfacesView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, VlanAssignmentMixin, CacheMixin, View
):
    """Sync selected interfaces from LibreNMS into NetBox."""

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        # The owner is resolved through a restricted queryset (get_object), so its view
        # permission is stated here: a missing grant is an explicit 403, not a 404.
        if object_type == "device":
            return [("view", Device), ("add", Interface), ("change", Interface)]
        elif object_type == "virtualmachine":
            return [("view", VirtualMachine), ("add", VMInterface), ("change", VMInterface)]
        else:
            raise Http404(f"Invalid object type: {object_type}")

    def post(self, request, object_type, object_id):
        """Sync selected interfaces from LibreNMS into NetBox."""
        # Set permissions dynamically based on object type
        self.required_object_permissions = {
            "POST": self.get_required_permissions_for_object_type(object_type),
        }

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        obj = self.get_object(object_type, object_id)
        self.object = obj  # Store for use in sync methods

        interface_name_field = get_interface_name_field(request, obj)
        self.interface_name_field = interface_name_field

        # Rebind the client to the POSTed server so cache reads, per-server id writes and
        # the redirect all use the exact server the user was viewing. Fail closed on a
        # stale/unknown key — the old `or self.librenms_api.server_key` fallback rebuilt
        # the lazy client, which can resolve to a different server (wrong-server sync) or
        # raise on a misconfigured default (500).
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return redirect(
                reverse(url_name, kwargs={"pk": object_id})
                + f"?tab=interfaces&interface_name_field={interface_name_field}"
            )
        self._post_server_key = server_key
        selected_port_ids = self.get_selected_port_ids(request)
        exclude_columns = request.POST.getlist("exclude_columns")

        redirect_url = (
            reverse(url_name, kwargs={"pk": object_id})
            # quote_plus the field too: it comes from the request, so unescaped special chars
            # could corrupt the redirect or inject extra query parameters.
            + f"?tab=interfaces&interface_name_field={quote_plus(interface_name_field)}"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        if selected_port_ids is None:
            return redirect(redirect_url)
        visible_port_ids = selected_port_ids
        self._selected_port_ids = set(visible_port_ids)
        self._auto_selected_port_ids = set()

        ports_data = self.get_cached_ports_data(request, obj, server_key)
        if ports_data is None:
            return redirect(redirect_url)

        relationships = self._get_cached_relationships(obj, server_key)
        lag_members, sub_interfaces = normalize_relationship_maps(relationships)
        if request.POST.get("auto_select_lag_members"):
            while True:
                related_rows = {
                    member_id
                    for member_id, aggregate_id in lag_members.items()
                    if aggregate_id in self._selected_port_ids
                }
                related_rows.update(
                    parent_id for child_id, parent_id in sub_interfaces.items() if child_id in self._selected_port_ids
                )
                added = related_rows - self._selected_port_ids
                if not added:
                    break
                self._selected_port_ids.update(added)
                self._auto_selected_port_ids.update(added - visible_port_ids)
        host_port_id_counts = {}
        for port in ports_data:
            if port.get("_source") == "oob":
                continue
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if port_id is not None:
                host_port_id_counts[port_id] = host_port_id_counts.get(port_id, 0) + 1
        duplicated_selected_ids = sorted(
            port_id for port_id in self._selected_port_ids if host_port_id_counts.get(port_id, 0) > 1
        )
        if duplicated_selected_ids:
            messages.warning(
                request,
                "Selected LibreNMS port IDs are duplicated in the cached interface data. "
                "Refresh LibreNMS data and resolve the duplicate IDs before syncing.",
            )
            return redirect(redirect_url)
        # Resolve inferred off-page owners only after the chassis and its members are locked.
        # A pre-lock position guess can become stale if membership positions change concurrently.
        self._auto_selected_target_ids = {}

        # Collects interfaces skipped because their LibreNMS port_id resolves to an
        # interface on a *different* device (see _resolve_device/vm_interface). Surfaced
        # below so the skip isn't silent — otherwise the user only sees it in the logs.
        self._skipped_conflicts = []
        self._synced_count = 0
        try:
            with transaction.atomic():
                try:
                    self.sync_selected_interfaces(
                        obj,
                        ports_data,
                        exclude_columns,
                        interface_name_field,
                        keep_locked_targets=True,
                    )

                    # Keep the target-device locks and their current object map through relationship
                    # validation and persistence. Reusing the map also avoids one permission-filtered
                    # Device lookup per selected VC relationship edge.
                    self._sync_lag_and_parent_relationships(
                        self.object,
                        ports_data,
                        relationships,
                        server_key,
                        excluded_columns=exclude_columns,
                    )
                finally:
                    self.__dict__.pop("_locked_target_devices", None)
        except IntegrityError:
            # This block is the outermost transaction, and Postgres validates Django's DEFERRABLE
            # INITIALLY DEFERRED foreign keys at its COMMIT. A related row deleted mid-sync
            # therefore surfaces here, past every inner savepoint handler, and would otherwise 500.
            logger.warning("Bulk sync: rolled back by a concurrent DB conflict at commit", exc_info=True)
            messages.error(
                request,
                "The sync was rolled back by a concurrent change to a related interface. "
                "Refresh the LibreNMS data and try again.",
            )
            return redirect(redirect_url)

        if self._skipped_conflicts:
            skipped = ", ".join(self._skipped_conflicts)
            messages.warning(
                request,
                f"{len(self._skipped_conflicts)} interface(s) skipped: {skipped}.",
            )
        # Only claim success when at least one interface was actually synced. Track an explicit
        # synced count rather than comparing skip-vs-selected sizes: a single selected display name
        # can be skipped after another selected port succeeds, so the explicit count remains the
        # source of truth for the success banner.
        if self._synced_count > 0:
            messages.success(request, "Selected interfaces synced successfully.")
        return redirect(redirect_url)

    def get_object(self, object_type, object_id):
        """Return the Device or VirtualMachine for the given type and ID (object-scoped)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        if object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        raise Http404("Invalid object type.")

    def get_selected_port_ids(self, request):
        """Return selected visible LibreNMS port IDs from POST data."""
        visible = {
            port_id
            for raw_port_id in request.POST.getlist("select")
            if (port_id := normalize_librenms_port_id(raw_port_id)) is not None
        }
        if not visible:
            messages.error(request, "No interfaces selected for synchronization.")
            return None
        return visible

    def get_cached_ports_data(self, request, obj, server_key=None):
        """Return cached LibreNMS port data for the given object."""
        if server_key is None:
            server_key = self.librenms_api.server_key
        # On VC member pages the GET tab writes ports under the resolved sync device's
        # cache key. Resolve the same device here — UNCONDITIONALLY, mirroring the
        # writers (BaseInterfaceTableView.post/get_context_data) — so the POST path
        # reads the same entry. Gating the resolve on the viewed member having no id
        # of its own diverges from the writers when the member holds a legacy bare-int
        # while a sibling holds the preferred explicit per-server mapping: the refresh
        # then caches under the sibling and this reader misses forever.
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        cached_data = cache.get(self.get_cache_key(cache_obj, "ports", server_key))
        # No cached entry at all (or a non-dict one) → ask the user to refresh before syncing.
        if not isinstance(cached_data, dict):
            self._cached_ports_payload = None
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        # A dict that simply lacks a 'ports' key is treated as 'no ports to sync' — a harmless
        # empty no-op (matching the historical behavior). But a PRESENT-but-malformed ports value
        # (None, a non-list, or a list with non-dict entries) is failed closed so the sync loops
        # don't 500 mid-sync.
        ports_data = cached_data.get("ports", [])
        if not isinstance(ports_data, list) or any(not isinstance(port, dict) for port in ports_data):
            messages.warning(
                request,
                "No cached data found. Please refresh the data before syncing.",
            )
            return None
        # Stash the whole payload so _get_cached_relationships can read port_stack_relationships
        # from it without a second cache round-trip for the same key.
        self._cached_ports_payload = cached_data
        return ports_data

    def _resolve_auto_selected_target_ids(
        self,
        obj,
        ports_data,
        port_ids,
        interface_name_field,
        server_key,
        *,
        members=None,
    ):
        """Resolve independently owned off-page rows to their Virtual Chassis members."""
        if not isinstance(obj, Device) or not port_ids:
            return {}

        if members is None:
            members = list(obj.virtual_chassis.members.all()) if obj.virtual_chassis else [obj]
        members_by_position = {member.vc_position: member for member in members if member.vc_position is not None}
        members_by_id = {member.pk: member for member in members}
        ports_by_id = {
            port_id: port
            for port in ports_data
            if (port_id := normalize_librenms_port_id(port.get("port_id"))) is not None and port.get("_source") != "oob"
        }
        candidate_port_ids = [
            ports_by_id[port_id].get("port_id", port_id)
            for raw_port_id in port_ids
            if (port_id := normalize_librenms_port_id(raw_port_id)) in ports_by_id
        ]
        if not candidate_port_ids:
            return {}
        candidate_ids = relationship_candidate_ids(obj, server_key, candidate_port_ids, ())
        interface_queryset = interface_queryset_for_object(obj).filter(pk__in=candidate_ids)
        viewable_ids = set(interface_queryset.restrict(self.request.user, "view").values_list("pk", flat=True))
        changeable_ids = set(interface_queryset.restrict(self.request.user, "change").values_list("pk", flat=True))
        interface_index = build_interface_index(
            obj,
            server_key,
            allowed_ids=viewable_ids | changeable_ids,
        )
        targets = {}
        for port_id in port_ids:
            port = ports_by_id.get(port_id)
            if port is None:
                continue
            target = resolve_interface_row_device(
                obj,
                port,
                interface_name_field,
                interfaces_by_port_id=interface_index["by_lnms_id"],
                members_by_position=members_by_position,
                members_by_id=members_by_id,
                return_device_on_failure=False,
            )
            if target is not None:
                targets[port_id] = target.pk
        return targets

    def _get_cached_relationships(self, obj, server_key):
        """Return port_stack_relationships from the cached port data, or empty dict."""
        # Reuse the payload get_cached_ports_data already fetched in post(); only hit the cache
        # again when called independently (e.g. in isolation/tests) — same VC-scoped key.
        cached_data = getattr(self, "_cached_ports_payload", None)
        if cached_data is None:
            cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
            cached_data = cache.get(self.get_cache_key(cache_obj, "ports", server_key))
        if isinstance(cached_data, dict):
            return cached_data.get("port_stack_relationships", {})
        return {}

    def _sync_lag_and_parent_relationships(
        self,
        obj,
        ports_data,
        relationships,
        server_key,
        *,
        excluded_columns=(),
    ):
        """
        Set LAG member and sub-interface parent relationships for synced interfaces.

        Runs after sync_selected_interfaces() so all interfaces already exist in NetBox.
        Only processes relationships where this interface is a member/child — the
        aggregate/parent may or may not be in the selected set (it just needs to exist
        in NB).

        Args:
            obj: The Device (or VirtualMachine) being synced.
            ports_data: The LibreNMS port dicts for the device.
            relationships (dict): The ``{lag_members, sub_interfaces}`` mapping to apply.
            server_key (str): The LibreNMS server key scoping stored-id reads.

        Returns:
            None
        """
        if not relationships:
            return
        excluded_columns = set(excluded_columns)

        # Normalize the relationship-map keys once at load through the helper used by both
        # readers, so the bulk path cannot drift on the corruption guard or key normalization.
        # resolve_port_relationships
        # emits normalized int keys, but a JSON cache round-trip stringifies dict keys, so the cached
        # map can arrive str- or int-keyed; re-normalizing lets every lookup below use a single
        # normalize_librenms_port_id(port_id) call. The helper also coerces a non-dict relationships
        # (e.g. a list from a corrupt / partial-write cache) to {} — the local `if not relationships`
        # guard above only catches a falsy value, so a truthy non-dict would otherwise AttributeError
        # on .get()/.items() here.
        lag_members, sub_interfaces = normalize_relationship_maps(relationships)
        if not lag_members and not sub_interfaces:
            return

        interface_name_field = self.interface_name_field
        unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(
            ports_data, interface_name_field
        )

        # Resolve the selection to stable LibreNMS port IDs.
        port_by_id = {}
        selected_port_ids = {
            str(port_id)
            for raw_port_id in getattr(self, "_selected_port_ids", set())
            if (port_id := normalize_librenms_port_id(raw_port_id)) is not None
        }
        selected_edge_source_ids = {
            port_id
            for port_id in selected_port_ids
            if normalize_librenms_port_id(port_id) in lag_members
            or normalize_librenms_port_id(port_id) in sub_interfaces
        }
        valid_name_port_ids = {
            normalize_librenms_port_id(port.get("port_id"))
            for port in ports_data
            if port.get("_source") != "oob" and syncable_interface_name(port, interface_name_field) is not None
        }
        selected_edge_source_ids &= {str(port_id) for port_id in valid_name_port_ids if port_id is not None}
        if not selected_edge_source_ids:
            return
        for port in ports_data:
            # OOB-controller rows are context-only (merged for shared-LOM display); sync_selected_interfaces()
            # already skips them. Exclude them here too: on a page where an OOB row shares the host display
            # name, adding its port_id would let the LAG/parent pass persist links on the hidden controller
            # row instead of the host interface.
            if port.get("_source") == "oob":
                continue
            pid = normalize_librenms_port_id(port.get("port_id"))
            if pid is None or pid not in unique_host_port_ids:
                continue
            canonical_pid = str(pid)
            port_by_id[canonical_pid] = port

        candidate_port_ids = []
        candidate_names = []
        for source_port_id in selected_edge_source_ids:
            source_entry = port_by_id.get(source_port_id)
            if source_entry is None:
                continue
            related_ids = (
                lag_members.get(normalize_librenms_port_id(source_port_id)),
                sub_interfaces.get(normalize_librenms_port_id(source_port_id)),
            )
            for raw_candidate_id in (source_entry.get("port_id"), *related_ids):
                candidate_id = normalize_librenms_port_id(raw_candidate_id)
                if candidate_id is None:
                    continue
                candidate_entry = port_by_id.get(str(candidate_id), {})
                candidate_port_ids.append(candidate_entry.get("port_id", raw_candidate_id))
                if candidate_id in unambiguous_name_port_ids:
                    candidate_name = candidate_entry.get(interface_name_field) or candidate_entry.get("ifName")
                    if candidate_name:
                        candidate_names.append(candidate_name)
        try:
            with transaction.atomic():
                obj, locked_device_ids = _lock_relationship_scope(
                    obj,
                    self.restricted_queryset(type(obj)),
                )
                if obj is None:
                    return
                candidate_ids = relationship_candidate_ids(
                    obj,
                    server_key,
                    candidate_port_ids,
                    candidate_names,
                )
                catalog_index, source_index, related_index, changeable_ids = _build_locked_relationship_indexes(
                    obj,
                    server_key,
                    self.request.user,
                    locked_device_ids,
                    candidate_ids=candidate_ids,
                )

                for port_id in selected_edge_source_ids:
                    if port_id not in port_by_id:
                        continue

                    # Pin both ends of the relationship to the owner this row was synced onto (the
                    # per-row device_selection target, or the VM). The VC-wide port_id search can
                    # otherwise resolve a child/parent uniquely onto a *different* member that carries
                    # the same stale librenms_id, persisting lag/parent on the wrong interface.
                    target_device = self._resolve_row_target_device(obj, port_id=port_id)
                    if target_device is None:
                        continue
                    expected_owner = interface_owner_for_object(target_device)

                    # LAG membership: this interface is a member of a LAG aggregate. Both ends are
                    # resolved/validated/persisted by the shared helpers below (same flow as the
                    # parent pass), differing only in the Interface-only source guard and the
                    # aggregate type=lag promotion.
                    raw_lag = lag_members.get(normalize_librenms_port_id(port_id))
                    if raw_lag is not None and normalize_librenms_port_id(raw_lag) in unique_host_port_ids:
                        member_iface, agg_iface = self._resolve_relationship_ends(
                            obj,
                            port_id,
                            raw_lag,
                            port_by_id,
                            catalog_index,
                            source_index,
                            related_index,
                            server_key,
                            expected_owner,
                            interface_name_field,
                            unambiguous_name_port_ids,
                            "LAG",
                            require_interface_source=True,  # VMInterface has no lag field
                        )
                        if member_iface and member_iface.lag_id != agg_iface.pk:
                            if agg_iface.type != "lag" and "type" in excluded_columns:
                                logger.warning(
                                    "Bulk sync: skipping LAG link %s -> %s because interface type is excluded",
                                    member_iface.name,
                                    agg_iface.name,
                                )
                                self._record_skipped_conflict(
                                    member_iface.name,
                                    "aggregate type is excluded",
                                )
                            elif agg_iface.type != "lag" and agg_iface.pk not in changeable_ids:
                                logger.warning(
                                    "Bulk sync: skipping LAG link %s -> %s because the aggregate cannot be changed",
                                    member_iface.name,
                                    agg_iface.name,
                                )
                            else:
                                self._apply_relationship_edge(
                                    member_iface, "lag", agg_iface, self._prepare_bulk_lag_aggregate, "LAG"
                                )

                    # Sub-interface parent: this interface is a child of a parent interface.
                    raw_parent = sub_interfaces.get(normalize_librenms_port_id(port_id))
                    if raw_parent is not None and normalize_librenms_port_id(raw_parent) in unique_host_port_ids:
                        child_iface, parent_iface = self._resolve_relationship_ends(
                            obj,
                            port_id,
                            raw_parent,
                            port_by_id,
                            catalog_index,
                            source_index,
                            related_index,
                            server_key,
                            expected_owner,
                            interface_name_field,
                            unambiguous_name_port_ids,
                            "parent",
                        )
                        if child_iface and child_iface.parent_id != parent_iface.pk:
                            self._apply_relationship_edge(child_iface, "parent", parent_iface, None, "parent")
        except IntegrityError:
            # Immediate conflicts (a unique violation, a row already gone at write time) surface
            # here and roll the relationship pass back as a unit. Deferred FK violations do NOT:
            # Postgres validates those at the outermost COMMIT, which post() handles.
            logger.warning(
                "Bulk sync: LAG/parent relationship pass rolled back by a concurrent DB conflict",
                exc_info=True,
            )
            messages.warning(
                self.request,
                "Interfaces synced, but LAG/parent relationships hit a concurrent change and were "
                "not applied. Re-run the sync.",
            )

    @staticmethod
    def _prepare_bulk_lag_aggregate(agg_iface):
        """
        LAG-pass hook: promote the aggregate to type=lag, returning ``(persist, restore)`` or None.

        member_iface.clean() only accepts the link when the aggregate is type=lag, so it
        is bumped in memory before validation. The aggregate object is reused across rows via
        the shared interface index, so a member whose link later fails validation must restore
        the in-memory type — otherwise a subsequent valid member sharing this aggregate would
        skip the save() and leave the aggregate's type stale in the DB. The restore path is why
        this passes ``with_restore=True`` to the shared promotion helper.
        """
        return _promote_lag_aggregate(agg_iface, with_restore=True)

    def _resolve_relationship_ends(
        self,
        obj,
        port_id,
        related_raw,
        port_by_id,
        catalog_index,
        source_index,
        related_index,
        server_key,
        source_expected_owner,
        interface_name_field,
        unambiguous_name_port_ids,
        log_kind,
        *,
        require_interface_source=False,
    ):
        """
        Resolve the ``(source, related)`` interface pair for one bulk LAG/parent edge.

        Both ends are resolved by stable LibreNMS port_id. The source is pinned to the row target.
        A selected related row is pinned to its own target, which can be another member of the same
        Virtual Chassis.
        Returns ``(None, None)`` — skip the row — on any lookup failure (logged at debug) or,
        when *require_interface_source* is set, when the source isn't an Interface (a
        VMInterface has no lag field).
        """
        related_port_id = str(related_raw)
        normalized_related_port_id = normalize_librenms_port_id(related_raw)
        related_expected_owner = None
        if normalized_related_port_id in getattr(self, "_selected_port_ids", set()):
            related_target = self._resolve_row_target_device(obj, port_id=normalized_related_port_id)
            if related_target is None:
                return None, None
            related_expected_owner = interface_owner_for_object(related_target)
        related_entry = port_by_id.get(related_port_id, {})
        # Use the active display field for the name fallback: in ifDescr mode the NetBox
        # interface name matches ifDescr, so hinting ifName would look up the wrong name and
        # silently skip the link. Fall back to ifName if absent.
        related_name = ""
        if normalized_related_port_id in unambiguous_name_port_ids:
            related_name = related_entry.get(interface_name_field) or related_entry.get("ifName", "")

        _, err = resolve_interface_by_port_id(
            obj,
            port_id,
            server_key,
            expected_owner=source_expected_owner,
            index=catalog_index,
        )
        if err:
            logger.debug("%s source catalog lookup failed during bulk sync: %s", log_kind, err)
            return None, None
        source_iface, err = resolve_interface_by_port_id(
            obj,
            port_id,
            server_key,
            expected_owner=source_expected_owner,
            index=source_index,
        )
        if err:
            logger.debug("%s source lookup failed during bulk sync: %s", log_kind, err)
            return None, None
        if require_interface_source and not isinstance(source_iface, Interface):
            return None, None  # VMInterface does not support lag

        _, err = resolve_interface_by_port_id(
            obj,
            related_port_id,
            server_key,
            name_hint=related_name,
            expected_owner=related_expected_owner,
            index=catalog_index,
        )
        if err:
            logger.debug("%s related catalog lookup failed during bulk sync: %s", log_kind, err)
            return None, None
        related_iface, err = resolve_interface_by_port_id(
            obj,
            related_port_id,
            server_key,
            name_hint=related_name,
            expected_owner=related_expected_owner,
            index=related_index,
        )
        if err:
            logger.debug("%s related lookup failed during bulk sync: %s", log_kind, err)
            return None, None
        return source_iface, related_iface

    def _apply_relationship_edge(self, source_iface, relation_field, related_iface, prepare_related, log_kind):
        """
        Set ``source_iface.<relation_field> = related_iface`` and persist, validating first.

        Thin bulk-pass wrapper over :func:`_apply_interface_relationship` (the shared
        set -> validate -> persist core, also used by the inline single-row endpoints). A
        validation failure is logged and skipped so the batch continues, never raised.
        ``prepare_related`` is the LAG pass's aggregate type=lag hook (returns
        ``(persist, restore)``); the parent pass passes None.
        """
        try:
            # Own savepoint: an IntegrityError from the persist poisons the enclosing batch
            # transaction ("current transaction is aborted" on every later row) unless the
            # failed statements are rolled back to a savepoint first — same reasoning as the
            # move-to-winner flow in migrate.py. It also keeps the pair atomic: a related-side
            # persist (LAG type bump) can't outlive a failed source save.
            with transaction.atomic():
                _apply_interface_relationship(source_iface, relation_field, related_iface, prepare_related)
        except ValidationError as exc:
            logger.warning(
                "Bulk sync: skipping invalid %s link %s -> %s: %s",
                log_kind,
                source_iface.name,
                related_iface.name,
                validation_error_detail(exc),
            )
            return
        except IntegrityError as exc:
            # Concurrent DB conflict (e.g. the related interface deleted between clean() and
            # existence check and the FK write): skip this row and keep the batch alive,
            # mirroring migrate.py's MoveInterfaceToWinnerView handling.
            logger.warning(
                "Bulk sync: skipping %s link %s -> %s due to a concurrent DB conflict: %s",
                log_kind,
                source_iface.name,
                related_iface.name,
                exc,
            )
            return
        logger.info("Bulk sync: set %s.%s = %s", source_iface.name, relation_field, related_iface.name)

    def sync_selected_interfaces(
        self,
        obj,
        ports_data,
        exclude_columns,
        interface_name_field,
        *,
        keep_locked_targets=False,
    ):
        """Create or update NetBox interfaces from LibreNMS port data."""
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        with transaction.atomic():
            if isinstance(obj, Device):
                locked_targets = self._lock_selected_device_targets(obj)
                obj = locked_targets.get(obj.pk)
                if obj is None:
                    for port in ports_data:
                        if normalize_librenms_port_id(port.get("port_id")) in selected_port_ids:
                            self._record_skipped_conflict(
                                port.get(interface_name_field),
                                "selected target unavailable",
                            )
                    return
                self._locked_target_devices = locked_targets
                self.object = obj
                self._auto_selected_target_ids = self._resolve_auto_selected_target_ids(
                    obj,
                    ports_data,
                    self._auto_selected_port_ids,
                    interface_name_field,
                    self._post_server_key,
                    members=list(locked_targets.values()),
                )
            elif isinstance(obj, VirtualMachine):
                obj = self.restricted_queryset(VirtualMachine).select_for_update(of=("self",)).filter(pk=obj.pk).first()
                if obj is None:
                    for port in ports_data:
                        if normalize_librenms_port_id(port.get("port_id")) in selected_port_ids:
                            self._record_skipped_conflict(
                                port.get(interface_name_field),
                                "selected target unavailable",
                            )
                    return
                self.object = obj
                vlan_scope_devices = [obj]
            if "vlans" not in exclude_columns:
                if isinstance(obj, Device):
                    vlan_scope_devices = self._selected_vlan_scope_devices(obj, ports_data, interface_name_field)
                self._prepare_vlan_lookup_maps(vlan_scope_devices)
            try:
                for port in ports_data:
                    # OOB-controller rows are merged into the host's interface list only for context
                    # (shared-LOM detection) and are never routed to a real target device by
                    # sync_interface(). They must not sync onto the host — and skipping them prevents
                    # a main/OOB interface-name collision (both "eth0") from double-processing one
                    # selection and overwriting the host interface with the OOB row's port_id/attrs.
                    if port.get("_source") == "oob":
                        continue
                    port_id = normalize_librenms_port_id(port.get("port_id"))

                    if port_id in selected_port_ids:
                        row_excludes = exclude_columns
                        if port_id in getattr(self, "_auto_selected_port_ids", set()) and "vlans" not in row_excludes:
                            row_excludes = [*row_excludes, "vlans"]
                        self.sync_interface(obj, port, row_excludes, interface_name_field)
            finally:
                if not keep_locked_targets:
                    self.__dict__.pop("_locked_target_devices", None)

    def _selected_vlan_scope_devices(self, obj, ports_data, interface_name_field):
        """Return the distinct locked owners whose selected rows will sync VLANs."""
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        auto_selected_port_ids = getattr(self, "_auto_selected_port_ids", set())
        owners = {}
        for port in ports_data:
            if port.get("_source") == "oob":
                continue
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if (
                port_id not in selected_port_ids
                or port_id in auto_selected_port_ids
                or syncable_interface_name(port, interface_name_field) is None
            ):
                continue
            owner = self._resolve_row_target_device(obj, port_id=port_id)
            if owner is not None:
                owners[owner.pk] = owner
        return list(owners.values())

    def _prepare_vlan_lookup_maps(self, vlan_scope_devices):
        """Build VLAN scope maps from owner rows locked for this sync transaction."""
        vlan_groups = self.get_vlan_groups_for_devices(vlan_scope_devices)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
        self._lookup_maps = lookup_maps
        self._lookup_maps_by_owner = {
            owner.pk: self.restrict_vlan_lookup_maps(
                lookup_maps,
                self.filter_vlan_groups_for_device(vlan_groups, owner),
            )
            for owner in vlan_scope_devices
        }
        self._vlan_owners_by_id = {owner.pk: owner for owner in vlan_scope_devices}

    def _lock_selected_device_targets(self, obj):
        """Lock the page Device and its current chassis scope in the shared lock order."""
        virtual_chassis_id = obj.virtual_chassis_id
        target_ids = {obj.pk}
        if virtual_chassis_id is not None:
            # The id came from obj, which this request already resolved through a scoped queryset.
            locked_chassis = relock_scoped_row(VirtualChassis, pk=virtual_chassis_id)
            if locked_chassis is None:
                return {}
            target_ids.update(Device.objects.filter(virtual_chassis_id=virtual_chassis_id).values_list("pk", flat=True))

        locked = {
            device.pk: device
            for device in self.restricted_queryset(Device)
            .select_for_update(of=("self",))
            .filter(pk__in=target_ids)
            .order_by("pk")
        }
        locked_obj = locked.get(obj.pk)
        if locked_obj is None or locked_obj.virtual_chassis_id != virtual_chassis_id:
            return {}
        if virtual_chassis_id is not None:
            locked = {
                device_id: device
                for device_id, device in locked.items()
                if device.virtual_chassis_id == virtual_chassis_id
            }
        return locked

    def _selected_row_target_id(self, port_id):
        """Return the target override keyed by the stable LibreNMS port ID."""
        port_id = normalize_librenms_port_id(port_id)
        if port_id is None:
            return None
        auto_targets = getattr(self, "_auto_selected_target_ids", {})
        if port_id in getattr(self, "_auto_selected_port_ids", set()):
            return auto_targets.get(port_id)
        return self.request.POST.get(f"device_selection_{port_id}") or auto_targets.get(port_id)

    def _resolve_row_target_device(self, obj, port_id=None):
        """
        Resolve the Device a given interface row syncs to.

        A stable port-ID-keyed override must identify an accessible VC member. An invalid, stale,
        or inaccessible explicit target returns ``None``. The relationship phase reuses this
        result so a LAG or parent link stays on the same owner as the synced row.

        Args:
            obj: The page Device (or VirtualMachine); returned as-is for VMs.
            port_id: The row's stable LibreNMS port_id, when known (keys the override).

        Returns:
            The selected VC member Device when valid, *obj* when no target was selected,
            or None when an explicit target is invalid or inaccessible.
        """
        if not isinstance(obj, Device):
            return obj
        normalized_port_id = normalize_librenms_port_id(port_id)
        if normalized_port_id in getattr(self, "_auto_selected_port_ids", set()) and normalized_port_id not in getattr(
            self, "_auto_selected_target_ids", {}
        ):
            return None
        selected_device_id = self._selected_row_target_id(port_id)
        if not selected_device_id:
            return obj
        try:
            # Scoped: the id comes from the POST, and VC membership proves where the device
            # sits, not that the caller's grant covers it.
            locked_targets = getattr(self, "_locked_target_devices", None)
            if locked_targets is None:
                target_device = self.restricted_queryset(Device).get(id=selected_device_id)
            else:
                target_device = locked_targets[int(selected_device_id)]
        except (Device.DoesNotExist, KeyError, ValueError, TypeError):
            return None
        # Both rows are current and locked in the HTTP sync path. Re-check that the
        # selected device is the page device or remains in the same virtual chassis.
        if target_device.id != obj.id and (
            obj.virtual_chassis_id is None or target_device.virtual_chassis_id != obj.virtual_chassis_id
        ):
            return None
        return target_device

    def sync_interface(self, obj, librenms_interface, exclude_columns, interface_name_field):
        """Create or update a single NetBox interface from LibreNMS data."""
        raw_interface_name = librenms_interface.get(interface_name_field)
        # update_interface_from_port bounds the name by the concrete writer model, so this gate
        # reads the same one; the default would let a name the writer refuses through.
        writer_model = VMInterface if isinstance(obj, VirtualMachine) else Interface
        interface_name = syncable_interface_name(librenms_interface, interface_name_field, writer_model)
        raw_port_id = librenms_interface.get("port_id")
        port_id = normalize_librenms_port_id(raw_port_id)
        lookup_port_id = raw_port_id if port_id is not None else None
        if interface_name is None:
            self._record_skipped_conflict(
                raw_interface_name,
                interface_name_rejection_reason(librenms_interface, interface_name_field, writer_model),
            )
            return

        if isinstance(obj, Device):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            target_device = self._resolve_row_target_device(obj, port_id=port_id)
            if target_device is None:
                # The user explicitly selected a target. If it is stale or outside the
                # caller's grant, do not silently sync the row onto the page device.
                self._record_skipped_conflict(interface_name, "selected target unavailable")
                return
            interface = self._resolve_device_interface(target_device, interface_name, lookup_port_id, server_key)
        elif isinstance(obj, VirtualMachine):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            interface = self._resolve_vm_interface(obj, interface_name, lookup_port_id, server_key)
        else:
            raise ValueError("Invalid object type.")

        if interface is None:
            logger.warning(
                "Skipping interface sync for '%s': unable to resolve target interface safely (port_id=%r).",
                interface_name,
                port_id,
            )
            # Record for the user-facing summary in post(). Defensive getattr: sync_interface
            # may be exercised directly (without post() initialising the list).
            self._record_skipped_conflict(interface_name, "port already mapped elsewhere or ambiguous")
            return

        # An interface resolved and is being synced — count it explicitly (defensive getattr:
        # sync_interface may be exercised directly without post() initialising the counter). The
        # count, not a skip-vs-selected size comparison, drives the success banner in post().
        if getattr(self, "_synced_count", None) is not None:
            self._synced_count += 1

        netbox_type = None
        if isinstance(obj, Device):
            netbox_type = self.get_netbox_interface_type(librenms_interface)

        self.update_interface_attributes(
            interface,
            librenms_interface,
            netbox_type,
            exclude_columns,
            interface_name_field,
        )

        # Sync VLANs if not excluded
        if "vlans" not in exclude_columns:
            self._sync_interface_vlans(interface, librenms_interface)

    def _record_skipped_conflict(self, interface_name, reason):
        """Record a row that cannot be synced to its requested target."""
        skipped = getattr(self, "_skipped_conflicts", None)
        if skipped is not None:
            skipped.append(f"{interface_name or '(unnamed)'} ({reason})")

    def _resolve_device_interface(self, target_device, interface_name, port_id, server_key):
        """Resolve a device interface using port_id first, then safe name fallback."""
        changeable = self.restricted_queryset(Interface, "change")
        if port_id:
            try:
                by_id = find_by_librenms_id(Interface, port_id, server_key)
            except AmbiguousLibreNMSIdError:
                # port_id matches multiple interfaces — skip this row rather than bind
                # to an arbitrary one (the caller records the skip).
                logger.warning("Skipping interface row — port_id %s is ambiguous (multiple matches).", port_id)
                return None
            if by_id is not None:
                if not changeable.filter(pk=by_id.pk).exists():
                    return None
                if by_id.device_id == target_device.id:
                    return by_id
                # The port_id resolves to an interface on a DIFFERENT device (a stale or
                # duplicate stored port_id, e.g. after a device replacement). The LibreNMS row
                # still describes THIS device's interface, and the rendered table binds it to the
                # current device's same-named interface, so fall back to that and update it —
                # but only if it already exists. Don't get_or_create here: spawning a new
                # interface when the id really belongs elsewhere would create a duplicate.
                # update_interface_attributes won't reassign the port_id off the other
                # interface (its existing_owner guard), so the foreign binding stays intact.
                existing_by_name = Interface.objects.filter(device=target_device, name=interface_name).first()
                if existing_by_name:
                    return (
                        existing_by_name
                        if interface_name_fallback_matches_port(existing_by_name, port_id, server_key)
                        and changeable.filter(pk=existing_by_name.pk).exists()
                        else None
                    )
                return None
        interface, created = Interface.objects.get_or_create(device=target_device, name=interface_name)
        if not created and port_id and not interface_name_fallback_matches_port(interface, port_id, server_key):
            return None
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def _resolve_vm_interface(self, vm, interface_name, port_id, server_key):
        """Resolve a VM interface using port_id first, then safe name fallback."""
        changeable = self.restricted_queryset(VMInterface, "change")
        if port_id:
            try:
                by_id = find_by_librenms_id(VMInterface, port_id, server_key)
            except AmbiguousLibreNMSIdError:
                logger.warning("Skipping VM interface row — port_id %s is ambiguous (multiple matches).", port_id)
                return None
            if by_id is not None:
                if not changeable.filter(pk=by_id.pk).exists():
                    return None
                if by_id.virtual_machine_id == vm.id:
                    return by_id
                # The port_id resolves to an interface on a DIFFERENT VM (a stale or duplicate
                # stored port_id). The LibreNMS row still describes THIS VM's interface, and the
                # rendered table binds it to this VM's same-named interface, so fall back to that
                # and update it — but only if it already exists (don't get_or_create a duplicate
                # for an id that really belongs elsewhere). update_interface_attributes won't
                # reassign the port_id off the other interface (its existing_owner guard).
                existing_by_name = VMInterface.objects.filter(virtual_machine=vm, name=interface_name).first()
                if existing_by_name:
                    return (
                        existing_by_name
                        if interface_name_fallback_matches_port(existing_by_name, port_id, server_key)
                        and changeable.filter(pk=existing_by_name.pk).exists()
                        else None
                    )
                return None
        interface, created = VMInterface.objects.get_or_create(virtual_machine=vm, name=interface_name)
        if not created and port_id and not interface_name_fallback_matches_port(interface, port_id, server_key):
            return None
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def get_netbox_interface_type(self, librenms_interface):
        """Return the NetBox interface type mapped from LibreNMS type and speed."""
        return get_netbox_interface_type(librenms_interface, speed_converter=convert_speed_to_kbps)

    def handle_mac_address(self, interface, ifPhysAddress):
        """Assign or create the MAC address for the given interface."""
        assign_interface_mac(interface, ifPhysAddress)

    def update_interface_attributes(
        self,
        interface,
        librenms_interface,
        netbox_type,
        exclude_columns,
        interface_name_field,
    ):
        """Update interface fields from LibreNMS data, respecting excluded columns."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        update_interface_from_port(
            interface,
            librenms_interface,
            server_key=server_key,
            interface_name_field=interface_name_field,
            exclude_columns=exclude_columns,
            netbox_type=netbox_type,
            speed_converter=convert_speed_to_kbps,
        )

    def _sync_interface_vlans(self, interface, librenms_port):
        """
        Sync VLAN assignments from LibreNMS to NetBox interface.
        Sets mode, untagged_vlan, and tagged_vlans based on LibreNMS data.

        Args:
            interface: NetBox Interface or VMInterface object
            librenms_port: Port data dict from LibreNMS with VLAN info
        """
        port_id = normalize_librenms_port_id(librenms_port.get("port_id"))

        # Build VLAN data from port
        vlan_data = {
            "untagged_vlan": librenms_port.get("untagged_vlan"),
            "tagged_vlans": librenms_port.get("tagged_vlans", []),
        }

        # Build per-VLAN group map from POST data
        vlan_group_map = {}
        all_vids = []
        if vlan_data["untagged_vlan"]:
            all_vids.append(str(vlan_data["untagged_vlan"]))
        for vid in vlan_data.get("tagged_vlans", []):
            all_vids.append(str(vid))

        for vid in all_vids:
            group_id = self.request.POST.get(f"vlan_group_{port_id}_{vid}", "")
            if group_id:
                vlan_group_map[vid] = group_id

        # Use mixin method to update interface VLAN assignments
        owner_id = getattr(interface, "device_id", None) or getattr(interface, "virtual_machine_id", None)
        lookup_maps_by_owner = getattr(self, "_lookup_maps_by_owner", None)
        if lookup_maps_by_owner is not None:
            lookup_maps = lookup_maps_by_owner.get(owner_id)
            if lookup_maps is None:
                logger.warning("Skipping VLAN sync for %s because its locked owner has no VLAN scope map", interface)
                return
        else:
            lookup_maps = self._lookup_maps
        owner = getattr(self, "_vlan_owners_by_id", {}).get(owner_id)
        for vid, group_id in list(vlan_group_map.items()):
            try:
                vid_int = int(vid)
            except (TypeError, ValueError):
                # The VID comes from the cached LibreNMS payload, which is only checked for being
                # a dict. A non-numeric value here would abort the whole sync transaction.
                logger.warning("Skipping VLAN group selection for non-numeric VID %r on %s", vid, interface)
                vlan_group_map.pop(vid, None)
                continue
            try:
                group_id_int = int(group_id)
            except (TypeError, ValueError):
                group_id_int = None
            if group_id_int is not None and (vid_int, group_id_int) in lookup_maps.get("vid_group_to_vlan", {}):
                continue

            groups = lookup_maps.get("vid_to_groups", {}).get(vid_int, [])
            selected_group = groups[0] if len(groups) == 1 else self._select_most_specific_group(groups, owner)
            if selected_group is None:
                vlan_group_map.pop(vid, None)
            else:
                vlan_group_map[vid] = str(selected_group.pk)
        self._update_interface_vlan_assignment(interface, vlan_data, vlan_group_map, lookup_maps)


class DeleteNetBoxInterfacesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """Delete interfaces that exist only in NetBox."""

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        # The owner is resolved through a restricted queryset, so its view permission is stated
        # here too (mirroring SyncInterfacesView): a missing grant is a 403, not a bare 404.
        if object_type == "device":
            return [("view", Device), ("delete", Interface)]
        elif object_type == "virtualmachine":
            return [("view", VirtualMachine), ("delete", VMInterface)]
        else:
            raise Http404(f"Invalid object type: {object_type}")

    def post(self, request, object_type, object_id):
        """Delete selected NetBox-only interfaces not present in LibreNMS."""
        # Set permissions dynamically based on object type
        self.required_object_permissions = {
            "POST": self.get_required_permissions_for_object_type(object_type),
        }

        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions_json("POST"):
            return error

        if object_type == "device":
            obj = self.restrict_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            obj = self.restrict_object_or_404(VirtualMachine, pk=object_id)
        else:
            return JsonResponse({"error": "Invalid object type"}, status=400)

        interface_ids = request.POST.getlist("interface_ids")

        if not interface_ids:
            return JsonResponse({"error": "No interfaces selected for deletion"}, status=400)

        deleted_count = 0
        errors = []
        interface_name = None

        try:
            with transaction.atomic():
                for interface_id in interface_ids:
                    interface_name = None
                    try:
                        with transaction.atomic():
                            if object_type == "device":
                                # Scoped by "delete": the ownership checks below prove where the
                                # interface sits, not that the grant covers it.
                                interface = self.restricted_queryset(Interface, "delete").get(id=interface_id)
                                interface_name = interface.name
                                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                                    valid_device_ids = [member.id for member in obj.virtual_chassis.members.all()]
                                    if interface.device_id not in valid_device_ids:
                                        errors.append(
                                            "Interface {} does not belong to this device or its virtual chassis".format(
                                                interface.name
                                            )
                                        )
                                        continue
                                elif interface.device_id != obj.id:
                                    errors.append(f"Interface {interface.name} does not belong to this device")
                                    continue
                            else:
                                interface = self.restricted_queryset(VMInterface, "delete").get(id=interface_id)
                                interface_name = interface.name
                                if interface.virtual_machine_id != obj.id:
                                    errors.append(f"Interface {interface.name} does not belong to this virtual machine")
                                    continue

                            interface.delete()
                        deleted_count += 1

                    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
                        errors.append(f"Interface with ID {interface_id} not found")
                        continue
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("Failed to delete interface %s", interface_name or interface_id)
                        errors.append(f"Error deleting interface {interface_name or interface_id}. Check server logs.")
                        continue

        except Exception:  # pragma: no cover
            logger.exception("DeleteNetBoxInterfacesView transaction failed")
            return JsonResponse({"error": "Transaction failed. Please check server logs."}, status=500)

        response_data = {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} interface(s)",
        }

        if errors:
            response_data["errors"] = errors
            response_data["message"] += f" with {len(errors)} error(s)"

        return JsonResponse(response_data)


def _lock_relationship_scope(obj, owner_queryset=None):
    """Lock an object's current relationship scope and recheck owner visibility."""
    if isinstance(obj, Device):
        virtual_chassis_id = obj.virtual_chassis_id
        device_ids = {obj.pk}
        if virtual_chassis_id is not None:
            # The id came from obj, which this request already resolved through a scoped queryset.
            locked_chassis = relock_scoped_row(VirtualChassis, pk=virtual_chassis_id)
            if locked_chassis is None:
                return None, set()
            device_ids.update(Device.objects.filter(virtual_chassis_id=virtual_chassis_id).values_list("pk", flat=True))
        locked = {
            device.pk: device
            for device in Device.objects.select_for_update(of=("self",)).filter(pk__in=device_ids).order_by("pk")
        }
        locked_obj = locked.get(obj.pk)
        if locked_obj is None or locked_obj.virtual_chassis_id != virtual_chassis_id:
            return None, set()
        if owner_queryset is not None and not owner_queryset.filter(pk=locked_obj.pk).exists():
            return None, set()
        return locked_obj, set(locked)
    if isinstance(obj, VirtualMachine):
        locked_obj = VirtualMachine.objects.select_for_update(of=("self",)).filter(pk=obj.pk).first()
        if (
            locked_obj is not None
            and owner_queryset is not None
            and not owner_queryset.filter(pk=locked_obj.pk).exists()
        ):
            return None, set()
        return locked_obj, set()
    return None, set()


def _build_locked_relationship_indexes(
    obj,
    server_key,
    user,
    locked_device_ids,
    *,
    candidate_q=None,
    candidate_ids=None,
):
    """Lock candidate interfaces, then derive permission indexes from their locked state."""
    if candidate_ids is None:
        candidate_queryset = interface_queryset_for_object(obj).filter(candidate_q)
        candidate_ids = set(candidate_queryset.values_list("pk", flat=True))
    else:
        candidate_ids = set(candidate_ids)
    catalog_index = build_interface_index(
        obj,
        server_key,
        allowed_ids=candidate_ids,
    )
    if isinstance(obj, Device):
        locked_ids = {
            interface.pk
            for interfaces in catalog_index["by_name"].values()
            for interface in interfaces
            if interface.device_id in locked_device_ids
        }
        catalog_index = filter_interface_index(catalog_index, locked_ids)
        candidate_ids &= locked_ids

    # A constrained grant can stop matching while this transaction waits for a candidate
    # row lock. Lock only rows the user could act on before the wait, then evaluate the grant
    # again from their locked state. The catalog stays unfiltered so hidden duplicate IDs and
    # names still make resolution fail closed without locking rows that were never permitted.
    permission_candidates = interface_queryset_for_object(obj).filter(pk__in=candidate_ids)
    if isinstance(obj, Device):
        actionable_owner_ids = set(
            Device.objects.restrict(user, "view").filter(pk__in=locked_device_ids).values_list("pk", flat=True)
        )
        permission_candidates = permission_candidates.filter(device_id__in=actionable_owner_ids)
    prelock_viewable_ids = set(permission_candidates.restrict(user, "view").values_list("pk", flat=True))
    prelock_changeable_ids = set(permission_candidates.restrict(user, "change").values_list("pk", flat=True))
    prelock_permitted_ids = prelock_viewable_ids | prelock_changeable_ids
    locked_index = build_interface_index(
        obj,
        server_key,
        lock=True,
        allowed_ids=prelock_permitted_ids,
    )
    locked_candidates = interface_queryset_for_object(obj).filter(pk__in=prelock_permitted_ids)
    viewable_ids = set(locked_candidates.restrict(user, "view").values_list("pk", flat=True))
    changeable_ids = set(locked_candidates.restrict(user, "change").values_list("pk", flat=True))
    related_index = filter_interface_index(locked_index, viewable_ids | changeable_ids)
    source_index = filter_interface_index(related_index, changeable_ids)
    return catalog_index, source_index, related_index, changeable_ids


def _promote_lag_aggregate(agg, *, with_restore):
    """
    Bump a LAG aggregate to ``type=lag`` in memory so a member's ``clean()`` accepts the link.

    Single home for the "promote aggregate to type=lag, persist only that column" rule shared by the
    bulk LAG pass (``SyncInterfacesView._prepare_bulk_lag_aggregate``) and the single-row LAG
    endpoint (``SyncInterfaceLagView._prepare_related``) so they can't drift on the promotion or the
    save fields. Returns None when *agg* isn't an Interface or is already ``type=lag``.

    The persist saves ONLY the ``type`` column (``update_fields=["type"]``) so a concurrent edit to
    the aggregate's other fields — loaded into the shared interface index outside the row lock — is
    not clobbered.

    Args:
        agg: The aggregate interface to promote.
        with_restore (bool): When True (the bulk pass, which reuses the aggregate across member
            rows), return a ``(persist, restore)`` pair — ``restore`` reverts the in-memory type so
            a later valid member sharing this aggregate still saves it if an earlier member's link
            failed validation. When False, return the bare ``persist`` callable.

    Returns:
        callable | tuple | None: ``persist`` (or ``(persist, restore)``), or None when nothing to do.
    """
    if not (isinstance(agg, Interface) and agg.type != "lag"):
        return None
    original_type = agg.type
    agg.type = "lag"

    def _persist():
        agg.save(update_fields=["type"])
        logger.info("Set interface %s type=lag", agg.name)

    if with_restore:
        return (_persist, lambda: setattr(agg, "type", original_type))
    return _persist


def _validate_relationship(source_iface, relation_field, related_iface):
    """
    Run NetBox's model validation for the new relationship FK.

    NetBox 4.4.x reads ``self.parent.virtual_chassis`` when the parent sits on another device.
    ``Interface`` has no such attribute (4.6 reads ``self.device.virtual_chassis``), so the
    validation NetBox means to run raises AttributeError instead. Tolerate it only for the edge
    that comparison exists to allow, two interfaces on members of one virtual chassis, and only
    when the failure really is that attribute.

    Args:
        source_iface: The interface whose FK was set.
        relation_field: The FK attribute that changed (``"lag"`` | ``"parent"``).
        related_iface: The interface the FK now points at.

    Raises:
        ValidationError: when NetBox rejects the relationship.
        AttributeError: any failure that is not the 4.4.x cross-chassis parent bug.
    """
    try:
        source_iface.clean()
    except AttributeError as exc:
        source_chassis = getattr(getattr(source_iface, "device", None), "virtual_chassis_id", None)
        related_chassis = getattr(getattr(related_iface, "device", None), "virtual_chassis_id", None)
        if not (
            relation_field == "parent"
            # exc.name is the attribute the failed access asked for (Python 3.10+), so this
            # matches the one dereference rather than any message mentioning it.
            and getattr(exc, "name", None) == "virtual_chassis"
            and source_chassis is not None
            and source_chassis == related_chassis
            and netbox_clean_reads_parent_virtual_chassis()
        ):
            raise
        logger.debug(
            "Interface %s: this NetBox cannot validate a parent on another chassis member; "
            "both interfaces belong to virtual chassis %s, so the edge is accepted.",
            source_iface.name,
            source_chassis,
        )


def _apply_interface_relationship(source_iface, relation_field, related_iface, prepare_related=None):
    """
    Set ``source_iface.<relation_field> = related_iface``, validate, and persist both sides.

    The single place the relationship set -> validate -> persist sequence lives, shared by the
    bulk pass (:meth:`SyncInterfacesView._apply_relationship_edge`) and the inline single-row
    endpoints (:class:`_BaseRelationshipSyncView`) so a fix applies once, not twice.

    ``prepare_related`` may mutate the related interface in memory before validation (e.g. bump
    an aggregate to ``type=lag``) and return either a ``persist`` callable or a
    ``(persist, restore)`` pair: ``persist`` runs only after the source validates, and
    ``restore`` undoes the in-memory mutation when validation fails — needed when the related
    object is reused across calls (a shared aggregate in the bulk pass).

    Both rows are persisted with ``update_fields`` so a concurrent edit to their other columns
    isn't clobbered: the objects may have been loaded into a shared index outside any row lock,
    so a full ``save()`` of the stale instance would lose-update the concurrent write.

    Raises:
        ValidationError: when the source fails ``clean()`` (after restoring the related
            mutation); the caller decides how to surface it (bulk logs+skips, single-row 409).
    """
    # Capture the source's original FK before mutating: source_iface (and the aggregate) are
    # reused across rows via the shared interface index, so a failed attempt must leave BOTH
    # unmutated. Otherwise a later edge validates source_iface against the rolled-back (but
    # still in-memory) FK, or — because the aggregate already looks type=lag in memory — a later
    # member sharing it skips the type bump and never persists it, leaving the DB type stale.
    relation_id_field = f"{relation_field}_id"
    original_related_id = getattr(source_iface, relation_id_field)
    setattr(source_iface, relation_field, related_iface)
    prepared = prepare_related(related_iface) if prepare_related else None
    if isinstance(prepared, tuple):
        persist_related, restore_related = prepared
    else:
        persist_related, restore_related = prepared, None

    def _restore_in_memory():
        setattr(source_iface, relation_id_field, original_related_id)
        if restore_related:
            restore_related()

    try:
        # These are existing, DB-valid rows and this path changes only one relationship FK.
        # NetBox's model clean() contains the cross-owner/type/self-link rules that matter here.
        # Running full_clean() would revalidate every unchanged FK and uniqueness constraint,
        # adding several SELECTs per edge while all relationship rows remain locked.
        _validate_relationship(source_iface, relation_field, related_iface)
        if persist_related:
            persist_related()
        source_iface.save(update_fields=[relation_field])
    except (ValidationError, IntegrityError):
        # clean() rejection OR a statement-time persist failure (the savepoint rolls back
        # the DB, but the in-memory instances stay mutated): undo both before the caller skips
        # this row and continues the batch against the shared index.
        _restore_in_memory()
        raise


class _BaseRelationshipSyncView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """
    Shared skeleton for the inline single-row relationship-sync endpoints (LAG / parent).

    SyncInterfaceLagView and SyncInterfaceParentView share the permission gate, current-cache
    edge validation, stable port ID resolution, and one transactional write path. They differ
    only in the FK attribute set, the POST field/label wording, VM support, and the LAG-only
    aggregate type bump. Keeping
    one flow here stops the two endpoints drifting (a fix to the resolve/validate/persist
    sequence applies once, not twice).

    Subclass contract (class attributes):
        relation_field   -- the Interface FK attribute set ("lag" | "parent").
        related_port_param -- the POST field carrying the related port ID.
        relation_label   -- human label in messages ("LAG" | "parent").
        source_label / related_label -- the two interfaces' roles ("Member"/"Aggregate",
            "Child"/"Parent"), used in the resolution error prefixes.
        supports_vm      -- whether VMInterface is a valid target (parent: yes; lag: no,
            VMInterface has no `lag` field).
    """

    relation_field: str
    related_port_param: str
    relation_label: str
    source_label: str
    related_label: str
    supports_vm: bool = False

    @staticmethod
    def _migrated_donor_error(obj, server_key):
        """Return a conflict response when a migrated Device is read-only."""
        if isinstance(obj, Device) and build_migrated_context(obj, server_key).get("migrated_to_marker"):
            return JsonResponse(
                {"error": "This LibreNMS source has been migrated and is read-only."},
                status=409,
            )
        return None

    def _required_permissions(self, object_type):
        """Object-type-scoped POST permissions; raise Http404 for an unsupported type."""
        if object_type == "device":
            return {"POST": [("view", Device), ("change", Interface)]}
        if object_type == "virtualmachine" and self.supports_vm:
            return {"POST": [("view", VirtualMachine), ("change", VMInterface)]}
        if object_type == "virtualmachine":
            # VMInterface has no `lag` field, so LAG membership sync is device-only. Reject up
            # front rather than resolving a VM and failing later on a mismatched permission.
            raise Http404(f"{self.relation_label} sync is only supported for device interfaces.")
        raise Http404("Invalid object type.")

    def _get_object(self, object_type, object_id):
        # restrict_object_or_404, not get_object_or_404: the POST gate above clears a CONSTRAINED
        # change grant (has_perm is asked without an instance), so a raw pk lookup would resolve a
        # device the user may not see. An out-of-scope id 404s like a nonexistent one.
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        if object_type == "virtualmachine" and self.supports_vm:
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        raise Http404("Invalid object type.")

    def _prepare_related(self, related_iface):
        """
        Hook: mutate the related interface in memory before the source is validated.

        Returns a no-arg callable that persists that mutation (invoked only after the source
        interface validates) or None when there's nothing to do. SyncInterfaceLagView
        overrides this to bump the aggregate's type to 'lag'; parent has no equivalent.
        """
        return None

    def _get_current_edge(self, obj, server_key, request, port_id, related_port_id):
        """Return the current cached edge rows and safe name hints, or ``None`` when stale."""
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        cached_data = cache.get(self.get_cache_key(cache_obj, "ports", server_key))
        if not isinstance(cached_data, dict):
            return None
        ports = cached_data.get("ports")
        relationships = cached_data.get("port_stack_relationships")
        if not is_list_of_dicts(ports) or not isinstance(relationships, dict):
            return None
        raw_edges = relationships.get("lag_members" if self.relation_field == "lag" else "sub_interfaces")
        if not isinstance(raw_edges, dict):
            return None

        lag_members, sub_interfaces = normalize_relationship_maps(relationships)
        edges = lag_members if self.relation_field == "lag" else sub_interfaces
        source_id = normalize_librenms_port_id(port_id)
        related_id = normalize_librenms_port_id(related_port_id)
        if source_id is None or related_id is None or edges.get(source_id) != related_id:
            return None

        ports_by_id = {}
        duplicate_port_ids = set()
        for port in ports:
            normalized_id = normalize_librenms_port_id(port.get("port_id"))
            if port.get("_source") == "oob" or normalized_id is None:
                continue
            if normalized_id in ports_by_id:
                duplicate_port_ids.add(normalized_id)
            else:
                ports_by_id[normalized_id] = port
        if source_id in duplicate_port_ids or related_id in duplicate_port_ids:
            return None
        source_port = ports_by_id.get(source_id)
        related_port = ports_by_id.get(related_id)
        if source_port is None or related_port is None:
            return None

        interface_name_field = get_interface_name_field(request, obj)
        _unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(ports, interface_name_field)
        source_name = ""
        if source_id in unambiguous_name_port_ids:
            source_name = source_port.get(interface_name_field) or ""
        related_name = ""
        if related_id in unambiguous_name_port_ids:
            related_name = related_port.get(interface_name_field) or ""
        return source_port, related_port, source_name, related_name, interface_name_field

    def post(self, request, object_type, object_id):
        # Set the object-type-scoped permissions BEFORE the gate (an unsupported type raises
        # Http404 here). JSON endpoint: require_all_permissions would return the mixin's
        # HTML/redirect on denial, breaking the fetch() caller, so use the _json variant.
        self.required_object_permissions = self._required_permissions(object_type)
        if error := self.require_all_permissions_json("POST"):
            return error

        obj = self._get_object(object_type, object_id)
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            return JsonResponse({"error": "Selected LibreNMS server is no longer configured."}, status=400)
        if error := self._migrated_donor_error(obj, server_key):
            return error
        port_id = request.POST.get("port_id", "").strip()
        related_port_id = request.POST.get(self.related_port_param, "").strip()

        if not port_id or not related_port_id:
            return JsonResponse({"error": f"port_id and {self.related_port_param} are required"}, status=400)

        current_edge = self._get_current_edge(obj, server_key, request, port_id, related_port_id)
        if current_edge is None:
            return JsonResponse(
                {"error": "The LibreNMS relationship changed or expired. Refresh and retry."},
                status=409,
            )
        source_port, related_port, source_name, related_name, _interface_name_field = current_edge

        # The IntegrityError wrapper sits OUTSIDE the atomic: a concurrent conflict (e.g. the
        # related interface deleted in the validate/write TOCTOU window) raises either at the
        # failed statement — propagating out of the atomic after rollback — or, for Django's
        # INITIALLY DEFERRED Postgres FKs, only at the atomic's COMMIT. Both land here and
        # become a JSON 409 instead of an unhandled 500 to the fetch() caller, mirroring the
        # bulk pass (_apply_relationship_edge).
        source_iface = None
        related_iface = None
        try:
            with transaction.atomic():
                obj, locked_device_ids = _lock_relationship_scope(
                    obj,
                    self.restricted_queryset(type(obj)),
                )
                if obj is None:
                    return JsonResponse(
                        {"error": "The interface owner changed concurrently. Refresh and retry."},
                        status=409,
                    )
                if error := self._migrated_donor_error(obj, server_key):
                    return error

                candidate_q = relationship_candidate_q(
                    server_key,
                    (source_port.get("port_id"), related_port.get("port_id")),
                    (source_name, related_name),
                )
                catalog_index, source_index, related_index, changeable_ids = _build_locked_relationship_indexes(
                    obj,
                    server_key,
                    request.user,
                    locked_device_ids,
                    candidate_q=candidate_q,
                )

                _, err = resolve_interface_by_port_id(
                    obj,
                    port_id,
                    server_key,
                    name_hint=source_name,
                    expected_owner=interface_owner_for_object(obj),
                    index=catalog_index,
                )
                if err:
                    return JsonResponse({"error": f"{self.source_label} interface: {err}"}, status=404)
                source_iface, err = resolve_interface_by_port_id(
                    obj,
                    port_id,
                    server_key,
                    name_hint=source_name,
                    expected_owner=interface_owner_for_object(obj),
                    index=source_index,
                )
                if err:
                    return JsonResponse({"error": f"{self.source_label} interface: {err}"}, status=404)

                _, err = resolve_interface_by_port_id(
                    obj,
                    related_port_id,
                    server_key,
                    name_hint=related_name,
                    index=catalog_index,
                )
                if err:
                    return JsonResponse({"error": f"{self.related_label} interface: {err}"}, status=404)
                related_iface, err = resolve_interface_by_port_id(
                    obj,
                    related_port_id,
                    server_key,
                    name_hint=related_name,
                    index=related_index,
                )
                if err:
                    return JsonResponse({"error": f"{self.related_label} interface: {err}"}, status=404)
                if (
                    self.relation_field == "lag"
                    and related_iface.type != "lag"
                    and related_iface.pk not in changeable_ids
                ):
                    return JsonResponse(
                        {"error": "Aggregate interface cannot be changed to type LAG."},
                        status=403,
                    )

                # Validate before persisting: a crafted POST with port_id == related_port_id
                # resolves source == related, so clean() rejects the resulting
                # self-relationship. The shared helper sets the FK, runs
                # _prepare_related (e.g. the aggregate's type=lag, persisted only on success), and
                # saves with update_fields.
                try:
                    _apply_interface_relationship(
                        source_iface, self.relation_field, related_iface, self._prepare_related
                    )
                except ValidationError as exc:
                    # Log the validation detail server-side and return a fixed message — don't echo
                    # exception text to the client (CodeQL py/stack-trace-exposure). The
                    # detail can include a self-link, incompatible types, or invalid chassis scope.
                    logger.warning(
                        "%s link validation failed (%s -> %s): %s",
                        self.relation_label,
                        source_iface.name,
                        related_iface.name,
                        validation_error_detail(exc),
                    )
                    return JsonResponse(
                        {
                            "error": (
                                f"Cannot link {source_iface.name} to {self.relation_label} {related_iface.name}: "
                                f"NetBox rejected the {self.relation_label} relationship. Check the interface "
                                "types, chassis membership, and that the two interfaces are not the same interface."
                            )
                        },
                        status=409,
                    )
                logger.info("Set %s.%s = %s", source_iface.name, self.relation_field, related_iface.name)
        except IntegrityError as exc:
            source_name = getattr(source_iface, "name", self.source_label.lower())
            related_name = getattr(related_iface, "name", self.related_label.lower())
            logger.warning(
                "%s link hit a concurrent DB conflict (%s -> %s): %s",
                self.relation_label,
                source_name,
                related_name,
                exc,
            )
            return JsonResponse(
                {
                    "error": (
                        f"Cannot link {source_name} to {self.relation_label} {related_name}: "
                        "a concurrent change interrupted the update. Refresh and retry."
                    )
                },
                status=409,
            )

        return JsonResponse(
            {
                "status": "success",
                "message": f"Linked {source_iface.name} to {self.relation_label} {related_iface.name}",
            }
        )


class SyncInterfaceLagView(_BaseRelationshipSyncView):
    """Set Interface.lag (member -> aggregate) based on LibreNMS port_stack data."""

    # Permissions are resolved per object_type in the shared post().
    relation_field = "lag"
    related_port_param = "lag_port_id"
    relation_label = "LAG"
    source_label = "Member"
    related_label = "Aggregate"
    supports_vm = False  # VMInterface has no `lag` field

    def _prepare_related(self, related_iface):
        """Promote the aggregate to type=lag so member_iface.clean() accepts the link."""
        # Single-row endpoint: no aggregate reuse across rows, so no restore needed.
        return _promote_lag_aggregate(related_iface, with_restore=False)


class SyncInterfaceParentView(_BaseRelationshipSyncView):
    """Set Interface.parent (sub-interface -> parent) based on LibreNMS port_stack data."""

    # Both Devices (Interface) and VMs (VMInterface, which also has a parent field) are
    # supported; permissions are resolved per object_type in the shared post().
    relation_field = "parent"
    related_port_param = "parent_port_id"
    relation_label = "parent"
    source_label = "Child"
    related_label = "Parent"
    supports_vm = True
