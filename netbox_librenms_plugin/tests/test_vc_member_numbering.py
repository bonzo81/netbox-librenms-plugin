"""
Virtual Chassis member numbering rules through the import, its previews, its caches and module sync.

LibreNMS reports Cisco stack members from 0, and Cisco numbers its switches from 1. A rule of the
imported DeviceType decides the NetBox ``vc_position``. The LibreNMS HTTP boundary is a real
loopback server; everything else is the real ORM, views and cache.
"""

import copy

import pytest
from django.urls import reverse

from netbox_librenms_plugin.data_shapes.envelope import unwrap_response
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
from netbox_librenms_plugin.tests.conftest import _shared_infra, make_superuser, transactional_db_with_all_apps
from netbox_librenms_plugin.tests.recordings import load_recording
from netbox_librenms_plugin.tests.test_bulk_import_job_control import _libre_device

pytestmark = pytest.mark.django_db

CISCO = "cisco-stackwise-3member"
JUNIPER = "juniper-vc-2member"
# The recorded hardware and chassis model of the Cisco stack.
CISCO_MODEL = "MODEL-f93a92"


def _cisco_type(model=CISCO_MODEL, slug="cisco"):
    from dcim.models import DeviceType, Manufacturer

    manufacturer, _ = Manufacturer.objects.get_or_create(slug=slug, defaults={"name": slug.title()})
    device_type, _ = DeviceType.objects.get_or_create(
        manufacturer=manufacturer, model=model, defaults={"slug": f"{slug}-{model}".lower()}
    )
    return device_type


def _rule(mode="one_based", manufacturer=None, pattern=""):
    from netbox_librenms_plugin.models import VCMemberNumberingRule

    return VCMemberNumberingRule.objects.create(
        manufacturer=manufacturer, device_type_pattern=pattern, numbering_mode=mode
    )


def _reported_device(recording):
    device_id = recording["device_id"]
    return unwrap_response(recording["responses"][f"GET /api/v0/devices/{device_id}"])[1]["devices"][0]


def _serve(server, recording, tag, hardware):
    """Load *recording* on *server* under a device row named after *tag*; return the row."""
    site, _mfr, _dt, _role = _shared_infra()
    device_id = recording["device_id"]
    row = _libre_device(
        device_id, f"{tag}-{device_id}", hardware=hardware, serial=_reported_device(recording)["serial"]
    )
    row["location"] = site.name
    server.load_recording(recording)
    server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]}, method="GET")
    return row


def _bulk_import(rows, mappings=None):
    """Import *rows* through the shared bulk importer, as the import view and the background job do."""
    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

    site, _mfr, _dt, role = _shared_infra()
    return bulk_import_devices_shared(
        [row["device_id"] for row in rows],
        server_key="default",
        manual_mappings_per_device={
            row["device_id"]: {
                "site_id": site.pk,
                "device_role_id": role.pk,
                **(mappings or {}).get(row["device_id"], {}),
            }
            for row in rows
        },
        libre_devices_cache={row["device_id"]: row for row in rows},
        user=make_superuser("vc-numbering-importer"),
    )


def _chassis(row):
    from dcim.models import VirtualChassis

    chassis = VirtualChassis.objects.get(domain=f"librenms-default-{row['device_id']}")
    return chassis, {member.vc_position: member for member in chassis.members.all()}


def _cisco_recording(positions=None):
    """Return the Cisco recording; *positions* replaces the reported member positions in row order."""
    recording = copy.deepcopy(load_recording(CISCO))
    if positions is not None:
        for key, value in recording["responses"].items():
            if "/inventory/" in key:
                rows = [row for row in unwrap_response(value)[1]["inventory"] if row["entPhysicalClass"] == "chassis"]
                for row, position in zip(rows, positions if rows else (), strict=True):
                    row["entPhysicalParentRelPos"] = position
    return recording


