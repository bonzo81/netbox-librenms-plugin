"""Real-HTTP contract tests for the development LibreNMS stub."""

import copy
import json
import runpy
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import requests

from netbox_librenms_plugin.data_shapes.recordings_store import load_recording
from netbox_librenms_plugin.tests.conftest import make_recording_api
from netbox_librenms_plugin.tests.mock_librenms_server import DEFAULT_STUB_RECORDINGS, LibreNMSStubServer

TOKEN = "dev-stub-token"
RECORDING_NAMES = DEFAULT_STUB_RECORDINGS


@pytest.fixture(autouse=True)
def bypass_proxy_for_local_stub(monkeypatch):
    """Send local stub requests directly to the in-process HTTP server."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def test_devcontainer_stub_keeps_import_cache_enabled():
    config_path = Path(__file__).resolve().parents[2] / ".devcontainer/config/plugin-config.py.example"
    plugin_config = runpy.run_path(config_path)["PLUGINS_CONFIG"]["netbox_librenms_plugin"]

    assert plugin_config["servers"]["stub"]["cache_timeout"] > 0


def test_devcontainer_stub_command_runs_the_server_as_a_package_module():
    """Keep the Compose command compatible with imports needed by recordings."""
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is required to validate the devcontainer command")

    compose_path = Path(__file__).resolve().parents[2] / ".devcontainer/docker-compose.yml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config", "--format", "json"],
        cwd=compose_path.parent.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    service = json.loads(result.stdout)["services"]["librenms-stub"]
    command = service["command"]

    assert command[:3] == ["python", "-m", "netbox_librenms_plugin.tests.mock_librenms_server"]
    assert service["image"].startswith("netboxcommunity/netbox:")
    assert service["environment"]["PYTHONPATH"] == "/opt/netbox/netbox"
    assert service["healthcheck"]["test"][:3] == ["CMD", "python", "-c"]


def _request(server, method, path, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers["X-Auth-Token"] = TOKEN
    return requests.request(method, f"{server.url}{path}", headers=headers, timeout=5, **kwargs)


def _start_stub():
    recordings = [load_recording(name) for name in RECORDING_NAMES]
    return LibreNMSStubServer(recordings=recordings, api_token=TOKEN).start()


def test_stub_serves_recordings_and_derived_instance_endpoints_over_real_http():
    server = _start_stub()
    try:
        api = make_recording_api(server.url, server_key="stub", token=TOKEN)

        assert api.test_connection() == {"version": "development-stub"}

        ok, devices = api.list_devices()
        assert ok is True
        assert {device["device_id"] for device in devices} == {1, 12, 25, 32, 39, 1000, 2000}
        assert {device["device_id"]: device["status"] for device in devices} == {
            1: 0,
            12: 1,
            25: 1,
            32: 0,
            39: 1,
            1000: 1,
            2000: 1,
        }
        assert all(device["location"] == "Lab" for device in devices)

        ok, ports = api.get_ports(1)
        assert ok is True
        recorded_ports = load_recording("arcos-lag-transceivers")["responses"]["GET /api/v0/devices/1/ports"]
        assert [port["port_id"] for port in ports["ports"]] == [port["port_id"] for port in recorded_ports["ports"]]
        assert any(port.get("vlans") for port in ports["ports"])

        ok, addresses = api.get_device_ips(1)
        assert ok is True
        assert addresses
        assert addresses[0]["ipv4_address"] == "192.0.2.94"
        assert addresses[0]["port_id"] is not None

        ok, vlans = api.get_device_vlans(1)
        assert ok is True
        assert {vlan["vlan_vlan"] for vlan in vlans} == {100, 200}
        assert all(vlan["device_id"] == 1 for vlan in vlans)

        ok, links = api.get_device_links(1)
        assert ok is True
        assert links["status"] == "ok"
        assert links["links"]

        ok, locations = api.get_locations()
        assert ok is True
        assert locations == [{"id": 1, "location": "Lab", "lat": None, "lng": None}]

        ok, poller_groups = api.get_poller_groups()
        assert ok is True
        assert poller_groups == [{"id": 0, "group_name": "default", "descr": "Development stub"}]
    finally:
        server.stop()


def test_stub_supports_librenms_device_filters_and_lookup_aliases():
    server = _start_stub()
    try:
        all_devices = _request(server, "GET", "/api/v0/devices").json()["devices"]
        management = _request(
            server,
            "GET",
            "/api/v0/devices",
            params={"type": "type", "query": "management"},
        ).json()["devices"]
        down = _request(server, "GET", "/api/v0/devices", params={"type": "down"}).json()["devices"]

        assert {device["device_id"] for device in management} == {12, 25}
        assert {device["device_id"] for device in down} == {1, 32}

        target = next(device for device in all_devices if device["device_id"] == 1000)
        for lookup in (target["device_id"], target["hostname"], target["ip"]):
            response = _request(server, "GET", f"/api/v0/devices/{lookup}")
            assert response.status_code == 200
            assert response.json()["devices"][0]["device_id"] == 1000
    finally:
        server.stop()


def test_stub_derives_the_oob_controller_device_from_the_recorded_host_pair():
    recording = load_recording("linux-host-oob")
    server = LibreNMSStubServer(recordings=[recording], api_token=TOKEN).start()
    try:
        api = make_recording_api(server.url, server_key="stub", token=TOKEN)

        ok, devices = api.list_devices()
        assert ok is True
        by_id = {device["device_id"]: device for device in devices}
        assert set(by_id) == {25, 39}
        assert by_id[25]["serial"] == by_id[39]["serial"]
        assert by_id[25]["os"] == "idrac"

        ok, ports = api.get_ports(25)
        assert ok is True
        assert len(ports["ports"]) == 10
    finally:
        server.stop()


def test_stub_replays_the_scaffolded_virtual_machine_recording():
    recording = load_recording("linux-virtual-machine")
    server = LibreNMSStubServer(recordings=[recording], api_token=TOKEN).start()
    try:
        api = make_recording_api(server.url, server_key="stub", token=TOKEN)

        ok, devices = api.list_devices()
        assert ok is True
        assert len(devices) == 1
        assert devices[0]["type"] == "server"
        assert "virtual machine" in devices[0]["hardware"].casefold()

        ok, ports = api.get_ports(recording["device_id"])
        assert ok is True
        assert {port["ifName"] for port in ports["ports"]} == {"lo", "ens3"}
    finally:
        server.stop()


def test_stub_preserves_recorded_ports_response_metadata():
    """The single installed ports route must retain recorded top-level fields."""
    recording = copy.deepcopy(load_recording("linux-virtual-machine"))
    route = f"GET /api/v0/devices/{recording['device_id']}/ports"
    recording["responses"][route]["count"] = len(recording["responses"][route]["ports"])
    server = LibreNMSStubServer(recordings=[recording], api_token=TOKEN).start()
    try:
        response = _request(server, "GET", f"/api/v0/devices/{recording['device_id']}/ports")

        assert response.status_code == 200
        assert response.json()["count"] == len(recording["responses"][route]["ports"])
    finally:
        server.stop()


def test_stub_preserves_recorded_oob_ports_response_metadata():
    """The derived OOB ports route must retain recorded top-level fields."""
    recording = copy.deepcopy(load_recording("linux-host-oob"))
    oob_id = recording["meta"]["oob_id"]
    route = f"GET /api/v0/devices/{oob_id}/ports"
    recording["responses"][route]["count"] = len(recording["responses"][route]["ports"])
    server = LibreNMSStubServer(recordings=[recording], api_token=TOKEN).start()
    try:
        response = _request(server, "GET", f"/api/v0/devices/{oob_id}/ports")

        assert response.status_code == 200
        assert response.json()["count"] == len(recording["responses"][route]["ports"])
    finally:
        server.stop()


def test_stub_aggregates_instance_wide_serial_sensors():
    server = _start_stub()
    try:
        response = _request(server, "GET", "/api/v0/resources/sensors")
        expected = load_recording("avocent-serial-ports")["responses"]["GET /api/v0/resources/sensors"]

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        for sensor in expected["sensors"]:
            assert sensor in payload["sensors"]
    finally:
        server.stop()


def test_stub_supports_filtered_inventory_without_a_recorded_query_variant():
    server = _start_stub()
    try:
        api = make_recording_api(server.url, server_key="stub", token=TOKEN)

        ok, inventory = api.get_inventory_filtered(12, ent_physical_class="chassis")

        assert ok is True
        assert inventory == []

        ok, unfiltered = api.get_inventory_filtered(1000)
        assert ok is True
        present_class = unfiltered[0]["entPhysicalClass"]

        ok, matched = api.get_inventory_filtered(1000, ent_physical_class=present_class)
        assert ok is True
        assert matched
        assert all(item["entPhysicalClass"] == present_class for item in matched)
    finally:
        server.stop()


def test_stub_rejects_wrong_tokens_and_unimplemented_routes():
    server = _start_stub()
    try:
        health = requests.get(f"{server.url}/healthz", timeout=5)
        unauthenticated = requests.get(f"{server.url}/api/v0/devices", timeout=5)
        unknown = _request(server, "GET", "/api/v0/unsupported")

        assert health.status_code == 200
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["status"] == "error"
        assert unknown.status_code == 404
        assert unknown.json()["status"] == "error"
    finally:
        server.stop()


def test_stub_device_and_location_writes_are_visible_until_restart():
    server = _start_stub()
    try:
        api = make_recording_api(server.url, server_key="stub", token=TOKEN)

        ok, message = api.add_device(
            {
                "hostname": "device-new.example.test",
                "snmp_version": "v2c",
                "community": "stub-community",
                "force_add": True,
            }
        )
        assert ok is True, message

        lookup = _request(server, "GET", "/api/v0/devices/device-new.example.test")
        assert lookup.status_code == 200
        created = lookup.json()["devices"][0]

        ok, message = api.update_device_field(
            created["device_id"],
            {"field": ["location", "override_sysLocation"], "data": ["Stub Row B", "1"]},
        )
        assert ok is True, message
        updated = _request(server, "GET", f"/api/v0/devices/{created['device_id']}").json()["devices"][0]
        assert updated["location"] == "Stub Row B"
        assert updated["override_sysLocation"] == "1"

        old_hostname = created["hostname"]
        new_hostname = "device-renamed.example.test"
        ok, message = api.update_device_field(
            created["device_id"],
            {"field": ["hostname"], "data": [new_hostname]},
        )
        assert ok is True, message
        assert _request(server, "GET", f"/api/v0/devices/{old_hostname}").status_code == 404
        renamed = _request(server, "GET", f"/api/v0/devices/{new_hostname}")
        assert renamed.status_code == 200
        assert renamed.json()["devices"][0]["device_id"] == created["device_id"]

        ok, location = api.add_location({"location": "Stub Row B", "lat": None, "lng": None})
        assert ok is True, location
        ok, locations = api.get_locations()
        assert ok is True
        assert any(item["location"] == "Stub Row B" for item in locations)
    finally:
        server.stop()


def test_stub_rejects_duplicate_recording_device_ids():
    recording = load_recording("cisco-stackwise-3member")

    try:
        LibreNMSStubServer(recordings=[recording, recording], api_token=TOKEN)
    except ValueError as exc:
        assert "Duplicate recording device_id 1000" in str(exc)
    else:
        raise AssertionError("duplicate device ids must fail closed")


def test_stub_rejects_alias_collision_without_mutating_the_device():
    server = _start_stub()
    try:
        first = server.devices[1]
        second = server.devices[12]
        original_hostname = first["hostname"]

        response = _request(
            server,
            "PATCH",
            "/api/v0/devices/1",
            json={"field": ["hostname"], "data": [second["hostname"]]},
        )

        assert response.status_code == 409
        assert server.devices[1]["hostname"] == original_hostname
        assert _request(server, "GET", f"/api/v0/devices/{original_hostname}").status_code == 200
        assert _request(server, "GET", f"/api/v0/devices/{second['hostname']}").json()["devices"][0]["device_id"] == 12
    finally:
        server.stop()


def test_stub_rejects_device_id_update_without_mutating_the_device():
    server = _start_stub()
    try:
        original = copy.deepcopy(server.devices[1])

        response = _request(
            server,
            "PATCH",
            "/api/v0/devices/1",
            json={"field": ["location", "device_id"], "data": ["Changed", 9999]},
        )

        assert response.status_code == 422
        assert response.json()["status"] == "error"
        assert server.devices[1] == original
        current = _request(server, "GET", "/api/v0/devices/1")
        assert current.status_code == 200
        assert current.json()["devices"] == [original]
    finally:
        server.stop()


@pytest.mark.parametrize("alias_kind", ["sysName", "ip"])
def test_stub_rejects_derived_alias_collision_before_creating_device(alias_kind):
    """A derived lookup alias collision must return 409 without changing stub state."""
    server = _start_stub()
    try:
        existing = server.devices[1]
        if alias_kind == "sysName":
            hostname = f"{existing['sysName']}.other.example.test"
        else:
            candidate_ip = f"198.51.100.{server._next_device_id % 254 + 1}"
            update = _request(
                server,
                "PATCH",
                "/api/v0/devices/1",
                json={"field": ["ip"], "data": [candidate_ip]},
            )
            assert update.status_code == 200
            hostname = "unique-ip-collision.example.test"

        devices_before = copy.deepcopy(server.devices)
        next_device_id_before = server._next_device_id

        response = _request(server, "POST", "/api/v0/devices", json={"hostname": hostname})

        assert response.status_code == 409
        assert server.devices == devices_before
        assert server._next_device_id == next_device_id_before
    finally:
        server.stop()


def test_constructor_failure_releases_the_bound_port():
    recording = copy.deepcopy(load_recording("linux-host-oob"))
    oob_id = recording["meta"]["oob_id"]
    recording["responses"].pop(f"GET /api/v0/devices/{oob_id}/ports")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(ValueError, match="has no ports response"):
        LibreNMSStubServer(recordings=[recording], api_token=TOKEN, port=port)

    replacement = LibreNMSStubServer(
        recordings=[load_recording("linux-host")],
        api_token=TOKEN,
        port=port,
    )
    replacement._server.server_close()
