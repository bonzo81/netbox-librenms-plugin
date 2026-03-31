from .cables import LibreNMSCableTable
from .device_status import DeviceStatusTable
from .interfaces import LibreNMSInterfaceTable, LibreNMSVMInterfaceTable, VCInterfaceTable
from .ipaddresses import IPAddressTable
from .locations import SiteLocationSyncTable
from .mappings import (
    DeviceTypeMappingTable,
    InterfaceTypeMappingTable,
    InventoryIgnoreRuleTable,
    ModuleBayMappingTable,
    ModuleTypeMappingTable,
    NormalizationRuleTable,
    PlatformMappingTable,
)
from .vlans import LibreNMSVLANTable
from .VM_status import VMStatusTable

__all__ = [
    "DeviceStatusTable",
    "DeviceTypeMappingTable",
    "InterfaceTypeMappingTable",
    "InventoryIgnoreRuleTable",
    "IPAddressTable",
    "LibreNMSCableTable",
    "LibreNMSInterfaceTable",
    "LibreNMSVLANTable",
    "LibreNMSVMInterfaceTable",
    "ModuleBayMappingTable",
    "ModuleTypeMappingTable",
    "NormalizationRuleTable",
    "PlatformMappingTable",
    "SiteLocationSyncTable",
    "VCInterfaceTable",
    "VMStatusTable",
]