class TestImport:
    """A1, A2, A4: the import writes the numbers that the DeviceType's rule gives."""

    def test_a_cisco_stack_reported_from_zero_imports_its_members_from_one(self, live_librenms):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        recording = _cisco_recording()
        row = _serve(live_librenms.server, recording, "cisco-one-based", CISCO_MODEL)

        result = _bulk_import([row])

        assert result["failed"] == []
        assert result["virtual_chassis_created"] == 1
        chassis, members = _chassis(row)
        assert sorted(members) == [1, 2, 3]
        assert [members[position].serial for position in (1, 2, 3)] == recording["expected"]["virtual_chassis"][
            "member_serials"
        ]
        assert {position: member.name for position, member in members.items()} == {
            position: f"{row['hostname']}-M{position}" for position in (1, 2, 3)
        }
        assert chassis.master.vc_position == 1
        # A4: the first number of a Cisco port name is the switch that owns the port.
        assert get_virtual_chassis_member(chassis.master, "GigabitEthernet1/0/1") == members[1]
        assert get_virtual_chassis_member(members[1], "GigabitEthernet3/0/1") == members[3]

    def test_an_interface_sync_writes_a_cisco_port_to_the_switch_its_name_names(self, client, live_librenms):
        """A4 through the writer: Gi2/0/1 goes to the member at 2, and Gi1/0/1 to the member at 1, not to 0's."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.interface_sync_post_helpers import post_interface_sync, seed_ports, sync_port

        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        recording = _cisco_recording()
        row = _serve(live_librenms.server, recording, "cisco-ports", CISCO_MODEL)
        _bulk_import([row])
        chassis, _members = _chassis(row)
        # LibreNMS reports switch 1 at 0 and switch 2 at 1.
        switch_1, switch_2 = recording["expected"]["virtual_chassis"]["member_serials"][:2]
        seed_ports(chassis.master, [sync_port(9101, "GigabitEthernet1/0/1"), sync_port(9102, "GigabitEthernet2/0/1")])
        client.force_login(make_superuser("vc-numbering-ports"))

        response = post_interface_sync(client, chassis.master, [9101, 9102], htmx=False)

        assert response.status_code == 302
        assert {
            interface.name: interface.device.serial for interface in Interface.objects.filter(name__endswith="/0/1")
        } == {"GigabitEthernet1/0/1": switch_1, "GigabitEthernet2/0/1": switch_2}

    def test_the_single_row_import_button_numbers_the_stack_from_one(self, client, live_librenms):
        """The row button posts one selected row to the bulk import view, which runs the shared importer."""
        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-button", CISCO_MODEL)
        _site, _mfr, _dt, role = _shared_infra()
        client.force_login(make_superuser("vc-numbering-button"))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {"select": [str(row["device_id"])], "server_key": "default", f"role_{row['device_id']}": str(role.pk)},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        _chassis_row, members = _chassis(row)
        assert sorted(members) == [1, 2, 3]

    def test_a_batch_numbers_each_stack_by_its_own_device_type(self, live_librenms):
        """A2: the Cisco rule leaves a Juniper Virtual Chassis at its reported 0 and 1."""
        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        _site, _mfr, generic_type, _role = _shared_infra()
        cisco_row = _serve(live_librenms.server, _cisco_recording(), "batch-cisco", CISCO_MODEL)
        juniper_row = _serve(live_librenms.server, load_recording(JUNIPER), "batch-juniper", generic_type.model)

        result = _bulk_import([cisco_row, juniper_row])

        assert result["failed"] == []
        assert result["virtual_chassis_created"] == 2
        assert sorted(_chassis(cisco_row)[1]) == [1, 2, 3]
        assert sorted(_chassis(juniper_row)[1]) == [0, 1]

    def test_without_a_rule_a_stack_keeps_its_reported_numbers(self, live_librenms):
        _cisco_type()
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-no-rule", CISCO_MODEL)

        _bulk_import([row])

        assert sorted(_chassis(row)[1]) == [0, 1, 2]

    @pytest.mark.parametrize(
        ("reported", "stored"), [((1, 2, 3), [1, 2, 3]), ((0, 2, 4), [1, 3, 5]), ((2, 4, 6), [2, 4, 6])]
    )
    def test_number_from_1_shifts_only_a_stack_numbered_from_0(self, live_librenms, reported, stored):
        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        row = _serve(live_librenms.server, _cisco_recording(reported), f"cisco-{reported[0]}", CISCO_MODEL)

        _bulk_import([row])

        assert sorted(_chassis(row)[1]) == stored

    @pytest.mark.parametrize(
        ("matched_mode", "override_mode", "stored"),
        [("one_based", "reported", [0, 1, 2]), ("reported", "one_based", [1, 2, 3])],
    )
    def test_a_device_type_override_numbers_the_stack_by_the_override_rule(
        self, live_librenms, matched_mode, override_mode, stored
    ):
        """The validation numbered the stack for the matched type; the import numbers it again, once."""
        matched = _cisco_type()
        override = _cisco_type(model="C9300-48P")
        _rule(matched_mode, matched.manufacturer, pattern=CISCO_MODEL)
        _rule(override_mode, override.manufacturer, pattern="C9300-48P")
        row = _serve(live_librenms.server, _cisco_recording(), f"override-{override_mode}", CISCO_MODEL)

        result = _bulk_import([row], {row["device_id"]: {"device_type_id": override.pk}})

        assert result["failed"] == []
        chassis, members = _chassis(row)
        assert sorted(members) == stored
        assert {member.device_type_id for member in members.values()} == {override.pk}

    def test_a_number_outside_the_vc_position_range_imports_nothing(self, live_librenms):
        from dcim.models import Device

        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        top = Device._meta.get_field("vc_position").validators[-1].limit_value
        row = _serve(live_librenms.server, _cisco_recording((0, 1, top)), "cisco-overflow", CISCO_MODEL)

        result = _bulk_import([row])

        assert [failure["device_id"] for failure in result["failed"]] == [row["device_id"]]
        assert not Device.objects.filter(name__startswith="cisco-overflow").exists()

    def test_a_broken_stored_rule_imports_nothing_and_never_falls_back_to_a_broader_rule(self, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.models import VCMemberNumberingRule

        cisco_type = _cisco_type()
        _rule("reported", cisco_type.manufacturer)
        VCMemberNumberingRule.objects.bulk_create(
            [
                VCMemberNumberingRule(
                    manufacturer=cisco_type.manufacturer, device_type_pattern="MODEL-(", numbering_mode="one_based"
                )
            ]
        )
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-broken-rule", CISCO_MODEL)

        result = _bulk_import([row])

        assert [failure["device_id"] for failure in result["failed"]] == [row["device_id"]]
        assert not Device.objects.filter(name__startswith="cisco-broken-rule").exists()


class TestProjection:
    """The one projection reads only the reported numbers, so a second projection changes nothing."""

    def test_projecting_a_projected_stack_again_gives_the_same_numbers(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import project_vc_members

        members = [
            {"inventory_position": position, "position": position, "serial": f"S{position}"} for position in (0, 1, 2)
        ]

        once = project_vc_members(members, "one_based")
        twice = project_vc_members(once, "one_based")

        assert [member["position"] for member in once] == [1, 2, 3]
        assert twice == once
        assert [member["position"] for member in members] == [0, 1, 2]

    @pytest.mark.parametrize(
        "positions",
        [(0, 0), (0, None), (-1, 0), (True, 1), ("1", 2)],
        ids=["shared", "missing", "negative", "bool", "text"],
    )
    def test_a_malformed_source_vector_is_refused(self, positions):
        from netbox_librenms_plugin.import_utils.virtual_chassis import VCNumberingError, project_vc_members

        with pytest.raises(VCNumberingError):
            project_vc_members([{"inventory_position": position} for position in positions], "reported")

    def test_a_stored_mode_that_is_no_choice_is_refused(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import VCNumberingError, project_vc_members

        with pytest.raises(VCNumberingError):
            project_vc_members([{"inventory_position": 0}, {"inventory_position": 1}], "zero_based")


def _validation(api, row, **options):
    from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

    return validate_device_for_import(row, api=api, server_key=api.server_key, include_vc_detection=True, **options)


class TestPreviews:
    """A3: every preview shows the numbers and names that the import writes."""

    def test_the_table_the_details_dialog_and_the_confirmation_show_what_the_import_writes(self, client, live_librenms):
        import json
        from html import unescape

        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-preview", CISCO_MODEL)
        _site, _mfr, _dt, role = _shared_infra()
        client.force_login(make_superuser("vc-numbering-preview"))

        validation = _validation(live_librenms.api, row)
        table_payload = DeviceImportTable._build_vc_attributes(validation, row)
        table = json.loads(unescape(table_payload.split('data-vc-info="')[1].split('"')[0]))
        details = client.get(
            reverse("plugins:netbox_librenms_plugin:device_vc_details", args=[row["device_id"]]),
            {"server_key": "default"},
        ).context["vc_data"]
        confirm = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {"select": [str(row["device_id"])], "server_key": "default", f"role_{row['device_id']}": str(role.pk)},
        ).context["devices"][0]["validation"]["virtual_chassis"]
        _bulk_import([row])

        _chassis_row, members = _chassis(row)
        written = sorted((position, member.name) for position, member in members.items())
        assert sorted((m["position"], m["suggested_name"]) for m in table["members"]) == written
        assert sorted((m["position"], m["suggested_name"]) for m in details["members"]) == written
        assert sorted((m["position"], m["suggested_name"]) for m in confirm["members"]) == written
        assert [m["position"] for m in details["members"] if m["is_master"]] == [1]

    def test_every_preview_names_members_with_the_naming_of_the_row(self, client, live_librenms):
        """Hostname without its domain, not the sysName: the table, its details link, the confirmation and the import agree."""
        import json
        from html import unescape

        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        cisco_type = _cisco_type()
        _rule(manufacturer=cisco_type.manufacturer)
        row = _serve(live_librenms.server, _cisco_recording(), "naming", CISCO_MODEL)
        row.update(hostname="naming-stack.example.com", sysName="sysname-stack.example.com")
        live_librenms.server.register(
            f"/api/v0/devices/{row['device_id']}", {"status": "ok", "devices": [row]}, method="GET"
        )
        naming = {"use_sysname": False, "strip_domain": True}
        _site, _mfr, _dt, role = _shared_infra()
        user = make_superuser("vc-numbering-naming")
        client.force_login(user)

        validation = _validation(live_librenms.api, row, **naming)
        record = {**row, "_validation": validation}
        table = json.loads(
            unescape(
                DeviceImportTable._build_vc_attributes(validation, record).split('data-vc-info="')[1].split('"')[0]
            )
        )
        details_url = unescape(
            DeviceImportTable([record], server_key="default", user=user)
            .render_virtual_chassis(None, record)
            .split('hx-get="')[1]
            .split('"')[0]
        )
        details = client.get(details_url).context["vc_data"]
        confirm = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {
                "select": [str(row["device_id"])],
                "server_key": "default",
                f"role_{row['device_id']}": str(role.pk),
                "use_sysname": "false",
                "strip_domain": "true",
            },
        ).context["devices"][0]["validation"]["virtual_chassis"]
        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

        bulk_import_devices_shared(
            [row["device_id"]],
            server_key="default",
            sync_options=naming,
            manual_mappings_per_device={row["device_id"]: {"site_id": _site.pk, "device_role_id": role.pk}},
            libre_devices_cache={row["device_id"]: row},
            user=user,
        )

        _chassis_row, members = _chassis(row)
        written = sorted((position, member.name) for position, member in members.items())
        assert written == [(position, f"naming-stack-M{position}") for position in (1, 2, 3)]
        for preview in (table, details, confirm):
            assert sorted((m["position"], m["suggested_name"]) for m in preview["members"]) == written

    def test_without_a_device_type_the_numbers_are_unavailable(self, live_librenms):
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-no-type", "UNMATCHED-HARDWARE")

        vc = _validation(live_librenms.api, row)["virtual_chassis"]

        assert vc["is_stack"] is True
        assert vc["numbering_available"] is False
        assert {member["position"] for member in vc["members"]} == {None}

    def test_a_numbering_error_blocks_the_row_and_names_no_rule(self, client, live_librenms):
        """The validation is cached and shown to any import user, so its refusal names no rule, field or value."""
        from netbox_librenms_plugin.models import VCMemberNumberingRule

        cisco_type = _cisco_type()
        (broken,) = VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=cisco_type.manufacturer, device_type_pattern="(SECRET-MARKER")]
        )
        row = _serve(live_librenms.server, _cisco_recording(), "cisco-preview-error", CISCO_MODEL)
        client.force_login(make_superuser("vc-numbering-preview-error"))

        validation = _validation(live_librenms.api, row)
        details = client.get(
            reverse("plugins:netbox_librenms_plugin:device_vc_details", args=[row["device_id"]]),
            {"server_key": "default"},
        )

        assert validation["can_import"] is False
        assert validation["virtual_chassis"]["numbering_error"] in validation["issues"]
        assert {member["position"] for member in validation["virtual_chassis"]["members"]} == {None}
        assert details.context["vc_data"]["numbering_error"]
        for text in (validation["virtual_chassis"]["numbering_error"], details.content.decode()):
            assert "SECRET-MARKER" not in text
            assert broken.get_absolute_url() not in text
            assert "device_type_pattern" not in text


class TestConfirmation:
    """The confirmation shows a numbering-blocked row and does not submit it."""

    def _confirm(self, client, rows):
        _site, _mfr, _dt, role = _shared_infra()
        client.force_login(make_superuser("vc-numbering-confirm"))
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {
                "select": [str(row["device_id"]) for row in rows],
                "server_key": "default",
                **{f"role_{row['device_id']}": str(role.pk) for row in rows},
            },
        )
        assert response.status_code == 200
        return response.content.decode()

    def _broken_cisco_rule(self):
        from netbox_librenms_plugin.models import VCMemberNumberingRule

        VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=_cisco_type().manufacturer, device_type_pattern="(")]
        )

    def test_a_blocked_row_is_shown_and_not_submitted(self, client, live_librenms):
        self._broken_cisco_rule()
        _site, _mfr, generic_type, _role = _shared_infra()
        blocked = _serve(live_librenms.server, _cisco_recording(), "confirm-blocked", CISCO_MODEL)
        ready = _serve(live_librenms.server, load_recording(JUNIPER), "confirm-ready", generic_type.model)

        body = self._confirm(client, [blocked, ready])

        assert f'name="select" value="{ready["device_id"]}"' in body
        assert f'name="select" value="{blocked["device_id"]}"' not in body
        assert blocked["hostname"] in body
        assert "data-bulk-confirm-submit disabled" not in body

    def test_a_confirmation_of_only_blocked_rows_cannot_be_submitted(self, client, live_librenms):
        self._broken_cisco_rule()
        blocked = _serve(live_librenms.server, _cisco_recording(), "confirm-only-blocked", CISCO_MODEL)

        body = self._confirm(client, [blocked])

        assert 'name="select"' not in body
        assert "data-bulk-confirm-submit disabled" in body


@pytest.fixture
def caching_librenms(configure_librenms, monkeypatch):
    """A loopback LibreNMS whose import searches the plugin caches."""
    from types import SimpleNamespace

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = MockLibreNMSServer().start()
    configure_librenms(
        {"default": {"librenms_url": server.url, "api_token": "test-token", "cache_timeout": 300, "verify_ssl": False}}
    )
    try:
        yield SimpleNamespace(server=server, api=LibreNMSAPI(server_key="default"))
    finally:
        server.stop()


def _search(librenms, row):
    """Run the import search for *row* through the shared validation and its cache; return the row's validation."""
    from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

    librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]}, method="GET")
    (found,) = process_device_filters(
        librenms.api, {"hostname": row["hostname"]}, vc_detection_enabled=True, clear_cache=False, show_disabled=True
    )
    return found["_validation"]


