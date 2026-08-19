"""A corrupt cache or request value must fail closed, never raise TypeError.

``x in {"a", "b"}`` raises ``TypeError: unhashable type: 'list'`` rather than returning False,
so a guard written to purge a bad entry raises instead. Redis is outside the process, and a
JSON body carries any type, so every one of these reads has to narrow before the membership
test. See ``constants.is_supported_interface_name_field``.
"""

import pytest

from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view


UNHASHABLE = ["ifName"]


class TestSupportedInterfaceNameFieldPredicate:
    """The one predicate every interface-name check routes through."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("ifName", True),
            ("ifDescr", True),
            ("ifAlias", False),
            (None, False),
            (5, False),
            (True, False),
            (["ifName"], False),
            ({"ifName": 1}, False),
            ({"ifName"}, False),
        ],
    )
    def test_returns_a_bool_for_every_input_shape(self, value, expected):
        from netbox_librenms_plugin.constants import is_supported_interface_name_field

        assert is_supported_interface_name_field(value) is expected

    def test_the_raw_membership_test_this_replaces_would_raise(self):
        """Positive control: prove the predicate is load-bearing, not decoration."""
        from netbox_librenms_plugin.constants import INTERFACE_NAME_FIELDS

        with pytest.raises(TypeError, match="unhashable"):
            UNHASHABLE in INTERFACE_NAME_FIELDS  # noqa: B015


@pytest.mark.django_db
class TestCorruptInterfaceNameFieldInCache:
    """A cached snapshot holding an unhashable name field is purged, not raised on."""

    @staticmethod
    def _seed(view, device, value, server_key="default"):
        from django.core.cache import cache

        key = view.get_cache_key(device, "ip_addresses", server_key=server_key)
        cache.set(
            key,
            {
                "ip_addresses": [],
                "mgmt_ip": "",
                "ports_by_id": {},
                "interface_name_field": value,
            },
            timeout=300,
        )
        return key

    def test_ip_tab_purges_the_entry_instead_of_raising(self):
        from dcim.models import Device
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        device = make_device("corrupt-name-field-tab", librenms_cf={"default": 7})
        user = make_user_with_perms("corrupt-name-field-tab", [("view", Device)])
        request = make_request("get", {}, user=user)
        view = make_view(BaseIPAddressTableView, request)
        key = self._seed(view, device, UNHASHABLE)
        assert cache.get(key) is not None, "the seed never landed, so the assertions below prove nothing"

        try:
            # The corrupt entry must be treated as a miss, so the reader returns None
            # rather than raising out of the guard that exists to purge it.
            result = view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
            purged = cache.get(key) is None
        finally:
            cache.delete(key)

        assert result is None
        assert purged, "the corrupt entry survived, so a later request hits the same raise"


@pytest.mark.django_db
class TestCorruptInterfaceNameFieldOnTheSyncPath:
    """The POST sync reader applies the same rule as the tab reader."""

    @staticmethod
    def _seed_and_read(name_field_value):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

        device = make_device(f"sync-name-field-{abs(hash(str(name_field_value))) % 10000}")
        view = SyncIPAddressesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        key = view.get_cache_key(device, "ip_addresses", "default")
        cache.set(
            key,
            {"ip_addresses": [], "ports_by_id": {}, "interface_name_field": name_field_value},
            timeout=300,
        )
        assert cache.get(key) is not None, "the seed never landed, so this proves nothing"
        try:
            return view.get_cached_ip_snapshot(device, require_create_metadata=True)
        finally:
            cache.delete(key)

    def test_an_unhashable_name_field_is_rejected(self):
        assert self._seed_and_read(UNHASHABLE) is None

    def test_a_valid_snapshot_is_accepted(self):
        """Positive control, so the rejection above cannot pass for the wrong reason."""
        assert self._seed_and_read("ifName") is not None
