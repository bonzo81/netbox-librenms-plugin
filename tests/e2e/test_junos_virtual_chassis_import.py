"""Import a two-member Junos Virtual Chassis through the UI and sync its member serials."""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import RECORDINGS_DIR, NetBoxAPI, recorded

RECORDING = "juniper-ex4400-vc-2member"
DEVICE_ID = 1002
DEVICE = recorded(RECORDING, f"/api/v0/devices/{DEVICE_ID}")["devices"][0]
HOSTNAME = DEVICE["hostname"]
(ROOT_SERIAL,) = (
    row["entPhysicalSerialNum"]
    for row in recorded(RECORDING, f"/api/v0/inventory/{DEVICE_ID}?entPhysicalContainedIn=0")["inventory"]
)
EXPECTED = json.loads((RECORDINGS_DIR / f"{RECORDING}.json").read_text())["expected"]["virtual_chassis"]
# The FPC serials in member order, keyed by the member number that Junos reports.
SERIAL_BY_POSITION = dict(zip(EXPECTED["member_positions"], EXPECTED["member_serials"], strict=True))
IMPORT_PATH = "/plugins/librenms_plugin/librenms-import/"


def _remove_imported_chassis(api: NetBoxAPI) -> None:
    """Delete the chassis and the member devices that an earlier import of the recording created."""
    for chassis in api.list("dcim/virtual-chassis", name=HOSTNAME):
        api.delete(f"dcim/virtual-chassis/{chassis['id']}")
    for device in api.list("dcim/devices", name__isw=HOSTNAME):
        api.delete(f"dcim/devices/{device['id']}")


@pytest.fixture
def import_placement(netbox_api: NetBoxAPI, placement: dict) -> dict:
    """Seed the device type that the import matches, and remove earlier imports."""
    manufacturer = netbox_api.get_or_create(
        "dcim/manufacturers", {"slug": "juniper"}, {"name": "Juniper", "slug": "juniper"}
    )
    netbox_api.get_or_create(
        "dcim/device-types",
        {"model": DEVICE["hardware"]},
        {"manufacturer": manufacturer["id"], "model": DEVICE["hardware"], "slug": DEVICE["hardware"].lower()},
    )
    _remove_imported_chassis(netbox_api)
    yield placement
    _remove_imported_chassis(netbox_api)


def _choose(page: Page, combobox, option: str) -> None:
    """Pick an option in a TomSelect combobox."""
    combobox.click()
    listbox = page.locator(f"#{combobox.get_attribute('aria-controls')}")
    listbox.get_by_role("option", name=option, exact=True).click()


def _search_with_vc_detection(page: Page, netbox_url: str):
    """Search the stub for the recorded device with Virtual Chassis detection on, and return its row."""
    page.goto(f"{netbox_url}{IMPORT_PATH}")
    page.get_by_label("LibreNMS Hostname").fill(HOSTNAME)
    page.get_by_label("Include Virtual Chassis Detection").check()
    page.get_by_label("Clear cache before search").check()
    page.get_by_role("button", name="Apply Filters").click()
    row = page.locator(f"#device-row-{DEVICE_ID}")
    # The search runs as a background job on the worker.
    expect(row).to_be_visible(timeout=120_000)
    return row


def _check_detected_members(page: Page, row) -> None:
    """Open the detection details from the row badge, and check both members and the master."""
    badge = row.get_by_role("button", name=f"Stack, {EXPECTED['member_count']}")
    expect(badge).to_be_visible()
    badge.click()
    modal = page.locator("#htmx-modal-content")
    expect(modal).to_contain_text(f"{EXPECTED['member_count']}-member stack")
    members = modal.locator("li")
    expect(members).to_have_count(EXPECTED["member_count"])
    for item, (position, serial) in zip(members.all(), sorted(SERIAL_BY_POSITION.items()), strict=True):
        expect(item).to_contain_text(f"Pos {position}")
        expect(item).to_contain_text(serial)
    expect(members.filter(has_text="Master")).to_have_text(re.compile(rf"Pos {EXPECTED['master_position']}\b"))
    modal.get_by_role("button", name="Close").last.click()
    expect(modal).to_be_hidden()


