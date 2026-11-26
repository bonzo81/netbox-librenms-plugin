"""Browser-level checks for the module tab, its mismatch modal and the VC serials dialog."""

import pytest
from test_sync_cache_browser import NETBOX_TABLE_CSS, _add_page_scripts


PREVIEW_URL = "https://plugin.example.com/devices/1/module-mismatch-preview/"
PREVIEW_QUERY = "module_id=55&ent_index=200&server_key=production&selected_device_id=1"
REPLACE_URL = "https://plugin.example.com/devices/1/replace-module/"
INSTALL_URL = "https://plugin.example.com/devices/1/install-module/"
SERIAL_URL = "https://plugin.example.com/devices/1/update-module-serial/"

# The attributes LibreNMSModuleTable.render_actions emits on the Replace button. The server side is
# pinned by test_render_actions_can_replace_renders_replace_button.
REPLACE_BUTTON = (
    '<button type="button" id="replace-btn" class="btn btn-sm btn-danger ms-1"'
    f' hx-get="{PREVIEW_URL}?{PREVIEW_QUERY}"'
    ' hx-target="#htmx-modal-content" hx-swap="innerHTML"'
    ' hx-sync="#htmx-modal-content:replace" hx-disabled-elt="this"'
    ' title="Replace module">Replace</button>'
)

# What ModuleMismatchPreviewView answers now: a whole modal content, not just a body.
PREVIEW_FRAGMENT = (
    '<div class="modal-header">'
    '<h5 class="modal-title">Module Mismatch</h5>'
    '<button type="button" class="btn-close" onclick="closeHtmxModal()" aria-label="Close"></button>'
    "</div>"
    '<div class="modal-body">'
    f'<form method="post" action="{REPLACE_URL}" hx-post="{REPLACE_URL}"'
    ' hx-target="#module-sync-content" hx-swap="innerHTML"'
    ' hx-sync="#module-sync-content:drop" hx-disabled-elt="find button[type=submit]">'
    '<button type="submit" id="modal-replace">Replace Module</button>'
    "</form></div>"
)


def _row_form(url, button_id):
    """Render one module row action form the way LibreNMSModuleTable.render_actions does."""
    return (
        f'<form method="post" action="{url}" hx-post="{url}"'
        ' hx-target="#module-sync-content" hx-swap="innerHTML"'
        ' hx-sync="#module-sync-content:drop"'
        ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
        f'<button type="submit" id="{button_id}">Go</button></form>'
    )


def _module_page_html(content):
    """Serve the module tab with the shared HTMX modal, both starting hidden like the sync page."""
    return f"""<html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
        <div id="module-sync-content">{content}</div>
        <div class="modal fade" id="htmx-modal" tabindex="-1" aria-labelledby="htmx-modal-label"
             aria-hidden="true" style="display:none">
          <span id="htmx-modal-label" class="visually-hidden">Dialog</span>
          <div class="modal-dialog modal-lg">
            <div class="modal-content" id="htmx-modal-content">
              <div class="modal-body">Loading…</div>
            </div>
          </div>
        </div>
        <script>document.addEventListener('click', function () {{
            window.clickCount = (window.clickCount || 0) + 1;
        }});</script>
        </body></html>"""


def _collect_posts(page):
    """Record every POST the page issues, so a dropped request is observable."""
    posts = []

    def _record(request):
        if request.method == "POST":
            posts.append(request.url)

    page.on("request", _record)
    return posts


