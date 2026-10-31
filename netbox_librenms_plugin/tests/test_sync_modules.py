"""Real ORM, cache, and view tests for module synchronization."""

import json
import re

import pytest
from django.core.cache import cache
from django.db import OperationalError, transaction

from netbox_librenms_plugin.server_mappings import LibreNMSPortBindingBusy, read_mapping
from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    configure_librenms_servers,
    install_module,
    make_device,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping, stored_mapping_for_test
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    aborting_statement,
    failing_statement,
    hold_port_claim,
    lock_row,
    lock_timeout,
    second_connection,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    get as view_get,
    make_request,
    message_texts,
    post as view_post,
    render_module_tab,
    rendered_modal_form,
    rendered_module_form,
    rendered_preview_query,
    rendered_preview_url,
    trusted_module_inventory_payload,
)
from netbox_librenms_plugin.module_grants import GrantRefusal, module_inventory_snapshot_digest
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE, classify_conflict
from netbox_librenms_plugin.views.sync.modules import SELECTED_ROWS_REFUSED_MESSAGE


@pytest.mark.django_db
class TestInstallSerialRulePreloading:
    """The install loop must not re-read the serial NormalizationRule rows per item."""

    def _install_two_items(self, device, bays, module_type, **kwargs):
        # Calls the installer directly: the shared _run_install_single helper is rewritten
        # further up the stack and its signature is not stable across branches.
        from unittest.mock import patch

        from dcim.models import Interface, ModuleBay

        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, _module_component_specs

        items = [
            {
                "entPhysicalIndex": index,
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": f"S/N SN{index}",
                "entPhysicalName": f"Line Card {index}",
                "entPhysicalContainedIn": 0,
            }
            for index in (10, 11)
        ]
        index_map = {item["entPhysicalIndex"]: item for item in items}
        remaining = list(bays)
        with (
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(BaseModuleTableView, "_match_bay_for_item", side_effect=lambda *a, **kw: remaining.pop(0)),
        ):
            for item in items:
                InstallBranchView._install_single(
                    device,
                    item,
                    {module_type.model: module_type},
                    naming_map=index_map,
                    ancestry_map=index_map,
                    module_bays=ModuleBay.objects.all(),
                    allowed_module_type_ids={module_type.pk},
                    changeable_components={model: model.objects.all() for _, _, model in _module_component_specs()},
                    changeable_interfaces=Interface.objects.all(),
                    deletable_interfaces=Interface.objects.all(),
                    exact_mappings=[],
                    regex_mappings=[],
                    manufacturer_id=device.device_type.manufacturer_id,
                    norm_rules_bay={},
                    **kwargs,
                )

    @staticmethod
    def _rule_queries(captured):
        return [q["sql"] for q in captured.captured_queries if "normalizationrule" in q["sql"].lower()]

    def test_preloaded_serial_rules_replace_the_per_item_rule_queries(self):
        """Without preloaded rules every installed item re-reads the serial rules."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.utils import preload_normalization_rules

        module_type = make_module_type("WS-X4748")
        device = make_device("install-serial-rules-dev")
        first_bays = [make_module_bay(device, "Slot 1"), make_module_bay(device, "Slot 2")]

        with CaptureQueriesContext(connection) as per_item:
            self._install_two_items(device, first_bays, module_type)

        # Two items, so the unpreloaded path reads the rule table more than once.
        assert len(self._rule_queries(per_item)) > 2, self._rule_queries(per_item)

        other = make_device("install-serial-rules-dev-2")
        second_bays = [make_module_bay(other, "Slot 1"), make_module_bay(other, "Slot 2")]

        with CaptureQueriesContext(connection) as preloaded:
            rules = preload_normalization_rules("serial", manufacturer=other.device_type.manufacturer)
            self._install_two_items(other, second_bays, module_type, norm_rules_serial=rules)

        # The preload itself reads both scopes once; the install loop then reads nothing.
        assert len(self._rule_queries(preloaded)) == 2, self._rule_queries(preloaded)

    def test_both_install_views_forward_the_serial_rules(self):
        """Every install loop must forward preloaded serial rules to the shared writer."""
        import ast
        import inspect

        from netbox_librenms_plugin.views.sync import modules as modules_module

        tree = ast.parse(inspect.getsource(modules_module))
        views = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in {"InstallBranchView", "InstallSelectedView"}
        ]
        assert len(views) == 2
        for view in views:
            installs = [
                node
                for node in ast.walk(view)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_install_single"
            ]
            assert installs, f"{view.name} must install its selected inventory"
            for install in installs:
                assert "norm_rules_serial" in {keyword.arg for keyword in install.keywords}


pytestmark = pytest.mark.django_db


def pytest_generate_tests(metafunc):
    """Parametrize ``spec_index`` over every component the running NetBox replicates."""
    if "spec_index" not in metafunc.fixturenames:
        return
    # Deferred import: the spec list grows with the running NetBox (4.7 adds two cooling components).
    from netbox_librenms_plugin.views.sync.modules import _module_component_specs

    metafunc.parametrize("spec_index", range(len(_module_component_specs())))


def _view(view_class, request, live_librenms):
    """Bind a real view to a real request and real LibreNMS client."""
    view = view_class()
    view._librenms_api = live_librenms.api
    view.setup(request)
    return view


def _post_request(data):
    return make_request("post", data, user=make_superuser(), path="/modules/")


def _rendered_post(view_class, device, rows, librenms_id, live_librenms, path_part, ent_index=None, **options):
    """Seed *rows*, then return the bound view and the post of the *path_part* form the tab renders for *ent_index*."""
    user = options.pop("user", None) or make_superuser()
    extra = options.pop("extra", {})
    assert not options, options
    view = view_class()
    view._librenms_api = live_librenms.api
    seed_inventory(view, device, rows, librenms_id=librenms_id)
    _action, fields = rendered_module_form(render_module_tab(device, user=user), path_part, ent_index)
    request = make_request("post", {**fields, **extra}, user=user, path="/modules/")
    view.setup(request)
    return view, request


def _without_grants(user, *names):
    """Delete the object permissions *names* of *user* and return the user with a fresh permission cache."""
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    permissions = ObjectPermission.objects.filter(name__in=names)
    assert permissions.count() == len(names)
    permissions.delete()
    return get_user_model().objects.get(pk=user.pk)


def _rendered_fields(device, path_part, ent_index=None, *, user=None, server_key="default"):
    """Return the fields of the *path_part* form that the cached Modules tab renders for *ent_index*."""
    content = render_module_tab(device, user=user, server_key=server_key)
    return rendered_module_form(content, path_part, ent_index)[1]


def test_module_inventory_snapshot_digest_is_canonical_and_content_sensitive():
    """Equivalent snapshots share a digest, while changed inventory content does not."""
    first = [{"entPhysicalIndex": 17, "nested": {"b": 2, "a": 1}}]
    reordered = [{"nested": {"a": 1, "b": 2}, "entPhysicalIndex": 17}]
    replacement = [{"entPhysicalIndex": 18, "nested": {"b": 2, "a": 1}}]

    assert module_inventory_snapshot_digest(first) == module_inventory_snapshot_digest(reordered)
    assert module_inventory_snapshot_digest(first) != module_inventory_snapshot_digest(replacement)


def _inventory_item(index, model, name, *, parent=0, serial="", phys_class="module", **extra):
    row = {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": phys_class,
        "entPhysicalContainedIn": parent,
        "entPhysicalSerialNum": serial,
    }
    row.update(extra)
    return row


def _run_install_single(device, item, index_map, module_types):
    """Run the shared installer against unrestricted real model querysets."""
    from dcim.models import Interface, ModuleBay

    from netbox_librenms_plugin.views.sync.modules import InstallBranchView, _module_component_specs

    return InstallBranchView._install_single(
        device,
        item,
        module_types,
        naming_map=index_map,
        ancestry_map=index_map,
        module_bays=ModuleBay.objects.all(),
        allowed_module_type_ids={module_type.pk for module_type in module_types.values()},
        changeable_components={model: model.objects.all() for _, _, model in _module_component_specs()},
        changeable_interfaces=Interface.objects.all(),
        deletable_interfaces=Interface.objects.all(),
        exact_mappings=[],
        regex_mappings=[],
        manufacturer_id=device.device_type.manufacturer_id,
        norm_rules_bay={},
    )


class TestInventoryCacheContract:
    """Module actions accept only current, per-device inventory snapshots."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"inventory": []}, []),
            ({"inventory": [{"entPhysicalIndex": 1}]}, [{"entPhysicalIndex": 1}]),
            (None, None),
            ([], None),
            ({"inventory": "bad"}, None),
            ({"inventory": ["bad"]}, None),
            ({"version": True, "inventory": []}, None),
        ],
    )
    def test_inventory_container_validation(self, payload, expected):
        from netbox_librenms_plugin.sync_cache import MODULE_INVENTORY_CACHE_VERSION, cached_module_inventory

        if isinstance(payload, dict) and "version" not in payload:
            payload = {"version": MODULE_INVENTORY_CACHE_VERSION, **payload}
        assert cached_module_inventory(payload) == expected

    def test_current_fingerprint_returns_the_real_cached_rows(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import _get_cached_inventory_for_device

        device = make_device("module-current-cache", librenms_cf={"default": 41})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        rows = [_inventory_item(1, "CARD", "Slot 1")]
        key = seed_inventory(view, device, rows, librenms_id=41)

        assert _get_cached_inventory_for_device(device, "default", view.get_cache_key) == rows
        assert cache.get(key)["librenms_id"] == 41

    def test_stale_fingerprint_is_rejected(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import _get_cached_inventory_for_device

        device = make_device("module-stale-cache", librenms_cf={"default": 42})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        seed_inventory(view, device, [_inventory_item(1, "CARD", "Slot 1")], librenms_id=99)

        assert _get_cached_inventory_for_device(device, "default", view.get_cache_key) is None

    def test_an_authorized_grant_hands_back_the_cached_row_itself(self, live_librenms):
        """The writer reads the row from the authenticated snapshot it checked the grant against."""
        from netbox_librenms_plugin.module_grants import ACTION_UPDATE_SERIAL
        from netbox_librenms_plugin.views.sync.modules import _authorize_module_grant

        device = make_device("resolve-row-hit", librenms_cf={"default": 771})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        module = install_module(device, make_module_bay(device, "Slot 1").name, "CARD", serial="OLD")
        item = _inventory_item(77, "CARD", "Slot 1", serial="NEW", _librenms_port_id=42, _librenms_ifname="Te1/1/1")
        seed_inventory(view, device, [item], librenms_id=771)
        token = _rendered_fields(device, "update-module-serial", 77)["inventory_binding"]

        granted = _authorize_module_grant(view, device, "default", ACTION_UPDATE_SERIAL, token)
        row = granted.grant.require(device_id=device.pk, ent_index="77", action_target={"module_id": module.pk})
        row = granted.row(row.ent_index)

        assert row["_librenms_port_id"] == 42
        assert row["_librenms_ifname"] == "Te1/1/1"
        assert granted.inventory == [item]
        assert any(entry is row for entry in granted.inventory)

    def test_a_grant_posted_without_its_index_is_stale(self, live_librenms):
        """Without the posted index there is no row to read, so the grant does not match the post."""
        from netbox_librenms_plugin.module_grants import ACTION_UPDATE_SERIAL, GrantRefusal, GrantRefused
        from netbox_librenms_plugin.views.sync.modules import _authorize_module_grant

        device = make_device("resolve-row-no-index", librenms_cf={"default": 772})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        module = install_module(device, make_module_bay(device, "Slot 1").name, "CARD", serial="OLD")
        item = _inventory_item(77, "CARD", "Slot 1", serial="NEW")
        seed_inventory(view, device, [item], librenms_id=772)
        token = _rendered_fields(device, "update-module-serial", 77)["inventory_binding"]
        granted = _authorize_module_grant(view, device, "default", ACTION_UPDATE_SERIAL, token)

        with pytest.raises(GrantRefused) as excinfo:
            granted.grant.require(device_id=device.pk, ent_index="", action_target={"module_id": module.pk})

        assert excinfo.value.refusal is GrantRefusal.STALE

    def test_a_snapshot_of_another_librenms_device_is_a_missing_snapshot(self, live_librenms):
        """A snapshot taken under a different LibreNMS device ID must not authorize any row."""
        from netbox_librenms_plugin.module_grants import ACTION_UPDATE_SERIAL, GrantRefusal, GrantRefused
        from netbox_librenms_plugin.views.sync.modules import _authorize_module_grant

        device = make_device("resolve-row-stale", librenms_cf={"default": 999})
        view = _view(_cache_view_class(), _post_request({}), live_librenms)
        seed_inventory(view, device, [_inventory_item(77, "CARD", "Te1/1/1")], librenms_id=555)

        with pytest.raises(GrantRefused) as excinfo:
            _authorize_module_grant(view, device, "default", ACTION_UPDATE_SERIAL, "any-token")

        assert excinfo.value.refusal is GrantRefusal.CACHE_MISSING


def _cache_view_class():
    from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

    return InstallSelectedView


class TestInventoryIdentityHelpers:
    @pytest.mark.parametrize(
        ("row", "port_id", "names"),
        [
            ({"_librenms_port_id": "17", "_librenms_ifname": "Ethernet1"}, 17, ["Ethernet1"]),
            (
                {
                    "port_id": 18,
                    "_librenms_ifname": "Ethernet2",
                    "_librenms_ifdescr": "uplink",
                    "entPhysicalName": "Ethernet2",
                },
                18,
                ["Ethernet2", "uplink"],
            ),
            ({"port_id": True, "entPhysicalName": "  "}, None, []),
        ],
    )
    def test_port_identity_is_normalized_and_deduplicated(self, row, port_id, names):
        from netbox_librenms_plugin.views.base.modules_view import _get_item_port_identity

        assert _get_item_port_identity(row) == (port_id, names)

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Ethernet2/1/17", [2, 1, 17]),
            ("xe-0/0/7", [0, 0, 7]),
            ("management", []),
            (None, []),
        ],
    )
    def test_interface_coordinates_follow_real_labels(self, label, expected):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        assert BaseModuleTableView._extract_interface_numeric_coordinates(label) == expected

    def test_unique_coordinate_match_selects_the_real_module_interface(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.base.modules_view import _select_module_interface_by_coordinates

        device = make_device("coordinate-device")
        bay = make_module_bay(device, "Coordinate Bay")
        module_type = make_module_type("COORDINATE-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        expected = Interface.objects.create(device=device, module=module, name="Ethernet2/1/17", type="other")
        Interface.objects.create(device=device, module=module, name="Ethernet2/1/18", type="other")

        selected = _select_module_interface_by_coordinates(
            device,
            list(module.interfaces.all()),
            {"entPhysicalName": "port 1/17"},
        )

        assert selected == expected

    def test_tied_coordinate_match_fails_closed(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.base.modules_view import _select_module_interface_by_coordinates

        device = make_device("coordinate-tie")
        bay = make_module_bay(device, "Coordinate Tie Bay")
        module_type = make_module_type("COORDINATE-TIE-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        # Both names score identically against 1/17: same final coordinate, same module coordinate.
        Interface.objects.create(device=device, module=module, name="Ethernet1/17", type="other")
        Interface.objects.create(device=device, module=module, name="Ethernet9/1/17", type="other")

        assert (
            _select_module_interface_by_coordinates(
                device,
                list(module.interfaces.all()),
                {"entPhysicalName": "port 1/17"},
            )
            is None
        )

    @pytest.mark.parametrize(
        ("in_second_member", "label"),
        [(False, "Ethernet1/17"), (True, "Ethernet3/5")],
        ids=["module-coordinate-only", "vc-position-only"],
    )
    def test_a_coordinate_match_needs_the_port_number(self, in_second_member, label):
        """The module and member coordinates are the same for each port of the module, so alone they pick no port."""
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.base.modules_view import _select_module_interface_by_coordinates

        device = make_device(f"coordinate-port-number-{in_second_member}")
        if in_second_member:
            make_virtual_chassis(
                f"coordinate-port-number-vc-{in_second_member}", make_device("coordinate-first"), device
            )
            device.refresh_from_db()
        prefix = device.vc_position or 1
        bay = make_module_bay(device, "Port Number Bay")
        module_type = make_module_type(f"PORT-NUMBER-CARD-{in_second_member}")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        Interface.objects.create(device=device, module=module, name=f"Ethernet{prefix}/1", type="other")
        Interface.objects.create(device=device, module=module, name="mgmt", type="other")

        assert (
            _select_module_interface_by_coordinates(device, list(module.interfaces.all()), {"_librenms_ifname": label})
            is None
        )


class TestInterfaceBinding:
    """Bind inventory port identities to real NetBox interfaces without reassignment."""

    def test_name_match_binds_port_id_and_module(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = make_device("bind-module-interface")
        bay = make_module_bay(device, "Bind Bay")
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type("BIND-CARD"),
            status="active",
        )
        interface = make_interface(device, "Ethernet20")

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 220, "_librenms_ifname": interface.name},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        interface.refresh_from_db()
        assert result == {"status": "bound", "interface": interface.name, "port_id": 220, "changed": True}
        assert interface.module == module
        assert read_mapping(interface).own_id("default") == 220

    def test_existing_port_owner_on_another_device_is_a_conflict(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        target = make_device("bind-target")
        owner_device = make_device("bind-existing-owner")
        owner = make_interface(owner_device, "Ethernet21")
        seed_mapping(owner, "default", own=221)

        result = _bind_interface_librenms_id(
            target,
            {"_librenms_port_id": 221, "_librenms_ifname": "Ethernet21"},
            None,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "conflict"
        assert owner_device.name in result["reason"]

    def test_ambiguous_module_interfaces_require_manual_mapping(self):
        from dcim.models import Interface, Module

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = make_device("bind-ambiguous")
        bay = make_module_bay(device, "Ambiguous Bay")
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type("AMBIGUOUS-CARD"),
            status="active",
        )
        Interface.objects.create(device=device, module=module, name="alpha", type="other")
        Interface.objects.create(device=device, module=module, name="beta", type="other")

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 222},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "skipped"
        assert "multiple module interfaces" in result["reason"]


def _bind_paused_after(read, bind, concurrent):
    """
    Run *bind* on its own connection, pause it after its first query that *read* matches, and run *concurrent*.

    *concurrent* runs and commits on the test's connection while the paused bind holds no row lock.
    Return what *bind* returned.
    """
    from threading import Event

    from django.db import close_old_connections, connection

    paused = Event()
    resume = Event()

    def pause_after_the_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not paused.is_set() and read(sql):
            paused.set()
            assert resume.wait(timeout=10)
        return result

    def run():
        close_old_connections()
        try:
            with connection.execute_wrapper(pause_after_the_read):
                return bind()
        finally:
            connection.close()

    with bounded_executor(max_workers=1) as executor:
        future = executor.submit(run)
        try:
            for _ in range(100):
                if paused.wait(timeout=0.1) or future.done():
                    break
            if not paused.is_set():
                if future.done():
                    raise AssertionError(f"bind finished before the paused read: {future.result()!r}")
                raise AssertionError("bind never reached the paused read")
            concurrent()
        finally:
            resume.set()
        return future.result(timeout=30)


@pytest.mark.django_db
@pytest.mark.parametrize("raises", [False, True])
def test_a_bind_worker_that_finishes_before_the_read_reports_its_outcome(raises):
    """The pause helper must surface an early result or the original worker exception."""

    def bind():
        if raises:
            raise RuntimeError("bind refused before its read")
        return {"status": "skipped"}

    exception = RuntimeError if raises else AssertionError
    message = "bind refused before its read" if raises else "bind finished before the paused read"
    with pytest.raises(exception, match=message):
        _bind_paused_after(lambda sql: False, bind, lambda: pytest.fail("no read was paused"))


def _candidate_read_by_name(sql):
    return 'FROM "dcim_interface"' in sql and '"name" IN' in sql


def _bind(device, port_id, name, server_key="default", module_pk=None):
    from dcim.models import Interface

    from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

    item = {"_librenms_port_id": port_id, "_librenms_ifname": name}
    return _bind_interface_librenms_id(device, item, module_pk, server_key, Interface.objects.all())


@transactional_db_with_all_apps()
@pytest.mark.parametrize(
    ("loser_server", "loser_port", "expected_status", "expected_mapping"),
    [
        ("secondary", 302, "bound", {"default": 301, "secondary": 302}),
        ("default", 303, "conflict", {"default": 301}),
    ],
    ids=["another-server", "same-server"],
)
def test_a_bind_that_read_its_interface_before_a_concurrent_bind_keeps_the_winners_mapping(
    loser_server, loser_port, expected_status, expected_mapping
):
    """Two module binds pick one unbound interface; the one that read it first must decide on the locked row."""
    device = make_device(f"bind-race-{loser_server}")
    interface = make_interface(device, "Ethernet30")
    winner = {}

    loser_result = _bind_paused_after(
        _candidate_read_by_name,
        lambda: _bind(device, loser_port, interface.name, loser_server),
        lambda: winner.update(_bind(device, 301, interface.name)),
    )

    assert winner["status"] == "bound"
    assert loser_result["status"] == expected_status
    if expected_status == "conflict":
        # The name chooses again under the lock, and the name now belongs to the winner's port.
        assert loser_result["reason"] == "Ethernet30 is already bound to a different LibreNMS port; not overwriting"
    interface.refresh_from_db()
    assert stored_mapping_for_test(interface) == expected_mapping


@transactional_db_with_all_apps()
def test_a_bind_whose_interface_was_renamed_and_its_name_reused_binds_nothing():
    """The name chose the interface, so the name must still choose it once the row is locked."""
    from dcim.models import Interface

    device = make_device("bind-renamed-by-name")
    original = make_interface(device, "Ethernet30")
    replacement = {}

    def rename_and_reuse_the_name():
        renamed = Interface.objects.get(pk=original.pk)
        renamed.name = "Ethernet31"
        renamed.save()
        replacement["interface"] = make_interface(device, "Ethernet30")

    result = _bind_paused_after(
        _candidate_read_by_name, lambda: _bind(device, 304, "Ethernet30"), rename_and_reuse_the_name
    )

    assert result["status"] == "skipped"
    assert "changed" in result["reason"]
    for interface in (Interface.objects.get(pk=original.pk), replacement["interface"]):
        interface.refresh_from_db()
        assert read_mapping(interface).own_id("default") is None


@transactional_db_with_all_apps()
def test_a_bind_whose_bound_interface_was_renamed_still_binds_it():
    """The port mapping chose the interface, so a rename after the choice does not matter."""
    from dcim.models import Interface, Module

    device = make_device("bind-renamed-by-mapping")
    module = Module.objects.create(
        device=device,
        module_bay=make_module_bay(device, "Renamed Bay"),
        module_type=make_module_type("RENAMED-CARD"),
        status="active",
    )
    interface = make_interface(device, "Ethernet30")
    seed_mapping(interface, "default", own=305, save=False)
    interface.save()

    def rename():
        renamed = Interface.objects.get(pk=interface.pk)
        renamed.name = "Ethernet31"
        renamed.save()

    # The port owner search reads the VM interfaces last, so the owner is chosen when that read returns.
    result = _bind_paused_after(
        lambda sql: 'FROM "virtualization_vminterface"' in sql,
        lambda: _bind(device, 305, "Ethernet30", module_pk=module.pk),
        rename,
    )

    assert result == {"status": "bound", "interface": "Ethernet31", "port_id": 305, "changed": True}
    interface.refresh_from_db()
    assert (interface.name, interface.module_id) == ("Ethernet31", module.pk)
    assert stored_mapping_for_test(interface) == {"default": 305}


class TestBranchCollection:
    """The table builds each branch list from its own rows, parent first, in the order it renders them."""

    @staticmethod
    def _rows(rows, tag):
        from netbox_librenms_plugin.tests.test_modules_view import _make_view, _run_build_context_real

        return _run_build_context_real(_make_view(), rows, make_device(tag))

    @staticmethod
    def _branch(rendered, index):
        (row,) = [row for row in rendered if row["ent_physical_index"] == index]
        return [entry for entry, _target in row["branch_rows"]]

    def test_parent_and_nested_children_are_collected_depth_first(self):
        rows = [
            _inventory_item(1, "PARENT", "Slot 1"),
            _inventory_item(2, "CHILD", "Daughter 1", parent=1),
            _inventory_item(3, "GRANDCHILD", "Optic 1", parent=2),
            _inventory_item(4, "SIBLING", "Daughter 2", parent=1),
        ]

        assert self._branch(self._rows(rows, "branch-depth-first"), 1) == [1, 2, 3, 4]

    def test_a_cycle_renders_no_row_and_so_no_branch(self):
        rows = [
            _inventory_item(1, "ONE", "One", parent=2),
            _inventory_item(2, "TWO", "Two", parent=1),
        ]

        assert self._rows(rows, "branch-cycle") == []

    def test_blank_model_container_is_skipped_but_its_child_is_kept(self):
        rows = [
            _inventory_item(1, "", "Container"),
            _inventory_item(2, "CHILD", "Child", parent=1),
        ]

        rendered = self._rows(rows, "branch-holder")

        assert [row["ent_physical_index"] for row in rendered] == [2]
        assert self._branch(rendered, 2) == [2]


class TestSharedInstaller:
    """Install rows against real types, bays, templates, and constraints."""

    def test_matching_type_and_bay_create_a_real_module(self):
        from dcim.models import Module

        device = make_device("installer-success")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("INSTALL-CARD")
        item = _inventory_item(1, module_type.model, bay.name, serial="INSTALL-SERIAL")

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        module = Module.objects.get(pk=result["module_pk"])
        assert result["status"] == "installed"
        assert module.module_bay == bay
        assert module.module_type == module_type
        assert module.serial == "INSTALL-SERIAL"

    def test_missing_type_skips_without_creating_a_module(self):
        from dcim.models import Module

        device = make_device("installer-no-type")
        make_module_bay(device, "Slot 1")
        item = _inventory_item(1, "UNKNOWN-CARD", "Slot 1")

        result = _run_install_single(device, item, {1: item}, {})

        assert result["status"] == "skipped"
        assert "no matching type" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    def test_oob_row_is_read_only_even_when_type_and_bay_match(self):
        from dcim.models import Module

        device = make_device("installer-oob")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("OOB-CARD")
        item = _inventory_item(1, module_type.model, bay.name, _source="oob")

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert result["status"] == "skipped"
        assert "read-only" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    def test_occupied_bay_returns_the_existing_module(self):
        device = make_device("installer-occupied")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("OCCUPIED-CARD")
        occupant = install_module(device, bay.name, module_type.model)
        item = _inventory_item(1, module_type.model, bay.name)

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert result["status"] == "skipped"
        assert result["reason"] == "bay already occupied"
        assert result["module_pk"] == occupant.pk

    @pytest.mark.parametrize("placeholder", ["-", "BUILTIN", "none", "Not Available"])
    def test_placeholder_serial_is_stored_as_blank(self, placeholder):
        from dcim.models import Module

        device = make_device(f"installer-placeholder-{len(placeholder)}")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type(f"PLACEHOLDER-CARD-{len(placeholder)}")
        item = _inventory_item(1, module_type.model, bay.name, serial=placeholder)

        result = _run_install_single(device, item, {1: item}, {module_type.model: module_type})

        assert Module.objects.get(pk=result["module_pk"]).serial == ""


class TestInstallAndUpdateViews:
    """Exercise real module mutation views from request through ORM state."""

    def test_single_install_creates_the_selected_module(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install", librenms_cf={"default": 51})
        bay = make_module_bay(device, "View Install Bay")
        module_type = make_module_type("VIEW-INSTALL-CARD")
        item = _inventory_item(510, module_type.model, bay.name, serial="VIEW-SERIAL")
        view, request = _rendered_post(InstallModuleView, device, [item], 51, live_librenms, "install-module", 510)

        response = view_post(view, request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert response.status_code == 302
        assert module.module_type == module_type
        assert module.serial == "VIEW-SERIAL"
        assert any("Installed VIEW-INSTALL-CARD" in text for text in message_texts(request))

    @pytest.mark.parametrize(("phys_class", "model"), [("port", "Linecard-Port"), ("container", "")])
    def test_single_install_refuses_a_holder_row_that_a_valid_grant_names(self, live_librenms, phys_class, model):
        """A grant signed for a holder row, e.g. by a page that older holder rules rendered, installs nothing."""
        from dcim.models import Module

        from netbox_librenms_plugin.inventory_policy import load_inventory_policy
        from netbox_librenms_plugin.module_grants import ACTION_INSTALL_MODULE, RENDER_ATTRIBUTED, sign_module_grant
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView, _get_cached_inventory_for_device

        device = make_device(f"view-install-holder-{phys_class}", librenms_cf={"default": 52})
        bay = make_module_bay(device, "Holder Bay")
        module_type = make_module_type(f"HOLDER-CARD-{phys_class.upper()}")
        holder = _inventory_item(520, model, bay.name, phys_class=phys_class)
        view = InstallModuleView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, [holder], librenms_id=52)
        inventory = _get_cached_inventory_for_device(device, "default", view.get_cache_key)
        grant = sign_module_grant(
            page_device_id=device.pk,
            device_id=device.pk,
            server_key="default",
            action=ACTION_INSTALL_MODULE,
            inventory_digest=module_inventory_snapshot_digest(inventory),
            policy_fingerprint=load_inventory_policy(device.pk).fingerprint,
            render_mode=RENDER_ATTRIBUTED,
            ent_index=520,
            action_target={"module_bay_id": bay.pk, "module_type_id": module_type.pk},
        )
        request = _post_request(
            {
                "module_bay_id": bay.pk,
                "module_type_id": module_type.pk,
                "ent_index": 520,
                "server_key": "default",
                "inventory_binding": grant,
            }
        )
        view.setup(request)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert not Module.objects.filter(device=device).exists()
        assert message_texts(request, "error") == ["Nothing was changed: not hardware."]

    def test_a_database_error_in_the_bind_keeps_the_install(self, live_librenms):
        """The bind's own savepoint takes a real PostgreSQL error; the install still commits."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-bind-error", librenms_cf={"default": 68})
        bay = make_module_bay(device, "Bind Error Bay")
        module_type = make_module_type("BIND-ERROR-CARD")
        interface = make_interface(device, "Ethernet68")
        item = _inventory_item(
            680, module_type.model, bay.name, _librenms_port_id=6680, _librenms_ifname=interface.name
        )
        view, request = _rendered_post(InstallModuleView, device, [item], 68, live_librenms, "install-module", 680)

        with aborting_statement(lambda sql, params: sql.startswith('UPDATE "dcim_interface"')) as failed:
            response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert failed, "precondition: the bind wrote the interface"
        assert response.status_code == 302
        assert Module.objects.get(device=device, module_bay=bay).module_type == module_type
        assert interface.module_id is None
        assert any("interface binding was skipped: unexpected error" in text for text in message_texts(request))

    def test_single_install_refuses_a_reused_inventory_index(self, live_librenms):
        """A stale install form must not apply data from a replacement inventory row."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-stale", librenms_cf={"default": 64})
        bay = make_module_bay(device, "Stale Install Bay")
        module_type = make_module_type("STALE-INSTALL-CARD")
        rendered_item = _inventory_item(640, module_type.model, bay.name, serial="ORIGINAL-SERIAL")
        replacement_item = {**rendered_item, "entPhysicalSerialNum": "REPLACEMENT-SERIAL"}
        view, request = _rendered_post(
            InstallModuleView, device, [rendered_item], 64, live_librenms, "install-module", 640
        )
        seed_inventory(view, device, [replacement_item], librenms_id=64)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert not Module.objects.filter(device=device, module_bay=bay).exists()
        # The grant names the snapshot that held the rendered row, so a reused index refuses.
        assert GrantRefusal.SNAPSHOT.message in message_texts(request, "error")

    def test_single_install_does_not_replace_an_occupied_bay(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-occupied", librenms_cf={"default": 52})
        bay = make_module_bay(device, "Occupied View Bay")
        module_type = make_module_type("VIEW-OCCUPIED-CARD")
        existing = install_module(device, bay.name, module_type.model, serial="ORIGINAL")
        request = _post_request(
            {
                "module_bay_id": bay.pk,
                "module_type_id": module_type.pk,
                "serial": "REPLACEMENT",
                "server_key": "default",
            }
        )

        response = view_post(_view(InstallModuleView, request, live_librenms), request, pk=device.pk)

        assert response.status_code == 302
        assert Module.objects.get(device=device, module_bay=bay) == existing
        assert Module.objects.get(pk=existing.pk).serial == "ORIGINAL"
        assert any("already has a module" in text for text in message_texts(request))

    def test_install_module_refuses_an_unresolved_ent_index(self, live_librenms):
        """An install reads its inventory metadata from the cache, never from the post."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-unresolved", librenms_cf={"default": 55})
        bay = make_module_bay(device, "Unresolved Install Bay")
        module_type = make_module_type("UNRESOLVED-INSTALL-CARD")
        interface = make_interface(device, "Ethernet55")
        item = _inventory_item(
            550,
            module_type.model,
            interface.name,
            serial="LNMS-SN",
            _librenms_port_id=5550,
            _librenms_ifname=interface.name,
        )
        # A crafted post: an index the snapshot does not carry, plus the identity and serial the
        # deleted form fallback used to read.
        request = _post_request(
            {
                "module_bay_id": bay.pk,
                "module_type_id": module_type.pk,
                "ent_index": 9999,
                "serial": "FORGED-SN",
                "librenms_port_id": 5550,
                "librenms_ifname": interface.name,
                "server_key": "default",
            }
        )
        view = _view(InstallModuleView, request, live_librenms)
        seed_inventory(view, device, [item], librenms_id=55)

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert not Module.objects.filter(device=device).exists()
        assert read_mapping(interface).own_id("default") is None
        assert interface.module_id is None
        assert GrantRefusal.STALE.message in message_texts(request, "error")

    def test_carrier_install_without_an_ent_index_still_installs_with_no_serial(self, live_librenms):
        """The carrier is not an inventory row, so its button posts no index and gets no serial."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("view-install-carrier", librenms_cf={"default": 60})
        bay = make_module_bay(device, "Carrier Install Bay")
        module_type = make_module_type("CARRIER-INSTALL-CARD")
        request = _post_request({"module_bay_id": bay.pk, "module_type_id": module_type.pk, "server_key": "default"})

        response = view_post(_view(InstallModuleView, request, live_librenms), request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert response.status_code == 302
        assert module.module_type == module_type
        assert module.serial == ""

    def test_update_serial_changes_the_real_module(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-update-serial", librenms_cf={"default": 53})
        bay = make_module_bay(device, "Serial Bay")
        module = install_module(device, bay.name, "SERIAL-CARD", serial="OLD")
        item = _inventory_item(530, module.module_type.model, bay.name, serial="NEW")
        view, request = _rendered_post(
            UpdateModuleSerialView, device, [item], 53, live_librenms, "update-module-serial", 530
        )

        response = view_post(view, request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert module.serial == "NEW"
        assert any("Updated serial" in text for text in message_texts(request))

    def test_persists_the_cached_serial_not_the_posted_one(self, live_librenms):
        """A replayed or edited form must not store a serial LibreNMS never reported."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-serial-cached", librenms_cf={"default": 56})
        bay = make_module_bay(device, "Cached Serial Bay")
        module = install_module(device, bay.name, "CACHED-SERIAL-CARD", serial="OLD-SN")
        item = _inventory_item(560, module.module_type.model, bay.name, serial="LNMS-SN")
        view, request = _rendered_post(
            UpdateModuleSerialView,
            device,
            [item],
            56,
            live_librenms,
            "update-module-serial",
            560,
            extra={"serial": "FORGED-SN"},
        )

        response = view_post(view, request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert module.serial == "LNMS-SN"
        assert any("LNMS-SN" in text for text in message_texts(request, "success"))

    def test_update_serial_refuses_a_reused_inventory_index(self, live_librenms):
        """A stale serial form must not apply a replacement row's serial."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-serial-stale", librenms_cf={"default": 65})
        bay = make_module_bay(device, "Stale Serial Bay")
        module = install_module(device, bay.name, "STALE-SERIAL-CARD", serial="OLD-SERIAL")
        rendered_item = _inventory_item(650, module.module_type.model, bay.name, serial="ORIGINAL-SERIAL")
        replacement_item = {**rendered_item, "entPhysicalSerialNum": "REPLACEMENT-SERIAL"}
        view, request = _rendered_post(
            UpdateModuleSerialView, device, [rendered_item], 65, live_librenms, "update-module-serial", 650
        )
        seed_inventory(view, device, [replacement_item], librenms_id=65)

        response = view_post(view, request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert module.serial == "OLD-SERIAL"
        # The grant names the snapshot that held the rendered row, so a reused index refuses.
        assert GrantRefusal.SNAPSHOT.message in message_texts(request, "error")

    def test_update_serial_rejects_a_row_bound_to_another_module(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-serial-binding", librenms_cf={"default": 62})
        expected_bay = make_module_bay(device, "Expected Serial Bay")
        forged_bay = make_module_bay(device, "Forged Serial Bay")
        expected = install_module(device, expected_bay.name, "BOUND-SERIAL-CARD", serial="EXPECTED-OLD")
        forged = Module.objects.create(
            device=device,
            module_bay=forged_bay,
            module_type=expected.module_type,
            serial="FORGED-OLD",
            status="active",
        )
        item = _inventory_item(620, expected.module_type.model, expected_bay.name, serial="ROW-SERIAL")
        # The rendered form names the expected module; the tampered post names the other one.
        view, request = _rendered_post(
            UpdateModuleSerialView,
            device,
            [item],
            62,
            live_librenms,
            "update-module-serial",
            620,
            extra={"module_id": str(forged.pk)},
        )

        response = view_post(view, request, pk=device.pk)

        forged.refresh_from_db()
        assert response.status_code == 302
        assert forged.serial == "FORGED-OLD"
        assert any(
            message.startswith("Inventory action is stale or does not match this row.")
            for message in message_texts(request, "error")
        )

    def test_refuses_an_oob_inventory_row_named_with_the_grant_of_a_host_row(self, live_librenms):
        """OOB controller inventory renders no action, so a crafted index for it carries another row's grant."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-serial-oob", librenms_cf={"default": 57})
        bay = make_module_bay(device, "OOB Serial Bay")
        module = install_module(device, bay.name, "OOB-SERIAL-CARD", serial="OLD-SN")
        host = _inventory_item(571, module.module_type.model, bay.name, serial="HOST-SN")
        oob = _inventory_item(570, module.module_type.model, bay.name, serial="OOB-SN", _source="oob")
        view, request = _rendered_post(
            UpdateModuleSerialView,
            device,
            [host, oob],
            57,
            live_librenms,
            "update-module-serial",
            571,
            extra={"ent_index": "570"},
        )

        response = view_post(view, request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert message_texts(request, "error") == [GrantRefusal.STALE.message]
        assert module.serial == "OLD-SN"

    def test_reports_an_inventory_index_that_is_not_cached(self, live_librenms):
        """A row the snapshot does not carry is refused instead of writing a blank serial."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("view-serial-missing", librenms_cf={"default": 58})
        bay = make_module_bay(device, "Missing Serial Bay")
        module = install_module(device, bay.name, "MISSING-SERIAL-CARD", serial="OLD-SN")
        item = _inventory_item(580, module.module_type.model, bay.name, serial="LNMS-SN")
        request = _post_request({"module_id": module.pk, "ent_index": 581, "server_key": "default"})
        view = _view(UpdateModuleSerialView, request, live_librenms)
        seed_inventory(view, device, [item], librenms_id=58)

        response = view_post(view, request, pk=device.pk)

        module.refresh_from_db()
        assert response.status_code == 302
        assert GrantRefusal.STALE.message in message_texts(request, "error")
        assert module.serial == "OLD-SN"

    def test_update_interface_binds_cached_inventory_identity(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-update-interface", librenms_cf={"default": 54})
        bay = make_module_bay(device, "Interface Bay")
        module = install_module(device, bay.name, "INTERFACE-CARD")
        interface = make_interface(device, "Ethernet54")
        item = _inventory_item(
            540,
            module.module_type.model,
            bay.name,
            _librenms_port_id=5540,
            _librenms_ifname=interface.name,
        )
        view, request = _rendered_post(
            UpdateModuleInterfaceView, device, [item], 54, live_librenms, "update-module-interface", 540
        )

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module == module
        assert read_mapping(interface).own_id("default") == 5540
        assert any("Updated interface" in text for text in message_texts(request))

    def test_update_interface_refuses_a_reused_inventory_index(self, live_librenms):
        """A stale interface form must not bind identity from a replacement row."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-interface-stale", librenms_cf={"default": 66})
        bay = make_module_bay(device, "Stale Interface Bay")
        module = install_module(device, bay.name, "STALE-INTERFACE-CARD")
        original = make_interface(device, "Ethernet66")
        replacement = make_interface(device, "Ethernet67")
        rendered_item = _inventory_item(
            660,
            module.module_type.model,
            bay.name,
            _librenms_port_id=5660,
            _librenms_ifname=original.name,
        )
        replacement_item = {
            **rendered_item,
            "_librenms_port_id": 5670,
            "_librenms_ifname": replacement.name,
        }
        view, request = _rendered_post(
            UpdateModuleInterfaceView, device, [rendered_item], 66, live_librenms, "update-module-interface", 660
        )
        seed_inventory(view, device, [replacement_item], librenms_id=66)

        response = view_post(view, request, pk=device.pk)

        original.refresh_from_db()
        replacement.refresh_from_db()
        assert response.status_code == 302
        assert original.module_id is None
        assert replacement.module_id is None
        assert read_mapping(original).own_id("default") is None
        assert read_mapping(replacement).own_id("default") is None
        # The grant names the snapshot that held the rendered row, so a reused index refuses.
        assert GrantRefusal.SNAPSHOT.message in message_texts(request, "error")

    def test_update_interface_rejects_a_row_bound_to_another_module(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-interface-binding", librenms_cf={"default": 63})
        expected_bay = make_module_bay(device, "Expected Interface Bay")
        forged_bay = make_module_bay(device, "Forged Interface Bay")
        expected = install_module(device, expected_bay.name, "BOUND-INTERFACE-CARD")
        forged = Module.objects.create(
            device=device,
            module_bay=forged_bay,
            module_type=expected.module_type,
            status="active",
        )
        interface = make_interface(device, "Ethernet63")
        item = _inventory_item(
            630,
            expected.module_type.model,
            expected_bay.name,
            _librenms_port_id=5630,
            _librenms_ifname=interface.name,
        )
        # The rendered form names the expected module; the tampered post names the other one.
        view, request = _rendered_post(
            UpdateModuleInterfaceView,
            device,
            [item],
            63,
            live_librenms,
            "update-module-interface",
            630,
            extra={"module_id": str(forged.pk)},
        )

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module_id is None
        assert read_mapping(interface).own_id("default") is None
        assert any(
            message.startswith("Inventory action is stale or does not match this row.")
            for message in message_texts(request, "error")
        )

    def test_update_module_interface_refuses_an_unresolved_ent_index(self, live_librenms):
        """Posted metadata carries no _source marker, so an unknown ent_index must not bind at all."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-update-interface-unresolved", librenms_cf={"default": 61})
        bay = make_module_bay(device, "Unresolved Interface Bay")
        module = install_module(device, bay.name, "UNRESOLVED-INTERFACE-CARD")
        interface = make_interface(device, "Ethernet61")
        # The one cached row is OOB, so no index at all makes this identity bindable.
        item = _inventory_item(
            610,
            module.module_type.model,
            interface.name,
            _librenms_port_id=5610,
            _librenms_ifname=interface.name,
            _source="oob",
        )
        request = _post_request(
            {
                "module_id": module.pk,
                "ent_index": 9999,
                "librenms_port_id": 5610,
                "librenms_ifname": interface.name,
                "server_key": "default",
            }
        )
        view = _view(UpdateModuleInterfaceView, request, live_librenms)
        seed_inventory(view, device, [item], librenms_id=61)

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert read_mapping(interface).own_id("default") is None
        assert interface.module_id is None
        assert GrantRefusal.STALE.message in message_texts(request, "error")

    def test_update_module_interface_refuses_an_oob_row_named_with_the_grant_of_a_host_row(self, live_librenms):
        """OOB rows render no action, so a crafted ent_index for one carries another row's grant and binds nothing."""
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("view-update-interface-oob", librenms_cf={"default": 59})
        bay = make_module_bay(device, "OOB Interface Bay")
        module = install_module(device, bay.name, "OOB-INTERFACE-CARD")
        interface = make_interface(device, "Ethernet59")
        user = make_user_with_perms(
            "view-update-interface-oob",
            [("view", Device), ("view", Module), ("view", Interface), ("change", Interface)],
        )
        # The host row renders the update; the OOB row carries everything a successful bind needs.
        host = _inventory_item(
            591, module.module_type.model, bay.name, _librenms_port_id=5591, _librenms_ifname=interface.name
        )
        oob = _inventory_item(
            590,
            module.module_type.model,
            interface.name,
            _librenms_port_id=5590,
            _librenms_ifname=interface.name,
            _source="oob",
        )
        view, request = _rendered_post(
            UpdateModuleInterfaceView,
            device,
            [host, oob],
            59,
            live_librenms,
            "update-module-interface",
            591,
            user=user,
            extra={"ent_index": "590"},
        )

        response = view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert response.status_code == 302
        assert message_texts(request, "error") == [GrantRefusal.STALE.message]
        assert read_mapping(interface).own_id("default") is None
        assert interface.module_id is None

    def test_a_row_retargeted_without_a_verified_override_is_refused(self, live_librenms):
        """
        A row can be installed onto a VC member whose manufacturer differs from the page device.

        The table renders the row for the page device. Another member needs the override that a
        verify of that member signs, and the member's own rule hides the row there, so none exists.
        """
        from dcim.models import Manufacturer, Module, ModuleType, VirtualChassis

        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        page_mfr = Manufacturer.objects.create(name="Target Page Vendor", slug="target-page-vendor")
        member_mfr = Manufacturer.objects.create(name="Target Member Vendor", slug="target-member-vendor")
        page = make_device_with_module_bays("target-rules-page", ["Slot 0"], manufacturer=page_mfr)
        member = make_device_with_module_bays("target-rules-member", ["Slot 0"], manufacturer=member_mfr)
        vc = VirtualChassis.objects.create(name="target-rules-vc")
        for position, device in ((1, page), (2, member)):
            device.virtual_chassis = vc
            device.vc_position = position
            device.save()
        seed_stored_mapping(page, {"default": {"id": 77}}, save=True)
        module_type = ModuleType.objects.create(manufacturer=member_mfr, model="TargetRulesModule")
        # Scoped to the MEMBER's manufacturer, so only a target-resolved lookup finds it.
        InventoryIgnoreRule.objects.create(
            name="target-rules-skip",
            match_type=InventoryIgnoreRule.MATCH_ENDS_WITH,
            pattern="Slot 0",
            action=InventoryIgnoreRule.ACTION_SKIP,
            require_serial_match_parent=False,
            manufacturer=member_mfr,
        )
        item = _inventory_item(100, module_type.model, "Slot 0")
        view, request = _rendered_post(
            InstallSelectedView,
            page,
            [item],
            77,
            live_librenms,
            "install-selected",
            extra={"select": ["100"], "device_selection_100": str(member.pk)},
        )

        view_post(view, request, pk=page.pk)

        assert not Module.objects.filter(device__in=[page, member]).exists()
        # Without this, a regression in bay matching or module-type resolution also installs
        # nothing and keeps the test green for a reason the grant never caused.
        assert message_texts(request, "error") == [SELECTED_ROWS_REFUSED_MESSAGE.format(rows="Slot 0")]

    def test_an_ancestor_skip_rule_of_another_member_does_not_hide_a_selected_row(self, live_librenms):
        """The parent belongs to the page member, so only that member's rules decide whether it hides the row."""
        from dcim.models import Manufacturer, Module, ModuleType, VirtualChassis

        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
        from netbox_librenms_plugin.tests.test_modules_view import _make_view, _run_build_context_real
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        page_mfr = Manufacturer.objects.create(name="Ancestor Page Vendor", slug="ancestor-page-vendor")
        member_mfr = Manufacturer.objects.create(name="Ancestor Member Vendor", slug="ancestor-member-vendor")
        page = make_device_with_module_bays("ancestor-rules-page", [], manufacturer=page_mfr, serial="SN-PAGE")
        member = make_device_with_module_bays(
            "ancestor-rules-member", ["Optic B"], manufacturer=member_mfr, serial="SN-MEMBER"
        )
        vc = VirtualChassis.objects.create(name="ancestor-rules-vc")
        for position, device in ((1, page), (2, member)):
            device.virtual_chassis = vc
            device.vc_position = position
            device.save()
        seed_stored_mapping(page, {"default": {"id": 78}}, save=True)
        module_type = ModuleType.objects.create(manufacturer=member_mfr, model="AncestorRulesOptic")
        # Scoped to the member's vendor: the serial-less parent stays with the page member, its child goes to the member.
        InventoryIgnoreRule.objects.create(
            name="ancestor-rules-skip",
            match_type=InventoryIgnoreRule.MATCH_ENDS_WITH,
            pattern="Card A",
            action=InventoryIgnoreRule.ACTION_SKIP,
            require_serial_match_parent=False,
            manufacturer=member_mfr,
        )
        rows = [
            _inventory_item(100, "ANCESTOR-PAGE-CARD", "Card A"),
            _inventory_item(101, module_type.model, "Optic B", parent=100, serial="SN-MEMBER"),
        ]
        table_indexes = {row["ent_physical_index"] for row in _run_build_context_real(_make_view(), rows, page)}
        view, request = _rendered_post(
            InstallSelectedView,
            page,
            rows,
            78,
            live_librenms,
            "install-selected",
            extra={"select": ["101"], "device_selection_101": str(member.pk)},
        )

        view_post(view, request, pk=page.pk)

        assert {100, 101} <= table_indexes
        assert not any("matched ignore rule" in text for text in message_texts(request)), message_texts(request)
        assert Module.objects.get(device=member, module_type=module_type).module_bay.name == "Optic B"

    def test_install_selected_uses_real_cache_mapping_and_models(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("view-install-selected", librenms_cf={"default": 55})
        bay = make_module_bay(device, "Selected Bay")
        module_type = make_module_type("SELECTED-CARD")
        ModuleBayMapping.objects.create(
            librenms_name="LibreNMS Selected Slot",
            librenms_class="module",
            netbox_bay_name=bay.name,
        )
        item = _inventory_item(550, module_type.model, "LibreNMS Selected Slot", serial="SELECTED-SERIAL")
        view, request = _rendered_post(
            InstallSelectedView, device, [item], 55, live_librenms, "install-selected", extra={"select": ["550"]}
        )

        response = view_post(view, request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert response.status_code == 302
        assert module.module_type == module_type
        assert module.serial == "SELECTED-SERIAL"
        assert any("Installed 1 module" in text for text in message_texts(request))

    @pytest.mark.parametrize("scenario", ["selected-row", "branch-child", "selected-ancestor"])
    def test_bulk_install_refuses_a_changed_inventory_snapshot(self, live_librenms, scenario):
        """Bulk actions must not use changed rows, descendants, or ancestor placement context."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        device = make_device(f"view-bulk-stale-{scenario}", librenms_cf={"default": 66})
        bay = make_module_bay(device, f"Stale {scenario} Bay")
        module_type = make_module_type(f"STALE-{scenario}-CARD")
        selected = _inventory_item(700, module_type.model, bay.name, serial="ORIGINAL-SERIAL")
        rendered_inventory = [selected]
        form, ent_index, extra = "install-selected", None, {"select": ["700"]}
        view_class = InstallSelectedView

        if scenario == "branch-child":
            # A child with a module type makes the table render the branch button.
            child_type = make_module_type(f"STALE-{scenario}-CHILD")
            rendered_inventory.append(_inventory_item(701, child_type.model, "Child", parent=700))
            changed_inventory = [rendered_inventory[0], {**rendered_inventory[1], "entPhysicalName": "Replacement"}]
            form, ent_index, extra = "install-branch", 700, {}
            view_class = InstallBranchView
        elif scenario == "selected-ancestor":
            ancestor = _inventory_item(699, "CHASSIS", "Original Parent", phys_class="chassis")
            selected["entPhysicalContainedIn"] = 699
            rendered_inventory.insert(0, ancestor)
            changed_inventory = [{**ancestor, "entPhysicalName": "Replacement Parent"}, selected]
        else:
            changed_inventory = [{**selected, "entPhysicalSerialNum": "REPLACEMENT-SERIAL"}]

        view, request = _rendered_post(
            view_class, device, rendered_inventory, 66, live_librenms, form, ent_index, extra=extra
        )
        seed_inventory(view, device, changed_inventory, librenms_id=66)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert not Module.objects.filter(device=device).exists()
        assert GrantRefusal.SNAPSHOT.message in message_texts(request, "error")

    @pytest.mark.parametrize(
        ("action", "request_data"),
        [
            ("install_branch", {"parent_index": "700", "server_key": "default"}),
            ("install_selected", {"select": ["700"], "server_key": "default"}),
        ],
    )
    def test_bulk_install_refuses_a_missing_inventory_binding(
        self,
        live_librenms,
        action,
        request_data,
    ):
        """Bulk actions must fail closed when the rendered snapshot binding is absent."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        device = make_device(f"view-bulk-unsigned-{action}", librenms_cf={"default": 67})
        bay = make_module_bay(device, f"Unsigned {action} Bay")
        module_type = make_module_type(f"UNSIGNED-{action}-CARD")
        inventory = [_inventory_item(700, module_type.model, bay.name)]
        request = _post_request(request_data)
        view_class = InstallBranchView if action == "install_branch" else InstallSelectedView
        view = _view(view_class, request, live_librenms)
        seed_inventory(view, device, inventory, librenms_id=67)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert not Module.objects.filter(device=device).exists()
        assert GrantRefusal.STALE.message in message_texts(request, "error")

    def test_batch_install_preloads_the_serial_normalization_rules_once(self, live_librenms):
        """The serial scope is queried once for the batch, not once per inventory row."""
        from dcim.models import Module
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("view-serial-rule-preload", librenms_cf={"default": 60})
        module_type = make_module_type("SERIAL-PRELOAD-CARD")
        inventory = []
        for position in range(1, 4):
            bay = make_module_bay(device, f"Preload Slot {position}")
            inventory.append(_inventory_item(600 + position, module_type.model, bay.name, serial=f"SN{position}"))
        view, request = _rendered_post(
            InstallSelectedView,
            device,
            inventory,
            60,
            live_librenms,
            "install-selected",
            extra={"select": [str(item["entPhysicalIndex"]) for item in inventory]},
        )

        with CaptureQueriesContext(connection) as captured:
            response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert Module.objects.filter(device=device).count() == len(inventory)
        serial_rule_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "normalizationrule" in query["sql"].lower() and "'serial'" in query["sql"]
        ]
        # One preload for the unscoped rules, one lazy fill for the device manufacturer.
        assert len(serial_rule_queries) <= 2, (
            f"the serial normalization rules were queried {len(serial_rule_queries)} times "
            f"for {len(inventory)} inventory rows"
        )

    def test_branch_install_does_not_use_a_bay_or_module_type_hidden_after_the_render(self, live_librenms):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("module-catalog-scope", librenms_cf={"default": 1})
        hidden_bay = make_module_bay(device, "Hidden Scope Bay")
        allowed_bay = make_module_bay(device, "Allowed Scope Bay")
        hidden_type = make_module_type("Hidden Scope Module Type")
        allowed_type = make_module_type("Allowed Scope Module Type")
        user = make_user_with_perms(
            "module-catalog-scope",
            [("view", Device), ("add", Module), ("add", Interface), ("change", Interface), ("delete", Interface)],
        )
        user = grant(user, "view", ModuleBay, constraints={"pk": allowed_bay.pk})
        user = grant(user, "view", ModuleType, constraints={"pk": allowed_type.pk})
        user = grant(user, "view", ModuleBay, name="module-catalog-scope-wide-bays")
        user = grant(user, "view", ModuleType, name="module-catalog-scope-wide-types")
        # The child gives the row its branch button while the wide grants still show the hidden bay and type.
        inventory = [
            _inventory_item(100, hidden_type.model, hidden_bay.name),
            _inventory_item(101, allowed_type.model, "Child", parent=100),
        ]
        _view_unused, rendered = _rendered_post(
            InstallBranchView, device, inventory, 1, live_librenms, "install-branch", 100, user=user
        )
        user = _without_grants(user, "module-catalog-scope-wide-bays", "module-catalog-scope-wide-types")
        request = make_request("post", rendered.POST.dict(), user=user, path="/modules/install-branch/")
        view = _view(InstallBranchView, request, live_librenms)

        view_post(view, request, pk=device.pk)

        assert not Module.objects.filter(module_bay=hidden_bay).exists()
        assert message_texts(request) == ["Skipped 2: Hidden Scope Bay: no matching type; Child: no matching bay"]

    def test_interface_outside_change_grant_is_not_bound_to_module(self, live_librenms):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("module-interface-scope", librenms_cf={"default": 2})
        bay = make_module_bay(device, "Interface Scope Bay")
        module = install_module(device, bay.name, "INTERFACE-SCOPE-CARD")
        hidden = make_interface(device, "Te1/1/1")
        allowed = make_interface(device, "Te1/1/2")
        seed_mapping(hidden, own=42)
        user = make_user_with_perms("module-interface-scope", [("view", Device), ("view", Module), ("view", Interface)])
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        user = grant(user, "change", Interface, name="module-interface-scope-wide")
        item = _inventory_item(
            77,
            module.module_type.model,
            bay.name,
            _librenms_port_id=42,
            _librenms_ifname=hidden.name,
        )
        # The page renders while the wide grant reaches the holder; the grant then narrows before the click.
        _view_unused, rendered = _rendered_post(
            UpdateModuleInterfaceView, device, [item], 2, live_librenms, "update-module-interface", 77, user=user
        )
        user = _without_grants(user, "module-interface-scope-wide")
        request = make_request("post", rendered.POST.dict(), user=user, path="/modules/update-interface/")
        view = _view(UpdateModuleInterfaceView, request, live_librenms)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code in {200, 302}
        reported_messages = message_texts(request)
        assert any("matching interface is not available for port_id 42" in text for text in reported_messages)
        assert "No cached inventory data" not in " ".join(reported_messages)
        hidden.refresh_from_db()
        assert hidden.module_id is None


class TestModuleBindLockConflicts:
    """
    A lock conflict in a module bind rolls back the whole action, so the middleware's "try again" is true.

    The busy claim is real: a second connection holds the claim on the port in its open transaction.
    """

    @staticmethod
    def _bound_item(index, module_type, bay, interface, port_id):
        return _inventory_item(
            index, module_type.model, bay.name, _librenms_port_id=port_id, _librenms_ifname=interface.name
        )

    def test_the_install_rolls_back_when_the_bind_meets_a_busy_port_claim(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("bind-busy-install", librenms_cf={"default": 81})
        bay = make_module_bay(device, "Busy Install Bay")
        module_type = make_module_type("BUSY-INSTALL-CARD")
        interface = make_interface(device, "Ethernet81")
        item = self._bound_item(810, module_type, bay, interface, 8101)
        view, request = _rendered_post(InstallModuleView, device, [item], 81, live_librenms, "install-module", 810)

        with second_connection() as other:
            hold_port_claim(other, 8101, "default")
            with pytest.raises(LibreNMSPortBindingBusy):
                view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert not Module.objects.filter(device=device, module_bay=bay).exists()
        assert read_mapping(interface).own_id("default") is None

    def test_the_replace_rolls_back_when_the_bind_meets_a_busy_port_claim(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("bind-busy-replace", librenms_cf={"default": 82})
        bay = make_module_bay(device, "Busy Replace Bay")
        installed = install_module(device, bay.name, "BUSY-REPLACE-OLD")
        new_type = make_module_type("BUSY-REPLACE-NEW")
        interface = make_interface(device, "Ethernet82")
        item = self._bound_item(820, new_type, bay, interface, 8201)
        view = ReplaceModuleView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, [item], librenms_id=82)
        _action, fields = rendered_modal_form(device, 820, "replace-module")
        request = _post_request(fields)
        view.setup(request)

        with second_connection() as other:
            hold_port_claim(other, 8201, "default")
            with pytest.raises(LibreNMSPortBindingBusy):
                view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert list(Module.objects.filter(device=device, module_bay=bay)) == [installed]
        assert read_mapping(interface).own_id("default") is None

    def test_the_interface_update_raises_a_busy_port_claim(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("bind-busy-update", librenms_cf={"default": 83})
        bay = make_module_bay(device, "Busy Update Bay")
        module = install_module(device, bay.name, "BUSY-UPDATE-CARD")
        interface = make_interface(device, "Ethernet83")
        item = self._bound_item(830, module.module_type, bay, interface, 8301)
        view, request = _rendered_post(
            UpdateModuleInterfaceView, device, [item], 83, live_librenms, "update-module-interface", 830
        )

        with second_connection() as other:
            hold_port_claim(other, 8301, "default")
            with pytest.raises(LibreNMSPortBindingBusy):
                view_post(view, request, pk=device.pk)

        interface.refresh_from_db()
        assert interface.module_id is None
        assert read_mapping(interface).own_id("default") is None

    @transactional_db_with_all_apps()
    def test_a_lock_conflict_in_the_adoption_rolls_back_the_bind_too(self, live_librenms):
        """The bind and the adoption share one transaction: a conflict must not leave the bind committed alone."""
        from dcim.models import Interface, InterfaceTemplate

        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("adopt-lock-update", librenms_cf={"default": 84})
        bay = make_module_bay(device, "Adopt Lock Bay")
        module = install_module(device, bay.name, "ADOPT-LOCK-CARD")
        InterfaceTemplate.objects.create(module_type=module.module_type, name="c2/1", type="other")
        primary = make_interface(device, "Ethernet84")
        standalone = make_interface(device, "c2/1")
        item = self._bound_item(840, module.module_type, bay, primary, 8401)
        view, request = _rendered_post(
            UpdateModuleInterfaceView, device, [item], 84, live_librenms, "update-module-interface", 840
        )

        with second_connection() as other:
            lock_row(other, Interface, standalone.pk)
            with lock_timeout(200), pytest.raises(OperationalError) as caught:
                view_post(view, request, pk=device.pk)

        primary.refresh_from_db()
        standalone.refresh_from_db()
        assert classify_conflict(caught.value)
        assert (primary.module_id, standalone.module_id) == (None, None)
        assert read_mapping(primary).own_id("default") is None


class TestModulesRedirectResponse:
    """_modules_redirect_response: the classic (non-HTMX) redirect back to the modules tab."""

    def test_classic_request_uses_redirect(self):
        """A plain form post is sent back to the modules tab anchor."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({}), "/sync/")

        assert response.status_code == 302
        assert response.url == "/sync/?tab=modules#librenms-module-table"

    def test_explicit_server_key_is_appended(self):
        """A server-scoped action returns the user to the cache namespace it just read or mutated."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({}), "/sync/", server_key="prod server")

        # quote_plus encodes the value and the fragment stays last.
        assert response.url == "/sync/?tab=modules&server_key=prod+server#librenms-module-table"

    def test_server_key_read_from_post_when_not_passed(self):
        """A call site that resolves no key still propagates the posted server context."""
        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        response = _modules_redirect_response(_post_request({"server_key": "production"}), "/sync/")

        assert response.url == "/sync/?tab=modules&server_key=production#librenms-module-table"


class TestModulesActionResponse:
    """Module actions swap the module tab in place over HTMX and keep the classic redirect."""

    SERVER_KEY = "prod"

    def _configure_server(self, settings):
        """Configure one LibreNMS server the module actions can resolve."""
        configure_librenms_servers(
            settings,
            {self.SERVER_KEY: {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}},
        )

    def _cache_key(self, device):
        """Return the module tab's inventory cache key for this class's server."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        return DeviceModuleTableView().get_cache_key(device, "inventory", server_key=self.SERVER_KEY)

    def _seed_inventory(
        self,
        device,
        bay,
        module_type,
        *,
        serial="ACTION-1",
        librenms_id=9201,
    ):
        """Seed one inventory row matching the bay and module type under the module tab's cache key."""
        inventory_item = {
            "entPhysicalIndex": 8201,
            "entPhysicalClass": "module",
            "entPhysicalModelName": module_type.model,
            "entPhysicalContainedIn": 0,
            "entPhysicalName": bay.name,
            "entPhysicalSerialNum": serial,
        }
        payload = trusted_module_inventory_payload(
            device,
            [inventory_item],
            server_key=self.SERVER_KEY,
            librenms_id=librenms_id,
        )
        cache.set(self._cache_key(device), payload, 300)
        return inventory_item

    def test_htmx_install_swaps_the_module_tab_in_place(self, client, settings, django_capture_on_commit_callbacks):
        """An HTMX install answers with the module tab fragment instead of navigating the browser."""
        from dcim.models import Module
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-install")
        bay = make_module_bay(device, "Action Bay")
        module_type = make_module_type("ACTION-CARD")
        self._seed_inventory(device, bay, module_type)
        user = make_superuser("modules-action-install-user")
        client.force_login(user)
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})
        fields = _rendered_fields(device, "install-module", 8201, user=user, server_key=self.SERVER_KEY)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(url, fields, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert response["HX-Reswap"] == "innerHTML"
        assert "HX-Redirect" not in response
        assert "HX-Refresh" not in response
        trigger = json.loads(response["HX-Trigger"])
        assert "closeModal" in trigger
        assert "librenmsCacheChanged" in trigger
        body = response.content.decode()
        assert 'id="librenms-module-table"' in body
        assert f'name="server_key" value="{self.SERVER_KEY}"' in body
        assert f"Installed {module_type.model} in {bay.name}" in body
        assert '<span class="badge bg-success text-white">Installed</span>' in body
        assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()

    def test_legacy_binding_source_metadata_does_not_render_a_warning(
        self,
        client,
        settings,
        django_capture_on_commit_callbacks,
    ):
        """Obsolete cached binding metadata must not add a warning to a successful install."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import (
            make_device,
            make_module_bay,
            make_module_type,
            make_superuser,
        )
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        self._configure_server(settings)
        device = make_device("modules-action-legacy-binding-source")
        bay = make_module_bay(device, "Legacy Bay")
        module_type = make_module_type("LEGACY-CARD")
        # Current producers cannot write this marker. A cache from an older process can still carry it
        # during deployment, so the consumer must ignore it instead of reviving the removed warning.
        inventory_item = {
            "entPhysicalIndex": 8201,
            "entPhysicalClass": "module",
            "entPhysicalModelName": module_type.model,
            "entPhysicalContainedIn": 0,
            "entPhysicalName": bay.name,
            "entPhysicalSerialNum": "LEGACY-ACTION-1",
            "_binding_source": "post_fallback",
        }
        payload = trusted_module_inventory_payload(
            device,
            [inventory_item],
            server_key=self.SERVER_KEY,
            librenms_id=9205,
        )
        cache_key = DeviceModuleTableView().get_cache_key(device, "inventory", server_key=self.SERVER_KEY)
        cache.set(cache_key, payload, 300)
        user = make_superuser("modules-action-legacy-binding-source-user")
        client.force_login(user)
        fields = _rendered_fields(device, "install-module", 8201, user=user, server_key=self.SERVER_KEY)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk}),
                fields,
                HTTP_HX_REQUEST="true",
            )

        assert response.status_code == 200
        assert "Interface identity fallback used" not in response.content.decode()

    def test_htmx_action_keeps_the_page_and_sort_of_the_current_url(self, client, settings):
        """The re-rendered table honours the page's own query (page, per_page), not the action URL's empty one."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-paged")
        module_type = make_module_type("PAGED-CARD")
        # NetBox's EnhancedPaginator folds up to 5 orphans into the last page, so 7 rows make a real page 2.
        bays = [make_module_bay(device, f"Bay {number:02d}") for number in range(1, 8)]
        payload = trusted_module_inventory_payload(
            device,
            [
                {
                    "entPhysicalIndex": 8200 + number,
                    "entPhysicalClass": "module",
                    "entPhysicalModelName": module_type.model,
                    "entPhysicalContainedIn": 0,
                    "entPhysicalName": bay.name,
                    "entPhysicalSerialNum": f"PAGED-{number}",
                }
                for number, bay in enumerate(bays, start=1)
            ],
            server_key=self.SERVER_KEY,
            librenms_id=9204,
        )
        cache.set(self._cache_key(device), payload, 300)
        client.force_login(make_superuser())
        sync_page = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk})
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {
                "server_key": self.SERVER_KEY,
                "module_bay_id": str(bays[0].pk),
                "module_type_id": str(module_type.pk),
                "serial": "PAGED-1",
            },
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL=(
                f"http://testserver{sync_page}?tab=modules&server_key={self.SERVER_KEY}"
                "&modules_per_page=1&modules_page=2#librenms-module-table"
            ),
        )

        assert response.status_code == 200
        body = response.content.decode()
        table = body.split('id="librenms-module-table"', 1)[1]
        assert "Bay 02" in table
        assert "Bay 01" not in table
        assert "modules_page=1" in body

    def test_classic_install_still_redirects_to_the_modules_tab(self, client, settings):
        """Without the HTMX header the same install keeps the server-scoped redirect contract."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-classic")
        bay = make_module_bay(device, "Classic Bay")
        module_type = make_module_type("CLASSIC-CARD")
        self._seed_inventory(device, bay, module_type, librenms_id=9202)
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {
                "server_key": self.SERVER_KEY,
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "ACTION-1",
            },
        )

        assert response.status_code == 302
        assert response.url.endswith(f"?tab=modules&server_key={self.SERVER_KEY}#librenms-module-table")

    def test_htmx_serial_update_from_the_modal_is_retargeted(self, client, settings):
        """A mismatch-modal action, which swaps nothing itself, is retargeted at the module tab."""
        from dcim.models import Module

        self._configure_server(settings)
        device = make_device("modules-action-serial")
        bay = make_module_bay(device, "Serial Bay")
        module_type = make_module_type("SERIAL-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD-SERIAL")
        self._seed_inventory(device, bay, module_type, librenms_id=9203)
        user = make_superuser()
        client.force_login(user)
        url, fields = rendered_modal_form(device, 8201, "update-module-serial", user=user, server_key=self.SERVER_KEY)

        response = client.post(url, fields, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert response["HX-Reswap"] == "innerHTML"
        body = response.content.decode()
        assert 'id="librenms-module-table"' in body
        assert f"Updated serial for {module_type.model} in {bay.name}" in body
        module.refresh_from_db()
        assert module.serial == "ACTION-1"

    def test_htmx_action_without_a_snapshot_renders_the_empty_tab(self, client, settings):
        """A missing inventory snapshot reports the error inside the re-rendered tab."""
        from django.urls import reverse

        self._configure_server(settings)
        device = make_device("modules-action-no-snapshot")
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})

        response = client.post(
            url,
            {"server_key": self.SERVER_KEY, "select": "8201"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert response["HX-Retarget"] == "#module-sync-content"
        assert "HX-Redirect" not in response
        body = response.content.decode()
        assert "No cached inventory data" in body
        assert "Refresh Modules" in body

    def _seed_serial_mismatch(self, suffix, *, conflict, librenms_id):
        """Seed a device whose installed module's serial differs from the cached LibreNMS serial."""
        from dcim.models import Module

        device = make_device(f"modules-preview-{suffix}")
        bay = make_module_bay(device, "Preview Bay")
        module_type = make_module_type(f"PREVIEW-CARD-{suffix}")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD-SERIAL")
        self._seed_inventory(device, bay, module_type, librenms_id=librenms_id)
        if conflict:
            other = make_device(f"modules-preview-{suffix}-holder")
            other_bay = make_module_bay(other, "Holder Bay")
            Module.objects.create(device=other, module_bay=other_bay, module_type=module_type, serial="ACTION-1")
        return device, module

    @pytest.mark.parametrize(
        ("conflict", "expected_action"),
        [(False, "update-module-serial"), (True, "move-module")],
        ids=["update_serial_only", "move"],
    )
    def test_the_mismatch_preview_answers_a_whole_modal_of_bound_forms(
        self, client, settings, conflict, expected_action
    ):
        """The preview fills #htmx-modal-content, so it must carry the header the JS used to build."""

        from netbox_librenms_plugin.tests._html_helpers import open_tags

        self._configure_server(settings)
        suffix = "move" if conflict else "serial"
        device, module = self._seed_serial_mismatch(suffix, conflict=conflict, librenms_id=9210 + int(conflict))
        client.force_login(make_superuser())

        response = client.get(rendered_preview_url(render_module_tab(device, server_key=self.SERVER_KEY), 8201))

        assert response.status_code == 200
        body = response.content.decode()
        # The shell owns id="htmx-modal-label"; a second copy here would duplicate the id.
        assert 'id="htmx-modal-label"' not in body
        assert 'class="modal-title"' in body
        assert "closeHtmxModal()" in body
        assert 'class="modal-body"' in body
        forms = open_tags(body, "form")
        # Both branches render the replace form plus the one the conflict state selects.
        assert len(forms) == 2
        assert any(expected_action in form["action"] for form in forms)
        assert any("replace-module" in form["action"] for form in forms)
        for form in forms:
            # The forms are swapped into the modal, so their own target must be the module tab.
            assert form["hx-target"] == "#module-sync-content"
            assert form["hx-swap"] == "innerHTML"
            assert form["hx-sync"] == "#module-sync-content:drop"

    def test_the_serial_only_form_submits_the_rendered_inventory_identity(self, client, settings):
        """The rendered serial-only form must submit enough identity to update its cached row."""
        from django.urls import reverse

        from netbox_librenms_plugin.tests._html_helpers import open_tags
        from netbox_librenms_plugin.tests.conftest import make_superuser

        self._configure_server(settings)
        device, module = self._seed_serial_mismatch("serial-submit", conflict=False, librenms_id=9212)
        client.force_login(make_superuser("modules-preview-serial-submit-user"))
        preview = client.get(rendered_preview_url(render_module_tab(device, server_key=self.SERVER_KEY), 8201))
        body = preview.content.decode()
        update_url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})
        form_body = next(
            form for form in re.findall(r"<form\b.*?</form>", body, flags=re.DOTALL) if f'action="{update_url}"' in form
        )
        payload = {
            field["name"]: field.get("value", "")
            for field in open_tags(form_body, "input")
            if field.get("name") != "csrfmiddlewaretoken"
        }

        response = client.post(
            update_url,
            payload,
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        module.refresh_from_db()
        assert module.serial == "ACTION-1"


class TestAddBayTemplatePostValidation:
    """AddBayTemplateView refuses a tampered target_kind before it touches any object."""

    def test_invalid_target_kind_returns_400(self, client):
        """The modal echoes target_kind from its GET render, so a bad value is a tampered request."""
        from dcim.models import ModuleBayTemplate
        from django.urls import reverse

        device = make_device("add-bay-template-tampered")
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

        response = client.post(url, {"target_kind": "bogus", "target_pk": "1", "name": "Slot 1"})

        assert response.status_code == 400
        assert b"Invalid target_kind" in response.content
        assert not ModuleBayTemplate.objects.filter(name="Slot 1").exists()

    def test_a_lock_conflict_on_the_new_bay_gives_the_try_again_answer(self, client):
        """NetBox 4.7 turns a deadlock in a tree save into a ValidationError; it is still a conflict."""
        from dcim.models import ModuleBay, ModuleBayTemplate
        from django.urls import reverse

        from netbox_librenms_plugin.tests.view_test_helpers import messages_on

        device = make_device("add-bay-template-conflict")
        make_module_bay(device, "Slot 1")
        module = install_module(device, "Slot 1", "BAY-CONFLICT-CARD")
        client.force_login(make_superuser())
        url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

        with failing_statement(lambda sql, params: sql.startswith('INSERT INTO "dcim_modulebay"'), "40P01") as failed:
            response = client.post(
                url,
                {"target_kind": "module_type", "target_pk": str(module.module_type.pk), "name": "Sub 1"},
                HTTP_REFERER=f"http://testserver{reverse('dcim:device', kwargs={'pk': device.pk})}",
            )

        assert failed, "precondition: the bay insert ran"
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
        assert not ModuleBayTemplate.objects.filter(module_type=module.module_type, name="Sub 1").exists()
        assert not ModuleBay.objects.filter(module=module, name="Sub 1").exists()


class TestVirtualChassisInterfaceNormalization:
    def _module(self, device, name):
        from dcim.models import Module

        bay = make_module_bay(device, f"{name} Bay")
        return Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=make_module_type(name),
            status="active",
        )

    def test_interface_name_is_rewritten_to_the_member_position(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        first = make_device("normalize-first")
        device = make_device("normalize-second")
        make_virtual_chassis("normalize-vc", first, device)
        module = self._module(device, "NORMALIZE-CARD")
        interface = Interface.objects.create(device=device, module=module, name="Te1/1/1", type="other")

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        interface.refresh_from_db()
        assert result == {"renamed": 1, "adopted": 0, "removed": 0, "skipped": 0}
        assert interface.name == "Te2/1/1"

    def test_existing_desired_name_is_adopted_and_generated_duplicate_removed(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        first = make_device("normalize-adopt-first")
        device = make_device("normalize-adopt-second")
        make_virtual_chassis("normalize-adopt-vc", first, device)
        module = self._module(device, "NORMALIZE-ADOPT-CARD")
        generated = Interface.objects.create(device=device, module=module, name="Te1/1/2", type="other")
        standalone = make_interface(device, "Te2/1/2")

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        standalone.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 1, "removed": 1, "skipped": 0}
        assert standalone.module == module
        assert not Interface.objects.filter(pk=generated.pk).exists()

    def _member_module_interface(self, tag, name):
        from dcim.models import Interface

        first = make_device(f"{tag}-first")
        device = make_device(f"{tag}-second")
        make_virtual_chassis(f"{tag}-vc", first, device)
        module = self._module(device, f"{tag.upper()}-CARD")
        return device, module, Interface.objects.create(device=device, module=module, name=name, type="other")

    @pytest.mark.parametrize(
        "statement, standalone",
        [('UPDATE "dcim_interface"', False), ('DELETE FROM "dcim_interface"', True)],
        ids=["rename", "remove"],
    )
    def test_a_lock_conflict_in_a_rename_or_a_removal_is_raised(self, statement, standalone):
        """The caller's transaction must roll back: a skipped count would hide the conflict."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device, module, interface = self._member_module_interface(f"normalize-conflict-{int(standalone)}", "Te1/1/3")
        if standalone:
            make_interface(device, "Te2/1/3")

        with pytest.raises(OperationalError) as caught, transaction.atomic():
            with failing_statement(lambda sql, params: sql.startswith(statement), "40P01"):
                _normalize_module_interface_names_for_vc_member(
                    device, module, Interface.objects.all(), Interface.objects.all()
                )

        assert classify_conflict(caught.value)
        interface.refresh_from_db()
        assert interface.name == "Te1/1/3"

    def test_a_rename_that_fails_for_another_reason_is_skipped_in_its_own_savepoint(self):
        """The failed rename rolls back alone, so the caller's transaction stays usable and commits the rest."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device, module, interface = self._member_module_interface("normalize-failed-rename", "Te1/1/4")

        with failing_statement(lambda sql, params: sql.startswith('UPDATE "dcim_interface"'), "23505"):
            result = _normalize_module_interface_names_for_vc_member(
                device, module, Interface.objects.all(), Interface.objects.all()
            )

        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        interface.refresh_from_db()
        assert interface.name == "Te1/1/4"

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ({}, ""),
            ({"renamed": 2, "adopted": 1, "removed": 1, "skipped": 0}, "renamed 2, adopted 1, removed 1"),
            ({"skipped": 3}, "skipped 3"),
        ],
    )
    def test_adjustment_summary_reports_nonzero_actions(self, counts, expected):
        from netbox_librenms_plugin.views.sync.modules import _format_vc_adjustment_summary

        assert _format_vc_adjustment_summary(counts) == expected


class TestModuleComponentAdoption:
    """Authorize standalone adoption for each component type NetBox replicates."""

    @staticmethod
    def _couples_rear_port(model):
        return any(field.name == "rear_port" for field in model._meta.get_fields())

    @staticmethod
    def _type_kwargs(model_name):
        if "Interface" in model_name:
            return {"type": "1000base-t"}
        if "FrontPort" in model_name or "RearPort" in model_name:
            return {"type": "8p8c"}
        return {}

    def test_matching_standalone_component_is_authorized(self, spec_index):
        from dcim.constants import MODULE_TOKEN
        from dcim.models import Module, RearPort, RearPortTemplate

        from netbox_librenms_plugin.views.sync.modules import (
            _authorize_adoptable_module_components,
            _module_component_specs,
            _module_template_adoption_name,
        )

        specs = _module_component_specs()
        template_attribute, _component_attribute, component_model = specs[spec_index]
        device = make_device(f"adopt-{component_model.__name__.lower()}")
        module_type = make_module_type(f"ADOPT-{component_model.__name__}")
        template_model = getattr(type(module_type), template_attribute).rel.related_model
        template_kwargs = {
            "module_type": module_type,
            "name": f"{MODULE_TOKEN}-adopt-{component_model.__name__.lower()}",
            **self._type_kwargs(template_model.__name__),
        }
        if template_model.__name__ == "FrontPortTemplate" and self._couples_rear_port(template_model):
            template_kwargs["rear_port"] = RearPortTemplate.objects.create(
                module_type=module_type,
                name=f"rear-template-{spec_index}",
                type="8p8c",
            )
            template_kwargs["rear_port_position"] = 1
        template = template_model.objects.create(**template_kwargs)
        bay = make_module_bay(device, f"Adopt Bay {spec_index}")
        bay.position = "A1"
        bay.save(update_fields=["position"])
        module = Module(device=device, module_bay=bay, module_type=module_type)
        expected_name = _module_template_adoption_name(template_attribute, template, module)
        component_kwargs = {
            "device": device,
            "name": expected_name,
            **self._type_kwargs(component_model.__name__),
        }
        if component_model.__name__ == "FrontPort" and self._couples_rear_port(component_model):
            component_kwargs["rear_port"] = RearPort.objects.create(
                device=device,
                name=f"rear-component-{spec_index}",
                type="8p8c",
            )
            component_kwargs["rear_port_position"] = 1
        standalone = component_model.objects.create(**component_kwargs)

        allowed = {model: model.objects.all() for _, _, model in specs}
        authorized = _authorize_adoptable_module_components(module, allowed)

        assert authorized[component_model] == {standalone.pk}


class TestModuleInterfaceMessages:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (
                {"status": "bound", "interface": "Ethernet1", "port_id": 1},
                "Updated interface Ethernet1 for CARD in Slot 1.",
            ),
            (
                {"status": "bound", "adopted_count": 2},
                "Updated interfaces for CARD in Slot 1: adopted 2 existing standalone interface(s).",
            ),
            (
                {"status": "bound", "interface": "Ethernet1", "adopted_count": 2},
                "Updated interface Ethernet1 for CARD in Slot 1 and adopted 2 existing standalone interface(s).",
            ),
            (
                {"status": "bound", "changed": False, "adopted_count": 0},
                "No interface changes were needed for CARD in Slot 1.",
            ),
            (
                {"status": "bound"},
                "No interface changes were needed for CARD in Slot 1.",
            ),
        ],
    )
    def test_message_describes_the_real_mutation(self, result, expected):
        from netbox_librenms_plugin.views.sync.modules import _module_interface_update_message

        assert _module_interface_update_message(result, "CARD in Slot 1") == expected

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ({"status": "installed", "module_pk": 1}, True),
            ({"status": "installed", "module_pk": None}, False),
            ({"status": "skipped", "module_pk": 1, "reason": "bay already occupied"}, True),
            ({"status": "skipped", "module_pk": 1, "reason": "no matching type"}, False),
        ],
    )
    def test_bind_attempt_requires_a_stable_module_context(self, result, expected):
        from netbox_librenms_plugin.views.sync.modules import _should_attempt_bind_for_result

        assert _should_attempt_bind_for_result(result) is expected


