"""Coverage tests for netbox_librenms_plugin.import_utils.cache module."""

from unittest.mock import patch


class TestGetLocationChoicesCacheKey:
    """Tests for get_location_choices_cache_key (line 14)."""

    def test_returns_correct_format(self):
        from netbox_librenms_plugin.import_utils.cache import get_location_choices_cache_key

        result = get_location_choices_cache_key("default")
        assert result == "librenms_locations_choices:default"

    def test_different_server_keys(self):
        from netbox_librenms_plugin.import_utils.cache import get_location_choices_cache_key

        assert get_location_choices_cache_key("primary") == "librenms_locations_choices:primary"
        assert get_location_choices_cache_key("secondary") == "librenms_locations_choices:secondary"


class TestGetActiveCachedSearches:
    """Tests for get_active_cached_searches (lines 52-131)."""

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_empty_cache_index_returns_empty_list(self, mock_cache):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        mock_cache.get.return_value = []
        result = get_active_cached_searches("default")
        assert result == []

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_none_cache_index_returns_empty_list(self, mock_cache):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        # cache.get(cache_index_key, []) returns [] when cache misses
        mock_cache.get.side_effect = lambda key, default=None: default if "cache_index" in key else None
        result = get_active_cached_searches("default")
        assert result == []

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_entry_with_remaining_time_is_returned(self, mock_cache):
        from datetime import datetime, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["some_cache_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "some_cache_key":
                return {
                    "cache_timeout": 300,
                    "cached_at": cached_at,
                    "filters": {},
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert len(result) == 1
        assert result[0]["remaining_seconds"] > 0
        assert result[0]["cache_key"] == "some_cache_key"
        assert result[0]["display_filters"] == {}

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_expired_entry_is_cleaned_up(self, mock_cache):
        from datetime import datetime, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        # Cached at epoch (way in the past)
        old_time = datetime.fromtimestamp(0, timezone.utc).isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["expired_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "expired_key":
                return {
                    "cache_timeout": 300,
                    "cached_at": old_time,
                    "filters": {},
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        # Expired entries should NOT be in results
        assert result == []
        # Cache index should be updated to remove expired keys
        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        assert "cache_index" in call_args[0][0]
        assert call_args[0][1] == []

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_location_id_enriched_from_cache(self, mock_cache):
        from datetime import datetime, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["search_key"]
            if key == "librenms_locations_choices:default":
                return [("42", "New York DC"), ("99", "London DC")]
            if key == "search_key":
                return {
                    "cache_timeout": 300,
                    "cached_at": cached_at,
                    "filters": {"location": "42"},
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert len(result) == 1
        assert result[0]["display_filters"]["location"] == "New York DC"

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_type_code_enriched_to_display_name(self, mock_cache):
        from datetime import datetime, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["search_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "search_key":
                return {
                    "cache_timeout": 300,
                    "cached_at": cached_at,
                    "filters": {"type": "network"},
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert len(result) == 1
        assert result[0]["display_filters"]["type"] == "Network"

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_missing_filters_key_falls_back_to_empty_dict(self, mock_cache):
        from datetime import datetime, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        now = datetime.now(timezone.utc)
        cached_at = now.isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["search_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "search_key":
                # No 'filters' key
                return {
                    "cache_timeout": 300,
                    "cached_at": cached_at,
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert len(result) == 1
        assert result[0]["display_filters"] == {}

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_timezone_naive_cached_at_normalized_to_utc(self, mock_cache):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        # naive datetime string (no tzinfo)
        naive_ts = "2099-01-01T12:00:00"

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["search_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "search_key":
                return {
                    "cache_timeout": 99999999,
                    "cached_at": naive_ts,
                    "filters": {},
                }
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        # Should not raise; remaining_seconds should be > 0
        assert len(result) == 1
        assert result[0]["remaining_seconds"] > 0

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_malformed_cached_at_falls_back_to_epoch(self, mock_cache):
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["search_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "search_key":
                return {
                    "cache_timeout": 300,
                    "cached_at": "NOT_A_VALID_DATETIME",
                    "filters": {},
                }
            return default

        mock_cache.get.side_effect = mock_get

        # malformed cached_at → epoch → expired → empty result
        result = get_active_cached_searches("default")
        assert result == []

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_metadata_none_skipped(self, mock_cache):
        """Cache key in index but metadata is None → skip."""
        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["gone_key"]
            if "librenms_locations_choices" in key:
                return None
            # metadata expired from cache
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert result == []
        # Should update index to remove the gone key
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.import_utils.cache.cache")
    def test_results_sorted_by_cached_at_most_recent_first(self, mock_cache):
        from datetime import datetime, timedelta, timezone

        from netbox_librenms_plugin.import_utils.cache import get_active_cached_searches

        now = datetime.now(timezone.utc)
        older = (now - timedelta(seconds=60)).isoformat()
        newer = now.isoformat()

        def mock_get(key, default=None):
            if "cache_index" in key:
                return ["older_key", "newer_key"]
            if "librenms_locations_choices" in key:
                return None
            if key == "older_key":
                return {"cache_timeout": 300, "cached_at": older, "filters": {}}
            if key == "newer_key":
                return {"cache_timeout": 300, "cached_at": newer, "filters": {}}
            return default

        mock_cache.get.side_effect = mock_get

        result = get_active_cached_searches("default")
        assert len(result) == 2
        assert result[0]["cached_at"] >= result[1]["cached_at"]


class TestGetCacheMetadataKeyDeterminism:
    """Tests that get_cache_metadata_key is deterministic."""

    def test_different_filter_values_produce_different_keys(self):
        """Different filter values should produce different cache keys."""
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        key1 = get_cache_metadata_key("default", {"location": "NYC"}, False)
        key2 = get_cache_metadata_key("default", {"location": "LON"}, False)
        assert key1 != key2

    def test_same_filters_produce_same_key(self):
        """Same filters in any insertion order should produce the same cache key."""
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        key1 = get_cache_metadata_key("default", {"location": "NYC", "type": "network"}, True)
        key2 = get_cache_metadata_key("default", {"type": "network", "location": "NYC"}, True)
        assert key1 == key2

    def test_none_values_excluded_from_hash(self):
        """None filter values should be excluded and produce same key as absent."""
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        key_with_none = get_cache_metadata_key("default", {"location": "NYC", "type": None}, False)
        key_without = get_cache_metadata_key("default", {"location": "NYC"}, False)
        assert key_with_none == key_without

    def test_different_server_keys_produce_different_keys(self):
        """Different server keys should produce different cache metadata keys."""
        from netbox_librenms_plugin.import_utils.cache import get_cache_metadata_key

        key1 = get_cache_metadata_key("production", {"location": "NYC"}, False)
        key2 = get_cache_metadata_key("staging", {"location": "NYC"}, False)
        assert key1 != key2
