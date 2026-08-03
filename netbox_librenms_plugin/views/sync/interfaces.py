import logging
from urllib.parse import quote_plus

from dcim.models import Device, Interface, MACAddress
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    convert_speed_to_kbps,
    find_by_librenms_id,
    get_interface_name_field,
    get_librenms_device_id,
    get_librenms_sync_device,
    normalize_librenms_port_id,
    normalize_relationship_maps,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

logger = logging.getLogger(__name__)


def _validation_error_detail(exc: ValidationError) -> str:
    """Flatten a ValidationError into a single human-readable string for a JSON error body."""
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{field}: {' '.join(str(m) for m in msgs)}" for field, msgs in exc.message_dict.items())
    return "; ".join(str(m) for m in exc.messages) if hasattr(exc, "messages") else str(exc)


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

        interface_name_field = get_interface_name_field(request)
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
        selected_interfaces = self.get_selected_interfaces(request, interface_name_field)
        # Stable LibreNMS port_ids selected directly (e.g. cross-page parents auto-included
        # by the LAG/parent JS). Display names ("select") can collide in ifDescr mode, so
        # the relationship sync keys on these ids to avoid binding the wrong port.
        self._selected_port_ids = {pid for pid in request.POST.getlist("select_port_id") if pid}
        exclude_columns = request.POST.getlist("exclude_columns")

        redirect_url = (
            reverse(url_name, kwargs={"pk": object_id})
            # quote_plus the field too: it comes from the request, so unescaped special chars
            # could corrupt the redirect or inject extra query params (issue #107).
            + f"?tab=interfaces&interface_name_field={quote_plus(interface_name_field)}"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        if selected_interfaces is None:
            return redirect(redirect_url)

        ports_data = self.get_cached_ports_data(request, obj, server_key)
        if ports_data is None:
            return redirect(redirect_url)

        # Prepare VLAN lookup maps if VLAN sync is enabled
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
        self._lookup_maps = lookup_maps

        # Collects interfaces skipped because their LibreNMS port_id resolves to an
        # interface on a *different* device (see _resolve_device/vm_interface). Surfaced
        # below so the skip isn't silent — otherwise the user only sees it in the logs.
        self._skipped_conflicts = []
        self._synced_count = 0
        self.sync_selected_interfaces(obj, selected_interfaces, ports_data, exclude_columns, interface_name_field)

        # After all interfaces are created/updated, set LAG and parent relationships
        relationships = self._get_cached_relationships(obj, server_key)
        self._sync_lag_and_parent_relationships(obj, selected_interfaces, ports_data, relationships, server_key)

        if self._skipped_conflicts:
            skipped = ", ".join(self._skipped_conflicts)
            messages.warning(
                request,
                f"{len(self._skipped_conflicts)} interface(s) skipped: {skipped}.",
            )
        # Only claim success when at least one interface was actually synced. Track an explicit
        # synced count rather than comparing skip-vs-selected sizes: a single selected display name
        # can match MULTIPLE LibreNMS ports (ifName/ifDescr collisions matched by name membership in
        # sync_selected_interfaces), so if one colliding port fails and another succeeds
        # len(_skipped_conflicts) can reach len(selected_interfaces) even though something WAS
        # synced — which would wrongly suppress the banner.
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

    def get_selected_interfaces(self, request, interface_name_field):
        """Return the list of selected interface names from POST data."""
        selected_interfaces = request.POST.getlist("select")
        if not selected_interfaces:
            messages.error(request, "No interfaces selected for synchronization.")
            return None
        return selected_interfaces

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

    def _sync_lag_and_parent_relationships(self, obj, selected_interfaces, ports_data, relationships, server_key):
        """
        Set LAG member and sub-interface parent relationships for synced interfaces.

        Runs after sync_selected_interfaces() so all interfaces already exist in NetBox.
        Only processes relationships where this interface is a member/child — the
        aggregate/parent may or may not be in the selected set (it just needs to exist
        in NB).

        Args:
            obj: The Device (or VirtualMachine) being synced.
            selected_interfaces: The interface display names selected for sync.
            ports_data: The LibreNMS port dicts for the device.
            relationships (dict): The ``{lag_members, sub_interfaces}`` mapping to apply.
            server_key (str): The LibreNMS server key scoping stored-id reads.

        Returns:
            None
        """
        if not relationships:
            return

        # Normalize the relationship-map keys once at load through the shared helper (the same one
        # get_context_data / SingleInterfaceVerifyView use via _build_relationship_maps), so the
        # bulk path can't drift on the corruption guard or key normalization. resolve_port_relationships
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

        # Resolve the selection to stable LibreNMS port_ids. The POST carries display names
        # ("select") plus any cross-page parents injected by stable id ("select_port_id").
        # Keying by port_id avoids collapsing distinct ports that share a display name —
        # possible in ifDescr mode, where the visible name is not a unique identifier.
        port_by_id = {}
        selected_port_ids = set(getattr(self, "_selected_port_ids", set()))
        for port in ports_data:
            # OOB-controller rows are context-only (merged for shared-LOM display); sync_selected_interfaces()
            # already skips them. Exclude them here too: on a page where an OOB row shares the host display
            # name, adding its port_id would let the LAG/parent pass persist links on the hidden controller
            # row instead of the host interface.
            if port.get("_source") == "oob":
                continue
            pid = port.get("port_id")
            if pid is None:
                continue
            port_by_id[str(pid)] = port
            if port.get(interface_name_field) in selected_interfaces:
                selected_port_ids.add(str(pid))

        # Build the interface lookup index once for the whole batch. Each port_id below
        # resolves both ends of a relationship; without a shared index every resolution
        # re-queries obj's interfaces and re-reads their librenms_id custom fields, which is
        # O(selected × interfaces). Built after sync_selected_interfaces() so newly created
        # interfaces are included.
        iface_index = _build_interface_index(obj, server_key)

        try:
            with transaction.atomic():
                for port_id in selected_port_ids:
                    if port_id not in port_by_id:
                        continue

                    # Pin both ends of the relationship to the owner this row was synced onto (the
                    # per-row device_selection target, or the VM). The VC-wide port_id search can
                    # otherwise resolve a child/parent uniquely onto a *different* member that carries
                    # the same stale librenms_id, persisting lag/parent on the wrong interface.
                    row_name = port_by_id[port_id].get(interface_name_field)
                    target_device = self._resolve_row_target_device(obj, row_name, port_id=port_id)
                    if target_device is None:
                        continue
                    expected_owner = _interface_owner_for_object(target_device)

                    # LAG membership: this interface is a member of a LAG aggregate. Both ends are
                    # resolved/validated/persisted by the shared helpers below (same flow as the
                    # parent pass), differing only in the Interface-only source guard and the
                    # aggregate type=lag promotion.
                    raw_lag = lag_members.get(normalize_librenms_port_id(port_id))
                    if raw_lag is not None:
                        member_iface, agg_iface = self._resolve_relationship_ends(
                            obj,
                            port_id,
                            raw_lag,
                            port_by_id,
                            iface_index,
                            server_key,
                            expected_owner,
                            interface_name_field,
                            "LAG",
                            require_interface_source=True,  # VMInterface has no lag field
                        )
                        if member_iface and member_iface.lag_id != agg_iface.pk:
                            if not _interfaces_same_owner(member_iface, agg_iface):
                                logger.warning(
                                    "Bulk sync: skipping cross-member LAG link %s -> %s (different devices)",
                                    member_iface.name,
                                    agg_iface.name,
                                )
                            else:
                                self._apply_relationship_edge(
                                    member_iface, "lag", agg_iface, self._prepare_bulk_lag_aggregate, "LAG"
                                )

                    # Sub-interface parent: this interface is a child of a parent interface.
                    raw_parent = sub_interfaces.get(normalize_librenms_port_id(port_id))
                    if raw_parent is not None:
                        child_iface, parent_iface = self._resolve_relationship_ends(
                            obj,
                            port_id,
                            raw_parent,
                            port_by_id,
                            iface_index,
                            server_key,
                            expected_owner,
                            interface_name_field,
                            "parent",
                        )
                        if child_iface and child_iface.parent_id != parent_iface.pk:
                            if not _interfaces_same_owner(child_iface, parent_iface):
                                logger.warning(
                                    "Bulk sync: skipping cross-member parent link %s -> %s (different devices)",
                                    child_iface.name,
                                    parent_iface.name,
                                )
                            else:
                                self._apply_relationship_edge(child_iface, "parent", parent_iface, None, "parent")
        except IntegrityError:
            # Django's Postgres FK constraints are INITIALLY DEFERRED: a related row
            # deleted mid-batch surfaces only at this block's COMMIT — after every
            # per-row guard has already passed — so it cannot be caught per edge. The
            # relationship pass rolls back as a unit (the interface sync itself already
            # committed); fail soft with a retry hint instead of 500ing the sync POST.
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

        member_iface.full_clean() only accepts the link when the aggregate is type=lag, so it
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
        iface_index,
        server_key,
        expected_owner,
        interface_name_field,
        log_kind,
        *,
        require_interface_source=False,
    ):
        """
        Resolve the ``(source, related)`` interface pair for one bulk LAG/parent edge.

        Both ends are resolved by stable LibreNMS port_id (owner-pinned, via the shared index).
        Returns ``(None, None)`` — skip the row — on any lookup failure (logged at debug) or,
        when *require_interface_source* is set, when the source isn't an Interface (a
        VMInterface has no lag field).
        """
        related_port_id = str(related_raw)
        related_entry = port_by_id.get(related_port_id, {})
        # Use the active display field for the name fallback: in ifDescr mode the NetBox
        # interface name matches ifDescr, so hinting ifName would look up the wrong name and
        # silently skip the link. Fall back to ifName if absent.
        related_name = related_entry.get(interface_name_field) or related_entry.get("ifName", "")

        source_iface, err = _resolve_interface_by_port_id(
            obj, port_id, server_key, expected_owner=expected_owner, index=iface_index
        )
        if err:
            logger.debug("%s source lookup failed during bulk sync: %s", log_kind, err)
            return None, None
        if require_interface_source and not isinstance(source_iface, Interface):
            return None, None  # VMInterface does not support lag

        related_iface, err = _resolve_interface_by_port_id(
            obj, related_port_id, server_key, name_hint=related_name, expected_owner=expected_owner, index=iface_index
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
                _validation_error_detail(exc),
            )
            return
        except IntegrityError as exc:
            # Concurrent DB conflict (e.g. the related interface deleted between full_clean's
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
        selected_interfaces,
        ports_data,
        exclude_columns,
        interface_name_field,
    ):
        """Create or update NetBox interfaces from LibreNMS port data."""
        selected_port_ids = getattr(self, "_selected_port_ids", set())
        with transaction.atomic():
            if isinstance(obj, Device):
                locked_targets = self._lock_selected_device_targets(
                    obj,
                    selected_interfaces,
                    selected_port_ids,
                    ports_data,
                    interface_name_field,
                )
                obj = locked_targets.get(obj.pk)
                if obj is None:
                    for interface_name in selected_interfaces:
                        self._record_skipped_conflict(interface_name, "selected target unavailable")
                    return
                self._locked_target_devices = locked_targets
            try:
                for port in ports_data:
                    # OOB-controller rows are merged into the host's interface list only for context
                    # (shared-LOM detection) and are never routed to a real target device by
                    # sync_interface(). They must not sync onto the host — and skipping them prevents
                    # a main/OOB interface-name collision (both "eth0") from double-processing one
                    # selection and overwriting the host interface with the OOB row's port_id/attrs.
                    if port.get("_source") == "oob":
                        continue
                    port_name = port.get(interface_name_field)
                    port_id = port.get("port_id")

                    if port_name in selected_interfaces or (port_id is not None and str(port_id) in selected_port_ids):
                        self.sync_interface(obj, port, exclude_columns, interface_name_field)
            finally:
                self.__dict__.pop("_locked_target_devices", None)

    def _lock_selected_device_targets(
        self,
        obj,
        selected_interfaces,
        selected_port_ids,
        ports_data,
        interface_name_field,
    ):
        """Lock the page device and selected VC targets for the sync transaction."""
        target_ids = {obj.pk}
        for port in ports_data:
            if port.get("_source") == "oob":
                continue
            interface_name = port.get(interface_name_field)
            port_id = port.get("port_id")
            if interface_name not in selected_interfaces and (
                port_id is None or str(port_id) not in selected_port_ids
            ):
                continue
            selected_id = self.request.POST.get(f"device_selection_{interface_name}")
            if selected_id:
                try:
                    target_ids.add(int(selected_id))
                except (TypeError, ValueError):
                    continue

        queryset = self.restricted_queryset(Device).select_for_update(of=("self",))
        return {device.pk: device for device in queryset.filter(pk__in=target_ids).order_by("pk")}

    def _resolve_row_target_device(self, obj, interface_name, port_id=None):
        """
        Resolve the Device a given interface row syncs to.

        Prefers a stable port-id-keyed override ``device_selection_port_<port_id>`` and falls back
        to ``device_selection_<name>``. Both must identify an accessible VC member. An invalid,
        stale, or inaccessible explicit target returns ``None``. The relationship phase reuses
        this result so a lag or parent link stays on the same owner as the synced row.

        The port-id-keyed form exists for a cross-page parent: when a sub-interface's parent lives
        on a different table page, the JS injects the parent only by its stable ``select_port_id``
        (there is no ``device_selection_<name>`` for it) plus ``device_selection_port_<port_id>``
        carrying the child row's live VC-member selection, so the off-page parent resolves onto the
        correct member instead of defaulting to the page device.

        Args:
            obj: The page Device (or VirtualMachine); returned as-is for VMs.
            interface_name (str): The interface row's name (keys the POST selection).
            port_id: The row's stable LibreNMS port_id, when known (keys the override).

        Returns:
            The selected VC member Device when valid, *obj* when no target was selected,
            or None when an explicit target is invalid or inaccessible.
        """
        if not isinstance(obj, Device):
            return obj
        selected_device_id = None
        if port_id is not None:
            selected_device_id = self.request.POST.get(f"device_selection_port_{port_id}")
        if not selected_device_id:
            selected_device_id = self.request.POST.get(f"device_selection_{interface_name}")
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

    def _vc_member_ids(self, obj):
        """
        VC member-id set for *obj*, computed once per request.

        ``_resolve_row_target_device`` runs once per selected row across two passes
        (interface sync + the relationship pass); the member set is loop-invariant, so cache it
        on the (per-request) view instance instead of re-querying ``members.values_list`` per row.
        """
        cached = getattr(self, "_vc_member_ids_cache", None)
        if cached is None:
            cached = set(obj.virtual_chassis.members.values_list("id", flat=True))
            self._vc_member_ids_cache = cached
        return cached

    def sync_interface(self, obj, librenms_interface, exclude_columns, interface_name_field):
        """Create or update a single NetBox interface from LibreNMS data."""
        interface_name = librenms_interface.get(interface_name_field)
        port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))

        if isinstance(obj, Device):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            target_device = self._resolve_row_target_device(obj, interface_name, port_id=port_id)
            if target_device is None:
                # The user explicitly selected a target. If it is stale or outside the
                # caller's grant, do not silently sync the row onto the page device.
                self._record_skipped_conflict(interface_name, "selected target unavailable")
                return
            interface = self._resolve_device_interface(target_device, interface_name, port_id, server_key)
        elif isinstance(obj, VirtualMachine):
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            interface = self._resolve_vm_interface(obj, interface_name, port_id, server_key)
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
            self._sync_interface_vlans(interface, librenms_interface, interface_name)

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
                    return existing_by_name if changeable.filter(pk=existing_by_name.pk).exists() else None
                return None
        interface, created = Interface.objects.get_or_create(device=target_device, name=interface_name)
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
                    return existing_by_name if changeable.filter(pk=existing_by_name.pk).exists() else None
                return None
        interface, created = VMInterface.objects.get_or_create(virtual_machine=vm, name=interface_name)
        return interface if created or changeable.filter(pk=interface.pk).exists() else None

    def get_netbox_interface_type(self, librenms_interface):
        """Return the NetBox interface type mapped from LibreNMS type and speed."""
        speed = convert_speed_to_kbps(librenms_interface.get("ifSpeed"))
        mappings = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface.get("ifType"))

        if speed is not None:
            speed_mapping = mappings.filter(librenms_speed__lte=speed).order_by("-librenms_speed").first()
            mapping = speed_mapping or mappings.filter(librenms_speed__isnull=True).first()
        else:
            mapping = mappings.filter(librenms_speed__isnull=True).first()

        return mapping.netbox_type if mapping else "other"

    def handle_mac_address(self, interface, ifPhysAddress):
        """Assign or create the MAC address for the given interface."""
        if ifPhysAddress:
            existing_mac = interface.mac_addresses.filter(mac_address=ifPhysAddress).first()
            if existing_mac:
                mac_obj = existing_mac
            else:
                mac_obj = MACAddress.objects.create(mac_address=ifPhysAddress)

            interface.mac_addresses.add(mac_obj)
            if hasattr(interface, "primary_mac_address"):
                interface.primary_mac_address = mac_obj

    def update_interface_attributes(
        self,
        interface,
        librenms_interface,
        netbox_type,
        exclude_columns,
        interface_name_field,
    ):
        """Update interface fields from LibreNMS data, respecting excluded columns."""
        is_device_interface = isinstance(interface, Interface)

        LIBRENMS_TO_NETBOX_MAPPING = {
            interface_name_field: "name",
            "ifType": "type",
            "ifSpeed": "speed",
            "ifAlias": "description",
            "ifMtu": "mtu",
        }

        for librenms_key, netbox_key in LIBRENMS_TO_NETBOX_MAPPING.items():
            if netbox_key in exclude_columns:
                continue

            if librenms_key == "ifSpeed":
                speed = convert_speed_to_kbps(librenms_interface.get(librenms_key))
                setattr(interface, netbox_key, speed)
            elif librenms_key == "ifType":
                if is_device_interface and hasattr(interface, netbox_key):
                    setattr(interface, netbox_key, netbox_type)
            elif librenms_key == "ifAlias":
                interface_name = librenms_interface.get(interface_name_field)
                if librenms_interface.get("ifAlias") != interface_name:
                    setattr(interface, netbox_key, librenms_interface.get(librenms_key))
            else:
                setattr(interface, netbox_key, librenms_interface.get(librenms_key))

        port_id = librenms_interface.get("port_id")
        if port_id is not None:
            server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
            normalized_port_id = normalize_librenms_port_id(port_id)
            if normalized_port_id is not None:
                try:
                    existing_owner = find_by_librenms_id(interface.__class__, normalized_port_id, server_key)
                except AmbiguousLibreNMSIdError:
                    logger.warning(
                        "Not setting port_id %s — it is ambiguous (matches multiple interfaces).",
                        normalized_port_id,
                    )
                else:
                    if existing_owner is None or existing_owner.pk == interface.pk:
                        set_librenms_device_id(interface, normalized_port_id, server_key)
                    else:
                        logger.warning(
                            "Not reassigning port_id %s from %s to %s.",
                            normalized_port_id,
                            existing_owner,
                            interface,
                        )

        if "enabled" not in exclude_columns:
            admin_status = librenms_interface.get("ifAdminStatus")
            interface.enabled = (
                True
                if admin_status is None
                else (admin_status.lower() == "up" if isinstance(admin_status, str) else bool(admin_status))
            )

        if "mac_address" not in exclude_columns:
            ifPhysAddress = librenms_interface.get("ifPhysAddress")
            self.handle_mac_address(interface, ifPhysAddress)

        interface.save()

    def _sync_interface_vlans(self, interface, librenms_port, interface_name):
        """
        Sync VLAN assignments from LibreNMS to NetBox interface.
        Sets mode, untagged_vlan, and tagged_vlans based on LibreNMS data.

        Args:
            interface: NetBox Interface or VMInterface object
            librenms_port: Port data dict from LibreNMS with VLAN info
            interface_name: Original interface name for form field lookup
        """
        # Get per-VLAN group selections from form (safely handle special chars in name)
        safe_name = interface_name.replace("/", "_").replace(":", "_")

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
            group_id = self.request.POST.get(f"vlan_group_{safe_name}_{vid}", "")
            if group_id:
                vlan_group_map[vid] = group_id

        # Use mixin method to update interface VLAN assignments
        self._update_interface_vlan_assignment(interface, vlan_data, vlan_group_map, self._lookup_maps)


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
                    except Exception as exc:  # pragma: no cover - defensive
                        errors.append(f"Error deleting interface {interface_name or interface_id}: {str(exc)}")
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


