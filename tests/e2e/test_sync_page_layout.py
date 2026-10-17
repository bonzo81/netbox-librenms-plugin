"""The header of the LibreNMS Connections card on the device sync page fits the screen width."""

import pytest

from .conftest import SERVER_KEY

HEADER = "#librenms-connections > .card-header"


@pytest.fixture(scope="module")
def linked_device(module_device) -> dict:
    return module_device(1, [], {})


def _header_layout(page) -> dict:
    """Return the controls that leave the header box, and the vertical centre of each header item."""
    return page.eval_on_selector(
        HEADER,
        """header => {
            const box = header.getBoundingClientRect();
            const items = [header.firstElementChild, ...header.querySelectorAll('.btn')];
            const rects = items.map(item => item.getBoundingClientRect());
            return {
                outside: items.filter((item, i) => rects[i].left < box.left || rects[i].right > box.right)
                    .map(item => item.innerText.trim()),
                centres: rects.map(rect => Math.round(rect.top + rect.height / 2)),
            };
        }""",
    )


@pytest.mark.parametrize("width", [390, 1440])
def test_connections_header_keeps_its_controls_inside_the_card(logged_in_page, netbox_url, linked_device, width):
    """A narrow screen wraps the header controls, and a wide one keeps them in one row."""
    page = logged_in_page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{netbox_url}/dcim/devices/{linked_device['id']}/librenms-sync/?server_key={SERVER_KEY}")

    layout = _header_layout(page)

    assert layout["outside"] == []
    if width == 1440:
        assert max(layout["centres"]) - min(layout["centres"]) <= 2, layout
