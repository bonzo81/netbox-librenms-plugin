"""Page helpers for the module tab of the LibreNMS sync page."""

from playwright.sync_api import Page, Request, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .conftest import SERVER_KEY

TABLE = "#librenms-module-table"


def row_selector(item: str | int) -> str:
    """Return the CSS selector of one inventory item's table row, by its name or its entPhysicalIndex."""
    if isinstance(item, int):
        return f"{TABLE} tbody tr[data-ent-index='{item}']"
    return f"{TABLE} tbody tr:has(td[data-col=name]:text-is('{item}'))"


def row(page: Page, name: str | int):
    """Return the table row locator of one inventory item."""
    return page.locator(row_selector(name))


def toasts(page: Page):
    """Return the toast container locator; every sync-tab partial repeats that id."""
    return page.locator("#django-messages").first


def dom_click(page: Page, selector: str) -> None:
    """Click through the DOM, because NetBox's fixed toast container can cover a control."""
    page.eval_on_selector(selector, "element => element.click()")


def forms_bound(page: Page, selector: str = TABLE) -> bool:
    """Report whether every hx-post form under a selector carries its HTMX binding."""
    return page.eval_on_selector_all(
        f"{selector} form[hx-post]",
        "forms => forms.length > 0 && forms.every(form => !!form['htmx-internal-data'])",
    )


def open_modules_tab(page: Page, netbox_url: str, device_pk: int) -> None:
    """Open the module sync tab and mark the page so a reload is detectable."""
    page.goto(f"{netbox_url}/dcim/devices/{device_pk}/librenms-sync/?tab=modules&server_key={SERVER_KEY}")
    expect(page.get_by_role("button", name="Refresh Modules")).to_be_visible()
    page.evaluate("window.__marker = 1")


def refresh_modules(page: Page, first_row: str | int) -> Request:
    """Click Refresh Modules, wait for the restored table, and return the cache-fragment request."""
    # The status check restores the tab through the loader, so wait for that response:
    # it is the swap the next action has to act on.
    with page.expect_response(
        lambda response: "/sync-cache-fragment/modules/" in response.url, timeout=60_000
    ) as fragment:
        dom_click(page, 'button:has-text("Refresh Modules")')
    expect(row(page, first_row)).to_have_count(1, timeout=60_000)
    return fragment.value.request


def mark_table(page: Page) -> None:
    """Flag the current table node so the next swap is observable."""
    page.eval_on_selector(TABLE, "table => table.dataset.e2eStale = '1'")


def wait_for_table_swap(page: Page) -> None:
    """Wait until a table without the stale flag replaced the flagged one."""
    try:
        page.wait_for_selector(f"{TABLE}:not([data-e2e-stale])", timeout=60_000)
    except PlaywrightTimeoutError as error:
        pane = page.eval_on_selector(
            "#module-sync-content", "pane => pane.innerText.replace(/\\s+/g, ' ').slice(0, 200)"
        )
        raise AssertionError(f"the module tab was not swapped in place. It now reads: {pane}") from error


def assert_no_navigation(page: Page, url_before: str) -> None:
    """Assert the page never reloaded: the marker survives and the URL is unchanged."""
    assert page.evaluate("window.__marker") == 1, "page reloaded: the in-page marker is gone"
    assert page.url == url_before, f"page navigated: {page.url} != {url_before}"


def run_row_action(page: Page, name: str | int, action: str, path: str) -> Request:
    """Submit one row's action form (install or install-branch) and wait for the in-place swap."""
    mark_table(page)
    with page.expect_request(lambda request: path in request.url and request.method == "POST") as posted:
        dom_click(page, f"{row_selector(name)} button[data-action={action}]")
    wait_for_table_swap(page)
    return posted.value


def install_row(page: Page, name: str | int) -> Request:
    """Install one row's module and wait for the in-place swap."""
    return run_row_action(page, name, "install", "/install-module/")


def install_branch(page: Page, name: str | int) -> Request:
    """Install one row's module with its installable children and wait for the in-place swap."""
    return run_row_action(page, name, "install-branch", "/install-branch/")
