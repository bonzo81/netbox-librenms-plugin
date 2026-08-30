"""Integration coverage for LibreNMS virtual-machine imports."""

from copy import deepcopy
from decimal import Decimal

import pytest

from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_vm
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, missing_pk


SERVER_KEY = "default"


def _configure_server(settings, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "VM import test server",
            "librenms_url": server.url,
            "api_token": "test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_api(settings):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    with librenms_mock_server() as server:
        _configure_server(settings, server)
        yield LibreNMSAPI(SERVER_KEY), server


def _payload(device_id, hostname=None, **overrides):
    name = hostname or f"vm-{device_id}.example.test"
    payload = {
        "device_id": device_id,
        "hostname": name,
        "sysName": name,
        "hardware": "Virtual machine",
        "serial": "-",
        "os": "linux",
        "ip": f"198.18.30.{int(device_id) % 250 + 1}",
        "version": "1.0",
        "location": "-",
    }
    payload.update(overrides)
    return payload


def _validation(tag, *, platform=None, role=None, resolved_name=None):
    return {
        "can_import": True,
        "issues": [],
        "cluster": {"cluster": make_cluster(f"{tag}-cluster")},
        "platform": {"platform": platform},
        "device_role": {"role": role},
        "resolved_name": resolved_name,
    }


def _vm_writer(tag):
    from virtualization.models import VirtualMachine

    return make_user_with_perms(
        f"vm-import-writer-{tag}",
        [("add", VirtualMachine)],
        plugin_write=False,
    )


@pytest.mark.django_db
class TestCreateVmFromLibrenms:
    def test_creation_persists_computed_name_platform_role_and_comment(self):
        from dcim.models import Platform
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        platform = Platform.objects.create(name="VM integration platform", slug="vm-integration-platform")
        _site, _manufacturer, _device_type, role = _shared_infra()
        validation = _validation("complete", platform=platform, role=role)

        vm = create_vm_from_librenms(
            _payload(6101, _computed_name="vm-computed"),
            validation,
        )

        vm.refresh_from_db()
        assert vm.name == "vm-computed"
        assert vm.cluster == validation["cluster"]["cluster"]
        assert vm.platform == platform
        assert vm.role == role
        assert vm.custom_field_data["librenms_id"] == {SERVER_KEY: 6101}
        assert "device_id=6101" in vm.comments
        assert "netbox-librenms-plugin" in vm.comments

    def test_validated_name_precedes_raw_name_recomputation(self):
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        vm = create_vm_from_librenms(
            _payload(6102, hostname="raw-name.example.test"),
            _validation("validated-name", resolved_name="validated-name"),
        )

        assert vm.name == "validated-name"

    def test_raw_name_uses_the_shared_name_rules_as_the_final_fallback(self):
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        vm = create_vm_from_librenms(
            _payload(
                6103,
                hostname="host-choice.example.test",
                sysName="system-choice.example.test",
            ),
            _validation("raw-name"),
            use_sysname=False,
            strip_domain=True,
        )

        assert vm.name == "host-choice"

    def test_explicit_role_overrides_the_validation_role(self):
        from dcim.models import DeviceRole
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        validation_role = DeviceRole.objects.create(
            name="VM validation role",
            slug="vm-validation-role",
            color="00ff00",
        )
        explicit_role = DeviceRole.objects.create(
            name="VM explicit role",
            slug="vm-explicit-role",
            color="ff0000",
        )

        vm = create_vm_from_librenms(
            _payload(6104),
            _validation("role-override", role=validation_role),
            role=explicit_role,
        )

        assert vm.role == explicit_role

    def test_non_importable_validation_is_rejected_before_creation(self):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        before = VirtualMachine.objects.count()

        with pytest.raises(ValueError, match="VM cannot be imported: Missing cluster, Missing role"):
            create_vm_from_librenms(
                _payload(6105),
                {"can_import": False, "issues": ["Missing cluster", "Missing role"]},
            )

        assert VirtualMachine.objects.count() == before

    @pytest.mark.parametrize(
        "invalid_id",
        [None, True, 0, -1, 1.9, Decimal("1.9"), "1.9", "", "not-an-id"],
    )
    def test_invalid_ids_are_rejected_without_partial_persistence(self, invalid_id):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        before = VirtualMachine.objects.count()

        with pytest.raises(ValueError, match="device_id"):
            create_vm_from_librenms(
                {
                    "device_id": invalid_id,
                    "hostname": "invalid-id-vm",
                    "_computed_name": "invalid-id-vm",
                },
                _validation(f"invalid-{str(invalid_id).replace('.', '-')[:8]}"),
            )

        assert VirtualMachine.objects.count() == before

    def test_digit_string_id_is_canonicalized_under_the_selected_server(self):
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        vm = create_vm_from_librenms(
            _payload("6106", hostname="digit-string-vm"),
            _validation("digit-string"),
            server_key="secondary",
        )

        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] == {"secondary": 6106}

    def test_device_owned_id_is_rejected_by_the_real_assignment_lock(self):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        owner = make_device("vm-import-device-owner", librenms_cf={SERVER_KEY: 6107})

        with pytest.raises(ValueError, match=f"already assigned to device '{owner.name}'"):
            create_vm_from_librenms(
                _payload(6107, hostname="conflicting-vm"),
                _validation("device-conflict"),
            )

        assert not VirtualMachine.objects.filter(name="conflicting-vm").exists()

    def test_vm_owned_id_is_rejected_by_the_real_assignment_lock(self):
        from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms

        owner = make_vm("vm-import-vm-owner")
        owner.custom_field_data["librenms_id"] = {SERVER_KEY: 6108}
        owner.save()

        with pytest.raises(ValueError, match=f"already assigned to VM '{owner.name}'"):
            create_vm_from_librenms(
                _payload(6108, hostname="second-conflicting-vm"),
                _validation("vm-conflict"),
            )


