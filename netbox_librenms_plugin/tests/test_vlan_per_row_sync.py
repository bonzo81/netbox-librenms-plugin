"""End-to-end coverage for per-row VLAN synchronization."""

import json
from html.parser import HTMLParser

import pytest


class _VLANControlParser(HTMLParser):
    """Collect the VLAN controls that a rendered table would submit."""

    def __init__(self):
        super().__init__()
        self.selected_vids = []
        self.group_fields = {}
        self._select_name = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "select":
            self.selected_vids.append(attributes.get("value"))
        elif tag == "select" and attributes.get("name", "").startswith("vlan_group_"):
            self._select_name = attributes["name"]
        elif tag == "option" and self._select_name and "selected" in attributes:
            self.group_fields[self._select_name] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select_name = None


def _sync_url(device):
    """Return the public VLAN synchronization endpoint for a device."""
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


def _seed_vlan_snapshot(device, rows, server_key="default"):
    """Store VLAN source rows under the real synchronization cache key."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    cache.set(SyncVLANsView().get_cache_key(device, "vlans", server_key), rows, timeout=60)


@pytest.mark.django_db
@pytest.mark.parametrize("source_vid", [501, "501", " 501 ", "0501"], ids=["int", "string", "padded", "leading-zero"])
def test_usable_vlan_vid_spellings_round_trip_through_rendered_controls(client, settings, source_vid):
    """Every accepted source spelling must submit one canonical grouped VLAN identity."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from django.urls import reverse
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    configure_default_librenms_server(settings)
    suffix = str(source_vid).strip().replace(" ", "-") or "empty"
    device = make_device(f"vid-round-trip-{suffix}", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name=f"VID group {suffix}", slug=f"vid-group-{suffix}")
    existing = VLAN.objects.create(vid=501, group=group, name="Application")
    source_rows = [{"vlan_vlan": source_vid, "vlan_name": existing.name}]
    comparison_view = DeviceVLANTableView()
    compared_rows = comparison_view.compare_vlans(
        source_rows,
        comparison_view._build_vlan_lookup_maps([group]),
        device=device,
    )
    table = LibreNMSVLANTable(compared_rows, vlan_groups=[group])
    request = RequestFactory().get("/")
    request.user = AnonymousUser()

    html = table.as_html(request)

    assert 'data-vlan-id="501"' in html
    assert 'name="vlan_group_501"' in html
    assert "Synced" in html
    parser = _VLANControlParser()
    parser.feed(html)
    assert parser.selected_vids == ["501"]
    assert parser.group_fields == {"vlan_group_501": str(group.pk)}

    _seed_vlan_snapshot(device, source_rows)
    client.force_login(make_superuser(f"vid-round-trip-{suffix}-user"))
    verify_response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_vlan_sync_group"),
        data=json.dumps({"vid": source_vid, "name": existing.name, "vlan_group_id": group.pk}),
        content_type="application/json",
    )
    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": parser.selected_vids,
            **parser.group_fields,
        },
    )

    assert verify_response.status_code == 200
    assert verify_response.json()["exists_in_netbox"] is True
    assert verify_response.json()["name_matches"] is True
    assert "Synced" in verify_response.json()["status_html"]
    assert response.status_code == 302
    assert VLAN.objects.filter(vid=501, group=group, pk=existing.pk).exists()
    assert not VLAN.objects.filter(vid=501, group__isnull=True).exists()
    assert VLAN.objects.filter(vid=501).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("source_vid", "displayed_vid"),
    [
        (None, "Missing"),
        ("", "Empty"),
        ("abc", "abc"),
        (0, "0"),
        (5000, "5000"),
        (501.0, "501.0"),
        (True, "True"),
        (
            """<script data-test="vid">alert(1)</script>""",
            "&lt;script data-test=&quot;vid&quot;&gt;alert(1)&lt;/script&gt;",
        ),
    ],
    ids=["missing", "empty", "text", "zero", "too-large", "float", "boolean", "html"],
)
def test_invalid_vlan_vid_rows_render_as_inert_escaped_diagnostics(source_vid, displayed_vid):
    """An unusable VID must stay visible without exposing any row control or identity."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    VLAN.objects.create(vid=1, name="Boolean alias guard")
    VLAN.objects.create(vid=501, name="Float alias guard")
    comparison_view = DeviceVLANTableView()
    compared_rows = comparison_view.compare_vlans(
        [
            {
                "vlan_vlan": source_vid,
                "vlan_name": "Invalid source row",
                "vlan_type": "ethernet",
                "vlan_state": "active",
            }
        ],
        comparison_view._build_vlan_lookup_maps([]),
    )
    table = LibreNMSVLANTable(compared_rows)
    request = RequestFactory().get("/")
    request.user = AnonymousUser()

    html = table.as_html(request)

    assert displayed_vid in html
    assert "Invalid VID" in html
    assert "Invalid source row" in html
    assert "ethernet" in html
    assert "Active" in html
    assert "<script" not in html
    assert "data-vlan-id" not in html
    assert 'name="select"' not in html
    assert 'name="vlan_group_' not in html
    assert 'name="sync_one"' not in html


@pytest.mark.django_db
def test_duplicate_canonical_vlan_vid_rows_are_blocked_on_render_and_post(client, settings):
    """Two source rows for one canonical VID must not expose controls or select a winner."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    configure_default_librenms_server(settings)
    device = make_device("duplicate-canonical-vid", librenms_cf={"default": {"id": 42}})
    source_rows = [
        {"vlan_vlan": "501", "vlan_name": "First duplicate"},
        {"vlan_vlan": "0501", "vlan_name": "Second duplicate"},
    ]
    comparison_view = DeviceVLANTableView()
    compared_rows = comparison_view.compare_vlans(source_rows, comparison_view._build_vlan_lookup_maps([]))
    table = LibreNMSVLANTable(compared_rows)
    request = RequestFactory().get("/")
    request.user = AnonymousUser()

    html = table.as_html(request)

    assert "First duplicate" in html
    assert "Second duplicate" in html
    assert html.count("Duplicate VID") == 2
    assert "data-vlan-id" not in html
    assert 'name="select"' not in html
    assert 'name="vlan_group_' not in html
    assert 'name="sync_one"' not in html

    _seed_vlan_snapshot(device, source_rows)
    client.force_login(make_superuser("duplicate-canonical-vid-user"))
    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "501",
            "vlan_group_501": "",
        },
    )

    assert response.status_code == 302
    assert not VLAN.objects.filter(vid=501).exists()
    assert any("duplicate source rows" in str(message).lower() for message in response.wsgi_request._messages)


