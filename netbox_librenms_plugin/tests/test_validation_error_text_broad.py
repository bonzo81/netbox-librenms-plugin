"""
A ValidationError that a broad handler catches reaches a user only through the superuser rule.

Each test drives a real request or a real import job. A real ``pre_save`` receiver of another plugin,
or an admin ``CUSTOM_VALIDATORS`` rule, refuses a write with a message that names a device outside
the user's scope. A restricted user gets the refused field or the model, and a superuser gets the
message.
"""

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_save
from django.urls import reverse
from extras.validators import CustomValidator

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_serial_device, make_superuser
from netbox_librenms_plugin.tests.test_background_jobs import librenms_server  # noqa: F401
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

HIDDEN = "(only a superuser sees the message)"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"]),
]


class _Refuses(CustomValidator):
    """An admin validator that refuses every clean() with *text* and no field."""

    def __init__(self, text):
        super().__init__()
        self._text = text

    def validate(self, instance, request):
        self.fail(self._text)


def _open_user(tag, models, *, device=None):
    """Return a user with every action on *models* and, when given, view and change on *device* only."""
    from core.models import ObjectType
    from dcim.models import Device
    from users.models import ObjectPermission

    user = make_user_with_perms(
        f"{tag}-viewer",
        [] if device is None else [("view", Device), ("change", Device)],
        constraints=None if device is None else {"pk": device.pk},
    )
    permission = ObjectPermission.objects.create(name=f"{tag}-open", actions=["view", "add", "change", "delete"])
    permission.object_types.set([ObjectType.objects.get_for_model(model) for model in models])
    permission.users.set([user])
    return get_user_model().objects.get(pk=user.pk)


def _refuse(model, hidden, *, pk=None):
    """Connect a pre_save receiver that refuses a save of *model* (or only row *pk*) and names *hidden*."""

    def refuse(sender, instance, **kwargs):
        if pk is None or instance.pk == pk:
            raise ValidationError({hidden.name: [f"Conflicts with {hidden.name}."]})

    pre_save.connect(refuse, sender=model, weak=False)
    return lambda: pre_save.disconnect(refuse, sender=model)


def _assert_shown(texts, hidden, superuser, restricted_text):
    texts = " | ".join(texts)
    if superuser:
        assert f"Conflicts with {hidden.name}." in texts, texts
    else:
        assert f"NetBox refuses {restricted_text} {HIDDEN}" in texts, texts
        assert hidden.name not in texts, texts


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_the_cable_sync_settings_save(client, superuser):
    from extras.models import Tag

    from netbox_librenms_plugin.models import LibreNMSSettings

    tag = f"vetext-settings-{int(superuser)}"
    hidden = make_device(f"{tag}-hidden")
    LibreNMSSettings.objects.get_or_create()
    client.force_login(make_superuser(f"{tag}-superuser") if superuser else _open_user(tag, [Tag]))

    disconnect = _refuse(Tag, hidden)
    try:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:settings"),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": f"{tag}-provenance",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Managed cable",
            },
        )
    finally:
        disconnect()

    _assert_shown([response.content.decode()], hidden, superuser, "the tag")


def test_the_ip_address_sync(client, settings, live_librenms, superuser):
    from dcim.models import Interface
    from ipam.models import IPAddress

    from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _configure_test_server, _refresh_ip_snapshot

    _configure_test_server(settings)
    tag = f"vetext-ipsync-{int(superuser)}"
    device, hidden = make_device(tag, librenms_cf={"default": {"id": 42}}), make_device(f"{tag}-hidden")
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"default": 7001}
    interface.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser(f"{tag}-refresher"))
    assert _refresh_ip_snapshot(client, device, "198.18.0.10/24", 24, live_librenms).status_code == 200
    client.force_login(
        make_superuser(f"{tag}-superuser") if superuser else _open_user(tag, [Interface, IPAddress], device=device)
    )

    disconnect = _refuse(IPAddress, hidden)
    try:
        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
                kwargs={"object_type": "device", "pk": device.pk},
            ),
            {"server_key": "default", "select": "198.18.0.10/24", "vrf_198.18.0.10/24": ""},
        )
    finally:
        disconnect()

    assert not IPAddress.objects.filter(address="198.18.0.10/24").exists()
    _assert_shown(_messages(response), hidden, superuser, "the IP address")