def _positions(validation):
    return [member["position"] for member in validation["virtual_chassis"]["members"]]


def _completed_job(librenms, row, user):
    from uuid import uuid4

    from core.models import Job

    return Job.objects.create(
        name="Completed import search",
        user=user,
        job_id=uuid4(),
        status="completed",
        data={
            "device_ids": [row["device_id"]],
            "filters": {"hostname": row["hostname"]},
            "server_key": librenms.api.server_key,
            "vc_detection_enabled": True,
        },
    )


def _job_rows(job, user):
    from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

    return LibreNMSImportView()._load_job_results(job.pk, user)


class TestCachedValidations:
    """A3 with warm caches: a configuration edit makes every cached validation miss."""

    def _edits(self):
        """Return ``{name: (setup, edit)}``: each edit changes the rule that numbers the cached Cisco row."""
        from dcim.models import DeviceType

        from netbox_librenms_plugin.models import DeviceTypeMapping

        def rule_deleted():
            rule = _rule(manufacturer=_cisco_type().manufacturer)
            return rule.delete

        def rule_edited():
            rule = _rule(manufacturer=_cisco_type().manufacturer)

            def edit():
                rule.numbering_mode = "reported"
                rule.save()

            return edit

        def mapping_remapped():
            reported = _cisco_type(model="C9300-REPORTED")
            numbered = _cisco_type(model="C9300-NUMBERED")
            _rule("one_based", numbered.manufacturer, pattern="C9300-NUMBERED")
            _rule("reported", reported.manufacturer, pattern="C9300-REPORTED")
            mapping = DeviceTypeMapping.objects.create(librenms_hardware="MAPPED-HARDWARE", netbox_device_type=numbered)

            def edit():
                mapping.netbox_device_type = reported
                mapping.save()

            return edit

        def device_type_renamed():
            cisco_type = _cisco_type()
            _rule("one_based", cisco_type.manufacturer, pattern=CISCO_MODEL)
            _rule("reported", cisco_type.manufacturer, pattern="RENAMED")

            def edit():
                renamed = DeviceType.objects.get(pk=cisco_type.pk)
                renamed.model = "RENAMED"
                renamed.part_number = CISCO_MODEL
                renamed.save()

            return edit

        return {
            "rule-deleted": (rule_deleted, CISCO_MODEL),
            "rule-edited": (rule_edited, CISCO_MODEL),
            "mapping-remapped": (mapping_remapped, "MAPPED-HARDWARE"),
            "device-type-renamed": (device_type_renamed, CISCO_MODEL),
        }

    @pytest.mark.parametrize("change", ["rule-deleted", "rule-edited", "mapping-remapped", "device-type-renamed"])
    def test_a_cached_search_shows_the_numbers_of_the_current_configuration(
        self, caching_librenms, django_capture_on_commit_callbacks, change
    ):
        setup, hardware = self._edits()[change]
        edit = setup()
        row = _serve(caching_librenms.server, _cisco_recording(), f"cached-{change}", hardware)
        assert _positions(_search(caching_librenms, row)) == [1, 2, 3]

        with django_capture_on_commit_callbacks(execute=True):
            edit()

        assert _positions(_search(caching_librenms, row)) == [0, 1, 2]

    @pytest.mark.parametrize("change", ["rule-deleted", "mapping-remapped", "device-type-renamed"])
    def test_a_completed_job_expires_after_a_configuration_edit(
        self, caching_librenms, django_capture_on_commit_callbacks, change
    ):
        setup, hardware = self._edits()[change]
        edit = setup()
        user = make_superuser(f"vc-numbering-job-{change}")
        row = _serve(caching_librenms.server, _cisco_recording(), f"job-{change}", hardware)
        _search(caching_librenms, row)
        job = _completed_job(caching_librenms, row, user)
        (cached,) = _job_rows(job, user)
        assert _positions(cached["_validation"]) == [1, 2, 3]

        with django_capture_on_commit_callbacks(execute=True):
            edit()

        assert _job_rows(job, user) == []

    def test_a_repaired_rule_makes_a_blocked_row_importable_again(
        self, caching_librenms, django_capture_on_commit_callbacks
    ):
        from netbox_librenms_plugin.models import VCMemberNumberingRule

        cisco_type = _cisco_type()
        (broken,) = VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=cisco_type.manufacturer, device_type_pattern="(")]
        )
        row = _serve(caching_librenms.server, _cisco_recording(), "cached-repair", CISCO_MODEL)
        blocked = _search(caching_librenms, row)
        assert blocked["virtual_chassis"]["numbering_error"] in blocked["issues"]

        with django_capture_on_commit_callbacks(execute=True):
            broken.device_type_pattern = CISCO_MODEL
            broken.numbering_mode = "one_based"
            broken.save()

        repaired = _search(caching_librenms, row)
        assert repaired["virtual_chassis"].get("numbering_error") is None
        assert not any(issue == blocked["virtual_chassis"]["numbering_error"] for issue in repaired["issues"])
        assert _positions(repaired) == [1, 2, 3]