def _interface_owner(iface):
    """Owner tuple ``(device_id, virtual_machine_id)`` for an Interface/VMInterface."""
    return (getattr(iface, "device_id", None), getattr(iface, "virtual_machine_id", None))


def _interface_owner_for_object(obj):
    """
    Build the owner tuple for the Device/VM that owns an interface.

    Matches the shape :func:`_interface_owner` produces (used as ``expected_owner``).

    Args:
        obj: The owning Device or VirtualMachine.

    Returns:
        tuple: ``(device_id, virtual_machine_id)`` — one element set, the other None.
    """
    if isinstance(obj, Device):
        return (obj.pk, None)
    return (None, obj.pk)


def _build_interface_index(obj, server_key):
    """
    Build a one-pass index of a device's interfaces for repeated resolution.

    VC-wide for a chassis member. A bulk relationship sync resolves many port_ids
    against the same interface set; without a shared index each
    :func:`_resolve_interface_by_port_id` call re-queries the DB and re-reads every
    interface's ``librenms_id`` custom field — O(selected × interfaces). Building this
    once collapses that to a single scan.

    Args:
        obj: The Device (or VirtualMachine) whose interfaces are indexed.
        server_key (str): The LibreNMS server key scoping stored-id reads.

    Returns:
        dict | None: ``{"by_lnms_id": {int: [iface, ...]}, "by_name": {name: [iface,
            ...]}}`` (lists so ambiguity stays detectable), or None for an unsupported
            object type.
    """
    if isinstance(obj, Device):
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
            iface_qs = Interface.objects.filter(device__in=member_ids)
        else:
            iface_qs = Interface.objects.filter(device=obj)
    elif isinstance(obj, VirtualMachine):
        iface_qs = VMInterface.objects.filter(virtual_machine=obj)
    else:
        return None

    by_lnms_id: dict = {}
    by_name: dict = {}
    for iface in iface_qs:
        stored_id = get_librenms_device_id(iface, server_key, auto_save=False)
        if stored_id is not None:
            by_lnms_id.setdefault(stored_id, []).append(iface)
        by_name.setdefault(iface.name, []).append(iface)
    return {"by_lnms_id": by_lnms_id, "by_name": by_name}


