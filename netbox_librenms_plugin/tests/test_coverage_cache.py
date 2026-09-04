"""Coverage for import cache behavior through Django's configured cache."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from django.core.cache import cache


@pytest.fixture
def server_key():
    """Give each concurrently running test an isolated cache namespace."""
    return f"cache-test-{uuid4().hex}"


def _metadata(server_key, cached_at, filters, **overrides):
    metadata = {
        "server_key": server_key,
        "cache_timeout": 300,
        "cached_at": cached_at,
        "filters": filters,
        "vc_enabled": False,
        "use_sysname": True,
        "strip_domain": False,
        "device_count": 1,
    }
    metadata.update(overrides)
    return metadata


def _store_indexed_search(server_key, metadata, *, cache_key=None):
    from netbox_librenms_plugin.import_utils.cache import get_cache_index_key

    cache_key = cache_key or f"cached-search:{uuid4().hex}"
    cache.set(cache_key, metadata, timeout=3600)
    cache.set(get_cache_index_key(server_key), [cache_key], timeout=3600)
    return cache_key


class TestCacheKeyContracts:
    def test_location_and_index_keys_are_scoped_to_the_server(self):
        from netbox_librenms_plugin.import_utils.cache import (
            get_cache_index_key,
            get_location_choices_cache_key,
        )

        assert get_location_choices_cache_key("primary") == "librenms_locations_choices:primary"
        assert get_location_choices_cache_key("secondary") == "librenms_locations_choices:secondary"
        assert get_cache_index_key("primary") == "librenms_cache_index_primary"

    def test_metadata_key_is_deterministic_across_filter_order(self):
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        first = get_cache_metadata_key("default", {"location": "NYC", "type": "network"}, True)
        reordered = get_cache_metadata_key("default", {"type": "network", "location": "NYC"}, True)

        assert first == reordered

    def test_metadata_key_distinguishes_values_servers_and_naming_options(self):
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        baseline = get_cache_metadata_key("primary", {"location": "NYC"}, False)

        assert baseline != get_cache_metadata_key("primary", {"location": "LON"}, False)
        assert baseline != get_cache_metadata_key("secondary", {"location": "NYC"}, False)
        assert baseline != get_cache_metadata_key("primary", {"location": "NYC"}, True)
        assert baseline != get_cache_metadata_key("primary", {"location": "NYC"}, False, use_sysname=False)
        assert baseline != get_cache_metadata_key("primary", {"location": "NYC"}, False, strip_domain=True)

    def test_none_filters_are_excluded_but_valid_falsy_values_are_preserved(self):
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        without_optional = get_cache_metadata_key("default", {"location": "NYC"}, False)
        with_none = get_cache_metadata_key("default", {"location": "NYC", "type": None}, False)
        with_zero = get_cache_metadata_key("default", {"location": "NYC", "type": 0}, False)
        with_false = get_cache_metadata_key("default", {"location": "NYC", "type": False}, False)

        assert with_none == without_optional
        assert with_zero != without_optional
        assert with_false != without_optional

    def test_validated_device_key_tracks_device_and_virtual_chassis_mode(self):
        from netbox_librenms_plugin.import_utils.cache import get_validated_device_cache_key

        baseline = get_validated_device_cache_key("default", {"type": "network"}, 42, False)
        same = get_validated_device_cache_key("default", {"type": "network"}, 42, False)

        assert baseline == same
        assert baseline != get_validated_device_cache_key("default", {"type": "network"}, 43, False)
        assert baseline != get_validated_device_cache_key("default", {"type": "network"}, 42, True)
        assert baseline.endswith("_42_novc_sysname=True_strip=False")

    def test_import_device_key_includes_the_server_and_device(self):
        from netbox_librenms_plugin.import_utils.cache import (
            get_import_device_cache_key,
            get_import_search_cache_key,
        )

        assert get_import_device_cache_key(42, "primary") == "import_device_data_primary_42"
        assert get_import_search_cache_key("primary", {"location": "42"}, {"hostname": "edge"}).startswith(
            "librenms_devices_import_primary_"
        )


class TestGetActiveCachedSearches:
    def test_missing_or_empty_index_returns_no_searches(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches, get_cache_index_key

        assert get_active_cached_searches(server_key) == []

        cache.set(get_cache_index_key(server_key), [], timeout=3600)

        assert get_active_cached_searches(server_key) == []

    def test_active_entry_returns_remaining_time_and_an_independent_display_copy(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        filters = {"hostname": "edge"}
        stored_timestamp = datetime.now(timezone.utc).isoformat()
        cache_key = _store_indexed_search(
            server_key,
            _metadata(server_key, stored_timestamp, filters),
        )

        result = get_active_cached_searches(server_key)

        assert len(result) == 1
        assert result[0]["cache_key"] == cache_key
        assert result[0]["cached_at"] == stored_timestamp
        assert 0 < result[0]["remaining_seconds"] <= 300
        assert result[0]["display_filters"] == {"hostname": "edge"}
        assert result[0]["display_filters"] is not result[0]["filters"]

    def test_location_and_type_filters_use_cached_display_names(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import (
            get_active_cached_searches,
            get_location_choices_cache_key,
        )

        cache.set(
            get_location_choices_cache_key(server_key),
            [("42", "Amsterdam DC"), ("99", "London DC")],
            timeout=3600,
        )
        _store_indexed_search(
            server_key,
            _metadata(
                server_key,
                datetime.now(timezone.utc).isoformat(),
                {"location": "42", "type": "network"},
            ),
        )

        result = get_active_cached_searches(server_key)

        assert result[0]["display_filters"] == {"location": "Amsterdam DC", "type": "Network"}

    def test_expired_entry_is_removed_from_the_real_index(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches, get_cache_index_key

        _store_indexed_search(
            server_key,
            _metadata(server_key, datetime.fromtimestamp(0, timezone.utc).isoformat(), {"hostname": "edge"}),
        )

        assert get_active_cached_searches(server_key) == []
        assert cache.get(get_cache_index_key(server_key)) == []

    @pytest.mark.parametrize(
        "invalid_metadata",
        [
            None,
            "not-a-mapping",
            {},
            {"server_key": "another-server"},
            {"server_key": "placeholder", "filters": {}},
        ],
    )
    def test_missing_or_invalid_metadata_is_removed_from_the_index(self, server_key, invalid_metadata):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches, get_cache_index_key

        cache_key = f"invalid-search:{uuid4().hex}"
        if invalid_metadata is not None:
            if isinstance(invalid_metadata, dict) and invalid_metadata.get("server_key") == "placeholder":
                invalid_metadata = {**invalid_metadata, "server_key": server_key}
            cache.set(cache_key, invalid_metadata, timeout=3600)
        cache.set(get_cache_index_key(server_key), [cache_key], timeout=3600)

        assert get_active_cached_searches(server_key) == []
        assert cache.get(get_cache_index_key(server_key)) == []

    def test_naive_future_timestamp_is_treated_as_utc(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        _store_indexed_search(
            server_key,
            _metadata(
                server_key,
                "2099-01-01T12:00:00",
                {"hostname": "future-edge"},
                cache_timeout=99_999_999_999,
            ),
        )

        result = get_active_cached_searches(server_key)

        assert len(result) == 1
        assert result[0]["remaining_seconds"] > 0

    def test_datetime_timestamp_is_used_without_string_parsing(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        stored_datetime = datetime.now(timezone.utc)
        _store_indexed_search(
            server_key,
            _metadata(server_key, stored_datetime, {"hostname": "datetime-edge"}),
        )

        result = get_active_cached_searches(server_key)

        assert result[0]["cached_at"] == stored_datetime.isoformat()
        assert result[0]["display_filters"] == {"hostname": "datetime-edge"}

    def test_datetime_timestamp_renders_as_iso_8601(self, server_key):
        """The cached-search timestamp must be safe for JavaScript date parsing."""
        from django.template.loader import render_to_string

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches_for_servers

        stored_datetime = datetime.now(timezone.utc)
        _store_indexed_search(
            server_key,
            _metadata(server_key, stored_datetime, {"hostname": "datetime-edge"}),
        )

        cached_searches = get_active_cached_searches_for_servers({server_key: "Primary"})
        output = render_to_string(
            "netbox_librenms_plugin/inc/_cached_search_links.html",
            {"cached_searches": cached_searches},
        )

        assert f'data-cache-timestamp="{stored_datetime.isoformat()}"' in output

    @pytest.mark.parametrize("cached_at", ["NOT_A_VALID_DATETIME", [], {}])
    def test_malformed_timestamp_expires_without_raising(self, server_key, cached_at):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        _store_indexed_search(
            server_key,
            _metadata(server_key, cached_at, {"hostname": "edge"}),
        )

        assert get_active_cached_searches(server_key) == []

    def test_results_are_sorted_most_recent_first(self, server_key):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches, get_cache_index_key

        now = datetime.now(timezone.utc)
        older_key = f"older-search:{uuid4().hex}"
        newer_key = f"newer-search:{uuid4().hex}"
        cache.set(
            older_key,
            _metadata(server_key, (now - timedelta(seconds=60)).isoformat(), {"hostname": "older-edge"}),
            timeout=3600,
        )
        cache.set(
            newer_key,
            _metadata(server_key, now.isoformat(), {"hostname": "newer-edge"}),
            timeout=3600,
        )
        cache.set(get_cache_index_key(server_key), [older_key, newer_key], timeout=3600)

        result = get_active_cached_searches(server_key)

        assert [search["display_filters"]["hostname"] for search in result] == ["newer-edge", "older-edge"]

    def test_multiple_servers_keep_namespaces_and_labels_separate(self):
        from netbox_librenms_plugin.import_utils.cache import (
            get_active_cached_searches_for_servers,
            get_cache_index_key,
        )

        first_server = f"primary-{uuid4().hex}"
        second_server = f"secondary-{uuid4().hex}"
        now = datetime.now(timezone.utc)
        for key, hostname, age in (
            (first_server, "primary-edge", 30),
            (second_server, "secondary-edge", 0),
        ):
            cache_key = f"server-search:{uuid4().hex}"
            cache.set(
                cache_key,
                _metadata(key, (now - timedelta(seconds=age)).isoformat(), {"hostname": hostname}),
                timeout=3600,
            )
            cache.set(get_cache_index_key(key), [cache_key], timeout=3600)

        result = get_active_cached_searches_for_servers(
            {first_server: "Primary LibreNMS", second_server: "Secondary LibreNMS"}
        )

        assert [(search["server_display_name"], search["display_filters"]["hostname"]) for search in result] == [
            ("Secondary LibreNMS", "secondary-edge"),
            ("Primary LibreNMS", "primary-edge"),
        ]