class TestConfigurationGeneration:
    """The import configuration generation moves after a commit of every input that selects a DeviceType or a rule."""

    def _edits(self):
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        def numbering_rule():
            return _rule("reported", _cisco_type().manufacturer)

        def mapping():
            return DeviceTypeMapping.objects.create(librenms_hardware="GEN-HW", netbox_device_type=_cisco_type())

        def normalization_rule():
            return NormalizationRule.objects.create(scope="device_type", match_pattern="^GEN-(.*)$", replacement=r"\1")

        def device_type():
            return DeviceType.objects.create(manufacturer=_cisco_type().manufacturer, model="GEN-DT", slug="gen-dt")

        def manufacturer():
            return Manufacturer.objects.create(name="Gen Vendor", slug="gen-vendor")

        return {
            "VCMemberNumberingRule": numbering_rule,
            "DeviceTypeMapping": mapping,
            "NormalizationRule": normalization_rule,
            "DeviceType": device_type,
            "Manufacturer": manufacturer,
        }

    @pytest.mark.parametrize(
        "model", ["VCMemberNumberingRule", "DeviceTypeMapping", "NormalizationRule", "DeviceType", "Manufacturer"]
    )
    def test_a_save_and_a_delete_each_move_the_generation_after_the_commit(
        self, django_capture_on_commit_callbacks, model
    ):
        from netbox_librenms_plugin.import_utils.cache import import_configuration_generation

        before = import_configuration_generation()
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            created = self._edits()[model]()
        assert import_configuration_generation() == before
        for callback in callbacks:
            callback()
        saved = import_configuration_generation()
        with django_capture_on_commit_callbacks(execute=True):
            created.delete()

        assert before < saved < import_configuration_generation()

    def test_a_rolled_back_edit_keeps_the_generation(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.import_utils.cache import import_configuration_generation

        before = import_configuration_generation()
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError), transaction.atomic():
                _rule("reported", _cisco_type().manufacturer)
                raise RuntimeError("roll back")

        assert import_configuration_generation() == before

    def test_an_expired_generation_never_comes_back(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils.cache import (
            IMPORT_CONFIGURATION_GENERATION_KEY,
            import_configuration_generation,
        )

        first = import_configuration_generation()
        cache.delete(IMPORT_CONFIGURATION_GENERATION_KEY)

        assert import_configuration_generation() > first

    def test_a_recreated_counter_misses_every_validation_that_survived_it(self, caching_librenms):
        """The counter can expire while cached validations survive; the new counter must not match them."""
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils.cache import IMPORT_CONFIGURATION_GENERATION_KEY
        from netbox_librenms_plugin.models import VCMemberNumberingRule

        rule = _rule(manufacturer=_cisco_type().manufacturer)
        row = _serve(caching_librenms.server, _cisco_recording(), "recreated-counter", CISCO_MODEL)
        assert _positions(_search(caching_librenms, row)) == [1, 2, 3]

        cache.delete(IMPORT_CONFIGURATION_GENERATION_KEY)
        # A queryset update sends no signal, so only the recreated counter can make the old result miss.
        VCMemberNumberingRule.objects.filter(pk=rule.pk).update(numbering_mode="reported")

        assert _positions(_search(caching_librenms, row)) == [0, 1, 2]

    def test_a_validation_that_a_configuration_edit_overtakes_stays_under_the_old_generation(self, caching_librenms):
        """A commit while the search validates moves the generation; the late result is unreachable."""
        from netbox_librenms_plugin.import_utils.cache import (
            _bump_import_configuration_generation,
            get_validated_device_cache_key,
        )

        from django.core.cache import cache

        # Unmatched hardware makes the validation read the chassis model, a read that only the validation makes.
        row = _serve(caching_librenms.server, _cisco_recording(), "overtaken", "UNMATCHED-HARDWARE")
        device_id = row["device_id"]
        recorded = caching_librenms.server.routes[f"GET /api/v0/inventory/{device_id}"]

        def inventory_while_a_commit_lands(**request):
            if "entPhysicalClass" in request["query"]:
                _bump_import_configuration_generation()
            return recorded(**request)

        caching_librenms.server.register(f"/api/v0/inventory/{device_id}", inventory_while_a_commit_lands, method="GET")
        before = cache.get(
            get_validated_device_cache_key(
                server_key="default", filters={"hostname": row["hostname"]}, device_id=device_id, vc_enabled=True
            )
        )

        _search(caching_librenms, row)

        key = get_validated_device_cache_key(
            server_key="default", filters={"hostname": row["hostname"]}, device_id=device_id, vc_enabled=True
        )
        assert before is None
        assert cache.get(key) is None


def _module_refresh(device):
    """POST a Modules refresh for *device* through the real view; return the rendered module context."""
    from netbox_librenms_plugin.tests.test_inventory_class_admission import _refresh

    _response, module_sync = _refresh(device, "default")
    return module_sync


def _module_render(device):
    """Render the Modules tab of *device* from its cached snapshot, as a page load does."""
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    view = DeviceModuleTableView()
    request = make_request("get", {"server_key": "default"}, user=make_superuser("vc-numbering-render"))
    view.setup(request)
    return view.get_context_data(request, device, server_key="default")


def _inventory_rows(recording, *, drop_serial_of=None, extra=()):
    """Return the recorded ``/all`` rows, without the serial of the member row *drop_serial_of*, plus *extra*."""
    device_id = recording["device_id"]
    rows = copy.deepcopy(
        unwrap_response(recording["responses"][f"GET /api/v0/inventory/{device_id}/all"])[1]["inventory"]
    )
    for row in rows:
        if row["entPhysicalIndex"] == drop_serial_of:
            row["entPhysicalSerialNum"] = ""
    return [*rows, *extra]


def _board(index, parent):
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "module",
        "entPhysicalName": f"Uplink module {index}",
        "entPhysicalDescr": "Uplink module",
        "entPhysicalModelName": "NM-UPLINK",
        "entPhysicalSerialNum": f"NM-SN-{index}",
        "entPhysicalContainedIn": parent,
        "entPhysicalParentRelPos": 1,
    }


