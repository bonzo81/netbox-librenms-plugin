"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# Import views as needed
from .base.cables_view import BaseCableTableView, SingleCableVerifyView
from .base.interfaces_view import BaseInterfaceTableView
from .base.ip_addresses_view import BaseIPAddressTableView
from .base.librenms_sync_view import BaseLibreNMSSyncView

from .mapping_views import (
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
)
from .status_check import (
    DeviceStatusListView,
    VMStatusListView,
)
from .sync.cables_sync import SyncCablesView
from .sync.devices_sync import AddDeviceToLibreNMSView, UpdateDeviceLocationView
from .sync.interfaces_sync import SyncInterfacesView
from .sync.locations_sync import SyncSiteLocationView
from .sync.ipaddresses_sync import SyncIPAddressesView

from .device_views import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    SingleInterfaceVerifyView,
)

from .vm_views import VMInterfaceTableView, VMIPAddressTableView, VMLibreNMSSyncView
