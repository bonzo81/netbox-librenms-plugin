"""
Module for initializing views for the NetBox LibreNMS plugin.
"""

# Import views as needed
from .base_views import (BaseCableSyncTableView, BaseInterfaceSyncTableView,
                         BaseIPAddressSyncTableView, BaseLibreNMSSyncView)
from .device_views import (DeviceCableTableView, DeviceInterfaceTableView,
                           DeviceIPAddressTableView, DeviceLibreNMSSyncView)
from .mapping_views import (InterfaceTypeMappingBulkDeleteView,
                            InterfaceTypeMappingChangeLogView,
                            InterfaceTypeMappingCreateView,
                            InterfaceTypeMappingDeleteView,
                            InterfaceTypeMappingEditView,
                            InterfaceTypeMappingListView,
                            InterfaceTypeMappingView)
from .sync_views import (AddDeviceToLibreNMSView, SyncInterfacesView,
                         SyncSiteLocationView, UpdateDeviceLocationView)
from .vm_views import (VMInterfaceTableView, VMIPAddressTableView,
                       VMLibreNMSSyncView)
