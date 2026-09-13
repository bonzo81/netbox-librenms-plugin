"""Integration tests for explicit virtual-machine import placement."""

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from django.http import QueryDict
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_cluster, make_device
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.test_modules_view import configure_servers
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms


@pytest.mark.django_db
def test_sync_import_post_creates_site_placed_vm_without_device_permissions(
    client,
    monkeypatch,
    settings,
):
    """An explicit VM target must not depend on a selected cluster or Device permissions."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7301
    placement_source = make_device("vm-site-placement-source")
    user = make_user_with_perms(
        "vm-site-placement-user",
        [("add", VirtualMachine)],
    )

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-site": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-site-placement.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.31",
            location=placement_source.site.name,
        )
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "vm-site",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "site",
            },
        )

    imported = VirtualMachine.objects.get(name="vm-site-placement.example.test")
    assert response.status_code == 302
    assert imported.site_id == placement_source.site_id
    assert imported.cluster_id is None
    assert imported.device_id is None
    assert not Device.objects.filter(name=imported.name).exists()


@pytest.mark.django_db
def test_confirmation_preserves_site_placed_vm_intent(client, monkeypatch, settings):
    """The confirmation form must not turn a cluster-less VM back into a Device."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7302
    placement_source = make_device("vm-confirm-placement-source")
    user = make_user_with_perms("vm-confirm-placement-user", [])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-confirm": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-confirm-placement.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.32",
            location=placement_source.site.name,
        )
        server.vc_inventory_callable(source_device_id, [], {})
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {
                "server_key": "vm-confirm",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "site",
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"Virtual Machine" in response.content
    assert placement_source.site.name.encode() in response.content
    assert f'name="object_type_{source_device_id}" value="virtualmachine"'.encode() in response.content
    assert f'name="vm_placement_{source_device_id}" value="site"'.encode() in response.content


@pytest.mark.django_db
def test_search_row_switches_to_ready_site_placed_vm(client, monkeypatch, settings):
    """An HTMX row refresh must show a cluster-less VM as ready when its site matches."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7303
    placement_source = make_device("vm-row-placement-source")
    user = make_user_with_perms("vm-row-placement-user", [])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-row": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-row-placement.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.33",
            location=placement_source.site.name,
        )
        client.force_login(user)

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:device_import_plan_update",
                kwargs={"device_id": source_device_id},
            ),
            {
                "server_key": "vm-row",
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "site",
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"Virtual machine" in response.content
    assert b"Matched site" in response.content
    assert b"device-ready" in response.content
    assert f'name="object_type_{source_device_id}"'.encode() in response.content
    assert f'name="vm_placement_{source_device_id}"'.encode() in response.content


@pytest.mark.django_db
def test_search_row_offers_netbox_host_selector(client, monkeypatch, settings):
    """Host placement must use NetBox's permission-scoped API selector."""
    from dcim.models import Device

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7309
    host = make_device("vm-row-selector-host")
    host.cluster = make_cluster("vm-row-selector-cluster")
    host.save(update_fields=["cluster"])
    user = make_user_with_perms("vm-host-selector-user", [("view", Device)])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-host-selector": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-host-selector.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.39",
            location="Unmatched VM location",
        )
        client.force_login(user)

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:device_import_plan_update",
                kwargs={"device_id": source_device_id},
            ),
            {
                "server_key": "vm-host-selector",
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "host",
                f"host_device_{source_device_id}": str(host.pk),
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert f'name="host_device_{source_device_id}"'.encode() in response.content
    assert b"/api/dcim/devices/" in response.content
    assert host.name.encode() in response.content
    assert b"device-ready" in response.content


@pytest.mark.django_db
def test_sync_import_post_creates_cluster_placed_vm(client, monkeypatch, settings):
    """An explicit cluster placement must work when the LibreNMS site does not match."""
    from virtualization.models import VirtualMachine

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7304
    cluster = make_cluster("vm-explicit-placement-cluster")
    user = make_user_with_perms("vm-cluster-placement-user", [("add", VirtualMachine)])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-cluster": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-cluster-placement.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.34",
            location="Unmatched VM location",
        )
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "vm-cluster",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "cluster",
                f"cluster_{source_device_id}": str(cluster.pk),
            },
        )

    imported = VirtualMachine.objects.get(name="vm-cluster-placement.example.test")
    assert response.status_code == 302
    assert imported.cluster_id == cluster.pk
    assert imported.device_id is None


