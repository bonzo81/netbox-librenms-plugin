"""
Fixtures for the end-to-end suite.

The suite is black-box: it drives the NetBox of the compose stack in docker/ through its web UI
and its REST API, and never imports the plugin. setup.sh builds and starts the stack.
"""

import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import requests
from playwright.sync_api import Page

USERNAME = "admin"
PASSWORD = "admin"
# The plugin server key of the stub in docker/plugins.py.
SERVER_KEY = "e2e"
# The stub reports this location for a recording that has none, and the recordings that have one use it too.
SITE_NAME = "Lab"
REQUEST_TIMEOUT = 30
JOB_END_STATES = ("completed", "errored", "failed")
RECORDINGS_DIR = Path(__file__).parents[2] / "netbox_librenms_plugin/data_shapes/recordings"
MAPPINGS_API = "plugins/librenms_plugin"


def recorded(name: str, route: str) -> dict:
    """Return the body that a recording, which the stub also serves, holds for one GET route."""
    body = json.loads((RECORDINGS_DIR / f"{name}.json").read_text())["responses"][f"GET {route}"]
    # A recorded response is either the body or a [status, body] pair.
    return body[1] if isinstance(body, list) else body


class NetBoxAPI:
    """A minimal NetBox REST client for seeding and checking objects."""

    def __init__(self, url: str, session: requests.Session) -> None:
        self.url = url
        self.session = session

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(method, f"{self.url}/api/{path}/", timeout=REQUEST_TIMEOUT, **kwargs)
        if not response.ok:
            raise AssertionError(f"{method} /api/{path}/ returned {response.status_code}: {response.text}")
        return response

    def get(self, path: str) -> dict:
        return self._request("GET", path).json()

    def list(self, path: str, **filters) -> list[dict]:
        body = self._request("GET", path, params={"limit": 1000, **filters}).json()
        assert body["next"] is None, f"/api/{path}/ has more than one page for {filters}"
        return body["results"]

    def create(self, path: str, data: dict) -> dict:
        return self._request("POST", path, json=data).json()

    def update(self, path: str, data: dict) -> dict:
        return self._request("PATCH", path, json=data).json()

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def get_or_create(self, path: str, lookup: dict, data: dict) -> dict:
        """Return the one object that matches *lookup*, and create it from *data* when there is none."""
        found = self.list(path, **lookup)
        assert len(found) <= 1, f"{len(found)} objects in /api/{path}/ match {lookup}"
        return found[0] if found else self.create(path, data)

    def delete_modules(self, device_id: int) -> None:
        """Delete every module of a device. A parent module takes the modules in its bays with it."""
        while modules := self.list("dcim/modules", device_id=device_id):
            self.delete(f"dcim/modules/{modules[0]['id']}")

    def installed_modules(self, device_id: int) -> dict[str, tuple[str, str]]:
        """Return {module bay name: (module type model, serial)} for the modules of a device."""
        return {
            module["module_bay"]["name"]: (module["module_type"]["model"], module["serial"])
            for module in self.list("dcim/modules", device_id=device_id)
        }

    def wait_for_job(self, job_id: int, timeout: float = 180) -> dict:
        """Poll a background job until it ends, and return it."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(f"core/jobs/{job_id}")
            if job["status"]["value"] in JOB_END_STATES:
                return job
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} did not end in {timeout}s: {job['status']['value']}")
            time.sleep(0.5)


@pytest.fixture(scope="session")
def netbox_url() -> str:
    port = os.environ.get("NETBOX_PORT", "").strip() or "8000"
    return (os.environ.get("NETBOX_URL", "").strip() or f"http://127.0.0.1:{port}").rstrip("/")


@pytest.fixture(scope="session")
def netbox_api(netbox_url: str) -> Iterator[NetBoxAPI]:
    session = requests.Session()
    # The suite talks only to the local stack, so a proxy from the environment must not apply.
    session.trust_env = False
    response = session.post(
        f"{netbox_url}/api/users/tokens/provision/",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    token = response.json()
    # NetBox 4.5 added v2 tokens; earlier releases return only a v1 key.
    if token.get("version") == 2:
        authorization = f"Bearer nbt_{token['key']}.{token['token']}"
    else:
        authorization = f"Token {token['key']}"
    session.headers.update({"Authorization": authorization, "Accept": "application/json"})
    api = NetBoxAPI(netbox_url, session)
    try:
        yield api
    finally:
        api.delete(f"users/tokens/{token['id']}")
        session.close()


@pytest.fixture(scope="session")
def placement(netbox_api: NetBoxAPI) -> dict:
    """Return the site and the device role that the seeded and imported devices use."""
    site = netbox_api.get_or_create("dcim/sites", {"name": SITE_NAME}, {"name": SITE_NAME, "slug": "lab"})
    role = netbox_api.get_or_create(
        "dcim/device-roles", {"slug": "e2e-switch"}, {"name": "E2E Switch", "slug": "e2e-switch", "color": "2196f3"}
    )
    return {"site": site, "role": role}


@pytest.fixture(scope="module")
def module_device(netbox_api: NetBoxAPI, placement: dict) -> Iterator[Callable[..., dict]]:
    """
    Return a factory that seeds a device for the module sync tab, linked to one stub device.

    The factory takes the LibreNMS device id, the module bay names of the device type, and
    {LibreNMS model: module bay names of its module type}. Every object has a name of its own,
    and the teardown deletes only what the factory created.
    """
    created: list[str] = []

    def _create(path: str, data: dict) -> dict:
        obj = netbox_api.create(path, data)
        created.append(f"{path}/{obj['id']}")
        return obj

    def factory(librenms_id: int, device_bays: list[str], module_types: dict[str, list[str]]) -> dict:
        run = uuid4().hex[:12]
        manufacturer = _create("dcim/manufacturers", {"name": f"E2E Modules {run}", "slug": f"e2e-modules-{run}"})
        device_type = _create(
            "dcim/device-types",
            {"manufacturer": manufacturer["id"], "model": f"E2E-MODULES-{run}", "slug": f"e2e-modules-{run}"},
        )
        for name in device_bays:
            _create("dcim/module-bay-templates", {"device_type": device_type["id"], "name": name})
        for model, bays in module_types.items():
            module_type = _create("dcim/module-types", {"manufacturer": manufacturer["id"], "model": model})
            for name in bays:
                _create("dcim/module-bay-templates", {"module_type": module_type["id"], "name": name})
            # Scoped to the manufacturer, so it wins over a global mapping for the same model.
            _create(
                f"{MAPPINGS_API}/module-type-mappings",
                {"librenms_model": model, "netbox_module_type": module_type["id"], "manufacturer": manufacturer["id"]},
            )
        return _create(
            "dcim/devices",
            {
                "name": f"e2e-modules-{run}",
                "device_type": device_type["id"],
                "role": placement["role"]["id"],
                "site": placement["site"]["id"],
                "custom_fields": {"librenms_id": {SERVER_KEY: librenms_id}},
            },
        )

    yield factory
    # Devices first, so their modules no longer hold the module types; then the newest objects first.
    for path in sorted(reversed(created), key=lambda path: not path.startswith("dcim/devices/")):
        netbox_api.delete(path)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    # The browser talks only to the local stack, so a proxy from the environment must not apply.
    return {**browser_type_launch_args, "args": [*browser_type_launch_args.get("args", []), "--no-proxy-server"]}


@pytest.fixture
def logged_in_page(page: Page, netbox_url: str) -> Page:
    """Return the browser page, signed in to NetBox as the superuser."""
    page.set_default_timeout(30_000)
    page.goto(f"{netbox_url}/login/")
    page.get_by_label("Username").fill(USERNAME)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url(lambda url: "/login/" not in url)
    return page
