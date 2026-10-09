"""
Add a device type mapping from the import validation modal.

The stub's IOS-XE recording reports a hardware string that no device type matches. After a
mapping is added from the modal, the modal stays open and shows the match, and the import
row behind it enables its Import button without a page reload.
"""

from uuid import uuid4

import pytest
from playwright.sync_api import expect

from .conftest import MAPPINGS_API, NetBoxAPI, recorded

DEVICE_ID = 45
DEVICE = recorded("iosxe-subinterfaces", f"/api/v0/devices/{DEVICE_ID}")["devices"][0]
IMPORT_PATH = "/plugins/librenms_plugin/librenms-import/"


def _mappings(api: NetBoxAPI) -> list[dict]:
    """Return the mappings of the recorded hardware. The plugin stores the hardware in lower case."""
    mappings = api.list(f"{MAPPINGS_API}/device-type-mappings")
    return [mapping for mapping in mappings if mapping["librenms_hardware"].lower() == DEVICE["hardware"].lower()]


def _remove_mappings(api: NetBoxAPI) -> None:
    for mapping in _mappings(api):
        api.delete(f"{MAPPINGS_API}/device-type-mappings/{mapping['id']}")


@pytest.fixture
def mapping_target(netbox_api: NetBoxAPI, placement: dict):
    """Create the device type that the test maps the hardware to, with no mapping for that hardware."""
    run = uuid4().hex[:12]
    manufacturer = netbox_api.create("dcim/manufacturers", {"name": f"E2E Mapping {run}", "slug": f"e2e-mapping-{run}"})
    device_type = netbox_api.create(
        "dcim/device-types",
        {"manufacturer": manufacturer["id"], "model": f"E2E-MAPPED-{run}", "slug": f"e2e-mapped-{run}"},
    )
    _remove_mappings(netbox_api)
    yield {"device_type": device_type, "manufacturer": manufacturer}
    _remove_mappings(netbox_api)
    netbox_api.delete(f"dcim/device-types/{device_type['id']}")
    netbox_api.delete(f"dcim/manufacturers/{manufacturer['id']}")


def test_mapping_updates_modal_and_row(logged_in_page, netbox_url, netbox_api, placement, mapping_target):
    page = logged_in_page
    page.goto(f"{netbox_url}{IMPORT_PATH}")
    page.get_by_label("LibreNMS Hostname").fill(DEVICE["hostname"])
    page.get_by_label("Clear cache before search").check()
    page.get_by_role("button", name="Apply Filters").click()
    row = page.locator(f"#device-row-{DEVICE_ID}")
    expect(row).to_be_visible(timeout=120_000)

    # A role is the other required field, so the mapping is all that the import still needs.
    role = page.locator(f"#role_{DEVICE_ID}-ts-control")
    role.click()
    page.locator(f"#{role.get_attribute('aria-controls')}").get_by_role(
        "option", name=placement["role"]["name"], exact=True
    ).click()
    expect(row.locator(".device-import-btn.device-ready")).to_have_count(0)

    row.get_by_role("button", name="Details").click()
    modal = page.locator("#htmx-modal-content")
    expect(modal).to_contain_text("No matching type")

    device_type = mapping_target["device_type"]
    modal.get_by_label("Search device types").fill(device_type["model"])
    modal.locator(f"#dt-dropdown-{DEVICE_ID} a", has_text=device_type["model"]).click()
    expect(modal.locator("input[name=device_type_id]")).to_have_value(str(device_type["id"]))
    modal.get_by_role("button", name="Add Mapping").click()

    expect(modal).not_to_contain_text("No matching type")
    expect(modal).to_be_visible()
    expect(row.locator(".device-import-btn.device-ready")).to_be_enabled()
    (mapping,) = _mappings(netbox_api)
    assert mapping["netbox_device_type"] == device_type["id"]
