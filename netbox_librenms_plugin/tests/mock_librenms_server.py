"""
Minimal HTTP mock for LibreNMS API responses.

Usage in tests: take the ``librenms_server`` fixture from ``conftest.py``, or open the context
manager directly:

    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    with librenms_mock_server() as server:
        ...
"""

import argparse
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from netbox_librenms_plugin.data_shapes.envelope import unwrap_response

# The stub only ever receives small JSON writes, so a declared body past this cap is a
# mistake or an abuse and is refused before a byte is read.
MAX_REQUEST_BODY_BYTES = 1 << 20


class _RawResponse:
    """One response body that the test server must not JSON-encode."""

    def __init__(self, status, body, content_type):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.content_type = content_type


class _DisconnectResponse:
    """Close the HTTP connection without sending a response."""


_DISCONNECT_RESPONSE = _DisconnectResponse()


class _LibreNMSHandler(BaseHTTPRequestHandler):
    """Request handler that dispatches to registered route responses."""

    def log_message(self, format, *args):  # noqa: A002
        if not self.server.quiet:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self._send_body(status, data, "application/json")

    def _send_body(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # A timeout test can close its socket before the delayed response is ready.
            pass

    def _handle_request(self, method, body=None):
        """Dispatch to the registered route for this path, with optional method+query fallback."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return

        if self._reject_unauthenticated():
            return

        routes = self.server.routes  # type: ignore[attr-defined]
        request = {
            "method": method,
            "path": path,
            # Preserve blank values so live requests normalize the same way as recordings.
            "query": parse_qs(query, keep_blank_values=True),
            "headers": dict(self.headers),
            "body": body,
        }
        self.server.requests.append(request)  # type: ignore[attr-defined]

        # Build lookup keys: prefer method+path+query, then path+query, then path-only.
        candidates = []
        if query:
            candidates.append(f"{method} {path}?{query}")
            candidates.append(f"{path}?{query}")
        candidates.append(f"{method} {path}")
        candidates.append(path)

        for key in candidates:
            if key in routes:
                entry = routes[key]
                if entry is _DISCONNECT_RESPONSE:
                    self.close_connection = True
                    return
                if isinstance(entry, _RawResponse):
                    self._send_body(entry.status, entry.body, entry.content_type)
                    return
                if callable(entry):
                    status, resp_body = entry(**request)
                else:
                    status, resp_body = entry
                self._send_json(status, resp_body)
                return

        self._send_json(404, {"status": "error", "message": f"No mock for {self.path}"})

    def do_GET(self):
        self._handle_request("GET")

    def _reject_unauthenticated(self):
        """Send 401 and report True when the request carries no valid token."""
        api_token = self.server.api_token  # type: ignore[attr-defined]
        # GET only: _handle_request answers /healthz before any auth check, so the exemption is
        # needed nowhere else. Extending it to a body-bearing method would let an unauthenticated
        # caller declare a Content-Length, send no body, and hold a handler thread in rfile.read().
        if not api_token or (urlparse(self.path).path == "/healthz" and self.command == "GET"):
            return False
        if self.headers.get("X-Auth-Token") == api_token:
            return False
        self._send_json(401, {"status": "error", "message": "Unauthorized"})
        return True

    def _handle_request_with_body(self, method):
        # Both checks run before read(), so a caller cannot hold a handler thread or its memory
        # on a body the stub would never accept.
        if self._reject_unauthenticated():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            # A negative length is truthy, so read() would block until the client disconnects.
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length"})
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_json(413, {"status": "error", "message": "Request body too large"})
            return
        raw_body = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw_body) if raw_body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = raw_body.decode(errors="replace")
        self._handle_request(method, body=body)

    def do_POST(self):
        self._handle_request_with_body("POST")

    def do_PATCH(self):
        self._handle_request_with_body("PATCH")


class MockLibreNMSServer:
    """Wrap a simple HTTP mock server with route registration and context management."""

    def __init__(self, host="127.0.0.1", port=0, *, api_token=None, quiet=True):
        self._server = ThreadingHTTPServer((host, port), _LibreNMSHandler)
        # A real LibreNMS always serves /system, and the settings page reads it to report the
        # server version. Seed a default so a test only registers it to assert something
        # specific, and can still override it to simulate an unhealthy server.
        self._server.routes = {
            "/api/v0/system": (200, {"status": "ok", "system": [{"local_ver": "24.1.0"}]}),
        }
        self._server.requests = []
        self._server.api_token = api_token
        self._server.quiet = quiet
        self.routes = self._server.routes  # expose on wrapper as documented
        self.requests = self._server.requests
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        _, bound_port = self._server.server_address
        client_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        self.url = f"http://{client_host}:{bound_port}"

    def register(self, path: str, body, status: int = 200, method: str | None = None):
        """Register a path response, optionally restricted to one HTTP method."""
        key = f"{method} {path}" if method else path
        if callable(body):
            self._server.routes[key] = body
        else:
            self._server.routes[key] = (status, body)

    def register_raw(
        self,
        path: str,
        body: str | bytes,
        status: int = 200,
        content_type: str = "text/plain",
        method: str | None = None,
    ):
        """Register a response body without JSON encoding it."""
        key = f"{method} {path}" if method else path
        self._server.routes[key] = _RawResponse(status, body, content_type)

    def register_disconnect(self, path: str, method: str | None = None):
        """Register a route that closes the connection before sending a response."""
        key = f"{method} {path}" if method else path
        self._server.routes[key] = _DISCONNECT_RESPONSE

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            import warnings

            warnings.warn(
                f"MockLibreNMSServer thread {self._thread.ident} did not exit within 5 s; "
                "socket may not be fully released",
                ResourceWarning,
                stacklevel=2,
            )

    # ------- default LibreNMS-shaped responses -------

    def add_device_response(self, device_id: int = 1, hostname: str = "test-host"):
        self.register(
            "/api/v0/devices",
            {"status": "ok", "id": device_id, "hostname": hostname},
            method="POST",
        )

    def device_info_response(
        self,
        device_id: int = 1,
        hostname: str = "test-host",
        hardware: str = "WS-C3560X-24T-S",
        os: str = "ios",
        serial: str = "SN123",
        ip: str = "192.168.1.1",
        version: str = "15.2(4)E7",
        features: str = "-",
        location: str = "-",
    ):
        self.register(
            f"/api/v0/devices/{device_id}",
            {
                "status": "ok",
                "devices": [
                    {
                        "device_id": device_id,
                        "hostname": hostname,
                        "hardware": hardware,
                        "os": os,
                        "serial": serial,
                        "sysName": hostname,
                        "ip": ip,
                        "version": version,
                        "features": features,
                        "location": location,
                    }
                ],
            },
        )

    def ports_response(self, device_id: int = 1, ports=None):
        if ports is None:
            ports = [
                {
                    "port_id": 101,
                    "ifName": "GigabitEthernet0/1",
                    "ifDescr": "GigabitEthernet0/1",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifAdminStatus": "up",
                    "ifAlias": "uplink",
                    "ifPhysAddress": "aa:bb:cc:dd:ee:01",
                    "ifMtu": 1500,
                    "ifVlan": 1,
                    "ifTrunk": 0,
                }
            ]
        self.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": ports})

    def auth_error_response(self, path="/api/v0/devices"):
        self.register(path, {"status": "error", "message": "Authentication failed"}, status=401)

    def inventory_response(self, device_id: int, items: list, status: int = 200):
        """Register a plain inventory response for /api/v0/inventory/{device_id}/all."""
        payload_status = "ok" if 200 <= status < 300 else "error"
        payload = (
            {"status": payload_status, "inventory": items} if payload_status == "ok" else {"status": payload_status}
        )
        self.register(
            f"/api/v0/inventory/{device_id}/all",
            payload,
            status=status,
            method="GET",
        )

    def load_recording(self, recording: dict):
        """Register recording responses with order-independent exact query matching for variants."""
        by_route: dict[tuple[str, str], list] = {}
        for key, value in recording.get("responses", {}).items():
            method, path, query = _split_recording_key(key)
            # Keep the FULL value set per key (sorted) so a repeated param like ?a=1&a=2 is a
            # distinct shape from ?a=1 — collapsing to v[0] would let them false-match.
            qdict = {k: tuple(sorted(v)) for k, v in parse_qs(query, keep_blank_values=True).items()} if query else {}
            status, body = unwrap_response(value)
            by_route.setdefault((method, path), []).append((qdict, status, body))

        for (method, path), variants in by_route.items():
            # A single queryless variant is registered as a bare path on purpose: capture.py keys
            # response-irrelevant endpoints (e.g. /ports) with key_params=None precisely so "the
            # loader serves it for any query the production readers send" (capture.py comment) —
            # get_ports() sends columns=…&with=vlans that the recording deliberately doesn't store.
            # A strict exact-match here would 404 that legitimate replay. Multiple variants of one
            # path ARE query-keyed, so those route through the exact-match selector.
            if len(variants) == 1 and not variants[0][0]:
                qdict, status, body = variants[0]
                self.register(path, body, status=status, method=method)
            else:
                self.register(path, _recording_variant_handler(path, variants), method=method)

    def vc_inventory_callable(self, device_id: int, root_items: list, children_by_parent_index: dict):
        """Register VC inventory responses for root and chassis-filtered child queries."""
        root = root_items
        children = children_by_parent_index

        def _handler(method, path, query, headers, body):
            contained_in = query.get("entPhysicalContainedIn", [None])[0]
            if contained_in == "0":
                return 200, {"status": "ok", "inventory": root}
            if contained_in is not None:
                # Require entPhysicalClass=chassis for child queries so tests catch
                # any regression where the production code stops sending the class filter.
                phy_class = query.get("entPhysicalClass", [None])[0]
                if phy_class != "chassis":
                    return 200, {"status": "ok", "inventory": []}
                try:
                    idx = int(contained_in)
                except (TypeError, ValueError):
                    return 404, {"status": "error", "message": "bad contained_in"}
                items = children.get(idx, [])
                return 200, {"status": "ok", "inventory": items}
            # No filter → return all (fallback for /all)
            all_items = list(root)
            for v in children.values():
                all_items.extend(v)
            return 200, {"status": "ok", "inventory": all_items}

        self.register(f"/api/v0/inventory/{device_id}", _handler, method="GET")
        self.register(f"/api/v0/inventory/{device_id}/all", _handler, method="GET")


DEFAULT_STUB_RECORDINGS = (
    "cisco-stackwise-3member",
    "arcos-lag-transceivers",
    "avocent-serial-ports",
    "linux-host",
    "linux-host-oob",
    "linux-virtual-machine",
)
DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "data_shapes" / "recordings"


def _split_recording_key(key):
    """Split a recording response key into its method, path and query, defaulting a bare key to GET."""
    method, separator, rest = key.partition(" ")
    if not separator:  # No verb prefix -> the whole key is a GET route.
        method, rest = "GET", key
    path, _, query = rest.partition("?")
    return method, path, query


def _unwrap_recorded_response(value):
    """Return the JSON body from a recording response value."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return value[1]
    return unwrap_response(value)[1]


class LibreNMSStubServer(MockLibreNMSServer):
    """Serve recorded LibreNMS data shapes, derive instance endpoints, and keep device and location writes in memory."""

    def __init__(self, recordings, *, api_token, host="127.0.0.1", port=0, quiet=True):
        super().__init__(host, port, api_token=api_token, quiet=quiet)
        self._lock = threading.RLock()
        self.devices = {}
        self.ports_by_device = {}
        self.ports_body_by_device = {}
        self.inventory_by_device = {}
        self.sensors = []
        self.vlans = []
        self.links_by_device = {}
        self.locations = []
        self._aliases = {}
        self._aliases_by_device = {}

        recordings = list(recordings)
        seen_device_ids = set()
        try:
            for recording in recordings:
                device_id = recording.get("device_id")
                if device_id in seen_device_ids or device_id in self.devices:
                    raise ValueError(f"Duplicate recording device_id {device_id}")
                seen_device_ids.add(device_id)
                self.load_recording(recording)
                self._load_stub_recording(recording)

            if not self.devices:
                raise ValueError("At least one recording with a device response is required")

            self._next_device_id = max(self.devices) + 1
            self._install_instance_routes()
        except BaseException:
            self._server.server_close()
            raise

    @staticmethod
    def _find_response(recording, route):
        """Return the first recorded body whose request key has the given route."""
        for key, value in recording.get("responses", {}).items():
            _, request_route, _ = _split_recording_key(key)
            if request_route == route:
                return _unwrap_recorded_response(value)
        return None

    @staticmethod
    def _normalise_device(device, device_id):
        """Add deterministic fields that the discovery screens require."""
        normalised = copy.deepcopy(device) if isinstance(device, dict) else {}
        normalised["device_id"] = device_id
        normalised["hostname"] = normalised.get("hostname") or f"device-{device_id}.example.test"
        normalised["sysName"] = normalised.get("sysName") or normalised["hostname"].split(".", 1)[0]
        normalised["ip"] = normalised.get("ip") or f"198.51.100.{device_id % 254 + 1}"
        normalised["hardware"] = normalised.get("hardware") or f"MODEL-STUB-{device_id}"
        normalised["os"] = normalised.get("os") or "stub-os"
        normalised["serial"] = normalised.get("serial") or f"SN-STUB-{device_id}"
        normalised["type"] = normalised.get("type") or "network"
        normalised["location"] = normalised.get("location") or "Lab"
        normalised["location_id"] = normalised.get("location_id") or 1
        status = normalised.get("status", 1)
        normalised["status"] = 1 if status in (True, 1, "1", "up") else 0
        normalised["disabled"] = int(bool(normalised.get("disabled", 0)))
        normalised["ignore"] = int(bool(normalised.get("ignore", 0)))
        return normalised

    @staticmethod
    def _add_approximate_vlans(ports):
        """Add a small VLAN scenario when a recording contains no VLAN data."""
        prepared = copy.deepcopy(ports) if isinstance(ports, list) else []
        if any(
            isinstance(port, dict) and (port.get("ifVlan") not in (None, "", 0, "0") or port.get("vlans"))
            for port in prepared
        ):
            return prepared

        candidates = [
            port
            for port in prepared
            if isinstance(port, dict)
            and port.get("port_id") is not None
            and port.get("ifType") in ("ethernetCsmacd", "ieee8023adLag")
        ]
        if not candidates:
            return prepared

        candidates[0]["ifVlan"] = 100
        candidates[0]["ifTrunk"] = 0
        candidates[0]["vlans"] = [{"vlan": 100, "untagged": 1, "state": "forwarding"}]
        if len(candidates) > 1:
            candidates[1]["ifVlan"] = 100
            candidates[1]["ifTrunk"] = "dot1Q"
            candidates[1]["vlans"] = [
                {"vlan": 100, "untagged": 1, "state": "forwarding"},
                {"vlan": 200, "untagged": 0, "state": "forwarding"},
            ]
        return prepared

    def _load_stub_recording(self, recording):
        device_id = recording.get("device_id")
        device_body = self._find_response(recording, f"/api/v0/devices/{device_id}")
        raw_devices = device_body.get("devices") if isinstance(device_body, dict) else None
        if not isinstance(raw_devices, list) or not raw_devices or not isinstance(raw_devices[0], dict):
            # Returning here would start a stub whose device, ports, inventory and OOB routes are
            # all silently missing, and the failure would surface much later as a puzzling 404.
            name = (recording.get("meta") or {}).get("name") or recording.get("name") or "<unnamed>"
            raise ValueError(f"stub recording {name!r} has no usable /api/v0/devices/{device_id} response")

        device = self._normalise_device(raw_devices[0], device_id)
        self.devices[device_id] = device

        ports_body = self._find_response(recording, f"/api/v0/devices/{device_id}/ports")
        recorded_ports = ports_body.get("ports") if isinstance(ports_body, dict) else []
        ports = self._add_approximate_vlans(recorded_ports)
        self.ports_by_device[device_id] = ports
        normalised_ports_body = copy.deepcopy(ports_body) if isinstance(ports_body, dict) else {"status": "ok"}
        normalised_ports_body["status"] = "ok"
        normalised_ports_body["ports"] = ports
        self.ports_body_by_device[device_id] = normalised_ports_body

        inventory = []
        seen_inventory = set()
        for key, value in recording.get("responses", {}).items():
            _, route, _ = _split_recording_key(key)
            if route not in (f"/api/v0/inventory/{device_id}", f"/api/v0/inventory/{device_id}/all"):
                continue
            body = _unwrap_recorded_response(value)
            items = body.get("inventory") if isinstance(body, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                identity = (
                    item.get("entPhysicalIndex"),
                    item.get("entPhysicalClass"),
                    item.get("entPhysicalName"),
                )
                if identity not in seen_inventory:
                    seen_inventory.add(identity)
                    inventory.append(copy.deepcopy(item))
        self.inventory_by_device[device_id] = inventory

        sensors_body = self._find_response(recording, "/api/v0/resources/sensors")
        sensors = sensors_body.get("sensors") if isinstance(sensors_body, dict) else None
        if not isinstance(sensors, list) and isinstance(sensors_body, dict):
            sensors = sensors_body.get("resources")
        if isinstance(sensors, list):
            self.sensors.extend(copy.deepcopy(item) for item in sensors if isinstance(item, dict))

        self._load_oob_controller(recording, device)

    def _load_oob_controller(self, recording, host_device):
        meta = recording.get("meta")
        oob_id = meta.get("oob_id") if isinstance(meta, dict) else None
        if not isinstance(oob_id, int) or isinstance(oob_id, bool):
            return
        if oob_id in self.devices:
            raise ValueError(f"Duplicate recording device_id {oob_id}")

        ports_body = self._find_response(recording, f"/api/v0/devices/{oob_id}/ports")
        ports = ports_body.get("ports") if isinstance(ports_body, dict) else None
        if not isinstance(ports, list):
            raise ValueError(f"OOB recording for device_id {oob_id} has no ports response")

        host_name = host_device.get("sysName") or host_device["hostname"].split(".", 1)[0]
        controller = self._normalise_device(
            {
                "hostname": f"{host_name}-oob.example.test",
                "sysName": f"{host_name}-oob",
                "ip": f"198.51.100.{oob_id % 254 + 1}",
                "hardware": "OOB Stub Controller",
                "os": "idrac",
                "serial": host_device["serial"],
                "type": "management",
                "location": host_device["location"],
                "location_id": host_device["location_id"],
                "status": 1,
            },
            oob_id,
        )
        self.devices[oob_id] = controller
        self.ports_by_device[oob_id] = copy.deepcopy(ports)
        normalised_ports_body = copy.deepcopy(ports_body)
        normalised_ports_body["status"] = "ok"
        normalised_ports_body["ports"] = copy.deepcopy(ports)
        self.ports_body_by_device[oob_id] = normalised_ports_body
        self.inventory_by_device[oob_id] = []

    def _install_instance_routes(self):
        self._build_locations()
        self._build_vlans()
        self._build_links()

        self.register(
            "/api/v0/system",
            {"status": "ok", "system": [{"version": "development-stub"}]},
            method="GET",
        )
        self.register("/api/v0/devices", self._list_devices, method="GET")
        self.register("/api/v0/devices", self._add_device, method="POST")
        self.register("/api/v0/resources/locations", self._get_locations, method="GET")
        self.register("/api/v0/locations", self._add_location, method="POST")
        self.register(
            "/api/v0/poller_group",
            {
                "status": "ok",
                "get_poller_group": [{"id": 0, "group_name": "default", "descr": "Development stub"}],
            },
            method="GET",
        )
        self.register("/api/v0/resources/sensors", self._get_sensors, method="GET")
        self.register("/api/v0/resources/vlans", self._get_vlans, method="GET")

        for device_id in self.devices:
            self._register_device_routes(device_id)

    def _register_device_routes(self, device_id):
        device = self.devices[device_id]
        alias_keys = {
            str(alias)
            for alias in (device_id, device.get("hostname"), device.get("sysName"), device.get("ip"))
            if alias not in (None, "")
        }
        for alias_key in alias_keys:
            other_id = self._aliases.get(alias_key)
            if other_id is not None and other_id != device_id:
                raise ValueError(f"Duplicate device lookup alias {alias_key!r}")

        old_alias_keys = self._aliases_by_device.get(device_id, set())
        for alias_key in old_alias_keys - alias_keys:
            self._aliases.pop(alias_key, None)
            self.routes.pop(f"GET /api/v0/devices/{alias_key}", None)
            self.routes.pop(f"PATCH /api/v0/devices/{alias_key}", None)

        for alias_key in alias_keys:
            self._aliases[alias_key] = device_id
            self.register(
                f"/api/v0/devices/{alias_key}",
                self._device_info_handler(device_id),
                method="GET",
            )
        self._aliases_by_device[device_id] = alias_keys

        # The plugin patches by whatever identifier it holds, so PATCH must answer on the same
        # aliases the device GET route does, not the numeric id alone.
        for patch_key in (str(device_id), *alias_keys):
            self.register(
                f"/api/v0/devices/{patch_key}",
                self._update_device_handler(device_id),
                method="PATCH",
            )
        self.register(
            f"/api/v0/devices/{device_id}/links",
            self._device_links_handler(device_id),
            method="GET",
        )
        self.register(
            f"/api/v0/devices/{device_id}/ip",
            self._device_ips_handler(device_id),
            method="GET",
        )
        self.register(
            f"/api/v0/devices/{device_id}/ports",
            self.ports_body_by_device.get(
                device_id,
                {"status": "ok", "ports": self.ports_by_device.get(device_id, [])},
            ),
            method="GET",
        )
        self.register(
            f"/api/v0/inventory/{device_id}",
            self._inventory_handler(device_id),
            method="GET",
        )
        self.register(
            f"/api/v0/inventory/{device_id}/all",
            {"status": "ok", "inventory": self.inventory_by_device.get(device_id, [])},
            method="GET",
        )

        for suffix, empty_body in (
            ("port_stack", {"status": "ok", "mappings": []}),
            ("transceivers", {"status": "ok", "transceivers": []}),
        ):
            route = f"/api/v0/devices/{device_id}/{suffix}"
            if f"GET {route}" not in self.routes:
                self.register(route, empty_body, method="GET")

        ports = self.ports_by_device.get(device_id, [])
        for port in ports:
            port_id = port.get("port_id") if isinstance(port, dict) else None
            if port_id is not None:
                self.register(
                    f"/api/v0/ports/{port_id}",
                    {"status": "ok", "port": [port]},
                    method="GET",
                )

    def _device_info_handler(self, device_id):
        def _handler(method, path, query, headers, body):
            with self._lock:
                return 200, {"status": "ok", "devices": [copy.deepcopy(self.devices[device_id])], "count": 1}

        return _handler

    def _list_devices(self, method, path, query, headers, body):
        with self._lock:
            devices = [copy.deepcopy(self.devices[device_id]) for device_id in sorted(self.devices)]

        filter_type = (query.get("type") or [None])[0]
        filter_value = (query.get("query") or [None])[0]
        if filter_type == "up":
            devices = [device for device in devices if device.get("status") == 1]
        elif filter_type == "down":
            devices = [device for device in devices if device.get("status") == 0]
        elif filter_type and filter_value:
            field_by_type = {
                "device_id": "device_id",
                "hostname": "hostname",
                "location_id": "location_id",
                "os": "os",
                "sysName": "sysName",
                "type": "type",
            }
            field = field_by_type.get(filter_type)
            if field:
                needle = str(filter_value).casefold()
                devices = [device for device in devices if needle in str(device.get(field, "")).casefold()]

        return 200, {"status": "ok", "count": len(devices), "devices": devices}

    def _add_device(self, method, path, query, headers, body):
        if not isinstance(body, dict) or not isinstance(body.get("hostname"), str) or not body["hostname"].strip():
            return 422, {"status": "error", "message": "hostname is required"}

        hostname = body["hostname"].strip()
        with self._lock:
            requested_aliases = (hostname, hostname.split(".", 1)[0])
            if any(str(alias) in self._aliases for alias in requested_aliases):
                return 409, {"status": "error", "message": "Device lookup alias already exists"}

            device_id = self._next_device_id
            for _ in range(254):
                device = self._normalise_device(
                    {
                        "hostname": hostname,
                        "sysName": hostname.split(".", 1)[0],
                        "os": "stub-os",
                        "hardware": "MODEL-STUB",
                        "location": "Lab",
                        "status": 1,
                        "type": "network",
                    },
                    device_id,
                )
                generated_aliases = (device_id, device["ip"])
                if not any(str(alias) in self._aliases for alias in generated_aliases):
                    break
                device_id += 1
            else:
                return 409, {"status": "error", "message": "No generated device address is available"}

            self._next_device_id = device_id + 1
            device["location_id"] = self._location_id(device["location"])
            self.devices[device_id] = device
            self.ports_by_device[device_id] = []
            self.inventory_by_device[device_id] = []
            self.links_by_device[device_id] = []
            self._register_device_routes(device_id)
        return 200, {"status": "ok", "message": f"Device added successfully (#{device_id})"}

    def _update_device_handler(self, device_id):
        def _handler(method, path, query, headers, body):
            fields = body.get("field") if isinstance(body, dict) else None
            values = body.get("data") if isinstance(body, dict) else None
            # LibreNMS documents both a scalar pair and equal-length lists; normalise the scalar
            # form so the stub accepts what the real API accepts.
            if isinstance(fields, str) and not isinstance(values, (list, dict)):
                fields, values = [fields], [values]
            if not isinstance(fields, list) or not isinstance(values, list) or len(fields) != len(values):
                return 422, {"status": "error", "message": "field and data must be equal-length lists"}
            if any(not isinstance(field, str) or not field for field in fields):
                return 422, {"status": "error", "message": "field names must be non-empty strings"}
            if "device_id" in fields:
                return 422, {"status": "error", "message": "device_id cannot be updated"}
            with self._lock:
                updates = dict(zip(fields, values, strict=True))
                if "location" in updates and (
                    not isinstance(updates["location"], str) or not updates["location"].strip()
                ):
                    return 422, {"status": "error", "message": "location must be a non-empty string"}
                candidate = {**self.devices[device_id], **updates}
                aliases = (candidate.get("hostname"), candidate.get("sysName"), candidate.get("ip"))
                if any(
                    self._aliases.get(str(alias)) not in (None, device_id)
                    for alias in aliases
                    if alias not in (None, "")
                ):
                    return 409, {"status": "error", "message": "Device lookup alias already exists"}
                if "location" in updates:
                    updates["location_id"] = self._location_id(updates["location"])
                self.devices[device_id].update(updates)
                self._register_device_routes(device_id)
            return 200, {"status": "ok", "message": "Device fields updated"}

        return _handler

    def _device_ips_handler(self, device_id):
        def _handler(method, path, query, headers, body):
            device = self.devices[device_id]
            ports = self.ports_by_device.get(device_id, [])
            first_port = next(
                (port for port in ports if isinstance(port, dict) and port.get("port_id") is not None),
                None,
            )
            addresses = []
            if first_port and device.get("ip"):
                addresses.append(
                    {
                        "ipv4_address": device["ip"],
                        "ipv4_prefixlen": 32,
                        "port_id": first_port["port_id"],
                    }
                )
            return 200, {"status": "ok", "addresses": addresses}

        return _handler

    def _device_links_handler(self, device_id):
        def _handler(method, path, query, headers, body):
            return 200, {"status": "ok", "links": copy.deepcopy(self.links_by_device.get(device_id, []))}

        return _handler

    def _inventory_handler(self, device_id):
        def _handler(method, path, query, headers, body):
            inventory = copy.deepcopy(self.inventory_by_device.get(device_id, []))
            physical_class = (query.get("entPhysicalClass") or [None])[0]
            contained_in = (query.get("entPhysicalContainedIn") or [None])[0]
            if physical_class is not None:
                inventory = [item for item in inventory if item.get("entPhysicalClass") == physical_class]
            if contained_in is not None:
                inventory = [item for item in inventory if str(item.get("entPhysicalContainedIn")) == str(contained_in)]
            return 200, {"status": "ok", "inventory": inventory}

        return _handler

    def _build_locations(self):
        names = []
        for device in self.devices.values():
            name = device.get("location") or "Lab"
            if name not in names:
                names.append(name)
        self.locations = [
            {"id": index, "location": name, "lat": None, "lng": None} for index, name in enumerate(names, start=1)
        ]
        # _normalise_device defaults every unrecorded location_id to 1, so realign each device
        # with the list before the location filter is served.
        for device in self.devices.values():
            device["location_id"] = self._location_id(device.get("location") or "Lab")
        for location in self.locations:
            self._register_location_patch(location["location"])

    def _location_id(self, name):
        """Return the listed id of location *name*, listing the location when it is new."""
        for item in self.locations:
            if item["location"] == name:
                return item["id"]
        location = {
            "id": max((item["id"] for item in self.locations), default=0) + 1,
            "location": name,
            "lat": None,
            "lng": None,
        }
        self.locations.append(location)
        self._register_location_patch(name)
        return location["id"]

    def _get_locations(self, method, path, query, headers, body):
        with self._lock:
            return 200, {"status": "ok", "locations": copy.deepcopy(self.locations)}

    def _add_location(self, method, path, query, headers, body):
        name = body.get("location") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip():
            return 422, {"status": "error", "message": "location is required"}
        name = name.strip()
        with self._lock:
            existing = next((item for item in self.locations if item["location"] == name), None)
            if existing:
                return 409, {"status": "error", "message": "Location already exists"}
            location = {
                "id": max((item["id"] for item in self.locations), default=0) + 1,
                "location": name,
                "lat": body.get("lat"),
                "lng": body.get("lng"),
            }
            self.locations.append(location)
            self._register_location_patch(name)
        return 200, {"status": "ok", "message": f"Location added successfully #{location['id']}"}

    def _register_location_patch(self, location_name):
        self.register(
            f"/api/v0/locations/{quote(location_name, safe='')}",
            self._update_location_handler(location_name),
            method="PATCH",
        )

    def _update_location_handler(self, location_name):
        def _handler(method, path, query, headers, body):
            if not isinstance(body, dict):
                return 422, {"status": "error", "message": "JSON object required"}
            with self._lock:
                location = next((item for item in self.locations if item["location"] == location_name), None)
                if location is None:
                    return 404, {"status": "error", "message": "Location not found"}
                for field in ("lat", "lng"):
                    if field in body:
                        location[field] = body[field]
            return 200, {"status": "ok", "message": "Location updated"}

        return _handler

    def _get_sensors(self, method, path, query, headers, body):
        return 200, {"status": "ok", "sensors": copy.deepcopy(self.sensors)}

    def _build_vlans(self):
        vlans = {}
        for device_id, ports in self.ports_by_device.items():
            for port in ports:
                port_vlans = port.get("vlans") if isinstance(port, dict) else None
                for item in port_vlans if isinstance(port_vlans, list) else []:
                    vlan_number = item.get("vlan") if isinstance(item, dict) else None
                    if vlan_number in (None, ""):
                        continue
                    try:
                        vlan_number = int(vlan_number)
                    except (TypeError, ValueError):
                        continue
                    vlans[(device_id, vlan_number)] = {
                        "device_id": device_id,
                        "vlan_vlan": vlan_number,
                        "vlan_domain": 1,
                        "vlan_name": f"Stub VLAN {vlan_number}",
                        "vlan_type": "ethernet",
                        "vlan_state": 1,
                    }
        self.vlans = [{"vlan_id": index, **vlan} for index, vlan in enumerate(vlans.values(), start=1)]

    def _get_vlans(self, method, path, query, headers, body):
        return 200, {"status": "ok", "vlans": copy.deepcopy(self.vlans)}

    def _build_links(self):
        endpoints = []
        for device_id in sorted(self.devices):
            port = next(
                (
                    item
                    for item in self.ports_by_device.get(device_id, [])
                    if isinstance(item, dict) and item.get("port_id") is not None and item.get("ifName")
                ),
                None,
            )
            if port:
                endpoints.append((device_id, port))

        self.links_by_device = {device_id: [] for device_id in self.devices}
        if len(endpoints) < 2:
            return
        for index, (device_id, local_port) in enumerate(endpoints):
            remote_device_id, remote_port = endpoints[(index + 1) % len(endpoints)]
            self.links_by_device[device_id].append(
                {
                    "local_port_id": local_port["port_id"],
                    "local_port": local_port.get("ifName"),
                    "remote_port_id": remote_port["port_id"],
                    "remote_port": remote_port.get("ifName"),
                    "remote_device_id": remote_device_id,
                    "remote_hostname": self.devices[remote_device_id]["hostname"],
                }
            )


def _recording_variant_handler(path: str, variants: list):
    """Build a route handler that returns only an exact recorded query variant."""

    def _handler(method, path, query, headers, body):
        incoming = {k: tuple(sorted(v if isinstance(v, list) else [v])) for k, v in (query or {}).items()}
        # Require EXACT query equality: a subset/contains match would let a request carrying extra
        # unexpected params still replay a cached response, so a request-shape regression would
        # false-pass instead of failing closed (404).
        for qdict, status, resp_body in variants:
            if incoming == qdict:
                return status, resp_body
        return 404, {"status": "error", "message": f"No recorded variant for {path}?{incoming}"}

    return _handler


@contextmanager
def librenms_mock_server():
    """Context manager that starts and stops a MockLibreNMSServer."""
    server = MockLibreNMSServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def load_stub_recordings(recording_names, recordings_dir=DEFAULT_RECORDINGS_DIR):
    """Load named source-tree recordings for the development stub."""
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording_from_directory

    return [load_recording_from_directory(name, recordings_dir) for name in recording_names]


def main(argv=None):
    """Run the persistent development stub used by the devcontainer service."""
    parser = argparse.ArgumentParser(description="Serve anonymized LibreNMS data-shape recordings over HTTP")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--token", default="dev-stub-token")
    parser.add_argument("--recordings-dir", default=str(DEFAULT_RECORDINGS_DIR))
    parser.add_argument("--recording", action="append", dest="recordings")
    args = parser.parse_args(argv)

    names = args.recordings or list(DEFAULT_STUB_RECORDINGS)
    recordings = load_stub_recordings(names, args.recordings_dir)
    server = LibreNMSStubServer(
        recordings=recordings,
        api_token=args.token,
        host=args.host,
        port=args.port,
        quiet=False,
    )
    print(
        f"LibreNMS development stub listening on {args.host}:{server._server.server_address[1]} "
        f"with recordings: {', '.join(names)}",
        flush=True,
    )
    try:
        server._server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server._server.server_close()


if __name__ == "__main__":
    main()