@pytest.mark.django_db
def test_unsigned_vlan_selection_requires_a_present_canonical_group_field(client, settings):
    """An absent group field must fail closed, while an explicit empty field selects global scope."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    missing_field_device = make_device("missing-vlan-group-field", librenms_cf={"default": {"id": 42}})
    explicit_global_device = make_device("explicit-global-vlan", librenms_cf={"default": {"id": 43}})
    _seed_vlan_snapshot(missing_field_device, [{"vlan_vlan": "0611", "vlan_name": "Missing group field"}])
    _seed_vlan_snapshot(explicit_global_device, [{"vlan_vlan": "0612", "vlan_name": "Explicit global"}])
    client.force_login(make_superuser("canonical-group-field-user"))

    missing_response = client.post(
        _sync_url(missing_field_device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "611",
        },
    )
    global_response = client.post(
        _sync_url(explicit_global_device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "612",
            "vlan_group_612": "",
        },
    )

    assert missing_response.status_code == 302
    assert not VLAN.objects.filter(vid=611).exists()
    assert any(
        "group selection is missing" in str(message).lower() for message in missing_response.wsgi_request._messages
    )
    assert global_response.status_code == 302
    assert VLAN.objects.filter(vid=612, group__isnull=True, name="Explicit global").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("sync_one", ["", "not-a-vid"], ids=["empty", "invalid"])
def test_present_unusable_sync_one_rejects_the_request_instead_of_running_bulk_rows(client, settings, sync_one):
    """A malformed per-row action must not fall through to checked bulk selections."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device(f"invalid-sync-one-{sync_one or 'empty'}", librenms_cf={"default": {"id": 42}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 701, "vlan_name": "Checked bulk row"}])
    client.force_login(make_superuser(f"invalid-sync-one-{sync_one or 'empty'}-user"))

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "sync_one": sync_one,
            "select": "701",
            "vlan_group_701": "",
        },
    )

    assert response.status_code == 302
    assert not VLAN.objects.filter(vid=701).exists()
    assert any("LibreNMS VID is invalid" in str(message) for message in response.wsgi_request._messages)


