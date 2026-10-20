"""
Built-in units of a fixed platform that takes transceivers but no modules (Nokia 7250 IXR-X).

The system board reports itself as "Slot 1" (ioModule), "MDA 1/1" (mdaModule, below Slot 1) and "Slot A"
(cpmModule), all with the chassis part number and serial. The transceiver in port 1/1/c1 sits below MDA 1/1.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
PART_NUMBER = "3HE15789AARF01"
OPTIC_MODEL = "QSFP-DD-400G-DR4"
CHASSIS, SLOT_1, MDA, OPTIC, SLOT_A = 1, 50, 60, 70, 80
BUILT_IN_UNITS = (SLOT_1, MDA, SLOT_A)


def ixr_inventory():
    """Return the ENTITY-MIB rows LibreNMS reports for the system board and one transceiver."""
    rows = [
        (CHASSIS, 0, "chassis", "Chassis", PART_NUMBER, "NS230961125"),
        (SLOT_1, CHASSIS, "ioModule", "Slot 1", PART_NUMBER, "NS230961125"),
        (MDA, SLOT_1, "mdaModule", "MDA 1/1", PART_NUMBER, "NS230961125"),
        (OPTIC, MDA, "port", "1/1/c1", OPTIC_MODEL, "OPTIC-SN-1"),
        (SLOT_A, CHASSIS, "cpmModule", "Slot A", PART_NUMBER, "NS230961125"),
    ]
    return [
        {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": phys_class,
            "entPhysicalName": name,
            "entPhysicalDescr": name,
            "entPhysicalModelName": model,
            "entPhysicalSerialNum": serial,
            "entPhysicalParentRelPos": 1,
        }
        for index, parent, phys_class, name, model, serial in rows
    ]


def seed_snapshot(device, inventory):
    """Cache *inventory* as the device's LibreNMS snapshot, as a Modules refresh stores it."""
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload
    from netbox_librenms_plugin.views.mixins import CacheMixin

    payload = trusted_module_inventory_payload(device, inventory, server_key=SERVER_KEY)
    cache.set(CacheMixin().get_cache_key(device, "inventory", server_key=SERVER_KEY), payload)


def ixr_device(name, *, part_number=PART_NUMBER):
    """Create a 7250 IXR-X with a device-level bay for port 1/1/c1 and cache its inventory snapshot."""
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type_with_bays

    device = make_device_with_module_bays(name, ["1/1/c1"])
    device.device_type.part_number = part_number
    device.device_type.save()
    make_module_type_with_bays(OPTIC_MODEL, manufacturer=device.device_type.manufacturer)
    seed_snapshot(device, ixr_inventory())
    return device


def model_rule(**fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        name="Fixed-platform built-in units",
        match_type="model_matches_device_type",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        **fields,
    )


