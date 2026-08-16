"""
Coverage tests for views/sync/ (cables, devices, interfaces, ip_addresses, locations, vlans).

Most DB interactions are mocked via MagicMock, but some tests are DB-backed
(``@pytest.mark.django_db``) where exercising the real ORM is clearer than mocking it.
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    make_view,
    message_texts,
    missing_pk,
)
from netbox_librenms_plugin.tests.view_test_helpers import get as _get
from netbox_librenms_plugin.tests.view_test_helpers import post as _post

# The views here are built with real requests and real users, so the whole file needs the DB.
pytestmark = pytest.mark.django_db

_seeded_cache_keys = set()


@pytest.fixture(autouse=True)
def _clear_seeded_cache_keys():
    """Delete the real-cache snapshots seeded by this module's view helpers."""
    yield

    from django.core.cache import cache

    for key in _seeded_cache_keys:
        cache.delete(key)
    _seeded_cache_keys.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(post_data=None, get_data=None, user=None):
    """A real request. POST wins when both are given; a GET-only call builds a GET request."""
    if post_data is None and get_data:
        return make_request("get", get_data, user=user)
    request = make_request("post", post_data or {}, user=user)
    request.GET = get_data or request.GET
    request.htmx = False
    return request


def _denied_response():
    resp = MagicMock()
    resp.status_code = 403
    return resp


# ===========================================================================
# views/sync/cables.py — SyncCablesView
# ===========================================================================


class TestSyncCablesViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404"
        ) as mock_get:
            result = view.post(view.request, pk=1)

        assert result.status_code == 403
        mock_get.assert_not_called()


class TestSyncCablesViewCacheMiss:
    def test_cache_miss_redirects_with_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["1"], "device_selection_1": "1"})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = None
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()
        mock_redirect.assert_called_once()


class TestSyncCablesViewNoSelection:
    def test_no_selection_redirects_with_error(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "99"}]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewSuccessPath:
    def test_valid_cable_created(self):
        """A real Cable is created between two real Interfaces (verified via the interfaces' cable FKs)."""
        from dcim.models import Cable

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        dev_local = make_device("cable-local")
        local = make_interface(dev_local, "Gi0/1")
        dev_remote = make_device("cable-remote")
        remote = make_interface(dev_remote, "Gi0/2")

        view.request = _make_request(post_data={"select": ["port1"], "device_selection_port1": str(dev_local.pk)})
        link_data = {
            "local_port_id": "port1",
            "local_port": "Gi0/1",
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
        }

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev_local,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [link_data]}
            view.post(view.request, pk=dev_local.pk)

        mock_msgs.success.assert_called_once()
        # A real cable now connects the two interfaces.
        local.refresh_from_db()
        remote.refresh_from_db()
        assert local.cable_id is not None
        assert local.cable_id == remote.cable_id
        assert Cable.objects.filter(pk=local.cable_id).exists()

    def test_unrelated_posted_device_cannot_redirect_the_local_termination(self):
        """A forged VC selection must reject the row, not cable the cached page interface."""
        from dcim.models import Cable

        page_device = make_device("cable-page-device")
        unrelated_device = make_device("cable-unrelated-device")
        remote_device = make_device("cable-forged-remote")
        cached_local = make_interface(page_device, "Gi0/1")
        make_interface(unrelated_device, "Gi0/1")
        remote = make_interface(remote_device, "Gi0/2")
        request = _make_request(
            post_data={
                "select": ["port1"],
                "device_selection_port1": str(unrelated_device.pk),
            }
        )
        view = _cables_view(
            request,
            page_device,
            [
                {
                    "local_port_id": "port1",
                    "local_port": "Gi0/1",
                    "netbox_local_interface_id": cached_local.pk,
                    "netbox_remote_interface_id": remote.pk,
                }
            ],
        )

        selected = view.get_selected_interfaces(request, page_device)
        assert selected == [{"device_id": str(unrelated_device.pk), "local_port_id": "port1"}]
        view._initial_device = page_device
        assert view._selected_device_is_in_page_context(unrelated_device.pk) is False

        _post(view, request, pk=page_device.pk)

        cached_local.refresh_from_db()
        remote.refresh_from_db()
        assert cached_local.cable_id is None
        assert remote.cable_id is None
        assert Cable.objects.count() == 0
        assert any(
            "Selected device is not part of this cable-sync page for interfaces: Gi0/1" in text
            for text in message_texts(request, "error")
        )

    def test_missing_interface_on_selected_vc_member_does_not_cable_cached_interface(self):
        """A selected VC member without the port must not cable the page member's cached port."""
        from dcim.models import Cable, VirtualChassis

        vc = VirtualChassis.objects.create(name="cable-vc-missing-port")
        page_device = make_device("cable-vc-page")
        page_device.virtual_chassis = vc
        page_device.vc_position = 1
        page_device.save()
        selected_member = make_device("cable-vc-selected")
        selected_member.virtual_chassis = vc
        selected_member.vc_position = 2
        selected_member.save()
        remote_device = make_device("cable-vc-remote")
        cached_local = make_interface(page_device, "Gi0/1")
        remote = make_interface(remote_device, "Gi0/2")
        request = _make_request(
            post_data={
                "select": ["port1"],
                "device_selection_port1": str(selected_member.pk),
            }
        )
        view = _cables_view(
            request,
            page_device,
            [
                {
                    "local_port_id": "port1",
                    "local_port": "Gi0/1",
                    "netbox_local_interface_id": cached_local.pk,
                    "netbox_remote_interface_id": remote.pk,
                }
            ],
        )

        _post(view, request, pk=page_device.pk)

        cached_local.refresh_from_db()
        assert cached_local.cable_id is None
        assert Cable.objects.count() == 0
        assert any("Gi0/1" in text for text in message_texts(request, "error"))


class TestSyncCablesViewSkipsOOBRows:
    def test_oob_sourced_link_is_never_cabled(self):
        """An OOB-controller cable row (_source="oob") merged into the host list must never create a cable on the host interface — it's context-only (shared-LOM detection), mirroring the interface- and module-sync OOB guards."""
        from dcim.models import Cable

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        dev_local = make_device("oob-cable-local")
        local = make_interface(dev_local, "mgmt0")
        dev_remote = make_device("oob-cable-remote")
        remote = make_interface(dev_remote, "Gi0/2")

        view.request = _make_request(post_data={"select": ["portOOB"]})
        # A fully-resolved OOB row (both NetBox interface ids set) so ONLY the _source guard
        # can stop the cable being created on the host interface.
        link_data = {
            "local_port_id": "portOOB",
            "local_port": "mgmt0",
            "netbox_local_interface_id": local.pk,
            "netbox_remote_interface_id": remote.pk,
            "_source": "oob",
        }

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=dev_local,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [link_data]}
            view.post(view.request, pk=dev_local.pk)

        # No cable was created on the host interface, and it was not reported as a success.
        local.refresh_from_db()
        assert local.cable_id is None
        assert Cable.objects.count() == 0
        mock_msgs.success.assert_not_called()


