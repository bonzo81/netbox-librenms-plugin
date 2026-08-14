"""Cache-only endpoints for open LibreNMS sync pages."""

import copy

from dcim.models import Device
from django.http import Http404, JsonResponse
from django.views import View
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.sync_cache import (
    SyncCacheConsistency,
    SyncTab,
    mapped_server_keys,
    request_actor_id,
)
from netbox_librenms_plugin.utils import get_interface_name_field
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin


class SyncCacheStatusView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Return current cache state without contacting LibreNMS."""

    def _model(self, object_type):
        if object_type == "device":
            return Device
        if object_type == "virtualmachine":
            return VirtualMachine
        raise Http404("Unsupported sync object type.")

    def get(self, request, object_type, pk):
        model = self._model(object_type)
        self.required_object_permissions = {"GET": [("view", model)]}
        if error := self.require_all_permissions_json("GET"):
            return error

        obj = self.restrict_object_or_404(model, pk=pk)
        server_key = request.GET.get("server_key")
        if not server_key or server_key not in mapped_server_keys(obj, server_key):
            raise Http404("LibreNMS server is not mapped to this object.")

        coordinator = SyncCacheConsistency(obj)
        return JsonResponse(
            {
                "object_type": object_type,
                "object_id": obj.pk,
                "server_key": server_key,
                "tabs": coordinator.status_for_request(request, server_key),
            }
        )


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
        if error := self.require_all_permissions_json("GET"):
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
        obj, error = self._object(request, object_type, pk)
        if error:
            return error
        try:
            sync_tab = SyncTab(tab)
        except ValueError:
            raise Http404("Unsupported sync tab.") from None

        server_key = request.GET.get("server_key")
        if not server_key or server_key not in mapped_server_keys(obj, server_key):
            raise Http404("LibreNMS server is not mapped to this object.")

        coordinator = SyncCacheConsistency(obj)
        if not coordinator.status(server_key, actor_id=request_actor_id(request))[sync_tab.value]["snapshot_available"]:
            raise Http404("The sync snapshot is not available.")

        view_class = self._tab_view(object_type, sync_tab)
        if view_class is None:
            raise Http404("This sync tab is not available for the object type.")
        tab_view = view_class()
        tab_view.request = copy.copy(request)
        tab_view.cache_only = True

        if sync_tab == SyncTab.INTERFACES:
            context = tab_view.get_context_data(request, obj, get_interface_name_field(request, obj))
            payload = {"interface_sync": context}
        elif sync_tab == SyncTab.CABLES:
            payload = {"cable_sync": tab_view.get_context_data(request, obj)}
        elif sync_tab == SyncTab.IP_ADDRESSES:
            payload = {"ip_sync": tab_view.get_context_data(request, obj)}
        elif sync_tab == SyncTab.MODULES:
            payload = {"module_sync": tab_view.get_context_data(request, obj)}
        else:
            payload = {"vlan_sync": tab_view.get_vlan_context(request, obj, server_key)}

        return tab_view.render_sync_partial(request, obj, server_key, payload)