def test_module_interface_prediction_signal_uses_real_templates():
    from dcim.models import InterfaceTemplate, Module
    from django.dispatch import receiver

    from netbox_librenms_plugin.signals import predict_module_interface_names
    from netbox_librenms_plugin.utils import get_module_adoptable_interface_names

    device = make_device("prediction-device")
    module_type = make_module_type("PREDICTION-CARD")
    InterfaceTemplate.objects.create(module_type=module_type, name="Ethernet1", type="other")
    bay = make_module_bay(device, "Prediction Bay")
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")

    @receiver(predict_module_interface_names)
    def rewrite(sender, device, module, names, **kwargs):
        return [f"{name}/child" for name in names]

    try:
        assert get_module_adoptable_interface_names(device, module) == (["Ethernet1/child"], False)
    finally:
        predict_module_interface_names.disconnect(rewrite)


def test_inventory_rows_are_json_serializable_for_cache_boundaries():
    row = _inventory_item(1, "CARD", "Slot 1", serial="SERIAL")

    assert json.loads(json.dumps(row)) == row


@pytest.mark.django_db
@pytest.mark.parametrize("mapping_kind", ["regex", "exact"])
def test_existing_bay_mapping_reuses_pattern_proposal_without_creating_bays(client, settings, mapping_kind):
    """Choose a free bay, preview a family rule, and save only the mapping."""
    from dcim.models import ModuleBay, ModuleBayTemplate
    from django.urls import reverse

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_superuser

    device = make_device_with_module_bays("existing-bay-mapping", ["RE0", "RE1"])
    client.force_login(make_superuser("existing-bay-mapping-user"))
    url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})
    inputs = {"mode": "map_existing", "librenms_name": "Routing Engine 0", "librenms_class": "other"}
    response = client.get(url, inputs)
    assert response.status_code == 200
    assert response.context["available_bay_names"] == ["RE0", "RE1"]
    assert b"Map Existing Bay" in response.content
    # The family rule is previewed on step two, once the operator has chosen the bay.
    review = client.get(url, {**inputs, "step": "kind", "name": "RE0"})
    assert review.status_code == 200
    assert review.context["mapping_pattern"]["netbox_replacement"] == r"RE\1"
    before_bays = list(ModuleBay.objects.values())
    before_templates = list(ModuleBayTemplate.objects.values())
    response = client.post(url, {**inputs, "name": "RE0", "mapping_kind": mapping_kind})
    assert response.status_code == 302
    mapping = ModuleBayMapping.objects.get(manufacturer=device.device_type.manufacturer, librenms_class="other")
    assert mapping.is_regex == (mapping_kind == "regex")
    if mapping_kind == "regex":
        import re

        assert re.fullmatch(mapping.librenms_name, "Routing Engine 1")
        assert re.sub(mapping.librenms_name, mapping.netbox_bay_name, "Routing Engine 1") == "RE1"
    else:
        assert mapping.librenms_name == "Routing Engine 0"
        assert mapping.netbox_bay_name == "RE0"
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    matcher = BaseModuleTableView()
    matcher._current_manufacturer_id = device.device_type.manufacturer_id
    bays = {bay.name: bay for bay in device.modulebays.all()}
    for index in (0, 1):
        item = {
            "entPhysicalName": "ROUTING-CARD",
            "entPhysicalDescr": f"Routing Engine {index}",
            "entPhysicalClass": "other",
        }
        matched = matcher._match_module_bay(item, {}, bays)
        if index == 0 or mapping_kind == "regex":
            assert matched == bays[f"RE{index}"]
        else:
            assert matched is None
    assert list(ModuleBay.objects.values()) == before_bays
    assert list(ModuleBayTemplate.objects.values()) == before_templates


