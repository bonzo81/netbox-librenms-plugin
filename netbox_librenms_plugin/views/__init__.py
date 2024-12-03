"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# Import views as needed
from .base_views import (
    BaseCableTableView,
    BaseInterfaceTableView,
    BaseIPAddressTableView,
    BaseLibreNMSSyncView,
)
from .device_views import (
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    SingleCableVerifyView,
    SingleInterfaceVerifyView,
)
from .mapping_views import (
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
)
from .sync.cables import (
    SyncCablesView,
)
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView
from .sync.interfaces import SyncInterfacesView
from .sync.locations import SyncSiteLocationView
from .vm_views import VMInterfaceTableView, VMIPAddressTableView, VMLibreNMSSyncView