def _cables_view(request, device, links):
    """The real SyncCablesView with the LibreNMS link snapshot seeded into the real cache."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    view = make_view(SyncCablesView, request)
    view._post_server_key = "default"
    key = view.get_cache_key(device, "links", "default")
    _seeded_cache_keys.add(key)
    cache.set(key, {"links": links})
    return view


class TestSyncCablesViewDuplicateCable:
    def test_duplicate_cable_shows_warning(self):
        from netbox_librenms_plugin.tests.conftest import cable_together

        dev = make_device("cable-dup-local")
        remote = make_device("cable-dup-remote")
        local_iface = make_interface(dev, "Gi0/1")
        remote_iface = make_interface(remote, "Gi0/2")
        cable_together(local_iface, remote_iface)  # already connected
        req = _make_request(post_data={"select": ["port1"]})
        view = _cables_view(
            req,
            dev,
            [
                {
                    "local_port_id": "port1",
                    "local_port": "Gi0/1",
                    "netbox_local_interface_id": local_iface.pk,
                    "netbox_remote_interface_id": remote_iface.pk,
                }
            ],
        )

        _post(view, req, pk=dev.pk)

        assert any("Cable already exists" in t for t in message_texts(req, "warning"))


class TestSyncCablesViewMissingRemote:
    def test_interface_does_not_exist_shows_error(self):
        from dcim.models import Interface

        dev = make_device("cable-missing-local")
        local_iface = make_interface(dev, "Gi0/1")
        gone_pk = missing_pk(Interface)
        req = _make_request(post_data={"select": ["port1"]})
        view = _cables_view(
            req,
            dev,
            [
                {
                    "local_port_id": "port1",
                    "local_port": "Gi0/1",
                    "netbox_local_interface_id": local_iface.pk,
                    "netbox_remote_interface_id": gone_pk,
                }
            ],
        )

        _post(view, req, pk=dev.pk)

        assert any("Remote device or interface not found" in t for t in message_texts(req, "error"))

    def test_a_remote_interface_outside_the_grant_is_reported_missing(self):
        """The remote id comes from the cached LibreNMS row; a constrained grant must not cable it."""
        from dcim.models import Cable, Device, Interface

        dev = make_device("cable-scoped-local")
        remote = make_device("cable-scoped-remote")
        local_iface = make_interface(dev, "Gi0/1")
        remote_iface = make_interface(remote, "Gi0/2")
        user = make_user_with_perms("cable-scoped", [("view", Device), ("add", Cable), ("change", Cable)])
        user = grant(user, "change", Interface, constraints={"device__name": "cable-scoped-local"})
        req = _make_request(post_data={"select": ["port1"]}, user=user)
        view = _cables_view(
            req,
            dev,
            [
                {
                    "local_port_id": "port1",
                    "local_port": "Gi0/1",
                    "netbox_local_interface_id": local_iface.pk,
                    "netbox_remote_interface_id": remote_iface.pk,
                }
            ],
        )

        _post(view, req, pk=dev.pk)

        assert any("Remote device or interface not found" in t for t in message_texts(req, "error"))
        assert not remote_iface.cable_terminations.exists()

    def test_view_only_interface_grant_cannot_create_a_cable(self):
        """Cable creation changes both terminations, so a view-only grant must not resolve them."""
        from dcim.models import Cable, Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        local_device = make_device("cable-view-only-local")
        remote_device = make_device("cable-view-only-remote")
        local_iface = make_interface(local_device, "Gi0/1")
        remote_iface = make_interface(remote_device, "Gi0/2")
        user = make_user_with_perms("cable-view-only", [("view", Interface)])
        request = _make_request(user=user)
        view = SyncCablesView()
        view.setup(request)
        view._post_server_key = "default"

        result = view.handle_cable_creation(
            {
                "local_port": "Gi0/1",
                "netbox_local_interface_id": local_iface.pk,
                "netbox_remote_interface_id": remote_iface.pk,
            },
            {"local_port_id": "1"},
        )

        assert result["status"] == "invalid"
        assert not Cable.objects.filter(terminations__termination_id=local_iface.pk).exists()


class TestSyncCablesViewMissingLinkData:
    def test_no_matching_link_data_reports_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port_unknown"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "other_port"}]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewInvalidLinkData:
    def test_missing_netbox_ids_reports_invalid(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.request = _make_request(post_data={"select": ["port1"]})
        view.get_cache_key = MagicMock(return_value="key")
        view._post_server_key = "default"

        mock_device = MagicMock(pk=1)
        # Missing netbox_local_interface_id
        link_data = {"local_port_id": "port1", "local_port": "Gi0/1", "netbox_remote_interface_id": 20}

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.cables.redirect"),
            patch("netbox_librenms_plugin.views.sync.cables.transaction"),
            patch("netbox_librenms_plugin.views.sync.cables.reverse", return_value="/sync/"),
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [link_data]}
            view.post(view.request, pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncCablesViewHelpers:
    def test_get_selected_interfaces_returns_none_when_empty(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request(post_data={})
        result = view.get_selected_interfaces(req, MagicMock(id=1))
        assert result is None

    def test_get_selected_interfaces_builds_list(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request(post_data={"select": ["port1", "port2"], "device_selection_port1": "5"})
        result = view.get_selected_interfaces(req, MagicMock(id=1))
        assert len(result) == 2
        assert result[0]["local_port_id"] == "port1"
        assert result[0]["device_id"] == "5"
        # port2 defaults to initial_device.id
        assert result[1]["device_id"] == 1

    def test_validate_prerequisites_false_on_no_cache(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            result = view.validate_prerequisites(None, ["some"])
        assert result is False
        mock_msgs.error.assert_called_once()

    def test_validate_prerequisites_false_on_no_selection(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()
        with patch("netbox_librenms_plugin.views.sync.cables.messages"):
            result = view.validate_prerequisites([{"port": "x"}], None)
        assert result is False

    def test_verify_cable_creation_requirements_true(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        data = {"netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}
        assert view.verify_cable_creation_requirements(data) is True

    def test_verify_cable_creation_requirements_false(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        data = {"netbox_local_interface_id": 1}
        assert view.verify_cable_creation_requirements(data) is False

    def test_display_sync_results_all_branches(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request()
        results = {
            "valid": ["Gi0/1"],
            "invalid": ["Gi0/2"],
            "duplicate": ["Gi0/3"],
            "missing_remote": ["Gi0/4"],
        }
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            view.display_sync_results(req, results)
        assert mock_msgs.success.call_count == 1
        assert mock_msgs.error.call_count == 2
        assert mock_msgs.warning.call_count == 1

    def test_display_sync_results_empty(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        req = _make_request()
        results = {"valid": [], "invalid": [], "duplicate": [], "missing_remote": []}
        with patch("netbox_librenms_plugin.views.sync.cables.messages") as mock_msgs:
            view.display_sync_results(req, results)
        mock_msgs.success.assert_not_called()
        mock_msgs.warning.assert_not_called()

    def test_get_cached_links_data_no_data(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        with (
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = None
            result = view.get_cached_links_data(_make_request(), MagicMock())
        assert result is None

    def test_get_cached_links_data_returns_links(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")

        with (
            patch("netbox_librenms_plugin.views.sync.cables.cache") as mock_cache,
            patch.object(
                type(view), "librenms_api", new_callable=lambda: property(lambda s: MagicMock(server_key="default"))
            ),
        ):
            mock_cache.get.return_value = {"links": [{"local_port_id": "p"}]}
            result = view.get_cached_links_data(_make_request(), MagicMock())
        assert result == [{"local_port_id": "p"}]

    def test_check_existing_cable(self):
        from netbox_librenms_plugin.tests.conftest import cable_together

        dev = make_device("cable-check")
        remote = make_device("cable-check-remote")
        local = make_interface(dev, "Gi0/1")
        far = make_interface(remote, "Gi0/2")
        free = make_interface(remote, "Gi0/3")
        view = make_view(_sync_cables_view_class())

        assert view.check_existing_cable(local, free) is False

        cable_together(local, far)

        assert view.check_existing_cable(local, free) is True

    def test_missing_local_interface_is_invalid_not_missing_remote(self):
        """A stale local interface ID must not be reported as missing remote data."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        remote = make_interface(make_device("cable-stale-local-remote"), "Gi0/2")
        view = make_view(SyncCablesView)

        result = view.handle_cable_creation(
            {
                "local_port": "Gi0/1",
                "netbox_local_interface_id": missing_pk(Interface),
                "netbox_remote_interface_id": remote.pk,
            },
            {"local_port_id": "port1"},
        )

        assert result == {"status": "invalid", "interface": "Gi0/1"}


def _sync_cables_view_class():
    from netbox_librenms_plugin.views.sync.cables import SyncCablesView

    return SyncCablesView


class TestSyncCablesViewProcessInterfaceSyncException:
    """Lines 147-149: outer except Exception handler in process_interface_sync."""

    def test_process_single_interface_exception_caught(self):
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        view = object.__new__(SyncCablesView)
        view.request = _make_request()

        interface = {"local_port_id": "port1"}
        cached_links = []

        mock_transaction = MagicMock()
        # Make __exit__ NOT suppress exceptions (return False)
        mock_transaction.atomic.return_value.__exit__.return_value = False

        with (
            patch("netbox_librenms_plugin.views.sync.cables.transaction", mock_transaction),
            patch.object(view, "process_single_interface", side_effect=RuntimeError("test error")),
        ):
            results = view.process_interface_sync([interface], cached_links)

        assert "port1" in results["invalid"]


def _add_device_view(request, *, add_result=(True, "Device added")):
    """The real AddDeviceToLibreNMSView; only the LibreNMS calls are stubbed."""
    from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

    view = make_view(AddDeviceToLibreNMSView, request)
    view._librenms_api.add_device.return_value = add_result
    return view


def _poller_groups(choices):
    return patch("netbox_librenms_plugin.forms._get_librenms_poller_group_choices", return_value=list(choices))


def _snmp_post(prefix, **fields):
    """POST data for the real SNMP form, prefixed the way the template renders it."""
    data = {"object_type": "device", f"{prefix}-snmp_version": fields.pop("snmp_version", "v2c")}
    data.update({f"{prefix}-{k}": v for k, v in fields.items()})
    return data


class TestAddDeviceToLibreNMSViewPermission:
    def test_permission_denied_returns_early(self):
        """The plugin write gate refuses before anything reaches LibreNMS."""
        from dcim.models import Device

        dev = make_device("addsnmp-denied")
        # Holds change_device (so the scoped resolve succeeds) but not the plugin write perm,
        # which is the only way to reach require_all_permissions' denial branch here.
        user = make_user_with_perms("addsnmp-noplugin", [("change", Device)], plugin_write=False)
        req = _make_request(post_data=_snmp_post("v1v2", hostname="r.example.com", community="public"), user=user)
        view = _add_device_view(req)

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        view._librenms_api.add_device.assert_not_called()

    def test_a_user_without_the_change_grant_gets_named_permission_error(self):
        """The permission gate reports the missing change grant before the scoped lookup."""
        from dcim.models import Device

        dev = make_device("addsnmp-viewonly")
        user = make_user_with_perms("addsnmp-viewer", [("view", Device)])
        req = _make_request(post_data=_snmp_post("v1v2", hostname="r.example.com", community="public"), user=user)
        view = _add_device_view(req)

        with _poller_groups([]):
            response = _post(view, req, object_id=dev.pk)

        assert response.status_code == 302
        assert any("dcim.change_device" in text for text in message_texts(req, "error"))
        view._librenms_api.add_device.assert_not_called()

    def test_a_device_outside_the_grant_404s(self):
        """The object_id is client-supplied, so an out-of-scope device must not be added."""
        from dcim.models import Device
        from django.http import Http404

        make_device("addsnmp-mine")
        theirs = make_device("addsnmp-theirs")
        user = make_user_with_perms("addsnmp-scoped", [("change", Device)], constraints={"name": "addsnmp-mine"})
        req = _make_request(post_data=_snmp_post("v1v2", hostname="r.example.com", community="public"), user=user)
        view = _add_device_view(req)

        with _poller_groups([]), pytest.raises(Http404):
            _post(view, req, object_id=theirs.pk)

        view._librenms_api.add_device.assert_not_called()

    def test_unknown_object_type_returns_400(self):
        dev = make_device("addsnmp-badtype")
        req = _make_request(post_data={"object_type": "rack"})
        view = _add_device_view(req)

        response = _post(view, req, object_id=dev.pk)

        assert response.status_code == 400


class TestAddDeviceToLibreNMSViewFormInvalid:
    def test_invalid_form_shows_errors(self):
        """The REAL form rejects the missing hostname/community, and each error is surfaced."""
        dev = make_device("addsnmp-invalid")
        req = _make_request(post_data={"v1v2-snmp_version": "v2c", "object_type": "device"})
        view = _add_device_view(req)

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        errors = message_texts(req, "error")
        assert any(t.startswith("hostname:") for t in errors)
        assert any(t.startswith("community:") for t in errors)
        view._librenms_api.add_device.assert_not_called()


class TestAddDeviceToLibreNMSViewFormValid:
    def test_valid_v2c_form_calls_api(self):
        dev = make_device("addsnmp-v2c")
        req = _make_request(post_data=_snmp_post("v1v2", hostname="router.example.com", community="public"))
        view = _add_device_view(req, add_result=(True, "Device added"))

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        payload = view._librenms_api.add_device.call_args[0][0]
        assert payload["hostname"] == "router.example.com"
        assert payload["snmp_version"] == "v2c"
        assert payload["community"] == "public"
        assert message_texts(req, "success") == ["Device added"]


class TestAddDeviceToLibreNMSViewFormValidExtraFields:
    """Covers lines 74-78: transport and port_association_mode optional fields."""

    def test_valid_form_with_transport_and_pam(self):
        dev = make_device("addsnmp-extra")
        req = _make_request(
            post_data=_snmp_post(
                "v1v2",
                hostname="router.example.com",
                community="public",
                port="161",
                transport="udp6",
                port_association_mode="ifName",
            )
        )
        view = _add_device_view(req)

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        payload = view._librenms_api.add_device.call_args[0][0]
        assert payload["transport"] == "udp6"
        assert payload["port_association_mode"] == "ifName"
        assert payload["port"] == 161

    def test_invalid_poller_group_ignored(self):
        """Covers lines 81-82: a non-numeric poller group id from LibreNMS is dropped, not fatal."""
        dev = make_device("addsnmp-badpoller")
        req = _make_request(
            post_data=_snmp_post("v1v2", hostname="router.example.com", community="public", poller_group="not-a-number")
        )
        view = _add_device_view(req)

        # LibreNMS supplies the option list, so a non-numeric id is a real possibility.
        with _poller_groups([("not-a-number", "Odd group")]):
            _post(view, req, object_id=dev.pk)

        payload = view._librenms_api.add_device.call_args[0][0]
        assert "poller_group" not in payload


