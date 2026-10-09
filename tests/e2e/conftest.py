"""
Fixtures for the end-to-end suite.

The suite is black-box: it drives the NetBox of the compose stack in docker/ through its web UI
and its REST API, and never imports the plugin. setup.sh builds and starts the stack.
"""

import os
import time
from collections.abc import Iterator

import pytest
import requests
from playwright.sync_api import Page

USERNAME = "admin"
PASSWORD = "admin"
REQUEST_TIMEOUT = 30
JOB_END_STATES = ("completed", "errored", "failed")


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