def test_selected_vlan_vids_are_canonical_deduplicated_and_report_invalid_values():
    """Selection parsing must return one decimal identity and explicit errors for rejected values."""
    from django.test import RequestFactory

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    request = RequestFactory().post(
        "/",
        {
            "select": ["501", "0501", " 501 ", "5000", "not-a-vid"],
        },
    )

    selection = SyncVLANsView._selected_vlan_vids(request, {})

    assert selection.vids == ["501"]
    assert selection.errors == [
        "VLAN 5000: the LibreNMS VID is invalid; skipped.",
        "VLAN not-a-vid: the LibreNMS VID is invalid; skipped.",
    ]


@pytest.mark.django_db
def test_per_row_group_selection_requires_vlan_group_view_permission(client, settings):
    """A per-row group selection must pass the VLAN-group permission gate."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    configure_default_librenms_server(settings)
    device = make_device("per-row-permission", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Per-row permission", slug="per-row-permission")
    user = make_user_with_perms(
        "per-row-permission-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "sync_one": "101",
            "vlan_group_101": str(group.pk),
        },
    )

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == ["Missing permissions: ipam.view_vlangroup"]
    assert not VLAN.objects.filter(vid=101).exists()


@pytest.mark.django_db
def test_force_all_intent_group_requires_vlan_group_view_permission(client, settings):
    """A token-only force-all request must include its VLAN group in the permission pass."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device("force-all-permission", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Force-all permission", slug="force-all-permission")
    vlan = VLAN.objects.create(vid=101, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    intent = SyncVLANsView()._build_conflict(
        vlan=vlan,
        proposed_name="Proposed name",
        obj=device,
        object_type="device",
        server_key="default",
    )["intent"]
    user = make_user_with_perms(
        "force-all-permission-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "force_all": "1",
            "conflict_intent": intent,
            "vlan_group_101": str(group.pk),
        },
    )

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == ["Missing permissions: ipam.view_vlangroup"]
    vlan.refresh_from_db()
    assert vlan.name == "Current name"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("selection_branch", "expected_work", "expected_errors"),
    [
        ("select", ["101", "102"], ["VLAN empty: the LibreNMS VID is invalid; skipped."]),
        ("sync-one", ["102"], []),
        ("confirm-conflicts", ["102"], ["VLAN not-a-vid: the LibreNMS VID is invalid; skipped."]),
        ("force-all", ["101", "102"], []),
    ],
)
def test_permission_selection_is_a_superset_of_every_work_list(selection_branch, expected_work, expected_errors):
    """The permission pass names every VID that any work-list branch can select."""
    from django.test import RequestFactory
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    device = make_device(f"selection-superset-{selection_branch}")
    group = VLANGroup.objects.create(
        name=f"Selection superset {device.pk}",
        slug=f"selection-superset-{device.pk}",
    )
    view = SyncVLANsView()
    conflicts = [
        view._build_conflict(
            vlan=VLAN.objects.create(vid=vid, group=group, name=f"Old {vid}"),
            proposed_name=f"New {vid}",
            obj=device,
            object_type="device",
            server_key="default",
        )
        for vid in (101, 102)
    ]
    intents = [conflict["intent"] for conflict in conflicts]
    selection_forms = {
        "select": {"select": ["101", "", "101", "102"]},
        "sync-one": {"sync_one": "102"},
        "confirm-conflicts": {
            "confirm_conflicts": "1",
            "force_conflict": ["102", "not-a-vid", "102"],
            "conflict_intent": intents,
        },
        "force-all": {"force_all": "1", "conflict_intent": intents},
    }
    request = RequestFactory().post("/", selection_forms[selection_branch])
    force_intents, errors = view._load_force_intents(request, device, "device", "default")

    assert errors == []
    work_list = view._selected_vlan_vids(request, force_intents)
    permission_superset = view._selected_vlan_vids(request)
    assert work_list.vids == expected_work
    assert work_list.errors == expected_errors
    assert set(work_list.vids) <= set(permission_superset.vids)


