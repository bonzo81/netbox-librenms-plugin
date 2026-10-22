"""Cache-only endpoints for open LibreNMS sync pages."""

import copy

from dcim.models import Device
from django.http import Http404, JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.server_mappings import mapped_device_servers
from netbox_librenms_plugin.sync_cache import (
    SyncCacheConsistency,
    SyncTab,
    request_actor_id,
)
from netbox_librenms_plugin.sync_query import SyncQuery
from netbox_librenms_plugin.utils import build_migrated_context, get_interface_name_field
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin


# The tabs whose table has a server-side filter.
FILTER_TABS = frozenset(
    {SyncTab.INTERFACES.value, SyncTab.CABLES.value, SyncTab.IP_ADDRESSES.value, SyncTab.VLANS.value}
)


class SyncCacheStatusView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Return current cache state without contacting LibreNMS."""

    # A fetch() caller reads the answer, so the middleware answers with a JSON error.
    answers_json = True

    def _model(self, object_type):
        if object_type == "device":
            return Device
        if object_type == "virtualmachine":
            return VirtualMachine
        raise Http404("Unsupported sync object type.")

    def get(self, request, object_type, pk):
        model = self._model(object_type)
        self.required_object_permissions = {"GET": [("view", model)]}
        if error := self.require_object_permissions_json("GET"):
            return error

        obj = self.restrict_object_or_404(model, pk=pk)
        server_key = request.GET.get("server_key")
        if not server_key or server_key not in mapped_device_servers(obj, active_server=server_key):
            raise Http404("LibreNMS server is not mapped to this object.")

        coordinator = SyncCacheConsistency(obj)
        response = JsonResponse(
            {
                "object_type": object_type,
                "object_id": obj.pk,
                "server_key": server_key,
                "tabs": coordinator.status_for_request(request, server_key),
            }
        )
        # Per-viewer sync state, so it must not be written to any browser or shared cache.
        response["Cache-Control"] = "no-store"
        return response


class SyncCacheFragmentView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Render one tab from its current cache without contacting LibreNMS."""

    def _object(self, request, object_type, pk):
        if object_type == "device":
            model = Device
        elif object_type == "virtualmachine":
            model = VirtualMachine
        else:
            raise Http404("Unsupported sync object type.")
        self.required_object_permissions = {"GET": [("view", model)]}
        if error := self.require_object_permissions_json("GET"):
            return None, error
        return self.restrict_object_or_404(model, pk=pk), None

    @staticmethod
    def _tab_view(object_type, tab):
        from netbox_librenms_plugin.views.object_sync.devices import (
            DeviceCableTableView,
            DeviceInterfaceTableView,
            DeviceIPAddressTableView,
            DeviceModuleTableView,
            DeviceVLANTableView,
        )
        from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView, VMIPAddressTableView

        if object_type == "device":
            return {
                SyncTab.INTERFACES: DeviceInterfaceTableView,
                SyncTab.CABLES: DeviceCableTableView,
                SyncTab.IP_ADDRESSES: DeviceIPAddressTableView,
                SyncTab.MODULES: DeviceModuleTableView,
                SyncTab.VLANS: DeviceVLANTableView,
            }.get(tab)
        return {
            SyncTab.INTERFACES: VMInterfaceTableView,
            SyncTab.IP_ADDRESSES: VMIPAddressTableView,
        }.get(tab)

    def get(self, request, object_type, pk, tab):
        obj, tab_view, server_key, payload, error = self._cached_tab(request, object_type, pk, tab)
        if error:
            return error
        response = tab_view.render_sync_partial(request, obj, server_key, payload)
        # Same identity-varying content as the status endpoint above.
        response["Cache-Control"] = "no-store"
        return response

    def _cached_tab(self, request, object_type, pk, tab, *, require_snapshot=True):
        """Return the object, the tab view, the server key and the tab context, read from the cache only."""
        obj, error = self._object(request, object_type, pk)
        if error:
            return None, None, None, None, error
        try:
            sync_tab = SyncTab(tab)
        except ValueError:
            raise Http404("Unsupported sync tab.") from None

        server_key = request.GET.get("server_key")
        if not server_key or server_key not in mapped_device_servers(obj, active_server=server_key):
            raise Http404("LibreNMS server is not mapped to this object.")

        coordinator = SyncCacheConsistency(obj)
        view_class = self._tab_view(object_type, sync_tab)
        if view_class is None:
            raise Http404("This sync tab is not available for the object type.")
        if require_snapshot:
            tab_status = coordinator.status(server_key, actor_id=request_actor_id(request)).get(sync_tab.value)
            if not tab_status or not tab_status.get("snapshot_available"):
                raise Http404("The sync snapshot is not available.")
        tab_view = view_class()
        tab_view.request = copy.copy(request)
        tab_view.cache_only = True

        if sync_tab == SyncTab.INTERFACES:
            interface_name_field = get_interface_name_field(request, obj)
            context = tab_view.get_context_data(request, obj, interface_name_field)
            payload = {"interface_sync": context, "interface_name_field": interface_name_field}
        elif sync_tab == SyncTab.CABLES:
            payload = {"cable_sync": tab_view.get_context_data(request, obj)}
        elif sync_tab == SyncTab.IP_ADDRESSES:
            payload = {"ip_sync": tab_view.get_context_data(request, obj)}
        elif sync_tab == SyncTab.MODULES:
            payload = {"module_sync": tab_view.get_context_data(request, obj)}
        else:
            payload = {"vlan_sync": tab_view.get_vlan_context(request, obj, server_key)}
        return obj, tab_view, server_key, payload, None


