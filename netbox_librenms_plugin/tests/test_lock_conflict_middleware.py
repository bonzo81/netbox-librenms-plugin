"""A lock conflict that escapes a plugin view is a visible "try again" answer, never a 500; anything else propagates."""

import ast
import json
import re
from pathlib import Path

import pytest
from dcim.models import Device
from django.urls import resolve, reverse

from netbox_librenms_plugin.middleware import REQUEST_FAILED_EVENT, TRY_AGAIN_MESSAGE, LockConflictMiddleware
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.lock_conflict_helpers import (
    lock_row,
    lock_timeout,
    second_connection,
    wrapped_database_error,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, messages_on
from netbox_librenms_plugin.transactions import CommittedFollowUpError

LOCK_TIMEOUT_MS = 200


@transactional_db_with_all_apps()
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_a_lock_conflict_in_a_view_without_the_runner_is_not_a_500(client, settings, htmx):
    """The preferred-server view locks its Device in its own transaction and does not retry."""
    configure_default_librenms_server(settings)
    device = make_device(f"middleware-preferred-server-{htmx}")
    client.force_login(make_superuser("middleware-preferred-server-user"))
    referer = "http://testserver" + reverse("dcim:device", kwargs={"pk": device.pk})
    headers = {"HTTP_REFERER": referer, **({"HTTP_HX_REQUEST": "true"} if htmx else {})}

    with second_connection() as other:
        lock_row(other, Device, device.pk)
        with lock_timeout(LOCK_TIMEOUT_MS):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:set_preferred_server", kwargs={"pk": device.pk}),
                {"object_type": "device", "server_key": "default"},
                **headers,
            )

    if htmx:
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert json.loads(response["HX-Trigger"]) == {REQUEST_FAILED_EVENT: None}
        assert TRY_AGAIN_MESSAGE in response.content.decode()
    else:
        assert response.status_code == 302
        assert response["Location"] == referer
        assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]


SYNC_SCRIPT = Path(__file__).parents[1] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"


def test_the_sync_forms_script_treats_the_try_again_event_as_a_failure():
    """The script gives the form back only on its failure events; the answer's HX-Trigger must be one of them."""
    match = re.search(r"^const HTMX_FAILURE_EVENTS = (\[[^\]]*\]);$", SYNC_SCRIPT.read_text(), re.MULTILINE)
    assert match, "librenms_sync.js no longer declares HTMX_FAILURE_EVENTS as one array literal"

    assert REQUEST_FAILED_EVENT in ast.literal_eval(match.group(1))


def _process(path, exception):
    """Run the middleware's exception hook for a real request resolved to *path*."""
    request = make_request("post", path=path)
    request.resolver_match = resolve(path)
    return LockConflictMiddleware(lambda _request: None).process_exception(request, exception)


def _plugin_view_path():
    return reverse("plugins:netbox_librenms_plugin:set_preferred_server", kwargs={"pk": 1})


@pytest.mark.django_db
def test_a_conflict_outside_the_plugin_is_left_alone():
    assert _process(reverse("dcim:site_list"), wrapped_database_error("40P01")) is None


@pytest.mark.django_db
def test_a_conflict_in_a_plugin_api_view_is_left_alone():
    """An API client gets NetBox's API error handling, not a toast or a redirect."""
    path = reverse("plugins-api:netbox_librenms_plugin-api:portstacklagpattern-list")

    assert _process(path, wrapped_database_error("40P01")) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(ValueError("not a conflict"), id="value-error"),
        pytest.param(wrapped_database_error("23505"), id="unique-violation"),
        pytest.param(CommittedFollowUpError("follow-up failed"), id="follow-up-without-a-cause"),
    ],
)
def test_an_error_that_is_not_a_conflict_propagates_from_a_plugin_view(exception):
    assert _process(_plugin_view_path(), exception) is None


@pytest.mark.django_db
def test_a_follow_up_failure_whose_cause_is_not_a_conflict_propagates():
    try:
        raise CommittedFollowUpError("follow-up failed") from RuntimeError("callback bug")
    except CommittedFollowUpError as exc:
        error = exc

    assert _process(_plugin_view_path(), error) is None