class TestAddDeviceToLibreNMSViewV3:
    def test_v3_form_submits_v3_data(self):
        dev = make_device("addsnmp-v3")
        req = _make_request(
            post_data=_snmp_post(
                "v3",
                snmp_version="v3",
                hostname="router.example.com",
                authlevel="authPriv",
                authname="user",
                authpass="auth123",
                authalgo="MD5",
                cryptopass="priv123",
                cryptoalgo="DES",
            )
        )
        view = _add_device_view(req, add_result=(False, "Error"))

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        payload = view._librenms_api.add_device.call_args[0][0]
        assert payload["snmp_version"] == "v3"
        assert payload["authlevel"] == "authPriv"
        assert payload["cryptoalgo"] == "DES"
        assert message_texts(req, "error") == ["Error"]


class TestAddDeviceToLibreNMSViewUnknownVersion:
    def test_unknown_snmp_version_shows_error(self):
        """A version string that is neither v1/v2c nor v3 is refused before reaching LibreNMS.

        ``snmp_version`` on the v3 form is a plain CharField whose ``initial`` does not constrain a
        BOUND form, so a posted "v99" survives validation, reaches form_valid as the version, and
        must hit the guard rather than be sent on.
        """
        dev = make_device("addsnmp-badversion")
        req = _make_request(
            post_data={
                "object_type": "device",
                "v3-snmp_version": "v99",
                "v3-hostname": "router.example.com",
                "v3-authlevel": "noAuthNoPriv",
                "v3-authname": "user",
            }
        )
        view = _add_device_view(req)

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        assert message_texts(req, "error") == ["Unknown SNMP version."]
        view._librenms_api.add_device.assert_not_called()

    def test_no_version_field_at_all_is_a_form_error(self):
        """With no snmp_version posted, the v3 form's required field rejects the submission."""
        dev = make_device("addsnmp-noversion")
        req = _make_request(
            post_data={
                "object_type": "device",
                "v3-hostname": "router.example.com",
                "v3-authlevel": "noAuthNoPriv",
                "v3-authname": "user",
            }
        )
        view = _add_device_view(req)

        with _poller_groups([]):
            _post(view, req, object_id=dev.pk)

        assert any(t.startswith("snmp_version:") for t in message_texts(req, "error"))
        view._librenms_api.add_device.assert_not_called()


class TestAddDeviceToLibreNMSViewGetFormClass:
    def test_v1v2_form_class(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV1V2

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"snmp_version": "v2c"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_v3_form_class(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV3

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"snmp_version": "v3"})
        assert view.get_form_class() is AddToLIbreSNMPV3

    def test_v1v2_via_prefix(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView, AddToLIbreSNMPV1V2

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request(post_data={"v1v2-snmp_version": "v1"})
        assert view.get_form_class() is AddToLIbreSNMPV1V2

    def test_get_object_virtualmachine(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        mock_vm = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_vm,
        ):
            result = view.get_object(5, object_type="virtualmachine")
        assert result is mock_vm

    def test_get_object_returns_none_for_missing_object_type(self):
        """Missing object_type now returns None (caller turns it into HTTP 400)."""
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        assert view.get_object(5) is None
        assert view.get_object(5, object_type="bogus") is None

    def test_get_object_raises_http404_when_device_not_found(self):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)

        import pytest

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            side_effect=Http404,
        ):
            with pytest.raises(Http404):
                view.get_object(5, object_type="device")

    def test_form_valid_with_poller_group(self):
        """poller_group valid int is passed to API."""
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view.request = _make_request()
        mock_api = MagicMock()
        mock_api.add_device.return_value = (True, "ok")
        view.object = MagicMock()
        view.object.get_absolute_url.return_value = "/d/"

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "hostname": "h.example.com",
            "snmp_version": "v2c",
            "community": "public",
            "force_add": False,
            "port": 161,
            "transport": "udp",
            "port_association_mode": None,
            "poller_group": "2",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.devices.messages"),
            patch("netbox_librenms_plugin.views.sync.devices.redirect"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.form_valid(mock_form, snmp_version="v2c")

        call_args = mock_api.add_device.call_args[0][0]
        assert call_args["poller_group"] == 2


class TestUpdateDeviceLocationView:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, pk=1)
        assert result.status_code == 403

    def _view_with_api(self):
        """View with a cached client — the blank-POST rebind reuses it (new contract)."""
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view.require_write_permission = MagicMock(return_value=None)
        mock_api = MagicMock()
        mock_api.server_key = "default"
        view._librenms_api = mock_api
        return view, mock_api

    def test_device_with_site_updates_location(self):
        view, mock_api = self._view_with_api()

        mock_device = MagicMock()
        mock_device.site.name = "London"
        mock_api.get_librenms_id.return_value = 42
        mock_api.update_device_field.return_value = (True, "ok")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices._device_sync_redirect"),
        ):
            view.request = _make_request()
            _post(view, view.request, pk=1)

        mock_api.update_device_field.assert_called_once()
        mock_msgs.success.assert_called_once()

    def test_device_with_site_api_failure(self):
        view, mock_api = self._view_with_api()

        mock_device = MagicMock()
        mock_device.site.name = "London"
        mock_api.get_librenms_id.return_value = 42
        mock_api.update_device_field.return_value = (False, "Connection refused")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices._device_sync_redirect"),
        ):
            view.request = _make_request()
            _post(view, view.request, pk=1)

        mock_msgs.error.assert_called_once()

    def test_device_no_site_shows_warning(self):
        view, _mock_api = self._view_with_api()

        mock_device = MagicMock()
        mock_device.site = None

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.devices._device_sync_redirect"),
        ):
            view.request = _make_request()
            _post(view, view.request, pk=1)

        mock_msgs.warning.assert_called_once()


# ===========================================================================
# views/sync/ip_addresses.py — SyncIPAddressesView
# ===========================================================================


class TestSyncIPAddressesViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, object_type="device", pk=1)
        assert result.status_code == 403


class TestSyncIPAddressesViewUnknownServerKey:
    def test_unknown_server_key_errors_without_500(self):
        """A stale/tampered POST server_key must surface a user-facing error + redirect, not raise (LibreNMSAPI raises KeyError for unknown keys)."""
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            # Drive the REAL failure mode end to end: LibreNMSAPI raises KeyError for an
            # unknown key, and the real build_librenms_api must swallow it into None so
            # the view degrades gracefully. Mocking build_librenms_api=None directly would
            # keep passing even if that try/except regressed — this exercises the swallow.
            patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI", side_effect=KeyError("ghost")) as mock_cls,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request(post_data={"server_key": "ghost", "select": ["10.0.0.1/24"]})
            result = view.post(view.request, object_type="device", pk=1)

        mock_cls.assert_called_once_with(server_key="ghost")
        mock_msgs.error.assert_called_once()
        # Must redirect back to the IP-sync tab (resolved server_key), not just "somewhere".
        mock_redirect.assert_called_once_with("/sync/?tab=ipaddresses&server_key=default")
        assert result is mock_redirect.return_value


class TestSyncIPAddressesViewCacheMiss:
    def test_cache_miss_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.rebind_api_for_server = MagicMock(return_value="default")
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.request = _make_request(post_data={"select": ["192.168.1.1/24"]})
            view.post(view.request, object_type="device", pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncIPAddressesViewNoSelection:
    def test_no_selection_redirects(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.rebind_api_for_server = MagicMock(return_value="default")
        view.get_cache_key = MagicMock(return_value="k")

        mock_device = MagicMock(pk=1)
        mock_api = MagicMock(server_key="default")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.transaction"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": [{"ip_address": "10.0.0.1"}]}
            view.request = _make_request(post_data={})
            view.post(view.request, object_type="device", pk=1)

        mock_msgs.error.assert_called_once()


class TestSyncIPAddressesGetManagementIp:
    """get_management_ip feeds the Primary-IP write decision, so it must read live LibreNMS info."""

    def test_fetches_live_device_info_not_cached(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._librenms_api = MagicMock()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"ip": "10.0.0.9"})

        assert view.get_management_ip(MagicMock()) == "10.0.0.9"
        # Unfixed: get_device_info(42) → use_cache defaults True → a stale render snapshot could
        # pick the wrong management IP as Primary. Fixed: use_cache=False.
        assert view._librenms_api.get_device_info.call_args.kwargs.get("use_cache") is False


class TestSyncIPAddressesViewIPWrites:
    """SyncIPAddressesView create/update/unchanged against real IPAddress rows."""

    def _view(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.rebind_api_for_server = MagicMock(return_value="default")
        view.get_cache_key = MagicMock(return_value="k")
        return view

    def _run(self, view, dev, ip_addresses, *, capture_response=False):
        mock_api = MagicMock(server_key="default")
        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.ip_addresses.redirect"),
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ip_addresses": ip_addresses}
            view.request = _make_request(post_data={"select": ["10.0.0.1/24"]})
            response = view.post(view.request, object_type="device", pk=dev.pk)
        if capture_response:
            return mock_msgs, response
        return mock_msgs

    def test_new_ip_is_created_and_assigned(self):
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        dev = make_device("ip-create")
        eth0 = make_interface(dev, "eth0")
        set_librenms_device_id(eth0, 5, "default")  # port_id 5 → eth0
        eth0.save()

        mock_msgs = self._run(
            view,
            dev,
            [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5, "interface_name": "eth0"}],
        )

        ip = IPAddress.objects.get(address="10.0.0.1/24")
        assert ip.assigned_object_id == eth0.pk  # really created + assigned
        assert ip.status == "active"
        mock_msgs.success.assert_called()

    def test_existing_ip_requires_confirmation_when_interface_differs(self):
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        dev = make_device("ip-update")
        eth0 = make_interface(dev, "eth0")
        set_librenms_device_id(eth0, 5, "default")
        eth0.save()
        eth1 = make_interface(dev, "eth1")
        ip = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=eth1, status="active")

        _mock_msgs, response = self._run(
            view,
            dev,
            [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "port_id": 5, "interface_name": "eth0"}],
            capture_response=True,
        )

        ip.refresh_from_db()
        assert ip.assigned_object_id == eth1.pk
        assert response.status_code == 200
        assert b"10.0.0.1/24" in response.content
        assert b"Reassign the existing IP address to the selected interface." in response.content

    def test_unchanged_ip_shows_warning(self):
        from ipam.models import IPAddress

        view = self._view()
        dev = make_device("ip-unchanged")
        # An unassigned IP with no interface to match → left unchanged (warning), not re-homed.
        ip = IPAddress.objects.create(address="10.0.0.1/24", status="active")

        mock_msgs = self._run(
            view, dev, [{"ip_address": "10.0.0.1", "ip_with_mask": "10.0.0.1/24", "interface_name": None}]
        )

        mock_msgs.warning.assert_called()
        ip.refresh_from_db()
        assert ip.assigned_object_id is None  # untouched