@pytest.mark.django_db
@pytest.mark.parametrize("inventory_name", ["Routing Engine 0", ""])
def test_unmatched_inventory_offers_existing_bay_mapping_on_the_sync_page(client, settings, inventory_name):
    """A missing automatic suggestion must not hide the mapping proposal modal."""
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    TestModulesActionResponse()._configure_server(settings)
    device = make_device_with_module_bays("unmatched-existing-bay", ["RE0", "RE1"])
    module_type = make_module_type("ROUTING-CARD", manufacturer=device.device_type.manufacturer)
    payload = trusted_module_inventory_payload(
        device,
        [
            {
                "entPhysicalIndex": 71,
                "entPhysicalClass": "module",
                "entPhysicalName": inventory_name,
                "entPhysicalDescr": "Routing Engine 0",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": "ROUTING-1",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 72,
                "entPhysicalClass": "port",
                "entPhysicalName": "Nested Optic",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": "OPTIC-1",
                "entPhysicalContainedIn": 71,
            },
        ],
        server_key="prod",
        librenms_id=9201,
    )
    cache.set(DeviceModuleTableView().get_cache_key(device, "inventory", server_key="prod"), payload, 300)
    cache.set("librenms_device_info_prod_9201", (True, {"device_id": 9201, "hostname": device.name}), 300)
    client.force_login(make_superuser("unmatched-existing-bay-user"))
    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "modules", "server_key": "prod"},
    )
    assert response.status_code == 200
    assert b"Routing Engine 0" in response.content
    assert b'data-action="map-existing-bay"' in response.content
    assert b"Nested Optic" in response.content
    mapping_rows = [
        row.record["name"]
        for row in response.context["module_sync"]["table"].rows
        if "mode=map_existing" in str(row.get_cell("actions"))
    ]
    assert mapping_rows == [inventory_name or "-"]
    assert "librenms_name=Routing+Engine+0" in str(response.context["module_sync"]["table"].rows[0].get_cell("actions"))


