"""
Module install and branch install on the device sync page.

The device is linked to the stub's Nokia SR OS recording. Its first line card holds an MDA, so
the seeded types give the card a bay in the device and the MDA a bay in the card's module type:
the MDA can only go in once the card is installed.
"""

import pytest
from playwright.sync_api import expect

from . import module_tab as tab
from .conftest import recorded

LIBRENMS_ID = 5
INVENTORY = recorded("nokia-timos-transceivers", f"/api/v0/inventory/{LIBRENMS_ID}/all")["inventory"]
CARD = next(item for item in INVENTORY if item["entPhysicalClass"] == "ioModule" and item["entPhysicalModelName"])
MDA = next(
    item
    for item in INVENTORY
    if item["entPhysicalClass"] == "mdaModule" and item["entPhysicalContainedIn"] == CARD["entPhysicalIndex"]
)
CARD_ROW = CARD["entPhysicalIndex"]
MDA_ROW = MDA["entPhysicalIndex"]
INSTALLED_BRANCH = {
    CARD["entPhysicalName"]: (CARD["entPhysicalModelName"], CARD["entPhysicalSerialNum"]),
    MDA["entPhysicalName"]: (MDA["entPhysicalModelName"], MDA["entPhysicalSerialNum"]),
}


@pytest.fixture(scope="module")
def seeded_device(module_device) -> dict:
    """Create the device with a bay for the card, and the card type with a bay for the MDA."""
    return module_device(
        LIBRENMS_ID,
        [CARD["entPhysicalName"]],
        {CARD["entPhysicalModelName"]: [MDA["entPhysicalName"]], MDA["entPhysicalModelName"]: []},
    )


@pytest.fixture
def modules_page(logged_in_page, netbox_url, netbox_api, seeded_device):
    """Leave the device with no modules, and open its module tab with fresh inventory."""
    netbox_api.delete_modules(seeded_device["id"])
    tab.open_modules_tab(logged_in_page, netbox_url, seeded_device["id"])
    tab.refresh_modules(logged_in_page, CARD_ROW)
    return logged_in_page


def test_clean_state_shows_the_card_matched_and_its_mda_without_a_bay(modules_page):
    """With no modules installed, the card is Matched and the MDA has no bay to go in yet."""
    page = modules_page
    expect(tab.row(page, CARD_ROW)).to_have_attribute("data-status", "Matched")
    mda = tab.row(page, MDA_ROW)
    expect(mda).to_have_attribute("data-status", "No Bay")
    expect(mda.locator("td[data-col=module_bay]")).to_contain_text("No matching bay")
    expect(tab.row(page, CARD_ROW).locator("button[data-action=install-branch]")).to_have_count(1)


def test_single_install_installs_the_card_and_gives_the_mda_its_bay(modules_page, netbox_api, seeded_device):
    """Installing the card alone creates one module, and its bay becomes the MDA's bay."""
    page = modules_page
    tab.install_row(page, CARD_ROW)

    expect(tab.row(page, CARD_ROW)).to_have_attribute("data-status", "Installed")
    expect(tab.row(page, MDA_ROW)).to_have_attribute("data-status", "Matched")
    expect(tab.row(page, MDA_ROW).locator("td[data-col=module_bay]")).to_contain_text(MDA["entPhysicalName"])
    card = CARD["entPhysicalName"]
    assert netbox_api.installed_modules(seeded_device["id"]) == {card: INSTALLED_BRANCH[card]}


def test_branch_install_creates_the_card_and_its_mda(modules_page, netbox_api, seeded_device):
    """Install Branch creates the parent module and the child module in the parent's bay."""
    page = modules_page
    tab.install_branch(page, CARD_ROW)

    expect(tab.row(page, CARD_ROW)).to_have_attribute("data-status", "Installed")
    expect(tab.row(page, MDA_ROW)).to_have_attribute("data-status", "Installed")
    assert netbox_api.installed_modules(seeded_device["id"]) == INSTALLED_BRANCH
    modules = {
        module["module_bay"]["name"]: module
        for module in netbox_api.list("dcim/modules", device_id=seeded_device["id"])
    }
    mda_bay = netbox_api.get(f"dcim/module-bays/{modules[MDA['entPhysicalName']]['module_bay']['id']}")
    assert mda_bay["module"]["id"] == modules[CARD["entPhysicalName"]]["id"], "the MDA is not in a bay of the card"


def test_branch_install_over_an_installed_card_installs_only_the_mda(modules_page, netbox_api, seeded_device):
    """A branch install whose parent bay is already occupied installs the rest without an error."""
    page = modules_page
    tab.install_row(page, CARD_ROW)
    expect(tab.row(page, CARD_ROW).locator("button[data-action=install-branch]")).to_have_count(1)

    tab.install_branch(page, CARD_ROW)

    expect(page.locator("#module-sync-content")).not_to_contain_text("Branch install failed")
    expect(tab.row(page, MDA_ROW)).to_have_attribute("data-status", "Installed")
    assert netbox_api.installed_modules(seeded_device["id"]) == INSTALLED_BRANCH


def test_full_workflow_leaves_no_installable_row(modules_page, netbox_api, seeded_device):
    """Install every Matched top-level row, with its branch when it has one, until none is left."""
    page = modules_page
    table = page.locator(tab.TABLE)
    for _ in range(len(INVENTORY)):
        matched = table.locator("tbody tr[data-status=Matched][data-depth='0']")
        if matched.count() == 0:
            break
        index = int(matched.first.get_attribute("data-ent-index"))
        if tab.row(page, index).locator("button[data-action=install-branch]").count():
            tab.install_branch(page, index)
        else:
            tab.install_row(page, index)

    expect(table.locator("tbody tr[data-status=Matched]")).to_have_count(0)
    assert netbox_api.installed_modules(seeded_device["id"]) == INSTALLED_BRANCH