@pytest.mark.django_db
@pytest.mark.parametrize("clustered_host", [False, True], ids=["standalone-host", "clustered-host"])
def test_sync_import_post_creates_host_placed_vm(
    client,
    clustered_host,
    monkeypatch,
    settings,
):
    """Host placement must add the host's cluster when NetBox requires it."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.utils import netbox_allows_standalone_vm_host

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7305 if not clustered_host else 7306
    host = make_device(f"vm-placement-host-{source_device_id}")
    if clustered_host:
        host.cluster = make_cluster("vm-placement-host-cluster")
        host.save(update_fields=["cluster"])
    user = make_user_with_perms(
        f"vm-host-placement-user-{source_device_id}",
        [("view", Device), ("add", VirtualMachine)],
    )

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-host": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname=f"vm-host-placement-{source_device_id}.example.test",
            hardware="VM hardware",
            serial="",
            ip=f"198.18.7.{source_device_id - 7260}",
            location="Unmatched VM location",
        )
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "vm-host",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "host",
                f"host_device_{source_device_id}": str(host.pk),
            },
        )

    assert response.status_code == 302
    if clustered_host or netbox_allows_standalone_vm_host():
        imported = VirtualMachine.objects.get(name=f"vm-host-placement-{source_device_id}.example.test")
        assert imported.device_id == host.pk
        assert imported.cluster_id == host.cluster_id
        assert imported.site_id == host.site_id
    else:
        assert not VirtualMachine.objects.filter(name=f"vm-host-placement-{source_device_id}.example.test").exists()


@pytest.mark.django_db
def test_confirmation_preserves_host_placement(client, monkeypatch, settings):
    """The confirmation step must show and resubmit the selected host Device."""
    from dcim.models import Device

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7310
    host = make_device("vm-confirm-host")
    user = make_user_with_perms("vm-confirm-host-user", [("view", Device)])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-confirm-host": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-confirm-host.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.40",
            location="Unmatched VM location",
        )
        server.vc_inventory_callable(source_device_id, [], {})
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {
                "server_key": "vm-confirm-host",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "host",
                f"host_device_{source_device_id}": str(host.pk),
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert host.name.encode() in response.content
    assert f'name="vm_placement_{source_device_id}" value="host"'.encode() in response.content
    assert f'name="host_device_{source_device_id}" value="{host.pk}"'.encode() in response.content


@pytest.mark.django_db
def test_sync_import_rejects_host_outside_user_scope(client, monkeypatch, settings):
    """A forged host ID must not bypass NetBox object restrictions."""
    from virtualization.models import VirtualMachine

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    source_device_id = 7311
    hidden_host = make_device("vm-hidden-placement-host")
    user = make_user_with_perms("vm-hidden-host-user", [("add", VirtualMachine)])

    with librenms_mock_server() as server:
        configure_servers(
            settings,
            {
                "vm-hidden-host": {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        server.device_info_response(
            device_id=source_device_id,
            hostname="vm-hidden-host.example.test",
            hardware="VM hardware",
            serial="",
            ip="198.18.7.41",
            location="Unmatched VM location",
        )
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
            {
                "server_key": "vm-hidden-host",
                "select": [str(source_device_id)],
                f"object_type_{source_device_id}": "virtualmachine",
                f"vm_placement_{source_device_id}": "host",
                f"host_device_{source_device_id}": str(hidden_host.pk),
            },
        )

    assert response.status_code == 302
    assert not VirtualMachine.objects.filter(name="vm-hidden-host.example.test").exists()


@pytest.mark.django_db
def test_device_target_rejects_forged_vm_placement(client, settings):
    """Placement data must not change a submitted Device target into a VM."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    source_device_id = 7307
    cluster = make_cluster("vm-forged-placement-cluster")
    user = make_user_with_perms(
        "vm-forged-placement-user",
        [("add", Device), ("change", Device), ("add", VirtualMachine)],
    )
    configure_servers(
        settings,
        {
            "vm-forged": {
                "librenms_url": "http://127.0.0.1:9",
                "api_token": "test-token",
                "verify_ssl": False,
            }
        },
    )
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {
            "server_key": "vm-forged",
            "select": [str(source_device_id)],
            f"object_type_{source_device_id}": "device",
            f"vm_placement_{source_device_id}": "cluster",
            f"cluster_{source_device_id}": str(cluster.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 400
    assert response.content == b"Invalid import selection"
    assert not Device.objects.filter(name="vm-forged-placement.example.test").exists()
    assert not VirtualMachine.objects.filter(name="vm-forged-placement.example.test").exists()


@pytest.mark.django_db
def test_background_job_deserializes_the_same_site_placement_plan(settings):
    """The queued path must consume the explicit plan without cluster-based classification."""
    from core.models import Job
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.jobs import ImportDevicesJob

    source_device_id = 7308
    placement_source = make_device("vm-job-placement-source")
    user = make_user_with_perms("vm-job-placement-user", [("add", VirtualMachine)])
    job = Job.objects.create(
        name="VM placement integration",
        user=user,
        job_id=uuid4(),
        data={},
    )
    configure_servers(
        settings,
        {
            "vm-job": {
                "librenms_url": "http://127.0.0.1:9",
                "api_token": "test-token",
                "verify_ssl": False,
            }
        },
    )
    libre_device = {
        "device_id": source_device_id,
        "hostname": "vm-job-placement.example.test",
        "sysName": "vm-job-placement.example.test",
        "hardware": "VM hardware",
        "serial": "-",
        "os": "linux",
        "ip": "198.18.7.38",
        "version": "1.0",
        "location": placement_source.site.name,
        "type": "server",
        "status": 1,
        "disabled": 0,
    }

    ImportDevicesJob(job).run(
        import_plans=[
            {
                "source_device_id": source_device_id,
                "object_type": "virtualmachine",
                "placement": {"method": "site"},
                "role_id": None,
            }
        ],
        server_key="vm-job",
        sync_options={"sync_interfaces": False, "sync_cables": False},
        libre_devices_cache={source_device_id: libre_device},
    )

    imported = VirtualMachine.objects.get(name="vm-job-placement.example.test")
    job.refresh_from_db()
    assert imported.site_id == placement_source.site_id
    assert job.data["imported_libre_vm_ids"] == [source_device_id]
    assert job.data["failed_count"] == 0


@pytest.mark.django_db
def test_background_job_applies_host_placement(settings):
    """The queued path must re-resolve and apply a permission-scoped host Device."""
    from core.models import Job
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.jobs import ImportDevicesJob

    source_device_id = 7315
    host = make_device("vm-job-placement-host")
    host.cluster = make_cluster("vm-job-placement-host-cluster")
    host.save(update_fields=["cluster"])
    user = make_user_with_perms(
        "vm-job-host-placement-user",
        [("view", Device), ("add", VirtualMachine)],
    )
    job = Job.objects.create(
        name="VM host placement integration",
        user=user,
        job_id=uuid4(),
        data={},
    )
    configure_servers(
        settings,
        {
            "vm-job-host": {
                "librenms_url": "http://127.0.0.1:9",
                "api_token": "test-token",
                "verify_ssl": False,
            }
        },
    )
    libre_device = {
        "device_id": source_device_id,
        "hostname": "vm-job-host-placement.example.test",
        "sysName": "vm-job-host-placement.example.test",
        "hardware": "VM hardware",
        "serial": "-",
        "os": "linux",
        "ip": "198.18.7.42",
        "version": "1.0",
        "location": "Unmatched VM location",
        "type": "server",
        "status": 1,
        "disabled": 0,
    }

    ImportDevicesJob(job).run(
        import_plans=[
            {
                "source_device_id": source_device_id,
                "object_type": "virtualmachine",
                "placement": {"method": "host", "host_device_id": host.pk},
                "role_id": None,
            }
        ],
        server_key="vm-job-host",
        libre_devices_cache={source_device_id: libre_device},
    )

    imported = VirtualMachine.objects.get(name="vm-job-host-placement.example.test")
    job.refresh_from_db()
    assert imported.device_id == host.pk
    assert imported.cluster_id == host.cluster_id
    assert job.data["imported_libre_vm_ids"] == [source_device_id]
    assert job.data["failed_count"] == 0


def test_import_intent_rejects_duplicate_object_type_values():
    """Duplicate controls must not make the model target ambiguous."""
    from netbox_librenms_plugin.import_plan import InvalidImportIntent, parse_import_row_intent

    data = QueryDict("object_type_7312=device&object_type_7312=virtualmachine")

    with pytest.raises(InvalidImportIntent, match="Submit object_type_7312 only once"):
        parse_import_row_intent(data, 7312)


@pytest.mark.parametrize("source_device_id", [0, -1])
def test_import_intent_rejects_non_positive_source_device_ids(source_device_id):
    """Synchronous imports must reject source IDs that jobs cannot deserialize."""
    from netbox_librenms_plugin.import_plan import InvalidImportIntent, parse_import_row_intent

    with pytest.raises(InvalidImportIntent, match="Source device ID must be a positive integer"):
        parse_import_row_intent(QueryDict(), source_device_id)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_device_id": 7313,
            "object_type": "device",
            "placement": {"method": "cluster", "cluster_id": 1},
        },
        {
            "source_device_id": 7314,
            "object_type": "virtualmachine",
            "placement": {"method": "host", "host_device_id": 1, "cluster_id": 2},
        },
    ],
)
def test_background_import_plan_rejects_contradictory_targets(payload):
    """Serialized jobs must fail closed on mixed Device and VM placement state."""
    from netbox_librenms_plugin.import_plan import InvalidImportIntent, deserialize_import_plans

    with pytest.raises(InvalidImportIntent):
        deserialize_import_plans([payload])