def _map_existing_modal(client, device, **params):
    """GET the map-existing modal for *device*, returning the response."""
    from django.urls import reverse

    query = {"mode": "map_existing", "librenms_name": "Routing Engine 0", "librenms_class": "other"}
    query.update(params)
    return client.get(reverse("plugins:netbox_librenms_plugin:add_bay_template", args=[device.pk]), query)


def _checked_mapping_kind(html):
    """Return the mapping_kind value the rendered form has checked, or None."""
    import re as _re

    checked = [
        _re.search(r'value="(\w+)"', tag).group(1)
        for tag in _re.findall(r"<input[^>]*name=\"mapping_kind\"[^>]*>", html)
        if "checked" in tag
    ]
    assert len(checked) <= 1, f"more than one mapping_kind is checked: {checked}"
    return checked[0] if checked else None


def _mapping_user(name):
    from dcim.models import Device, ModuleBay

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    return make_user_with_perms(name, [("view", Device), ("view", ModuleBay), ("add", ModuleBayMapping)])


@pytest.mark.django_db
def test_the_map_existing_modal_chooses_the_bay_before_the_mapping_kind(client):
    """
    Step one only picks a bay: nothing is preselected and no kind is offered yet.

    The kind used to be decided against the alphabetically first bay, so an unrelated bay could
    force the exact default onto a whole slot family. Deferring it removes that guess.
    """
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device = make_device_with_module_bays("map-existing-step-one", ["LCMIC1", "RE0", "RE1"])
    client.force_login(_mapping_user("map-existing-step-one-user"))

    response = _map_existing_modal(client, device)

    assert response.status_code == 200
    assert response.context["mapping_step"] == "bay"
    html = response.content.decode()
    # No bay is preselected: the operator has to choose one.
    assert 'value="" selected' in html
    assert 'value="LCMIC1" selected' not in html
    # The kind belongs to step two, so it must not be decidable here.
    assert 'name="mapping_kind"' not in html
    # The rendered fragment must actually be able to reach step two with the chosen bay.
    assert 'id="add-bay-next"' in html
    assert "step=kind" in html
    assert 'hx-include="#add-bay-name"' in html