def tab_rows(device, *, user=None):
    """Build the Modules tab from its cached snapshot through the real view and return its rows by index."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request(
        "get",
        {"tab": "modules", "server_key": SERVER_KEY},
        user=user,
        path=reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk}),
    )
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    context = view.get_context_data(request, device, server_key=SERVER_KEY)
    return {row["ent_physical_index"]: row for row in context["table"].data}


class TestNestedBuiltInUnits:
    """The model rule looks through ancestors that are built-in units themselves."""

    def test_the_model_rule_hides_every_built_in_unit_and_lifts_the_transceiver(self):
        device = ixr_device("ixr-nested")
        model_rule()

        rows = tab_rows(device)

        assert set(rows) == {OPTIC}
        assert (rows[OPTIC]["depth"], rows[OPTIC]["module_bay"]) == (0, "1/1/c1")

    def test_without_the_part_number_the_built_in_units_stay_rows(self):
        device = ixr_device("ixr-no-part-number", part_number="")
        model_rule()

        rows = tab_rows(device)

        assert {SLOT_1, SLOT_A, OPTIC} <= set(rows)
        assert rows[OPTIC]["depth"] > 0

    def test_a_unit_below_a_real_module_is_not_matched(self):
        from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, _check_ignore_rules

        rule = model_rule()
        inventory = {item["entPhysicalIndex"]: item for item in ixr_inventory()}
        # A line card of its own model holds the unit, so the unit is a real FRU on that card.
        inventory[SLOT_1] = {**inventory[SLOT_1], "entPhysicalModelName": "3HE16474AA"}
        identity = DeviceIdentity(type_models=frozenset({PART_NUMBER.casefold()}))

        assert _check_ignore_rules(inventory[MDA], inventory[SLOT_1], [rule], inventory, identity) is None
        assert _check_ignore_rules(inventory[SLOT_1], inventory[CHASSIS], [rule], inventory, identity) is None

    def test_a_built_in_chain_must_end_at_the_chassis(self):
        from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, _check_ignore_rules

        rule = model_rule()
        inventory = {item["entPhysicalIndex"]: item for item in ixr_inventory()}
        inventory[SLOT_1] = {**inventory[SLOT_1], "entPhysicalContainedIn": 999}
        identity = DeviceIdentity(type_models=frozenset({PART_NUMBER.casefold()}))

        assert _check_ignore_rules(inventory[MDA], inventory[SLOT_1], [rule], inventory, identity) is None

    def test_the_serial_rule_still_takes_direct_chassis_children_only(self):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        device = ixr_device("ixr-serial-rule", part_number="")
        device.serial = "NS230961125"
        device.save()
        InventoryIgnoreRule.objects.create(
            name="Embedded board", match_type="serial_matches_device", pattern="", action="transparent"
        )

        rows = tab_rows(device)

        assert SLOT_1 not in rows
        assert SLOT_A not in rows
        assert MDA in rows

    def test_the_lifted_transceiver_installs_into_the_device_bay(self):
        from dcim.models import Module

        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request,
            post,
            render_module_tab,
            rendered_module_form,
        )
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = ixr_device("ixr-install")
        model_rule()
        _action, fields = rendered_module_form(render_module_tab(device), "install-module", OPTIC)

        post(InstallModuleView(), make_request(data=fields), pk=device.pk)

        module = Module.objects.get(device=device, serial="OPTIC-SN-1")
        assert (module.module_bay.name, module.module_bay.module_id) == ("1/1/c1", None)


# --- Mark built-in -----------------------------------------------------------------------------------


class _Controls(HTMLParser):
    """Collect each rendered "Mark built-in" control with the index of its table row."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.row, self.controls = None, {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "data-ent-index" in attrs:
            self.row = int(attrs["data-ent-index"])
        if attrs.get("data-action") == "mark-built-in":
            url = urlsplit(attrs.get("hx-get") or attrs.get("href"))
            self.controls[self.row] = (tag, url.path, dict(parse_qsl(url.query, keep_blank_values=True)))


def controls(device, *, user=None):
    from netbox_librenms_plugin.tests.view_test_helpers import render_module_tab

    return _Controls(render_module_tab(device, user=user)).controls


def modal_form(device, index, *, user=None):
    """Open the confirm modal of row *index* through the real view and return the fields its form posts."""
    from netbox_librenms_plugin.tests.view_test_helpers import get, make_request, rendered_module_form
    from netbox_librenms_plugin.views.sync.modules import MarkBuiltInView

    tag, _path, query = controls(device)[index]
    assert tag == "button"
    response = get(MarkBuiltInView(), make_request("get", query, user=user), pk=device.pk)
    assert response.status_code == 200, response.content
    action, fields = rendered_module_form(response.content.decode(), "mark-built-in", index)
    assert action == reverse("plugins:netbox_librenms_plugin:mark_built_in", kwargs={"pk": device.pk})
    return fields


def mark(device, fields, *, user=None):
    """POST the confirm form through the real view and return the request, for its messages."""
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post
    from netbox_librenms_plugin.views.sync.modules import MarkBuiltInView

    request = make_request(data=fields, user=user)
    post(MarkBuiltInView(), request, pk=device.pk)
    return request


def built_in_rules():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return [rule for rule in InventoryIgnoreRule.objects.order_by("pk") if rule.hides_built_in_units]


