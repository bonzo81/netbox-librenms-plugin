"""Host and OOB interface name collision behavior."""

import pytest
from dcim.models import Interface
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView


SERVER_KEY = "default"
CONTESTED_REASON = "derived interface name is already used by another row"


def _port(port_id, name, *, source=None, description=None, dedup_conflict=False):
    port = {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": description or name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
    }
    if source is not None:
        port["_source"] = source
    if dedup_conflict:
        port["_dedup_conflict"] = True
    return port


def _sync(
    client,
    device,
    ports,
    selected,
    *,
    name_field="ifName",
    exclude_columns=None,
    target_devices=None,
):
    cache.set(
        SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY),
        {"ports": ports, "port_stack_relationships": {}},
        timeout=300,
    )
    post_data = {
        "server_key": SERVER_KEY,
        "interface_name_field": name_field,
        "select": [str(port_id) for port_id in selected],
        "exclude_columns": exclude_columns or ["vlans", "mac_address", "description", "mtu", "speed", "type"],
    }
    for port_id, target in (target_devices or {}).items():
        post_data[f"device_selection_{port_id}"] = str(target.pk)
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": device.pk},
        ),
        post_data,
    )


def _binding(interface):
    return get_librenms_device_id(interface, SERVER_KEY, auto_save=False)


