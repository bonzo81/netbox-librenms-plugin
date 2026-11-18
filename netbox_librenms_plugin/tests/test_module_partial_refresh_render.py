"""
A partial module refresh renders nothing rather than a degraded table.

``post()`` only fingerprints ``librenms_id``/``oob_librenms_id`` when it reads a snapshot back,
so a truncated one would be served as complete until its TTL expired. The refresh therefore
drops the snapshot and renders an empty tab whenever any of the three fetches failed, instead
of building a table from data it knows to be incomplete.
"""

from copy import deepcopy

import pytest
from django.conf import settings
from django.test import override_settings

from netbox_librenms_plugin.sync_cache import module_inventory_cache_payload
from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
from netbox_librenms_plugin.tests.view_test_helpers import post as _post

SERVER_KEY = "default"
INVENTORY = [
    {
        "entPhysicalIndex": 1,
        "entPhysicalName": "Bay 1",
        "entPhysicalModelName": "Partial Model",
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "PARTIAL-1",
    }
]


def _server_settings(server_key, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        server_key: {"librenms_url": server.url, "api_token": "test-token"}
    }
    return override_settings(PLUGINS_CONFIG=plugin_config)


def _register_refresh(server, *, transceivers=(True, []), ports=(True, {"ports": []}), inventory=(True, INVENTORY)):
    inventory_ok, inventory_data = inventory
    server.inventory_response(42, inventory_data if inventory_ok else [], status=200 if inventory_ok else 500)
    transceivers_ok, transceiver_data = transceivers
    server.register(
        "/api/v0/devices/42/transceivers",
        {"status": "ok", "transceivers": transceiver_data} if transceivers_ok else {"status": "error"},
        status=200 if transceivers_ok else 500,
    )
    ports_ok, ports_data = ports
    server.register(
        "/api/v0/devices/42/ports",
        {"status": "ok", **ports_data} if ports_ok else {"status": "error"},
        status=200 if ports_ok else 500,
    )


