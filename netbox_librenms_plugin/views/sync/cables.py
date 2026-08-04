import logging
from urllib.parse import quote_plus

from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device, Interface
from django.contrib import messages
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
    classify_cable_action,
    get_cable_sync_settings,
    get_librenms_cable_tag,
    get_librenms_sync_device,
    render_cable_trace,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


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
            ("change", Interface),
            ("add", Cable),
            ("change", Cable),
        ],
    }

    def get_selected_interfaces(self, request, initial_device):
        """
        Return selected interface entries from POST data.

        Each ``select`` value is a ``local_port_id`` (stable LibreNMS identifier)
        so that matching against cached link data is user-preference agnostic.

        Args:
            request (HttpRequest): The request that contains the selected interface data.
            initial_device (Device): The page device to use when no device override is selected.

        Returns:
            list[dict] | None: The selected interface entries, or None when no interfaces are selected.
        """
        selected_interfaces = []
        selected_data = [x for x in request.POST.getlist("select") if x]

        if not selected_data:
            return None

        for port_id in selected_data:
            override = request.POST.get(f"device_selection_{port_id}")
            device_id = override or initial_device.id
            selected_interfaces.append({"device_id": device_id, "local_port_id": port_id})

        return selected_interfaces

    def get_cached_links_data(self, request, obj):
        """Return cached LibreNMS link data for the given object."""
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        cache_obj = get_librenms_sync_device(obj, server_key=server_key) or obj
        cached_data = cache.get(self.get_cache_key(cache_obj, "links", server_key))
        if not cached_data:
            return None
        return cached_data.get("links", [])

    def create_cable(self, local_interface, remote_interface, request):
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
                sync_settings = get_cable_sync_settings()
                base_desc = sync_settings.cable_sync_description
                description = f"{base_desc} ({server_key})" if server_key else base_desc
                cable = Cable(
                    a_terminations=[local_interface],
                    b_terminations=[remote_interface],
                    status="connected",
                    color=sync_settings.cable_sync_tag_color,
                    description=description,
                    tenant=getattr(getattr(remote_interface, "device", None), "tenant", None),
                )
                cable.save()
                cable.tags.add(get_librenms_cable_tag())
            return True
        except Exception as exc:  # pragma: no cover - protects UX
            messages.error(request, f"Failed to create cable: {str(exc)}")
            return False

    def _apply_cable_action(self, local_term, remote_term, link_data, display_name, force):
        """
        Classify the sync for one resolved termination pair and act on (or defer) it.

        Delegates the tag-based decision to :func:`classify_cable_action` and then performs the
        chosen action: create a fresh enriched cable, add the librenms tag to an already-matching
        cable, overwrite plugin-owned cables outright, or — when a non-owned cable would be
        destroyed and *force* is not set — return a ``conflict`` result carrying the full trace(s)
        of what would be deleted, so the caller can raise the force-confirm modal.

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
        decision = classify_cable_action(local_term, remote_term)
        action = decision["action"]

        # A manually picked remote (CableRemotePickerView) re-pointing over an EXISTING cable
        # always confirms through the warning modal — even a plugin-owned one. The silent
        # safe-overwrite is reserved for LibreNMS-driven re-points (refresh data moved); a
        # human-initiated change of a live cable gets the full trace and the force checkbox.
        if action == "safe_overwrite" and link_data.get("manual_remote_id"):
            action = "needs_force"

        if action == "noop":
            # The desired cable already exists and is already tagged — nothing to do.
            return {"status": "duplicate", "interface": display_name}
        if action == "tag_only":
            # Same connection, just missing our tag: add it (non-destructive, no force needed).
            decision["cable"].tags.add(get_librenms_cable_tag())
            return {"status": "tagged", "interface": display_name}
        if action == "create":
            if self.create_cable(local_term, remote_term, self.request):
                return {"status": "valid", "interface": display_name}
            return {"status": "invalid", "interface": display_name}  # pragma: no cover
        if action == "safe_overwrite" or (action == "needs_force" and force):
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
            if self.create_cable(local_term, remote_term, self.request):
                return {"status": "overwritten", "interface": display_name}
            # Create failed AFTER deleting the old cable(s): raise so the per-interface atomic block
            # (process_interface_sync) rolls the deletes back rather than orphaning the connection.
            raise RuntimeError(f"cable overwrite failed for {display_name}")  # pragma: no cover

        # needs_force and not force → defer; carry each doomed cable's full end-to-end trace so the
        # modal can show the user exactly what a forced overwrite would destroy.
        return {
            "status": "conflict",
            "interface": display_name,
            "port_id": str(link_data.get("local_port_id", "")),
            "trace": [render_cable_trace(cable) for cable in decision["to_remove"]],
            # Only the endpoint-attached segment(s) are ever deleted — patch-panel trunks and
            # other mid-path segments carry other circuits and always stay. The modal uses these
            # labels to mark precisely which trace hops die.
            "removed_cables": [f"#{cable.pk}" for cable in decision["to_remove"]],
        }

    def validate_prerequisites(self, cached_links, selected_interfaces):
        """Validate that cached data and selections are present before sync."""
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

    def process_single_interface(self, interface, cached_links, force=False):
        """Process cable creation for a single interface from cached link data."""
        port_id = str(interface.get("local_port_id", ""))
        try:
            link_data = next(link for link in cached_links if str(link.get("local_port_id", "")) == port_id)
            # OOB-controller rows are merged into the host's cable list only for context
            # (shared-LOM detection) and must never be synced onto the host: their local_port can
            # resolve to a host interface (shared name), and creating a cable from OOB-controller
            # LLDP data would attach it to the wrong device. Mirrors the OOB guards in interface
            # sync (interfaces.py) and module sync (modules.py).
            if link_data.get("_source") == "oob":
                return {"status": "skipped", "interface": link_data.get("local_port") or port_id}
            # Apply posted device_id (VC member selection) without mutating the cached list.
            link_data = {**link_data, "device_id": interface.get("device_id", link_data.get("device_id"))}
            return self.handle_cable_creation(link_data, interface, force=force)
        except StopIteration:
            return {"status": "invalid", "interface": port_id}

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
        # Serial rows use ConsoleServerPort ↔ ConsolePort instead of Interface ↔ Interface.
        if link_data.get("_source") == "serial":
            return self.handle_serial_cable_creation(link_data, interface, force=force)

        display_name = link_data.get("local_port") or interface.get("local_port_id", "")
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
            return self._apply_cable_action(local_interface, remote_interface, link_data, display_name, force)
        except Interface.DoesNotExist:
            return {"status": "missing_remote", "interface": display_name}

    def handle_serial_cable_creation(self, link_data, interface, force=False):
        """Create a ConsoleServerPort ↔ ConsolePort cable for a serial row."""
        display_name = link_data.get("local_port") or interface.get("local_port_id", "")
        csp_id = link_data.get("netbox_local_interface_id")
        cp_id = link_data.get("netbox_remote_interface_id")

        if not csp_id or not cp_id:
            return {"status": "missing_remote", "interface": display_name}

        try:
            csp = ConsoleServerPort.objects.get(pk=csp_id)
            cp = ConsolePort.objects.get(pk=cp_id)
            return self._apply_cable_action(csp, cp, link_data, display_name, force)

        except (ConsoleServerPort.DoesNotExist, ConsolePort.DoesNotExist):
            return {"status": "missing_remote", "interface": display_name}

    def process_interface_sync(self, selected_interfaces, cached_links, force=False):
        """
        Process cable sync for all selected interfaces and return results.

        Each interface is processed in its own atomic block so individual
        failures roll back only that cable without affecting others.

        Force-protected conflicts (a would-be overwrite of a cable the plugin does not solely own,
        submitted without ``force``) are bucketed under ``conflict`` AND stashed in full, with the
        re-submit ``port_id`` and the doomed cable's trace, on ``self._pending_conflicts`` so
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
            "duplicate": [],
            "missing_remote": [],
            "rejected_selection": [],
            "skipped": [],
            "overwritten": [],
            "tagged": [],
            "conflict": [],
            "denied": [],
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
                    # (device_selection_<port_id>) would silently revert to the page device.
                    result["device_id"] = interface.get("device_id")
                    self._pending_conflicts.append(result)
            except Exception:
                logger.exception("Failed to sync cable for port_id %s", interface.get("local_port_id", ""))
                results["invalid"].append(interface.get("local_port_id", ""))

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
        self._post_server_key = server_key
        self._initial_device = initial_device
        # The force-confirm modal re-submits with force=on to authorise overwriting a cable the
        # plugin does not solely own (foreign/extra/no tag). Unchecked by default → opt-in.
        force = request.POST.get("force") == "on"
        redirect_url = (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[initial_device.pk])}?tab=cables"
            + (f"&server_key={quote_plus(server_key)}" if server_key else "")
        )

        selected_interfaces = self.get_selected_interfaces(request, initial_device)
        cached_links = self.get_cached_links_data(request, initial_device)

        if self.validate_prerequisites(cached_links, selected_interfaces):
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
        if results.get("rejected_selection"):
            messages.error(
                request,
                "Selected device is not part of this cable-sync page for interfaces: "
                f"{', '.join(results['rejected_selection'])}",
            )
        if results.get("denied"):
            messages.error(
                request,
                "You do not have permission to delete the existing cable(s) for: "
                f"{', '.join(results['denied'])} (dcim.delete_cable required to overwrite).",
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
        if results.get("tagged"):
            messages.info(
                request,
                f"Tagged existing cable(s) as LibreNMS-managed for: {', '.join(results['tagged'])}",
            )
        if results.get("conflict"):
            messages.warning(
                request,
                "Overwrite protection: confirm in the dialog to replace non-managed cable(s) for: "
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