class NetBoxOnlyInterfacesView(SyncCacheFragmentView):
    """Render the NetBox-only interfaces of the interfaces tab, which its dialog loads when it opens."""

    def get(self, request, object_type, pk):
        obj, tab_view, server_key, payload, error = self._cached_tab(request, object_type, pk, SyncTab.INTERFACES.value)
        if error:
            return error
        # A device can have thousands of these, so the tab leaves them out until the dialog asks.
        tab_view.partial_template_name = "netbox_librenms_plugin/inc/_netbox_only_interfaces.html"
        response = tab_view.render_sync_partial(request, obj, server_key, payload)
        response["Cache-Control"] = "no-store"
        return response


class SyncTabFilterView(SyncCacheFragmentView):
    """
    Render one filtered sync tab for its filter form, from the cache only.

    The tab content includes its Refresh toolbar. The tab links carry the page query outside it,
    so the response renders those links out of band. ``HX-Push-Url`` puts the sync page URL with the
    new query in the address bar.
    """

    def get(self, request, object_type, pk, tab):
        if tab not in FILTER_TABS:
            raise Http404("This sync tab has no filter.")
        # A tab with no snapshot renders its empty state, as the sync page does.
        obj, tab_view, server_key, payload, error = self._cached_tab(
            request, object_type, pk, tab, require_snapshot=False
        )
        if error:
            return error
        response = tab_view.render_sync_partial(request, obj, server_key, payload)
        # The sync page is the object's LibreNMS Sync tab, which NetBox serves under the object's own path.
        page_path = reverse(f"{obj._meta.app_label}:{obj._meta.model_name}_librenms_sync", args=[obj.pk])
        page_query = SyncQuery.from_request(request)
        coordinator = SyncCacheConsistency(obj)
        out_of_band = render_to_string(
            "netbox_librenms_plugin/inc/_sync_filter_out_of_band.html",
            {
                "tab": tab,
                "active_sync_tab": tab,
                "sync_tab_urls": {
                    sync_tab.value: page_query.tab_url(page_path, sync_tab.value)
                    for sync_tab in coordinator.applicable_tabs()
                },
                "sync_cache_status": coordinator.status_for_request(request, server_key, active_tab=SyncTab(tab)),
                "interface_name_field": get_interface_name_field(request, obj),
            },
            request=request,
        )
        response.content += out_of_band.encode()
        response["HX-Push-Url"] = f"{page_path}?{request.GET.urlencode()}"
        response["Cache-Control"] = "no-store"
        return response
