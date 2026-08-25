import functools
import hashlib
import json
import logging
from urllib.parse import quote_plus

from dcim.models import Cable, CableTermination, ConsolePort, ConsoleServerPort, Device, Interface
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View

from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    schedule_request_cache_mutation,
)
from netbox_librenms_plugin.utils import (
    apply_cable_manual_picks,
    cable_path_reaches,
    classify_cable_action,
    coerce_librenms_id,
    get_cable_sync_settings,
    get_librenms_cable_tag,
    get_librenms_sync_device,
    get_migrated_to_marker,
    is_list_of_dicts,
    render_cable_trace,
    resolve_interface_on_device,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    redirect_with_server_key,
)

logger = logging.getLogger(__name__)


@functools.cache
def _termination_topology_columns():
    """
    Return the ``(order_by, values)`` column tuples the topology fingerprint may name.

    NetBox added ``connector`` and ``positions`` to CableTermination after 4.4. Naming either one
    there raises ``FieldError: Cannot resolve keyword ...`` and takes down every cable sync, and
    Django reports only the first unresolvable keyword, so both are resolved from the model rather
    than assumed. Follow the model, not the version.

    Returns:
        tuple[tuple[str, ...], tuple[str, ...]]: Columns for ``order_by()`` and for
            ``values_list()``, in the order the fingerprint hashes them.
    """
    present = {field.name for field in CableTermination._meta.get_fields()}
    connector = ("connector",) if "connector" in present else ()
    optional = tuple(name for name in ("connector", "positions") if name in present)
    return (
        ("cable_id", "cable_end", *connector, "pk"),
        ("cable_id", "cable_end", "termination_type_id", "termination_id", *optional),
    )


