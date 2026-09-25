import logging
from dataclasses import dataclass
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

from netbox_librenms_plugin.constants import (
    HOST_NAME_COLLISION_REASON,
    NAME_OWNER_STALE,
    OOB_INVENTORY_SOURCE,
    PORT_ID_SOURCE_COLLISION_REASON,
    REPORTED_NAME_PORT_COLLISION_REASON,
)
from netbox_librenms_plugin.interface_relationships import (
    build_interface_index,
    filter_interface_index,
    interface_owner_for_object,
    interface_queryset_for_object,
    relationship_candidate_ids,
    relationship_candidate_q,
    resolve_interface_by_port_id,
)
from netbox_librenms_plugin.interface_rules import (
    PortSyncBlocked,
    RuleDecisionKind,
    decision_reason,
    interface_rules_for_request,
    rule_names,
)
from netbox_librenms_plugin.interface_sync import (
    interface_owner_platform_id,
    update_interface_from_port,
)
from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    apply_transition_to_response,
    schedule_request_cache_mutation,
)
from netbox_librenms_plugin.transactions import run_transaction
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    LibreNMSPortBindingConflict,
    claim_librenms_port_binding,
    build_migrated_context,
    coerce_model_pk,
    convert_speed_to_kbps,
    find_interface_by_librenms_port_id,
    get_interface_name_field,
    get_interface_port_identity_sets,
    get_librenms_device_id,
    get_librenms_sync_device,
    get_migrated_to_marker,
    interface_name_fallback_matches_port,
    is_list_of_dicts,
    netbox_interface_clean,
    normalize_librenms_port_id,
    normalize_relationship_maps,
    reported_name_owners,
    resolve_interface_row_device,
    set_librenms_device_id,
    syncable_interface_name,
    synced_interface_names,
    validation_error_detail,
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


class _ConflictingRowTargetError(Exception):
    """A sync POST names more than one Virtual Chassis member for one row."""


class _DuplicatedSelectionError(Exception):
    """The related-row walk reached a port ID the cached snapshot holds more than once."""


_DUPLICATED_SELECTION_MESSAGE = (
    "Selected LibreNMS port IDs are duplicated in the cached interface data. "
    "Refresh LibreNMS data and resolve the duplicate IDs before syncing."
)


def _duplicated_port_ids(ports_data):
    """Return the port IDs the snapshot holds more than once, or on both the host and the OOB side."""
    host_port_id_counts = {}
    port_id_sources = {}
    for port in ports_data:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            is_oob = port.get("_source") == OOB_INVENTORY_SOURCE
            port_id_sources.setdefault(port_id, set()).add(is_oob)
            if not is_oob:
                host_port_id_counts[port_id] = host_port_id_counts.get(port_id, 0) + 1
    return {
        port_id
        for port_id, sources in port_id_sources.items()
        if host_port_id_counts.get(port_id, 0) > 1 or len(sources) > 1
    }


@dataclass(frozen=True)
class _SnapshotNameDecisions:
    """The writer's per-row targets, names, rejections and name holders for one snapshot."""

    target_device_ids: dict
    names: dict
    rejected: dict
    owners: dict


@dataclass(frozen=True)
class _BulkRelationshipContext:
    """Shared immutable inputs for one locked bulk relationship pass."""

    obj: object
    port_by_id: dict
    lag_members: dict
    sub_interfaces: dict
    bridge_members: dict
    catalog_index: dict
    source_index: dict
    related_index: dict
    changeable_ids: set
    server_key: str
    interface_name_field: str
    unique_host_port_ids: set
    unambiguous_name_port_ids: set
    excluded_columns: set


class _HostInterfaceNameConflict(Exception):
    """An OOB row cannot claim a host interface by its name."""