@pytest.mark.django_db
class TestBulkImportVms:
    def test_empty_import_requires_real_permission_and_returns_empty_groups(self, librenms_api):
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api

        assert bulk_import_vms({}, api, user=_vm_writer("empty")) == {
            "success": [],
            "failed": [],
            "skipped": [],
        }

    def test_permission_denial_propagates_before_any_http_request(self, librenms_api, django_user_model):
        from django.core.exceptions import PermissionDenied
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api
        user = django_user_model.objects.create_user(username="vm-import-denied")

        with pytest.raises(PermissionDenied, match="virtualization.add_virtualmachine"):
            bulk_import_vms({6201: {}}, api, user=user)

    def test_missing_librenms_device_is_recorded_as_failed(self, librenms_api):
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, server = librenms_api
        server.register("/api/v0/devices/6202", {"status": "error"}, status=404)

        result = bulk_import_vms({6202: {}}, api, user=_vm_writer("not-found"))

        assert result["success"] == []
        assert result["skipped"] == []
        assert result["failed"] == [{"device_id": 6202, "error": "Device 6202 not found in LibreNMS"}]

    def test_existing_vm_is_skipped_from_live_validation(self, librenms_api):
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, server = librenms_api
        existing = make_vm("bulk-existing-vm")
        existing.custom_field_data["librenms_id"] = {SERVER_KEY: 6203}
        existing.save()
        server.device_info_response(device_id=6203, hostname=existing.name)

        result = bulk_import_vms({6203: {}}, api, user=_vm_writer("existing"))

        assert result["failed"] == []
        assert result["success"] == []
        assert result["skipped"] == [{"device_id": 6203, "reason": f"VM already exists: {existing.name}"}]

    def test_live_import_applies_cluster_role_server_and_name_preferences(self, librenms_api):
        from dcim.models import DeviceRole
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, server = librenms_api
        cluster = make_cluster("bulk-live-cluster")
        role = DeviceRole.objects.create(name="Bulk VM role", slug="bulk-vm-role", color="00ff00")
        server.register(
            "/api/v0/devices/6204",
            {
                "status": "ok",
                "devices": [
                    _payload(
                        6204,
                        hostname="host-selected.example.test",
                        sysName="system-ignored.example.test",
                    )
                ],
            },
        )

        result = bulk_import_vms(
            {6204: {"cluster_id": cluster.pk, "device_role_id": role.pk}},
            api,
            sync_options={"use_sysname": False, "strip_domain": True},
            user=_vm_writer("live"),
        )

        assert result["failed"] == []
        assert result["skipped"] == []
        assert len(result["success"]) == 1
        vm = result["success"][0]["device"]
        vm.refresh_from_db()
        assert vm.name == "host-selected"
        assert vm.cluster == cluster
        assert vm.role == role
        assert vm.custom_field_data["librenms_id"] == {SERVER_KEY: 6204}
        assert result["success"][0]["message"] == "VM host-selected created successfully"

    def test_prefetched_device_cache_can_complete_an_import_without_http(self, librenms_api):
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api
        cluster = make_cluster("bulk-prefetched-cluster")

        result = bulk_import_vms(
            {6205: {"cluster_id": cluster.pk}},
            api,
            libre_devices_cache={6205: _payload(6205, hostname="prefetched-vm")},
            user=_vm_writer("prefetched"),
        )

        assert result["failed"] == []
        assert result["success"][0]["device"].name == "prefetched-vm"

    def test_deleted_cluster_selection_fails_before_vm_creation(self, librenms_api):
        from virtualization.models import Cluster, VirtualMachine
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api
        cluster_id = missing_pk(Cluster)

        result = bulk_import_vms(
            {6206: {"cluster_id": cluster_id}},
            api,
            libre_devices_cache={6206: _payload(6206)},
            user=_vm_writer("missing-cluster"),
        )

        assert result["failed"] == [
            {"device_id": 6206, "error": f"Selected cluster (id={cluster_id}) no longer exists"}
        ]
        assert not VirtualMachine.objects.filter(name="vm-6206.example.test").exists()

    def test_deleted_role_selection_fails_before_vm_creation(self, librenms_api):
        from dcim.models import DeviceRole
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api
        cluster = make_cluster("bulk-missing-role-cluster")
        role_id = missing_pk(DeviceRole)

        result = bulk_import_vms(
            {6207: {"cluster_id": cluster.pk, "device_role_id": role_id}},
            api,
            libre_devices_cache={6207: _payload(6207)},
            user=_vm_writer("missing-role"),
        )

        assert result["failed"] == [{"device_id": 6207, "error": f"Selected role (id={role_id}) no longer exists"}]
        assert not VirtualMachine.objects.filter(name="vm-6207.example.test").exists()

    def test_missing_manual_cluster_stays_a_validation_failure(self, librenms_api):
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api

        result = bulk_import_vms(
            {6208: {}},
            api,
            libre_devices_cache={6208: _payload(6208)},
            user=_vm_writer("no-cluster"),
        )

        assert result["success"] == []
        assert len(result["failed"]) == 1
        assert result["failed"][0]["device_id"] == 6208
        assert "Cluster must be manually selected" in result["failed"][0]["error"]

    def test_one_bad_row_does_not_stop_later_real_imports(self, librenms_api):
        from virtualization.models import Cluster
        from netbox_librenms_plugin.import_utils.vm_operations import bulk_import_vms

        api, _server = librenms_api
        cluster = make_cluster("bulk-mixed-cluster")
        absent_cluster_id = missing_pk(Cluster)
        payloads = {
            6209: _payload(6209, hostname="mixed-failed-vm"),
            6210: _payload(6210, hostname="mixed-success-vm"),
        }

        result = bulk_import_vms(
            {
                6209: {"cluster_id": absent_cluster_id},
                6210: {"cluster_id": cluster.pk},
            },
            api,
            libre_devices_cache=payloads,
            user=_vm_writer("mixed"),
        )

        assert [row["device_id"] for row in result["failed"]] == [6209]
        assert [row["device_id"] for row in result["success"]] == [6210]
        assert result["success"][0]["device"].name == "mixed-success-vm"
