"""VC detection when the LibreNMS device endpoint faults (see test_import_utils.py)."""

import pytest

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

DEVICE_ID = 9901
SERVER_KEY = "vc_device_fault"


def _chassis(index, serial, position):
    """Build one ENTITY-MIB chassis row."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "chassis",
        "entPhysicalSerialNum": serial,
        "entPhysicalModelName": "C9300-48U",
        "entPhysicalName": f"Switch {index}",
        "entPhysicalDescr": f"Chassis {index}",
        "entPhysicalParentRelPos": position,
    }


def _stack_root(index=1):
    """Build the StackWise-style root entry the detection walks from."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "stack",
        "entPhysicalSerialNum": "",
        "entPhysicalModelName": "",
        "entPhysicalName": "StackSub-0/0",
        "entPhysicalDescr": "Stack",
        "entPhysicalContainedIn": 0,
    }


@pytest.mark.django_db
def test_a_faulting_device_endpoint_still_detects_the_stack(settings, librenms_server):
    """A device-endpoint fault must not hide the members the inventory endpoint returned."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_librenms_servers(
        settings,
        {
            SERVER_KEY: {
                "librenms_url": librenms_server.url,
                "api_token": "token",
                "cache_timeout": 300,
                "verify_ssl": False,
            }
        },
    )
    api = LibreNMSAPI(server_key=SERVER_KEY)
    # A non-404 fault answers with a LibreNMSLookupError payload. That dataclass has no
    # __bool__, so it is always truthy and a guard on the payload alone let it through.
    librenms_server.register(f"/api/v0/devices/{DEVICE_ID}", {"status": "error"}, status=500)
    librenms_server.vc_inventory_callable(
        DEVICE_ID,
        [_stack_root()],
        {1: [_chassis(101, "SN-A", 1), _chassis(102, "SN-B", 2)]},
    )

    result = detect_virtual_chassis_from_inventory(api, DEVICE_ID)

    assert result is not None
    assert result["is_stack"] is True
    assert result["member_count"] == 2
    assert [member["serial"] for member in result["members"]] == ["SN-A", "SN-B"]
    # Without a device name or serial the naming and master detection degrade, but the
    # members are still offered.
    assert [member["suggested_name"] for member in result["members"]] == ["Member-1", "Member-2"]
    assert [member["is_master"] for member in result["members"]] == [False, False]
