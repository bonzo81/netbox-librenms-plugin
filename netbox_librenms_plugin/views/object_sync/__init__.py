"""Views backing the LibreNMS sync tabs on Device and VM detail pages."""

from .cache_status import SyncCacheFragmentView, SyncCacheStatusView  # noqa: F401
from .devices import (  # noqa: F401
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
)
from .vms import (  # noqa: F401
    VMInterfaceTableView,
    VMIPAddressTableView,
    VMLibreNMSSyncView,
)