@pytest.mark.django_db
def test_the_map_existing_modal_derives_the_kind_from_the_chosen_bay(client):
    """
    The reported MX304 case: RE0 derives a family pattern, so regex is the honest default.

    LCMIC1 sorts first and derives nothing from "Routing Engine 0", which is exactly what used
    to force the exact default and leave the operator with a one-bay rule.
    """
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device = make_device_with_module_bays("map-existing-step-two", ["LCMIC1", "RE0", "RE1"])
    client.force_login(_mapping_user("map-existing-step-two-user"))

    response = _map_existing_modal(client, device, step="kind", name="RE0")

    assert response.status_code == 200
    assert response.context["mapping_step"] == "kind"
    assert response.context["chosen_name"] == "RE0"
    assert response.context["mapping_default_kind"] == "regex"
    assert response.context["mapping_pattern"]["netbox_replacement"] == r"RE\1"
    html = response.content.decode()
    # The regex radio must be the checked one; "checked" appearing anywhere would also pass if
    # the server had preselected exact.
    assert _checked_mapping_kind(html) == "regex"
    # The bay travels to the POST as a hidden field, so it cannot drift from what was reviewed.
    assert '<input type="hidden" name="name" value="RE0">' in html


@pytest.mark.django_db
def test_the_map_existing_modal_offers_exact_only_when_no_pattern_derives(client):
    """A bay carrying a digit the LibreNMS name lacks supports no family rule."""
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device = make_device_with_module_bays("map-existing-exact-only", ["LCMIC1", "RE0"])
    client.force_login(_mapping_user("map-existing-exact-only-user"))

    response = _map_existing_modal(client, device, step="kind", name="LCMIC1")

    assert response.status_code == 200
    assert response.context["mapping_pattern"] is None
    assert response.context["mapping_default_kind"] == "exact"
    html = response.content.decode()
    assert 'id="add-bay-mapping-kind-regex"' not in html
    assert _checked_mapping_kind(html) == "exact"