def test_the_replace_button_opens_a_modal_whose_forms_are_htmx_bound(page):
    """The preview carries hx- forms, so a fetch()+innerHTML load left them unbound and reloaded the page."""
    page.set_content(_module_page_html(REPLACE_BUTTON))
    page.route(f"{PREVIEW_URL}?*", lambda route: route.fulfill(body=PREVIEW_FRAGMENT))
    page.route(
        REPLACE_URL,
        lambda route: route.fulfill(
            body='<p id="swapped">done</p>',
            headers={
                "HX-Retarget": "#module-sync-content",
                "HX-Reswap": "innerHTML",
                "HX-Trigger": '{"closeModal": null}',
            },
        ),
    )
    _add_page_scripts(page)
    # NetBox imports HTMX as a module: with no htmx global, HTML the page inserts itself stays unbound.
    assert page.evaluate("typeof htmx") == "undefined"

    with page.expect_request(f"{PREVIEW_URL}?*") as preview:
        page.click("#replace-btn")
    page.wait_for_selector("#htmx-modal-content .modal-body")

    assert preview.value.headers.get("hx-request") == "true"
    # No Bootstrap in the harness, so showModal falls back to display + .show.
    assert page.locator("#htmx-modal").is_visible()
    assert "show" in page.get_attribute("#htmx-modal", "class")
    # The shell's hidden label takes the fragment's title, so the dialog is announced by name.
    assert page.evaluate("document.getElementById('htmx-modal-label').textContent.trim()") == "Module Mismatch"

    with page.expect_request(REPLACE_URL) as action:
        page.click("#modal-replace")
    page.wait_for_selector("#module-sync-content #swapped")

    assert action.value.headers.get("hx-request") == "true"
    assert page.locator("#module-sync-content #swapped").inner_text() == "done"
    # The action swaps outside the modal, so the modal closes on the response's closeModal trigger.
    assert page.locator("#htmx-modal").is_hidden()


def test_a_failed_preview_is_reported_inside_the_modal(page):
    """HTMX swaps no 4xx answer, so the refused preview would be silent without the error listener."""
    page.set_content(_module_page_html(REPLACE_BUTTON))
    page.route(
        f"{PREVIEW_URL}?*",
        lambda route: route.fulfill(status=400, body="No cached inventory data. Please refresh modules first."),
    )
    _add_page_scripts(page)

    with page.expect_request(f"{PREVIEW_URL}?*"):
        page.click("#replace-btn")
    page.wait_for_selector("#htmx-modal-content .alert-danger")

    assert page.locator("#htmx-modal").is_visible()
    assert "No cached inventory data" in page.locator("#htmx-modal-content .alert-danger").inner_text()


def test_a_preview_that_never_reaches_the_server_is_reported_too(page):
    """
    A transport-level failure fires htmx:sendError, not htmx:responseError.

    Without a listener for it the click produces no request, no modal and no message, which reads
    to the user as a dead button.
    """
    page.set_content(_module_page_html(REPLACE_BUTTON))
    page.route(f"{PREVIEW_URL}?*", lambda route: route.abort())
    _add_page_scripts(page)

    page.click("#replace-btn")
    page.wait_for_selector("#htmx-modal-content .alert-danger")

    assert page.locator("#htmx-modal").is_visible()
    # The button must not stay stuck in its in-flight disabled state either.
    assert page.locator("#replace-btn").is_enabled()


def test_the_error_modal_keeps_its_accessible_name_and_announces_the_alert(page):
    """The dialog is labelled by an element outside the content the error handler replaces."""
    page.set_content(_module_page_html(REPLACE_BUTTON))
    page.route(f"{PREVIEW_URL}?*", lambda route: route.fulfill(status=400, body="No cached inventory data."))
    _add_page_scripts(page)

    with page.expect_request(f"{PREVIEW_URL}?*"):
        page.click("#replace-btn")
    page.wait_for_selector("#htmx-modal-content .alert-danger")

    label_id = page.get_attribute("#htmx-modal", "aria-labelledby")
    assert page.evaluate(f"document.getElementById({label_id!r}).textContent.trim()") == "Request failed"
    assert page.get_attribute("#htmx-modal-content .alert-danger", "role") == "alert"


def test_a_second_row_action_is_dropped_while_the_first_is_in_flight(page):
    """A second action's response could not retarget from its detached form, so its write never showed."""
    page.set_content(_module_page_html(_row_form(INSTALL_URL, "action-a") + _row_form(SERIAL_URL, "action-b")))
    pending = []
    page.route(INSTALL_URL, lambda route: pending.append(route))
    page.route(SERIAL_URL, lambda route: route.fulfill(body='<p id="swapped">B</p>'))
    _add_page_scripts(page)
    posts = _collect_posts(page)

    with page.expect_request(INSTALL_URL):
        page.click("#action-a")
    page.click("#action-b")
    page.wait_for_function("window.clickCount === 2")

    assert posts == [INSTALL_URL]

    pending[0].fulfill(body='<p id="swapped">A</p>')
    page.wait_for_selector("#module-sync-content #swapped")

    assert page.locator("#module-sync-content #swapped").inner_text() == "A"


