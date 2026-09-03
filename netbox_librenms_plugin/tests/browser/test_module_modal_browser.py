"""Browser-level checks for the module tab: the mismatch modal and two racing row actions."""

from test_sync_cache_browser import _add_page_scripts


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
    '<h5 id="htmx-modal-label" class="modal-title">Module Mismatch</h5>'
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