def test_the_cable_create(client, superuser):
    from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device
    from extras.models import Tag

    from netbox_librenms_plugin.tests.test_cable_overwrite import SERVER_KEY, _rendered_sync_data
    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    tag = f"vetext-cable-{int(superuser)}"
    acs, (csp,), _ = make_serial_device(f"{tag}-acs", csp_names=["ttyS5"])
    _router, _, (cp,) = make_serial_device(f"{tag}-router", cp_names=["console-B"])
    hidden = make_device(f"{tag}-hidden")
    link = {
        "local_port": "ttyS5",
        "local_port_id": f"serial:{csp.pk}-s",
        "_source": "serial",
        "device_id": acs.id,
        "remote_device": _router.name,
        "netbox_local_interface_id": csp.pk,
        "netbox_remote_interface_id": cp.pk,
        "can_create_cable": True,
        "is_configured": True,
        "sensor_id": 1,
        "sensor_index_int": 5,
    }
    cache.set(object.__new__(SyncCablesView).get_cache_key(acs, "links", SERVER_KEY), {"links": [link]}, timeout=300)
    client.force_login(make_superuser(f"{tag}-renderer"))
    post_data = _rendered_sync_data(client, acs, link["local_port_id"])
    if superuser:
        client.force_login(make_superuser(f"{tag}-superuser"))
    else:
        client.force_login(_open_user(tag, [Cable, ConsolePort, ConsoleServerPort, Device, Tag]))

    disconnect = _refuse(Cable, hidden)
    try:
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[acs.pk]),
            data=post_data,
            HTTP_HX_REQUEST="true",
        )
    finally:
        disconnect()

    assert ConsoleServerPort.objects.get(pk=csp.pk).cable_id is None
    _assert_shown([response.content.decode(), *_messages(response)], hidden, superuser, "the cable")


def test_a_background_import_saves_only_the_hidden_text(settings, librenms_server, superuser):  # noqa: F811
    """A job's data and log are read later by each viewer of the job, so they hide the message for any job user."""
    from core.choices import JobStatusChoices
    from core.models import Job
    from dcim.models import Device, DeviceRole
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.jobs import ImportDevicesJob
    from netbox_librenms_plugin.tests.conftest import make_cluster
    from netbox_librenms_plugin.tests.test_background_jobs import SERVER_KEY, _device_payload, _import_user
    from netbox_librenms_plugin.tests.view_test_helpers import grant

    tag = f"vetext-job-{int(superuser)}"
    infrastructure, hidden = make_device(f"{tag}-infra"), make_device(f"{tag}-hidden")
    cluster = make_cluster(f"{tag}-cluster")
    if superuser:
        user = make_superuser(f"{tag}-superuser")
    else:
        user = grant(_import_user(tag), "view", DeviceRole, constraints={"pk": infrastructure.role_id})
    job = Job.objects.create(name=tag, user=user, job_id=uuid4(), data={})
    rows = {
        6481: _device_payload(
            6481,
            hostname=f"{tag}-device",
            hardware=infrastructure.device_type.model,
            location=infrastructure.site.name,
        ),
        6482: _device_payload(6482, hostname=f"{tag}-vm"),
    }
    librenms_server.register("/api/v0/devices/6481", {"status": "ok", "devices": [rows[6481]]})
    refusal = _Refuses(f"Conflicts with {hidden.name}.")
    settings.CUSTOM_VALIDATORS = {"dcim.device": [refusal], "virtualization.virtualmachine": [refusal]}

    # handle() runs the whole job lifecycle: terminate() saves the data and the log entries.
    ImportDevicesJob.handle(
        job,
        import_plans=[
            {"source_device_id": 6481, "object_type": "device", "role_id": infrastructure.role_id, "rack_id": None},
            {
                "source_device_id": 6482,
                "object_type": "virtualmachine",
                "placement": {"method": "cluster", "cluster_id": cluster.pk},
                "role_id": None,
            },
        ],
        server_key=SERVER_KEY,
        sync_options={"sync_interfaces": False, "sync_cables": False},
        libre_devices_cache=rows,
    )

    job = Job.objects.get(pk=job.pk)
    assert job.status == JobStatusChoices.STATUS_COMPLETED, (job.status, job.error, job.log_entries)
    assert not Device.objects.filter(name=f"{tag}-device").exists()
    assert not VirtualMachine.objects.filter(name=f"{tag}-vm").exists()
    errors = {error["device_id"]: error["error"] for error in job.data["errors"]}
    assert set(errors) == {6481, 6482}, job.data
    _assert_shown([errors[6481]], hidden, False, "the device")
    _assert_shown([errors[6482]], hidden, False, "the virtual machine")
    failures = {
        prefix: [entry["message"] for entry in job.log_entries if entry["message"].startswith(prefix)]
        for prefix in ("Failed to import device 6481: ", "Failed to import VM 6482: ")
    }
    assert all(failures.values()), job.log_entries
    _assert_shown(failures["Failed to import device 6481: "], hidden, False, "the device")
    _assert_shown(failures["Failed to import VM 6482: "], hidden, False, "the virtual machine")
    assert hidden.name not in str(job.log_entries), job.log_entries