@pytest.mark.django_db
def test_per_row_create_ignores_other_ticked_rows(client, settings):
    """A per-row create synchronizes only the button's VLAN."""
    from ipam.models import VLAN

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("per-row-create", librenms_cf={"default": {"id": 42}})
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 201, "vlan_name": "Create this"},
            {"vlan_vlan": 202, "vlan_name": "Ignore tick one"},
            {"vlan_vlan": 203, "vlan_name": "Ignore tick two"},
        ],
    )
    client.force_login(make_superuser("per-row-create-user"))

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "sync_one": "201",
            "vlan_group_201": "",
            "select": ["202", "203"],
        },
    )

    assert response.status_code == 302
    assert list(VLAN.objects.filter(vid__in=[201, 202, 203]).values_list("vid", "name")) == [(201, "Create this")]


@pytest.mark.django_db
def test_per_row_update_keeps_one_row_through_confirmation(client, settings):
    """A per-row rename conflict stays scoped to that row through confirmation."""
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("per-row-update", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Per-row update", slug="per-row-update")
    selected = VLAN.objects.create(vid=301, group=group, name="Current selected")
    ticked = VLAN.objects.create(vid=302, group=group, name="Current ticked")
    _seed_vlan_snapshot(
        device,
        [
            {"vlan_vlan": 301, "vlan_name": "Updated selected"},
            {"vlan_vlan": 302, "vlan_name": "Updated ticked"},
        ],
    )
    client.force_login(make_superuser("per-row-update-user"))
    url = _sync_url(device)

    disclosure = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "sync_one": "301",
            "select": "302",
            "vlan_group_301": str(group.pk),
            "vlan_group_302": str(group.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert disclosure.status_code == 200
    assert [conflict["vid"] for conflict in disclosure.context["conflicts"]] == [301]
    assert b'name="select" value="301"' in disclosure.content
    assert b'name="select" value="302"' not in disclosure.content
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Current selected"
    assert ticked.name == "Current ticked"

    conflict = disclosure.context["conflicts"][0]
    confirmation = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "confirm_conflicts": "1",
            "force_conflict": "301",
            "select": "301",
            "conflict_intent": conflict["intent"],
            "vlan_group_301": str(group.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert confirmation.status_code == 200
    assert confirmation.headers["HX-Redirect"]
    selected.refresh_from_db()
    ticked.refresh_from_db()
    assert selected.name == "Updated selected"
    assert ticked.name == "Current ticked"


@pytest.mark.django_db
def test_vlan_table_renders_each_per_row_action_state():
    """The VLAN table renders create, update, and synchronized row states."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory
    from ipam.models import VLAN

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    VLAN.objects.create(vid=402, name="Current update")
    VLAN.objects.create(vid=403, name="Already synced")
    comparison_view = DeviceVLANTableView()
    compared_rows = comparison_view.compare_vlans(
        [
            {"vlan_vlan": 401, "vlan_name": "Create row"},
            {"vlan_vlan": 402, "vlan_name": "Updated name"},
            {"vlan_vlan": 403, "vlan_name": "Already synced"},
        ],
        comparison_view._build_vlan_lookup_maps([]),
    )
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    table = LibreNMSVLANTable(compared_rows)

    html = table.as_html(request)

    assert 'name="sync_one" value="401"' in html
    assert 'title="Create this VLAN in NetBox"' in html
    assert "mdi-plus-thick" in html
    assert "> Create</button>" in html
    assert 'name="sync_one" value="402"' in html
    assert 'title="Update this VLAN\'s name in NetBox"' in html
    assert "mdi-pencil" in html
    assert "> Update</button>" in html
    assert "mdi-check-circle" in html
    assert "Synced" in html

    table.migrated_to_marker = True
    migrated_html = table.as_html(request)

    assert 'name="sync_one"' not in migrated_html
    assert "Not in NetBox" in migrated_html
    assert "Name differs" in migrated_html
    assert "Synced" in migrated_html


@pytest.mark.django_db
@pytest.mark.parametrize("vid", [None, ""], ids=["none", "empty-string"])
def test_vlan_table_row_without_usable_vid_has_no_action(vid):
    """A row without a usable VID renders status text instead of a submit button."""
    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    comparison_view = DeviceVLANTableView()
    compared_row = comparison_view.compare_vlans(
        [{"vlan_vlan": vid, "vlan_name": "Missing identity"}],
        comparison_view._build_vlan_lookup_maps([]),
    )[0]
    table = LibreNMSVLANTable([compared_row])

    vid_html = str(table.render_vlan_id(None, compared_row))
    status_html = str(table.render_status(None, compared_row))

    assert 'name="sync_one"' not in status_html
    assert "Invalid VID" in vid_html


def test_vlan_status_action_escapes_the_reported_vid():
    """The table escapes an unusable reported VID and does not render a submit button."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
    from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

    comparison_view = DeviceVLANTableView()
    compared_rows = comparison_view.compare_vlans(
        [{"vlan_vlan": """"><script>alert(1)</script>""", "vlan_name": "Unsafe identity"}],
        {"vid_to_groups": {}, "vid_to_vlans": {}},
    )
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    html = LibreNMSVLANTable(compared_rows).as_html(request)

    assert "<script>" not in html
    assert "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Invalid VID" in html
    assert """name="sync_one""" not in html


@pytest.mark.parametrize(
    ("exists_in_netbox", "name_matches", "expected_action", "unexpected_action"),
    [
        (False, False, "Create", "Synced"),
        (True, False, "Update", "Synced"),
        (True, True, "Synced", "Create"),
    ],
)
def test_vlan_status_action_uses_data_facts_when_css_class_is_widened(
    monkeypatch,
    exists_in_netbox,
    name_matches,
    expected_action,
    unexpected_action,
):
    """Additional presentation classes must not change the row action."""
    from netbox_librenms_plugin import utils

    get_css_class = utils.get_vlan_sync_css_class
    monkeypatch.setattr(
        utils,
        "get_vlan_sync_css_class",
        lambda exists, matches: f"{get_css_class(exists, matches)} fw-bold",
    )

    html = str(utils.render_vlan_sync_action(601, exists_in_netbox, name_matches))

    assert expected_action in html
    assert unexpected_action not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("sync_actions", "expected_actions"),
    [(True, True), (False, False), (None, True)],
    ids=["actions-enabled", "actions-disabled", "actions-default-enabled"],
)
def test_verify_vlan_group_returns_the_shared_status_cell(client, sync_actions, expected_actions):
    """The verify endpoint re-renders status through the shared server-side renderer."""
    from django.urls import reverse
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.utils import render_vlan_sync_action

    group = VLANGroup.objects.create(
        name=f"Verify per-row {sync_actions}",
        slug=f"verify-per-row-{str(sync_actions).lower()}",
    )
    VLAN.objects.create(vid=501, group=group, name="Current name")
    client.force_login(make_superuser(f"verify-per-row-{sync_actions}"))

    payload = {
        "vid": "501",
        "name": "Updated name",
        "vlan_group_id": group.pk,
    }
    if sync_actions is not None:
        payload["sync_actions"] = sync_actions
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_vlan_sync_group"),
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["status_html"] == str(
        render_vlan_sync_action(501, True, False, actions_enabled=expected_actions)
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("vid", "message"),
    [
        (None, "No VID provided"),
        ("", "No VID provided"),
        (" ", "Invalid VID"),
        ("abc", "Invalid VID"),
        (0, "Invalid VID"),
        (5000, "Invalid VID"),
        (501.0, "Invalid VID"),
        (True, "Invalid VID"),
    ],
    ids=["missing", "empty", "whitespace", "text", "zero", "too-large", "float", "boolean"],
)
def test_verify_vlan_group_rejects_every_unusable_vlan_identity(client, vid, message):
    """The verify endpoint must apply the same VLAN identity boundary as render and write paths."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_superuser

    client.force_login(make_superuser(f"verify-invalid-vid-{str(vid).replace(' ', 'space')}"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_vlan_sync_group"),
        data=json.dumps({"vid": vid, "name": "Unusable", "vlan_group_id": ""}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": message}


@pytest.mark.django_db
def test_invalid_only_selection_still_reports_the_batch_summary(client, settings):
    """A request whose every selection is unusable must keep its skip summary."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("invalid-only-summary", librenms_cf={"default": {"id": 77}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 0, "vlan_name": "Invalid VID"}])
    client.force_login(make_superuser("invalid-only-summary-user"))

    response = client.post(
        _sync_url(device),
        {"server_key": "default", "action": "create_vlans", "select": "0", "vlan_group_0": ""},
    )

    assert response.status_code == 302
    rendered = [str(message) for message in response.wsgi_request._messages]
    assert "VLAN 0: the LibreNMS VID is invalid; skipped." in rendered
    assert "No VLANs synced: 1 skipped (invalid VLAN VID)." in rendered
    from ipam.models import VLAN

    assert not VLAN.objects.filter(vid=0).exists()


@pytest.mark.django_db
def test_unusable_selection_diagnostic_does_not_echo_an_unbounded_value(client, settings):
    """An oversized POST value must be reported as a bounded preview, not echoed whole."""
    from netbox_librenms_plugin.tests.conftest import (
        configure_default_librenms_server,
        make_device,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device("unbounded-vid-diagnostic", librenms_cf={"default": {"id": 78}})
    _seed_vlan_snapshot(device, [{"vlan_vlan": 3201, "vlan_name": "Valid VLAN"}])
    client.force_login(make_superuser("unbounded-vid-diagnostic-user"))
    oversized = "x" * 5000

    response = client.post(
        _sync_url(device),
        {"server_key": "default", "action": "create_vlans", "select": oversized},
    )

    assert response.status_code == 302
    diagnostics = [str(message) for message in response.wsgi_request._messages]
    assert any("the LibreNMS VID is invalid" in message for message in diagnostics)
    assert all(len(message) < 200 for message in diagnostics)
    assert not any(oversized in message for message in diagnostics)


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["force-all", "confirm-conflicts"])
def test_intent_only_group_requires_vlan_group_view_permission(client, settings, mode):
    """A signed intent that names a group must reach the permission gate without a POST field."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device(f"intent-only-permission-{mode}", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name=f"Intent only {mode}", slug=f"intent-only-{mode}")
    vlan = VLAN.objects.create(vid=101, group=group, name="Current name")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Proposed name"}])
    intent = SyncVLANsView()._build_conflict(
        vlan=vlan,
        proposed_name="Proposed name",
        obj=device,
        object_type="device",
        server_key="default",
    )["intent"]
    user = make_user_with_perms(
        f"intent-only-permission-{mode}-user",
        [("view", Device), ("add", VLAN), ("change", VLAN)],
    )
    client.force_login(user)
    payload = {"server_key": "default", "action": "create_vlans", "conflict_intent": intent}
    if mode == "force-all":
        payload["force_all"] = "1"
    else:
        payload["confirm_conflicts"] = "1"
        payload["force_conflict"] = "101"

    response = client.post(_sync_url(device), payload)

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == ["Missing permissions: ipam.view_vlangroup"]
    vlan.refresh_from_db()
    assert vlan.name == "Current name"
