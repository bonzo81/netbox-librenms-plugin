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
    CreatePlatformFromImportView,
    DeviceClusterUpdateView,
    DeviceConflictActionView,
    DeviceRackUpdateView,
    DeviceRoleUpdateView,
    DeviceValidationDetailsView,
    DeviceVCDetailsView,
    LibreNMSImportView,
    SaveUserPrefView,
)
from .imports.actions import (  # noqa: F401
    AddAsOOBView,
    AddDeviceTypeMappingView,
    AddPlatformMappingView,
    MergeNetBoxDevicesView,
    PromoteToHostView,
)
from .mapping_views import (  # noqa: F401
    CarrierAutoInstallRuleBulkDeleteView,
    CarrierAutoInstallRuleBulkExportYAMLView,
    CarrierAutoInstallRuleBulkImportView,
    CarrierAutoInstallRuleChangeLogView,
    CarrierAutoInstallRuleCreateView,
    CarrierAutoInstallRuleDeleteView,
    CarrierAutoInstallRuleEditView,
    CarrierAutoInstallRuleListView,
    CarrierAutoInstallRuleView,
    DeviceTypeMappingBulkDeleteView,
    DeviceTypeMappingBulkExportYAMLView,
    DeviceTypeMappingBulkImportView,
    DeviceTypeMappingChangeLogView,
    DeviceTypeMappingCreateView,
    DeviceTypeMappingDeleteView,
    DeviceTypeMappingEditView,
    DeviceTypeMappingListView,
    DeviceTypeMappingView,
    InterfaceTypeMappingBulkDeleteView,
    InterfaceTypeMappingBulkExportYAMLView,
    InterfaceTypeMappingBulkImportView,
    InterfaceTypeMappingChangeLogView,
    InterfaceTypeMappingCreateView,
    InterfaceTypeMappingDeleteView,
    InterfaceTypeMappingEditView,
    InterfaceTypeMappingListView,
    InterfaceTypeMappingView,
    InventoryIgnoreRuleBulkDeleteView,
    InventoryIgnoreRuleBulkExportYAMLView,
    InventoryIgnoreRuleBulkImportView,
    InventoryIgnoreRuleChangeLogView,
    InventoryIgnoreRuleCreateView,
    InventoryIgnoreRuleDeleteView,
    InventoryIgnoreRuleEditView,
    InventoryIgnoreRuleListView,
    InventoryIgnoreRuleView,
    LocationMappingBulkDeleteView,
    LocationMappingBulkExportYAMLView,
    LocationMappingBulkImportView,
    LocationMappingChangeLogView,
    LocationMappingCreateView,
    LocationMappingDeleteView,
    LocationMappingEditView,
    LocationMappingListView,
    LocationMappingView,
    ModuleBayMappingBulkDeleteView,
    ModuleBayMappingBulkExportYAMLView,
    ModuleBayMappingBulkImportView,
    ModuleBayMappingChangeLogView,
    ModuleBayMappingCreateView,
    ModuleBayMappingDeleteView,
    ModuleBayMappingEditView,
    ModuleBayMappingListView,
    ModuleBayMappingView,
    ModuleTypeMappingBulkDeleteView,
    ModuleTypeMappingBulkExportYAMLView,
    ModuleTypeMappingBulkImportView,
    ModuleTypeMappingChangeLogView,
    ModuleTypeMappingCreateView,
    ModuleTypeMappingDeleteView,
    ModuleTypeMappingEditView,
    ModuleTypeMappingListView,
    ModuleTypeMappingView,
    NormalizationRuleBulkDeleteView,
    NormalizationRuleBulkExportYAMLView,
    NormalizationRuleBulkImportView,
    NormalizationRuleChangeLogView,
    NormalizationRuleCreateView,
    NormalizationRuleDeleteView,
    NormalizationRuleEditView,
    NormalizationRuleListView,
    NormalizationRuleView,
    PlatformMappingBulkDeleteView,
    PlatformMappingBulkExportYAMLView,
    PlatformMappingBulkImportView,
    PlatformMappingChangeLogView,
    PlatformMappingCreateView,
    PlatformMappingDeleteView,
    PlatformMappingEditView,
    PlatformMappingListView,
    PlatformMappingView,
    PortStackLagPatternBulkDeleteView,
    PortStackLagPatternBulkExportYAMLView,
    PortStackLagPatternBulkImportView,
    PortStackLagPatternChangeLogView,
    PortStackLagPatternCreateView,
    PortStackLagPatternDeleteView,
    PortStackLagPatternEditView,
    PortStackLagPatternListView,
    PortStackLagPatternView,
)
from .object_sync import (  # noqa: F401
    DeviceCableTableView,
    DeviceInterfaceTableView,
    DeviceIPAddressTableView,
    DeviceLibreNMSSyncView,
    DeviceModuleTableView,
    DeviceVLANTableView,
    SaveVlanGroupOverridesView,
    SingleInterfaceVerifyView,
    SingleModuleVerifyView,
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
    ConvertLegacyLibreNMSIdView,
    CreateAndAssignPlatformView,
    RemoveServerMappingView,
    UpdateDeviceNameView,
    UpdateDevicePlatformView,
    UpdateDeviceSerialView,
    UpdateDeviceTypeView,
)
from .sync.devices import AddDeviceToLibreNMSView, UpdateDeviceLocationView  # noqa: F401
from .sync.interfaces import (  # noqa: F401
    DeleteNetBoxInterfacesView,
    SyncInterfaceLagView,
    SyncInterfaceParentView,
    SyncInterfacesView,
)
from .sync.ip_addresses import SyncIPAddressesView  # noqa: F401
from .sync.locations import SyncSiteLocationView  # noqa: F401
from .sync.migrate import (  # noqa: F401
    MoveInterfaceToWinnerView,
    MoveIPAddressToWinnerView,
    TransferDeviceIPView,
)
from .sync.modules import (  # noqa: F401
    AddBayTemplateView,
    InstallBranchView,
    InstallModuleView,
    InstallSelectedView,
    ModuleMismatchPreviewView,
    MoveModuleView,
    ReplaceModuleView,
    UpdateModuleInterfaceView,
    UpdateModuleSerialView,
    VCNormalizationReportView,
)
from .sync.vlans import SyncVLANsView  # noqa: F401