class TestModuleAttribution:
    """A5: module sync gives a row the member that import numbered, and refuses when the numbers disagree."""

    def _imported(self, live_librenms, tag):
        cisco_type = _cisco_type()
        rule = _rule(manufacturer=cisco_type.manufacturer)
        recording = _cisco_recording()
        row = _serve(live_librenms.server, recording, tag, CISCO_MODEL)
        assert _bulk_import([row])["virtual_chassis_created"] == 1
        _chassis_row, members = _chassis(row)
        return recording, rule, members

    def _serve_inventory(self, live_librenms, recording, rows):
        device_id = recording["device_id"]
        live_librenms.server.register(
            f"/api/v0/inventory/{device_id}/all", {"status": "ok", "inventory": rows}, method="GET"
        )
        live_librenms.server.register(
            f"/api/v0/devices/{device_id}/transceivers", {"status": "ok", "transceivers": []}, method="GET"
        )

    def test_a_row_under_the_member_reported_at_0_goes_to_the_member_at_1(self, live_librenms):
        """Member 0's row has no serial, so only its projected position can name its NetBox member."""
        recording, _rule_row, members = self._imported(live_librenms, "attr-cisco")
        rows = _inventory_rows(recording, drop_serial_of=100, extra=[_board(101, 100), _board(301, 300)])
        self._serve_inventory(live_librenms, recording, rows)

        module_sync = _module_refresh(members[2])

        assert module_sync.get("vc_numbering_error") is None
        owners = {row["ent_physical_index"]: row["selected_device_id"] for row in module_sync["table"].data}
        assert owners[101] == members[1].pk
        assert owners[301] == members[3].pk

    @pytest.mark.parametrize("change", ["rule-deleted", "rule-reported"])
    def test_a_rule_change_after_the_import_refuses_the_module_table(self, live_librenms, change):
        """The members stay at 1, 2, 3 and the rule now projects 0, 1, 2: no row gets a member."""
        recording, rule, members = self._imported(live_librenms, f"attr-{change}")
        if change == "rule-deleted":
            rule.delete()
        else:
            rule.numbering_mode = "reported"
            rule.save()
        self._serve_inventory(live_librenms, recording, _inventory_rows(recording, extra=[_board(101, 100)]))

        module_sync = _module_refresh(members[1])

        assert module_sync["table"] is None
        assert module_sync["vc_numbering_error"]
        # The read succeeded, so the tab keeps the refusal instead of a "refresh failed" placeholder.
        status = SyncCacheConsistency(members[1]).status("default")[SyncTab.MODULES.value]
        assert (status["state"], status["snapshot_available"]) == ("ready", True)
        assert _module_render(members[1])["vc_numbering_error"] == module_sync["vc_numbering_error"]

    def test_a_member_row_whose_serial_names_another_member_refuses_the_module_table(self, live_librenms):
        from dcim.models import Device

        recording, _rule_row, members = self._imported(live_librenms, "attr-swapped")
        Device.objects.filter(pk=members[1].pk).update(serial="SWAP-TMP")
        Device.objects.filter(pk=members[2].pk).update(serial=members[1].serial)
        Device.objects.filter(pk=members[1].pk).update(serial=members[2].serial)
        self._serve_inventory(live_librenms, recording, _inventory_rows(recording))

        module_sync = _module_refresh(members[1])

        assert module_sync["table"] is None
        assert module_sync["vc_numbering_error"]

    def test_a_partial_inventory_after_a_rule_deletion_refuses_the_module_table(self, live_librenms):
        """One serial-less member row is no stack; it must not land on a member by its raw number."""
        recording, rule, members = self._imported(live_librenms, "attr-partial")
        rule.delete()
        rows = [
            row
            for row in _inventory_rows(recording, drop_serial_of=200, extra=[_board(201, 200)])
            if row["entPhysicalIndex"] in (1, 200, 201)
        ]
        self._serve_inventory(live_librenms, recording, rows)

        module_sync = _module_refresh(members[1])

        assert module_sync["table"] is None
        assert module_sync["vc_numbering_error"]

    @pytest.mark.parametrize("viewer", ["rule-viewer", "restricted"])
    def test_a_broken_numbering_rule_is_named_only_to_a_viewer_of_the_rule(self, live_librenms, viewer):
        from dcim.models import Device, Module, ModuleBay, ModuleType
        from django.test.signals import template_rendered

        from netbox_librenms_plugin.models import VCMemberNumberingRule
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        recording, _rule_row, members = self._imported(live_librenms, f"attr-broken-{viewer}")
        manufacturer = members[1].device_type.manufacturer
        (broken,) = VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=manufacturer, device_type_pattern="(SECRET-MARKER")]
        )
        self._serve_inventory(live_librenms, recording, _inventory_rows(recording))
        perms = [("view", Device), ("view", Module), ("view", ModuleBay), ("view", ModuleType)]
        if viewer == "rule-viewer":
            perms.append(("view", VCMemberNumberingRule))
        user = make_user_with_perms(f"vc-numbering-{viewer}", perms)
        view = DeviceModuleTableView()
        contexts = []

        def capture(sender, template, context, **kwargs):
            if template.name == view.partial_template_name:
                contexts.append(context["module_sync"])

        template_rendered.connect(capture)
        try:
            response = post(view, make_request("post", {"server_key": "default"}, user=user), pk=members[1].pk)
        finally:
            template_rendered.disconnect(capture)

        (module_sync,) = contexts
        content = response.content.decode()
        assert module_sync["table"] is None
        assert module_sync["inventory_rule_error"].visible is (viewer == "rule-viewer")
        # The read was complete, so the tab keeps the rule error instead of a "refresh failed" placeholder.
        status = SyncCacheConsistency(members[1]).status("default")[SyncTab.MODULES.value]
        assert (status["state"], status["snapshot_available"]) == ("ready", True)
        assert _module_render(members[1])["inventory_rule_error"].rule_pk == broken.pk
        assert ("SECRET-MARKER" in content) is (viewer == "rule-viewer")
        assert (broken.get_absolute_url() in content) is (viewer == "rule-viewer")

    def test_a_device_outside_a_virtual_chassis_keeps_every_row(self, live_librenms):
        """Without members there is no numbering to check: the rows stay on the page device."""
        from netbox_librenms_plugin.tests.conftest import make_device, map_device_to_librenms

        recording = _cisco_recording()
        device = make_device("attr-standalone")
        map_device_to_librenms(device, recording["device_id"], server_key="default")
        _serve(live_librenms.server, recording, "attr-standalone", CISCO_MODEL)
        self._serve_inventory(live_librenms, recording, _inventory_rows(recording, extra=[_board(101, 100)]))

        module_sync = _module_refresh(device)

        assert module_sync.get("vc_numbering_error") is None
        assert {row["selected_device_id"] for row in module_sync["table"].data} == {device.pk}


