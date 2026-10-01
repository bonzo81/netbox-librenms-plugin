"""Real-ORM tests for module interface binding, adoption, and VC name normalization scopes."""

import pytest

from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post as view_post
from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_row_digest

pytestmark = pytest.mark.django_db


def _module_with_interfaces(device, bay_name, model, names):
    """Install a module and give it interfaces named *names*."""
    from dcim.models import Interface

    module = install_module(device, bay_name, model)
    for name in names:
        Interface.objects.create(device=device, module=module, name=name, type="other")
    return module


# The device custom field and the seeded snapshot must carry the same id, or the cache reads stale.
ADOPTION_LIBRENMS_ID = 71
ADOPTION_ENT_INDEX = 710


def _adoption_inventory_row(model, name):
    """A cached row with no port identity, so only the template adoption can change NetBox."""
    return {
        "entPhysicalIndex": ADOPTION_ENT_INDEX,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
    }


class TestInterfacePortBinding:
    """Binding a LibreNMS port_id falls back through name, coordinates, and the lone interface."""

    def _bind(self, device, item, module_pk):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        return _bind_interface_librenms_id(device, item, module_pk, "default", Interface.objects.all())

    def test_coordinates_pick_the_module_interface_when_no_name_matches(self):
        device = make_device_with_module_bays("bind-coordinates", ["Slot 1"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-COORD-CARD", ["Ethernet1/17", "Ethernet1/18"])
        item = {"_librenms_port_id": 8801, "_librenms_ifname": "port 1/17"}

        result = self._bind(device, item, module.pk)

        assert result["status"] == "bound"
        assert result["interface"] == "Ethernet1/17"
        bound = module.interfaces.get(name="Ethernet1/17")
        assert read_mapping(bound).own_id("default") == 8801

    def test_a_lone_module_interface_is_used_when_nothing_else_narrows_it(self):
        device = make_device_with_module_bays("bind-lone", ["Slot 1"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-LONE-CARD", ["Uplink"])
        item = {"_librenms_port_id": 8802, "_librenms_ifdescr": "Unmatched Label"}

        result = self._bind(device, item, module.pk)

        assert result["status"] == "bound"
        assert result["interface"] == "Uplink"
        # The identity has to reach the database, not just the returned dict.
        assert read_mapping(module.interfaces.get(name="Uplink")).own_id("default") == 8802

    def test_no_module_context_and_no_name_match_reports_a_skip(self):
        device = make_device("bind-nothing")
        make_interface(device, "Ethernet1")
        item = {"_librenms_port_id": 8803, "_librenms_ifname": "Unmatched Label"}

        result = self._bind(device, item, None)

        assert result == {
            "status": "skipped",
            "reason": "no matching interface found for port_id 8803",
        }

    def test_a_name_bound_to_another_port_yields_to_the_next_name(self):
        """Ethernet1/1 is bound to port 8999, so the port's ifDescr Uplink1 must take port 8804."""
        device = make_device_with_module_bays("bind-name-rebound", ["Slot 1"])
        taken = make_interface(device, "Ethernet1/1")
        taken.custom_field_data["librenms_id"] = {"default": 8999}
        taken.save(update_fields=["custom_field_data"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-REBOUND-CARD", ["Uplink1"])
        item = {"_librenms_port_id": 8804, "_librenms_ifname": "Ethernet1/1", "_librenms_ifdescr": "Uplink1"}

        result = self._bind(device, item, module.pk)

        assert result["status"] == "bound"
        assert result["interface"] == "Uplink1"
        taken.refresh_from_db()
        assert read_mapping(taken).own_id("default") == 8999
        assert read_mapping(module.interfaces.get(name="Uplink1")).own_id("default") == 8804

    def test_a_refused_name_does_not_fall_through_to_the_lone_module_interface(self):
        """Ethernet1/1 names port 8805 but is bound to 8999, so the unrelated lone Uplink must stay unbound."""
        device = make_device_with_module_bays("bind-name-refused", ["Slot 1"])
        taken = make_interface(device, "Ethernet1/1")
        taken.custom_field_data["librenms_id"] = {"default": 8999}
        taken.save(update_fields=["custom_field_data"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-REFUSED-CARD", ["Uplink"])
        item = {"_librenms_port_id": 8805, "_librenms_ifname": "Ethernet1/1"}

        result = self._bind(device, item, module.pk)

        assert result == {
            "status": "conflict",
            "reason": "Ethernet1/1 is already bound to a different LibreNMS port; not overwriting",
        }
        taken.refresh_from_db()
        assert read_mapping(taken).own_id("default") == 8999
        assert read_mapping(module.interfaces.get(name="Uplink")).own_id("default") is None

    @pytest.mark.parametrize("stored", [None, {"default": 8999}], ids=["unbound", "bound-elsewhere"])
    def test_a_named_interface_outside_the_change_scope_is_not_passed_over(self, stored):
        """The table sees every interface, so the writer must choose from all of them and then check scope."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = make_device_with_module_bays(f"bind-name-out-of-scope-{bool(stored)}", ["Slot 1"])
        named = make_interface(device, "Ethernet1/1")
        if stored is not None:
            named.custom_field_data["librenms_id"] = stored
            named.save(update_fields=["custom_field_data"])
        module = _module_with_interfaces(device, "Slot 1", "BIND-SCOPE-CARD", ["Uplink"])
        item = {"_librenms_port_id": 8806, "_librenms_ifname": "Ethernet1/1"}

        result = _bind_interface_librenms_id(device, item, module.pk, "default", Interface.objects.exclude(pk=named.pk))

        assert result == {"status": "skipped", "reason": "matching interface is not available for port_id 8806"}
        assert read_mapping(module.interfaces.get(name="Uplink")).own_id("default") is None


def _render_module_tab(device, user):
    """Render the real module table from its seeded cache."""
    from django.urls import reverse

    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request(
        "get",
        {"tab": "modules", "server_key": "default"},
        user=user,
        path=reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk}),
    )
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    context = view.get_context_data(request, device)
    return context["table"].as_html(request)


class TestModuleTableShowsTheWriterChoice:
    """The modules table shows the interface that Update Interface then binds."""

    def _seed(self, tag, interface_names, identity):
        """Install a module with *interface_names* and seed its cached row with port 8811."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device_with_module_bays(f"table-choice-{tag}", ["Slot 1"])
        module = _module_with_interfaces(device, "Slot 1", f"TABLE-CHOICE-{tag.upper()}", interface_names)
        device.custom_field_data["librenms_id"] = {"default": ADOPTION_LIBRENMS_ID}
        device.save(update_fields=["custom_field_data"])
        device.__dict__.pop("cf", None)
        row = {**_adoption_inventory_row(module.module_type.model, "Slot 1"), "_librenms_port_id": 8811, **identity}
        cache_key = seed_inventory(DeviceModuleTableView(), device, [row], librenms_id=ADOPTION_LIBRENMS_ID)
        return device, module, row, cache_key

    def _post_update(self, device, module, row, user, live_librenms):
        """POST Update Interface for the seeded row and return the response and its request."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        request = make_request(
            "post",
            {
                "module_id": str(module.pk),
                "ent_index": str(ADOPTION_ENT_INDEX),
                "server_key": "default",
                "inventory_binding": module_inventory_binding_token(
                    device.pk,
                    "default",
                    "update_module_interface",
                    {"module_id": module.pk},
                    ADOPTION_ENT_INDEX,
                    module_inventory_row_digest(row),
                ),
            },
            user=user,
            path="/modules/",
        )
        view = UpdateModuleInterfaceView()
        view._librenms_api = live_librenms.api
        return view_post(view, request, pk=device.pk), request

    @pytest.mark.parametrize(
        ("interface_names", "identity", "expected", "source"),
        [
            (["Ethernet1/17", "Ethernet1/18"], {"_librenms_ifname": "port 1/17"}, "Ethernet1/17", "coordinates"),
            (["Ethernet2/17"], {"_librenms_ifname": "port 1/17"}, "Ethernet2/17", "coordinates"),
            (["Uplink"], {"_librenms_ifdescr": "Unmatched Label"}, "Uplink", "lone module interface"),
            (["Uplink"], {"_librenms_ifname": "Te1/1/5"}, "Uplink", "lone module interface"),
        ],
        ids=["coordinates", "lone-port-number-agrees", "lone-interface", "lone-interface-without-coordinates"],
    )
    def test_the_update_binds_the_interface_the_table_shows(
        self, live_librenms, interface_names, identity, expected, source
    ):
        from django.core.cache import cache

        device, module, row, cache_key = self._seed(source[:5], interface_names, identity)
        user = make_superuser()
        shown = module.interfaces.get(name=expected)
        try:
            content = _render_module_tab(device, user)
            response, _request = self._post_update(device, module, row, user, live_librenms)
        finally:
            cache.delete(cache_key)

        assert (
            f'<a href="{shown.get_absolute_url()}" title="Matched by {source}, confidence low">{expected}</a>'
            in content
        )
        assert "Update Interface" in content
        assert response.status_code == 302
        assert [
            interface.name for interface in module.interfaces.all() if read_mapping(interface).own_id("default") == 8811
        ] == [expected]

    @pytest.mark.parametrize("lone_name", ["Te1/2/1", "Te1/1/7"], ids=["module-and-port-differ", "port-differs"])
    def test_a_lone_interface_with_another_port_number_shows_no_match(self, live_librenms, lone_name):
        """The item names port Te1/1/5, so a lone interface with another port number is not that port."""
        from django.core.cache import cache

        device, module, row, cache_key = self._seed("contra", [lone_name], {"_librenms_ifname": "Te1/1/5"})
        lone = module.interfaces.get(name=lone_name)
        user = make_superuser()
        try:
            content = _render_module_tab(device, user)
            _response, request = self._post_update(device, module, row, user, live_librenms)
        finally:
            cache.delete(cache_key)

        assert lone.get_absolute_url() not in content
        assert "Update Interface" not in content
        assert any("no matching interface found for port_id 8811" in text for text in message_texts(request))
        lone.refresh_from_db()
        assert read_mapping(lone).own_id("default") is None

    def test_a_local_oob_holder_is_shown_and_bound(self, live_librenms):
        """The writer binds the one interface that holds the port, an OOB holder included, so the table shows it."""
        from django.core.cache import cache

        device, module, row, cache_key = self._seed("oob", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        uplink = module.interfaces.get(name="Uplink")
        uplink.custom_field_data["librenms_id"] = {"default": {"oob": {"id": 8811}}}
        uplink.save(update_fields=["custom_field_data"])
        user = make_superuser()
        try:
            content = _render_module_tab(device, user)
            self._post_update(device, module, row, user, live_librenms)
        finally:
            cache.delete(cache_key)

        assert (
            f'<a href="{uplink.get_absolute_url()}" title="Matched by port id, confidence high">Uplink</a>' in content
        )
        uplink.refresh_from_db()
        assert read_mapping(uplink).own_id("default") == 8811

    @pytest.mark.parametrize(
        "stored",
        [{"default": 8811}, {"default": {"id": 8999, "oob": {"id": 8811}}}],
        ids=["already-bound", "own-id-is-another-port"],
    )
    def test_a_holder_whose_own_id_is_set_offers_no_update(self, live_librenms, stored):
        """The table reads the holder's own ID: the same port needs no bind, and another port refuses it."""
        from django.core.cache import cache

        device, module, _row, cache_key = self._seed("own", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        uplink = module.interfaces.get(name="Uplink")
        uplink.custom_field_data["librenms_id"] = stored
        uplink.save(update_fields=["custom_field_data"])
        try:
            content = _render_module_tab(device, make_superuser())
        finally:
            cache.delete(cache_key)

        assert (
            f'<a href="{uplink.get_absolute_url()}" title="Matched by port id, confidence high">Uplink</a>' in content
        )
        assert "Update Interface" not in content

    def test_a_port_held_by_another_device_shows_no_match(self, live_librenms):
        """The writer refuses a port that another device's interface holds, so the table shows no match."""
        from django.core.cache import cache

        device, module, _row, cache_key = self._seed("held", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        holder = make_interface(make_device("table-choice-holder"), "Ethernet9")
        holder.custom_field_data["librenms_id"] = {"default": 8811}
        holder.save(update_fields=["custom_field_data"])
        try:
            content = _render_module_tab(device, make_superuser())
        finally:
            cache.delete(cache_key)

        assert module.interfaces.get(name="Uplink").get_absolute_url() not in content
        assert "Update Interface" not in content

    def test_a_module_interface_bound_to_another_port_shows_no_match(self, live_librenms):
        """The writer refuses the lone interface when it holds another port, so the table shows no match."""
        from dcim.models import Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module, row, cache_key = self._seed("bound", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        uplink = module.interfaces.get(name="Uplink")
        uplink.custom_field_data["librenms_id"] = {"default": 8999}
        uplink.save(update_fields=["custom_field_data"])
        try:
            content = _render_module_tab(device, make_superuser())
        finally:
            cache.delete(cache_key)

        assert uplink.get_absolute_url() not in content
        assert _bind_interface_librenms_id(device, row, module.pk, "default", Interface.objects.all()) == {
            "status": "conflict",
            "reason": "Uplink is already bound to a different LibreNMS port; not overwriting",
        }

    @pytest.mark.parametrize("refusal", ["held-elsewhere", "other-module"])
    def test_a_refused_port_bind_hides_the_template_adoption_action(self, live_librenms, refusal):
        """The writer skips the adoption when the port bind is refused, so the table offers no Update Interface."""
        from dcim.models import InterfaceTemplate
        from django.core.cache import cache

        device, module, _row, cache_key = self._seed(f"adopt-{refusal[:4]}", [], {"_librenms_ifname": "Ethernet1/1"})
        InterfaceTemplate.objects.create(module_type=module.module_type, name="Uplink", type="other")
        make_interface(device, "Uplink")
        if refusal == "held-elsewhere":
            holder = make_interface(make_device("table-choice-adopt-holder"), "Ethernet9")
            holder.custom_field_data["librenms_id"] = {"default": 8811}
            holder.save(update_fields=["custom_field_data"])
        else:
            make_module_bay(device, "Slot 2")
            _module_with_interfaces(device, "Slot 2", "TABLE-CHOICE-OTHER", ["Ethernet1/1"])
        try:
            content = _render_module_tab(device, make_superuser())
        finally:
            cache.delete(cache_key)

        assert module.module_type.model in content
        assert "Update Interface" not in content

    def test_a_port_that_two_interfaces_hold_shows_no_match(self, live_librenms):
        """The writer refuses a port held twice, so the table does not show the local holder as a match."""
        from django.core.cache import cache

        device, _module, _row, cache_key = self._seed("twice", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        local = make_interface(device, "Ethernet9")
        for holder in (local, make_interface(make_device("table-choice-twice-other"), "Ethernet9")):
            holder.custom_field_data["librenms_id"] = {"default": 8811}
            holder.save(update_fields=["custom_field_data"])
        try:
            content = _render_module_tab(device, make_superuser())
        finally:
            cache.delete(cache_key)

        assert local.get_absolute_url() not in content
        assert "Update Interface" not in content

    def test_an_interface_the_user_may_not_change_offers_no_update(self, live_librenms):
        """The writer skips a choice outside the change scope, so the table offers no Update Interface."""
        from dcim.models import Device, Interface, Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        device, module, _row, cache_key = self._seed("nochange", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        user = make_user_with_perms(
            "table-choice-nochange-user", [("view", Device), ("view", Module), ("view", Interface)]
        )
        user = grant(user, "change", Interface, constraints={"name": "Management1"})
        try:
            content = _render_module_tab(device, user)
        finally:
            cache.delete(cache_key)

        assert module.interfaces.get(name="Uplink").get_absolute_url() in content
        assert "Update Interface" not in content

    def test_an_interface_the_user_may_not_view_is_not_shown(self, live_librenms):
        """The choice reads every interface, so the table must hide one outside the user's view scope."""
        from dcim.models import Device, Interface, Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        device, module, _row, cache_key = self._seed("hidden", ["Uplink"], {"_librenms_ifdescr": "Unmatched Label"})
        user = make_user_with_perms("table-choice-hidden-user", [("view", Device), ("view", Module)])
        user = grant(user, "view", Interface, constraints={"name": "Management1"})
        try:
            content = _render_module_tab(device, user)
        finally:
            cache.delete(cache_key)

        assert "TABLE-CHOICE-HIDDEN" in content
        assert module.interfaces.get(name="Uplink").get_absolute_url() not in content


class TestRecordBindOutcome:
    """A failed bind is reported in the install summary without claiming a change."""

    def test_a_non_bound_outcome_is_recorded_as_skipped(self):
        from netbox_librenms_plugin.views.sync.modules import _record_bind_outcome

        skipped = []
        changed = _record_bind_outcome(
            {"status": "conflict", "reason": "port_id 5 already assigned"},
            {"name": "CARD → Slot 1"},
            skipped,
        )

        assert changed is False
        assert skipped == ["CARD → Slot 1: port_id 5 already assigned"]

    def test_a_bound_outcome_reports_whether_netbox_changed(self):
        from netbox_librenms_plugin.views.sync.modules import _record_bind_outcome

        skipped = []
        assert _record_bind_outcome({"status": "bound", "changed": True}, {"name": "row"}, skipped) is True
        assert _record_bind_outcome({"status": "bound", "changed": False}, {"name": "row"}, skipped) is False
        assert skipped == []


class TestVCNameNormalizationScopes:
    """Interfaces outside the caller's change or delete grant are skipped, never rewritten."""

    def _member_with_module(self, tag, interface_name="Te1/1/1"):
        from dcim.models import Interface

        first = make_device(f"{tag}-first")
        device = make_device(f"{tag}-second")
        make_virtual_chassis(f"{tag}-vc", first, device)
        bay = make_module_bay(device, f"{tag} Bay")
        module = install_module(device, bay.name, f"{tag.upper()}-CARD")
        interface = Interface.objects.create(device=device, module=module, name=interface_name, type="other")
        return device, module, interface

    def _normalize(self, device, module, changeable, deletable):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        return _normalize_module_interface_names_for_vc_member(device, module, changeable, deletable)

    def test_an_interface_outside_the_change_grant_is_skipped(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-nochange")

        result = self._normalize(device, module, Interface.objects.exclude(pk=interface.pk), Interface.objects.all())

        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert interface.name == "Te1/1/1"

    def test_a_name_that_does_not_carry_a_member_position_is_left_alone(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-norewrite", interface_name="xe-0/0/0")

        result = self._normalize(device, module, Interface.objects.all(), Interface.objects.all())

        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
        assert interface.name == "xe-0/0/0"

    def test_a_conflict_outside_the_change_grant_is_skipped(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-conflict-nochange")
        conflict = make_interface(device, "Te2/1/1")

        result = self._normalize(device, module, Interface.objects.exclude(pk=conflict.pk), Interface.objects.all())

        conflict.refresh_from_db()
        interface.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert conflict.module_id is None
        assert interface.name == "Te1/1/1"

    def test_an_undeletable_generated_interface_blocks_the_adoption(self):
        from dcim.models import Interface

        device, module, interface = self._member_with_module("vcscope-nodelete")
        conflict = make_interface(device, "Te2/1/1")

        result = self._normalize(device, module, Interface.objects.all(), Interface.objects.exclude(pk=interface.pk))

        conflict.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert conflict.module_id is None
        assert Interface.objects.filter(pk=interface.pk).exists()


class TestUpdateModuleInterfaceAdoption:
    """The update action adopts standalone template interfaces when the cached row binds nothing."""

    def _module_with_templates(self, tag, template_names):
        from dcim.models import InterfaceTemplate, Module

        device = make_device(f"{tag}-device", librenms_cf={"default": ADOPTION_LIBRENMS_ID})
        bay = make_module_bay(device, f"{tag} Bay")
        module_type = make_module_type(f"{tag.upper()}-CARD")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")
        # Templates are added after the install so the interfaces stay standalone.
        for name in template_names:
            InterfaceTemplate.objects.create(module_type=module_type, name=name, type="other")
        return device, module

    def _seed(self, view, device, module, inventory_item=None):
        inventory_item = inventory_item or _adoption_inventory_row(module.module_type.model, module.module_bay.name)
        seed_inventory(
            view,
            device,
            [inventory_item],
            librenms_id=ADOPTION_LIBRENMS_ID,
        )

    def _post(self, view_class, device, data, live_librenms, module=None):
        data = data.copy()
        if module is not None:
            inventory_item = _adoption_inventory_row(module.module_type.model, module.module_bay.name)
            data["inventory_binding"] = module_inventory_binding_token(
                device.pk,
                data["server_key"],
                "update_module_interface",
                {"module_id": module.pk},
                data["ent_index"],
                module_inventory_row_digest(inventory_item),
            )
        request = make_request("post", data, user=make_superuser(), path="/modules/")
        view = view_class()
        view._librenms_api = live_librenms.api
        if module is not None:
            self._seed(view, device, module, inventory_item)
        return view, request, view_post(view, request, pk=device.pk)

    def test_a_missing_module_id_reports_an_error(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, _module = self._module_with_templates("adopt-bad-id", ["Ethernet1/1"])

        _view, request, response = self._post(
            UpdateModuleInterfaceView, device, {"server_key": "default"}, live_librenms
        )

        assert response.status_code == 302
        assert "Missing or invalid module ID." in message_texts(request, "error")

    def test_standalone_template_interfaces_are_adopted(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-plain", ["Ethernet1/1", "Ethernet1/2"])
        first = make_interface(device, "Ethernet1/1")
        second = make_interface(device, "Ethernet1/2")
        untouched = make_interface(device, "Management1")

        _view, request, response = self._post(
            UpdateModuleInterfaceView,
            device,
            {"module_id": str(module.pk), "ent_index": str(ADOPTION_ENT_INDEX), "server_key": "default"},
            live_librenms,
            module=module,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        untouched.refresh_from_db()
        assert response.status_code == 302
        assert first.module_id == module.pk
        assert second.module_id == module.pk
        assert untouched.module_id is None
        assert any("adopted 2 existing standalone interface(s)" in text for text in message_texts(request, "success"))

    def test_a_module_type_with_no_matching_standalone_interface_reports_the_reason(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-nothing", ["Ethernet1/1"])

        _view, request, response = self._post(
            UpdateModuleInterfaceView,
            device,
            {"module_id": str(module.pk), "ent_index": str(ADOPTION_ENT_INDEX), "server_key": "default"},
            live_librenms,
            module=module,
        )

        assert response.status_code == 302
        assert any("no matching standalone interfaces found" in text for text in message_texts(request, "warning"))

    def test_an_interface_outside_the_change_grant_is_not_adopted(self, live_librenms):
        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module = self._module_with_templates("adopt-scoped", ["Ethernet1/1"])
        blocked = make_interface(device, "Ethernet1/1")
        user = make_user_with_perms("adopt-scoped-user", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints={"name": "Management1"})
        inventory_item = _adoption_inventory_row(module.module_type.model, module.module_bay.name)
        request = make_request(
            "post",
            {
                "module_id": str(module.pk),
                "ent_index": str(ADOPTION_ENT_INDEX),
                "server_key": "default",
                "inventory_binding": module_inventory_binding_token(
                    device.pk,
                    "default",
                    "update_module_interface",
                    {"module_id": module.pk},
                    ADOPTION_ENT_INDEX,
                    module_inventory_row_digest(inventory_item),
                ),
            },
            user=user,
            path="/modules/",
        )
        view = UpdateModuleInterfaceView()
        view._librenms_api = live_librenms.api
        self._seed(view, device, module, inventory_item)

        response = view_post(view, request, pk=device.pk)

        blocked.refresh_from_db()
        assert response.status_code == 302
        assert blocked.module_id is None
        assert any("no matching standalone interfaces found" in text for text in message_texts(request, "warning"))
