"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# Import views as needed
from .base_views import (
    BaseCableSyncTableView,
    BaseInterfaceSyncTableView,
    BaseIPAddressSyncTableView,
    BaseLibreNMSSyncView,
)
from .device_views import (
    DeviceLibreNMSSyncView,
    DeviceInterfaceTableView,
    DeviceCableTableView,
    DeviceIPAddressTableView,
)
from .sync_views import (
    SyncInterfacesView,
    AddDeviceToLibreNMSView,
    UpdateDeviceLocationView,
    SyncSiteLocationView,
)
from .vm_views import (
    VMLibreNMSSyncView,
    VMInterfaceTableView,
    VMIPAddressTableView,
)
from .mapping_views import (
    InterfaceTypeMappingListView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingChangeLogView,
)