class TestSyncIPAddressesViewHelpers:
    def test_device_sync_declares_interface_view_permission(self):
        from dcim.models import Interface

        view = make_view(_sync_ip_view_class(), _make_request())

        assert ("view", Interface) in view._required_permissions("device")["POST"]

    def test_vm_sync_declares_interface_view_permission(self):
        from virtualization.models import VMInterface

        view = make_view(_sync_ip_view_class(), _make_request())

        assert ("view", VMInterface) in view._required_permissions("virtualmachine")["POST"]

    def test_blank_vrf_selection_does_not_require_vrf_permission(self):
        from ipam.models import VRF

        req = _make_request(post_data={"vrf_10.0.0.1": ""})
        view = make_view(_sync_ip_view_class(), req)

        assert ("view", VRF) not in view._required_permissions("device")["POST"]

    def test_nonblank_vrf_selection_requires_vrf_permission(self):
        from ipam.models import VRF

        req = _make_request(post_data={"vrf_10.0.0.1": "1"})
        view = make_view(_sync_ip_view_class(), req)

        assert ("view", VRF) in view._required_permissions("device")["POST"]

    def test_required_permissions_are_stable_across_repeated_posts(self):
        """Reusing a view instance must not accumulate duplicate owner or VRF permissions."""
        req = _make_request(post_data={"vrf_10.0.0.1": "1"})
        view = make_view(_sync_ip_view_class(), req)

        first = view._required_permissions("device")
        view.required_object_permissions = first
        second = view._required_permissions("device")

        assert second == first

    def test_set_primary_requires_change_scope_on_the_owner(self):
        """The primary-IP toggle must resolve the owner through its change grant."""
        from dcim.models import Device, Interface
        from django.core.cache import cache
        from django.http import Http404
        from ipam.models import IPAddress

        target = make_device("primary-owner-hidden")
        decoy = make_device("primary-owner-allowed")
        user = make_user_with_perms(
            "primary-owner-change-scope",
            [("add", IPAddress), ("change", IPAddress), ("view", Interface)],
        )
        user = grant(user, "view", Device, constraints={"pk": target.pk})
        user = grant(user, "change", Device, constraints={"pk": decoy.pk})
        request = _make_request(
            post_data={"select": ["10.0.0.1"], "set-primary-ip-toggle": "on"},
            user=user,
        )
        view = make_view(_sync_ip_view_class(), request)
        view.rebind_api_for_server = MagicMock(return_value="default")
        view.get_cache_key = MagicMock(return_value="primary-owner-change-scope")
        cache.delete("primary-owner-change-scope")

        with pytest.raises(Http404):
            _post(view, request, object_type="device", pk=target.pk)

    def test_get_object_device(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        mock_device = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_object_virtualmachine(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        mock_vm = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_vm,
        ):
            result = view.get_object("virtualmachine", 1)
        assert result is mock_vm

    def test_get_object_invalid_type_raises(self):
        from django.http import Http404
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        try:
            view.get_object("unknown", 1)
            assert False, "Should have raised Http404"
        except Http404:
            pass

    def test_get_vrf_selection_none_when_no_vrf(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        req = _make_request(post_data={})
        result = view.get_vrf_selection(req, "10.0.0.1")
        assert result is None

    def test_get_vrf_selection_returns_vrf(self):
        from ipam.models import VRF

        vrf = VRF.objects.create(name="Blue")
        req = _make_request(post_data={"vrf_10.0.0.1": str(vrf.pk)})
        view = make_view(_sync_ip_view_class(), req)

        assert view.get_vrf_selection(req, "10.0.0.1") == vrf

    def test_get_vrf_selection_fails_when_requested_vrf_is_missing(self):
        from ipam.models import VRF

        absent_pk = missing_pk(VRF)
        req = _make_request(post_data={"vrf_10.0.0.1": str(absent_pk)})
        view = make_view(_sync_ip_view_class(), req)

        with pytest.raises(ValueError, match="Selected VRF"):
            view.get_vrf_selection(req, "10.0.0.1")

    def test_get_vrf_selection_fails_when_id_is_invalid(self):
        req = _make_request(post_data={"vrf_10.0.0.1": "not-an-id"})
        view = make_view(_sync_ip_view_class(), req)

        with pytest.raises(ValueError, match="Selected VRF"):
            view.get_vrf_selection(req, "10.0.0.1")

    def test_get_vrf_selection_fails_for_a_vrf_outside_the_grant(self):
        """A constrained grant must not turn a requested VRF into global scope."""
        from ipam.models import VRF

        VRF.objects.create(name="Mine")
        theirs = VRF.objects.create(name="Theirs")
        user = make_user_with_perms("vrf-scoped", [("view", VRF)], constraints={"name": "Mine"})
        req = _make_request(post_data={"vrf_10.0.0.1": str(theirs.pk)}, user=user)
        view = make_view(_sync_ip_view_class(), req)

        with pytest.raises(ValueError, match="Selected VRF"):
            view.get_vrf_selection(req, "10.0.0.1")

    def test_out_of_scope_vrf_fails_the_row_without_writing_a_global_ip(self):
        """A restricted requested VRF must not silently create the address without a VRF."""
        from ipam.models import IPAddress, VRF

        from netbox_librenms_plugin.utils import set_librenms_device_id

        mine = VRF.objects.create(name="VRF Mine")
        theirs = VRF.objects.create(name="VRF Theirs")
        user = make_user_with_perms("vrf-row-scoped", [("view", VRF)], constraints={"pk": mine.pk})
        req = _make_request(post_data={"vrf_10.0.0.2/24": str(theirs.pk)}, user=user)
        view = make_view(_sync_ip_view_class(), req)
        view._post_server_key = "default"
        device = make_device("vrf-row-device")
        interface = make_interface(device, "eth0")
        set_librenms_device_id(interface, 5, "default")
        interface.save()
        cached = [{"ip_address": "10.0.0.2", "ip_with_mask": "10.0.0.2/24", "port_id": 5}]

        with patch("netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip", return_value=False):
            results = view.process_ip_sync(req, ["10.0.0.2/24"], cached, device, "device")

        assert results["failed"] == ["10.0.0.2/24"]
        assert "Selected VRF" in results["errors"]["10.0.0.2/24"]
        assert not IPAddress.objects.filter(address="10.0.0.2/24").exists()

    def test_get_ip_tab_url_device(self):
        from dcim.models import Device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = "prod"
        mock_api = MagicMock(server_key="prod")
        mock_device = MagicMock(spec=Device, pk=5)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/device/5/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_device)
        assert "ipaddresses" in url
        assert "server_key=prod" in url

    def test_display_sync_results_all_branches(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        req = _make_request()
        results = {
            "created": ["10.0.0.1"],
            "updated": ["10.0.0.2"],
            "unchanged": ["10.0.0.3"],
            "failed": ["10.0.0.4"],
        }
        with patch("netbox_librenms_plugin.views.sync.ip_addresses.messages") as mock_msgs:
            view.display_sync_results(req, results)
        assert mock_msgs.success.call_count == 2
        assert mock_msgs.warning.call_count == 1
        assert mock_msgs.error.call_count == 1

    def test_get_ip_tab_url_vm(self):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = "default"
        mock_api = MagicMock(server_key="default")
        mock_vm = MagicMock(spec=VirtualMachine, pk=7)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/vm/7/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_vm)
        assert "ipaddresses" in url

    def test_get_ip_tab_url_no_server_key(self):
        from dcim.models import Device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        view = object.__new__(SyncIPAddressesView)
        view._post_server_key = None
        mock_api = MagicMock(server_key=None)
        mock_device = MagicMock(spec=Device, pk=5)

        with (
            patch("netbox_librenms_plugin.views.sync.ip_addresses.reverse", return_value="/device/5/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            url = view.get_ip_tab_url(mock_device)
        assert "server_key" not in url


def _sync_ip_view_class():
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    return SyncIPAddressesView


class TestSyncIPAddressesViewVMInterface:
    def test_vm_interface_resolved(self):
        """A VM IP is bound to the VM's own interface, matched by name."""
        from django.core.cache import cache
        from ipam.models import IPAddress
        from virtualization.models import VMInterface

        vm = make_vm("ipsync-vm")
        vmiface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        req = _make_request(post_data={"select": ["10.0.0.5/24"]})
        view = make_view(_sync_ip_view_class(), req)
        view.rebind_api_for_server = MagicMock(return_value="default")
        cache.set(
            view.get_cache_key(vm, "ip_addresses", "default"),
            {
                "ip_addresses": [
                    {
                        "ip_address": "10.0.0.5",
                        "ip_with_mask": "10.0.0.5/24",
                        "port_id": 9,
                        "interface_name": "eth0",
                    }
                ]
            },
        )

        with patch("netbox_librenms_plugin.views.sync.ip_addresses.get_librenms_device_id", return_value=9):
            _post(view, req, object_type="virtualmachine", pk=vm.pk)

        ip = IPAddress.objects.get(address="10.0.0.5/24")
        assert ip.assigned_object == vmiface


class TestSyncIPAddressesViewInterfaceResolution:
    """The sync re-resolves the target interface against current NetBox state rather than trusting the cached ``interface_url`` (which goes stale when an interface is synced after the IP rows were cached)."""

    def _view(self):
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        return make_view(SyncIPAddressesView)

    def test_match_interface_by_port_id(self):
        dev = make_device("ipres-byid")
        iface = make_interface(dev, "eth0")
        result = self._view()._match_interface(
            {"port_id": 5, "interface_name": "ignored"},
            {"5": iface},
            {},
        )
        assert result == iface

    def test_match_interface_by_name_fallback(self):
        dev = make_device("ipres-byname")
        iface = make_interface(dev, "eth0")
        result = self._view()._match_interface(
            {"port_id": 5, "interface_name": "eth0"},
            {},
            {"eth0": iface},
        )
        assert result == iface

    def test_match_interface_no_match_returns_none(self):
        dev = make_device("ipres-nomatch")
        result = self._view()._match_interface(
            {"port_id": 99, "interface_name": "eth9"},
            {"5": make_interface(dev, "eth5")},
            {"eth0": make_interface(dev, "eth0")},
        )
        assert result is None

    def test_match_interface_handles_missing_keys(self):
        assert self._view()._match_interface({}, {}, {}) is None

    def test_build_interface_maps_indexes_by_id_and_name(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        dev = make_device("ipres-build")
        eth0 = make_interface(dev, "eth0")
        set_librenms_device_id(eth0, 7, "default")  # mutates custom_field_data...
        eth0.save()  # ...which set_librenms_device_id does not persist; _build_interface_maps re-queries

        by_id, by_name, _by_pk = view._build_interface_maps(dev, "default")

        assert set(by_id) == {"7"}
        assert by_id["7"].pk == eth0.pk
        assert set(by_name) == {"eth0"}
        assert by_name["eth0"].pk == eth0.pk

    def test_build_interface_maps_marks_duplicate_port_id_ambiguous(self):
        """Two interfaces sharing the same stored port id must mark that id ambiguous (None) rather than silently keeping the last one — so the IP isn't bound to an arbitrary interface."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        dev = make_device("ipres-dup")
        a = make_interface(dev, "eth0")
        b = make_interface(dev, "eth1")
        set_librenms_device_id(a, 7, "default")
        set_librenms_device_id(b, 7, "default")  # same port id on both
        a.save()
        b.save()

        by_id, by_name, _by_pk = view._build_interface_maps(dev, "default")

        assert by_id == {"7": None}  # ambiguous → no usable target
        assert set(by_name) == {"eth0", "eth1"}

    def test_match_interface_falls_through_to_name_on_ambiguous_port_id(self):
        """An ambiguous port id (None value) falls through to the (unambiguous) name match instead of skipping the row — mirroring the render path (which drops the ambiguous id and links by name). by_name is itself fail-closed (obj's own wins; sibling-only collision maps to None), so this can't bind to an arbitrary interface."""
        dev = make_device("ipres-ambig-name")
        named = make_interface(dev, "eth0")
        result = self._view()._match_interface(
            {"port_id": 7, "interface_name": "eth0"},
            {"7": None},
            {"eth0": named},
        )
        assert result == named

    def test_match_interface_stays_none_when_id_and_name_both_ambiguous(self):
        """With the port id ambiguous AND the name mapping to None (sibling-only collision) and no interface_url, the row still fails closed to None — no arbitrary bind."""
        dev = make_device("ipres-ambig-both")
        make_interface(dev, "eth0")
        result = self._view()._match_interface(
            {"port_id": 7, "interface_name": "eth0"},
            {"7": None},
            {"eth0": None},  # name also ambiguous (sibling-only collision)
        )
        assert result is None

    def test_match_interface_does_not_use_url_after_ambiguous_stable_matches(self):
        """An ambiguous stable ID and name must not fall through to a cached interface URL."""
        dev = make_device("ipres-ambig-url")
        fallback = make_interface(dev, "cached-target")
        result = self._view()._match_interface(
            {
                "port_id": 7,
                "interface_name": "eth0",
                "interface_url": fallback.get_absolute_url(),
            },
            {"7": None},
            {"eth0": None},
            {str(fallback.pk): fallback},
        )
        assert result is None

    def test_existing_ip_is_not_rewritten_without_confirmation(self):
        """A first-pass bulk sync reports a conflict and leaves the existing assignment intact."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        view._post_server_key = "default"

        dev = make_device("iplock-existing")
        iface = make_interface(dev, "eth9")
        set_librenms_device_id(iface, 91, "default")
        iface.save()
        # Pre-existing row for the same address, not yet bound to this interface.
        existing = IPAddress.objects.create(address="10.9.9.9/24")

        ip_data = {
            "ip_address": "10.9.9.9",
            "ip_with_mask": "10.9.9.9/24",
            "interface_url": None,
            "port_id": 91,
            "interface_name": "eth9",
        }
        request = _make_request(post_data={"select": ["10.9.9.9/24"]})
        results = view.process_ip_sync(request, ["10.9.9.9/24"], [ip_data], dev, "device")

        existing.refresh_from_db()
        assert existing.assigned_object is None
        assert results["updated"] == []
        assert [conflict["row_id"] for conflict in results["conflicts"]] == ["10.9.9.9/24"]

    def test_stale_interface_url_still_assigns_after_interface_synced(self):
        """Regression: cached row was enriched before the interface existed (``interface_url`` is None), but the interface has since been synced."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._view()
        view._post_server_key = "default"

        dev = make_device("ipres-stale")
        # Interface synced *after* these rows were cached, carrying port id 5.
        iface = make_interface(dev, "lo0.0")
        set_librenms_device_id(iface, 5, "default")
        iface.save()

        # Stale enriched row: interface_url is None because the interface did
        # not exist in NetBox when the IP data was fetched/cached.
        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": None,
            "port_id": 5,
            "interface_name": "lo0.0",
        }

        request = _make_request(post_data={"select": ["10.0.0.1/24"]})
        results = view.process_ip_sync(request, ["10.0.0.1/24"], [ip_data], dev, "device")

        created = IPAddress.objects.get(address="10.0.0.1/24")
        assert created.assigned_object == iface
        assert results["created"] == ["10.0.0.1/24"]
        assert results["primary_no_interface"] == []

    # --- Virtual-chassis member resolution (characterization: behavior must NOT drift when the
    #     manual member expansion is routed through the shared helper) ---------------------------

    @staticmethod
    def _make_vc(name, positions):
        """Build a real VirtualChassis with one member Device per position. Returns (vc, {pos: dev})."""
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name=name)
        members = {}
        for pos in positions:
            dev = make_device(f"{name}-m{pos}")
            dev.virtual_chassis = vc
            dev.vc_position = pos
            dev.save()
            members[pos] = dev
        vc.master = members[positions[0]]
        vc.save()
        return vc, members

    def test_build_interface_maps_indexes_all_vc_members(self):
        """LibreNMS treats a VC as one logical device, so a member's IP can resolve to an interface on ANOTHER member: _build_interface_maps(viewed_member) must index every member's interfaces, keyed by their stored LibreNMS port id."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        _vc, members = self._make_vc("ipres-vc", [1, 2])
        m1, m2 = members[1], members[2]
        i1 = make_interface(m1, "Ethernet1")
        i2 = make_interface(m2, "Ethernet2")
        set_librenms_device_id(i1, 101, "default")
        set_librenms_device_id(i2, 202, "default")
        i1.save()
        i2.save()

        # Build the maps from the viewed member m1 — m2's interface must still be present.
        by_id, by_name, _by_pk = self._view()._build_interface_maps(m1, "default")

        assert by_id["101"].pk == i1.pk
        assert by_id["202"].pk == i2.pk  # cross-member: resolved from a different member
        assert by_name["Ethernet1"].pk == i1.pk
        assert by_name["Ethernet2"].pk == i2.pk

    def test_ip_sync_skips_vc_interface_outside_the_users_grant(self):
        """A cached sibling port must not bypass a constrained Interface view grant."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        _vc, members = self._make_vc("ipres-vc-scoped", [1, 2])
        visible = make_interface(members[1], "Ethernet1")
        hidden = make_interface(members[2], "Ethernet2")
        set_librenms_device_id(hidden, 202, "default")
        hidden.save()

        user = make_user_with_perms(
            "ipres-vc-scoped",
            [("view", Device), ("add", IPAddress), ("change", IPAddress)],
        )
        user = grant(user, "view", Interface, constraints={"pk": visible.pk})
        request = _make_request(post_data={"select": ["10.0.0.2/24"]}, user=user)
        view = make_view(_sync_ip_view_class(), request)
        view._post_server_key = "default"
        cached = [
            {
                "ip_address": "10.0.0.2",
                "ip_with_mask": "10.0.0.2/24",
                "port_id": 202,
                "interface_name": "Ethernet2",
            }
        ]

        with patch("netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip", return_value=False):
            results = view.process_ip_sync(request, ["10.0.0.2/24"], cached, members[1], "device")

        assert results["skipped_no_interface"] == ["10.0.0.2/24"]
        assert not IPAddress.objects.filter(address="10.0.0.2/24").exists()

    def test_primary_ip_set_from_sibling_vc_member_interface(self):
        """Because _build_interface_maps indexes every VC member, a synced IP can resolve to a SIBLING member's interface — and NetBox's Device.clean() explicitly ACCEPTS a primary IP on any same-VC member's non-mgmt-only interface (vc_interfaces(if_master=False)), so the sync must set it rather than warn 'sync interfaces first' forever."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        _vc, members = self._make_vc("ipres-vc-prim", [1, 2])
        m1, m2 = members[1], members[2]
        # The matching interface lives on the SIBLING member m2, carrying port id 5.
        sibling_iface = make_interface(m2, "eth5")
        set_librenms_device_id(sibling_iface, 5, "default")
        sibling_iface.save()

        view = self._view()
        view._post_server_key = "default"
        # The synced address is LibreNMS's management IP → the set-primary path is taken.
        view.get_management_ip = MagicMock(return_value="10.0.0.1")

        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": None,
            "port_id": 5,
            "interface_name": "eth5",
        }
        request = _make_request(post_data={"select": ["10.0.0.1/24"]})
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip",
            return_value=True,
        ):
            results = view.process_ip_sync(request, ["10.0.0.1/24"], [ip_data], m1, "device")

        # The IP is created and bound to the sibling's interface (the maps include all members)...
        created = IPAddress.objects.get(address="10.0.0.1/24")
        assert created.assigned_object == sibling_iface
        # ...and m1's primary IP IS set — the exact assignment NetBox accepts manually.
        m1.refresh_from_db()
        assert m1.primary_ip4_id == created.pk
        assert results["primary_set"] == ["10.0.0.1/24"]
        assert results["primary_no_interface"] == []
        # Proof the persisted state satisfies NetBox's own invariants.
        m1.full_clean()

    def test_primary_ip_not_set_from_sibling_mgmt_only_interface(self):
        """A sibling member's mgmt_only interface is excluded from vc_interfaces(if_master=False), so NetBox would REJECT it as primary — the sync must keep refusing exactly that case."""
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import set_librenms_device_id

        _vc, members = self._make_vc("ipres-vc-mgmt", [1, 2])
        m1, m2 = members[1], members[2]
        sibling_iface = Interface.objects.create(device=m2, name="mgmt0", type="other", mgmt_only=True)
        set_librenms_device_id(sibling_iface, 5, "default")
        sibling_iface.save()

        view = self._view()
        view._post_server_key = "default"
        view.get_management_ip = MagicMock(return_value="10.0.0.2")

        ip_data = {
            "ip_address": "10.0.0.2",
            "ip_with_mask": "10.0.0.2/24",
            "interface_url": None,
            "port_id": 5,
            "interface_name": "mgmt0",
        }
        request = _make_request(post_data={"select": ["10.0.0.2/24"]})
        with patch(
            "netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip",
            return_value=True,
        ):
            results = view.process_ip_sync(request, ["10.0.0.2/24"], [ip_data], m1, "device")

        created = IPAddress.objects.get(address="10.0.0.2/24")
        assert created.assigned_object == sibling_iface
        m1.refresh_from_db()
        assert m1.primary_ip4_id is None
        assert results["primary_set"] == []
        assert results["primary_interface_not_eligible"] == ["10.0.0.2/24"]

        view.display_sync_results(request, results)
        warnings = message_texts(request, "warning")
        assert any("matched interface is not eligible" in text for text in warnings)
        assert all("Sync interfaces first" not in text for text in warnings)

    def test_primary_ip_row_locks_the_device_before_writing_the_address(self):
        """The primary-IP row writes the address and then the device (primary_ip4), while the migrate move views lock Device then IPAddress — so this path must take the device row lock FIRST or the two orders close a deadlock cycle."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = make_device("ipres-lockorder")
        iface = make_interface(dev, "eth0")
        set_librenms_device_id(iface, 5, "default")
        iface.save()

        view = self._view()
        view._post_server_key = "default"
        view.get_management_ip = MagicMock(return_value="10.0.0.1")

        ip_data = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/24",
            "interface_url": None,
            "port_id": 5,
            "interface_name": "eth0",
        }
        request = _make_request(post_data={"select": ["10.0.0.1/24"]})
        with (
            patch(
                "netbox_librenms_plugin.views.sync.ip_addresses.resolve_set_primary_ip",
                return_value=True,
            ),
            CaptureQueriesContext(connection) as ctx,
        ):
            results = view.process_ip_sync(request, ["10.0.0.1/24"], [ip_data], dev, "device")

        assert results["primary_set"] == ["10.0.0.1/24"]
        sqls = [q["sql"] for q in ctx.captured_queries]
        device_locks = [i for i, sql in enumerate(sqls) if 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql]
        address_writes = [i for i, sql in enumerate(sqls) if 'INSERT INTO "ipam_ipaddress"' in sql]
        assert device_locks, "the primary-IP row must lock the device row (SELECT ... FOR UPDATE)"
        assert address_writes, "the address must be written"
        assert min(device_locks) < min(address_writes), "the device lock must precede the address write"

    def test_build_interface_maps_vc_shared_name_prefers_viewed_member(self):
        """A name shared across VC members resolves to the VIEWED member's own interface (matching the rendered table), not ambiguous."""
        _vc, members = self._make_vc("ipres-vcdup", [1, 2])
        own = make_interface(members[1], "Ethernet1")
        make_interface(members[2], "Ethernet1")  # sibling reuses the name — must NOT block binding

        _by_id, by_name, _by_pk = self._view()._build_interface_maps(members[1], "default")

        # The render indexes only the viewed object's interfaces, so the sync must agree:
        # the viewed member's own Ethernet1 wins rather than being nulled as ambiguous.
        assert by_name["Ethernet1"].pk == own.pk

    def test_build_interface_maps_sibling_only_shared_name_is_ambiguous(self):
        """A name owned only by sibling members (none on the viewed one) stays ambiguous (None) — fail safe."""
        _vc, members = self._make_vc("ipres-vcsib", [1, 2, 3])
        # members[1] (the viewed member) has NO Ethernet9; two siblings share it → can't pick one.
        make_interface(members[2], "Ethernet9")
        make_interface(members[3], "Ethernet9")

        _by_id, by_name, _by_pk = self._view()._build_interface_maps(members[1], "default")

        assert by_name["Ethernet9"] is None

    def test_build_interface_maps_non_vc_device_only_its_own_interfaces(self):
        """A standalone (non-VC) device must index only its own interfaces — not another device's same-named interface."""
        dev = make_device("ipres-standalone")
        other = make_device("ipres-other")
        own = make_interface(dev, "Ethernet1")
        make_interface(other, "Ethernet1")  # unrelated device, must NOT appear

        _by_id, by_name, _by_pk = self._view()._build_interface_maps(dev, "default")

        assert set(by_name) == {"Ethernet1"}
        assert by_name["Ethernet1"].pk == own.pk


# ===========================================================================
# views/sync/vlans.py — SyncVLANsView
# ===========================================================================


class TestSyncVLANsViewPermissionDenied:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.request = _make_request()

        result = view.post(view.request, object_type="device", object_id=1)
        assert result.status_code == 403


class TestSyncVLANsViewInvalidAction:
    def test_invalid_action_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = _make_request(post_data={"action": "bad_action"})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewNoSelection:
    def test_no_selection_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"some": "data"}
            view.request = _make_request(post_data={"action": "create_vlans"})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewCacheMiss:
    def test_cache_miss_shows_error(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.transaction"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=1)

        mock_msgs.error.assert_called_once()


class TestSyncVLANsViewCreateVLAN:
    """SyncVLANsView create path against a real VLAN row (only the cached LibreNMS data and the messages/redirect/reverse framework seams are mocked; the real get_or_create + transaction run and the created VLAN is reloaded from the DB)."""

    def _run(self, vlans):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")
        dev = make_device("vlan-sync-dev")

        # Leave reverse() UNPATCHED so the success path exercises the real plugin route contract
        # (a typo'd/invalid URL name would raise NoReverseMatch here instead of passing silently).
        with (
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect") as mock_redirect,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = vlans
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=dev.pk)

        # The real reverse() resolved and the redirect targets the VLAN tab on the active server.
        mock_redirect.assert_called_once()
        redirect_url = mock_redirect.call_args.args[0]
        assert redirect_url.endswith("?tab=vlans&server_key=default")
        return mock_msgs

    def test_new_vlan_created(self):
        from ipam.models import VLAN

        mock_msgs = self._run([{"vlan_vlan": 100, "vlan_name": "Management"}])
        vlan = VLAN.objects.get(vid=100, group=None)
        assert vlan.name == "Management"
        assert vlan.status == "active"
        mock_msgs.success.assert_called_once()


class TestSyncVLANsViewUpdateVLAN:
    def test_existing_vlan_name_updated(self):
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")
        dev = make_device("vlan-update-dev")
        VLAN.objects.create(vid=100, name="OldName", status="active")

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = [{"vlan_vlan": 100, "vlan_name": "Management"}]
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=dev.pk)

        assert VLAN.objects.get(vid=100, group=None).name == "Management"  # renamed in place


class TestSyncVLANsViewUnchangedVLAN:
    def test_unchanged_vlan_counts_as_skipped(self):
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._post_server_key = "default"
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="default")
        dev = make_device("vlan-unchanged-dev")
        VLAN.objects.create(vid=100, name="Management", status="active")  # already matches

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.vlans.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.vlans.redirect"),
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = [{"vlan_vlan": 100, "vlan_name": "Management"}]
            view.request = _make_request(post_data={"action": "create_vlans", "select": ["100"]})
            view.post(view.request, object_type="device", object_id=dev.pk)

        mock_msgs.success.assert_called()
        assert "unchanged" in str(mock_msgs.success.call_args_list)
        assert VLAN.objects.filter(vid=100).count() == 1  # no duplicate created


def _vlan_view(request, device, cached_vlans):
    """The real SyncVLANsView with the LibreNMS VLAN snapshot seeded into the real cache."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    view = make_view(SyncVLANsView, request)
    view._post_server_key = "default"
    key = view.get_cache_key(device, "vlans", "default")
    _seeded_cache_keys.add(key)
    cache.set(key, cached_vlans)
    return view


def _vlan_group(name):
    from ipam.models import VLANGroup

    return VLANGroup.objects.create(name=name, slug=name.lower().replace(" ", "-"))


class TestSyncVLANsViewAddConstraints:
    def test_created_vlan_outside_add_grant_is_rolled_back(self):
        from dcim.models import Device
        from ipam.models import VLAN

        dev = make_device("vlan-add-scope")
        user = make_user_with_perms(
            "vlan-add-scope",
            [("view", Device), ("change", VLAN)],
        )
        user = grant(user, "add", VLAN, constraints={"vid": 200})
        req = _make_request(
            post_data={"action": "create_vlans", "select": ["201"]},
            user=user,
        )
        view = _vlan_view(req, dev, [{"vlan_vlan": 201, "vlan_name": "Outside add scope"}])

        response = _post(view, req, object_type="device", object_id=dev.pk)

        assert response.status_code == 302
        assert not VLAN.objects.filter(vid=201).exists()
        assert any("add permission" in text.lower() for text in message_texts(req, "error"))


class TestSyncVLANsViewWithGroup:
    def test_vlan_created_in_group(self):
        from ipam.models import VLAN

        dev = make_device("vlan-grp-create")
        group = _vlan_group("Prod Group")
        req = _make_request(post_data={"action": "create_vlans", "select": ["200"], "vlan_group_200": str(group.pk)})
        view = _vlan_view(req, dev, [{"vlan_vlan": 200, "vlan_name": "Production"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        vlan = VLAN.objects.get(vid=200, group=group)
        assert vlan.name == "Production"
        assert vlan.status == "active"

    def test_leading_zero_selection_uses_the_canonical_cached_vid(self):
        """A canonical group field must sync a leading-zero selection."""
        from ipam.models import VLAN

        dev = make_device("vlan-group-canonical")
        group = _vlan_group("Canonical Group")
        req = _make_request(post_data={"action": "create_vlans", "select": ["0200"], "vlan_group_200": str(group.pk)})
        view = _vlan_view(req, dev, [{"vlan_vlan": 200, "vlan_name": "Canonical"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert VLAN.objects.filter(vid=200, group=group, name="Canonical").exists()

    def test_invalid_vlan_group_id_is_rejected(self):
        """A requested-but-missing VLAN group fails closed: no VLAN is created (not even a global one) and an error is surfaced."""
        from ipam.models import VLAN, VLANGroup

        dev = make_device("vlan-grp-missing")
        absent_pk = missing_pk(VLANGroup)
        req = _make_request(post_data={"action": "create_vlans", "select": ["200"], "vlan_group_200": str(absent_pk)})
        view = _vlan_view(req, dev, [{"vlan_vlan": 200, "vlan_name": "Production"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        # Fail closed: never create a VLAN in any scope, and surface an error.
        assert not VLAN.objects.filter(vid=200).exists()
        assert any("no longer exists" in t for t in message_texts(req, "error"))

    def test_a_vlan_group_outside_the_grant_is_rejected(self):
        """The group id is posted by the client, so a constrained grant must not widen the scope."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        dev = make_device("vlan-grp-scoped")
        mine = _vlan_group("Scoped Mine")
        theirs = _vlan_group("Scoped Theirs")
        user = make_user_with_perms(
            "vlan-scoped",
            [("view", Device), ("add", VLAN), ("change", VLAN)],
        )
        user = grant(user, "view", VLANGroup, constraints={"name": mine.name})
        req = _make_request(
            post_data={"action": "create_vlans", "select": ["200"], "vlan_group_200": str(theirs.pk)},
            user=user,
        )
        view = _vlan_view(req, dev, [{"vlan_vlan": 200, "vlan_name": "Production"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert not VLAN.objects.filter(vid=200).exists()
        assert any("no longer exists" in t for t in message_texts(req, "error"))

    def test_existing_vlan_outside_change_grant_is_not_renamed(self):
        """A matching hidden VLAN must not be changed through the unrestricted manager."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        dev = make_device("vlan-hidden-existing")
        visible = VLAN.objects.create(vid=300, name="Visible", status="active")
        hidden = VLAN.objects.create(vid=301, name="Keep this name", status="active")
        user = make_user_with_perms(
            "vlan-hidden-existing",
            [("view", Device), ("view", VLANGroup), ("add", VLAN)],
        )
        user = grant(user, "change", VLAN, constraints={"pk": visible.pk})
        req = _make_request(
            post_data={"action": "create_vlans", "select": ["301"]},
            user=user,
        )
        view = _vlan_view(req, dev, [{"vlan_vlan": 301, "vlan_name": "Unauthorized rename"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        hidden.refresh_from_db()
        assert hidden.name == "Keep this name"
        assert any("permission" in text.lower() for text in message_texts(req, "error"))

    def test_duplicate_global_vid_is_skipped_without_aborting_the_batch(self):
        """An ambiguous global VID must not pick one row or roll back later valid rows."""
        from ipam.models import VLAN

        dev = make_device("vlan-duplicate-global")
        VLAN.objects.create(vid=300, name="First", status="active")
        VLAN.objects.create(vid=300, name="Second", status="active")
        req = _make_request(post_data={"action": "create_vlans", "select": ["300", "301"]})
        view = _vlan_view(
            req,
            dev,
            [
                {"vlan_vlan": 300, "vlan_name": "Ambiguous rename"},
                {"vlan_vlan": 301, "vlan_name": "Unambiguous"},
            ],
        )

        _post(view, req, object_type="device", object_id=dev.pk)

        assert set(VLAN.objects.filter(vid=300).values_list("name", flat=True)) == {"First", "Second"}
        assert VLAN.objects.filter(vid=301, name="Unambiguous").exists()
        assert any("several VLANs" in text for text in message_texts(req, "error"))
        assert any("1 skipped (VLAN match ambiguous)" in text for text in message_texts(req, "success"))

    def test_duplicate_global_vid_is_ambiguous_before_change_scope_is_applied(self):
        """A constrained grant must not make an ambiguous VID look unique."""
        from dcim.models import Device
        from ipam.models import VLAN, VLANGroup

        dev = make_device("vlan-duplicate-scoped")
        permitted = VLAN.objects.create(vid=302, name="Permitted", status="active")
        hidden = VLAN.objects.create(vid=302, name="Hidden", status="active")
        user = make_user_with_perms(
            "vlan-duplicate-scoped",
            [("view", Device), ("view", VLANGroup), ("add", VLAN)],
        )
        user = grant(user, "change", VLAN, constraints={"pk": permitted.pk})
        req = _make_request(post_data={"action": "create_vlans", "select": ["302"]}, user=user)
        view = _vlan_view(req, dev, [{"vlan_vlan": 302, "vlan_name": "Ambiguous rename"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        permitted.refresh_from_db()
        hidden.refresh_from_db()
        assert permitted.name == "Permitted"
        assert hidden.name == "Hidden"
        assert any("several VLANs" in text for text in message_texts(req, "error"))

    def test_invalid_vid_string_skipped(self):
        """A non-numeric selection is skipped, and the rest of the batch still syncs.

        The batch carries a valid VID after the bad one so a `break` in place of the
        `continue` would be caught — a single-item batch cannot tell them apart.
        """
        from ipam.models import VLAN

        dev = make_device("vlan-badvid")
        req = _make_request(post_data={"action": "create_vlans", "select": ["not-a-vid", "100"]})
        view = _vlan_view(req, dev, [{"vlan_vlan": 100, "vlan_name": "Mgmt"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert VLAN.objects.filter(vid=100, name="Mgmt").exists()
        assert VLAN.objects.count() == 1

    def test_unknown_vid_in_cache_skipped(self):
        """A VID absent from the cached snapshot is skipped, and the rest of the batch syncs."""
        from ipam.models import VLAN

        dev = make_device("vlan-unknownvid")
        req = _make_request(post_data={"action": "create_vlans", "select": ["999", "100"]})
        view = _vlan_view(req, dev, [{"vlan_vlan": 100, "vlan_name": "Mgmt"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert not VLAN.objects.filter(vid=999).exists()
        assert VLAN.objects.filter(vid=100, name="Mgmt").exists()

    def test_invalid_vlan_name_does_not_abort_the_batch(self):
        """An invalid LibreNMS name is skipped while the next valid VLAN is created."""
        from ipam.models import VLAN

        dev = make_device("vlan-invalid-name")
        max_length = VLAN._meta.get_field("name").max_length
        invalid_name = "x" * (max_length + 1)
        req = _make_request(post_data={"action": "create_vlans", "select": ["400", "401"]})
        view = _vlan_view(
            req,
            dev,
            [
                {"vlan_vlan": 400, "vlan_name": invalid_name},
                {"vlan_vlan": 401, "vlan_name": "Valid name"},
            ],
        )

        _post(view, req, object_type="device", object_id=dev.pk)

        assert not VLAN.objects.filter(vid=400).exists()
        assert VLAN.objects.filter(vid=401, name="Valid name").exists()
        assert any("name is invalid" in text for text in message_texts(req, "error"))
        assert any("1 skipped (invalid VLAN name)" in text for text in message_texts(req, "success"))

    def test_invalid_vlan_vid_does_not_abort_the_batch(self):
        """An out-of-range LibreNMS VID is skipped while the next valid VLAN is created."""
        from ipam.models import VLAN

        dev = make_device("vlan-invalid-vid")
        req = _make_request(post_data={"action": "create_vlans", "select": ["0", "401"]})
        view = _vlan_view(
            req,
            dev,
            [
                {"vlan_vlan": 0, "vlan_name": "Invalid VID"},
                {"vlan_vlan": 401, "vlan_name": "Valid VID"},
            ],
        )

        _post(view, req, object_type="device", object_id=dev.pk)

        assert not VLAN.objects.filter(vid=0).exists()
        assert VLAN.objects.filter(vid=401, name="Valid VID").exists()
        assert any("VID is invalid" in text for text in message_texts(req, "error"))
        assert any("1 skipped (invalid VLAN VID)" in text for text in message_texts(req, "success"))


class TestSyncVLANsViewGroupedUpdateSkip:
    """Lines 134-139: grouped VLAN update (elif) and unchanged (else) paths."""

    def test_grouped_vlan_name_updated(self):
        from ipam.models import VLAN

        dev = make_device("vlan-grp-update")
        group = _vlan_group("Update Group")
        vlan = VLAN.objects.create(vid=300, group=group, name="OldGroupedName", status="active")
        req = _make_request(post_data={"action": "create_vlans", "select": ["300"], "vlan_group_300": str(group.pk)})
        view = _vlan_view(req, dev, [{"vlan_vlan": 300, "vlan_name": "NewGroupedName"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert VLAN.objects.get(pk=vlan.pk).name == "NewGroupedName"

    def test_grouped_vlan_unchanged_skipped(self):
        from ipam.models import VLAN

        dev = make_device("vlan-grp-same")
        group = _vlan_group("Same Group")
        vlan = VLAN.objects.create(vid=300, group=group, name="SameName", status="active")
        last_updated = VLAN.objects.get(pk=vlan.pk).last_updated
        req = _make_request(post_data={"action": "create_vlans", "select": ["300"], "vlan_group_300": str(group.pk)})
        view = _vlan_view(req, dev, [{"vlan_vlan": 300, "vlan_name": "SameName"}])

        _post(view, req, object_type="device", object_id=dev.pk)

        assert VLAN.objects.get(pk=vlan.pk).last_updated == last_updated
        assert any("unchanged" in t for t in message_texts(req, "success"))

    def test_get_object_device(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        mock_device = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_object_invalid_raises(self):
        from django.http import Http404
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        try:
            view.get_object("vm_type", 1)
            assert False, "Should have raised Http404"
        except Http404:
            pass

    def test_redirect_with_server_key(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        view = object.__new__(SyncVLANsView)
        view._post_server_key = "production"
        mock_api = MagicMock(server_key="production")

        with (
            patch("netbox_librenms_plugin.views.sync.vlans.reverse", return_value="/device/1/sync/"),
            patch("netbox_librenms_plugin.views.sync.vlans.redirect") as mock_redirect,
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view._redirect("device", 1)

        call_url = mock_redirect.call_args[0][0]
        assert "server_key=production" in call_url


# ===========================================================================
# views/sync/locations.py — SyncSiteLocationView
# ===========================================================================


def _make_site(name, *, latitude=None, longitude=None):
    """A real Site, optionally with coordinates.

    Re-read from the DB so the coordinate fields come back as the Decimals the view actually
    formats in production, not the Python floats that were passed in.
    """
    from dcim.models import Site
    from django.utils.text import slugify

    site = Site.objects.create(name=name, slug=slugify(name), latitude=latitude, longitude=longitude)
    return Site.objects.get(pk=site.pk)


def _location_view(request=None, *, locations=None, add_result=None, update_result=None):
    """The real SyncSiteLocationView with only the LibreNMS location calls stubbed."""
    from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

    view = make_view(SyncSiteLocationView, request)
    view._librenms_api.get_locations.return_value = (True, locations if locations is not None else [])
    view._librenms_api.add_location.return_value = add_result or (True, "ok")
    view._librenms_api.update_location.return_value = update_result or (True, "ok")
    return view


class TestSyncSiteLocationViewPost:
    def test_permission_denied_returns_early(self):
        """Without the plugin write permission the LibreNMS API is never called."""
        site = _make_site("Perm Site", latitude=51.5, longitude=-0.12)
        user = make_user_with_perms("loc-noplugin", [], plugin_write=False)
        req = _make_request(post_data={"action": "create", "pk": str(site.pk)}, user=user)
        view = _location_view(req)

        _post(view, req)

        view._librenms_api.add_location.assert_not_called()

    def test_site_view_permission_is_required_before_post_processing(self):
        """Plugin write permission alone must not authorize reading the posted Site."""
        site = _make_site("No Site Read", latitude=1.0, longitude=1.0)
        user = make_user_with_perms("loc-no-site-read", [])
        req = _make_request(post_data={"action": "create", "pk": str(site.pk)}, user=user)
        view = _location_view(req)

        response = _post(view, req)

        assert response.status_code == 302
        assert any("dcim.view_site" in text for text in message_texts(req, "error"))
        view._librenms_api.add_location.assert_not_called()

    def test_missing_pk_shows_error(self):
        req = _make_request(post_data={"action": "create"})
        view = _location_view(req)

        _post(view, req)

        assert message_texts(req, "error") == ["No site ID provided."]

    def test_site_not_found_shows_error(self):
        from dcim.models import Site

        absent_pk = missing_pk(Site)
        req = _make_request(post_data={"action": "create", "pk": str(absent_pk)})
        view = _location_view(req)

        _post(view, req)

        assert message_texts(req, "error") == ["Site not found."]

    def test_site_outside_the_grant_is_reported_as_not_found(self):
        """The pk is posted by the client, so an out-of-scope site must not reach LibreNMS."""
        from dcim.models import Site

        _make_site("Loc Mine", latitude=1.0, longitude=1.0)
        theirs = _make_site("Loc Theirs", latitude=2.0, longitude=2.0)
        user = make_user_with_perms("loc-scoped", [("view", Site)], constraints={"name": "Loc Mine"})
        req = _make_request(post_data={"action": "create", "pk": str(theirs.pk)}, user=user)
        view = _location_view(req)

        _post(view, req)

        assert message_texts(req, "error") == ["Site not found."]
        view._librenms_api.add_location.assert_not_called()

    def test_unknown_action_shows_error(self):
        site = _make_site("Action Site")
        req = _make_request(post_data={"action": "banana", "pk": str(site.pk)})
        view = _location_view(req)

        _post(view, req)

        assert message_texts(req, "error") == ["Unknown action 'banana'."]


class TestSyncSiteLocationViewCreate:
    def test_create_without_coords_shows_warning(self):
        site = _make_site("London")
        req = _make_request(post_data={"action": "create", "pk": str(site.pk)})
        view = _location_view(req)

        _post(view, req)

        assert any("Latitude and/or longitude is missing" in t for t in message_texts(req, "warning"))
        view._librenms_api.add_location.assert_not_called()

    def test_create_success(self):
        site = _make_site("London", latitude=51.5, longitude=-0.12)
        req = _make_request(post_data={"action": "create", "pk": str(site.pk)})
        view = _location_view(req, add_result=(True, "ok"))

        _post(view, req)

        assert message_texts(req, "success") == ["Location 'London' created in LibreNMS successfully."]
        # The site's real coordinates are what gets sent, stringified.
        view._librenms_api.add_location.assert_called_once_with(
            {"lat": "51.500000", "lng": "-0.120000", "location": "London"}
        )

    def test_create_failure(self):
        site = _make_site("London", latitude=51.5, longitude=-0.12)
        req = _make_request(post_data={"action": "create", "pk": str(site.pk)})
        view = _location_view(req, add_result=(False, "Server error"))

        _post(view, req)

        assert any("Server error" in t for t in message_texts(req, "error"))


class TestSyncSiteLocationViewUpdate:
    def test_update_without_coords_shows_warning(self):
        site = _make_site("Berlin", longitude=13.4)
        req = _make_request(post_data={"action": "update", "pk": str(site.pk)})
        view = _location_view(req)

        _post(view, req)

        assert any("Latitude and/or longitude is missing" in t for t in message_texts(req, "warning"))

    def test_update_api_failure_fetching_locations(self):
        site = _make_site("Berlin", latitude=52.5, longitude=13.4)
        req = _make_request(post_data={"action": "update", "pk": str(site.pk)})
        view = _location_view(req)
        view._librenms_api.get_locations.return_value = (False, "Connection error")

        _post(view, req)

        assert message_texts(req, "error") == ["Failed to retrieve LibreNMS locations."]

    def test_update_no_matching_location(self):
        site = _make_site("Berlin", latitude=52.5, longitude=13.4)
        req = _make_request(post_data={"action": "update", "pk": str(site.pk)})
        view = _location_view(req, locations=[{"location": "Paris"}])

        _post(view, req)

        assert message_texts(req, "error") == ["Could not find matching location for site 'Berlin'"]

    def test_update_success(self):
        site = _make_site("Berlin", latitude=52.5, longitude=13.4)
        req = _make_request(post_data={"action": "update", "pk": str(site.pk)})
        view = _location_view(req, locations=[{"location": "Berlin"}], update_result=(True, "ok"))

        _post(view, req)

        assert message_texts(req, "success") == ["Location 'Berlin' updated in LibreNMS successfully."]
        # The update payload carries coordinates only — renaming is not this action's job.
        view._librenms_api.update_location.assert_called_once_with("Berlin", {"lat": "52.500000", "lng": "13.400000"})

    def test_update_failure(self):
        site = _make_site("Berlin", latitude=52.5, longitude=13.4)
        req = _make_request(post_data={"action": "update", "pk": str(site.pk)})
        view = _location_view(req, locations=[{"location": "Berlin"}], update_result=(False, "boom"))

        _post(view, req)

        assert any("boom" in t for t in message_texts(req, "error"))


class TestSyncSiteLocationViewHelpers:
    def test_match_site_by_name(self):
        site = _make_site("London")
        locations = [{"location": "London"}, {"location": "Berlin"}]

        result = _location_view().match_site_with_location(site, locations)

        assert result["location"] == "London"

    def test_match_site_by_slug(self):
        from dcim.models import Site

        site = Site.objects.create(name="LondonDC", slug="london")

        result = _location_view().match_site_with_location(site, [{"location": "london"}])

        assert result is not None

    def test_match_site_not_found(self):
        site = _make_site("Tokyo")

        result = _location_view().match_site_with_location(site, [{"location": "London"}])

        assert result is None

    def test_check_coordinates_match_true(self):
        assert _location_view().check_coordinates_match(51.5, -0.12, "51.5", "-0.12") is True

    def test_check_coordinates_match_false(self):
        assert _location_view().check_coordinates_match(51.5, -0.12, "52.0", "-0.12") is False

    def test_check_coordinates_none_returns_false(self):
        assert _location_view().check_coordinates_match(None, -0.12, "51.5", "-0.12") is False

    def test_build_location_data_with_name(self):
        site = _make_site("London", latitude=51.5, longitude=-0.12)

        data = _location_view().build_location_data(site)

        assert data == {"location": "London", "lat": "51.500000", "lng": "-0.120000"}

    def test_build_location_data_without_name(self):
        site = _make_site("London", latitude=51.5, longitude=-0.12)

        data = _location_view().build_location_data(site, include_name=False)

        assert "location" not in data

    def test_get_site_by_pk_returns_none_on_not_found(self):
        from dcim.models import Site

        absent_pk = missing_pk(Site)

        assert _location_view().get_site_by_pk(absent_pk) is None

    def test_get_site_by_pk_returns_none_for_a_site_outside_the_grant(self):
        from dcim.models import Site

        _make_site("Bypk Mine")
        theirs = _make_site("Bypk Theirs")
        user = make_user_with_perms("loc-bypk", [("view", Site)], constraints={"name": "Bypk Mine"})

        view = _location_view(_make_request(user=user))

        assert view.get_site_by_pk(theirs.pk) is None

    def test_get_queryset_api_failure_returns_empty(self):
        _make_site("Any Site")
        view = _location_view(_make_request())
        view._librenms_api.get_locations.return_value = (False, "Error")

        assert view.get_queryset() == []

    def test_create_sync_data_no_match(self):
        site = _make_site("Atlantis")

        result = _location_view().create_sync_data(site, [{"location": "London"}])

        assert result.librenms_location is None
        assert result.is_synced is False


class TestSyncSiteLocationViewSuperMethods:
    """Lines 26-28, 32-35: get_table and get_context_data (call super())."""

    def test_get_table_configures_table(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_table = MagicMock()
        mock_request = MagicMock()
        view.request = mock_request

        with patch("netbox_librenms_plugin.views.sync.locations.SingleTableView.get_table", return_value=mock_table):
            result = view.get_table()

        mock_table.configure.assert_called_once_with(mock_request)
        assert result is mock_table

    def test_get_context_data_adds_filter_form(self):
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        view = object.__new__(SyncSiteLocationView)
        mock_queryset = MagicMock()
        mock_filterset_cls = MagicMock()
        mock_filterset_instance = MagicMock()
        mock_filterset_cls.return_value = mock_filterset_instance
        view.filterset = mock_filterset_cls
        view.request = MagicMock()
        view.request.GET = {}
        view.get_queryset = MagicMock(return_value=mock_queryset)

        parent_ctx = {"some_key": "some_value"}
        with patch(
            "netbox_librenms_plugin.views.sync.locations.SingleTableView.get_context_data", return_value=parent_ctx
        ):
            result = view.get_context_data()

        assert "filter_form" in result
        assert result["filter_form"] is mock_filterset_instance.form


class TestSyncSiteLocationViewGetQuerysetFilterset:
    """Lines 44-49: filterset branch in get_queryset."""

    def test_get_queryset_with_filterset_and_get_params(self):
        """A GET query runs the real filterset over the real Site rows."""
        _make_site("London")
        _make_site("Paris")
        view = _location_view(_make_request(get_data={"q": "London"}), locations=[{"location": "London"}])

        result = view.get_queryset()

        assert [row.netbox_site.name for row in result] == ["London"]

    def test_get_queryset_no_get_params_returns_list(self):
        """No GET params → the unfiltered sync-data list, one row per Site."""
        from dcim.models import Site

        _make_site("London")
        _make_site("Paris")
        view = _location_view(_make_request(), locations=[{"location": "London"}])

        result = view.get_queryset()

        assert len(result) == Site.objects.count()
        assert {row.netbox_site.name for row in result} >= {"London", "Paris"}

    def test_get_queryset_lists_only_sites_in_the_users_grant(self):
        """A read-only constrained grant renders only its sites through the GET gate."""
        from dcim.models import Site

        mine = _make_site("Location List Mine")
        _make_site("Location List Theirs")
        user = make_user_with_perms(
            "loc-list-scoped",
            [("view", Site)],
            constraints={"pk": mine.pk},
            plugin_write=False,
        )
        request = _make_request(user=user)
        view = _location_view(request, locations=[])

        response = _get(view, request)

        assert response.status_code == 200
        assert [row.netbox_site.pk for row in response.context_data["table"].data] == [mine.pk]