@pytest.mark.django_db
def test_the_map_existing_modal_falls_back_to_the_chooser_for_an_unavailable_bay(client):
    """A bay filled since the modal opened returns the operator to a fresh chooser."""
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device = make_device_with_module_bays("map-existing-stale-bay", ["RE0"])
    client.force_login(_mapping_user("map-existing-stale-bay-user"))

    response = _map_existing_modal(client, device, step="kind", name="Gone")

    assert response.status_code == 200
    assert response.context["mapping_step"] == "bay"
    assert response.context["chosen_name"] == ""


@pytest.mark.django_db
def test_saving_the_reviewed_regex_mapping_stores_the_family_rule(client):
    """End to end: the kind reviewed in step two is the rule that gets written."""
    from django.urls import reverse

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device = make_device_with_module_bays("map-existing-save", ["LCMIC1", "RE0", "RE1"])
    client.force_login(_mapping_user("map-existing-save-user"))

    # Submit the kind the modal itself checked, so a wrong server default fails this test too
    # rather than being papered over by a hardcoded "regex".
    review = _map_existing_modal(client, device, step="kind", name="RE0")
    reviewed_kind = _checked_mapping_kind(review.content.decode())
    assert reviewed_kind == "regex"

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:add_bay_template", args=[device.pk]),
        {
            "mode": "map_existing",
            "librenms_name": "Routing Engine 0",
            "librenms_class": "other",
            "name": "RE0",
            "mapping_kind": reviewed_kind,
        },
    )

    assert response.status_code in (200, 302)
    mapping = ModuleBayMapping.objects.get(librenms_class="other")
    assert mapping.is_regex is True
    assert mapping.librenms_name == r"^Routing\ Engine\ (\d+)$"
    assert mapping.netbox_bay_name == r"RE\1"


@pytest.mark.django_db
def test_the_map_existing_bay_button_needs_the_modals_view_permissions(client, settings):
    """The button must not offer a modal that the user's permissions then refuse."""
    from dcim.models import Device, ModuleBay
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    TestModulesActionResponse()._configure_server(settings)
    device = make_device_with_module_bays("map-existing-view-perms", ["RE0"])
    module_type = make_module_type("VIEWPERM-CARD", manufacturer=device.device_type.manufacturer)
    payload = trusted_module_inventory_payload(
        device,
        [
            {
                "entPhysicalIndex": 81,
                "entPhysicalClass": "module",
                "entPhysicalName": "Routing Engine 0",
                "entPhysicalDescr": "Routing Engine 0",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": "VIEWPERM-1",
                "entPhysicalContainedIn": 0,
            }
        ],
        server_key="prod",
        librenms_id=9203,
    )
    snapshot_key = DeviceModuleTableView().get_cache_key(device, "inventory", server_key="prod")
    device_info_key = "librenms_device_info_prod_9203"
    page_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    modal_url = reverse("plugins:netbox_librenms_plugin:add_bay_template", args=[device.pk])
    modal_inputs = {"mode": "map_existing", "librenms_name": "Routing Engine 0", "librenms_class": "other"}

    try:
        cache.set(snapshot_key, payload, 300)
        cache.set(device_info_key, (True, {"device_id": 9203, "hostname": device.name}), 300)

        # Positive control: with every permission _map_existing_bay requires, the modal opens and
        # the button is offered. Without it the negative case below could pass for any reason.
        allowed = make_user_with_perms(
            "map-existing-allowed",
            [("view", Device), ("view", ModuleBay), ("add", ModuleBayMapping)],
        )
        client.force_login(allowed)
        assert client.get(modal_url, modal_inputs).status_code == 200
        assert (
            b'data-action="map-existing-bay"' in client.get(page_url, {"tab": "modules", "server_key": "prod"}).content
        )

        # view_modulebay is the permission the button gate missed.
        denied = make_user_with_perms("map-existing-denied", [("view", Device), ("add", ModuleBayMapping)])
        client.force_login(denied)
        # Precondition: the modal really does refuse this user.
        assert client.get(modal_url, modal_inputs).status_code == 302
        # Effect: so the row must not render a button that leads there.
        assert (
            b'data-action="map-existing-bay"'
            not in client.get(page_url, {"tab": "modules", "server_key": "prod"}).content
        )
    finally:
        cache.delete(snapshot_key)
        cache.delete(device_info_key)


@pytest.mark.django_db
def test_existing_bay_mapping_requires_mapping_permission_but_not_template_creation(client):
    """Mapping an existing bay needs no permission to create templates or bays."""
    from dcim.models import Device, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    device = make_device_with_module_bays("mapping-permissions", ["RE0"])
    user = make_user_with_perms("mapping-permissions-user", [("view", Device), ("view", ModuleBay)])
    client.force_login(user)
    url = reverse("plugins:netbox_librenms_plugin:add_bay_template", args=[device.pk])
    inputs = {"mode": "map_existing", "librenms_name": "Routing Engine 0", "name": "RE0", "mapping_kind": "regex"}
    from django.contrib.messages import get_messages

    for response in (client.get(url, inputs), client.post(url, inputs)):
        assert response.status_code == 302
        assert any("add_modulebaymapping" in str(message) for message in get_messages(response.wsgi_request))
    assert not ModuleBayMapping.objects.filter(manufacturer=device.device_type.manufacturer).exists()
    client.force_login(grant(user, "add", ModuleBayMapping))
    assert client.get(url, inputs).status_code == 200
    assert client.post(url, inputs).status_code == 302
    assert ModuleBayMapping.objects.filter(manufacturer=device.device_type.manufacturer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["occupied", "other-device", "hidden"])
def test_existing_bay_mapping_rejects_unavailable_targets(client, target):
    """A forged target cannot select an occupied, foreign, or restricted bay."""
    from dcim.models import Device, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import ModuleBayMapping
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    device = make_device_with_module_bays("mapping-target-device", ["RE0", "RE1"])
    visible = device.modulebays.get(name="RE0")
    requested_name = "RE1"
    if target == "occupied":
        bay = device.modulebays.get(name="RE1")
        from dcim.models import Module

        Module.objects.create(
            device=device, module_type=make_module_type("MAPPING-CARD"), module_bay=bay, status="active"
        )
    elif target == "other-device":
        make_device_with_module_bays("mapping-other-device", ["RE2"])
        requested_name = "RE2"
    user = make_user_with_perms("mapping-target-user", [("view", Device), ("add", ModuleBayMapping)])
    user = grant(user, "view", ModuleBay, constraints={"pk": visible.pk} if target == "hidden" else None)
    client.force_login(user)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:add_bay_template", args=[device.pk]),
        {"mode": "map_existing", "librenms_name": "Routing Engine 0", "name": requested_name},
    )
    assert response.status_code == 400
    assert not ModuleBayMapping.objects.filter(manufacturer=device.device_type.manufacturer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("raw_serial, expected", [("S/N SERIAL123", "SERIAL123"), ("00123", "0123")])
def test_preview_and_replace_normalize_cached_serial_once(client, raw_serial, expected):
    """Preview and replacement must interpret raw cached serials like installation."""
    from dcim.models import Module
    from django.core.cache import cache

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    NormalizationRule.objects.filter(scope="serial").delete()
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^0(.*)$", replacement=r"\1")
    device = make_device("serial-normalization")
    module_type = make_module_type("serial-card", manufacturer=device.device_type.manufacturer)
    bay = make_module_bay(device, "Slot 1")
    installed = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD")
    key = CacheMixin().get_cache_key(device, "inventory", server_key="default")
    cache.set(
        key,
        trusted_module_inventory_payload(
            device,
            [
                {
                    "entPhysicalIndex": 100,
                    "entPhysicalClass": "module",
                    "entPhysicalName": "Slot 1",
                    "entPhysicalModelName": module_type.model,
                    "entPhysicalSerialNum": raw_serial,
                }
            ],
        ),
    )
    user = make_superuser()
    client.force_login(user)
    preview_url = rendered_preview_url(render_module_tab(device, user=user), 100)
    # The row reads as installed once normalized, so the stored serial lands after the page renders.
    Module.objects.filter(pk=installed.pk).update(serial=expected)
    preview = client.get(preview_url)
    assert preview.status_code == 200
    assert preview.context["librenms_serial"] == expected
    assert preview.context["serial_mismatch"] is False
    replace_url, replace_fields = rendered_module_form(preview.content.decode(), "replace-module", 100)
    response = client.post(replace_url, replace_fields)
    assert response.status_code == 302
    assert Module.objects.get(module_bay=bay).serial == expected
    assert cache.get(key)["inventory"][0]["entPhysicalSerialNum"] == raw_serial


@pytest.mark.django_db
@pytest.mark.parametrize(
    "endpoint,mixed_manufacturers",
    [("install_branch", False), ("install_selected", False), ("install_selected", True)],
)
def test_bulk_install_reads_serial_rules_once_per_manufacturer(client, endpoint, mixed_manufacturers):
    """A batch must normalize every serial without querying rules for every item."""
    from dcim.models import Manufacturer, Module, VirtualChassis
    from django.core.cache import cache
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    device = make_device_with_module_bays("serial-rule-batch", ["Slot 1", "Slot 2", "Slot 3"])
    module_type = make_module_type("Batch Card", manufacturer=device.device_type.manufacturer)
    member = None
    if mixed_manufacturers:
        manufacturer = Manufacturer.objects.create(name="Batch Vendor", slug="batch-vendor")
        member = make_device_with_module_bays("serial-rule-member", ["Slot 2"], manufacturer=manufacturer)
        chassis = VirtualChassis.objects.create(name="serial-rule-chassis", master=device)
        device.virtual_chassis = chassis
        device.vc_position = 1
        device.save()
        member.virtual_chassis = chassis
        member.vc_position = 2
        member.save()
    NormalizationRule.objects.filter(scope="serial").delete()
    NormalizationRule.objects.create(
        scope="serial", manufacturer=device.device_type.manufacturer, match_pattern=r"^S/N (.+)$", replacement=r"\1"
    )
    if member is not None:
        NormalizationRule.objects.create(
            scope="serial",
            manufacturer=member.device_type.manufacturer,
            match_pattern=r"^S/N (.+)$",
            replacement=r"MEMBER-\1",
        )
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "stack" if mixed_manufacturers else "chassis",
            "entPhysicalContainedIn": 0,
        }
    ]
    if endpoint == "install_branch":
        # A branch runs the list of one rendered row, so the slots sit below a carrier row.
        rows[0].update(entPhysicalClass="module", entPhysicalName="Carrier", entPhysicalModelName="BATCH-CARRIER")
    rows.extend(
        {
            "entPhysicalIndex": number + 1,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": f"Slot {number}",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": f"S/N BATCH-{number}",
        }
        for number in (1, 2, 3)
    )
    if member is not None:
        # The position attributes the slot to the member, so the table renders it there.
        rows[2]["entPhysicalParentRelPos"] = member.vc_position
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", "default"), trusted_module_inventory_payload(device, rows)
    )
    user = make_superuser("serial-rule-batch-user")
    if endpoint == "install_branch":
        data = _rendered_fields(device, "install-branch", 1, user=user)
    else:
        data = {**_rendered_fields(device, "install-selected", user=user), "select": ["2", "3", "4"]}
    if member is not None:
        data["device_selection_3"] = str(member.pk)
    client.force_login(user)
    with CaptureQueriesContext(connection) as queries:
        response = client.post(reverse(f"plugins:netbox_librenms_plugin:{endpoint}", args=[device.pk]), data)
    assert response.status_code == 302
    expected = {"BATCH-1", "BATCH-3"}
    if member is None:
        expected.add("BATCH-2")
    else:
        assert Module.objects.get(device=member).serial == "MEMBER-BATCH-2"
    assert set(Module.objects.filter(device=device).values_list("serial", flat=True)) == expected
    serial_queries = [
        query["sql"]
        for query in queries
        if "SELECT" in query["sql"] and "normalizationrule" in query["sql"] and "'serial'" in query["sql"]
    ]
    # A preload reads the manufacturer's rules and the global rules. A mixed install preloads up to three times:
    # once for the stack's member attribution and once for each destination manufacturer.
    assert len(serial_queries) <= (6 if mixed_manufacturers else 2), serial_queries


@pytest.mark.django_db
@pytest.mark.parametrize("index", [None, "", 200])
def test_replace_action_requires_a_source_inventory_index(index):
    """Only rows that can address the preview endpoint may offer replacement."""
    from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
    from netbox_librenms_plugin.tests.conftest import make_device

    device = make_device("replace-source-index")
    table = LibreNMSModuleTable(
        [],
        device=device,
        server_key="default",
        has_write_permission=True,
        can_add_module=True,
        can_change_module=True,
        can_delete_module=True,
    )
    table.inventory_snapshot_digest = "replace-source-snapshot"
    table.policy_fingerprint = "replace-source-policy"
    html = str(
        table.render_actions(None, {"can_replace": True, "installed_module_id": 55, "ent_physical_index": index})
    )
    assert ('data-action="replace"' in html) is (index == 200)