def _mismatch_modal(**context):
    """Render the mismatch modal template with a hand-made context in the shape the preview view builds."""
    from pathlib import Path
    from types import SimpleNamespace

    from django.template import Context, Engine, Library

    engine = Engine()
    engine.template_libraries["helpers"] = Library()
    urls = Library()

    @urls.simple_tag(name="url")
    def url(name, **kwargs):
        return SERIAL_URL if name.endswith(":update_module_serial") else REPLACE_URL

    # The plugin package needs NetBox, so the serial text filter is a stub.
    @urls.filter(name="page_text")
    def page_text(value):
        return "" if value is None else str(value)

    engine.template_builtins.append(urls)
    engine.template_libraries["librenms_serial"] = Library()
    template = Path(__file__).parents[2] / "templates/netbox_librenms_plugin/htmx/module_mismatch_modal.html"
    base = {
        "installed_module": SimpleNamespace(pk=55, module_type=SimpleNamespace(model="Test Card")),
        "serial_mismatch": True,
        "type_mismatch": False,
        "librenms_serial": "TEST-SERIAL",
        "server_key": "production",
        "device_pk": 1,
        "ent_index": 200,
        "csrf_token": "test-token",
        # The view offers Replace only when the delete scope covers both removal trees.
        "replace_in_delete_scope": True,
        "replace_removal": {"count": 1, "names": ["Test Card"], "hidden": 0},
    }
    return engine.from_string(template.read_text()).render(Context({**base, **context}, use_l10n=False))


def test_mismatch_action_disables_competing_modal_submissions(page):
    """Every mutation button must show that the shared request scope is busy."""
    from playwright.sync_api import expect

    fragment = _mismatch_modal()
    page.set_content(_module_page_html(REPLACE_BUTTON))
    page.route(f"{PREVIEW_URL}?*", lambda route: route.fulfill(body=fragment))
    pending = []
    page.route(SERIAL_URL, lambda route: pending.append(route))
    _add_page_scripts(page)
    page.click("#replace-btn")
    buttons = page.locator("#htmx-modal-content button[type=submit]")
    expect(buttons).to_have_count(2)
    with page.expect_request(SERIAL_URL):
        buttons.first.click()
    expect(buttons.first).to_be_disabled()
    expect(buttons.last).to_be_disabled()
    pending[0].fulfill(body='<p id="serial-updated">Updated</p>')
    expect(page.locator("#serial-updated")).to_be_visible()
    expect(buttons.first).to_be_enabled()
    expect(buttons.last).to_be_enabled()


def test_an_unbroken_long_serial_stays_inside_the_mismatch_modal_on_a_narrow_screen(page):
    """A 51-character serial has no break point, so the serial cells must wrap it inside the dialog."""
    serials = ["A" * 51, "B" * 51]
    fragment = _mismatch_modal(installed_serial=serials[0], librenms_serial=serials[1])
    page.set_viewport_size({"width": 400, "height": 800})
    page.set_content(
        f"<style>{NETBOX_TABLE_CSS} .modal-dialog {{ margin: 0 16px; }}</style>"
        f'<div class="modal-dialog"><div class="modal-content">{fragment}</div></div>'
    )

    state = page.evaluate(
        """() => {
            const dialog = document.querySelector('.modal-dialog').getBoundingClientRect();
            const body = document.querySelector('.modal-body');
            const row = Array.from(document.querySelectorAll('.modal-body tr'))
                .find((tr) => tr.cells[0].textContent.trim() === 'Serial');
            return {
                scrollWidth: body.scrollWidth,
                clientWidth: body.clientWidth,
                dialogRight: dialog.right,
                cellTexts: Array.from(row.cells).slice(1).map((cell) => cell.textContent.trim()),
                cellOverflows: Array.from(row.cells).slice(1).map((cell) => cell.scrollWidth - cell.clientWidth),
                cellRights: Array.from(row.cells).slice(1).map((cell) => cell.getBoundingClientRect().right),
            };
        }"""
    )

    assert state["cellTexts"] == serials, state
    # A cell that clips its text with overflow: hidden stays inside the dialog, so check the text itself.
    assert all(overflow <= 0 for overflow in state["cellOverflows"]), state
    assert state["scrollWidth"] <= state["clientWidth"] + 1, state
    assert all(right <= state["dialogRight"] + 1 for right in state["cellRights"]), state