def _refresh(device, server, **failure):
    """Run a real modules refresh POST and return the rendered tab context."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    captured = {}
    original = BaseModuleTableView.render_sync_partial

    def capture(self, request, obj, server_key, context, **kwargs):
        captured["context"] = context
        return original(self, request, obj, server_key, context, **kwargs)

    request = make_request("post", {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")
    # The concrete subclass, so a complete refresh can actually build its table.
    view = DeviceModuleTableView()
    _register_refresh(server, **failure)

    BaseModuleTableView.render_sync_partial = capture
    try:
        with _server_settings(SERVER_KEY, server):
            response = _post(view, request, pk=device.pk)
    finally:
        BaseModuleTableView.render_sync_partial = original
    # render_sync_partial nests the tab context under its own key.
    context = captured.get("context", {})
    return response, context.get("module_sync", context), request


@pytest.mark.django_db
class TestPartialModuleRefreshRendersEmpty:
    """Each partial-failure flag must reach the response as an empty tab."""

    @pytest.fixture(autouse=True)
    def _configure_server(self, settings):
        """Configure a usable default LibreNMS server for each test."""
        configure_default_librenms_server(settings)

    @staticmethod
    def _device(name):
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay

        device = make_device(name, librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        return device

    def test_a_complete_refresh_renders_a_table(self, librenms_server):
        """Positive control: the failures below must not pass for the wrong reason."""
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key

        device = self._device("partial-refresh-complete")
        # Built by the production helpers, so a renamed key cannot make the cleanup a silent no-op.
        # The modules tab has no last-fetched key (TAB_SPECS[MODULES].has_last_fetched_key is False).
        written_keys = (
            sync_snapshot_key(device, "inventory", SERVER_KEY),
            SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY),
        )

        try:
            _response, context, request = _refresh(device, librenms_server)

            assert context.get("table") is not None, "a complete refresh must still build the table"
            assert "Inventory data refreshed successfully." in message_texts(request)
            # The DB rolls back but the cache does not, so the finally below must reach every key
            # a success writes; a later test reusing this device pk would read whatever leaks.
            assert [key for key in written_keys if cache.get(key) is not None] == list(written_keys)
        finally:
            for key in written_keys:
                cache.delete(key)

    def test_a_module_serial_with_a_nul_renders_and_reads_back(self, librenms_server):
        """PostgreSQL text cannot hold a NUL, so the serial has no identity and no lookup sends it to the database."""
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = self._device("partial-refresh-nul-serial")
        written_keys = (
            sync_snapshot_key(device, "inventory", SERVER_KEY),
            SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY),
        )
        inventory = [{**INVENTORY[0], "entPhysicalSerialNum": "ABC\x00DEF"}]
        try:
            response, context, _request = _refresh(device, librenms_server, inventory=(True, inventory))

            assert response.status_code == 200
            assert context.get("table") is not None
            request = make_request("get", {"tab": "modules", "server_key": SERVER_KEY})
            view = DeviceModuleTableView()
            view.setup(request, pk=device.pk)
            view.cache_only = True
            assert view.get_context_data(request, device, server_key=SERVER_KEY)["table"] is not None
        finally:
            for key in written_keys:
                cache.delete(key)

    @pytest.mark.parametrize(
        "failure,expected,empty_rows_notice",
        [
            ({"transceivers": (False, "transceiver fetch failed")}, "transceiver fetch failed", True),
            ({"ports": (False, "port fetch failed")}, "port metadata fetch failed", True),
            # An inventory failure returns before the partial-outcome warning, so it carries none.
            (
                {"inventory": (False, "inventory fetch failed")},
                "Failed to fetch inventory from LibreNMS; see server logs for details.",
                False,
            ),
        ],
        ids=["transceivers", "ports", "inventory"],
    )
    def test_a_partial_refresh_renders_no_table(self, failure, expected, empty_rows_notice, librenms_server):
        from netbox_librenms_plugin.serial_identity import SerialRules
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key

        device = self._device(f"partial-refresh-{'-'.join(failure)}")
        # Built by the production helper, so a change to the key scheme cannot silently make
        # the assertion below vacuous.
        cache_key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        cache.set(
            cache_key,
            module_inventory_cache_payload(INVENTORY, 42, None, serial_context=SerialRules.load().for_device(device)),
            timeout=300,
        )
        assert cache.get(cache_key) is not None, "the seed never landed, so the drop assertion proves nothing"
        state_key = SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY)

        try:
            _response, context, request = _refresh(device, librenms_server, **failure)
            snapshot_dropped = cache.get(cache_key) is None
            state_recorded = cache.get(state_key)
        finally:
            cache.delete(cache_key)
            cache.delete(state_key)

        # mark_refresh_failure() writes this alongside dropping the snapshot; the finally above
        # must remove it, or a reused device pk carries REFRESH_FAILED into a later test.
        assert state_recorded["state"] == "refresh_failed", state_recorded
        assert context.get("table") is None, "an incomplete refresh rendered a degraded table"
        assert context.get("cache_expiry") is None
        assert snapshot_dropped, "the truncated snapshot was left to be served as complete"
        assert any(expected in text for text in message_texts(request)), message_texts(request)
        assert "Inventory data refreshed successfully." not in message_texts(request)
        warning = next(text for text in message_texts(request) if expected in text)
        assert "Inventory refreshed" not in warning, warning
        if empty_rows_notice:
            assert "no module rows were loaded" in warning, warning
        else:
            # The inventory failure returns before the partial-outcome warning, so the notice must
            # not appear. Checking every message catches it arriving as a separate one.
            assert all("no module rows were loaded" not in text for text in message_texts(request)), message_texts(
                request
            )


@pytest.mark.django_db
class TestSerialRuleEditStalesTheSnapshot:
    """S5: a snapshot merges transceivers by serial, so a serial rule edit makes it stale."""

    @pytest.fixture(autouse=True)
    def _configure_server(self, settings):
        configure_default_librenms_server(settings)

    @pytest.mark.parametrize("change", ["rule-deleted", "device-type-changed"])
    def test_a_cached_read_rejects_a_snapshot_merged_under_another_serial_context(self, librenms_server, change):
        from dcim.models import Device, DeviceType, Manufacturer
        from django.core.cache import cache

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.sync_cache import (
            SyncCacheConsistency,
            SyncTab,
            cached_module_inventory,
            sync_snapshot_key,
        )
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device(f"context-edit-snapshot-{change}", librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
        NormalizationRule.objects.create(
            scope="serial",
            match_pattern=r"^S/N\s+(.+)$",
            replacement=r"\1",
            manufacturer=device.device_type.manufacturer,
        )
        # Under the rule the transceiver's "S/N PARTIAL-1" is the module's "PARTIAL-1", so the merge drops it.
        transceivers = [{"entity_physical_index": 20, "serial": "S/N PARTIAL-1", "model": "SFP-RULE", "type": "sfp"}]
        snapshot_key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        try:
            _response, context, _request = _refresh(device, librenms_server, transceivers=(True, transceivers))
            assert context.get("table") is not None
            cached = cached_module_inventory(cache.get(snapshot_key))
            assert [item["entPhysicalIndex"] for item in cached] == [1]

            if change == "rule-deleted":
                NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
            else:
                other_maker = Manufacturer.objects.create(name="Context edit maker", slug="context-edit-maker")
                other_type = DeviceType.objects.create(
                    manufacturer=other_maker, model="Context edit model", slug="context-edit-model"
                )
                Device.objects.filter(pk=device.pk).update(device_type=other_type)
                device.refresh_from_db()
            request = make_request("get", {"tab": "modules", "server_key": SERVER_KEY})
            view = DeviceModuleTableView()
            view.setup(request, pk=device.pk)
            view.cache_only = True
            context = view.get_context_data(request, device, server_key=SERVER_KEY)

            assert context["table"] is None
        finally:
            cache.delete(snapshot_key)
            cache.delete(SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY))

    def test_masterless_chassis_members_read_the_snapshot_that_one_refresh_stamped(self, librenms_server):
        """Without a master each member has its own context, so the shared snapshot uses its owner's context."""
        from dcim.models import DeviceType, Manufacturer
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils.virtual_chassis import VC_NUMBERING_NO_MASTER
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.sync_cache import (
            SyncCacheConsistency,
            SyncTab,
            cached_module_inventory,
            sync_snapshot_key,
        )
        from netbox_librenms_plugin.tests.conftest import make_module_bay, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        chassis, (owner, page) = make_virtual_chassis_members("masterless-context")
        chassis.master = None
        chassis.save()
        seed_stored_mapping(owner, {SERVER_KEY: 42}, save=True)
        other_maker = Manufacturer.objects.create(name="Masterless page maker", slug="masterless-page-maker")
        page.device_type = DeviceType.objects.create(
            manufacturer=other_maker, model="Masterless page model", slug="masterless-page-model"
        )
        page.save()
        for member in (owner, page):
            make_module_bay(member, "Bay 1")
        # The owner's manufacturer has its own chain, so the two members' contexts differ.
        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
        NormalizationRule.objects.create(
            scope="serial",
            match_pattern=r"^S/N\s+(.+)$",
            replacement=r"\1",
            manufacturer=owner.device_type.manufacturer,
        )
        # Only the owner's chain makes "S/N PARTIAL-1" the module's "PARTIAL-1", so the merge shows the context.
        transceivers = [{"entity_physical_index": 20, "serial": "S/N PARTIAL-1", "model": "SFP-RULE", "type": "sfp"}]
        snapshot_key = sync_snapshot_key(owner, "inventory", SERVER_KEY)
        try:
            _response, context, _request = _refresh(page, librenms_server, transceivers=(True, transceivers))
            # Without a master the numbering rule is unknown, so the tab refuses the table but keeps the snapshot.
            assert context["vc_numbering_error"] == VC_NUMBERING_NO_MASTER
            cached = cached_module_inventory(cache.get(snapshot_key))
            assert [item["entPhysicalIndex"] for item in cached] == [1], "the merge did not use the owner's context"

            for member in (page, owner, page):
                status = SyncCacheConsistency(member).status(SERVER_KEY)[SyncTab.MODULES.value]
                request = make_request("get", {"tab": "modules", "server_key": SERVER_KEY})
                view = DeviceModuleTableView()
                view.setup(request, pk=member.pk)
                view.cache_only = True
                read = view.get_context_data(request, member, server_key=SERVER_KEY)

                assert read["vc_numbering_error"] == VC_NUMBERING_NO_MASTER, member.name
                assert (status["state"], status["snapshot_available"]) == ("ready", True), member.name
                assert cache.get(snapshot_key) is not None, f"the read of {member.name} deleted the snapshot"
        finally:
            cache.delete(snapshot_key)
            cache.delete(SyncCacheConsistency(page).state_key(SyncTab.MODULES, SERVER_KEY))

    def test_status_rejects_a_snapshot_when_another_makers_serial_rule_breaks(self, librenms_server):
        """The table validates every serial chain, so the status must not call its snapshot available."""
        from dcim.models import Manufacturer
        from django.core.cache import cache

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("status-unrelated-broken-rule", librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        snapshot_key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        state_key = SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY)
        try:
            _response, context, _request = _refresh(device, librenms_server)
            assert context.get("table") is not None
            other_maker = Manufacturer.objects.create(name="Unrelated status maker", slug="unrelated-status-maker")
            rule = NormalizationRule.objects.create(
                scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1", manufacturer=other_maker
            )
            NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"invalid-\1")

            status = SyncCacheConsistency(device).status(SERVER_KEY)[SyncTab.MODULES.value]
            request = make_request("get", {"tab": "modules", "server_key": SERVER_KEY})
            view = DeviceModuleTableView()
            view.setup(request, pk=device.pk)
            view.cache_only = True
            read = view.get_context_data(request, device, server_key=SERVER_KEY)

            assert read["table"] is None
            assert status["snapshot_available"] is False
        finally:
            cache.delete(snapshot_key)
            cache.delete(state_key)

    def test_a_device_type_change_during_the_fetch_stamps_the_context_the_merge_used(
        self, librenms_server, monkeypatch
    ):
        """The merge, the stamp and the signed policy read one observation of the Device."""
        from netbox_librenms_plugin.serial_identity import SerialRules
        from dcim.models import Device, DeviceType, Manufacturer
        from django.core.cache import cache

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay
        from netbox_librenms_plugin.views.base import modules_view

        device = make_device("type-change-during-fetch", librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
        new_maker = Manufacturer.objects.create(name="Fetch race maker", slug="fetch-race-maker")
        new_type = DeviceType.objects.create(manufacturer=new_maker, model="Fetch race model", slug="fetch-race-model")
        # Only the new maker's chain makes "S/N PARTIAL-1" the module's "PARTIAL-1".
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1", manufacturer=new_maker
        )
        transceivers = [{"entity_physical_index": 20, "serial": "S/N PARTIAL-1", "model": "SFP-RULE", "type": "sfp"}]
        original = modules_view.load_inventory_policy

        def retype_before_the_policy_read(page_device_id):
            # The refresh read its sync device before the LibreNMS fetch; the type changes during the fetch.
            Device.objects.filter(pk=device.pk).update(device_type=new_type)
            return original(page_device_id)

        monkeypatch.setattr(modules_view, "load_inventory_policy", retype_before_the_policy_read)
        snapshot_key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        try:
            _response, context, _request = _refresh(device, librenms_server, transceivers=(True, transceivers))
            payload = cache.get(snapshot_key)
        finally:
            cache.delete(snapshot_key)
            cache.delete(SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY))

        device.refresh_from_db()
        assert context.get("table") is not None
        assert payload["serial_context"] == SerialRules.load().for_device(device).fingerprint
        assert [item["entPhysicalIndex"] for item in payload["inventory"]] == [1], "the merge used the old type"

    def test_a_master_type_change_after_the_policy_load_does_not_reach_the_stamp(self, librenms_server, monkeypatch):
        """A non-master owner's snapshot uses the master that the policy loaded, not one read after the load."""
        from netbox_librenms_plugin.serial_identity import SerialRules
        from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis
        from django.core.cache import cache

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, sync_snapshot_key
        from netbox_librenms_plugin.tests.conftest import make_module_bay, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
        from netbox_librenms_plugin.views.base import modules_view

        chassis, (master, owner) = make_virtual_chassis_members("master-retype-after-load")
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
        seed_stored_mapping(owner, {SERVER_KEY: 42}, save=True)
        for member in (master, owner):
            make_module_bay(member, "Bay 1")
        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
        new_maker = Manufacturer.objects.create(name="Master retype maker", slug="master-retype-maker")
        new_type = DeviceType.objects.create(
            manufacturer=new_maker, model="Master retype model", slug="master-retype-model"
        )
        # Only the new master maker's chain makes "S/N PARTIAL-1" the module's "PARTIAL-1".
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1", manufacturer=new_maker
        )
        observed = SerialRules.load().for_device(Device.objects.get(pk=owner.pk)).fingerprint
        transceivers = [{"entity_physical_index": 20, "serial": "S/N PARTIAL-1", "model": "SFP-RULE", "type": "sfp"}]
        original = modules_view.load_inventory_policy
        loads = []

        def retype_the_master_after_the_first_load(page_device_id):
            policy = original(page_device_id)
            if not loads:
                # The policy has read the master; its type changes before the merge reads a context.
                Device.objects.filter(pk=master.pk).update(device_type=new_type)
            loads.append(policy)
            return policy

        monkeypatch.setattr(modules_view, "load_inventory_policy", retype_the_master_after_the_first_load)
        snapshot_key = sync_snapshot_key(owner, "inventory", SERVER_KEY)
        try:
            _refresh(owner, librenms_server, transceivers=(True, transceivers))
            payload = cache.get(snapshot_key)
        finally:
            cache.delete(snapshot_key)
            cache.delete(SyncCacheConsistency(owner).state_key(SyncTab.MODULES, SERVER_KEY))

        assert loads, "precondition: the refresh loaded a policy"
        assert payload["serial_context"] == observed == loads[0].chassis_serial_context().fingerprint
        assert [item["entPhysicalIndex"] for item in payload["inventory"]] == [1, 20], "the merge read the new master"


