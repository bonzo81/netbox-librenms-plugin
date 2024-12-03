"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# Import views as needed
from .base.cables import BaseCableTableView
from .base.interfaces import BaseInterfaceTableView
from .base.ip_addresses import BaseIPAddressTableView
from .base.librenms_sync import BaseLibreNMSSyncView

from .mapping_views import (
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
)
from .sync.cables import SyncCablesView
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView
from .sync.interfaces import SyncInterfacesView
from .sync.locations import SyncSiteLocationView

from .device_views import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    SingleCableVerifyView,
    SingleInterfaceVerifyView,
)

from .vm_views import VMInterfaceTableView, VMIPAddressTableView, VMLibreNMSSyncView