@dataclass(frozen=True)
class _InterfaceSyncOutcome:
    """What one committed sync attempt reports; ``post()`` publishes it after the transaction."""

    skipped_conflicts: tuple
    kept_name_conflicts: tuple
    synced_count: int
    mutated: bool
    warnings: tuple


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

        obj = self.get_object(object_type, object_id)
        self.object = obj  # Store for use in sync methods

        interface_name_field = get_interface_name_field(request, obj)
        self.interface_name_field = interface_name_field

        # Rebind the client to the POSTed server so cache reads, per-server id writes and
        # the redirect all use the exact server the user was viewing. Fail closed on a
        # stale/unknown key — the old `or self.librenms_api.server_key` fallback rebuilt
        # the lazy client, which can resolve to a different server (wrong-server sync) or
        # raise on a misconfigured default (500).
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._tab_response(request, object_type, interface_name_field, None)
        self._post_server_key = server_key
        selected_port_ids = self.get_selected_port_ids(request)
        exclude_columns = request.POST.getlist("exclude_columns")

        if selected_port_ids is None:
            return self._tab_response(request, object_type, interface_name_field, server_key)
        visible_port_ids = selected_port_ids

        ports_data = self.get_cached_ports_data(request, obj, server_key)
        if ports_data is None:
            return self._tab_response(request, object_type, interface_name_field, server_key)

        relationships = self._get_cached_relationships(obj, server_key)
        # The related-row walk runs under the owner locks (_lock_sync_scope), with the writer's state.
        self._related_walk = (
            normalize_relationship_maps(relationships) if request.POST.get("auto_select_lag_members") else None
        )
        if visible_port_ids & _duplicated_port_ids(ports_data):
            messages.warning(request, _DUPLICATED_SELECTION_MESSAGE)
            return self._tab_response(request, object_type, interface_name_field, server_key)

        try:
            outcome = run_transaction(
                lambda: self._sync_attempt(
                    visible_port_ids,
                    ports_data,
                    relationships,
                    exclude_columns,
                    interface_name_field,
                    server_key,
                )
            )
        except LibreNMSPortBindingConflict as conflict:
            self._synced_count = 0
            self._mutated = False
            messages.warning(request, str(conflict))
            return self._tab_response(request, object_type, interface_name_field, server_key)
        except _DuplicatedSelectionError:
            # The walk reached a duplicated port before anything was written; the attempt rolled back.
            messages.warning(request, _DUPLICATED_SELECTION_MESSAGE)
            return self._tab_response(request, object_type, interface_name_field, server_key)
        except IntegrityError:
            # The runner checks Django's deferred foreign keys as the attempt's last step. A related
            # row deleted mid-sync therefore surfaces here, past every inner savepoint handler.
            logger.warning("Bulk sync: rolled back by a concurrent DB conflict", exc_info=True)
            messages.error(
                request,
                "The sync was rolled back by a concurrent change to a related interface. "
                "Refresh the LibreNMS data and try again.",
            )
            return self._tab_response(request, object_type, interface_name_field, server_key)

        for warning in outcome.warnings:
            messages.warning(request, warning)
        if outcome.skipped_conflicts:
            skipped = ", ".join(outcome.skipped_conflicts)
            messages.warning(
                request,
                f"{len(outcome.skipped_conflicts)} interface(s) skipped: {skipped}.",
            )
        for current_name, reported_name, conflict_reason in outcome.kept_name_conflicts:
            reason = (
                f"the {conflict_reason}"
                if conflict_reason is not None
                else "the reported name is in use on the same interface owner"
            )
            messages.warning(
                request,
                f"Interface '{current_name}' kept its current name because {reason}: '{reported_name}'.",
            )
        # Only claim success when at least one interface was actually synced. Track an explicit
        # synced count rather than comparing skip-vs-selected sizes: a single selected display name
        # can be skipped after another selected port succeeds, so the explicit count remains the
        # source of truth for the success banner.
        if outcome.synced_count > 0:
            messages.success(request, "Selected interfaces synced successfully.")
        if outcome.mutated:
            cache_transition = schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.INTERFACES,
                server_key,
            )
        else:
            cache_transition = None
        return apply_transition_to_response(
            request, self._tab_response(request, object_type, interface_name_field, server_key), cache_transition
        )

    def _sync_attempt(
        self,
        visible_port_ids,
        ports_data,
        relationships,
        exclude_columns,
        interface_name_field,
        server_key,
    ):
        """
        Run one attempt of the sync transaction and return what it reports.

        ``run_transaction`` calls this once for each attempt. Each attempt sets every attempt list and
        counter afresh, and it re-locks and re-reads the objects it writes, so a retry reads nothing
        that a failed attempt derived. It adds no message: ``post()`` publishes the committed outcome.

        Args:
            visible_port_ids (set[int]): The port IDs the user selected.
            ports_data (list[dict]): The cached snapshot rows.
            relationships (dict): The cached relationship maps.
            exclude_columns (list[str]): The columns this sync must not write.
            interface_name_field (str): Port field that contains the interface name.
            server_key (str): The validated POSTed server key.

        Returns:
            _InterfaceSyncOutcome: What the attempt synced, skipped and warned about.

        """
        self._selected_port_ids = set(visible_port_ids)
        self._auto_selected_port_ids = set()
        # Inferred off-page owners are resolved only after the chassis and its members are locked.
        self._auto_selected_target_ids = {}
        # Rows the sync skips (for example a port ID bound to another device's interface) are reported.
        self._skipped_conflicts = []
        self._kept_name_conflicts = []
        self._synced_count = 0
        self._mutated = False
        self._attempt_warnings = []
        try:
            self.sync_selected_interfaces(
                self.object,
                ports_data,
                exclude_columns,
                interface_name_field,
                keep_locked_targets=True,
            )
            # The relationship pass reuses the locked target map of the attribute pass.
            self._sync_interface_relationships(
                self.object,
                ports_data,
                relationships,
                server_key,
                excluded_columns=exclude_columns,
            )
        finally:
            self.__dict__.pop("_locked_target_devices", None)
        return _InterfaceSyncOutcome(
            skipped_conflicts=tuple(self._skipped_conflicts),
            kept_name_conflicts=tuple(self._kept_name_conflicts),
            synced_count=self._synced_count,
            mutated=self._mutated,
            warnings=tuple(self._attempt_warnings),
        )

    def _tab_response(self, request, object_type, interface_name_field, server_key):
        """
        Return the interfaces tab after a sync or rebind POST.

        An htmx submit gets the ``#interface-sync-content`` fragment in place, so the page, the page
        size and the scroll position survive. A plain submit gets a redirect to the tab. The tab is
        rendered from ``self.object``: the page object as the sync locked and decided it, so the
        render reads the platform the writer read.

        Args:
            request (HttpRequest): The sync or rebind request.
            object_type (str): ``device`` or ``virtualmachine``.
            interface_name_field (str): Port field that contains the interface name.
            server_key (str | None): The validated POSTed server key, or None when it names no server.

        Returns:
            HttpResponse: The fragment, or a redirect to the interfaces tab.

        """
        obj = self.object
        if request.headers.get("HX-Request") != "true":
            url_name = (
                "dcim:device_librenms_sync"
                if object_type == "device"
                else "plugins:netbox_librenms_plugin:vm_librenms_sync"
            )
            # quote_plus the request-supplied values so they cannot inject extra query parameters.
            return redirect(
                reverse(url_name, kwargs={"pk": obj.pk})
                + f"?tab=interfaces&interface_name_field={quote_plus(interface_name_field)}"
                + (f"&server_key={quote_plus(server_key)}" if server_key else "")
            )

        # Imported here: the object_sync views import this module's view stack.
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView

        tab_view = DeviceInterfaceTableView() if object_type == "device" else VMInterfaceTableView()
        tab_view.setup(request, pk=obj.pk)
        if server_key is None:
            # The same empty tab the refresh renders for a server that is no longer configured.
            server_key = self.active_server_key
            context = {"object": obj, "table": None, "cache_expiry": None, "server_key": None}
        else:
            tab_view.rebind_api_for_server(server_key)
            context = tab_view.get_context_data(request, obj, interface_name_field, server_key)
        return tab_view.render_sync_partial(
            request,
            obj,
            server_key,
            {"interface_sync": context, "interface_name_field": interface_name_field},
        )

    def get_object(self, object_type, object_id):
        """Return the Device or VirtualMachine for the given type and ID (object-scoped)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        if object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        raise Http404("Invalid object type.")

    def get_selected_port_ids(self, request):
        """Return selected visible LibreNMS port IDs from POST data."""
        # A row's own Sync button wins over the tick boxes: the user pressed one row, not the
        # selection, and the browser submits both. Same shape as the cables tab, so the row goes
        # through this one view with the same permission, cache and duplicate-id checks.
        sync_one = normalize_librenms_port_id(request.POST.get("sync_one"))
        if sync_one is not None:
            return {sync_one}
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

    def _infer_snapshot_target_ids(
        self,
        obj,
        ports_data,
        port_ids,
        interface_name_field,
        server_key,
        *,
        members=None,
    ):
        """Resolve snapshot rows to the Virtual Chassis members inferred by the render path."""
        if not isinstance(obj, Device) or not port_ids:
            return {}

        locked_members = {member.pk: member for member in members or ()}
        members = (
            [locked_members.get(member.pk, member) for member in obj.virtual_chassis.members.all()]
            if obj.virtual_chassis_id is not None
            else [locked_members.get(obj.pk, obj)]
        )
        members_by_position = {member.vc_position: member for member in members if member.vc_position is not None}
        members_by_id = {member.pk: member for member in members}
        ports_by_id = {
            port_id: port
            for port in ports_data
            if (port_id := normalize_librenms_port_id(port.get("port_id"))) is not None
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
                return_device_on_failure=port.get("_source") == OOB_INVENTORY_SOURCE,
            )
            if target is not None:
                targets[port_id] = target.pk
        return targets

    def _expand_related_rows(self, obj, ports_data, interface_name_field, *, inferred_ids, device_for):
        """
        Add the related rows the walk reaches to the selection, deciding each one as the writer will.

        The caller holds the owner locks and passes their state. A row is added, and walked through,
        only when ``_row_owner`` gives it an owner and ``check_interface_write`` allows the write.
        A related row it refuses is reported with the writer's reason.

        Args:
            obj (Device | VirtualMachine): The locked page object.
            ports_data (list[dict]): The cached snapshot rows.
            interface_name_field (str): Port field that contains the selected interface name.
            inferred_ids (dict[int, int]): Member IDs inferred under the lock, by port ID.
            device_for (callable | None): Returns the locked member Device for an ID.

        Raises:
            _DuplicatedSelectionError: The walk reaches a port the snapshot holds more than once.

        """
        if getattr(self, "_related_walk", None) is None:
            return
        lag_members, sub_interfaces, bridge_members = self._related_walk
        rules = interface_rules_for_request(self.request)
        duplicated = _duplicated_port_ids(ports_data)
        ports_by_id = {
            port_id: port
            for port in ports_data
            if (port_id := normalize_librenms_port_id(port.get("port_id"))) is not None
        }
        oob_port_ids = getattr(self, "_oob_port_ids", set())
        visible_port_ids = set(self._selected_port_ids)

        def refusal(port_id, *, auto_added):
            port = ports_by_id.get(port_id)
            if port is None:
                return "not in the cached LibreNMS data; refresh the data"
            owner = self._row_owner(
                obj,
                port_id,
                auto_added=auto_added,
                inferred_ids=inferred_ids,
                oob_port_ids=oob_port_ids,
                device_for=device_for,
            )
            if owner is None:
                return "selected target unavailable"
            return decision_reason(rules.check_interface_write(port, platform_id=owner.platform_id))

        frontier = {port_id for port_id in visible_port_ids if refusal(port_id, auto_added=False) is None}
        refused = set()
        while frontier:
            related_rows = {member_id for member_id, aggregate_id in lag_members.items() if aggregate_id in frontier}
            related_rows.update(parent_id for child_id, parent_id in sub_interfaces.items() if child_id in frontier)
            related_rows.update(bridge_id for member_id, bridge_id in bridge_members.items() if member_id in frontier)
            frontier = set()
            for port_id in related_rows - self._selected_port_ids - refused:
                if port_id in duplicated:
                    raise _DuplicatedSelectionError(port_id)
                reason = refusal(port_id, auto_added=True)
                if reason is None:
                    frontier.add(port_id)
                    continue
                refused.add(port_id)
                name = (ports_by_id.get(port_id) or {}).get(interface_name_field) or port_id
                self._skipped_conflicts.append(f"related row {name} not synced: {reason}")
            self._selected_port_ids.update(frontier)
            self._auto_selected_port_ids.update(frontier)

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

    def _sync_interface_relationships(
        self,
        obj,
        ports_data,
        relationships,
        server_key,
        *,
        excluded_columns=(),
    ):
        """
        Set the relationships discovered for synced interfaces.

        Runs after sync_selected_interfaces() so all interfaces already exist in NetBox.
        Only processes relationships where this interface is a member/child — the
        aggregate/parent may or may not be in the selected set (it just needs to exist
        in NB).

        Args:
            obj: The Device (or VirtualMachine) being synced.
            ports_data: The LibreNMS port dicts for the device.
            relationships (dict): The normalized relationship maps to apply.
            server_key (str): The LibreNMS server key scoping stored-id reads.
            excluded_columns: Fields that the current sync must not update.

        Returns:
            None

        """
        if not relationships:
            return
        lag_members, sub_interfaces, bridge_members = normalize_relationship_maps(relationships)
        if not lag_members and not sub_interfaces and not bridge_members:
            return
        interface_name_field = self.interface_name_field
        unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(
            ports_data, interface_name_field
        )
        relationship_maps = (lag_members, sub_interfaces, bridge_members)
        selected_edge_source_ids, port_by_id = self._relationship_source_rows(
            obj,
            ports_data,
            relationship_maps,
            unique_host_port_ids,
            interface_name_field,
        )
        if not selected_edge_source_ids:
            return
        candidate_port_ids, candidate_names = self._relationship_candidate_hints(
            selected_edge_source_ids,
            port_by_id,
            relationship_maps,
            unambiguous_name_port_ids,
            interface_name_field,
        )
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
                context = _BulkRelationshipContext(
                    obj=obj,
                    port_by_id=port_by_id,
                    lag_members=lag_members,
                    sub_interfaces=sub_interfaces,
                    bridge_members=bridge_members,
                    catalog_index=catalog_index,
                    source_index=source_index,
                    related_index=related_index,
                    changeable_ids=changeable_ids,
                    server_key=server_key,
                    interface_name_field=interface_name_field,
                    unique_host_port_ids=unique_host_port_ids,
                    unambiguous_name_port_ids=unambiguous_name_port_ids,
                    excluded_columns=set(excluded_columns),
                )
                self._apply_bulk_relationships(context, selected_edge_source_ids)
        except IntegrityError:
            # Immediate conflicts (a unique violation, a row already gone at write time) surface
            # here and roll the relationship pass back as a unit. Deferred FK violations do NOT:
            # Postgres validates those at the outermost COMMIT, which post() handles.
            logger.warning(
                "Bulk sync: relationship pass rolled back by a concurrent DB conflict",
                exc_info=True,
            )
            self._attempt_warnings.append(
                "Interfaces synced, but relationships hit a concurrent change and were not applied. Re-run the sync."
            )

    def _relationship_source_rows(
        self,
        obj,
        ports_data,
        relationship_maps,
        unique_host_port_ids,
        interface_name_field,
    ):
        """Return selected relationship source IDs and the unambiguous host-port index."""
        relationship_source_ids = set().union(*(mapping.keys() for mapping in relationship_maps))
        selected_source_ids = {
            str(port_id)
            for raw_port_id in getattr(self, "_selected_port_ids", set())
            if (port_id := normalize_librenms_port_id(raw_port_id)) in relationship_source_ids
        }
        writer_model = VMInterface if isinstance(obj, VirtualMachine) else Interface
        valid_name_ids = {
            normalize_librenms_port_id(port.get("port_id"))
            for port in ports_data
            if port.get("_source") != OOB_INVENTORY_SOURCE
            and syncable_interface_name(port, interface_name_field, writer_model) is not None
        }
        selected_source_ids &= {str(port_id) for port_id in valid_name_ids if port_id is not None}
        port_by_id = {
            str(port_id): port
            for port in ports_data
            if port.get("_source") != OOB_INVENTORY_SOURCE
            and (port_id := normalize_librenms_port_id(port.get("port_id"))) in unique_host_port_ids
        }
        return selected_source_ids, port_by_id

    @staticmethod
    def _relationship_candidate_hints(
        selected_source_ids,
        port_by_id,
        relationship_maps,
        unambiguous_name_port_ids,
        interface_name_field,
    ):
        """Collect stable IDs and safe names for one bounded candidate query."""
        candidate_port_ids = []
        candidate_names = []
        for source_id in selected_source_ids:
            source_port = port_by_id.get(source_id)
            if source_port is None:
                continue
            normalized_source_id = normalize_librenms_port_id(source_id)
            related_ids = tuple(mapping.get(normalized_source_id) for mapping in relationship_maps)
            for raw_candidate_id in (source_port.get("port_id"), *related_ids):
                candidate_id = normalize_librenms_port_id(raw_candidate_id)
                if candidate_id is None:
                    continue
                candidate_port = port_by_id.get(str(candidate_id), {})
                candidate_port_ids.append(candidate_port.get("port_id", raw_candidate_id))
                if candidate_id in unambiguous_name_port_ids:
                    candidate_name = candidate_port.get(interface_name_field) or candidate_port.get("ifName")
                    if candidate_name:
                        candidate_names.append(candidate_name)
        return candidate_port_ids, candidate_names

    def _apply_bulk_relationships(self, context, selected_source_ids):
        """Apply every relationship map while the owners and candidates remain locked."""
        for port_id in selected_source_ids:
            if port_id not in context.port_by_id:
                continue
            target = self._resolve_row_target_device(context.obj, port_id=port_id)
            if target is None:
                continue
            expected_owner = interface_owner_for_object(target)
            self._apply_bulk_lag(context, port_id, expected_owner)
            self._apply_bulk_parent(context, port_id, expected_owner)
            self._apply_bulk_bridge(context, port_id, expected_owner)

    def _resolve_bulk_relationship(self, context, port_id, related_port_id, expected_owner, label, **kwargs):
        """Resolve one relationship pair from the locked bulk indexes."""
        return self._resolve_relationship_ends(
            context.obj,
            port_id,
            related_port_id,
            context.port_by_id,
            context.catalog_index,
            context.source_index,
            context.related_index,
            context.server_key,
            expected_owner,
            context.interface_name_field,
            context.unambiguous_name_port_ids,
            label,
            **kwargs,
        )

    def _apply_bulk_lag(self, context, port_id, expected_owner):
        """Apply one LAG edge, including aggregate type preparation."""
        raw_lag = context.lag_members.get(normalize_librenms_port_id(port_id))
        if raw_lag is None or normalize_librenms_port_id(raw_lag) not in context.unique_host_port_ids:
            return
        member, aggregate = self._resolve_bulk_relationship(
            context,
            port_id,
            raw_lag,
            expected_owner,
            "LAG",
            require_interface_source=True,
        )
        if member is None or (member.lag_id == aggregate.pk and not _lag_aggregate_needs_promotion(aggregate)):
            return
        decisions = self._bulk_edge_decisions(context, "LAG", (port_id, member), (raw_lag, aggregate))
        if decisions is None:
            return
        if _lag_aggregate_needs_promotion(aggregate) and (conflict := _promotion_conflict(decisions[1], "lag")):
            self._record_skipped_conflict(
                member.name, f"LAG link to {aggregate.name} not synced; {aggregate.name}: {conflict}"
            )
            return
        if aggregate.type != "lag" and "type" in context.excluded_columns:
            logger.warning(
                "Bulk sync: skipping LAG link %s -> %s because interface type is excluded",
                member.name,
                aggregate.name,
            )
            self._record_skipped_conflict(member.name, "aggregate type is excluded")
            return
        if aggregate.type != "lag" and aggregate.pk not in context.changeable_ids:
            logger.warning(
                "Bulk sync: skipping LAG link %s -> %s because the aggregate cannot be changed",
                member.name,
                aggregate.name,
            )
            return
        if self._apply_relationship_edge(member, "lag", aggregate, self._prepare_bulk_lag_aggregate, "LAG"):
            self._mutated = True

    def _apply_bulk_parent(self, context, port_id, expected_owner):
        """Apply one parent edge, including child type preparation."""
        raw_parent = context.sub_interfaces.get(normalize_librenms_port_id(port_id))
        if raw_parent is None or normalize_librenms_port_id(raw_parent) not in context.unique_host_port_ids:
            return
        child, parent = self._resolve_bulk_relationship(context, port_id, raw_parent, expected_owner, "parent")
        if child is None or (child.parent_id == parent.pk and not _parent_child_needs_promotion(child)):
            return
        decisions = self._bulk_edge_decisions(context, "parent", (port_id, child), (raw_parent, parent))
        if decisions is None:
            return
        if _parent_child_needs_promotion(child) and (conflict := _promotion_conflict(decisions[0], "virtual")):
            self._record_skipped_conflict(child.name, f"parent link to {parent.name} not synced; {conflict}")
            return
        if _parent_child_needs_promotion(child) and "type" in context.excluded_columns:
            logger.warning(
                "Bulk sync: skipping parent link %s -> %s because interface type is excluded",
                child.name,
                parent.name,
            )
            self._record_skipped_conflict(child.name, "child type is excluded")
            return
        if self._apply_relationship_edge(
            child,
            "parent",
            parent,
            None,
            "parent",
            prepare_source=self._prepare_bulk_parent_child,
        ):
            self._mutated = True

    def _apply_bulk_bridge(self, context, port_id, expected_owner):
        """Apply one bridge edge independently from LAG and parent state."""
        raw_bridge = context.bridge_members.get(normalize_librenms_port_id(port_id))
        if raw_bridge is None or normalize_librenms_port_id(raw_bridge) not in context.unique_host_port_ids:
            return
        member, bridge = self._resolve_bulk_relationship(context, port_id, raw_bridge, expected_owner, "bridge")
        if member is None or member.bridge_id == bridge.pk:
            return
        if self._bulk_edge_decisions(context, "bridge", (port_id, member), (raw_bridge, bridge)) is None:
            return
        if self._apply_relationship_edge(member, "bridge", bridge, None, "bridge"):
            self._mutated = True

    def _bulk_edge_decisions(self, context, label, source, related):
        """
        Decide both ends of one bulk edge; a blocked end skips the edge and keeps the current link.

        A blocked source row already reported its own skip, so only a blocked related end is reported.

        Args:
            context (_BulkRelationshipContext): The locked bulk pass inputs.
            label (str): The relationship label for the message.
            source (tuple): The source ``(port_id, interface)``.
            related (tuple): The related ``(port_id, interface)``.

        Returns:
            tuple[RuleDecision, RuleDecision] | None: Both decisions, or None when the edge is skipped.

        """
        (source_port_id, source_iface), (related_port_id, related_iface) = source, related
        decisions, blocked_end, reason = _relationship_decisions(
            interface_rules_for_request(self.request),
            (context.port_by_id.get(str(normalize_librenms_port_id(source_port_id))), source_iface),
            (context.port_by_id.get(str(normalize_librenms_port_id(related_port_id))), related_iface),
        )
        if reason is not None and blocked_end is related_iface:
            self._record_skipped_conflict(
                source_iface.name, f"{label} link to {related_iface.name} not synced; {related_iface.name}: {reason}"
            )
        return decisions

    @staticmethod
    def _prepare_bulk_lag_aggregate(agg_iface):
        """
        LAG-pass hook: promote the aggregate to type=lag and return ``(persist, restore)`` or None.

        NetBox's Interface.clean() does not check the aggregate's type, so the plugin promotes it
        to keep a member off a non-LAG aggregate. The aggregate object is reused across rows via
        the shared interface index, so a member whose link later fails validation must restore
        the in-memory type. Otherwise, a subsequent valid member sharing this aggregate would
        skip the save() and leave the aggregate's type stale in the DB. The restore path is why
        this passes ``with_restore=True`` to the shared promotion helper.

        Args:
            agg_iface (Interface): The LAG aggregate to promote.

        Returns:
            tuple[callable, callable] | None: The persist and restore callables, or None when no
                promotion is needed.

        """
        return _promote_lag_aggregate(agg_iface, with_restore=True)

    @staticmethod
    def _prepare_bulk_parent_child(child_iface):
        """Promote a parent child and retain a restore hook for the shared bulk index."""
        return _promote_parent_child(child_iface, with_restore=True)

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
        Resolve the ``(source, related)`` interface pair for one bulk relationship edge.

        Both ends are resolved by stable LibreNMS port_id. The source is pinned to the row target.
        A selected related row is pinned to its own target, which can be another member of the same
        Virtual Chassis.
        Returns ``(None, None)`` and skips the row on any lookup failure (logged at debug). It also
        skips the row when *require_interface_source* is set and the source is not an Interface
        (a VMInterface has no lag field).

        Args:
            obj (Device | VirtualMachine): The object whose relationship scope contains the interfaces.
            port_id (str): The stable LibreNMS port ID for the source interface.
            related_raw (int | str): The raw LibreNMS port ID for the related interface.
            port_by_id (dict): The cached port rows keyed by normalized LibreNMS port ID.
            catalog_index (dict): The ambiguity-preserving index for all candidate interfaces.
            source_index (dict): The index of candidate source interfaces that can be changed.
            related_index (dict): The index of candidate related interfaces that can be viewed or changed.
            server_key (str): The LibreNMS server key that scopes stored IDs.
            source_expected_owner (tuple): The expected device or virtual machine owner for the source.
            interface_name_field (str): The cached port field used for interface names.
            unambiguous_name_port_ids (set): The port IDs that permit a safe name fallback.
            log_kind (str): The relationship label used in debug messages.
            require_interface_source (bool): Whether the source must be an Interface instead of a VMInterface.

        Returns:
            tuple[Interface | VMInterface | None, Interface | VMInterface | None]: The resolved interface pair,
                or ``(None, None)`` when the row must be skipped.

        """
        related_port_id = str(related_raw)
        normalized_related_port_id = normalize_librenms_port_id(related_raw)
        refused_ids = getattr(self, "_foreign_bound_port_ids", set())
        if normalize_librenms_port_id(port_id) in refused_ids or normalized_related_port_id in refused_ids:
            return None, None
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

    def _apply_relationship_edge(
        self,
        source_iface,
        relation_field,
        related_iface,
        prepare_related,
        log_kind,
        *,
        prepare_source=None,
    ):
        """
        Set ``source_iface.<relation_field> = related_iface`` and persist, validating first.

        Thin bulk-pass wrapper over :func:`_apply_interface_relationship` (the shared
        set -> validate -> persist core, also used by the inline single-row endpoints). A
        validation failure is logged and skipped so the batch continues, never raised.
        ``prepare_related`` is the LAG pass's aggregate type=lag hook (returns
        ``(persist, restore)``); the parent pass passes None.

        Args:
            source_iface (Interface | VMInterface): The interface whose relationship field is updated.
            relation_field (str): The relationship field to update.
            related_iface (Interface | VMInterface): The interface assigned to the relationship field.
            prepare_related (callable | None): The hook that prepares the related interface before validation.
            log_kind (str): The relationship label used in log messages.
            prepare_source (callable | None): The hook that prepares the source interface before
                validation, mirroring ``prepare_related`` on the other side of the edge.

        Returns:
            bool: True when the relationship is saved, or False when validation or persistence fails.

        """
        try:
            # Own savepoint: an IntegrityError from the persist poisons the enclosing batch
            # transaction ("current transaction is aborted" on every later row) unless the
            # failed statements are rolled back to a savepoint first — same reasoning as the
            # move-to-winner flow in migrate.py. It also keeps the pair atomic: a related-side
            # persist (LAG type bump) can't outlive a failed source save.
            with transaction.atomic():
                _apply_interface_relationship(
                    source_iface,
                    relation_field,
                    related_iface,
                    prepare_related,
                    prepare_source,
                )
        except ValidationError as exc:
            logger.warning(
                "Bulk sync: skipping invalid %s link %s -> %s: %s",
                log_kind,
                source_iface.name,
                related_iface.name,
                validation_error_detail(exc),
            )
            return False
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
            return False
        logger.info("Bulk sync: set %s.%s = %s", source_iface.name, relation_field, related_iface.name)
        return True

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
        self._foreign_bound_port_ids = set()
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        with transaction.atomic():
            locked_obj = self._lock_sync_scope(obj, ports_data, interface_name_field)
            if locked_obj is None:
                for port in ports_data:
                    if normalize_librenms_port_id(port.get("port_id")) in selected_port_ids:
                        self._record_skipped_conflict(
                            port.get(interface_name_field),
                            "selected target unavailable",
                        )
                return
            obj = locked_obj
            if "vlans" not in exclude_columns:
                vlan_scope_devices = (
                    self._selected_vlan_scope_devices(obj, ports_data, interface_name_field)
                    if isinstance(obj, Device)
                    else [obj]
                )
                if vlan_warning := self._prepare_vlan_lookup_maps(vlan_scope_devices):
                    self._attempt_warnings.append(vlan_warning)
            writer_model = VMInterface if isinstance(obj, VirtualMachine) else Interface
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            decisions = self._snapshot_name_decisions(obj, ports_data, interface_name_field, writer_model, server_key)
            synced_names, rejected_names = decisions.names, decisions.rejected
            try:
                for port in ports_data:
                    port_id = normalize_librenms_port_id(port.get("port_id"))
                    if port_id not in selected_port_ids:
                        continue
                    row_excludes = exclude_columns
                    if port_id in getattr(self, "_auto_selected_port_ids", set()) and "vlans" not in row_excludes:
                        row_excludes = [*row_excludes, "vlans"]
                    if port.get("_source") == OOB_INVENTORY_SOURCE and self._oob_row_is_unsyncable(
                        port,
                        interface_name_field,
                        rejected_names,
                    ):
                        continue
                    if reason := rejected_names.get(port_id):
                        if reason == PORT_ID_SOURCE_COLLISION_REASON or (
                            "name" not in row_excludes and reason != REPORTED_NAME_PORT_COLLISION_REASON
                        ):
                            self._record_skipped_conflict(port.get(interface_name_field), reason)
                            continue
                    synced_name = synced_names.get(port_id)
                    if (
                        synced_name is None
                        and port.get("_source") != OOB_INVENTORY_SOURCE
                        and ("name" in row_excludes or reason == REPORTED_NAME_PORT_COLLISION_REASON)
                    ):
                        synced_name = syncable_interface_name(port, interface_name_field, writer_model)
                    self.sync_interface(
                        obj,
                        port,
                        row_excludes,
                        interface_name_field,
                        synced_name,
                        name_conflict_reason=(
                            reason
                            if port.get("_source") == OOB_INVENTORY_SOURCE
                            or reason == REPORTED_NAME_PORT_COLLISION_REASON
                            else None
                        ),
                        name_owner_port_id=(
                            self._visible_name_owner_port_id(decisions, port_id, writer_model)
                            if reason == REPORTED_NAME_PORT_COLLISION_REASON
                            else None
                        ),
                    )
            finally:
                if not keep_locked_targets:
                    self.__dict__.pop("_locked_target_devices", None)

    def _lock_sync_scope(self, obj, ports_data, interface_name_field):
        """
        Lock the page owner, and for a Device its chassis scope, then infer every row's target.

        Args:
            obj (Device | VirtualMachine): The page object.
            ports_data (list[dict]): The cached snapshot rows.
            interface_name_field (str): Port field that contains the selected interface name.

        Returns:
            Device | VirtualMachine | None: The locked page object, or None when it is unavailable.

        """
        if isinstance(obj, VirtualMachine):
            obj = self.restricted_queryset(VirtualMachine).select_for_update(of=("self",)).filter(pk=obj.pk).first()
            if obj is not None:
                self.object = obj
                self._expand_related_rows(obj, ports_data, interface_name_field, inferred_ids={}, device_for=None)
            return obj
        if not isinstance(obj, Device):
            return obj
        locked_targets = self._lock_selected_device_targets(obj)
        obj = locked_targets.get(obj.pk)
        if obj is None:
            return None
        self._locked_target_devices = locked_targets
        self.object = obj
        snapshot_port_ids = {
            port_id for port in ports_data if (port_id := normalize_librenms_port_id(port.get("port_id"))) is not None
        }
        self._snapshot_target_ids = self._infer_snapshot_target_ids(
            obj,
            ports_data,
            snapshot_port_ids,
            interface_name_field,
            self._post_server_key,
            members=list(locked_targets.values()),
        )
        host_port_ids = {
            port_id
            for port in ports_data
            if port.get("_source") != OOB_INVENTORY_SOURCE
            and (port_id := normalize_librenms_port_id(port.get("port_id"))) is not None
        }
        self._oob_port_ids = snapshot_port_ids - host_port_ids
        # Decided from the locked rows, so the walk and the writer read one owner and platform.
        self._expand_related_rows(
            obj, ports_data, interface_name_field, inferred_ids=self._snapshot_target_ids, device_for=locked_targets.get
        )
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        self._auto_selected_target_ids = {
            port_id: target_id
            for port_id, target_id in self._snapshot_target_ids.items()
            if port_id in self._auto_selected_port_ids or port_id in selected_port_ids
        }
        return obj

    def _snapshot_name_decisions(self, obj, ports_data, interface_name_field, writer_model, server_key):
        """
        Resolve every snapshot row's target and name the way the writer will.

        Args:
            obj (Device | VirtualMachine): The locked page object.
            ports_data (list[dict]): The cached snapshot rows.
            interface_name_field (str): Port field that contains the selected interface name.
            writer_model (type): ``Interface`` or ``VMInterface``.
            server_key (str): The active LibreNMS server key.

        Returns:
            _SnapshotNameDecisions: Targets, candidate names, rejections and name holders.

        """
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        target_device_ids = {}
        for port in ports_data:
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if port_id is None:
                continue
            if isinstance(obj, VirtualMachine):
                target = obj
            elif port_id in selected_port_ids:
                target = self._resolve_row_target_device(obj, port_id=port_id)
            else:
                inferred_target_id = self._snapshot_target_ids.get(port_id, obj.pk)
                target = self._locked_target_devices.get(inferred_target_id)
            if target is not None:
                target_device_ids[port_id] = target.pk
        reserved_name_port_ids = self._reserved_name_port_ids(obj, server_key)
        names, rejected = synced_interface_names(
            ports_data,
            interface_name_field,
            writer_model,
            target_device_ids=target_device_ids,
            reserved_name_port_ids_by_device=reserved_name_port_ids,
        )
        owners = reported_name_owners(
            ports_data,
            interface_name_field,
            names,
            rejected,
            target_device_ids=target_device_ids,
            reserved_name_port_ids_by_device=reserved_name_port_ids,
            snapshot_complete=not (getattr(self, "_cached_ports_payload", None) or {}).get("oob_incomplete"),
            model=writer_model,
        )
        return _SnapshotNameDecisions(target_device_ids, names, rejected, owners)

    @staticmethod
    def _name_holder_filter(writer_model, target_id, name):
        """Return the lookup for the interface that holds ``name`` on one target owner."""
        owner_field = "virtual_machine_id" if writer_model is VMInterface else "device_id"
        return {owner_field: target_id, "name": name}

    def _visible_name_owner_port_id(self, decisions, port_id, writer_model):
        """Return the port that holds a row's reported name when the caller may view its interface."""
        owner = decisions.owners.get(port_id)
        if owner is None:
            return None
        holder = self._name_holder_filter(writer_model, decisions.target_device_ids.get(port_id), owner.name)
        return owner.port_id if self.restricted_queryset(writer_model, "view").filter(**holder).exists() else None

    def _reserved_name_port_ids(self, obj, server_key):
        """Return active-server port IDs bound to each target interface name."""
        if isinstance(obj, Device):
            target_ids = getattr(self, "_locked_target_devices", {obj.pk: obj})
            interfaces = Interface.objects.filter(device_id__in=target_ids).only(
                "device_id",
                "name",
                "custom_field_data",
            )
            owner_field = "device_id"
        else:
            interfaces = VMInterface.objects.filter(virtual_machine=obj).only(
                "virtual_machine_id",
                "name",
                "custom_field_data",
            )
            owner_field = "virtual_machine_id"

        reserved = {}
        for interface in interfaces:
            port_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
            if port_id is not None:
                reserved.setdefault(getattr(interface, owner_field), {}).setdefault(interface.name, set()).add(port_id)
        return reserved

    def _selected_vlan_scope_devices(self, obj, ports_data, interface_name_field):
        """Return the distinct locked owners whose selected rows will sync VLANs."""
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        auto_selected_port_ids = getattr(self, "_auto_selected_port_ids", set())
        owners = {}
        for port in ports_data:
            if port.get("_source") == OOB_INVENTORY_SOURCE and port.get("_dedup_conflict"):
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
        """Build VLAN scope maps from owner rows locked for this sync transaction; return a warning or None."""
        # The gate checks add/change on the interface model, not IPAM, so read VLANs as the
        # caller. A caller without the grant matches no VLAN, which the warning below names.
        vlan_scope_user = self.vlan_scope_user()
        vlan_groups = self.get_vlan_groups_for_devices(vlan_scope_devices, user=vlan_scope_user)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups, user=vlan_scope_user)
        self._lookup_maps = lookup_maps
        self._lookup_maps_by_owner = {
            owner.pk: self.restrict_vlan_lookup_maps(
                lookup_maps,
                self.filter_vlan_groups_for_device(vlan_groups, owner),
            )
            for owner in vlan_scope_devices
        }
        self._vlan_owners_by_id = {owner.pk: owner for owner in vlan_scope_devices}
        hidden = self.hidden_vlan_permissions(vlan_scope_devices, vlan_scope_user)
        # A hidden VLAN reads as absent, so syncing would clear an existing untagged assignment and
        # drop hidden tagged VLANs. Skip the VLAN write entirely rather than destroy what we
        # cannot see. A constrained grant hides rows while passing the permission-name check, so
        # the row comparison below is what catches it.
        self._vlan_scope_incomplete = bool(hidden) or self.vlan_scope_is_incomplete(
            vlan_scope_devices,
            vlan_scope_user,
            scoped_groups=vlan_groups,
        )
        if hidden:
            return (
                f"VLANs were not synced for the selected interfaces: your account is missing "
                f"{', '.join(hidden)}. Existing VLAN assignments were left unchanged."
            )
        if self._vlan_scope_incomplete:
            # A constrained grant passes the permission-name check, so the branch above says
            # nothing. Without this the VLAN write is skipped silently and the user cannot tell why.
            return (
                "VLANs were not synced for the selected interfaces: your account cannot view every "
                "VLAN in scope for this device. Existing VLAN assignments were left unchanged."
            )
        return None

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

    def _posted_row_target(self, port_id):
        """
        Return the one member the POST selects for a row, or None when it selects none.

        Raises:
            _ConflictingRowTargetError: The POST names more than one member for the row.

        """
        posted = self.request.POST.getlist(f"device_selection_{port_id}")
        if len(posted) > 1:
            raise _ConflictingRowTargetError(port_id)
        return posted[0] if posted else None

    def _row_owner(self, obj, port_id, *, auto_added, inferred_ids, oob_port_ids, device_for):
        """
        Return the device one row writes to, under the writer's owner rule, or None when it has none.

        The writer and the related-row walk both call this, so the walk cannot decide a row with
        another owner than the writer uses.

        Args:
            obj (Device | VirtualMachine): The page object; a VM owns every row.
            port_id (int): The row's normalized LibreNMS port ID.
            auto_added (bool): The walk added the row: only its inferred member counts, never a
                posted member value.
            inferred_ids (dict[int, int]): Inferred member IDs by port ID.
            oob_port_ids (set[int]): OOB rows with a page-owner fallback when inference finds no member.
            device_for (callable | None): Returns the chassis member Device the caller may target
                for an ID, or None.

        Returns:
            Device | VirtualMachine | None: The owner, or None when the row has no valid owner.

        """
        if not isinstance(obj, Device):
            return obj
        if auto_added:
            selected_device_id = inferred_ids.get(port_id)
            if selected_device_id is None:
                return None
        else:
            try:
                selected_device_id = self._posted_row_target(port_id) or inferred_ids.get(port_id)
            except _ConflictingRowTargetError:
                return None
        if not selected_device_id:
            # An OOB row belongs to the page device. A host row of a chassis with no selected or
            # inferred member has no owner: the interface rules would read the wrong platform.
            return obj if obj.virtual_chassis_id is None or port_id in oob_port_ids else None
        target_device = device_for(coerce_model_pk(selected_device_id)) if device_for else None
        if target_device is None:
            return None
        # Re-check that the selected device is the page device or stays in the same virtual chassis.
        if target_device.id != obj.id and (
            obj.virtual_chassis_id is None or target_device.virtual_chassis_id != obj.virtual_chassis_id
        ):
            return None
        return target_device

    def _target_device_for(self, device_id):
        """Return the locked target Device for an ID, or the caller's scoped Device before the lock."""
        if device_id is None:
            return None
        locked_targets = getattr(self, "_locked_target_devices", None)
        if locked_targets is not None:
            return locked_targets.get(device_id)
        # Scoped: the id comes from the POST, and VC membership proves where the device sits,
        # not that the caller's grant covers it.
        return self.restricted_queryset(Device).filter(pk=device_id).first()

    def _resolve_row_target_device(self, obj, port_id=None):
        """
        Resolve the Device a given interface row syncs to.

        A stable port-ID-keyed override must identify an accessible VC member. An invalid, stale,
        or inaccessible explicit target returns ``None``. The relationship phase reuses this
        result so an interface relationship stays on the same owner as the synced row.

        Args:
            obj: The page Device (or VirtualMachine); returned as-is for VMs.
            port_id: The row's stable LibreNMS port_id, when known (keys the override).

        Returns:
            The selected or inferred VC member Device when valid, *obj* for a Device outside a
            chassis or an OOB row, or None when a chassis host row has no member or an explicit
            target is invalid or inaccessible.

        """
        normalized_port_id = normalize_librenms_port_id(port_id)
        return self._row_owner(
            obj,
            normalized_port_id,
            auto_added=normalized_port_id in getattr(self, "_auto_selected_port_ids", set()),
            inferred_ids=getattr(self, "_auto_selected_target_ids", {}),
            oob_port_ids=getattr(self, "_oob_port_ids", set()),
            device_for=self._target_device_for,
        )

    @transaction.atomic
    def sync_interface(  # noqa: C901
        self,
        obj,
        librenms_interface,
        exclude_columns,
        interface_name_field,
        synced_name,
        *,
        name_conflict_reason=None,
        name_owner_port_id=None,
    ):
        """Create or update a single NetBox interface from LibreNMS data."""
        interface_name = synced_name
        raw_port_id = librenms_interface.get("port_id")
        port_id = normalize_librenms_port_id(raw_port_id)
        lookup_port_id = raw_port_id if port_id is not None else None

        if not isinstance(obj, (Device, VirtualMachine)):
            raise ValueError("Invalid object type.")
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        target_device = None
        if isinstance(obj, Device):
            target_device = self._resolve_row_target_device(obj, port_id=port_id)
            if target_device is None:
                # Never sync the row onto the page device instead: a stale or out-of-scope choice,
                # or no member at all, leaves the row without an owner.
                self._record_skipped_conflict(interface_name, self._missing_target_reason(port_id))
                return
        # The rules decide the port for its owner before the resolvers below can create anything.
        owner = target_device if target_device is not None else obj
        try:
            interface_rules_for_request(self.request).decide_interface_write(
                librenms_interface, platform_id=owner.platform_id
            )
        except PortSyncBlocked as blocked:
            self._record_skipped_conflict(interface_name or librenms_interface.get(interface_name_field), str(blocked))
            return
        if port_id is not None:
            claim_librenms_port_binding(port_id, server_key)
        # Resolve the owner only after claiming the cross-model identity.
        try:
            port_owner = find_interface_by_librenms_port_id(port_id, server_key) if port_id is not None else None
            port_owner_is_ambiguous = False
        except AmbiguousLibreNMSIdError:
            # port_id matches multiple interfaces — skip this row rather than bind
            # to an arbitrary one (recorded below).
            logger.warning("Skipping interface row — port_id %s is ambiguous (multiple matches).", port_id)
            port_owner, port_owner_is_ambiguous = None, True
        if port_owner is not None and (
            (
                target_device is not None
                and (not isinstance(port_owner, Interface) or port_owner.device_id != target_device.pk)
            )
            or (
                target_device is None
                and (not isinstance(port_owner, VMInterface) or port_owner.virtual_machine_id != obj.pk)
            )
        ):
            self.__dict__.setdefault("_foreign_bound_port_ids", set()).add(port_id)
            self._record_skipped_conflict(
                interface_name, "LibreNMS port ID is already assigned to another NetBox interface"
            )
            return
        if port_owner_is_ambiguous:
            interface = None
        elif target_device is not None:
            try:
                interface = self._resolve_device_interface(
                    target_device,
                    interface_name,
                    lookup_port_id,
                    server_key,
                    port_owner=port_owner,
                    oob=librenms_interface.get("_source") == OOB_INVENTORY_SOURCE,
                )
            except _HostInterfaceNameConflict:
                self._record_skipped_conflict(interface_name, "host interface already uses this name")
                return
        else:
            interface = self._resolve_vm_interface(
                obj, interface_name, lookup_port_id, server_key, port_owner=port_owner
            )

        if interface is None:
            logger.warning(
                "Skipping interface sync for '%s': unable to resolve target interface safely (port_id=%r).",
                interface_name,
                port_id,
            )
            # Record for the user-facing summary in post(). Defensive getattr: sync_interface
            # may be exercised directly (without post() initialising the list).
            skip_reason = name_conflict_reason or "port already mapped elsewhere or ambiguous"
            if name_owner_port_id is not None:
                skip_reason = f"{skip_reason} {name_owner_port_id}"
            self._record_skipped_conflict(interface_name, skip_reason)
            return

        # An interface resolved and is being synced — count it explicitly (defensive getattr:
        # sync_interface may be exercised directly without post() initialising the counter). The
        # count, not a skip-vs-selected size comparison, drives the success banner in post().
        if getattr(self, "_synced_count", None) is not None:
            self._synced_count += 1

        # The name that the permission check saw; a concurrent rename can put the fresh name out of view.
        checked_name = interface.name
        created = bool(getattr(interface, "_librenms_sync_created", False))
        interface, changed = self.update_interface_attributes(
            interface,
            librenms_interface,
            exclude_columns,
            interface_name_field,
            synced_name,
            created=created,
        )
        changed = changed or created
        # The writer kept the stored name, so the written row still carries it.
        if "name" not in exclude_columns and interface.name != synced_name:
            kept_names = getattr(self, "_kept_name_conflicts", None)
            if kept_names is not None:
                kept_names.append((checked_name, synced_name, name_conflict_reason))

        # Sync VLANs if not excluded, and never when the caller cannot read the whole VLAN scope.
        if "vlans" not in exclude_columns and not getattr(self, "_vlan_scope_incomplete", False):
            changed = self._sync_interface_vlans(interface, librenms_interface) or changed
        if changed and getattr(self, "_mutated", None) is not None:
            self._mutated = True

    def _missing_target_reason(self, port_id):
        """Return why a row resolved to no target device."""
        if port_id in getattr(self, "_auto_selected_port_ids", set()):
            return "selected target unavailable"
        try:
            posted = self._posted_row_target(port_id)
        except _ConflictingRowTargetError:
            return "the request names more than one Virtual Chassis member for it"
        if not posted and port_id not in getattr(self, "_auto_selected_target_ids", {}):
            return "select the Virtual Chassis member that owns it"
        return "selected target unavailable"

    def _record_skipped_conflict(self, interface_name, reason):
        """Record a row that cannot be synced to its requested target."""
        skipped = getattr(self, "_skipped_conflicts", None)
        if skipped is not None:
            skipped.append(f"{interface_name or '(unnamed)'} ({reason})")

    def _oob_row_is_unsyncable(self, port, interface_name_field, rejected_names):
        """
        Report whether a selected OOB row must be skipped, recording why.

        A shared LOM is one physical port reported on both sides, so syncing both rows would
        model it twice.

        Args:
            port (dict): The OOB port row.
            interface_name_field (str): Port field that contains the selected interface name.
            rejected_names (dict[int, str]): Rejection reason by normalized port ID.

        Returns:
            bool: True when the row was skipped and the skip recorded.

        """
        if port.get("_dedup_conflict"):
            self._record_skipped_conflict(
                port.get(interface_name_field),
                "shared LOM already synced from the host side",
            )
            return True
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if rejected_names.get(port_id) == HOST_NAME_COLLISION_REASON:
            self._record_skipped_conflict(port.get(interface_name_field), HOST_NAME_COLLISION_REASON)
            return True
        return False

    def _resolve_device_interface(self, target_device, interface_name, port_id, server_key, *, port_owner, oob=False):
        """Resolve a device interface from the port's owner first, then safe name fallback."""
        changeable = self.restricted_queryset(Interface, "change")
        if port_id and port_owner is not None:
            if not isinstance(port_owner, Interface) or port_owner.device_id != target_device.pk:
                raise LibreNMSPortBindingConflict(
                    "The LibreNMS port ID is already assigned to another NetBox interface."
                )
            return port_owner if changeable.filter(pk=port_owner.pk).exists() else None
        if interface_name is None:
            return None
        interface, created = Interface.objects.get_or_create(device=target_device, name=interface_name)
        if oob and not created:
            # The controller row has no claim on an existing host interface by name.
            if not self.restricted_queryset(Interface).filter(pk=interface.pk).exists():
                return None
            raise _HostInterfaceNameConflict
        if not created and port_id and not interface_name_fallback_matches_port(interface, port_id, server_key):
            return None
        if created:
            interface._librenms_sync_created = True
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def _resolve_vm_interface(self, vm, interface_name, port_id, server_key, *, port_owner):
        """Resolve a VM interface from the port's owner first, then safe name fallback."""
        changeable = self.restricted_queryset(VMInterface, "change")
        if port_id and port_owner is not None:
            if not isinstance(port_owner, VMInterface) or port_owner.virtual_machine_id != vm.pk:
                raise LibreNMSPortBindingConflict(
                    "The LibreNMS port ID is already assigned to another NetBox interface."
                )
            return port_owner if changeable.filter(pk=port_owner.pk).exists() else None
        if interface_name is None:
            return None
        interface, created = VMInterface.objects.get_or_create(virtual_machine=vm, name=interface_name)
        if not created and port_id and not interface_name_fallback_matches_port(interface, port_id, server_key):
            return None
        if created:
            interface._librenms_sync_created = True
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def update_interface_attributes(
        self,
        interface,
        librenms_interface,
        exclude_columns,
        interface_name_field,
        synced_name,
        *,
        created,
    ):
        """Update interface fields from LibreNMS data, respecting excluded columns (``created`` and the result as in the writer)."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        return update_interface_from_port(
            interface,
            librenms_interface,
            rules=interface_rules_for_request(self.request),
            synced_name=synced_name,
            server_key=server_key,
            interface_name_field=interface_name_field,
            created=created,
            changeable_queryset=self.restricted_queryset(type(interface), "change"),
            exclude_columns=exclude_columns,
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

        # Build VLAN data from port. "mode" is the 802.1Q mode LibreNMS reported via ifTrunk.
        vlan_data = {
            "mode": librenms_port.get("mode"),
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
                return False
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
        result = self._update_interface_vlan_assignment(
            interface,
            vlan_data,
            vlan_group_map,
            lookup_maps,
            changeable_queryset=self.restricted_queryset(type(interface), "change"),
        )
        return bool(result and result.get("changed"))


class _RebindRefusedError(Exception):
    """A rebind precondition failed; the message is safe to show the caller."""


class RebindInterfacePortView(SyncInterfacesView):
    """
    Move a stale LibreNMS binding to the host row whose reported name the bound interface holds.

    Only the row's port ID comes from the POST. The row, its rejection, the name holder and its
    classification are re-derived from the cached snapshot the same way the sync writer derives them.
    """

    def get_required_permissions_for_object_type(self, object_type):
        """Return the required permissions based on object type."""
        if object_type == "device":
            return [("view", Device), ("change", Interface)]
        if object_type == "virtualmachine":
            return [("view", VirtualMachine), ("change", VMInterface)]
        raise Http404(f"Invalid object type: {object_type}")

    def post(self, request, object_type, object_id):
        """Rebind the interface that holds one host row's reported name to that row's port."""
        self.required_object_permissions = {
            "POST": self.get_required_permissions_for_object_type(object_type),
        }
        if error := self.require_all_permissions("POST"):
            return error

        obj = self.get_object(object_type, object_id)
        self.object = obj
        interface_name_field = get_interface_name_field(request, obj)
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return self._tab_response(request, object_type, interface_name_field, None)
        if isinstance(obj, Device) and build_migrated_context(obj, server_key).get("migrated_to_marker"):
            messages.error(request, "This LibreNMS source has been migrated and is read-only.")
            return self._tab_response(request, object_type, interface_name_field, server_key)
        port_id = normalize_librenms_port_id(request.POST.get("rebind_one"))
        expected_port_id = normalize_librenms_port_id(request.POST.get(f"rebind_expected_port_{port_id}"))
        if port_id is None or expected_port_id is None:
            messages.error(request, "The Rebind request is incomplete. Refresh the page and try again.")
            return self._tab_response(request, object_type, interface_name_field, server_key)

        self._post_server_key = server_key
        self._selected_port_ids = {port_id}
        self._auto_selected_port_ids = set()
        ports_data = self.get_cached_ports_data(request, obj, server_key)
        if ports_data is None:
            return self._tab_response(request, object_type, interface_name_field, server_key)
        try:
            with transaction.atomic():
                interface = self._rebind(obj, ports_data, port_id, expected_port_id, interface_name_field, server_key)
        except LibreNMSPortBindingConflict as conflict:
            messages.error(request, str(conflict))
            return self._tab_response(request, object_type, interface_name_field, server_key)
        except _RebindRefusedError as refusal:
            messages.error(request, str(refusal))
            return self._tab_response(request, object_type, interface_name_field, server_key)
        finally:
            self.__dict__.pop("_locked_target_devices", None)

        messages.success(
            request,
            f"NetBox interface '{interface.name}' is now bound to LibreNMS port {port_id} instead of port "
            f"{expected_port_id}. Sync the row to update its other fields.",
        )
        transition = schedule_request_cache_mutation(request, obj, SyncTab.INTERFACES, server_key)
        return apply_transition_to_response(
            request, self._tab_response(request, object_type, interface_name_field, server_key), transition
        )

    def _rebind(self, obj, ports_data, port_id, expected_port_id, interface_name_field, server_key):
        """
        Re-derive every precondition under lock, then move the binding.

        Args:
            obj (Device | VirtualMachine): The page object.
            ports_data (list[dict]): The cached snapshot rows.
            port_id (int): The submitted row's normalized port ID.
            expected_port_id (int): The holder's port ID that the operator saw and confirmed.
            interface_name_field (str): Port field that contains the selected interface name.
            server_key (str): The active LibreNMS server key.

        Returns:
            Interface | VMInterface: The rebound interface.

        Raises:
            _RebindRefusedError: When a precondition fails.

        """
        rows = [port for port in ports_data if normalize_librenms_port_id(port.get("port_id")) == port_id]
        if len(rows) != 1 or rows[0].get("_source") == OOB_INVENTORY_SOURCE:
            raise _RebindRefusedError(
                f"LibreNMS port {port_id} is not one host row in the cached data. Refresh the data and try again."
            )
        locked_obj = self._lock_sync_scope(obj, ports_data, interface_name_field)
        if locked_obj is None:
            raise _RebindRefusedError("The interface owner is no longer available.")
        writer_model = VMInterface if isinstance(locked_obj, VirtualMachine) else Interface
        decisions = self._snapshot_name_decisions(
            locked_obj, ports_data, interface_name_field, writer_model, server_key
        )
        target_id = decisions.target_device_ids.get(port_id)
        target = self._locked_target_devices.get(target_id) if isinstance(locked_obj, Device) else locked_obj
        if target is None:
            raise _RebindRefusedError(f"LibreNMS port {port_id} has no interface owner. Select one and try again.")
        if isinstance(locked_obj, Device) and get_migrated_to_marker(target, server_key):
            raise _RebindRefusedError("The row's device has been migrated and is read-only.")
        # A rebind writes the interface's binding, so the rules decide the port first.
        rule_refusal = decision_reason(
            interface_rules_for_request(self.request).check_interface_write(rows[0], platform_id=target.platform_id)
        )
        if rule_refusal is not None:
            raise _RebindRefusedError(f"Rebind is refused for LibreNMS port {port_id}: {rule_refusal}.")
        owner = decisions.owners.get(port_id)
        if owner is None or decisions.rejected.get(port_id) != REPORTED_NAME_PORT_COLLISION_REASON:
            raise _RebindRefusedError(
                f"No other LibreNMS port holds the reported name of port {port_id}. Refresh the data and try again."
            )
        holder_filter = self._name_holder_filter(writer_model, target_id, owner.name)
        interface = (
            self.restricted_queryset(writer_model, "change")
            .select_for_update(of=("self",))
            .filter(**holder_filter)
            .first()
        )
        # Only a holder the caller may both view and change can name its port in a message.
        if interface is None or not self.restricted_queryset(writer_model, "view").filter(pk=interface.pk).exists():
            raise _RebindRefusedError(f"You cannot change NetBox interface '{owner.name}'.")
        current_port_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
        if current_port_id != expected_port_id or owner.port_id != expected_port_id:
            raise _RebindRefusedError(
                f"NetBox interface '{owner.name}' changed after the page was loaded. Refresh the data and try again."
            )
        if owner.status != NAME_OWNER_STALE:
            raise _RebindRefusedError(f"Rebind is refused. {owner.explanation()}")
        claim_librenms_port_binding(port_id, server_key)
        try:
            port_is_bound = find_interface_by_librenms_port_id(port_id, server_key) is not None
        except AmbiguousLibreNMSIdError:
            port_is_bound = True
        if port_is_bound:
            raise _RebindRefusedError(f"LibreNMS port {port_id} is already bound to a NetBox interface.")
        interface.snapshot()
        set_librenms_device_id(interface, port_id, server_key)
        if get_librenms_device_id(interface, server_key, auto_save=False) != port_id:
            raise _RebindRefusedError(
                f"NetBox interface '{owner.name}' stores its LibreNMS ID in the legacy format. Convert it first."
            )
        interface.save()
        return interface


class DeleteNetBoxInterfacesView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """Delete interfaces that exist only in NetBox."""

    DROP_SYNC_SUBJECT_CLAIM_WITHOUT_SERVER = True

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

    def post(self, request, object_type, object_id):  # noqa: C901
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

        server_key = self.resolve_posted_server_key_or_none(request.POST)

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

        if deleted_count and server_key:
            schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.INTERFACES,
                server_key,
                source_fragment_required=True,
            )
        return apply_request_cache_transition(request, JsonResponse(response_data))


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


def _relationship_decisions(rules, *ends):
    """
    Decide each ``(port, interface)`` end of a relationship edge for an interface write.

    Each end reads the platform of the object that owns its interface.

    Args:
        rules (InterfaceRuleMatcher): The request's rule snapshot.
        *ends (tuple): ``(port record or None, interface)`` per end.

    Returns:
        tuple: The decisions (None when an end is blocked), the first blocked interface, and why.

    """
    decisions = []
    for port, interface in ends:
        if port is None:
            return None, interface, "not in the cached LibreNMS data; refresh the data"
        decision = rules.check_interface_write(port, platform_id=interface_owner_platform_id(interface))
        reason = decision_reason(decision)
        if reason is not None:
            return None, interface, reason
        decisions.append(decision)
    return tuple(decisions), None, None


def _promotion_conflict(decision, target_type):
    """Return why promoting an end to *target_type* would override its Set type rule, or None."""
    if decision.kind is RuleDecisionKind.SET_TYPE and decision.netbox_type != target_type:
        return f"interface {rule_names(decision.rules)} sets type {decision.netbox_type}, not {target_type}"
    return None


def _lag_aggregate_needs_promotion(agg):
    """Return whether *agg* is an Interface that is not yet ``type=lag``."""
    return isinstance(agg, Interface) and agg.type != "lag"


def _promote_lag_aggregate(agg, *, with_restore):
    """
    Bump a LAG aggregate to ``type=lag`` in memory; NetBox's ``clean()`` does not require it.

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
    if not _lag_aggregate_needs_promotion(agg):
        return None
    original_type = agg.type
    agg.type = "lag"

    def _persist():
        # NetBox 4.4 can crash while it validates a cross-member parent before it reports
        # the type conflict. Reject the invalid aggregate state before calling clean().
        if agg.parent_id is not None:
            raise ValidationError({"type": "A LAG aggregate cannot have a parent interface."})
        # Validate the rest of the prepared aggregate state before saving its new type.
        agg.clean()
        agg.save(update_fields=["type"])
        logger.info("Set interface %s type=lag", agg.name)

    if with_restore:
        return (_persist, lambda: setattr(agg, "type", original_type))
    return _persist