def written(device):
    """Return what the action may write: the part number of the device type and every ignore rule."""
    from dcim.models import DeviceType

    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return (
        DeviceType.objects.get(pk=device.device_type_id).part_number,
        list(InventoryIgnoreRule.objects.order_by("pk").values()),
    )


def user_with(name, *perms):
    from dcim.models import Device, Interface, ModuleBay

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    return make_user_with_perms(name, [("view", Device), ("view", Interface), ("view", ModuleBay), *perms])


class TestCandidate:
    """built_in_candidate_model(): the one test that the table and the writer apply."""

    @staticmethod
    def _model(index, *, type_models=(), has_bay=False, **changes):
        from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, built_in_candidate_model

        inventory = {item["entPhysicalIndex"]: item for item in ixr_inventory()}
        inventory[index] = {**inventory[index], **changes}
        identity = DeviceIdentity(type_models=frozenset(type_models))
        return built_in_candidate_model(inventory[index], inventory, identity, has_bay=has_bay)

    def test_a_chassis_level_unit_without_a_bay_is_a_candidate(self):
        assert self._model(SLOT_1) == PART_NUMBER
        assert self._model(SLOT_A) == PART_NUMBER

    @pytest.mark.parametrize(
        ("index", "kwargs"),
        [
            (SLOT_1, {"has_bay": True}),
            (SLOT_1, {"type_models": {PART_NUMBER.casefold()}}),
            (SLOT_1, {"entPhysicalModelName": ""}),
            (SLOT_1, {"entPhysicalClass": "powerSupply"}),
            (SLOT_1, {"_source": "oob"}),
            (SLOT_1, {"entPhysicalContainedIn": 999}),
            (MDA, {}),
            (OPTIC, {}),
            (CHASSIS, {}),
        ],
        ids=[
            "bay",
            "known-model",
            "no-model",
            "power-supply",
            "oob",
            "unknown-parent",
            "below-a-module",
            "port",
            "chassis",
        ],
    )
    def test_other_rows_are_not_candidates(self, index, kwargs):
        assert self._model(index, **kwargs) == ""

    def test_a_unit_below_a_built_in_unit_is_a_candidate(self):
        """A unit of another model below the built-in board sits at chassis level."""
        assert self._model(MDA, type_models={PART_NUMBER.casefold()}, entPhysicalModelName="3HE16474AA") == "3HE16474AA"