@pytest.mark.django_db
class TestASnapshotReaderKeepsAFreshSnapshot:
    """A reader that rejects the snapshot it read must not delete one that another request stored since."""

    @pytest.fixture(autouse=True)
    def _configure_server(self, settings):
        configure_default_librenms_server(settings)

    @staticmethod
    def _stored_after_the_read(monkeypatch, key, old, fresh):
        """Return *old* to the reader of *key*, as if another request stored *fresh* just after the read."""
        from django.core.cache import cache, caches

        backend = caches["default"]
        real_get = backend.get
        injected = []

        def get(cache_key, *args, **kwargs):
            if cache_key == key and not injected:
                # One concurrent write: later reads see the real cache.
                injected.append(True)
                cache.set(key, fresh, timeout=300)
                return old
            return real_get(cache_key, *args, **kwargs)

        monkeypatch.setattr(backend, "get", get)
        return real_get

    @pytest.mark.parametrize("rejected", ["malformed", "other-device", "other-serial-context"])
    def test_the_module_table_reader(self, monkeypatch, rejected):
        from netbox_librenms_plugin.serial_identity import SerialRules
        from django.core.cache import cache

        from netbox_librenms_plugin.serial_identity import SerialContext
        from netbox_librenms_plugin.sync_cache import sync_snapshot_key
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device(f"fresh-snapshot-{rejected}", librenms_cf={SERVER_KEY: 42})
        make_module_bay(device, "Bay 1")
        context = SerialRules.load().for_device(device)
        fresh = module_inventory_cache_payload(INVENTORY, 42, None, serial_context=context)
        old = {
            "malformed": {"version": -1},
            "other-device": module_inventory_cache_payload(INVENTORY, 43, None, serial_context=context),
            "other-serial-context": module_inventory_cache_payload(
                INVENTORY, 42, None, serial_context=SerialContext(None, (), unavailable="another context")
            ),
        }[rejected]
        key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        real_get = self._stored_after_the_read(monkeypatch, key, old, fresh)
        request = make_request("get", {"tab": "modules", "server_key": SERVER_KEY})
        view = DeviceModuleTableView()
        view.setup(request, pk=device.pk)
        view.cache_only = True
        try:
            read = view.get_context_data(request, device, server_key=SERVER_KEY)
            stored = real_get(key)
        finally:
            cache.delete(key)

        assert read["table"] is None
        assert stored == fresh

    def test_the_module_writer_reader(self, monkeypatch):
        from netbox_librenms_plugin.serial_identity import SerialRules
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import sync_snapshot_key
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.mixins import CacheMixin
        from netbox_librenms_plugin.views.sync.modules import _get_cached_module_snapshot

        device = make_device("fresh-snapshot-writer", librenms_cf={SERVER_KEY: 42})
        fresh = module_inventory_cache_payload(
            INVENTORY, 42, None, serial_context=SerialRules.load().for_device(device)
        )
        key = sync_snapshot_key(device, "inventory", SERVER_KEY)
        real_get = self._stored_after_the_read(monkeypatch, key, {"version": -1}, fresh)
        try:
            # The action tests the raw snapshot for None, so an empty dict must not stand in for a miss.
            snapshot = _get_cached_module_snapshot(device, SERVER_KEY, CacheMixin().get_cache_key)
            stored = real_get(key)
        finally:
            cache.delete(key)

        assert snapshot is None
        assert stored == fresh