def test_integrated_module_badge_tracks_the_active_theme():
    """The parent label must use paired theme colors rather than a fixed light surface."""
    from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

    table = LibreNMSModuleTable([])
    html = str(table.render_status("Integrated", {"status": "Integrated", "integrated_in_name": "Carrier 1"}))
    assert "Integrated in Carrier 1" in html
    assert "bg-body-secondary" in html
    assert "text-body" in html
    assert "bg-light" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("oob_index,oob_parent", [(-500, 0), (1, -500)])
def test_refresh_drops_out_of_spec_oob_inventory(live_librenms, oob_index, oob_parent):
    """Negative OOB index fields prevent the refresh from caching an inventory snapshot."""
    from django.core.cache import cache

    from netbox_librenms_plugin.server_mappings import attach_oob

    from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    device = make_device("signed-inventory", librenms_cf={"default": 777})
    apply_mapping_change(device, attach_oob(device, "default", 999, oob_type="idrac9"))
    device.save(update_fields=["custom_field_data"])
    # RFC 2737 defines entPhysicalIndex as 1..2147483647.
    # The old offset would shift the negative OOB index onto main index 1500.
    for device_id, index, parent, name in ((777, 1500, 0, "Main card"), (999, oob_index, oob_parent, "OOB card")):
        live_librenms.server.register(
            f"/api/v0/inventory/{device_id}/all",
            {
                "status": "ok",
                "inventory": [
                    {
                        "entPhysicalIndex": index,
                        "entPhysicalContainedIn": parent,
                        "entPhysicalName": name,
                        "entPhysicalClass": "module",
                        "entPhysicalModelName": "TEST-CARD",
                        "entPhysicalSerialNum": name,
                    }
                ],
            },
        )
    live_librenms.server.register("/api/v0/devices/777/transceivers", {"status": "ok", "transceivers": []})
    live_librenms.server.register("/api/v0/devices/777/ports", {"status": "ok", "ports": []})
    request = _post_request({"server_key": "default"})
    view = _view(DeviceModuleTableView, request, live_librenms)
    response = view_post(view, request, pk=device.pk)

    assert response.status_code == 200
    # The refused OOB payload makes the refresh incomplete, so it must cache no snapshot.
    assert cache.get(view.get_cache_key(device, "inventory", server_key="default")) is None
    assert b"OOB controller inventory fetch failed" in response.content


@pytest.mark.django_db
def test_refresh_rejects_a_negative_main_inventory_index(live_librenms):
    """A negative index in the MAIN inventory fails the refresh instead of caching a bad snapshot."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    device = make_device("signed-main-inventory", librenms_cf={"default": 778})
    live_librenms.server.register(
        "/api/v0/inventory/778/all",
        {
            "status": "ok",
            "inventory": [
                {
                    "entPhysicalIndex": -1,
                    "entPhysicalContainedIn": 0,
                    "entPhysicalName": "Main card",
                    "entPhysicalClass": "module",
                    "entPhysicalModelName": "TEST-CARD",
                    "entPhysicalSerialNum": "Main card",
                }
            ],
        },
    )
    live_librenms.server.register("/api/v0/devices/778/transceivers", {"status": "ok", "transceivers": []})
    live_librenms.server.register("/api/v0/devices/778/ports", {"status": "ok", "ports": []})
    request = _post_request({"server_key": "default"})
    view = _view(DeviceModuleTableView, request, live_librenms)
    response = view_post(view, request, pk=device.pk)

    assert response.status_code == 200
    assert cache.get(view.get_cache_key(device, "inventory", server_key="default")) is None
    assert b"Failed to fetch inventory from LibreNMS" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("policy", [None, "name", "serial"])
@pytest.mark.parametrize("member_visible", [True, False])
def test_a_selected_install_uses_each_members_destination_and_ignore_policy(client, policy, member_visible):
    """The manifest carries the member the table attributes the row to, under that member's rules."""
    from dcim.models import Manufacturer, Module
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type,
        make_superuser,
        make_virtual_chassis,
    )
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("branch-owner-page", ["Slot 2"])
    page.serial = "BRANCH-PAGE"
    page.save()
    manufacturer = Manufacturer.objects.create(name="Branch Member Vendor", slug="branch-member-vendor")
    member = make_device_with_module_bays("branch-owner-member", ["Slot 2"], manufacturer=manufacturer)
    member.serial = "BRANCH-MEMBER"
    member.save()
    make_virtual_chassis("branch-owner-chassis", page, member)
    module_type = make_module_type("Branch Member Card", manufacturer=manufacturer)
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    if policy:
        InventoryIgnoreRule.objects.create(
            name="Member policy",
            manufacturer=manufacturer,
            match_type="serial_matches_device" if policy == "serial" else "contains",
            pattern="" if policy == "serial" else "Slot 2",
            action="skip",
            require_serial_match_parent=False,
        )
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "chassis",
            "entPhysicalSerialNum": page.serial,
        },
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 2",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": member.serial,
        },
    ]
    policy_inputs = load_inventory_policy(page.pk)
    _default, contexts = BaseModuleTableView()._build_inventory_ignore_contexts(
        policy_inputs.page_device, rows, policy_inputs.vc_members, policy_inputs
    )
    assert contexts[("index", 2)]["selected_device"].pk == member.pk
    cache_key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(cache_key, trusted_module_inventory_payload(page, rows))
    if member_visible:
        user = make_superuser("branch-owner-user")
    else:
        from dcim.models import Device, Interface, ModuleBay, ModuleType
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        user = make_user_with_perms(
            "branch-owner-restricted",
            [
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "view", Device, constraints={"pk": page.pk})
    client.force_login(user)
    fields = _rendered_fields(page, "install-selected", user=user)
    try:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:install_selected", args=[page.pk]), {**fields, "select": ["2"]}
        )
    finally:
        cache.delete(cache_key)
    assert response.status_code == 302
    assert not Module.objects.filter(device=page).exists()
    assert Module.objects.filter(device=member).count() == (0 if policy or not member_visible else 1)


@pytest.mark.django_db(transaction=True)
def test_branch_install_refreshes_page_serial_after_waiting_for_its_lock(client):
    """An edit committed before the page lock refuses the branch rendered before it."""
    from dcim.models import Device, Module
    from django.core.cache import cache
    from django.db import close_old_connections, connection
    from django.urls import reverse

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("branch-fresh-page", ["System"], serial="OLD-PAGE-SERIAL")
    module_type = make_module_type("Branch System", manufacturer=page.device_type.manufacturer)
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    InventoryIgnoreRule.objects.create(
        name="Skip device system",
        manufacturer=page.device_type.manufacturer,
        match_type="serial_matches_device",
        pattern="",
        action="skip",
    )
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "System",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "CURRENT-PAGE-SERIAL",
        },
        # The child gives the row its branch button.
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Optic",
            "entPhysicalModelName": make_module_type("Branch System Optic").model,
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    user = make_superuser("branch-fresh-page-user")
    fields = _rendered_fields(page, "install-branch", 1, user=user)
    changed = []

    def change_serial():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '2s'")
            Device.objects.filter(pk=page.pk).update(serial="CURRENT-PAGE-SERIAL")
        finally:
            close_old_connections()

    def edit_before_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            with bounded_executor(max_workers=1) as executor:
                executor.submit(change_serial).result(timeout=5)
            changed.append(True)
        return execute(sql, params, many, context)

    client.force_login(user)
    try:
        with connection.execute_wrapper(edit_before_lock):
            response = client.post(reverse("plugins:netbox_librenms_plugin:install_branch", args=[page.pk]), fields)
    finally:
        cache.delete(key)
    assert changed == [True]
    assert response.status_code == 302
    assert not Module.objects.filter(device=page).exists()
    assert GrantRefusal.POLICY.message in [str(message) for message in response.wsgi_request._messages]


@pytest.mark.django_db
@pytest.mark.parametrize("transparent", [False, True])
def test_branch_install_cuts_parent_bay_lookup_at_a_member_boundary(client, transparent):
    from dcim.models import Module
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type,
        make_module_type_with_bays,
        make_superuser,
        make_virtual_chassis,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("branch-boundary-page", ["Carrier"], serial="PAGE-SERIAL")
    member = make_device_with_module_bays("branch-boundary-member", ["Carrier", "Card"], serial="MEMBER-SERIAL")
    make_virtual_chassis("branch-boundary-chassis", page, member)
    carrier_type = make_module_type_with_bays("Boundary Carrier", bay_names=["Card"])
    card_type = make_module_type("Boundary Card")
    unrelated = Module.objects.create(
        device=member,
        module_bay=member.modulebays.get(name="Carrier"),
        module_type=carrier_type,
        status="active",
        serial="OTHER-CARRIER",
    )
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": carrier_type.model,
            "entPhysicalSerialNum": page.serial,
        },
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Card",
            "entPhysicalModelName": card_type.model,
            "entPhysicalSerialNum": member.serial,
        },
        # The card goes to the member, so this page child is what gives the carrier its branch button.
        {
            "entPhysicalIndex": 4,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Page Optic",
            "entPhysicalModelName": make_module_type("Boundary Page Optic").model,
        },
    ]
    if transparent:
        rows[1].update(entPhysicalName="Transparent member", entPhysicalClass="chassis")
        rows.append(
            {
                "entPhysicalIndex": 3,
                "entPhysicalContainedIn": 2,
                "entPhysicalClass": "module",
                "entPhysicalName": "Card",
                "entPhysicalModelName": card_type.model,
                "entPhysicalSerialNum": "CHILD-CARD",
                "entPhysicalParentRelPos": 1,
            }
        )
        InventoryIgnoreRule.objects.create(
            name="Transparent member chassis",
            match_type="contains",
            pattern="Transparent member",
            action="transparent",
            require_serial_match_parent=False,
        )
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    user = make_superuser("branch-boundary-user")
    client.force_login(user)
    try:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:install_branch", args=[page.pk]),
            _rendered_fields(page, "install-branch", 1, user=user),
        )
    finally:
        cache.delete(key)
    assert response.status_code == 302
    card = Module.objects.get(module_type=card_type)
    assert card.device_id == member.pk
    assert card.module_bay_id == member.modulebays.get(name="Card", module__isnull=True).pk
    assert not Module.objects.filter(module_bay__module=unrelated).exists()


def _member_card_under_page_carrier(*, card_on_member):
    """Cache a page carrier row with a card row below it; the member holds another carrier with a Card bay."""
    from dcim.models import Module

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type,
        make_module_type_with_bays,
        make_virtual_chassis,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("selected-boundary-page", ["Carrier", "Card"], serial="PAGE-SERIAL")
    member = make_device_with_module_bays("selected-boundary-member", ["Carrier", "Card"], serial="MEMBER-SERIAL")
    make_virtual_chassis("selected-boundary-chassis", page, member)
    carrier_type = make_module_type_with_bays("Selected Boundary Carrier", bay_names=["Card"])
    card_type = make_module_type("Selected Boundary Card")
    unrelated = Module.objects.create(
        device=member,
        module_bay=member.modulebays.get(name="Carrier"),
        module_type=carrier_type,
        status="active",
        serial="OTHER-CARRIER",
    )
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": carrier_type.model,
            "entPhysicalSerialNum": page.serial,
        },
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Card",
            "entPhysicalModelName": card_type.model,
            # Without the member serial, the page render attributes the card to the page with its carrier.
            "entPhysicalSerialNum": member.serial if card_on_member else "SN-CARD",
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    return page, member, unrelated, card_type, key


def _verified_selection_grant(client, content, page, device, index):
    """POST the verify request the member select of row *index* sends for *device*; return the row's selection grant."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import module_tab_forms

    (select,) = [attrs for attrs in module_tab_forms(content).selects if attrs.get("data-module") == str(index)]
    (depth,) = re.findall(rf'<tr[^>]*data-ent-index="{index}"[^>]*data-depth="(\d+)"', content)
    verified = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps(
            {
                "device_id": device.pk,
                "page_device_id": page.pk,
                "ent_physical_index": index,
                "depth": int(depth),
                "server_key": "default",
                "verify_binding": select.get("data-verify-binding"),
            }
        ),
        content_type="application/json",
    )
    assert verified.status_code == 200, verified.content
    (link,) = [
        link
        for link in module_tab_forms(verified.json()["formatted_row"]["device_selection"]).links
        if "selection_grant" in link
    ]
    return link["value"]


@pytest.mark.django_db
def test_table_names_no_member_bay_after_a_module_row_of_the_page():
    """A page module above a member row does not name the member bay, so the card offers the member Card bay."""
    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    page, member, _unrelated, _card_type, key = _member_card_under_page_carrier(card_on_member=True)
    request = make_request("get", {"server_key": "default"}, user=make_superuser("table-boundary-user"), path="/m/")
    view = DeviceModuleTableView()
    view.setup(request, pk=page.pk)
    view.cache_only = True
    try:
        rows = list(view.get_context_data(request, page, server_key="default")["table"].data)
    finally:
        cache.delete(key)
    (card,) = [row for row in rows if row["ent_physical_index"] == 2]
    assert card["selected_device_id"] == member.pk
    assert card["module_bay_id"] == member.modulebays.get(name="Card", module__isnull=True).pk
    assert card["can_install"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("anchor_on_member", [False, True])
def test_table_counts_a_bay_position_only_under_a_module_of_the_target_member(anchor_on_member):
    """The slot position under a carrier picks a member bay only when the member owns that carrier."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type,
        make_superuser,
        make_virtual_chassis,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    page = make_device_with_module_bays("position-boundary-page", ["Slot 1", "Slot 2"], serial="PAGE-SERIAL")
    member = make_device_with_module_bays("position-boundary-member", ["Slot 1", "Slot 2"], serial="MEMBER-SERIAL")
    make_virtual_chassis("position-boundary-chassis", page, member)
    carrier_type = make_module_type("Position Boundary Carrier")
    card_type = make_module_type("Position Boundary Card")
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": carrier_type.model,
            "entPhysicalSerialNum": member.serial if anchor_on_member else page.serial,
        },
        *(
            {
                "entPhysicalIndex": index,
                "entPhysicalContainedIn": 1,
                "entPhysicalClass": "container",
                "entPhysicalName": "",
                "entPhysicalParentRelPos": index,
            }
            for index in (2, 3)
        ),
        {
            "entPhysicalIndex": 4,
            "entPhysicalContainedIn": 3,
            "entPhysicalClass": "module",
            "entPhysicalName": "Card",
            "entPhysicalModelName": card_type.model,
            "entPhysicalSerialNum": member.serial,
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    request = make_request("get", {"server_key": "default"}, user=make_superuser("position-boundary-user"), path="/m/")
    view = DeviceModuleTableView()
    view.setup(request, pk=page.pk)
    view.cache_only = True
    try:
        table_rows = list(view.get_context_data(request, page, server_key="default")["table"].data)
    finally:
        cache.delete(key)
    (card,) = [row for row in table_rows if row["ent_physical_index"] == 4]
    assert card["selected_device_id"] == member.pk
    expected = member.modulebays.get(name="Slot 2").pk if anchor_on_member else None
    assert card.get("module_bay_id") == expected


@pytest.mark.django_db
@pytest.mark.parametrize("override", [False, True])
def test_selected_install_walks_the_parent_chain_of_the_render_that_authorized_the_row(client, override):
    """A manifest row stops its parent walk at a member boundary; a pinned override walks the whole pinned render."""
    from dcim.models import Module

    from netbox_librenms_plugin.tests.conftest import make_superuser

    page, member, unrelated, card_type, key = _member_card_under_page_carrier(card_on_member=not override)
    user = make_superuser("selected-boundary-user")
    client.force_login(user)
    try:
        content = render_module_tab(page, user=user)
        action, fields = rendered_module_form(content, "install-selected")
        fields["select"] = ["2"]
        if override:
            grant = _verified_selection_grant(client, content, page, member, 2)
            fields.update({"device_selection_2": str(member.pk), "selection_grant_2": grant})
        response = client.post(action, fields)
    finally:
        cache.delete(key)
    assert response.status_code == 302
    card = Module.objects.get(module_type=card_type)
    assert card.device_id == member.pk
    # The pinned member render attributes the carrier row to the member too, so the card goes into its carrier.
    expected = member.modulebays.get(name="Card", module=unrelated if override else None)
    assert card.module_bay_id == expected.pk


@pytest.mark.django_db
@pytest.mark.parametrize("posted_grant", ["verified", "forged"])
def test_selected_install_back_on_the_manifest_member_follows_the_verified_render(client, posted_grant):
    """A member change and a change back leave the pinned row on the page, so its grant decides the install."""
    from dcim.models import Module

    from netbox_librenms_plugin.tests.conftest import make_superuser

    page, member, unrelated, card_type, key = _member_card_under_page_carrier(card_on_member=True)
    user = make_superuser("selected-round-trip-user")
    client.force_login(user)
    try:
        content = render_module_tab(page, user=user)
        action, fields = rendered_module_form(content, "install-selected")
        # The browser verifies the row for each member the user picks: the page first, then the member again.
        _verified_selection_grant(client, content, page, page, 2)
        grant = _verified_selection_grant(client, content, page, member, 2)
        forged = fields["inventory_binding"]
        fields.update(
            {
                "select": ["2"],
                "device_selection_2": str(member.pk),
                "selection_grant_2": grant if posted_grant == "verified" else forged,
            }
        )
        response = client.post(action, fields)
    finally:
        cache.delete(key)
    assert response.status_code == 302
    if posted_grant == "forged":
        assert not Module.objects.filter(module_type=card_type).exists()
        return
    # The verified member render puts the card into the member carrier, which its row showed.
    card = Module.objects.get(module_type=card_type)
    assert card.module_bay_id == member.modulebays.get(name="Card", module=unrelated).pk


@pytest.mark.django_db
@pytest.mark.parametrize("member_left", [False, True])
def test_branch_install_runs_onto_the_signed_member_until_it_leaves_the_chassis(client, member_left):
    from dcim.models import Device, Module
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type,
        make_superuser,
        make_virtual_chassis,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("branch-moved-page", ["Card"], serial="PAGE-SERIAL")
    member = make_device_with_module_bays("branch-moved-member", ["Card"], serial="MEMBER-SERIAL")
    make_virtual_chassis("branch-moved-chassis", page, member)
    module_type = make_module_type("Moved Card")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "chassis",
            "entPhysicalName": "Member chassis",
            "entPhysicalSerialNum": member.serial,
        },
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Card",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "SN-MOVED-CARD",
        },
        # The child gives the card its branch button; it fits no bay, so only the card installs.
        {
            "entPhysicalIndex": 3,
            "entPhysicalContainedIn": 2,
            "entPhysicalClass": "module",
            "entPhysicalName": "Optic",
            "entPhysicalModelName": make_module_type("Moved Card Optic").model,
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    user = make_superuser("branch-moved-user")
    fields = _rendered_fields(page, "install-branch", 2, user=user)
    assert fields["selected_device_id"] == str(member.pk)
    if member_left:
        Device.objects.filter(pk=member.pk).update(virtual_chassis=None, vc_position=None)
    client.force_login(user)
    try:
        response = client.post(reverse("plugins:netbox_librenms_plugin:install_branch", args=[page.pk]), fields)
    finally:
        cache.delete(key)
    assert response.status_code == 302
    assert not Module.objects.filter(device=page).exists()
    assert Module.objects.filter(device=member).count() == int(not member_left)
    if member_left:
        assert GrantRefusal.POLICY.message in [str(message) for message in response.wsgi_request._messages]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("valid_binding", [True, False])
def test_branch_install_releases_its_lock_before_rendering(client, valid_binding):
    from dcim.models import Module
    from django.core.cache import cache
    from django.db import connection
    from django.test.signals import template_rendered
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("branch-render-page", ["Line Card"])
    module_type = make_module_type("Branch Render Card", manufacturer=page.device_type.manufacturer)
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "Line Card",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "RENDER-CARD",
        },
        # The child gives the card its branch button; it fits no bay, so only the card installs.
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Optic",
            "entPhysicalModelName": make_module_type("Branch Render Optic").model,
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    user = make_superuser("branch-render-user")
    fields = _rendered_fields(page, "install-branch", 1, user=user)
    if not valid_binding:
        fields["inventory_binding"] = "stale"
    observed = []

    def record_transaction(sender, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'")
            observed.append((connection.in_atomic_block, cursor.fetchone()[0]))

    client.force_login(user)
    template_rendered.connect(record_transaction)
    try:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:install_branch", args=[page.pk]), fields, HTTP_HX_REQUEST="true"
        )
    finally:
        template_rendered.disconnect(record_transaction)
        cache.delete(key)
    assert response.status_code == 200
    assert Module.objects.filter(device=page).count() == int(valid_binding)
    assert observed
    assert set(observed) == {(False, 0)}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("refusal", ["occupied", "duplicate_serial", "missing"])
