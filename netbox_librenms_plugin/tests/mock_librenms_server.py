"""
Minimal HTTP mock for LibreNMS API responses.

Usage in tests: take the ``librenms_server`` fixture from ``conftest.py``, or open the context
manager directly:

    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    with librenms_mock_server() as server:
        ...
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _RawResponse:
    """One response body that the test server must not JSON-encode."""

    def __init__(self, status, body, content_type):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.content_type = content_type


class _LibreNMSHandler(BaseHTTPRequestHandler):
    """Request handler that dispatches to registered route responses."""

    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress request logs in tests

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

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw_body) if raw_body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw_body.decode(errors="replace")

    def do_POST(self):
        self._handle_request("POST", body=self._read_json_body())

    def do_PATCH(self):
        self._handle_request("PATCH", body=self._read_json_body())


class MockLibreNMSServer:
    """Wrap a simple HTTP mock server with route registration and context management."""

    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _LibreNMSHandler)
        # A real LibreNMS always serves /system, and the settings page reads it to report the
        # server version. Seed a default so a test only registers it to assert something
        # specific, and can still override it to simulate an unhealthy server.
        self._server.routes = {
            "/api/v0/system": (200, {"status": "ok", "system": [{"local_ver": "24.1.0"}]}),
        }
        self._server.requests = []
        self.routes = self._server.routes  # expose on wrapper as documented
        self.requests = self._server.requests
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        _, port = self._server.server_address
        self.url = f"http://127.0.0.1:{port}"

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
            method, sep, rest = key.partition(" ")
            if not sep:  # No verb prefix → default to GET on the whole key.
                method, rest = "GET", key
            path, _, query = rest.partition("?")
            # Keep the FULL value set per key (sorted) so a repeated param like ?a=1&a=2 is a
            # distinct shape from ?a=1 — collapsing to v[0] would let them false-match.
            qdict = {k: tuple(sorted(v)) for k, v in parse_qs(query, keep_blank_values=True).items()} if query else {}
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
                status, body = value
            else:
                status, body = 200, value
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