def test_the_recording_signature_keeps_the_reported_numbers_under_a_rule():
    """The signature describes the LibreNMS data, so a numbering rule does not change its position base."""
    from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature

    _rule(manufacturer=_cisco_type().manufacturer)

    assert compute_shape_signature(load_recording(CISCO))["virtual_chassis"]["position_base"] == 0


def test_a_configuration_edit_between_two_rows_of_one_search_leaves_the_later_row_under_the_old_generation(
    caching_librenms,
):
    """The batch read its rules once, so a row it validates after an edit is no result of the current configuration."""
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
    from netbox_librenms_plugin.import_utils.cache import (
        _bump_import_configuration_generation,
        get_validated_device_cache_key,
    )

    server = caching_librenms.server
    first = _serve(server, _cisco_recording(), "batch-edit", "UNMATCHED-HARDWARE")
    second = {**first, "device_id": first["device_id"] + 1, "hostname": "batch-edit-second"}
    server.register(f"/api/v0/devices/{second['device_id']}", {"status": "ok", "devices": [second]}, method="GET")
    recorded = server.routes[f"GET /api/v0/inventory/{first['device_id']}"]

    def first_row_inventory_while_a_commit_lands(**request):
        # Unmatched hardware makes only the validation read the chassis model.
        if "entPhysicalClass" in request["query"]:
            _bump_import_configuration_generation()
        return recorded(**request)

    server.register(f"/api/v0/inventory/{first['device_id']}", first_row_inventory_while_a_commit_lands, method="GET")
    server.register("/api/v0/devices", {"status": "ok", "devices": [first, second]}, method="GET")
    filters = {"location": first["location"]}

    process_device_filters(
        caching_librenms.api, filters, vc_detection_enabled=False, clear_cache=False, show_disabled=True
    )

    key = get_validated_device_cache_key(
        server_key="default", filters=filters, device_id=second["device_id"], vc_enabled=False
    )
    assert cache.get(key) is None


