"""
The status lists report a lock conflict in a LibreNMS ID discovery as an unknown status, never as "Not Found".

The conflict is real: a second connection holds the device's row, which the discovery write locks,
under a short ``lock_timeout`` on the test's connection.
"""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    bind_librenms_server,
    make_cluster,
    make_device,
    make_superuser,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection
from netbox_librenms_plugin.tests.view_test_helpers import messages_on
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

UNKNOWN = '<i class="mdi mdi-help-circle"></i> Unknown'
NOT_FOUND = '<i class="mdi mdi-close-circle"></i> Not Found'


@transactional_db_with_all_apps()
@pytest.mark.parametrize("kind", ["device", "virtualmachine"])
def test_a_lock_conflict_in_the_discovery_write_leaves_the_status_unknown(client, settings, librenms_server, kind):
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    bind_librenms_server(settings, librenms_server, server_key="default")
    if kind == "device":
        obj = make_device("status-busy.example.net")
        model, url, filters = Device, "device_status_list", {"site": obj.site_id}
    else:
        obj = make_vm("status-busy-vm.example.net", make_cluster("status-busy-cluster"))
        model, url, filters = VirtualMachine, "vm_status_list", {"cluster": obj.cluster_id}
    librenms_server.register(
        f"/api/v0/devices/{obj.name}", {"status": "ok", "devices": [{"device_id": 4401, "hostname": obj.name}]}
    )
    client.force_login(make_superuser(f"status-busy-{kind}-user"))

    with second_connection() as other:
        lock_row(other, model, obj.pk)
        with lock_timeout(200):
            response = client.get(reverse(f"plugins:netbox_librenms_plugin:{url}"), filters)

    content = response.content.decode()
    assert response.status_code == 200
    assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
    assert UNKNOWN in content
    assert NOT_FOUND not in content
    obj.refresh_from_db()
    assert obj.custom_field_data.get("librenms_id") in (None, {})
