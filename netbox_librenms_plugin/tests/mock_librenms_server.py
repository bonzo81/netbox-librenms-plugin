"""
Minimal HTTP mock for LibreNMS API responses.

Usage in tests (add to conftest.py or inline):

    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    @pytest.fixture
    def librenms_server():
        with librenms_mock_server() as server:
            yield server
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class _LibreNMSHandler(BaseHTTPRequestHandler):
    """Request handler that dispatches to registered route responses."""

    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress request logs in tests

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_request(self, method, body=None):
        """Dispatch to the registered route for this path, with optional method+query fallback."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query
        routes = self.server.routes  # type: ignore[attr-defined]

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
                if callable(entry):
                    status, resp_body = entry(
                        method=method,
                        path=path,
                        query=parse_qs(query),
                        headers=dict(self.headers),
                        body=body,
                    )
                else:
                    status, resp_body = entry
                self._send_json(status, resp_body)
                return

        self._send_json(404, {"status": "error", "message": f"No mock for {self.path}"})

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            body = raw_body.decode(errors="replace")
        self._handle_request("POST", body=body)


class MockLibreNMSServer:
    """
    Context-manager wrapper around a simple HTTP mock server.

    Attributes:
        url (str): Base URL for the mock server (e.g. "http://127.0.0.1:PORT").
        routes (dict): Mapping of URL path → (status_code, body_dict) or callable.
            Callable routes receive keyword arguments: method, path, query, headers, body
            and must return (status_code, body_dict).
            Routes can also be keyed as "METHOD /path" for method-specific matching,
            or "/path?query" for query-specific matching.
    """

    def __init__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _LibreNMSHandler)
        self._server.routes = {}
        self.routes = self._server.routes  # expose on wrapper as documented
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        _, port = self._server.server_address
        self.url = f"http://127.0.0.1:{port}"

    def register(self, path: str, body, status: int = 200, method: str | None = None):
        """
        Register a mock response for a URL path.

        If *method* is given the route is stored as ``"METHOD /path"`` and only
        matches requests using that HTTP verb.  Omit *method* (or pass ``None``)
        to match any verb on that path.

        *body* may be a ``dict`` (serialised to JSON) or a callable.  When a
        callable is provided it is stored directly and invoked by the handler on
        each matching request; the *status* argument is ignored in that case.
        """
        key = f"{method} {path}" if method else path
        if callable(body):
            self._server.routes[key] = body
        else:
            self._server.routes[key] = (status, body)

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
        )

    def vc_inventory_callable(self, device_id: int, root_items: list, children_by_parent_index: dict):
        """
        Register a callable route for VC detection two-call pattern.

        detect_virtual_chassis_from_inventory() calls get_inventory_filtered() twice:
          1. entPhysicalContainedIn=0 → root items
          2. entPhysicalClass=chassis&entPhysicalContainedIn=<parent_index> → member chassis items

        children_by_parent_index: dict mapping parent index (int) → list of chassis items
        """
        root = root_items
        children = children_by_parent_index

        def _handler(method, path, query, headers, body):
            contained_in = query.get("entPhysicalContainedIn", [None])[0]
            if contained_in == "0":
                return 200, {"status": "ok", "inventory": root}
            if contained_in is not None:
                # Only return chassis children when explicitly requesting chassis class
                phy_class = query.get("entPhysicalClass", [None])[0]
                if phy_class is not None and phy_class != "chassis":
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

        self.routes[f"/api/v0/inventory/{device_id}"] = _handler
        self.routes[f"/api/v0/inventory/{device_id}/all"] = _handler


@contextmanager
def librenms_mock_server():
    """Context manager that starts and stops a MockLibreNMSServer."""
    server = MockLibreNMSServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