def test_single_module_refusal_releases_its_lock_before_rendering(client, refusal):
    from dcim.models import Module, ModuleBay
    from django.core.cache import cache
    from django.db import connection
    from django.test.signals import template_rendered
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("single-render-page", ["Target", "Existing"])
    bay = page.modulebays.get(name="Target")
    module_type = make_module_type("Single Render Card", manufacturer=page.device_type.manufacturer)
    row = {
        "entPhysicalIndex": 1,
        "entPhysicalContainedIn": 0,
        "entPhysicalClass": "module",
        "entPhysicalName": "Target",
        "entPhysicalModelName": module_type.model,
        "entPhysicalSerialNum": "RENDER-CARD",
    }
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, [row]))
    user = make_superuser("single-render-user")
    fields = _rendered_fields(page, "install-module", 1, user=user)
    if refusal != "missing":
        # The table offers no install over the occupant or the duplicate serial, so it lands after the render.
        Module.objects.create(
            device=page,
            module_bay=bay if refusal == "occupied" else page.modulebays.get(name="Existing"),
            module_type=module_type,
            serial="RENDER-CARD",
        )
        # The install drops the snapshot; a refresh caches the same rows, so the rendered grant still holds.
        cache.set(key, trusted_module_inventory_payload(page, [row]))
    observed = []
    acquired = []

    def remove_bay_at_lock(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "pg_advisory_xact_lock" in sql and not acquired:
            acquired.append(True)
            if refusal == "missing":
                ModuleBay.objects.filter(pk=bay.pk).delete()
        return result

    def record_transaction(sender, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'")
            observed.append((connection.in_atomic_block, cursor.fetchone()[0]))

    client.force_login(user)
    template_rendered.connect(record_transaction)
    try:
        with connection.execute_wrapper(remove_bay_at_lock):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:install_module", args=[page.pk]), fields, HTTP_HX_REQUEST="true"
            )
    finally:
        template_rendered.disconnect(record_transaction)
        cache.delete(key)
    expected = {
        "occupied": "Module bay 'Target' already has a module installed.",
        "duplicate_serial": "Serial 'RENDER-CARD' is already installed on this device. Nothing was installed.",
        "missing": "Module bay no longer exists.",
    }
    assert response.status_code == 200
    assert [str(message) for message in response.wsgi_request._messages] == [expected[refusal]]
    assert Module.objects.filter(device=page).count() == (0 if refusal == "missing" else 1)
    assert acquired == [True]
    assert observed
    assert set(observed) == {(False, 0)}


def test_module_advisory_lock_sections_do_not_render_responses():
    """Keep response rendering out of the transactions that hold page advisory locks."""
    import ast
    import inspect

    from netbox_librenms_plugin.views.sync import modules

    tree = ast.parse(inspect.getsource(modules))
    violations = set()
    for node in ast.walk(tree):
        atomic = isinstance(node, ast.With) or (
            isinstance(node, ast.FunctionDef)
            and any(ast.unparse(decorator) == "transaction.atomic" for decorator in node.decorator_list)
        )
        if not atomic:
            continue
        calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
        if not any(ast.unparse(call.func) == "_lock_page_device_serials" for call in calls):
            continue
        violations.update(call.lineno for call in calls if ast.unparse(call.func) == "_modules_action_response")
    assert not violations, f"Response rendering holds a page advisory lock at lines {sorted(violations)}"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("selected_member, deleted", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("matched_row", ["selected", "ancestor"])
def test_selected_install_refreshes_target_serial_after_waiting_for_its_lock(
    client, selected_member, deleted, matched_row
):
    """An edit committed before the page lock refuses the selected install rendered before it.

    The rule matches the selected row itself, or the top-level parent of a selected child.
    """
    from dcim.models import Device, Module
    from django.core.cache import cache
    from django.db import close_old_connections, connection
    from django.urls import reverse

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays("selected-fresh-page", ["System"], serial="OLD-PAGE-SERIAL")
    target = page
    if selected_member:
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

        target = make_device_with_module_bays("selected-fresh-member", ["System"], serial="OLD-MEMBER-SERIAL")
        make_virtual_chassis("selected-fresh-chassis", page, target)
    module_type = make_module_type("Branch System", manufacturer=page.device_type.manufacturer)
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(name="Admit modules", match_type="class_is", pattern="module", action="include")
    InventoryIgnoreRule.objects.create(
        name="Skip device system",
        manufacturer=page.device_type.manufacturer,
        match_type="serial_matches_device",
        pattern="",
        action="skip",
    )
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "module",
            "entPhysicalName": "System",
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "CURRENT-PAGE-SERIAL",
        }
    ]
    selected_index = 1
    if matched_row == "ancestor":
        # The rule now matches the top-level parent; the selected child is the row that fits the "System" bay.
        rows[0].update(entPhysicalName="Chassis unit", entPhysicalModelName="Chassis Unit Model")
        rows.append(
            {
                "entPhysicalIndex": 2,
                "entPhysicalContainedIn": 1,
                "entPhysicalClass": "module",
                "entPhysicalName": "System",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": "CHILD-SERIAL",
            }
        )
        selected_index = 2
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    user = make_superuser("selected-fresh-page-user")
    fields = _rendered_fields(page, "install-selected", user=user)
    changed = []

    def change_serial():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '2s'")
            if deleted:
                Device.objects.filter(pk=target.pk).delete()
            else:
                Device.objects.filter(pk=target.pk).update(serial="CURRENT-PAGE-SERIAL")
        finally:
            close_old_connections()

    def edit_before_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            with bounded_executor(max_workers=1) as executor:
                executor.submit(change_serial).result(timeout=5)
            changed.append(True)
        return execute(sql, params, many, context)

    client.force_login(user)
    try:
        with connection.execute_wrapper(edit_before_lock):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:install_selected", args=[page.pk]),
                {**fields, "select": [str(selected_index)], f"device_selection_{selected_index}": str(target.pk)},
            )
    finally:
        cache.delete(key)
    assert changed == [True]
    assert response.status_code == 302
    assert not Module.objects.filter(device__in=[page, target]).exists()
    # The edit lands before the page lock, so the policy the writer loads is no longer the rendered one.
    assert [str(message) for message in response.wsgi_request._messages] == [GrantRefusal.POLICY.message]


def _backends_blocked_by(observer, holder_pid):
    """Return how many backends wait for an advisory lock that *holder_pid* holds; *observer* is any other connection."""
    with observer.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            " AND %s = ANY(pg_blocking_pids(pid))",
            [holder_pid],
        )
        return cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["device-serial", "rule-added"])
@pytest.mark.parametrize("writer", ["install", "selected", "branch"])
def test_a_writer_waiting_for_the_page_lock_refuses_a_change_committed_during_the_wait(client, writer, change):
    """Another session holds the page lock; a change commits while the writer waits, so the writer refuses it."""
    import time

    from dcim.models import Device, Module
    from django.core.cache import cache
    from django.db import close_old_connections

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser
    from netbox_librenms_plugin.tests.view_test_helpers import render_module_tab, rendered_module_form
    from netbox_librenms_plugin.utils import advisory_lock_key
    from netbox_librenms_plugin.views.mixins import CacheMixin

    page = make_device_with_module_bays(f"lock-wait-{writer}-{change}", ["Carrier"], serial="LOCK-WAIT-PAGE")
    carrier = make_module_type(f"Lock Wait Carrier {writer} {change}", manufacturer=page.device_type.manufacturer)
    card = make_module_type(f"Lock Wait Card {writer} {change}", manufacturer=page.device_type.manufacturer)
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "chassis",
            "entPhysicalSerialNum": page.serial,
        },
        {
            "entPhysicalIndex": 2,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": carrier.model,
            "entPhysicalSerialNum": "SN-CARRIER",
        },
        {
            "entPhysicalIndex": 3,
            "entPhysicalContainedIn": 2,
            "entPhysicalClass": "module",
            "entPhysicalName": "Card",
            "entPhysicalModelName": card.model,
            "entPhysicalSerialNum": "SN-CARD",
        },
    ]
    key = CacheMixin().get_cache_key(page, "inventory", "default")
    cache.set(key, trusted_module_inventory_payload(page, rows))
    content = render_module_tab(page)
    if writer == "install":
        action, fields = rendered_module_form(content, "install-module", 2)
    elif writer == "branch":
        action, fields = rendered_module_form(content, "install-branch", 2)
    else:
        action, fields = rendered_module_form(content, "install-selected")
        fields = {**fields, "select": ["2"]}
    user = make_superuser(f"lock-wait-{writer}-{change}")

    def post_while_the_lock_is_held():
        close_old_connections()
        try:
            client.force_login(user)
            return client.post(action, fields)
        finally:
            close_old_connections()

    try:
        with second_connection() as holder, second_connection() as observer, bounded_executor(1) as executor:
            with holder.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    [advisory_lock_key(f"netbox-librenms-plugin:module-serial:{page.pk}")],
                )
                cursor.execute("SELECT pg_backend_pid()")
                holder_pid = cursor.fetchone()[0]
            posted = executor.submit(post_while_the_lock_is_held)
            deadline = time.monotonic() + 5
            while not _backends_blocked_by(observer, holder_pid):
                assert time.monotonic() < deadline, "the writer never waited for the page lock"
                time.sleep(0.02)
            # The change commits while the writer waits; this connection is in autocommit.
            if change == "device-serial":
                Device.objects.filter(pk=page.pk).update(serial="LOCK-WAIT-CHANGED")
            else:
                InventoryIgnoreRule.objects.create(
                    name="Lock wait rule", match_type="contains", pattern="no-such-entity", action="skip"
                )
            holder.rollback()
            response = posted.result(timeout=10)
    finally:
        cache.delete(key)

    assert response.status_code == 302
    assert [str(message) for message in response.wsgi_request._messages] == [GrantRefusal.POLICY.message]
    assert not Module.objects.filter(device=page).exists()


@pytest.mark.parametrize(
    "details, expected",
    [
        ("Invalid module.", "Invalid module."),
        (["Invalid module.", "Choose a bay."], "Invalid module.; Choose a bay."),
        ({"serial": ["Invalid serial."]}, "serial: Invalid serial."),
    ],
)
def test_module_validation_details_are_plain_text(details, expected):
    from django.core.exceptions import ValidationError
    from dcim.models import Module
    from django.contrib.auth import get_user_model
    from netbox_librenms_plugin.views.sync.modules import _module_write_failure

    user = get_user_model()(is_superuser=True, is_active=True)
    assert _module_write_failure(ValidationError(details), Module, user) == expected


def test_module_database_conflicts_use_safe_error_text():
    from django.db import IntegrityError
    from dcim.models import Module
    from django.contrib.auth import get_user_model
    from netbox_librenms_plugin.utils import DATABASE_ERROR_MESSAGE
    from netbox_librenms_plugin.views.sync.modules import _module_write_failure

    user = get_user_model()(is_superuser=True, is_active=True)
    assert _module_write_failure(IntegrityError("Duplicate module."), Module, user) == DATABASE_ERROR_MESSAGE


@pytest.mark.django_db
def test_install_branch_non_numeric_parent_index(client):
    from django.urls import reverse
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser

    device = make_device("invalid-branch-index")
    client.force_login(make_superuser("invalid-branch-index-user"))
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:install_branch", args=[device.pk]),
        {"parent_index": "abc", "server_key": "default"},
    )
    assert response.status_code == 302
    assert "Invalid parent inventory index." in message_texts(response.wsgi_request)


@pytest.mark.django_db
class TestGenericModuleTypeFallback:
    """A model the table resolves only through the Generic manufacturer must resolve the same way in every writer."""

    @staticmethod
    def _generic_only_type(model):
        """Create *model* under a vendor and under Generic, so only the Generic index can resolve it."""
        from dcim.models import Manufacturer

        generic, _ = Manufacturer.objects.get_or_create(name="Generic", defaults={"slug": "generic"})
        make_module_type(model)
        return make_module_type(model, manufacturer=generic)

    def test_a_manufacturer_renamed_to_generic_rebuilds_the_cached_index(self):
        from dcim.models import Manufacturer

        from netbox_librenms_plugin.utils import get_module_types_indexed, resolve_module_type

        manufacturer = Manufacturer.objects.create(name="Soon Generic", slug="soon-generic")
        make_module_type("GENERIC-RENAMED")
        renamed_type = make_module_type("GENERIC-RENAMED", manufacturer=manufacturer)
        assert resolve_module_type(renamed_type.model, get_module_types_indexed()) is None

        manufacturer.name = "Generic"
        manufacturer.save()

        assert resolve_module_type(renamed_type.model, get_module_types_indexed()) == renamed_type

    def test_the_selected_install_uses_the_generic_type(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("generic-selected", librenms_cf={"default": 91})
        make_module_bay(device, "Generic Slot")
        generic_type = self._generic_only_type("GENERIC-SHARED-SELECTED")
        rows = [_inventory_item(910, generic_type.model, "Generic Slot", serial="SN-GENERIC-SELECTED")]
        # The table must resolve the Generic type too, or it renders no install manifest for the row.
        view, request = _rendered_post(
            InstallSelectedView, device, rows, 91, live_librenms, "install-selected", extra={"select": ["910"]}
        )

        view_post(view, request, pk=device.pk)

        assert Module.objects.filter(device=device, module_type=generic_type).exists()

    def test_the_replace_uses_the_generic_type(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("generic-replace", librenms_cf={"default": 92})
        bay = make_module_bay(device, "Generic Replace Bay")
        install_module(device, bay.name, "GENERIC-REPLACE-OLD")
        generic_type = self._generic_only_type("GENERIC-SHARED-REPLACE")
        item = {
            "entPhysicalIndex": 920,
            "entPhysicalModelName": generic_type.model,
            "entPhysicalName": bay.name,
            "entPhysicalClass": "module",
            "entPhysicalSerialNum": "SN-GENERIC-REPLACE",
        }
        seed_inventory(ReplaceModuleView(), device, [item], librenms_id=92)
        _action, fields = rendered_modal_form(device, 920, "replace-module")
        request = _post_request(fields)
        view = _view(ReplaceModuleView, request, live_librenms)

        view_post(view, request, pk=device.pk)

        assert device.modules.get(module_bay=bay).module_type == generic_type

    def test_the_replace_preview_recognises_the_generic_type(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

        device = make_device("generic-preview", librenms_cf={"default": 93})
        bay = make_module_bay(device, "Generic Preview Bay")
        generic_type = self._generic_only_type("GENERIC-SHARED-PREVIEW")
        install_module(device, bay.name, generic_type.model, manufacturer=generic_type.manufacturer)
        item = {
            "entPhysicalIndex": 930,
            "entPhysicalModelName": generic_type.model,
            "entPhysicalName": bay.name,
            "entPhysicalClass": "module",
            "entPhysicalSerialNum": "SN-GENERIC-PREVIEW",
        }
        seed_inventory(ModuleMismatchPreviewView(), device, [item], librenms_id=93)
        user = make_superuser()
        request = make_request("get", rendered_preview_query(device, 930, user=user), user=user)
        view = _view(ModuleMismatchPreviewView, request, live_librenms)

        response = view_get(view, request, pk=device.pk)

        assert response.status_code == 200
        # The badge marks a LibreNMS model that resolved to the installed module's type.
        assert b"mdi-check-decagram" in response.content