def _resolve_interface_by_port_id(
    obj, port_id: str, server_key: str, name_hint: str = "", expected_owner=None, index=None
):
    """
    Resolve a LibreNMS port_id to a NetBox Interface/VMInterface.

    Searches obj's interfaces for one whose librenms_id custom field matches port_id
    (all VC member interfaces for a Device in a Virtual Chassis), then falls back to an
    exact name match when *name_hint* is provided (e.g. the interface was created
    manually without a librenms_id).

    Args:
        obj: The Device (or VirtualMachine) whose interfaces are searched.
        port_id (str): The LibreNMS port_id to resolve.
        server_key (str): The LibreNMS server key scoping stored-id reads.
        name_hint (str): An exact interface name to fall back to.
        expected_owner: Optional ``(device_id, virtual_machine_id)`` tuple; a match
            whose owner differs is rejected. Because the VC search spans every member,
            a stale/reused librenms_id can otherwise resolve uniquely onto a different
            member than the row was synced to.
        index: Optional prebuilt lookup from :func:`_build_interface_index` so a bulk
            caller doesn't rebuild it per call; built once here when None.

    Returns:
        tuple: ``(interface, None)`` on success or ``(None, error_str)`` on failure
            (missing, ambiguous, or wrong-owner).
    """
    if not port_id:
        return None, "port_id is required"

    if index is None:
        index = _build_interface_index(obj, server_key)
        if index is None:
            return None, f"Unsupported object type: {type(obj).__name__}"

    # Use the shared normalizer (rejects bools / non-positive ids) so id resolution stays
    # consistent with every other port_id coercion in this module, rather than a hand-rolled
    # int(port_id) if isdigit() that would accept e.g. "0".
    target_id = normalize_librenms_port_id(port_id)
    # Collect every interface in scope whose stored LibreNMS id matches, then fail on
    # ambiguity rather than binding lag/parent to an arbitrary first match. Mirrors the
    # ambiguity-safe behaviour of _resolve_device_interface()/_resolve_vm_interface():
    # two interfaces carrying the same stale librenms_id must surface, not silently pick one.
    matches = list(index["by_lnms_id"].get(target_id, [])) if target_id is not None else []
    ambiguous_msg = f"LibreNMS port_id {port_id} is ambiguous on {obj} (matches multiple interfaces)"
    if expected_owner is not None:
        # Prefer an id-match on the owner this row was synced onto. A stale/reused librenms_id
        # can resolve uniquely onto a *different* VC member; don't let that foreign match block
        # the name_hint fallback to the (often manually-created, id-less) interface on the
        # expected owner — fall through to the name lookup below before giving up.
        owned = [m for m in matches if _interface_owner(m) == expected_owner]
        if len(owned) == 1:
            return owned[0], None
        if len(owned) > 1:
            return None, ambiguous_msg
    else:
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, ambiguous_msg

    if name_hint:
        iface, err = _resolve_interface_by_name_hint(obj, name_hint, index=index, expected_owner=expected_owner)
        if err:
            return None, err
        if iface is not None:
            if expected_owner is not None and _interface_owner(iface) != expected_owner:
                return None, f"Interface name '{name_hint}' resolves to a different owner than the selected row"
            return iface, None

    # Nothing resolved. If an id-match existed but only on a different owner, report that
    # (the row points at a port owned elsewhere); otherwise it's a plain not-found.
    if expected_owner is not None and matches:
        return None, f"LibreNMS port_id {port_id} resolves to a different owner than the selected row"
    return None, f"Interface with LibreNMS port_id {port_id} not found on {obj}"


