"""Render the tab templates and check their toolbar geometry and cache lifetime."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import expect

from test_sync_cache_browser import STYLE_PATH, TEMPLATE_DIR, _add_page_scripts
from test_sync_page_layout_browser import NETBOX_PAGE_CSS


TABS = [
    ("interface", "interface_sync"),
    ("cable", "cable_sync"),
    ("ipaddress", "ip_sync"),
    ("vlan", "vlan_sync"),
    ("module", "module_sync"),
]
NOW = datetime(2026, 10, 25, 10, tzinfo=UTC)
CSS = (
    NETBOX_PAGE_CSS
    + """
:root { --tblr-success: #2fb344; --tblr-warning: #f59f00; --tblr-danger: #d63939; }
body { padding: 16px; }
.justify-content-between { justify-content: space-between; }
.justify-content-end { justify-content: flex-end; }
.align-items-end { align-items: flex-end; }
.gap-2 { gap: .5rem; }
.ms-auto { margin-left: auto; }
.m-2 { margin: .5rem !important; }
.mb-3 { margin-bottom: 1rem; }
.btn, input, select { font: inherit; height: 30px; }
.btn { align-items: center; border: 1px solid; border-radius: 4px; text-decoration: none; }
.form-label { display: block; }
.modal.show { display: block !important; }
.dropdown-menu, .collapse:not(.show) { display: none; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }
"""
)


def render_tab(
    tab, key, *, model="device", cached=True, migrated=False, partial=False, timeout=600, server_key="secondary"
):
    """Use real templates with only NetBox tags and table rendering replaced."""
    from django.conf import settings
    from django.template import Context, Engine, Library

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    tags = Library()
    tags.simple_tag(lambda name, *args, **kwargs: f"/endpoint/{name.rsplit(':', 1)[-1]}/", name="url")
    tags.filter("meta", lambda obj, attr: model)
    query = Library()
    query.simple_tag(lambda path="", **kwargs: path or "/sync/", name="sync_tab_url")
    query.simple_tag(
        lambda **kwargs: [
            ("server_key", "secondary"),
            ("tab", {"interface": "interfaces", "ipaddress": "ipaddresses"}.get(tab, tab + "s")),
        ],
        name="sync_tab_query_items",
    )
    query.simple_tag(lambda server_key: timeout, name="sync_cache_timeout")
    fixtures = {
        "inc/table.html": '<table id="test-rows"><tbody><tr><td>Ethernet1</td></tr></tbody></table>',
        "inc/messages.html": "",
        "netbox_librenms_plugin/inc/paginator.html": "",
    }
    engine = Engine(
        dirs=[str(TEMPLATE_DIR.parent)],
        loaders=[("django.template.loaders.locmem.Loader", fixtures), "django.template.loaders.filesystem.Loader"],
    )
    engine.template_builtins.append(tags)
    engine.template_libraries.update({"helpers": tags, "static": Library(), "librenms_sync_query": query})

    def field(name, choices=()):
        return SimpleNamespace(
            auto_id=f"id_{tab}_{name}",
            html_name=f"{tab}_{name}",
            label=name,
            value="",
            errors=[],
            field=SimpleNamespace(choices=choices),
        )

    filters = SimpleNamespace(
        is_valid=True,
        in_use=True,
        active=True,
        form=SimpleNamespace(
            q=field("q"),
            sync_state=field("sync_state", [("", "All sync states"), ("new", "New")]),
            errors={},
            column_fields=[],
            columns_have_input=False,
        ),
    )
    subject = SimpleNamespace(pk=1, virtual_chassis=None)
    table = SimpleNamespace(rows=[1]) if cached else None
    data = {
        "object": subject,
        "table": table,
        "vlan_table": table,
        "server_key": server_key,
        "filters": filters,
        "cache_expiry": NOW + timedelta(seconds=timeout) if cached else None,
        "install_selected_inventory_binding": "binding",
        "relationship_diagnostics": {"pairs_seen": 2, "pairs_usable": 2},
    }
    context = {
        "object": subject,
        key: data,
        "has_librenms_id": True,
        "sync_partial_response": partial,
        "has_write_permission": True,
        "server_key": server_key,
        "csrf_token": "token",
        "migrated_to_marker": migrated,
        "request": SimpleNamespace(htmx=partial),
    }
    name = f"_{tab}_sync{'_content' if partial else ''}.html"
    return engine.get_template(f"netbox_librenms_plugin/{name}").render(Context(context, use_l10n=False, use_tz=False))


def load_tab(page, tab, key, **kwargs):
    page.clock.install(time=NOW)
    page.set_content(
        f"<style>{CSS}</style><div class='tab-pane' data-tab-id='{tab}s'>{render_tab(tab, key, **kwargs)}</div>"
    )
    page.add_style_tag(path=str(STYLE_PATH))


@pytest.mark.parametrize(("tab", "key"), TABS)
def test_refresh_and_filters_share_the_primary_toolbar_row(page, tab, key):
    load_tab(page, tab, key)
    page.set_viewport_size({"width": 1440, "height": 900})
    toolbar = page.locator(".sync-tab-primary-toolbar")
    refresh = page.locator("button[hx-post].btn-outline-primary")
    assert toolbar.locator("button[hx-post].btn-outline-primary").count() == 1
    sync = toolbar.locator("button[type=submit]").first.bounding_box()
    box = refresh.bounding_box()
    assert box["y"] == pytest.approx(sync["y"], abs=1)
    if tab != "module":
        search = toolbar.locator("input[type=search]").bounding_box()
        assert search["y"] == pytest.approx(box["y"], abs=1)


def test_relationship_diagnostics_follow_the_primary_toolbar(page):
    load_tab(page, "interface", "interface_sync")
    toolbar = page.locator(".sync-tab-primary-toolbar").bounding_box()
    diagnostics = page.locator(".sync-tab-lead-row").bounding_box()
    assert diagnostics is not None
    assert diagnostics["y"] >= toolbar["y"] + toolbar["height"]
    _add_page_scripts(page)
    page.locator(".sync-tab-lead-row button").click()
    expect(page.locator("#relationshipDiagnosticsModal")).to_be_visible()


@pytest.mark.parametrize(("tab", "key"), TABS)
def test_cache_bar_drains_and_warns_in_the_last_minute(page, tab, key):
    load_tab(page, tab, key, timeout=180)
    _add_page_scripts(page)
    page.evaluate("initializeCountdowns()")
    bar = page.get_by_role("progressbar")
    expect(bar).to_have_count(1)
    start = bar.locator(".sync-cache-fill").bounding_box()["width"]
    refresh_width = page.locator("button[hx-post].btn-outline-primary").bounding_box()["width"]
    assert bar.bounding_box()["width"] == pytest.approx(refresh_width, abs=1)
    assert start < refresh_width
    expect(bar.locator(".mdi-timer-outline")).to_have_count(1)
    expect(bar.locator(".sync-cache-remaining")).to_have_text("3m")
    assert bar.get_attribute("aria-valuemax") == "180"
    page.clock.fast_forward(60_000)
    middle = bar.locator(".sync-cache-fill").bounding_box()["width"]
    assert middle == pytest.approx(start * 2 / 3, abs=1)
    expect(bar.locator(".sync-cache-remaining")).to_have_text("2m")
    page.clock.fast_forward(61_000)
    assert bar.get_attribute("aria-valuenow") == "59"
    assert bar.get_attribute("aria-valuetext") == bar.get_attribute("title")
    assert bar.locator(".sync-cache-fill").evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(245, 159, 0)"
    expect(bar.locator(".sync-cache-remaining")).to_have_text("59s")
    for selector in (".mdi-timer-outline", ".sync-cache-remaining"):
        assert bar.locator(selector).evaluate("e => getComputedStyle(e).color") == "rgb(245, 159, 0)"
    label_style = bar.locator(".sync-cache-remaining").evaluate(
        "e => ({font: getComputedStyle(e).fontFamily, digits: getComputedStyle(e).fontVariantNumeric})"
    )
    assert "monospace" in label_style["font"]
    assert label_style["digits"] == "tabular-nums"


@pytest.mark.parametrize(("tab", "key"), TABS)
@pytest.mark.parametrize("cached,migrated", [(True, False), (False, False), (True, True)])
def test_toolbar_fits_phone_width_and_keeps_refresh(page, tab, key, cached, migrated):
    load_tab(page, tab, key, cached=cached, migrated=migrated)
    page.set_viewport_size({"width": 390, "height": 900})
    expect(page.locator("button[hx-post].btn-outline-primary")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    if tab == "module" and cached and not migrated:
        sync_box = page.locator("#install-selected-btn").bounding_box()
        refresh_box = page.locator(".sync-tab-refresh-toolbar button").bounding_box()
        assert sync_box["y"] == pytest.approx(refresh_box["y"], abs=1)
    if migrated:
        expect(page.locator(".sync-tab-actions button[type=submit]")).to_have_count(0)
    if not cached:
        expect(page.locator(".sync-tab-lead-row")).to_have_count(0)
        assert page.get_by_role("progressbar").count() == 0


def test_vm_refresh_replaces_toolbar_and_keeps_server_and_spinner(page):
    load_tab(page, "interface", "interface_sync", model="virtualmachine")
    held = []
    page.route("**/endpoint/vm_interface_sync/**", lambda route: held.append(route))
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    refresh = page.locator("button[hx-post].btn-outline-primary")
    refresh.click()
    expect(refresh).to_be_disabled()
    expect(refresh.locator(".spinner-border")).to_have_count(1)
    assert parse_qs(held[0].request.post_data)["server_key"] == ["secondary"]
    held[0].fulfill(body=render_tab("interface", "interface_sync", model="virtualmachine", partial=True, timeout=120))
    expect(page.get_by_role("progressbar")).to_have_attribute("aria-valuemax", "120")
    expect(page.locator("button[hx-post].btn-outline-primary")).to_be_enabled()


def test_cache_expiry_removes_rows_and_actions_but_keeps_refresh(page):
    from test_sync_cache_browser import _page_html, _state

    page.clock.install(time=NOW)
    page.set_content(
        f"<style>{CSS}</style>" + _page_html({"interfaces": _state("ready"), "ipaddresses": _state("ready-ip")})
    )
    page.locator("#interface-sync-content").evaluate(
        "(e, html) => e.outerHTML = html", render_tab("interface", "interface_sync", timeout=120)
    )
    _add_page_scripts(page)
    page.add_style_tag(path=str(STYLE_PATH))
    page.evaluate("initializeSyncCacheConsistency(); initializeCountdowns()")
    expect(page.locator("#test-rows")).to_have_count(1)
    page.clock.fast_forward(120_000)
    expect(page.locator("#test-rows")).to_have_count(0)
    expect(page.locator("#interface-sync-form")).to_have_count(0)
    expect(page.locator(".sync-tab-actions")).to_have_count(0)
    expect(page.locator("#interface-sync-content button[hx-post].btn-outline-primary")).to_be_visible()
    bar = page.get_by_role("progressbar")
    expect(bar).to_have_count(1)
    expect(bar).to_have_attribute("aria-valuenow", "0")
    expect(bar.locator(".sync-cache-remaining")).to_have_text("0s")
    assert bar.locator(".sync-cache-fill").bounding_box()["width"] == 0
    for selector in (".mdi-timer-outline", ".sync-cache-remaining"):
        assert bar.locator(selector).evaluate("e => getComputedStyle(e).color") == "rgb(214, 57, 57)"
    expect(page.locator(".sync-filter-controls, .sync-filter-form")).to_have_count(0)
    page.evaluate("""() => reconcileSyncCacheStatus({
        ...syncCacheController().status,
        interfaces: {...syncCacheController().status.interfaces, state: 'expired', snapshot_available: false}
    })""")
    expect(page.get_by_role("progressbar")).to_have_count(1)


@pytest.mark.parametrize(("tab", "key"), TABS[:-1])
def test_filter_controls_need_a_snapshot_even_with_filter_input(page, tab, key):
    load_tab(page, tab, key, cached=False)
    expect(page.locator(".sync-filter-controls, .sync-filter-form")).to_have_count(0)
    expect(page.locator("button[hx-post].btn-outline-primary")).to_be_visible()


@pytest.mark.parametrize("field,value", [("q", "Ethernet"), ("sync_state", "new")])
def test_filter_controls_submit_the_get_form(page, field, value):
    load_tab(page, "interface", "interface_sync")
    page.route("**/browser-fixture?*", lambda route: route.fulfill(body="<p id='filtered'></p>"))
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    with page.expect_navigation() as navigation:
        if field == "q":
            page.locator("input[type=search]").fill(value)
            page.clock.fast_forward(500)
        else:
            page.locator(".sync-filter-controls select").select_option(value)
    query = parse_qs(navigation.value.url.split("?", 1)[1])
    assert query[f"interface_{field}"] == [value]
    assert query["server_key"] == ["secondary"]
    assert query["tab"] == ["interfaces"]


def test_sync_actions_submit_selected_rows_and_toolbar_options(page):
    load_tab(page, "interface", "interface_sync")
    page.locator("#test-rows td").evaluate("e => e.innerHTML = '<input type=checkbox name=select value=17 checked>'")
    posted = []
    page.route(
        "**/endpoint/sync_selected_interfaces/**",
        lambda route: (
            posted.append(route.request),
            route.fulfill(body=render_tab("interface", "interface_sync", partial=True)),
        )[-1],
    )
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    button = page.locator('button[form="interface-sync-form"]')
    assert page.locator("#autoSelectLagMembers").evaluate("e => e.form.id") == "interface-sync-form"
    button.click()
    page.wait_for_function("document.querySelector('button[form=interface-sync-form]') !== null")
    assert parse_qs(posted[0].post_data)["select"] == ["17"]
    assert parse_qs(posted[0].post_data)["server_key"] == ["secondary"]


def test_active_filters_share_the_row_with_actions_in_the_netbox_frame(page):
    load_tab(page, "interface", "interface_sync")
    page.set_viewport_size({"width": 1440, "height": 900})
    # These dimensions match the live NetBox sidebar and control widths.
    page.add_style_tag(
        content="""
        .tab-pane { width: 1105px; }
        .sync-tab-actions { width: 434px; }
        .sync-filter-controls select { width: 161px; }
        .sync-filter-toggle { width: 80px; }
        .sync-filter-clear { width: 96px; }
        .sync-tab-refresh-toolbar button { width: 147px; }
    """
    )
    sync = page.locator('button[form="interface-sync-form"]').bounding_box()
    refresh = page.locator(".sync-tab-refresh-toolbar button").bounding_box()
    clear = page.locator(".sync-filter-clear").bounding_box()
    assert sync["y"] == pytest.approx(refresh["y"], abs=1)
    assert clear["y"] == pytest.approx(refresh["y"], abs=1)


@pytest.mark.parametrize(("tab", "key"), TABS)
def test_configuration_error_swap_disables_refresh_without_a_server(page, tab, key):
    """HTMX must replace a scoped refresh with an inert button after server resolution fails."""
    load_tab(page, tab, key)
    posted = []

    def configuration_error(route):
        posted.append(parse_qs(route.request.post_data))
        route.fulfill(body=render_tab(tab, key, partial=True, cached=False, server_key=None))

    page.route("**/endpoint/**", configuration_error)
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    page.locator(".sync-tab-refresh-toolbar button").click()
    expect(page.locator("#test-rows")).to_have_count(0)
    refresh = page.locator(".sync-tab-refresh-toolbar button")
    expect(refresh).to_be_disabled()
    assert refresh.get_attribute("hx-post") is None
    refresh.evaluate("button => button.click()")
    assert len(posted) == 1
    assert posted[0]["server_key"] == ["secondary"]