class TestMarkBuiltIn:
    """The confirm modal and the POST that records the part number and makes sure the model rule covers it."""

    def test_marking_records_the_part_number_and_creates_the_rule(self):
        from dcim.models import DeviceType

        from netbox_librenms_plugin.models import BUILT_IN_UNITS_RULE

        device = ixr_device("ixr-mark", part_number="")
        assert set(controls(device)) == {SLOT_1, SLOT_A}

        mark(device, modal_form(device, SLOT_1))

        assert DeviceType.objects.get(pk=device.device_type_id).part_number == PART_NUMBER
        (rule,) = built_in_rules()
        assert rule.manufacturer_id == device.device_type.manufacturer_id
        assert {field: getattr(rule, field) for field in BUILT_IN_UNITS_RULE} == BUILT_IN_UNITS_RULE
        rows = tab_rows(device)
        assert set(rows) == {OPTIC}
        assert (rows[OPTIC]["depth"], rows[OPTIC]["module_bay"]) == (0, "1/1/c1")
        assert controls(device) == {}

    @pytest.mark.parametrize(
        ("existing", "reused"),
        [
            ({}, True),
            ({"manufacturer": "device"}, True),
            ({"model_pattern": "^OTHER$"}, False),
            ({"enabled": False}, False),
        ],
        ids=["global", "manufacturer", "filtered", "disabled"],
    )
    def test_an_enabled_unfiltered_rule_is_reused(self, existing, reused):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        device = ixr_device(f"ixr-reuse-{'-'.join(existing) or 'global'}", part_number="")
        if existing.get("manufacturer"):
            existing = {**existing, "manufacturer": device.device_type.manufacturer}
        rule = model_rule(**existing)
        count = InventoryIgnoreRule.objects.count()

        mark(device, modal_form(device, SLOT_1))

        assert InventoryIgnoreRule.objects.count() == count + (0 if reused else 1)
        (covering,) = built_in_rules()
        assert (covering == rule) is reused
        assert set(tab_rows(device)) == {OPTIC}

    def test_a_second_device_type_reuses_the_rule_and_a_stale_form_is_refused(self):
        from dcim.models import DeviceType

        from netbox_librenms_plugin.module_grants import GrantRefusal
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts

        first = ixr_device("ixr-first", part_number="")
        second = ixr_device("ixr-second", part_number="")
        stale = modal_form(second, SLOT_A)

        mark(first, modal_form(first, SLOT_1))
        before = written(second)
        refused = mark(second, stale)

        assert message_texts(refused, "error") == [GrantRefusal.POLICY.message]
        assert written(second) == before
        mark(second, modal_form(second, SLOT_A))
        assert DeviceType.objects.get(pk=second.device_type_id).part_number == PART_NUMBER
        assert len(built_in_rules()) == 1

    def test_a_virtual_chassis_row_marks_the_device_type_of_its_member(self):
        from dcim.models import DeviceType, Manufacturer, VirtualChassis

        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

        page = ixr_device("ixr-vc-page", part_number="")
        vendor = Manufacturer.objects.create(name="IXR member vendor", slug="ixr-member-vendor")
        member = make_device_with_module_bays("ixr-vc-member", [], manufacturer=vendor)
        chassis = VirtualChassis.objects.create(name="ixr-vc", master=page)
        for position, device in ((1, page), (2, member)):
            device.virtual_chassis, device.vc_position = chassis, position
            device.save(update_fields=["virtual_chassis", "vc_position"])
        member_board = {
            **ixr_inventory()[1],
            "entPhysicalIndex": 90,
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 2,
            "entPhysicalName": "Member board",
            "entPhysicalModelName": "MEMBER-BOARD",
        }
        seed_snapshot(page, [*ixr_inventory(), member_board])
        assert tab_rows(page)[90]["selected_device_id"] == member.pk

        fields = modal_form(page, 90)
        mark(page, fields)

        assert fields["selected_device_id"] == str(member.pk)
        assert DeviceType.objects.get(pk=member.device_type_id).part_number == "MEMBER-BOARD"
        assert DeviceType.objects.get(pk=page.device_type_id).part_number == ""
        assert [rule.manufacturer_id for rule in built_in_rules()] == [vendor.pk]

    def test_a_part_number_set_elsewhere_is_never_overwritten(self):
        """A grant signed for a device type that already has a part number still writes nothing."""
        from netbox_librenms_plugin.inventory_policy import load_inventory_policy
        from netbox_librenms_plugin.module_grants import (
            ACTION_MARK_BUILT_IN,
            RENDER_ATTRIBUTED,
            module_inventory_snapshot_digest,
            sign_module_grant,
        )

        device = ixr_device("ixr-signed-part-number", part_number="3HE00000AAAA01")
        token = sign_module_grant(
            page_device_id=device.pk,
            device_id=device.pk,
            server_key=SERVER_KEY,
            action=ACTION_MARK_BUILT_IN,
            ent_index=SLOT_1,
            action_target={"device_type_id": device.device_type_id},
            inventory_digest=module_inventory_snapshot_digest(ixr_inventory()),
            policy_fingerprint=load_inventory_policy(device.pk).fingerprint,
            render_mode=RENDER_ATTRIBUTED,
        )
        fields = {"server_key": SERVER_KEY, "selected_device_id": device.pk, "ent_index": SLOT_1}
        before = written(device)

        mark(device, {**fields, "inventory_binding": token})

        assert written(device) == before

    def test_the_created_rule_is_the_rule_migration_0026_seeds(self):
        import importlib

        from netbox_librenms_plugin.models import BUILT_IN_UNITS_RULE

        seeded = importlib.import_module("netbox_librenms_plugin.migrations.0026_seed_cisco_fixed_platform_rule")

        assert seeded.FIXED_PLATFORM_RULE == BUILT_IN_UNITS_RULE