def _resolve_interface_by_name_hint(obj, name_hint, index=None, expected_owner=None):
    """
    Exact-name fallback for :func:`_resolve_interface_by_port_id`.

    Args:
        obj: The Device (or VirtualMachine) whose interfaces are searched.
        name_hint (str): The exact interface name to match.
        index: Optional prebuilt lookup from :func:`_build_interface_index`; used when
            supplied so a bulk caller avoids a per-name DB query.
        expected_owner: Optional ``(device_id, virtual_machine_id)`` tuple. On a VC, members
            commonly share interface names, so the whole chassis matching a name would read as
            ambiguous; narrow to the selected owner FIRST — mirroring the port-id resolution — so
            an id-less interface still resolves on its own member (genuine same-owner duplicates
            still error).

    Returns:
        tuple: ``(iface, None)`` on a unique match, ``(None, None)`` when nothing
            matches, or ``(None, error_str)`` on an ambiguous name.
    """
    if index is not None:
        matches = index["by_name"].get(name_hint, [])
        if expected_owner is not None:
            owned = [m for m in matches if _interface_owner(m) == expected_owner]
            if owned:
                matches = owned
        if not matches:
            return None, None
        if len(matches) > 1:
            return None, f"Interface name '{name_hint}' is ambiguous on {obj}"
        return matches[0], None
    try:
        if isinstance(obj, Device):
            if expected_owner is not None and expected_owner[0] is not None:
                # Owner-pinned to the selected VC member so identical names on other members
                # don't read as ambiguous (matches the index branch above).
                iface = Interface.objects.get(device_id=expected_owner[0], name=name_hint)
            elif hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                member_ids = obj.virtual_chassis.members.values_list("id", flat=True)
                iface = Interface.objects.get(device__in=member_ids, name=name_hint)
            else:
                iface = Interface.objects.get(device=obj, name=name_hint)
        else:
            iface = VMInterface.objects.get(virtual_machine=obj, name=name_hint)
        return iface, None
    except (Interface.DoesNotExist, VMInterface.DoesNotExist):
        return None, None
    except (Interface.MultipleObjectsReturned, VMInterface.MultipleObjectsReturned):
        return None, f"Interface name '{name_hint}' is ambiguous on {obj}"