def _parent_child_needs_promotion(child):
    """Return whether a physical device interface needs promotion before it can have a parent."""
    return isinstance(child, Interface) and child.is_wired and getattr(child, "channel_id", None) is None


def _promote_parent_child(child, *, with_restore):
    """Promote a non-channel child to type=virtual before parent validation."""
    if not _parent_child_needs_promotion(child):
        return None
    original_type = child.type
    child.type = "virtual"

    def _persist():
        child.save(update_fields=["type"])
        logger.info("Set interface %s type=virtual", child.name)

    if with_restore:
        return (_persist, lambda: setattr(child, "type", original_type))
    return _persist


def _apply_interface_relationship(
    source_iface,
    relation_field,
    related_iface,
    prepare_related=None,
    prepare_source=None,
):
    """
    Set ``source_iface.<relation_field> = related_iface``, validate, and persist both sides.

    The single place the relationship set -> validate -> persist sequence lives, shared by the
    bulk pass (:meth:`SyncInterfacesView._apply_relationship_edge`) and the inline single-row
    endpoints (:class:`_BaseRelationshipSyncView`) so a fix applies once, not twice.

    The optional preparation hooks may mutate either interface before validation. Each hook
    returns a persist callable or a ``(persist, restore)`` pair. The persist call runs only after
    validation. The restore call repairs shared in-memory objects after a failed edge.

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
    prepared_source = prepare_source(source_iface) if prepare_source else None
    prepared_related = prepare_related(related_iface) if prepare_related else None

    def _callbacks(prepared):
        return prepared if isinstance(prepared, tuple) else (prepared, None)

    persist_source, restore_source = _callbacks(prepared_source)
    persist_related, restore_related = _callbacks(prepared_related)

    def _restore_in_memory():
        setattr(source_iface, relation_id_field, original_related_id)
        if restore_source:
            restore_source()
        if restore_related:
            restore_related()

    try:
        # These are existing, DB-valid rows and this path changes only one relationship FK.
        # NetBox's model clean() contains the cross-owner/type/self-link rules that matter here.
        # Running full_clean() would revalidate every unchanged FK and uniqueness constraint,
        # adding several SELECTs per edge while all relationship rows remain locked.
        netbox_interface_clean(source_iface)
        if persist_related:
            persist_related()
        if persist_source:
            persist_source()
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
    Shared skeleton for the inline single-row relationship-sync endpoints.

    The LAG, parent, and bridge views share the permission gate, current-cache
    edge validation, stable port ID resolution, and one transactional write path. They differ
    only in the FK attribute, POST field, wording, VM support, and preparation hooks. Keeping
    one flow here stops the endpoints drifting (a fix to the resolve/validate/persist
    sequence applies once, not twice).

    Subclass contract (class attributes):
        relation_field   -- the interface FK attribute set ("lag" | "parent" | "bridge").
        related_port_param -- the POST field carrying the related port ID.
        relation_label   -- human label in messages.
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
        Prepare the related interface in memory before the source is validated.

        Returns a no-arg callable that persists that mutation (invoked only after the source
        interface validates) or None when there's nothing to do. SyncInterfaceLagView
        overrides this to bump the aggregate's type to 'lag'; parent has no equivalent.

        Args:
            related_iface (Interface | VMInterface): The related interface that a subclass can prepare.

        """
        return None

    def _prepare_source(self, source_iface):
        """Prepare the source interface before validation when a subclass requires it."""
        return None

    def _related_needs_preparation(self, related_iface):
        """Report whether _prepare_related has work to do on an already-linked pair."""
        return False

    def _source_needs_preparation(self, source_iface):
        """Return whether source preparation must repair an existing relationship."""
        return False

    def _promotion_conflict(self, source_iface, related_iface, decisions):
        """Return ``(interface, reason)`` when a needed promotion would override a Set type rule."""
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
        relationship_map_names = {
            "lag": "lag_members",
            "parent": "sub_interfaces",
            "bridge": "bridge_members",
        }
        map_name = relationship_map_names[self.relation_field]
        raw_edges = relationships.get(map_name)
        if not isinstance(raw_edges, dict):
            return None

        lag_members, sub_interfaces, bridge_members = normalize_relationship_maps(relationships)
        edges = {
            "lag": lag_members,
            "parent": sub_interfaces,
            "bridge": bridge_members,
        }[self.relation_field]
        source_id = normalize_librenms_port_id(port_id)
        related_id = normalize_librenms_port_id(related_port_id)
        if source_id is None or related_id is None or edges.get(source_id) != related_id:
            return None

        ports_by_id = {}
        duplicate_port_ids = set()
        for port in ports:
            normalized_id = normalize_librenms_port_id(port.get("port_id"))
            if port.get("_source") == OOB_INVENTORY_SOURCE or normalized_id is None:
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

    def post(self, request, object_type, object_id):  # noqa: C901
        # Set the object-type-scoped permissions BEFORE the gate (an unsupported type raises
        # Http404 here). JSON endpoint: require_all_permissions would return the mixin's
        # HTML/redirect on denial, breaking the fetch() caller, so use the _json variant.
        self.required_object_permissions = self._required_permissions(object_type)
        if error := self.require_all_permissions_json("POST"):
            return error

        obj = self._get_object(object_type, object_id)
        server_key = self.rebind_api_for_posted_server(request.POST)
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
        relationship_changed = False
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
                decisions, blocked_end, reason = _relationship_decisions(
                    interface_rules_for_request(request), (source_port, source_iface), (related_port, related_iface)
                )
                if reason is None and (conflict := self._promotion_conflict(source_iface, related_iface, decisions)):
                    blocked_end, reason = conflict
                if reason is not None:
                    return JsonResponse(
                        {
                            "error": (
                                f"Cannot link {source_iface.name} to {self.relation_label} {related_iface.name}. "
                                f"{blocked_end.name}: {reason}."
                            )
                        },
                        status=409,
                    )
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
                    if (
                        getattr(source_iface, f"{self.relation_field}_id") != related_iface.pk
                        or self._related_needs_preparation(related_iface)
                        or self._source_needs_preparation(source_iface)
                    ):
                        _apply_interface_relationship(
                            source_iface,
                            self.relation_field,
                            related_iface,
                            self._prepare_related,
                            self._prepare_source,
                        )
                        relationship_changed = True
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
                if relationship_changed:
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

        if relationship_changed:
            schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.INTERFACES,
                server_key,
                source_fragment_required=True,
            )
        response = JsonResponse(
            {
                "status": "success",
                "message": f"Linked {source_iface.name} to {self.relation_label} {related_iface.name}",
            }
        )
        return apply_request_cache_transition(request, response)


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
        """Promote the aggregate to type=lag; NetBox's clean() accepts a link to any type."""
        # Single-row endpoint: no aggregate reuse across rows, so no restore needed.
        return _promote_lag_aggregate(related_iface, with_restore=False)

    def _related_needs_preparation(self, related_iface):
        """Check whether the aggregate needs promotion back to a LAG type."""
        return _lag_aggregate_needs_promotion(related_iface)

    def _promotion_conflict(self, source_iface, related_iface, decisions):
        """Refuse a promotion to LAG that a Set type rule on the aggregate contradicts."""
        conflict = _promotion_conflict(decisions[1], "lag") if _lag_aggregate_needs_promotion(related_iface) else None
        return (related_iface, conflict) if conflict else None


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

    def _prepare_source(self, source_iface):
        """Promote a non-channel device interface so NetBox accepts its parent."""
        return _promote_parent_child(source_iface, with_restore=False)

    def _source_needs_preparation(self, source_iface):
        """Repair a parent child that was edited back to a physical type."""
        return _parent_child_needs_promotion(source_iface)

    def _promotion_conflict(self, source_iface, related_iface, decisions):
        """Refuse a promotion to virtual that a Set type rule on the child contradicts."""
        conflict = _promotion_conflict(decisions[0], "virtual") if _parent_child_needs_promotion(source_iface) else None
        return (source_iface, conflict) if conflict else None


class SyncInterfaceBridgeView(_BaseRelationshipSyncView):
    """Set Interface.bridge or VMInterface.bridge from LibreNMS port-stack data."""

    relation_field = "bridge"
    related_port_param = "bridge_port_id"
    relation_label = "bridge"
    source_label = "Member"
    related_label = "Bridge"
    supports_vm = True
