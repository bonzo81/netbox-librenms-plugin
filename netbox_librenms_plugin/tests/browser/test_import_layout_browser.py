"""Browser checks for the focused device-import table layout."""

import json
from pathlib import Path

import pytest


ASSET_ROOT = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin"


@pytest.mark.parametrize("viewport_width", [1280, 720])
def test_all_optional_columns_fit_the_import_results_card(page, viewport_width):
    """Every column choice must scale inside the results card."""
    page.set_viewport_size({"width": viewport_width, "height": 720})
    page.set_content(
        """
        <div id="device-import-results" style="width: calc(100% - 32px); max-width: 1000px">
          <span id="import-columns-count"></span>
          <input id="use-sysname-toggle-cb" type="checkbox" checked>
          <input id="strip-domain-toggle-cb" type="checkbox">
          <input class="import-column-toggle" type="checkbox" value="location" checked>
          <input class="import-column-toggle" type="checkbox" value="hardware" checked>
          <input class="import-column-toggle" type="checkbox" value="hostname" checked>
          <input class="import-column-toggle" type="checkbox" value="sysname" checked>
          <div class="device-import-table-wrapper">
            <table id="device-import-table">
              <thead><tr>
                <th class="w-1 import-select-column"></th>
                <th class="import-name-column">NetBox object</th>
                <th data-import-column="location">Location</th>
                <th data-import-column="hardware">Hardware</th>
                <th data-import-column="hostname">Hostname</th>
                <th data-import-column="sysname">System name</th>
                <th class="import-setup-column">Import setup</th>
                <th class="import-actions-column">Actions</th>
              </tr></thead>
              <tbody><tr>
                <td class="import-select-column"></td>
                <td class="import-name-column">edge-with-a-long-name.example.test</td>
                <td data-import-column="location">Long test location</td>
                <td data-import-column="hardware">Long hardware model name</td>
                <td data-import-column="hostname">edge-with-a-long-name.example.test</td>
                <td data-import-column="sysname">edge-with-a-long-system-name</td>
                <td class="import-setup-column"><select class="import-setup-select"><option>Required role</option></select></td>
                <td class="import-actions-column"><div class="btn-group">
                  <button class="btn">&#8595;<span class="device-import-action-label"> Import</span></button>
                  <button class="btn">i<span class="device-import-action-label"> Details</span></button>
                </div></td>
              </tr></tbody>
            </table>
          </div>
        </div>
        """
    )
    page.add_style_tag(
        content=(
            "#device-import-table th, #device-import-table td { padding: 0.5rem; }"
            ".btn-group { display: inline-flex; white-space: nowrap; }"
            ".btn { padding: 0.25rem 0.5rem; }"
        )
    )
    page.add_style_tag(path=ASSET_ROOT / "css" / "librenms_import.css")
    page.add_script_tag(path=ASSET_ROOT / "js" / "librenms_import.js")

    wrapper = page.locator(".device-import-table-wrapper")
    setup = page.locator("tbody .import-setup-column")
    actions = page.locator("tbody .import-actions-column")
    action_group = actions.locator(".btn-group")
    dimensions = wrapper.evaluate("element => ({ client: element.clientWidth, scroll: element.scrollWidth })")

    assert dimensions["scroll"] <= dimensions["client"]
    assert setup.evaluate("element => element.getBoundingClientRect().width") <= dimensions["client"] * 0.24
    action_box = actions.bounding_box()
    group_box = action_group.bounding_box()
    assert group_box["x"] + group_box["width"] <= action_box["x"] + action_box["width"]


@pytest.mark.parametrize(
    ("sysname", "expected_name", "expected_source"),
    [
        ("999.1.2.3", "999", "From sysName, domain removed"),
        (".example.test", "device-42", "From fallback name, domain removed"),
    ],
)
def test_import_name_preview_matches_backend_resolution(page, sysname, expected_name, expected_source):
    """The live preview must use the same resolved names as the importer."""
    variants = {
        "sysname_full": {"name": sysname, "source": "sysName"},
        "sysname_stripped": {
            "name": expected_name,
            "source": expected_source.removeprefix("From ").removesuffix(", domain removed"),
        },
    }
    page.set_content(
        f"""
        <input id="use-sysname-toggle-cb" type="checkbox" checked>
        <input id="strip-domain-toggle-cb" type="checkbox" checked>
        <table><tbody><tr data-device-id="42">
          <td>
            <strong data-import-name data-import-name-variants='{json.dumps(variants)}'>
              server-rendered-name
            </strong>
            <div data-import-name-source>server-rendered-source</div>
          </td>
        </tr></tbody></table>
        """
    )
    page.add_script_tag(path=ASSET_ROOT / "js" / "librenms_import.js")

    assert page.locator("[data-import-name]").text_content() == expected_name
    assert page.locator("[data-import-name-source]").text_content() == expected_source
