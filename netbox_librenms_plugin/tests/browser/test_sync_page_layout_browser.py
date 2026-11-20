"""Browser checks for the sync page layout at phone width."""

from pathlib import Path
from types import SimpleNamespace

import pytest


TEMPLATES_DIR = Path(__file__).parents[2] / "templates"
STYLE_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "css" / "librenms_sync.css"
PHONE_WIDTH = 390

# The netbox.css rules that set how far the sync page cards can shrink.
NETBOX_PAGE_CSS = """
*, ::before, ::after { box-sizing: border-box; }
body { margin: 0; font: 14px/1.4 sans-serif; }
.page-body { padding: 0 16px; }
.row { display: flex; flex-wrap: wrap; margin: 0 -.75rem; }
.row > * { flex-shrink: 0; width: 100%; max-width: 100%; padding: 0 .75rem; }
@media (min-width: 768px) { .col-md-3 { flex: 0 0 auto; width: 25%; } .col-md-9 { flex: 0 0 auto; width: 75%; } }
.card { display: flex; flex-direction: column; min-width: 0; border: 1px solid #ccc; }
.card-header { margin: 0; padding: .75rem 1rem; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: .25rem .5rem; vertical-align: top; }
.table thead th { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }
.d-flex { display: flex !important; }
.flex-wrap { flex-wrap: wrap !important; }
.align-items-center { align-items: center !important; }
.column-gap-2 { column-gap: .5rem !important; }
.column-gap-3 { column-gap: 1rem !important; }
.row-gap-1 { row-gap: .25rem !important; }
.ms-2 { margin-left: .5rem !important; }
.ms-3 { margin-left: 1rem !important; }
.text-nowrap { white-space: nowrap !important; }
.badge { display: inline-flex; gap: .25rem; padding: .25em .5em; font-size: .857em; white-space: nowrap; }
.btn { display: inline-flex; padding: .25rem .5rem; white-space: nowrap; }
.modal, .d-none { display: none !important; }
"""


def _render_sync_page(model_name):
    """Render the real sync page template for one subject, with NetBox's base template and tags stubbed."""
    from django.template import Context, Engine, Library

    stubs = Library()

    @stubs.simple_tag(name="url")
    def url(*args, **kwargs):
        return "/stub/"

    @stubs.simple_tag(name="static")
    def static(path):
        return f"/static/{path}"

    @stubs.simple_tag(name="render_table")
    def render_table(*args, **kwargs):
        return ""

    @stubs.filter(name="meta")
    def meta(obj, attr):
        return obj._meta[attr]

    # The real tag reads the request's page query, which this request-less render has none of.
    sync_query = Library()
    sync_query.simple_tag(lambda *args, **kwargs: "?", name="sync_tab_url")
    sync_query.simple_tag(lambda *args, **kwargs: [], name="sync_tab_query_items")

    base = ("django.template.loaders.locmem.Loader", {"generic/object.html": "{% block content %}{% endblock %}"})
    engine = Engine(dirs=[str(TEMPLATES_DIR)], loaders=[base, "django.template.loaders.filesystem.Loader"])
    engine.template_builtins.append(stubs)
    engine.template_libraries.update({name: Library() for name in ("buttons", "helpers", "plugins", "static", "i18n")})
    engine.template_libraries["django_tables2"] = stubs
    engine.template_libraries["librenms_sync_query"] = sync_query

    # The widest values seen live: a long FQDN name, an unmatched hardware string, and a missing OS platform.
    assessment = SimpleNamespace(
        reason="os_platform_missing", state="proposed", operation="create_os_platform", tracks_version=True
    )
    subject = SimpleNamespace(
        pk=1,
        name="dist-switch-01.dc1.example.net",
        serial="",
        site=SimpleNamespace(pk=1, name="Main datacenter"),
        device_type=None,
        platform=None,
        virtual_chassis=None,
        _meta={"model_name": model_name, "verbose_name": model_name},
    )
    context = {
        "object": subject,
        "object_model_name": model_name,
        "found_in_librenms": True,
        "librenms_device_id": 42,
        "sysName": "dist-switch-01.dc1.example.net",
        "resolved_name": "dist-switch-01",
        "librenms_device_hardware": "Juniper EX4650-48Y-AFO",
        "librenms_device_serial": "XH3721AF0042",
        "librenms_device_location": "Main datacenter, Rack 12",
        "platform_info": {"assessment": assessment, "librenms_os": "junos", "librenms_version": "23.4R2-S3.9"},
        "csrf_token": "t",
    }
    return engine.get_template("netbox_librenms_plugin/librenms_sync_base.html").render(
        Context(context, use_l10n=False)
    )


@pytest.mark.parametrize("model_name", ["device", "virtualmachine"])
def test_the_information_sync_card_fits_a_phone_width_page(page, model_name):
    """At 390 px the page does not scroll sideways, and the comparison table stays inside its card."""
    page.set_viewport_size({"width": PHONE_WIDTH, "height": 800})
    page.set_content(f"<style>{NETBOX_PAGE_CSS}</style><div class='page-body'>{_render_sync_page(model_name)}</div>")
    page.add_style_tag(path=str(STYLE_PATH))

    state = page.evaluate(
        """() => {
            const header = Array.from(document.querySelectorAll('.card-header'))
                .find((element) => element.textContent.includes('Information Sync'));
            const card = header.closest('.card').getBoundingClientRect();
            const table = header.closest('.card').querySelector('table').getBoundingClientRect();
            return {
                scrollWidth: document.documentElement.scrollWidth,
                innerWidth: window.innerWidth,
                card: [card.left, card.right],
                table: [table.left, table.right],
            };
        }"""
    )

    assert state["scrollWidth"] <= state["innerWidth"], state
    assert state["card"][0] <= state["table"][0] and state["table"][1] <= state["card"][1], state