def _vc_serials_modal(items):
    """Render the VC serials dialog of librenms_sync_base.html with a hand-made context in the view's shape."""
    from pathlib import Path
    from types import SimpleNamespace

    from django.template import Context, Engine, Library

    templates = Path(__file__).parents[2] / "templates"
    engine = Engine(dirs=[str(templates)])
    urls = Library()

    @urls.simple_tag(name="url")
    def url(name, **kwargs):
        return "https://plugin.example.com/devices/1/assign-vc-serial/"

    # The plugin package needs NetBox, so the serial text filter is a stub.
    @urls.filter(name="page_text")
    def page_text(value):
        return "" if value is None else str(value)

    engine.template_builtins.append(urls)
    source = (templates / "netbox_librenms_plugin/librenms_sync_base.html").read_text()
    # The dialog is the body of one {% if %} block, so the slice holds balanced tags.
    start = source.index("<!-- VC Member Serials Modal -->")
    dialog = source[start : source.index("{% endif %}", source.index("</table>", start))]
    members = [SimpleNamespace(pk=pk, name=f"core-stack-building-a-m{pk}") for pk in (1, 2)]
    context = {
        "object": SimpleNamespace(pk=1),
        "vc_inventory_serials": items,
        "vc_serial_members": members,
        "csrf_token": "test-token",
    }
    return engine.from_string(dialog).render(Context(context, use_l10n=False))


# NetBox replaces each select with a Tom Select control, which keeps the selected name on one line.
TOM_SELECT_STAND_IN = """() => {
    for (const select of document.querySelectorAll('#vc-serials-form select')) {
        const control = document.createElement('div');
        control.className = 'ts-control';
        control.textContent = select.selectedOptions[0].textContent.trim();
        select.hidden = true;
        select.after(control);
    }
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_a_long_vc_member_serial_wraps_in_a_table_without_a_scroll_wrapper(page, width):
    """A scroll wrapper clips the member dropdown, so the 50-character serial must wrap in its cell instead."""
    from types import SimpleNamespace

    serial = "N" * 50
    items = [
        {"model": "EX4400-24X-S", "serial": serial, "librenms_serial": serial, "assigned_member": None},
        {
            "model": "EX4400-48MP",
            "serial": "SHORT-B",
            "librenms_serial": "SHORT-B",
            "assigned_member": None,
            "selected_member": SimpleNamespace(pk=1),
        },
    ]
    page.set_viewport_size({"width": width, "height": 800})
    page.set_content(
        f"<style>{NETBOX_TABLE_CSS} .modal-dialog {{ margin: 0 16px; max-width: 1140px; }}"
        " .ts-control { overflow: hidden; white-space: nowrap; padding: .25rem 1.75rem .25rem .5rem; }</style>"
        f"{_vc_serials_modal(items)}"
    )
    page.evaluate(TOM_SELECT_STAND_IN)

    state = page.evaluate(
        """() => {
            const table = document.querySelector('#vc-serials-form table');
            const body = table.closest('.modal-body');
            const cell = table.tBodies[0].rows[0].cells[1];
            return {
                wrapper: Boolean(table.closest('.table-responsive')),
                controls: Array.from(table.querySelectorAll('.ts-control')).map((control) => control.textContent),
                text: cell.textContent.trim(),
                overflow: cell.scrollWidth - cell.clientWidth,
                bodyOverflow: body.scrollWidth - body.clientWidth,
            };
        }"""
    )

    assert state["wrapper"] is False, state
    assert state["controls"] == ["Select member...", "core-stack-building-a-m1"], state
    assert state["text"] == serial, state
    assert state["overflow"] <= 0, state
    # A member column that keeps the selected name on one line must not push the table out of the dialog.
    assert state["bodyOverflow"] <= 1, state
