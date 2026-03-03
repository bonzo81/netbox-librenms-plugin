"""
Module for initializing views for the NetBox LibreNMS plugin.

All imports below are intentional re-exports consumed by urls.py and
other modules.  The F401 suppressions prevent linters from flagging
them as unused within this file.
"""

from .base.cables_view import BaseCableTableView, SingleCableVerifyView  # noqa: F401
from .base.interfaces_view import BaseInterfaceTableView  # noqa: F401
from .base.ip_addresses_view import BaseIPAddressTableView, SingleIPAddressVerifyView  # noqa: F401
from .base.librenms_sync_view import BaseLibreNMSSyncView  # noqa: F401
from .base.vlan_table_view import BaseVLANTableView  # noqa: F401
from .imports import (  # noqa: F401
    BulkImportConfirmView,
    BulkImportDevicesView,
    DeviceClusterUpdateView,
    DeviceConflictActionView,
    DeviceRackUpdateView,
    DeviceRoleUpdateView,
    DeviceValidationDetailsView,
    DeviceVCDetailsView,
    LibreNMSImportView,
    SaveUserPrefView,
)
from .mapping_views import (  # noqa: F401
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingBulkImportView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
)
from .object_sync import (  # noqa: F401
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    DeviceVLANTableView,
    SaveVlanGroupOverridesView,
    SingleInterfaceVerifyView,
    SingleVlanGroupVerifyView,
    VerifyVlanSyncGroupView,
    VMInterfaceTableView,
    VMIPAddressTableView,
    VMLibreNMSSyncView,
)
from .settings_views import LibreNMSSettingsView, TestLibreNMSConnectionView  # noqa: F401
from .status_check import DeviceStatusListView, VMStatusListView  # noqa: F401
from .sync.cables import SyncCablesView  # noqa: F401
from .sync.device_fields import (  # noqa: F401
    AssignVCSerialView,
    CreateAndAssignPlatformView,
    UpdateDeviceNameView,
    UpdateDevicePlatformView,
    UpdateDeviceSerialView,
    UpdateDeviceTypeView,
)
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView  # noqa: F401
from .sync.interfaces import DeleteNetBoxInterfacesView, SyncInterfacesView  # noqa: F401
from .sync.ip_addresses import SyncIPAddressesView  # noqa: F401
from .sync.locations import SyncSiteLocationView  # noqa: F401
from .sync.vlans import SyncVLANsView  # noqa: F401