def _interfaces_same_owner(a, b) -> bool:
    """
    Return True when both interfaces belong to the same Device (or same VM).

    `_resolve_interface_by_port_id` searches all members of a Virtual Chassis, so a
    stale or ambiguous port_stack relationship can resolve a LAG/parent pair onto two
    different member devices. NetBox forbids a cross-device lag/parent, so callers must
    reject such a pair instead of persisting an invalid link.

    Args:
        a: The first interface.
        b: The second interface.

    Returns:
        bool: True when *a* and *b* share the same owning Device or VM.
    """
    return (getattr(a, "device_id", None), getattr(a, "virtual_machine_id", None)) == (
        getattr(b, "device_id", None),
        getattr(b, "virtual_machine_id", None),
    )


def _promote_lag_aggregate(agg, *, with_restore):
    """
    Bump a LAG aggregate to ``type=lag`` in memory so a member's ``full_clean()`` accepts the link.

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
        ValidationError: when the source fails ``full_clean()`` (after restoring the related
            mutation); the caller decides how to surface it (bulk logs+skips, single-row 409).
    """
    # Capture the source's original FK before mutating: source_iface (and the aggregate) are
    # reused across rows via the shared interface index, so a failed attempt must leave BOTH
    # unmutated. Otherwise a later edge validates source_iface against the rolled-back (but
    # still in-memory) FK, or — because the aggregate already looks type=lag in memory — a later
    # member sharing it skips the type bump and never persists it, leaving the DB type stale.
    original_related = getattr(source_iface, relation_field)
    setattr(source_iface, relation_field, related_iface)
    prepared = prepare_related(related_iface) if prepare_related else None
    if isinstance(prepared, tuple):
        persist_related, restore_related = prepared
    else:
        persist_related, restore_related = prepared, None

    def _restore_in_memory():
        setattr(source_iface, relation_field, original_related)
        if restore_related:
            restore_related()

    try:
        source_iface.full_clean()
        if persist_related:
            persist_related()
        source_iface.save(update_fields=[relation_field])
    except (ValidationError, IntegrityError):
        # full_clean() rejection OR a statement-time persist failure (the savepoint rolls back
        # the DB, but the in-memory instances stay mutated): undo both before the caller skips
        # this row and continues the batch against the shared index.
        _restore_in_memory()
        raise