def test_import_dispatch_does_not_derive_model_from_placement_truthiness():
    """Placement fields must not control Device-versus-VM dispatch."""
    workflow_root = Path(__file__).parents[1]
    workflow_paths = [
        workflow_root / "views" / "imports" / "actions.py",
        workflow_root / "jobs.py",
    ]
    model_target_names = {"is_vm", "requested_vm", "vm_imports", "vm_ids_to_import"}
    placement_tokens = ("cluster", "host_device", "placement")

    class ModelSelectionVisitor(ast.NodeVisitor):
        """Find model-selection assignments controlled by placement state."""

        def __init__(self):
            self.placement_conditions = []
            self.violations = []

        def visit_If(self, node):
            condition = ast.unparse(node.test).lower()
            self.placement_conditions.append(any(token in condition for token in placement_tokens))
            for statement in node.body:
                self.visit(statement)
            self.placement_conditions.pop()
            for statement in node.orelse:
                self.visit(statement)

        @classmethod
        def _assigned_names(cls, target):
            if isinstance(target, ast.Name):
                return {target.id}
            if isinstance(target, ast.Subscript):
                return cls._assigned_names(target.value)
            if isinstance(target, (ast.Tuple, ast.List)):
                return set().union(*(cls._assigned_names(element) for element in target.elts))
            return set()

        def visit_Assign(self, node):
            assigned_names = set().union(*(self._assigned_names(target) for target in node.targets))
            value = ast.unparse(node.value).lower()
            if assigned_names & model_target_names and (
                any(token in value for token in placement_tokens) or any(self.placement_conditions)
            ):
                self.violations.append((node.lineno, sorted(assigned_names & model_target_names)))
            self.generic_visit(node.value)

    violations = {}
    for path in workflow_paths:
        visitor = ModelSelectionVisitor()
        visitor.visit(ast.parse(path.read_text()))
        if visitor.violations:
            violations[path.name] = visitor.violations

    assert violations == {}