@pytest.mark.django_db
def test_host_and_colliding_oob_rows_sync_under_distinct_names(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name", librenms_cf={SERVER_KEY: {"id": 71}})
    client.force_login(make_superuser("oob-derived-name-user"))
    ports = [
        _port("8501", "eth0"),
        _port("8502", "eth0", source="oob"),
        _port("8503", "bmc0", source="oob"),
    ]

    response = _sync(client, device, ports, [8501, 8502, 8503])

    assert response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth0", "eth0-oob", "bmc0"}
    assert _binding(interfaces["eth0"]) == 8501
    assert _binding(interfaces["eth0-oob"]) == 8502
    assert _binding(interfaces["bmc0"]) == 8503


@pytest.mark.django_db
def test_oob_row_is_skipped_when_the_host_owns_its_derived_name(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-taken", librenms_cf={SERVER_KEY: {"id": 72}})
    client.force_login(make_superuser("oob-derived-name-taken-user"))
    ports = [
        _port(8511, "eth0"),
        _port(8512, "eth0-oob"),
        _port(8513, "eth0", source="oob"),
    ]

    response = _sync(client, device, ports, [8513])

    assert response.status_code == 302
    assert not Interface.objects.filter(device=device).exists()
    assert any(CONTESTED_REASON in str(message) for message in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_cross_source_duplicate_port_id_is_rejected_before_writes(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-cross-source-port-id", librenms_cf={SERVER_KEY: {"id": 80}})
    client.force_login(make_superuser("oob-cross-source-port-id-user"))
    ports = [
        _port("1", "eth0"),
        _port(1, "eth0", source="oob"),
    ]

    response = _sync(client, device, ports, [1])

    assert response.status_code == 302
    assert not Interface.objects.filter(device=device).exists()
    response_messages = [str(message) for message in get_messages(response.wsgi_request)]
    assert any("duplicated in the cached interface data" in message for message in response_messages)


@pytest.mark.django_db
def test_two_oob_rows_contesting_one_synced_name_are_both_skipped(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-contested", librenms_cf={SERVER_KEY: {"id": 73}})
    client.force_login(make_superuser("oob-derived-name-contested-user"))
    ports = [
        _port(8521, "eth0"),
        _port(8522, "eth0", source="oob"),
        _port(8523, "eth0-oob", source="oob"),
    ]

    response = _sync(client, device, ports, [8522, 8523])

    assert response.status_code == 302
    assert not Interface.objects.filter(device=device).exists()
    warnings = [str(message) for message in get_messages(response.wsgi_request)]
    assert sum(CONTESTED_REASON in message for message in warnings) == 1
    assert "2 interface(s) skipped" in warnings[0]


@pytest.mark.django_db
def test_shared_lom_does_not_contest_an_ordinary_oob_name(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-shared-lom-name-claim", librenms_cf={SERVER_KEY: {"id": 81}})
    client.force_login(make_superuser("oob-shared-lom-name-claim-user"))
    ports = [
        _port(9201, "eth0"),
        _port(9202, "eth0", source="oob", dedup_conflict=True),
        _port(9203, "eth0-oob", source="oob"),
    ]

    response = _sync(client, device, ports, [9201, 9203])

    assert response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth0", "eth0-oob"}
    assert _binding(interfaces["eth0"]) == 9201
    assert _binding(interfaces["eth0-oob"]) == 9203
    response_messages = [str(message) for message in get_messages(response.wsgi_request)]
    assert not any(CONTESTED_REASON in message for message in response_messages)


@pytest.mark.django_db
def test_syncing_a_derived_oob_name_twice_is_idempotent(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab

    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-idempotent", librenms_cf={SERVER_KEY: {"id": 74}})
    client.force_login(make_superuser("oob-derived-name-idempotent-user"))
    ports = [
        _port(8531, "eth0"),
        _port(8532, "eth0", source="oob"),
    ]

    with django_capture_on_commit_callbacks(execute=True):
        first_response = _sync(client, device, ports, [8531, 8532])
    list(get_messages(first_response.wsgi_request))
    coordinator = SyncCacheConsistency(device)
    cache.delete(coordinator.state_key(SyncTab.INTERFACES, SERVER_KEY))

    with django_capture_on_commit_callbacks(execute=True):
        second_response = _sync(client, device, ports, [8531, 8532])

    assert first_response.status_code == second_response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth0", "eth0-oob"}
    assert _binding(interfaces["eth0"]) == 8531
    assert _binding(interfaces["eth0-oob"]) == 8532
    messages = [str(message) for message in get_messages(second_response.wsgi_request)]
    assert messages[-1:] == ["Selected interfaces synced successfully."]
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, SERVER_KEY)) is None


@pytest.mark.django_db
def test_derived_oob_name_survives_the_host_row_leaving_the_snapshot(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-host-gone", librenms_cf={SERVER_KEY: {"id": 79}})
    client.force_login(make_superuser("oob-derived-name-host-gone-user"))
    host = _port(9101, "eth0")
    oob = _port(9102, "eth0", source="oob")

    first_response = _sync(client, device, [host, oob], [9101, 9102])

    assert first_response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth0", "eth0-oob"}
    assert _binding(interfaces["eth0"]) == 9101
    assert _binding(interfaces["eth0-oob"]) == 9102
    list(get_messages(first_response.wsgi_request))

    second_response = _sync(client, device, [oob], [9102])

    assert second_response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth0", "eth0-oob"}
    assert _binding(interfaces["eth0"]) == 9101
    assert _binding(interfaces["eth0-oob"]) == 9102
    response_messages = [str(message) for message in get_messages(second_response.wsgi_request)]
    assert not any("sync was rolled back" in message for message in response_messages)


@pytest.mark.django_db
def test_bound_oob_keeps_its_name_when_unbound_reported_name_is_occupied(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-unbound-destination", librenms_cf={SERVER_KEY: {"id": 82}})
    make_interface(device, "eth0")
    client.force_login(make_superuser("oob-derived-name-unbound-destination-user"))
    host = _port(9301, "eth0")
    oob = _port(9302, "eth0", source="oob")

    first_response = _sync(client, device, [host, oob], [9302])

    assert first_response.status_code == 302
    bound = Interface.objects.get(device=device, name="eth0-oob")
    assert _binding(bound) == 9302
    bound.enabled = False
    bound.save(update_fields=["enabled"])
    list(get_messages(first_response.wsgi_request))

    second_response = _sync(client, device, [oob], [9302])

    assert second_response.status_code == 302
    bound.refresh_from_db()
    assert bound.name == "eth0-oob"
    assert bound.enabled is True
    assert _binding(bound) == 9302
    assert Interface.objects.filter(device=device, name="eth0").count() == 1
    response_messages = [str(message) for message in get_messages(second_response.wsgi_request)]
    assert not any("sync was rolled back" in message for message in response_messages)
    assert any(
        "kept its current name" in message and "reported name is in use" in message for message in response_messages
    )


@pytest.mark.django_db
def test_bound_oob_keeps_its_name_when_reported_name_is_bound_to_another_server(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-other-server", librenms_cf={SERVER_KEY: {"id": 83}})
    occupied = make_interface(device, "eth0")
    set_librenms_device_id(occupied, 9911, "secondary")
    occupied.save()
    client.force_login(make_superuser("oob-derived-name-other-server-user"))
    host = _port(9311, "eth0")
    oob = _port(9312, "eth0", source="oob")

    first_response = _sync(client, device, [host, oob], [9312])

    assert first_response.status_code == 302
    bound = Interface.objects.get(device=device, name="eth0-oob")
    assert _binding(bound) == 9312
    bound.enabled = False
    bound.save(update_fields=["enabled"])
    list(get_messages(first_response.wsgi_request))

    second_response = _sync(client, device, [oob], [9312])

    assert second_response.status_code == 302
    bound.refresh_from_db()
    occupied.refresh_from_db()
    assert bound.name == "eth0-oob"
    assert bound.enabled is True
    assert _binding(bound) == 9312
    assert get_librenms_device_id(occupied, "secondary", auto_save=False) == 9911
    response_messages = [str(message) for message in get_messages(second_response.wsgi_request)]
    assert not any("sync was rolled back" in message for message in response_messages)
    assert any(
        "kept its current name" in message and "reported name is in use" in message for message in response_messages
    )


@pytest.mark.django_db
def test_syncing_oob_before_host_keeps_the_same_names_and_bindings(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-order", librenms_cf={SERVER_KEY: {"id": 75}})
    client.force_login(make_superuser("oob-derived-name-order-user"))
    ports = [
        _port(8541, "eth1"),
        _port(8542, "eth1", source="oob"),
    ]

    first_response = _sync(client, device, ports, [8542])
    second_response = _sync(client, device, ports, [8541])

    assert first_response.status_code == second_response.status_code == 302
    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    assert set(interfaces) == {"eth1", "eth1-oob"}
    assert _binding(interfaces["eth1"]) == 8541
    assert _binding(interfaces["eth1-oob"]) == 8542


@pytest.mark.django_db
def test_oob_derived_name_that_exceeds_the_model_limit_is_skipped(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-derived-name-too-long", librenms_cf={SERVER_KEY: {"id": 77}})
    client.force_login(make_superuser("oob-derived-name-too-long-user"))
    limit = Interface._meta.get_field("name").max_length
    name = "x" * limit
    ports = [
        _port(8561, name),
        _port(8562, name, source="oob"),
    ]

    response = _sync(client, device, ports, [8562])

    assert response.status_code == 302
    assert not Interface.objects.filter(device=device).exists()
    warnings = [str(message) for message in get_messages(response.wsgi_request)]
    assert any(f"derived interface name is longer than the {limit} characters" in message for message in warnings)


@pytest.mark.django_db
def test_excluding_name_does_not_bind_an_oob_port_to_an_overlong_host_name(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-excluded-derived-name-too-long", librenms_cf={SERVER_KEY: {"id": 79}})
    client.force_login(make_superuser("oob-excluded-derived-name-too-long-user"))
    name = "x" * Interface._meta.get_field("name").max_length
    host = _port(8581, name)
    oob = _port(8582, name, source="oob")

    oob_response = _sync(client, device, [host, oob], [8582], exclude_columns=["name", "vlans"])

    assert oob_response.status_code == 302
    assert not Interface.objects.filter(device=device).exists()
    assert any(
        "derived interface name is longer" in str(message) for message in get_messages(oob_response.wsgi_request)
    )

    host_response = _sync(client, device, [host, oob], [8581])

    assert host_response.status_code == 302
    host_interface = Interface.objects.get(device=device, name=name)
    assert _binding(host_interface) == 8581


@pytest.mark.django_db
def test_excluding_name_still_updates_an_oob_interface_bound_by_port_id(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-excluded-derived-name-bound", librenms_cf={SERVER_KEY: {"id": 80}})
    interface = make_interface(device, "management-controller")
    interface.enabled = False
    set_librenms_device_id(interface, 8592, SERVER_KEY)
    interface.save()
    client.force_login(make_superuser("oob-excluded-derived-name-bound-user"))
    name = "x" * Interface._meta.get_field("name").max_length
    ports = [_port(8591, name), _port(8592, name, source="oob")]

    response = _sync(client, device, ports, [8592], exclude_columns=["name", "vlans"])

    assert response.status_code == 302
    interface.refresh_from_db()
    assert interface.name == "management-controller"
    assert interface.enabled is True
    assert _binding(interface) == 8592
    assert Interface.objects.filter(device=device).count() == 1


@pytest.mark.django_db
def test_excluding_name_preserves_an_operator_chosen_name(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-excluded-name", librenms_cf={SERVER_KEY: {"id": 78}})
    interface = make_interface(device, "management-controller")
    interface.enabled = False
    set_librenms_device_id(interface, 8572, SERVER_KEY)
    interface.save()
    client.force_login(make_superuser("oob-excluded-name-user"))
    ports = [
        _port(8571, "eth0"),
        _port(8572, "eth0", source="oob"),
    ]

    response = _sync(
        client,
        device,
        ports,
        [8572],
        exclude_columns=["name", "vlans", "mac_address", "description", "mtu", "speed", "type"],
    )

    assert response.status_code == 302
    interface.refresh_from_db()
    assert interface.name == "management-controller"
    assert interface.enabled is True
    assert _binding(interface) == 8572
    assert Interface.objects.filter(device=device).count() == 1


@pytest.mark.django_db
def test_excluding_name_does_not_bind_oob_port_to_unbound_host_interface(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("oob-excluded-colliding-name", librenms_cf={SERVER_KEY: {"id": 87}})
    host_interface = make_interface(device, "eth0")
    client.force_login(make_superuser("oob-excluded-colliding-name-user"))
    ports = [
        _port(9501, "eth0"),
        _port(9502, "eth0-oob"),
        _port(9503, "eth0", source="oob"),
    ]

    response = _sync(
        client,
        device,
        ports,
        [9503],
        exclude_columns=["name", "vlans", "mac_address", "description", "mtu", "speed", "type"],
    )

    assert response.status_code == 302
    host_interface.refresh_from_db()
    assert _binding(host_interface) is None
    assert Interface.objects.filter(device=device).count() == 1
    assert any(CONTESTED_REASON in str(message) for message in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_excluding_name_updates_a_host_row_when_its_reported_name_is_occupied(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("host-excluded-occupied-name", librenms_cf={SERVER_KEY: {"id": 84}})
    interface = make_interface(device, "operator-name")
    interface.enabled = False
    set_librenms_device_id(interface, 9301, SERVER_KEY)
    interface.save()
    occupied = make_interface(device, "eth0")
    set_librenms_device_id(occupied, 9302, SERVER_KEY)
    occupied.save()
    client.force_login(make_superuser("host-excluded-occupied-name-user"))

    response = _sync(
        client,
        device,
        [_port(9301, "eth0")],
        [9301],
        exclude_columns=["name", "vlans", "mac_address", "description", "mtu", "speed", "type"],
    )

    assert response.status_code == 302
    interface.refresh_from_db()
    occupied.refresh_from_db()
    assert interface.name == "operator-name"
    assert interface.enabled is True
    assert _binding(interface) == 9301
    assert occupied.name == "eth0"
    assert _binding(occupied) == 9302
    response_messages = [str(message) for message in get_messages(response.wsgi_request)]
    assert not any("interface(s) skipped" in message for message in response_messages)


@pytest.mark.django_db
def test_bound_host_reports_that_its_reported_name_belongs_to_another_port(client, settings):
    configure_default_librenms_server(settings)
    device = make_device("host-occupied-name-reason", librenms_cf={SERVER_KEY: {"id": 85}})
    interface = make_interface(device, "operator-name")
    interface.enabled = False
    set_librenms_device_id(interface, 9301, SERVER_KEY)
    interface.save()
    occupied = make_interface(device, "eth0")
    set_librenms_device_id(occupied, 9302, SERVER_KEY)
    occupied.save()
    client.force_login(make_superuser("host-occupied-name-reason-user"))

    response = _sync(client, device, [_port(9301, "eth0")], [9301])

    assert response.status_code == 302
    response_messages = [str(message) for message in get_messages(response.wsgi_request)]
    assert any("reported name belongs to a different LibreNMS port" in message for message in response_messages)
    assert not any("derived interface name" in message for message in response_messages)
    interface.refresh_from_db()
    occupied.refresh_from_db()
    assert interface.name == "operator-name"
    assert interface.enabled is True
    assert _binding(interface) == 9301
    assert occupied.name == "eth0"
    assert _binding(occupied) == 9302


@pytest.mark.django_db
def test_cross_page_host_row_reserves_its_inferred_member_name(client, settings):
    configure_default_librenms_server(settings)
    _chassis, (viewed_member, target_member) = make_virtual_chassis_members("oob-cross-page-name")
    set_librenms_device_id(viewed_member, 86, SERVER_KEY)
    viewed_member.save()
    host_interface = make_interface(target_member, "Ethernet2/1")
    client.force_login(make_superuser("oob-cross-page-name-user"))
    host = _port(9401, "Ethernet2/1")
    oob = _port(9402, "Ethernet2/1", source="oob")

    response = _sync(
        client,
        viewed_member,
        [host, oob],
        [9402],
        target_devices={9402: target_member},
    )

    assert response.status_code == 302
    host_interface.refresh_from_db()
    assert _binding(host_interface) is None
    oob_interface = Interface.objects.get(device=target_member, name="Ethernet2/1-oob")
    assert _binding(oob_interface) == 9402
    assert not Interface.objects.filter(device=viewed_member, name__in=["Ethernet2/1", "Ethernet2/1-oob"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["derived", "contested", "rebind"])
def test_verify_preserves_the_full_table_name_and_action_metadata(client, settings, case):
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

    configure_default_librenms_server(settings)
    device = make_device("verify-name-metadata", librenms_cf={SERVER_KEY: {"id": 71}})
    user = make_superuser("verify-name-metadata-user")
    client.force_login(user)
    if case == "rebind":
        interface = make_interface(device, "eth0")
        set_librenms_device_id(interface, 8501, SERVER_KEY)
        interface.save()
        ports = [_port(8502, "eth0")]
    else:
        ports = [_port(8501, "eth0"), _port(8502, "eth0", source="oob")]
        if case == "contested":
            ports.append(_port(8503, "eth0-oob"))
    cache.set(
        SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY),
        {"ports": ports, "port_stack_relationships": {}},
        timeout=300,
    )
    request = make_request("get", {"server_key": SERVER_KEY}, user=user)
    view = DeviceInterfaceTableView()
    view.setup(request, pk=device.pk)
    context = view.get_context_data(request, device, "ifName", server_key=SERVER_KEY)
    record = next(row for row in context["table"].data if row["port_id"] == 8502)
    expected = context["table"].format_interface_data(record, device)
    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_interface"),
        {"device_id": device.pk, "port_id": 8502, "server_key": SERVER_KEY, "interface_name_field": "ifName"},
        content_type="application/json",
    )
    assert response.status_code == 200
    actual = response.json()["formatted_row"]
    for key in ("parent", "actions"):
        assert actual[key] == expected[key]
    marker = {"derived": "Will sync as eth0-oob", "contested": "Name conflict", "rebind": "Rebind"}[case]
    assert marker in expected["parent"] + expected["actions"]