def _import_through_confirm_step(page: Page, row, role_name: str, netbox_api: NetBoxAPI) -> None:
    """Pick the role, confirm the import with its chassis members, and wait for the import job."""
    _choose(page, page.locator(f"#role_{DEVICE_ID}-ts-control"), role_name)
    import_button = row.locator(".device-import-btn.device-ready")
    expect(import_button).to_have_attribute("data-vc-member-count", str(EXPECTED["member_count"]))
    import_button.click()

    modal = page.locator("#htmx-modal-content")
    expect(modal.get_by_role("heading", name="Confirm Import")).to_be_visible()
    modal.get_by_text(f"VC · {EXPECTED['member_count']}").click()
    members = modal.locator(f"#bulk-vc-{DEVICE_ID} li")
    expect(members).to_have_count(EXPECTED["member_count"])
    for serial in SERIAL_BY_POSITION.values():
        expect(members.filter(has_text=serial)).to_have_count(1)
    modal.get_by_role("button", name="Import 1 device").click()

    job_link = page.get_by_role("link", name="Jobs interface")
    expect(job_link).to_be_visible()
    job_id = int(re.search(r"/core/jobs/(\d+)/", job_link.get_attribute("href")).group(1))
    job = netbox_api.wait_for_job(job_id)
    assert job["status"]["value"] == "completed", job
    assert job["data"]["failed_count"] == 0, job["data"]
    assert job["data"]["success_count"] == 1, job["data"]


def _imported_members(netbox_api: NetBoxAPI) -> tuple[dict, list[dict]]:
    """Return the one imported chassis and its members in position order."""
    chassis = netbox_api.list("dcim/virtual-chassis", name=HOSTNAME)
    assert len(chassis) == 1, chassis
    members = netbox_api.list("dcim/devices", virtual_chassis_id=chassis[0]["id"], ordering="vc_position")
    return chassis[0], members


def test_junos_virtual_chassis_imports_both_members_and_syncs_their_serials(
    logged_in_page: Page, netbox_url: str, netbox_api: NetBoxAPI, import_placement: dict
):
    page = logged_in_page
    row = _search_with_vc_detection(page, netbox_url)
    _check_detected_members(page, row)
    _import_through_confirm_step(page, row, import_placement["role"]["name"], netbox_api)

    chassis, members = _imported_members(netbox_api)
    assert [member["vc_position"] for member in members] == sorted(SERIAL_BY_POSITION)
    assert {member["vc_position"]: member["serial"] for member in members} == SERIAL_BY_POSITION
    master = next(member for member in members if member["id"] == chassis["master"]["id"])
    assert master["vc_position"] == EXPECTED["master_position"]
    assert master["serial"] == ROOT_SERIAL

    page.goto(f"{netbox_url}/dcim/virtual-chassis/{chassis['id']}/")
    for member in members:
        expect(page.get_by_role("link", name=member["name"], exact=True).first).to_be_visible()

    # A member without its serial gives Sync All a write to make.
    other = next(member for member in members if member["id"] != master["id"])
    netbox_api.update(f"dcim/devices/{other['id']}", {"serial": ""})

    page.goto(f"{netbox_url}/dcim/devices/{master['id']}/librenms-sync/")
    page.get_by_role("button", name=f"VC Serials ({EXPECTED['member_count']})").click()
    modal = page.locator("#vc-serials-modal")
    expect(modal).to_be_visible()
    rows = modal.locator("tbody tr")
    expect(rows).to_have_count(EXPECTED["member_count"])
    for serial in SERIAL_BY_POSITION.values():
        expect(rows.filter(has_text=serial)).to_have_count(1)
    expect(rows.filter(has_text=master["serial"])).to_contain_text(master["name"])
    unassigned = rows.filter(has_text=SERIAL_BY_POSITION[other["vc_position"]])
    _choose(page, unassigned.locator("input[role=combobox]"), other["name"])
    modal.get_by_role("button", name="Sync All").click()
    success = page.get_by_role("alert").filter(
        has_text=f"Successfully assigned {EXPECTED['member_count']} serial number(s)"
    )
    expect(success).to_be_visible()

    _, members = _imported_members(netbox_api)
    assert {member["vc_position"]: member["serial"] for member in members} == SERIAL_BY_POSITION