@transactional_db_with_all_apps()
def test_concurrent_commits_each_move_the_generation_and_a_concurrent_start_reads_one_counter():
    """Workers with their own database connections commit edits at once: no bump is lost, no start is split."""
    from threading import Barrier

    from django.core.cache import cache
    from django.db import connection, transaction

    from netbox_librenms_plugin.import_utils.cache import (
        IMPORT_CONFIGURATION_GENERATION_KEY,
        import_configuration_generation,
    )
    from netbox_librenms_plugin.models import VCMemberNumberingRule
    from netbox_librenms_plugin.tests.conftest import bounded_executor

    workers = 4
    cache.delete(IMPORT_CONFIGURATION_GENERATION_KEY)
    start = Barrier(workers)

    def first_read():
        start.wait(timeout=10)
        try:
            return import_configuration_generation()
        finally:
            connection.close()

    with bounded_executor(max_workers=workers) as executor:
        firsts = [future.result(timeout=30) for future in [executor.submit(first_read) for _ in range(workers)]]
    assert len(set(firsts)) == 1

    commit = Barrier(workers)

    def commit_an_edit(index):
        commit.wait(timeout=10)
        try:
            with transaction.atomic():
                VCMemberNumberingRule.objects.create(
                    device_type_pattern=f"CONCURRENT-{index}", numbering_mode="reported"
                )
        finally:
            connection.close()

    with bounded_executor(max_workers=workers) as executor:
        for future in [executor.submit(commit_an_edit, index) for index in range(workers)]:
            future.result(timeout=30)

    assert import_configuration_generation() == firsts[0] + workers