class TestMarkBuiltInWithAPartNumber:
    """A device type with another part number keeps it: the action opens a pre-filled rule form instead."""

    def test_the_link_prefills_a_narrow_rule_that_hides_the_unit(self):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        device = ixr_device("ixr-link", part_number="3HE00000AAAA01")
        before = written(device)
        tag, path, query = controls(device)[SLOT_1]

        assert (tag, path) == ("a", reverse("plugins:netbox_librenms_plugin:inventoryignorerule_add"))
        assert written(device) == before
        rule = InventoryIgnoreRule(
            name=query["name"],
            match_type=query["match_type"],
            pattern=query["pattern"],
            action=query["action"],
            model_pattern=query["model_pattern"],
            require_serial_match_parent=bool(query["require_serial_match_parent"]),
            manufacturer_id=int(query["manufacturer"]),
        )
        rule.full_clean()
        rule.save()
        rows = tab_rows(device)
        assert SLOT_1 not in rows
        # The rule names one class, so the cpmModule board keeps its own action.
        assert SLOT_A in rows

    def test_the_rule_form_opens_with_the_prefilled_values(self, client):
        from netbox_librenms_plugin.tests.conftest import make_superuser

        device = ixr_device("ixr-link-form", part_number="3HE00000AAAA01")
        _tag, path, query = controls(device)[SLOT_1]
        client.force_login(make_superuser("ixr-link-form-user"))

        response = client.get(path, query)

        assert response.status_code == 200
        form = response.context["form"]
        assert {name: form[name].value() for name in ("match_type", "pattern", "action", "model_pattern")} == {
            "match_type": "class_is",
            "pattern": "ioModule",
            "action": "transparent",
            "model_pattern": f"^{PART_NUMBER}$",
        }
        assert "checked" not in str(form["require_serial_match_parent"])
        assert str(form["manufacturer"].value()) == str(device.device_type.manufacturer_id)


class TestMarkBuiltInPermissions:
    """The writer needs change on the device type and add (or change, when it reuses one) on the rule."""

    @staticmethod
    def _perms(*names):
        from dcim.models import DeviceType

        from netbox_librenms_plugin.models import InventoryIgnoreRule

        known = {
            "change-device-type": ("change", DeviceType),
            "add-rule": ("add", InventoryIgnoreRule),
            "change-rule": ("change", InventoryIgnoreRule),
        }
        return [known[name] for name in names]

    @pytest.mark.parametrize(
        ("reuse", "perms", "allowed"),
        [
            (False, ("change-device-type", "add-rule"), True),
            (False, ("add-rule",), False),
            (False, ("change-device-type", "change-rule"), False),
            (True, ("change-device-type", "change-rule"), True),
            (True, ("change-device-type", "add-rule"), False),
        ],
        ids=["create", "create-without-device-type", "create-without-add-rule", "reuse", "reuse-without-change-rule"],
    )
    def test_each_permission_is_required(self, reuse, perms, allowed):
        from netbox_librenms_plugin.tests.conftest import make_superuser

        name = f"ixr-perm-{'reuse' if reuse else 'create'}-{'-'.join(perms)}"
        device = ixr_device(name, part_number="")
        if reuse:
            model_rule()
        user = user_with(name, *self._perms(*perms))
        fields = modal_form(device, SLOT_1, user=make_superuser(f"{name}-admin"))
        before = written(device)

        mark(device, fields, user=user)

        assert (written(device) != before) is allowed

    @pytest.mark.parametrize(
        ("reuse", "perms"),
        [
            (False, ("add-rule",)),
            (False, ("change-device-type", "change-rule")),
            (True, ("change-device-type", "add-rule")),
        ],
        ids=["no-device-type", "no-add-rule", "no-change-rule"],
    )
    def test_the_table_hides_the_action_without_the_permissions(self, reuse, perms):
        name = f"ixr-hidden-{'reuse' if reuse else 'create'}-{'-'.join(perms)}"
        device = ixr_device(name, part_number="")
        if reuse:
            model_rule()

        assert controls(device, user=user_with(name, *self._perms(*perms))) == {}
        assert set(
            controls(
                device, user=user_with(f"{name}-ok", *self._perms("change-device-type", "add-rule", "change-rule"))
            )
        ) == {SLOT_1, SLOT_A}