class SyncCablesView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Create NetBox cables using cached LibreNMS link data."""

    required_object_permissions = {
        "POST": [
            # The device whose cable tab is being synced is resolved through a restricted
            # queryset, so state that read here: a missing grant is then an explicit 403
            # rather than a puzzling 404 at the lookup.
            ("view", Device),
            # Creating a cable changes the cable state of both terminations. Resolve the
            # client-supplied ids through the same change scope NetBox's cable form uses.
            ("add", Cable),
            ("change", Cable),
        ],
    }

    def _get_cable_sync_settings(self, *, lock=False):
        """Return the cable settings loaded once for this sync request."""
        if lock:
            self._cable_sync_settings = get_cable_sync_settings(lock=True)
        elif not hasattr(self, "_cable_sync_settings"):
            self._cable_sync_settings = get_cable_sync_settings()
        return self._cable_sync_settings

    def _get_provenance_tag(self, *, create=False, sync_settings=None):
        """
        Return the configured provenance tag loaded once for this sync request.

        The tag is plugin infrastructure: its name and color come from the plugin settings row,
        and the plugin bootstraps it on first use the same way it bootstraps the ``librenms_id``
        custom field. Cable sync therefore does not ask the syncing user for ``extras.add_tag``.
        """
        sync_settings = sync_settings or self._get_cable_sync_settings()
        if not getattr(self, "_cable_provenance_tag_resolved", False):
            self._cable_provenance_tag = get_librenms_cable_tag(create=False, sync_settings=sync_settings)
            self._cable_provenance_tag_resolved = True
        if create and self._cable_provenance_tag is None:
            self._cable_provenance_tag = get_librenms_cable_tag(
                sync_settings=sync_settings,
            )
        return self._cable_provenance_tag

    def get_selected_interfaces(self, request, initial_device):
        """
        Return selected interface entries from POST data.

        Each ``select`` value is the snapshot row identity. The LibreNMS port ID remains
        a property of the matched cached row and can identify more than one neighbor.

        Args:
            request (HttpRequest): The request that contains the selected interface data.
            initial_device (Device): The page device to use when no device override is selected.

        Returns:
            list[dict] | None: The selected interface entries, or None when no interfaces are selected.
        """
        selected_interfaces = []
        sync_one = request.POST.get("sync_one")
        selected_data = [sync_one] if sync_one else [x for x in request.POST.getlist("select") if x]

        if not selected_data:
            return None

        for row_id in selected_data:
            override = request.POST.get(f"device_selection_{row_id}")
            device_id = override or initial_device.id
            selected_interfaces.append(
                {
                    "device_id": device_id,
                    "row_id": row_id,
                    "expected_local_id": coerce_librenms_id(request.POST.get(f"expected_local_id_{row_id}")),
                    "expected_local_device_id": coerce_librenms_id(
                        request.POST.get(f"expected_local_device_id_{row_id}")
                    ),
                    "expected_remote_id": coerce_librenms_id(request.POST.get(f"expected_remote_id_{row_id}")),
                    "expected_remote_device_id": coerce_librenms_id(
                        request.POST.get(f"expected_remote_device_id_{row_id}")
                    ),
                    "expected_cable_intent": request.POST.get(f"expected_cable_intent_{row_id}"),
                }
            )

        return selected_interfaces

    def get_cached_links_data(self, request, obj):
        """
        Return cached LibreNMS link data for the given object.

        Args:
            request: The current sync request.
            obj: The page device.

        Returns:
            list[dict] | None: Enriched link rows, or None when the cache is unavailable.
        """
        self._cable_row_identity_error = False
        self._cable_source_permission_error = False
        self._incomplete_sources = set()
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        if not self.restricted_queryset(Device, "view").filter(pk=cache_obj.pk).exists():
            self._cable_source_permission_error = True
            return None
        self._cache_device = cache_obj
        cache_key = self.get_cache_key(cache_obj, "links", server_key)
        cached_data = cache.get(cache_key)
        if not isinstance(cached_data, dict) or not is_list_of_dicts(cached_data.get("links")):
            return None
        links, _applied = apply_cable_manual_picks(
            cache,
            cache_key,
            cached_data,
            request.user.pk,
            cached_data["links"],
        )
        if cached_data["links"] and not links:
            self._cable_row_identity_error = True
        incomplete = cached_data.get("incomplete_sources")
        if isinstance(incomplete, list):
            self._incomplete_sources = {source.lower() for source in incomplete if isinstance(source, str)}
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = DeviceCableTableView()
        view.request = request
        return view.enrich_links_data(
            links,
            obj,
            server_key=server_key,
            sync_device=cache_obj,
        )

    def create_cable(self, local_interface, remote_interface, request, *, sync_settings=None):
        """
        Create an enriched cable between the local and remote terminations.

        Beyond the bare connection, the cable is stamped with provenance so the plugin can later
        recognise its own cables (and protect a future DCIM-driven remodel from being overwritten):
        the ``librenms`` tag, a configured color, and a description carrying the acting server key.
        The tenant follows the REMOTE side (the target device) — when the two devices are in
        different tenants the remote's wins, never the terminal-server side. Type is left blank
        because LibreNMS doesn't tell us the physical cable and NetBox has no serial/rollover type.

        Returns:
            True on success, False on failure.
        """
        try:
            # Cable + provenance stamp succeed or fail together: a tags.add failure after
            # cable.save() would otherwise commit an untagged cable while the UI reports
            # failure — and an untagged cable isn't recognized as plugin-owned, so the
            # user's retry hits a force-confirm conflict against their own cable. The
            # savepoint also protects the overwrite path's per-interface atomic block.
            with transaction.atomic():
                server_key = getattr(self, "_post_server_key", None)
                sync_settings = sync_settings or self._get_cable_sync_settings(lock=True)
                base_desc = sync_settings.cable_sync_description
                description_max_length = Cable._meta.get_field("description").max_length
                if server_key:
                    bounded_key = str(server_key)[: description_max_length - len(" ()")]
                    suffix = f" ({bounded_key})"
                else:
                    suffix = ""
                description = f"{base_desc[: description_max_length - len(suffix)]}{suffix}"
                cable = Cable(
                    a_terminations=[local_interface],
                    b_terminations=[remote_interface],
                    status="connected",
                    color=sync_settings.cable_sync_tag_color,
                    description=description,
                    tenant=getattr(getattr(remote_interface, "device", None), "tenant", None),
                )
                cable.save()
                cable.tags.add(self._get_provenance_tag(create=True, sync_settings=sync_settings))
            return True
        except Exception as exc:  # pragma: no cover - protects UX
            messages.error(request, f"Failed to create cable: {str(exc)}")
            return False

    def _apply_cable_action(self, local_term, remote_term, link_data, display_name, force):
        """
        Classify the sync for one resolved termination pair and act on (or defer) it.

        Delegates the decision to :func:`classify_cable_action` and then performs the chosen
        action: create a fresh enriched cable, add the librenms tag to an already-matching cable,
        or require confirmation of the exact current cable IDs before any replacement.

        Args:
            local_term: The near-side termination (ConsoleServerPort / Interface).
            remote_term: The far-side termination (ConsolePort / Interface).
            link_data (dict): The cached link row (used for the conflict re-submit ``port_id``).
            display_name (str): Human label for the local port, echoed in the result.
            force (bool): When True, a ``needs_force`` conflict is overwritten instead of deferred.

        Returns:
            dict: A result with ``status`` in ``{valid, overwritten, tagged, duplicate, conflict,
                denied, invalid}`` plus ``interface``; conflicts also carry ``port_id`` and
                ``trace`` (and are stamped with the resolved ``device_id`` by the caller).
        """
        with transaction.atomic():
            locked_terms = self._lock_cable_terminations(
                local_term,
                remote_term,
                expected_local_owner_id=coerce_librenms_id(
                    link_data.get("netbox_local_device_id") or link_data.get("device_id")
                ),
                expected_remote_owner_id=coerce_librenms_id(link_data.get("netbox_remote_device_id")),
            )
            if locked_terms is None:
                return {
                    "status": getattr(self, "_termination_lock_failure", "denied"),
                    "interface": display_name,
                }
            locked_initial_device = getattr(self, "_locked_initial_device", None)
            locked_origin_device = getattr(self, "_locked_origin_device", None)
            locked_local_owner = getattr(self, "_locked_local_owner", None)
            locked_cache_device = getattr(self, "_locked_cache_device", None)
            server_key = getattr(self, "_post_server_key", None)
            if any(
                get_migrated_to_marker(device, server_key)
                for device in (
                    locked_initial_device,
                    locked_origin_device,
                    locked_local_owner,
                    locked_cache_device,
                )
                if device is not None
            ):
                return {"status": "stale", "interface": display_name}
            local_term, remote_term = locked_terms
            locked_cables = self._lock_current_cables(local_term, remote_term)
            visible_cable_ids = set(
                self.restricted_queryset(Cable, "view").filter(pk__in=locked_cables).values_list("pk", flat=True)
            )
            if visible_cable_ids != set(locked_cables):
                return {"status": "denied", "interface": display_name}
            if (
                local_term.cable_id is not None
                and remote_term.cable_id is not None
                and local_term.cable_id != remote_term.cable_id
                and cable_path_reaches(local_term, remote_termination=remote_term)
            ):
                return {"status": "patch_path", "interface": display_name}
            current_cable_state = self._cable_state_token(locked_cables, lock=True)
            current_cable_intent = self._cable_intent_token(
                current_cable_state,
                local_term,
                remote_term,
            )
            sync_settings = self._get_cable_sync_settings(lock=True)
            provenance_tag = self._get_provenance_tag(sync_settings=sync_settings) if locked_cables else None
            decision = classify_cable_action(local_term, remote_term, provenance_tag)
            for key in ("cable",):
                if decision.get(key) is not None:
                    decision[key] = locked_cables.get(decision[key].pk, decision[key])
            decision["to_remove"] = [locked_cables.get(cable.pk, cable) for cable in decision["to_remove"]]
            return self._apply_locked_cable_action(
                local_term,
                remote_term,
                link_data,
                display_name,
                force,
                decision,
                current_cable_intent,
                sync_settings,
            )

    def _lock_cable_terminations(
        self,
        local_term,
        remote_term,
        *,
        expected_local_owner_id=None,
        expected_remote_owner_id=None,
    ):
        """Lock owners before terminations, then re-check scope and endpoint identity."""
        self._termination_lock_failure = "denied"
        grouped = {}
        for termination in (local_term, remote_term):
            grouped.setdefault(type(termination), set()).add(termination.pk)

        # Check the submitted candidates before taking locks so a forged request cannot lock
        # rows that are outside its current object scope. The same check runs again after the
        # rows are locked, where it protects mutable constraint-based grants. Cabling a
        # termination is a write, so change scope is what governs it: the POST gate does not ask
        # for view on the termination models either, and demanding it here would lock out a
        # change-only grant.
        for model, requested_ids in grouped.items():
            permitted_ids = set(
                self.restricted_queryset(model, "change").filter(pk__in=requested_ids).values_list("pk", flat=True)
            )
            if permitted_ids != requested_ids:
                return None

        # Every cable writer locks Device owners before Interface/CSP/CP rows. Parent-child
        # relationship sync uses the same order, so the two features cannot form a Device ↔
        # Interface deadlock cycle. The termination owner IDs are candidate evidence only; the
        # locked rows below must still have these exact owners.
        expected_owner_by_key = {
            (type(termination), termination.pk): termination.device_id for termination in (local_term, remote_term)
        }
        owner_ids = set(expected_owner_by_key.values())
        initial_device = getattr(self, "_initial_device", None)
        if initial_device is not None:
            owner_ids.add(initial_device.pk)
        origin_device = getattr(self, "_origin_device", None)
        if origin_device is not None:
            owner_ids.add(origin_device.pk)
        cache_device = getattr(self, "_cache_device", None)
        if cache_device is not None:
            owner_ids.add(cache_device.pk)
        owner_ids = sorted(owner_ids)
        visible_owner_ids = set(
            self.restricted_queryset(Device, "view").filter(pk__in=owner_ids).values_list("pk", flat=True)
        )
        if visible_owner_ids != set(owner_ids):
            return None
        locked_owners = list(Device.objects.select_for_update().filter(pk__in=owner_ids).order_by("pk"))
        locked_owners_by_id = {owner.pk: owner for owner in locked_owners}
        if set(locked_owners_by_id) != set(owner_ids):
            return None

        locked_by_key = {}
        for model in sorted(grouped, key=lambda item: item._meta.label_lower):
            requested_ids = grouped[model]
            locked = list(model.objects.select_for_update().filter(pk__in=requested_ids).order_by("pk"))
            locked_by_key.update({(model, termination.pk): termination for termination in locked})
        if set(locked_by_key) != set(expected_owner_by_key):
            self._termination_lock_failure = "stale"
            return None
        if any(termination.device_id != expected_owner_by_key[key] for key, termination in locked_by_key.items()):
            self._termination_lock_failure = "stale"
            return None

        self._locked_initial_device = locked_owners_by_id.get(getattr(initial_device, "pk", None))
        self._locked_origin_device = locked_owners_by_id.get(getattr(origin_device, "pk", None))
        self._locked_cache_device = locked_owners_by_id.get(getattr(cache_device, "pk", None))
        locked_local = locked_by_key[(type(local_term), local_term.pk)]
        locked_remote = locked_by_key[(type(remote_term), remote_term.pk)]
        locked_local_owner = locked_owners_by_id[locked_local.device_id]
        locked_remote_owner = locked_owners_by_id[locked_remote.device_id]
        self._locked_local_owner = locked_local_owner
        self._locked_remote_owner = locked_remote_owner
        if expected_local_owner_id is not None and locked_local_owner.pk != expected_local_owner_id:
            self._termination_lock_failure = "stale"
            return None
        if expected_remote_owner_id is not None and locked_remote_owner.pk != expected_remote_owner_id:
            self._termination_lock_failure = "stale"
            return None
        if (
            self._locked_initial_device is not None
            and self._locked_origin_device is not None
            and self._locked_origin_device.pk != self._locked_initial_device.pk
            and (
                self._locked_origin_device.virtual_chassis_id is None
                or self._locked_origin_device.virtual_chassis_id != self._locked_initial_device.virtual_chassis_id
            )
        ):
            self._termination_lock_failure = "stale"
            return None
        if (
            self._locked_initial_device is not None
            and self._locked_cache_device is not None
            and self._locked_cache_device.pk != self._locked_initial_device.pk
            and (
                self._locked_cache_device.virtual_chassis_id is None
                or self._locked_cache_device.virtual_chassis_id != self._locked_initial_device.virtual_chassis_id
            )
        ):
            self._termination_lock_failure = "stale"
            return None
        if (
            self._locked_initial_device is not None
            and locked_local_owner.pk != self._locked_initial_device.pk
            and (
                self._locked_initial_device.virtual_chassis_id is None
                or locked_local_owner.virtual_chassis_id != self._locked_initial_device.virtual_chassis_id
            )
        ):
            self._termination_lock_failure = "stale"
            return None
        visible_owner_ids = set(
            self.restricted_queryset(Device, "view").filter(pk__in=owner_ids).values_list("pk", flat=True)
        )
        if visible_owner_ids != set(owner_ids):
            return None

        for model, requested_ids in grouped.items():
            permitted_after_lock = set(
                self.restricted_queryset(model, "change").filter(pk__in=requested_ids).values_list("pk", flat=True)
            )
            if permitted_after_lock != requested_ids:
                return None

        return (
            locked_local,
            locked_remote,
        )

    @staticmethod
    def _lock_current_cables(local_term, remote_term):
        """Lock the cables currently attached to either locked termination."""
        cable_ids = sorted(
            {
                cable_id
                for cable_id in (
                    getattr(local_term, "cable_id", None),
                    getattr(remote_term, "cable_id", None),
                )
                if cable_id is not None
            }
        )
        return {cable.pk: cable for cable in Cable.objects.select_for_update().filter(pk__in=cable_ids).order_by("pk")}

    @staticmethod
    def _cable_state_token(cables, *, lock=False):
        """Fingerprint the exact Cable rows and termination topology."""
        cable_ids = sorted(cables)
        order_by, values = _termination_topology_columns()
        terminations = CableTermination.objects.filter(cable_id__in=cable_ids).order_by(*order_by)
        if lock:
            terminations = terminations.select_for_update()
        topology = list(terminations.values_list(*values))
        payload = json.dumps([cable_ids, topology], separators=(",", ":"), sort_keys=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _cable_intent_token(cable_state, local_term, remote_term):
        """Fingerprint the confirmed topology and the exact desired endpoints."""
        endpoints = [
            [local_term._meta.label_lower, local_term.pk],
            [remote_term._meta.label_lower, remote_term.pk],
        ]
        payload = json.dumps([cable_state, endpoints], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _apply_locked_cable_action(
        self,
        local_term,
        remote_term,
        link_data,
        display_name,
        force,
        decision,
        current_cable_intent,
        sync_settings,
    ):
        """Apply a cable decision made from locked current rows."""
        action = decision["action"]
        if force:
            confirmed_intent = link_data.get("expected_cable_intent")
            if not isinstance(confirmed_intent, str) or confirmed_intent != current_cable_intent:
                return {"status": "stale", "interface": display_name}

        if action == "noop":
            # The desired cable already exists and is already tagged — nothing to do.
            return {"status": "duplicate", "interface": display_name}
        if action == "tag_only":
            # Same connection, just missing our tag: add it (non-destructive, no force needed).
            changeable = self.restricted_queryset(Cable, "change").filter(pk=decision["cable"].pk).exists()
            if not changeable:
                return {"status": "denied", "interface": display_name}
            decision["cable"].tags.add(self._get_provenance_tag(create=True, sync_settings=sync_settings))
            return {"status": "tagged", "interface": display_name}
        if action == "create":
            if self.create_cable(local_term, remote_term, self.request, sync_settings=sync_settings):
                return {"status": "valid", "interface": display_name}
            # create_cable already flashed the concrete error; "invalid" would additionally claim
            # the row had no LibreNMS link data.
            return {"status": "failed", "interface": display_name}
        if action == "unsupported":
            return {"status": "unsupported", "interface": display_name}
        if action == "needs_force" and force:
            # Overwriting DELETES the occupying cable(s), but the view's blanket POST gate only
            # covers add/change Cable. Require the delete perm precisely on the destructive
            # branch, so create-only syncs keep working for add/change users.
            user = getattr(self.request, "user", None)
            if user is None or not user.has_perm("dcim.delete_cable"):
                return {"status": "denied", "interface": display_name}
            to_remove = decision["to_remove"]
            # has_perm is asked without an instance, so a CONSTRAINED delete_cable grant clears the
            # check above. Confirm every doomed cable is inside the user's own delete scope before
            # destroying it — an out-of-scope one denies the whole overwrite rather than part of it.
            deletable = set(
                self.restricted_queryset(Cable, "delete")
                .filter(pk__in=[cable.pk for cable in to_remove])
                .values_list("pk", flat=True)
            )
            if any(cable.pk not in deletable for cable in to_remove):
                return {"status": "denied", "interface": display_name}
            # Remove the occupying cable(s) first (NetBox forbids a second cable on a live endpoint).
            for cable in to_remove:
                cable.delete()
            if self.create_cable(local_term, remote_term, self.request, sync_settings=sync_settings):
                return {"status": "overwritten", "interface": display_name}
            # Create failed AFTER deleting the old cable(s): raise so the per-interface atomic block
            # (process_interface_sync) rolls the deletes back rather than orphaning the connection.
            raise RuntimeError(f"cable overwrite failed for {display_name}")  # pragma: no cover

        # needs_force and not force: _apply_cable_action already denied any row whose current
        # cables are outside view scope, so the modal can carry each doomed cable's end-to-end
        # trace and show what a forced overwrite would destroy, without hidden path objects.
        return {
            "status": "conflict",
            "interface": display_name,
            "row_id": link_data["row_id"],
            "trace": [
                render_cable_trace(cable, user=getattr(self.request, "user", None)) for cable in decision["to_remove"]
            ],
            # Only the endpoint-attached segment(s) are ever deleted — patch-panel trunks and
            # other mid-path segments carry other circuits and always stay. The modal uses these
            # labels to mark precisely which trace hops die.
            "removed_cables": [f"#{cable.pk}" for cable in decision["to_remove"]],
            "expected_local_id": coerce_librenms_id(link_data.get("netbox_local_interface_id")),
            "expected_local_device_id": coerce_librenms_id(link_data.get("netbox_local_device_id")),
            "expected_remote_id": coerce_librenms_id(link_data.get("netbox_remote_interface_id")),
            "expected_remote_device_id": coerce_librenms_id(link_data.get("netbox_remote_device_id")),
            "expected_cable_intent": current_cable_intent,
        }

    def validate_prerequisites(self, cached_links, selected_interfaces):
        """
        Validate that cached data and selections are present before sync.

        Args:
            cached_links: The cached and enriched cable rows.
            selected_interfaces: The rows selected by the user.

        Returns:
            bool: True when cable sync can continue.
        """
        if getattr(self, "_cable_source_permission_error", False):
            messages.error(
                self.request,
                "You do not have permission to view the LibreNMS cable source device.",
            )
            return False

        if getattr(self, "_cable_row_identity_error", False):
            messages.error(
                self.request,
                "Cable sync cannot continue because multiple cached cable rows have the same identity. "
                "Resolve the duplicate LibreNMS link data before syncing.",
            )
            return False

        if not cached_links:
            messages.error(
                self.request,
                "Cache has expired. Please refresh the cable data before syncing.",
            )
            return False

        if selected_interfaces is None:
            messages.error(self.request, "No interfaces selected for synchronization.")
            return False

        return True

    def _selected_row_link_data(self, interface, cached_links):
        """
        Resolve one selection to its cached row with the posted VC-member override applied.

        The pre-flight uniqueness gate and the sync itself must judge the same termination:
        re-resolving the local end in only one of them lets two rows overridden onto the same
        VC member pass the gate and then collapse onto one interface.

        Args:
            interface (dict): One entry from :meth:`get_selected_interfaces`.
            cached_links (list): The enriched cached rows for this page.

        Returns:
            tuple: ``(link_data, result)`` — exactly one is not None. *result* carries the
                status of a row that cannot be synced at all.
        """
        row_id = interface.get("row_id", "")
        matching_rows = [link for link in cached_links if link.get("row_id") == row_id]
        if len(matching_rows) != 1:
            return None, {"status": "invalid", "interface": row_id}
        link_data = matching_rows[0]
        display_name = link_data.get("local_port") or row_id
        # OOB-controller rows are merged into the host's cable list only for context
        # (shared-LOM detection) and must never be synced onto the host: their local_port can
        # resolve to a host interface (shared name), and creating a cable from OOB-controller
        # LLDP data would attach it to the wrong device. Mirrors the OOB guards in interface
        # sync (interfaces.py) and module sync (modules.py).
        if link_data.get("_source") == "oob":
            return None, {"status": "skipped", "interface": display_name}
        # A source named in incomplete_sources contributed no fresh rows to this snapshot, so a
        # row still carrying it was carried over from an earlier refresh (see the permission-skip
        # carry-over in cables_view). LibreNMS may have moved that sensor since, so the row is
        # unverified: show it, but never create or replace a cable from it.
        if (link_data.get("_source") or "host").lower() in getattr(self, "_incomplete_sources", set()):
            return None, {"status": "unverified", "interface": display_name}
        # A multi-termination (breakout) cable on either end stops the enrichment before it
        # resolves the remote IDs, so the expected-endpoint check would report the row as
        # "stale". Name the real reason instead; classify_cable_action still catches a cable
        # that became multi-termination after the snapshot.
        if link_data.get("_multi_termination_unsupported"):
            return None, {"status": "unsupported", "interface": display_name}
        # Apply posted device_id (VC member selection) without mutating the cached list.
        link_data = {
            **link_data,
            "device_id": interface.get("device_id", link_data.get("device_id")),
            "expected_cable_intent": interface.get("expected_cable_intent"),
        }
        selected_device_id = coerce_librenms_id(interface.get("device_id"))
        current_local_device_id = coerce_librenms_id(link_data.get("netbox_local_device_id"))
        if selected_device_id is not None and selected_device_id != current_local_device_id:
            if not self._selected_device_is_in_page_context(selected_device_id):
                return None, {"status": "rejected_selection", "interface": display_name}
            selected_device = self.restricted_queryset(Device, "view").filter(pk=selected_device_id).first()
            if selected_device is None:
                return None, {"status": "rejected_selection", "interface": display_name}
            selected_local = resolve_interface_on_device(
                selected_device,
                getattr(self, "_post_server_key", None) or self.librenms_api.server_key,
                link_data.get("local_port_id"),
                [link_data.get("local_port"), link_data.get("local_port_alt")],
            )
            if (
                selected_local is None
                or not self.restricted_queryset(Interface, "change").filter(pk=selected_local.pk).exists()
            ):
                return None, {"status": "stale", "interface": display_name}
            link_data["netbox_local_interface_id"] = selected_local.pk
            link_data["netbox_local_device_id"] = selected_local.device_id
        return link_data, None

    def process_single_interface(self, interface, cached_links, force=False):
        """Process cable creation for a single interface from cached link data."""
        link_data, result = self._selected_row_link_data(interface, cached_links)
        if result is not None:
            return result
        display_name = link_data.get("local_port") or interface.get("row_id", "")
        current_local_id = coerce_librenms_id(link_data.get("netbox_local_interface_id"))
        current_local_device_id = coerce_librenms_id(link_data.get("netbox_local_device_id"))
        current_remote_id = coerce_librenms_id(link_data.get("netbox_remote_interface_id"))
        current_remote_device_id = coerce_librenms_id(link_data.get("netbox_remote_device_id"))
        if (
            interface.get("expected_local_id") != current_local_id
            or interface.get("expected_local_device_id") != current_local_device_id
            or interface.get("expected_remote_id") != current_remote_id
            or interface.get("expected_remote_device_id") != current_remote_device_id
        ):
            return {"status": "stale", "interface": display_name}
        return self.handle_cable_creation(link_data, interface, force=force)

    def verify_cable_creation_requirements(self, link_data):
        """Return True if all required NetBox IDs are present in link data."""
        required_fields = [
            "netbox_local_interface_id",
            "netbox_remote_interface_id",
        ]

        return all(link_data.get(field) for field in required_fields)

    def _selected_device_is_in_page_context(self, selected_device_id):
        """Return whether a posted device is the page device or one of its VC members."""
        initial_device = getattr(self, "_initial_device", None)
        if initial_device is None:
            return False
        try:
            selected_device_id = int(selected_device_id)
        except (TypeError, ValueError):
            return False
        if selected_device_id == initial_device.pk:
            return True
        virtual_chassis = getattr(initial_device, "virtual_chassis", None)
        return bool(virtual_chassis and virtual_chassis.members.filter(pk=selected_device_id).exists())

    def handle_cable_creation(self, link_data, interface, force=False):
        """Create a cable from link data and return the operation result."""
        resolved = self._resolve_cable_terminations(link_data, interface)
        if "status" in resolved:
            return resolved
        return self._apply_cable_action(
            resolved["local"],
            resolved["remote"],
            link_data,
            resolved["interface"],
            force,
        )

    def _resolve_cable_terminations(self, link_data, interface):
        """
        Resolve one row's current permission-scoped cable terminations.

        Args:
            link_data: The enriched cached cable row.
            interface: The selected row data from the request.

        Returns:
            dict: The resolved terminations or a status result.
        """
        display_name = link_data.get("local_port") or interface.get("row_id", "")
        if link_data.get("_source") == "serial":
            csp_id = link_data.get("netbox_local_interface_id")
            cp_id = link_data.get("netbox_remote_interface_id")
            if not csp_id:
                return {"status": "invalid", "interface": display_name}
            if not cp_id:
                return {"status": "missing_remote", "interface": display_name}
            try:
                csp = self.restricted_queryset(ConsoleServerPort, "change").get(pk=csp_id)
            except ConsoleServerPort.DoesNotExist:
                return {"status": "invalid", "interface": display_name}
            try:
                cp = self.restricted_queryset(ConsolePort, "change").get(pk=cp_id)
            except ConsolePort.DoesNotExist:
                return {"status": "missing_remote", "interface": display_name}
            if str(interface.get("device_id")) != str(csp.device_id):
                return {"status": "rejected_selection", "interface": display_name}
            return {"local": csp, "remote": cp, "interface": display_name}

        if not self.verify_cable_creation_requirements(link_data):
            if not link_data.get("netbox_remote_device_id") or not link_data.get("netbox_remote_interface_id"):
                return {"status": "missing_remote", "interface": display_name}
            return {"status": "invalid", "interface": display_name}

        try:
            local_interface = self.restricted_queryset(Interface, "change").get(
                pk=link_data["netbox_local_interface_id"]
            )
        except Interface.DoesNotExist:
            return {"status": "invalid", "interface": display_name}

        # Honour user's VC member selection: if the selected device_id differs from
        # the cached interface's device, look up the same port name on that device.
        selected_device_id = interface.get("device_id")
        if selected_device_id and str(local_interface.device_id) != str(selected_device_id):
            if not self._selected_device_is_in_page_context(selected_device_id):
                logger.debug(
                    "Selected device %s is outside the cable-sync page context; rejecting cable creation",
                    selected_device_id,
                )
                return {"status": "rejected_selection", "interface": display_name}
            port_name = link_data.get("local_port") or local_interface.name
            try:
                local_interface = self.restricted_queryset(Interface, "change").get(
                    device_id=selected_device_id,
                    name=port_name,
                )
            except Interface.DoesNotExist:
                logger.debug(
                    "Port %s not found on selected device %s; rejecting cable creation",
                    port_name,
                    selected_device_id,
                )
                return {"status": "invalid", "interface": display_name}

        try:
            remote_interface = self.restricted_queryset(Interface, "change").get(
                pk=link_data["netbox_remote_interface_id"]
            )
        except Interface.DoesNotExist:
            return {"status": "missing_remote", "interface": display_name}
        return {"local": local_interface, "remote": remote_interface, "interface": display_name}

    def validate_selected_endpoint_uniqueness(self, selected_interfaces, cached_links):
        """Reject a batch when two selected rows resolve to one NetBox termination."""
        endpoints = set()
        for interface in selected_interfaces:
            link_data, result = self._selected_row_link_data(interface, cached_links)
            if result is not None:
                continue
            resolved = self._resolve_cable_terminations(link_data, interface)
            if "status" in resolved:
                continue
            local_key = (resolved["local"]._meta.label_lower, resolved["local"].pk)
            remote_key = (resolved["remote"]._meta.label_lower, resolved["remote"].pk)
            if local_key == remote_key or local_key in endpoints or remote_key in endpoints:
                messages.error(
                    self.request,
                    "Selected rows resolve to the same cable endpoint. No cables were changed.",
                )
                return False
            endpoints.update((local_key, remote_key))
        return True

    def process_interface_sync(self, selected_interfaces, cached_links, force=False):
        """
        Process cable sync for all selected interfaces and return results.

        Each interface is processed in its own atomic block so individual
        failures roll back only that cable without affecting others.

        Force-protected replacements submitted without ``force`` are bucketed under ``conflict``
        and stashed in full. The state includes the re-submit row identity and the doomed cable's
        trace on ``self._pending_conflicts`` so
        :meth:`_sync_response` can raise the force-confirm modal without changing this return type.

        Args:
            selected_interfaces (list[dict]): The selected interface entries to synchronize.
            cached_links (list[dict]): The cached LibreNMS link data.
            force (bool): Overwrite a cable the plugin does not solely own.

        Returns:
            dict[str, list[str]]: The interface names grouped by synchronization result.
        """
        results = {
            "valid": [],
            "invalid": [],
            "failed": [],
            "duplicate": [],
            "missing_remote": [],
            "rejected_selection": [],
            "skipped": [],
            "overwritten": [],
            "tagged": [],
            "conflict": [],
            "denied": [],
            "stale": [],
            "unverified": [],
            "unsupported": [],
            "patch_path": [],
        }
        self._pending_conflicts = []

        for interface in selected_interfaces:
            try:
                with transaction.atomic():
                    result = self.process_single_interface(interface, cached_links, force=force)
                results[result["status"]].append(result.get("interface", ""))
                if result["status"] == "conflict":
                    # Carry the row's RESOLVED sync device so the force re-submit re-targets the
                    # exact interface the user confirmed — without it, a VC member override
                    # (device_selection_<row_id>) would silently revert to the page device.
                    result["device_id"] = interface.get("device_id")
                    self._pending_conflicts.append(result)
            except PermissionDenied:
                # A permission raised anywhere below (signals, custom validators) is a denial, not
                # missing link data.
                logger.warning("Denied cable sync for row %s", interface.get("row_id", ""))
                results["denied"].append(interface.get("row_id", ""))
            except Exception:
                logger.exception("Failed to sync cable row %s", interface.get("row_id", ""))
                results["failed"].append(interface.get("row_id", ""))

        return results

    def post(self, request, pk):
        """Sync selected cable connections from LibreNMS into NetBox."""
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        initial_device = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return redirect(
                f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            )
        origin_device = initial_device
        raw_origin_device_id = request.POST.get("origin_device_id")
        if raw_origin_device_id is not None:
            origin_device_id = coerce_librenms_id(raw_origin_device_id)
            if origin_device_id is None:
                messages.error(request, "A valid cable page device is required.")
                return redirect_with_server_key(
                    request,
                    f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}"
                    "?tab=cables",
                    server_key,
                )
            origin_device = self.restrict_object_or_404(Device, pk=origin_device_id)
            same_device = origin_device.pk == initial_device.pk
            same_chassis = (
                origin_device.virtual_chassis_id is not None
                and origin_device.virtual_chassis_id == initial_device.virtual_chassis_id
            )
            if not same_device and not same_chassis:
                messages.error(request, "The cable page and selected device do not match.")
                return redirect_with_server_key(
                    request,
                    f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}"
                    "?tab=cables",
                    server_key,
                )
        if get_migrated_to_marker(origin_device, server_key) or get_migrated_to_marker(initial_device, server_key):
            messages.error(request, "This device has been migrated and is read-only for this LibreNMS server.")
            return redirect_with_server_key(
                request,
                f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}"
                "?tab=cables",
                server_key,
            )
        self._post_server_key = server_key
        self._initial_device = initial_device
        self._origin_device = origin_device
        # The force-confirm modal re-submits with force=on to authorize replacement of the exact
        # current cable topology and desired endpoints. The checkbox is clear by default.
        force = request.POST.get("force") == "on"
        redirect_url = (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if self.validate_prerequisites(
            cached_links, selected_interfaces
        ) and self.validate_selected_endpoint_uniqueness(
            selected_interfaces,
            cached_links,
        ):
            results = self.process_interface_sync(selected_interfaces, cached_links, force=force)
            self.display_sync_results(request, results)
            if results["valid"] or results.get("overwritten") or results.get("tagged"):
                schedule_request_cache_mutation(
                    request,
                    initial_device,
                    SyncTab.CABLES,
                    server_key,
                )

        response = self._sync_response(request, initial_device, server_key, redirect_url)
        return apply_request_cache_transition(request, response)

    def _sync_response(self, request, obj, server_key, redirect_url):
        """
        Return the post-sync response: an HTMX partial re-render, or a full-page redirect.

        An HTMX submit gets the same ``#cable-sync-content`` partial the "Refresh Cables" action
        produces, rebuilt from the (now cable-updated) cache so a just-synced row flips to
        "Cable Found" and drops its Sync button — no full-page reload. The sync flash messages
        render inline via the partial's ``inc/messages.html`` include, and the page's global
        ``htmx:afterSwap`` handler re-initialises the table's checkboxes/filters/selects.

        A non-HTMX submit (JS disabled, or a direct POST) still gets the redirect, where Django
        messages survive to the reloaded tab.
        """
        # htmx always sends "HX-Request: true"; match the exact value (mirrors modules.py) so a
        # non-htmx POST — or a test's mock request whose headers aren't a real dict — falls through
        # to the redirect rather than the partial re-render.
        if request.headers.get("HX-Request") != "true":
            return redirect(redirect_url)

        # Delegate the table/partial machinery to the cable-table view. Imported locally to avoid
        # a module-load import cycle (object_sync imports the base cable view stack).
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = DeviceCableTableView()
        view.setup(request, pk=obj.pk)
        # Rebind the delegated view's client to the POST-scoped server so its cache read and
        # re-enrichment target the same server the sync acted on (mirrors the refresh path).
        # A POSTed key that names no configured server (stale tab / forged form) must not be
        # reused: it would namespace the cache read AND render_sync_partial's migrated-donor
        # context under a bogus key. Degrade to the session/default server's RESOLVED key (and,
        # like that path, never touch the lazy librenms_api property — it can raise here).
        resolved_key = view.rebind_api_for_server_or_default(server_key)
        context = view._prepare_context(request, obj, fetch_fresh=False, server_key=resolved_key)
        if context is None:
            # Cache genuinely gone (e.g. TTL elapsed between refresh and sync): render an empty
            # table but keep the sync flash messages.
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": resolved_key}
        elif context.get("table") is not None:
            # _prepare_context derives the table's pagination/sort htmx_url from request.path, which
            # here is the sync POST endpoint. Repoint it at the cable-table refresh endpoint (a GET
            # there returns the same fragment) so paging/sorting still works after a sync swap —
            # matching the URL the "Refresh Cables" action produces.
            tab_url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[obj.pk])
            context["table"].htmx_url = f"{tab_url}?tab=cables" + (
                f"&server_key={quote_plus(resolved_key)}" if resolved_key else ""
            )

        # Overwrite-protected conflicts: surface them for the force-confirm modal, which the partial
        # injects into the shared #htmx-modal via an out-of-band swap (auto-shown by the page JS).
        conflicts = getattr(self, "_pending_conflicts", [])
        if conflicts:
            context["overwrite_conflicts"] = conflicts
            context["server_key"] = resolved_key
            context["object"] = obj
            context["origin_device_id"] = getattr(getattr(self, "_origin_device", None), "pk", obj.pk)
        elif request.POST.get("force"):
            # A force submit comes FROM the force-confirm modal; with every conflict resolved
            # the main swap only refreshes the table, so ship the close_modal OOB block too —
            # otherwise the modal stays open over the refreshed content.
            context["close_modal"] = True
        return view.render_sync_partial(request, obj, resolved_key, {"cable_sync": context})

    def display_sync_results(self, request, results):
        """Display flash messages summarizing the cable sync results."""
        if results["missing_remote"]:
            messages.error(
                request,
                f"Remote device or interface not found in NetBox for: {', '.join(results['missing_remote'])}",
            )
        if results["invalid"]:
            messages.error(
                request,
                f"No LibreNMS link data found for interfaces: {', '.join(results['invalid'])}",
            )
        if results.get("failed"):
            messages.error(
                request,
                f"Failed to sync cables for interfaces: {', '.join(results['failed'])}",
            )
        if results.get("rejected_selection"):
            messages.error(
                request,
                "Selected device is not part of this cable-sync page for interfaces: "
                f"{', '.join(results['rejected_selection'])}",
            )
        if results.get("denied"):
            messages.error(
                request,
                "You do not have permission to change the selected cable connection(s) for: "
                f"{', '.join(results['denied'])}.",
            )
        if results.get("stale"):
            messages.error(
                request,
                "The cable state or target changed after confirmation. Refresh and review the current cable for: "
                f"{', '.join(results['stale'])}",
            )
        if results.get("unverified"):
            messages.error(
                request,
                "These links come from a LibreNMS source the last refresh could not read, so they "
                "may be out of date. Refresh Cables before syncing: "
                f"{', '.join(results['unverified'])}.",
            )
        if results.get("unsupported"):
            messages.error(
                request,
                "Multi-termination cables cannot be changed by cable sync. Update them in NetBox for: "
                f"{', '.join(results['unsupported'])}.",
            )
        if results["duplicate"]:
            messages.warning(
                request,
                f"Cable already exists for interfaces: {', '.join(results['duplicate'])}",
            )
        if results.get("skipped"):
            messages.info(
                request,
                "Skipped OOB-controller links (context only, not syncable to the host): "
                f"{', '.join(results['skipped'])}",
            )
        if results.get("patch_path"):
            messages.info(
                request,
                f"Skipped links already modeled through a patch path: {', '.join(results['patch_path'])}",
            )
        if results.get("tagged"):
            messages.info(
                request,
                f"Tagged existing cable(s) as LibreNMS-managed for: {', '.join(results['tagged'])}",
            )
        if results.get("conflict"):
            messages.warning(
                request,
                "Overwrite protection: confirm the current cable(s) in the dialog for: "
                f"{', '.join(results['conflict'])}",
            )
        if results.get("overwritten"):
            messages.success(
                request,
                f"Overwrote existing cable for interfaces: {', '.join(results['overwritten'])}",
            )
        if results["valid"]:
            messages.success(
                request,
                f"Successfully created cable for interfaces: {', '.join(results['valid'])}",
            )