class _BaseRelationshipSyncView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """
    Shared skeleton for the inline single-row relationship-sync endpoints (LAG / parent).

    SyncInterfaceLagView and SyncInterfaceParentView were ~90% identical — permission gate,
    resolve both ends by stable port_id (owner-pinned), same-owner guard, then a
    validate-then-persist under one transaction — differing only in the FK attribute set,
    the POST field/label wording, VM support, and the LAG-only aggregate type bump. Keeping
    one flow here stops the two endpoints drifting (a fix to the resolve/validate/persist
    sequence applies once, not twice).

    Subclass contract (class attributes):
        relation_field   -- the Interface FK attribute set ("lag" | "parent").
        related_port_param / related_name_param -- the POST fields carrying the related
            port_id and its display-name hint.
        relation_label   -- human label in messages ("LAG" | "parent").
        source_label / related_label -- the two interfaces' roles ("Member"/"Aggregate",
            "Child"/"Parent"), used in the resolution error prefixes.
        supports_vm      -- whether VMInterface is a valid target (parent: yes; lag: no,
            VMInterface has no `lag` field).
    """

    relation_field: str
    related_port_param: str
    related_name_param: str
    relation_label: str
    source_label: str
    related_label: str
    supports_vm: bool = False

    def _required_permissions(self, object_type):
        """Object-type-scoped POST permissions; raise Http404 for an unsupported type."""
        # The owner is resolved through a restricted queryset (_get_object), so its view
        # permission belongs here: a missing grant is an explicit 403, not a 404 at the lookup.
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
        """Resolve the owner the row belongs to, scoped to what the caller may see."""
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

    def post(self, request, object_type, object_id):
        # Set the object-type-scoped permissions BEFORE the gate (an unsupported type raises
        # Http404 here). JSON endpoint: require_all_permissions would return the mixin's
        # HTML/redirect on denial, breaking the fetch() caller, so use the _json variant.
        self.required_object_permissions = self._required_permissions(object_type)
        if error := self.require_all_permissions_json("POST"):
            return error

        obj = self._get_object(object_type, object_id)
        server_key = request.POST.get("server_key") or self.librenms_api.server_key
        port_id = request.POST.get("port_id", "").strip()
        related_port_id = request.POST.get(self.related_port_param, "").strip()
        related_name = request.POST.get(self.related_name_param, "").strip()

        if not port_id or not related_port_id:
            return JsonResponse({"error": f"port_id and {self.related_port_param} are required"}, status=400)

        # obj is the VC member the JS posted (vcMemberSelect), so both ends must belong to it —
        # pin the owner so a stale librenms_id can't resolve onto another member.
        expected_owner = _interface_owner_for_object(obj)
        # Build the interface index ONCE and share it across both resolutions below — otherwise
        # each _resolve_interface_by_port_id call rebuilds it internally (a VC-wide interface scan
        # re-reading every librenms_id custom field), doubling the DB work per click for no benefit.
        iface_index = _build_interface_index(obj, server_key)
        source_iface, err = _resolve_interface_by_port_id(
            obj, port_id, server_key, expected_owner=expected_owner, index=iface_index
        )
        if err:
            return JsonResponse({"error": f"{self.source_label} interface: {err}"}, status=404)

        related_iface, err = _resolve_interface_by_port_id(
            obj, related_port_id, server_key, name_hint=related_name, expected_owner=expected_owner, index=iface_index
        )
        if err:
            return JsonResponse({"error": f"{self.related_label} interface: {err}"}, status=404)

        if not _interfaces_same_owner(source_iface, related_iface):
            return JsonResponse(
                {"error": f"{self.source_label} and {self.related_label.lower()} interfaces are on different devices."},
                status=409,
            )

        # The IntegrityError wrapper sits OUTSIDE the atomic: a concurrent conflict (e.g. the
        # related interface deleted in the validate/write TOCTOU window) raises either at the
        # failed statement — propagating out of the atomic after rollback — or, for Django's
        # INITIALLY DEFERRED Postgres FKs, only at the atomic's COMMIT. Both land here and
        # become a JSON 409 instead of an unhandled 500 to the fetch() caller, mirroring the
        # bulk pass (_apply_relationship_edge).
        try:
            with transaction.atomic():
                # Validate before persisting: a crafted POST with port_id == related_port_id
                # resolves source == related and passes the same-owner check, so full_clean() is
                # what rejects the resulting self-relationship. The shared helper sets the FK, runs
                # _prepare_related (e.g. the aggregate's type=lag, persisted only on success), and
                # saves with update_fields.
                try:
                    _apply_interface_relationship(
                        source_iface, self.relation_field, related_iface, self._prepare_related
                    )
                except ValidationError as exc:
                    # Log the validation detail server-side and return a fixed message — don't echo
                    # exception text to the client (CodeQL py/stack-trace-exposure). The
                    # cross-device case is already rejected above, so this is a self-relationship
                    # or another NetBox model constraint.
                    logger.warning(
                        "%s link validation failed (%s -> %s): %s",
                        self.relation_label,
                        source_iface.name,
                        related_iface.name,
                        _validation_error_detail(exc),
                    )
                    return JsonResponse(
                        {
                            "error": (
                                f"Cannot link {source_iface.name} to {self.relation_label} {related_iface.name}: "
                                f"NetBox rejected the {self.relation_label} relationship. Check the interface "
                                "types and that the two interfaces are not the same interface."
                            )
                        },
                        status=409,
                    )
                logger.info("Set %s.%s = %s", source_iface.name, self.relation_field, related_iface.name)
        except IntegrityError as exc:
            logger.warning(
                "%s link hit a concurrent DB conflict (%s -> %s): %s",
                self.relation_label,
                source_iface.name,
                related_iface.name,
                exc,
            )
            return JsonResponse(
                {
                    "error": (
                        f"Cannot link {source_iface.name} to {self.relation_label} {related_iface.name}: "
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
    related_name_param = "lag_name"
    relation_label = "LAG"
    source_label = "Member"
    related_label = "Aggregate"
    supports_vm = False  # VMInterface has no `lag` field

    def _prepare_related(self, related_iface):
        """Promote the aggregate to type=lag so member_iface.full_clean() accepts the link."""
        # Single-row endpoint: no aggregate reuse across rows, so no restore needed.
        return _promote_lag_aggregate(related_iface, with_restore=False)


class SyncInterfaceParentView(_BaseRelationshipSyncView):
    """Set Interface.parent (sub-interface -> parent) based on LibreNMS port_stack data."""

    # Both Devices (Interface) and VMs (VMInterface, which also has a parent field) are
    # supported; permissions are resolved per object_type in the shared post().
    relation_field = "parent"
    related_port_param = "parent_port_id"
    related_name_param = "parent_name"
    relation_label = "parent"
    source_label = "Child"
    related_label = "Parent"
    supports_vm = True
