"""
Tests for views/imports/list.py — targeting ≥95% coverage.

Tests the LibreNMSImportView: get_required_permission, should_use_background_job,
_load_job_results, get, get_queryset, get_table, and _get_import_queryset.

Conventions:
- Plain pytest classes (no Django TestCase)
- No @pytest.mark.django_db — all DB interactions mocked
- Inline imports inside test methods
- object.__new__(ViewClass) for instantiation
- MagicMock for all external dependencies
"""

from unittest.mock import MagicMock, patch


class TestGetRequiredPermission:
    """Tests for get_required_permission()."""

    def test_returns_device_view_permission(self):
        """get_required_permission returns the 'view' permission for Device model."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        with patch("netbox_librenms_plugin.views.imports.list.Device") as mock_device:
            with patch("utilities.permissions.get_permission_for_model") as mock_perm:
                mock_perm.return_value = "dcim.view_device"
                result = view.get_required_permission()
                assert result == "dcim.view_device"
                mock_perm.assert_called_once_with(mock_device, "view")


class TestShouldUseBackgroundJob:
    """Tests for should_use_background_job()."""

    def test_non_superuser_returns_false(self):
        """Non-superusers always get synchronous mode."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._filter_form_data = {"use_background_job": True}
        view.request = MagicMock()
        view.request.user.is_superuser = False

        assert view.should_use_background_job() is False

    def test_superuser_use_background_true(self):
        """Superuser with use_background_job=True returns True."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._filter_form_data = {"use_background_job": True}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is True

    def test_superuser_use_background_false(self):
        """Superuser with use_background_job=False returns False."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._filter_form_data = {"use_background_job": False}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is False

    def test_superuser_field_missing_defaults_true(self):
        """When use_background_job key absent, default is True for superusers."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._filter_form_data = {}
        view.request = MagicMock()
        view.request.user.is_superuser = True

        assert view.should_use_background_job() is True


class TestLoadJobResults:
    """Tests for _load_job_results()."""

    def test_job_not_found_returns_empty(self):
        """Returns [] when job doesn't exist (DoesNotExist path lines 79-81)."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        class MockDoesNotExist(Exception):
            pass

        with patch("netbox_librenms_plugin.views.imports.list.logger") as mock_logger:
            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.DoesNotExist = MockDoesNotExist
                mock_job_cls.objects.get.side_effect = MockDoesNotExist("not found")

                result = view._load_job_results(999)
                assert result == []
                mock_logger.warning.assert_called()

    def test_job_not_completed_returns_empty(self):
        """Returns [] when job status is not 'completed'."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        with patch("netbox_librenms_plugin.views.imports.list.logger"):
            mock_job = MagicMock()
            mock_job.status = "running"

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                result = view._load_job_results(42)
                assert result == []

    def test_empty_device_ids_returns_empty(self):
        """Returns [] when job data has no device_ids."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        with patch("netbox_librenms_plugin.views.imports.list.logger"):
            mock_job = MagicMock()
            mock_job.status = "completed"
            mock_job.data = {"device_ids": [], "filters": {}, "server_key": "default"}

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                result = view._load_job_results(42)
                assert result == []

    def test_devices_loaded_from_cache(self):
        """Returns validated devices found in cache for each device_id."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        mock_device_a = {"device_id": 1, "hostname": "router1"}
        mock_device_b = {"device_id": 2, "hostname": "router2"}

        with patch("netbox_librenms_plugin.views.imports.list.logger"):
            mock_job = MagicMock()
            mock_job.status = "completed"
            mock_job.data = {
                "device_ids": [1, 2],
                "filters": {},
                "server_key": "default",
                "vc_detection_enabled": False,
                "use_sysname": True,
                "strip_domain": False,
                "cached_at": "2024-01-01T00:00:00",
                "cache_timeout": 300,
            }

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    with patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key") as mock_key:
                        mock_key.side_effect = lambda **kwargs: f"key_{kwargs['device_id']}"
                        mock_cache.get.side_effect = lambda key: mock_device_a if key == "key_1" else mock_device_b

                        result = view._load_job_results(42)
                        assert len(result) == 2
                        assert mock_device_a in result
                        assert mock_device_b in result

    def test_load_job_results_sets_vc_detection_enabled_from_job_data(self):
        """vc_detection_enabled from job data is preserved on the view instance."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        with patch("netbox_librenms_plugin.views.imports.list.logger"):
            mock_job = MagicMock()
            mock_job.status = "completed"
            mock_job.data = {
                "device_ids": [1],
                "filters": {},
                "server_key": "default",
                "vc_detection_enabled": True,
                "use_sysname": True,
                "strip_domain": False,
            }

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    with patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key") as mock_key:
                        mock_key.return_value = "key_1"
                        mock_cache.get.return_value = {"device_id": 1}

                        result = view._load_job_results(42)

        assert len(result) == 1
        assert view._vc_detection_enabled is True

    def test_cache_miss_skips_device(self):
        """Devices missing from cache are silently skipped."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        mock_device = {"device_id": 1, "hostname": "router1"}

        with patch("netbox_librenms_plugin.views.imports.list.logger"):
            mock_job = MagicMock()
            mock_job.status = "completed"
            mock_job.data = {
                "device_ids": [1, 2],
                "filters": {},
                "server_key": "default",
                "vc_detection_enabled": False,
                "use_sysname": True,
                "strip_domain": False,
            }

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    with patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key") as mock_key:
                        mock_key.side_effect = lambda **kwargs: f"key_{kwargs['device_id']}"
                        # device_id=2 is missing from cache (returns None)
                        mock_cache.get.side_effect = lambda key: mock_device if key == "key_1" else None

                        result = view._load_job_results(42)
                        assert len(result) == 1
                        assert result[0] == mock_device

    def test_all_cache_expired_logs_error(self):
        """When all devices missing from cache, logs error and returns []."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)

        with patch("netbox_librenms_plugin.views.imports.list.logger") as mock_logger:
            mock_job = MagicMock()
            mock_job.status = "completed"
            mock_job.data = {
                "device_ids": [1, 2],
                "filters": {},
                "server_key": "default",
                "vc_detection_enabled": False,
                "use_sysname": True,
                "strip_domain": False,
            }

            with patch("core.models.Job") as mock_job_cls:
                mock_job_cls.objects.get.return_value = mock_job

                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    with patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key") as mock_key:
                        mock_key.return_value = "some_key"
                        mock_cache.get.return_value = None

                        result = view._load_job_results(42)
                        assert result == []
                        mock_logger.error.assert_called_once()

    def test_load_job_results_sets_name_flags_from_job_data(self):
        """_load_job_results mirrors use_sysname/strip_domain from job metadata."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.data = {
            "device_ids": [1],
            "filters": {},
            "server_key": "default",
            "vc_detection_enabled": False,
            "use_sysname": False,
            "strip_domain": True,
        }

        with patch("core.models.Job") as mock_job_cls:
            mock_job_cls.objects.get.return_value = mock_job
            with patch("netbox_librenms_plugin.import_utils.get_validated_device_cache_key", return_value="cache_key"):
                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    mock_cache.get.return_value = {"device_id": 1, "hostname": "router1"}
                    result = view._load_job_results(1)

        assert len(result) == 1
        assert view._use_sysname is False
        assert view._strip_domain is True


class TestGetTable:
    """Tests for get_table()."""

    def test_returns_table_with_import_data(self):
        """get_table returns a DeviceImportTable populated from _import_data."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._import_data = [{"device_id": 1, "hostname": "router1"}]

        request = MagicMock()
        request.GET.get.return_value = None

        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table

            result = view.get_table([], request, bulk_actions=True)
            assert result is mock_table
            mock_table_cls.assert_called_once_with(
                view._import_data,
                order_by=None,
            )

    def test_get_table_loads_import_data_when_missing(self):
        """When _import_data is absent, get_table calls _get_import_queryset."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        # No _import_data set
        view._job_results_loaded = False
        view._filters_submitted = False

        request = MagicMock()
        request.GET.get.return_value = None

        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable") as mock_table_cls:
            mock_table_cls.return_value = MagicMock()
            view.get_table([], request, bulk_actions=True)
            assert hasattr(view, "_import_data")


class TestGetQueryset:
    """Tests for get_queryset()."""

    def test_returns_empty_device_queryset(self):
        """get_queryset always returns Device.objects.none()."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = False

        request = MagicMock()

        with patch("netbox_librenms_plugin.views.imports.list.Device") as mock_device:
            mock_device.objects.none.return_value = []
            result = view.get_queryset(request)
            assert result == []
            mock_device.objects.none.assert_called_once()

    def test_sets_import_data(self):
        """get_queryset sets _import_data via _get_import_queryset."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = False

        request = MagicMock()

        with patch("netbox_librenms_plugin.views.imports.list.Device") as mock_device:
            mock_device.objects.none.return_value = []
            view.get_queryset(request)
            assert hasattr(view, "_import_data")
            assert view._import_data == []


class TestGetImportQueryset:
    """Tests for _get_import_queryset()."""

    def test_job_results_loaded_returns_existing(self):
        """When _job_results_loaded is True, returns existing _import_data."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = True
        view._import_data = [{"device_id": 1}]

        result = view._get_import_queryset()
        assert result == [{"device_id": 1}]

    def test_filters_not_submitted_returns_empty(self):
        """When no filters submitted, returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = False

        result = view._get_import_queryset()
        assert result == []

    def test_filter_warning_returns_empty(self):
        """When _filter_warning is set, returns empty list."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = True
        view._filter_warning = "Some warning"

        result = view._get_import_queryset()
        assert result == []

    def test_calls_process_device_filters(self):
        """When filters submitted and valid, calls process_device_filters."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = True
        view._filter_warning = None
        view._filter_form_data = {
            "librenms_location": "DC1",
            "enable_vc_detection": False,
            "clear_cache": False,
            "show_disabled": False,
            "exclude_existing": False,
        }
        view._vc_detection_enabled = False
        view._cache_cleared = False
        view._use_sysname = True
        view._strip_domain = False

        mock_request = MagicMock()
        view._request = mock_request
        mock_api = MagicMock()
        mock_api.server_key = "default"
        view._librenms_api = mock_api

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([{"device_id": 1}], False)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings_cls:
                    mock_settings_cls.objects.first.return_value = None

                    with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                        mock_cache.get.return_value = None

                        result = view._get_import_queryset()
                        mock_process.assert_called_once()
                        assert result == [{"device_id": 1}]


class TestGetView:
    """Tests for the get() method of LibreNMSImportView."""

    def _make_view_with_request(self, superuser=True, query_params=None):
        """Helper to set up a view instance with a mock request."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        request = MagicMock()
        request.user.is_superuser = superuser
        request.user.username = "testuser"

        params = query_params or {}
        request.GET.get = lambda key, default=None: params.get(key, default)
        request.GET.__contains__ = lambda self, key: key in params

        view.request = request
        return view, request

    def test_get_job_id_loads_results(self):
        """When job_id is in GET params, _load_job_results is called."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"job_id": "42"})

        mock_devices = [{"device_id": 1, "hostname": "router1"}]
        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(view, "_load_job_results", return_value=mock_devices) as mock_load:
            with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.return_value = None
                    mock_settings.objects.get_or_create.return_value = (None, False)

                    with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                        mock_pref.return_value = None

                        mock_form_cls = MagicMock()
                        mock_form = MagicMock()
                        mock_form.is_valid.return_value = False
                        mock_form_cls.return_value = mock_form
                        view.filterset_form = mock_form_cls

                        with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                            mock_render.return_value = MagicMock()

                            with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                                ) as mock_searches:
                                    mock_searches.return_value = []

                                    with patch.object(view, "get_server_info", return_value={}):
                                        view.get(request)
                                        mock_load.assert_called_once_with(42)

    def test_get_job_id_preserves_vc_flag_when_query_flag_missing(self):
        """job_id pages keep vc_detection_enabled from loaded job results."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"job_id": "42"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        def _load_job_side_effect(_job_id):
            view._vc_detection_enabled = True
            return [{"device_id": 1, "hostname": "router1"}]

        with patch.object(view, "_load_job_results", side_effect=_load_job_side_effect) as mock_load:
            with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.return_value = None
                    mock_settings.objects.get_or_create.return_value = (None, False)

                    with patch("netbox_librenms_plugin.views.imports.list.get_user_pref", return_value=None):
                        mock_form_cls = MagicMock()
                        mock_form = MagicMock()
                        mock_form.is_valid.return_value = False
                        mock_form_cls.return_value = mock_form
                        view.filterset_form = mock_form_cls

                        with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                            mock_render.return_value = MagicMock()

                            with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.list.get_active_cached_searches",
                                    return_value=[],
                                ):
                                    with patch.object(view, "get_server_info", return_value={}):
                                        view.get(request)

        mock_load.assert_called_once_with(42)
        context = mock_render.call_args[0][2]
        assert context["vc_detection_enabled"] is True

    def test_get_invalid_job_id_logs_warning(self):
        """Invalid (non-integer) job_id is caught and logged."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"job_id": "not-an-int"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch("netbox_librenms_plugin.views.imports.list.logger") as mock_logger:
                                        view.get(request)
                                        mock_logger.warning.assert_called()

    def test_get_no_job_id_renders_template(self):
        """Normal GET without job_id renders the import template."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request()

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    view.get(request)
                                    mock_render.assert_called_once()
                                    # Verify called with correct template
                                    call_args = mock_render.call_args
                                    assert "librenms_import.html" in call_args[0][1]

    def test_get_settings_exception_falls_back_to_none(self):
        """LibreNMSSettings exception during GET is caught and settings set to None."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request()

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.side_effect = Exception("DB error")
                mock_settings.objects.get_or_create.side_effect = Exception("DB error")

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    # Should not raise
                                    view.get(request)
                                    mock_render.assert_called_once()
                                    ctx = mock_render.call_args[0][2]
                                    assert ctx["settings"] is None

    def test_get_filters_submitted_with_valid_form(self):
        """When filters are submitted and form is valid, processes filters."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"apply_filters": "1", "librenms_location": "DC1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta_key:
                                        mock_meta_key.return_value = "meta_key"
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.return_value = 5
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                with patch.object(view, "_get_import_queryset", return_value=[]):
                                                    view.get(request)
                                                mock_render.assert_called_once()

    def test_get_background_job_enqueued_for_superuser(self):
        """Superuser with workers available triggers background job enqueue."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(
            superuser=True,
            query_params={"apply_filters": "1", "librenms_location": "DC1"},
        )

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": True,  # Background mode
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue") as mock_workers:
                        mock_workers.return_value = 1  # Workers available

                        with patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key") as mock_meta:
                            mock_meta.return_value = "meta_key"

                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                mock_cache.get.return_value = None  # No cached results

                                with patch(
                                    "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                ) as mock_count:
                                    mock_count.return_value = 10

                                    with patch("netbox_librenms_plugin.jobs.FilterDevicesJob") as mock_job_cls:
                                        mock_job = MagicMock()
                                        mock_job.pk = 123
                                        mock_job.job_id = "uuid-123"
                                        mock_job_cls.enqueue.return_value = mock_job

                                        import json
                                        from django.http import JsonResponse

                                        result = view.get(request)
                                        assert isinstance(result, JsonResponse)
                                        data = json.loads(result.content)
                                        assert "job_pk" in data
                                        assert "poll_url" in data

    def test_get_no_workers_falls_back_to_sync(self):
        """With no RQ workers, falls back to synchronous processing."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(
            superuser=True,
            query_params={"apply_filters": "1", "librenms_location": "DC1"},
        )

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": True,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue") as mock_workers:
                        mock_workers.return_value = 0  # No workers

                        with patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key") as mock_meta:
                            mock_meta.return_value = "meta_key"

                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                mock_cache.get.return_value = None

                                with patch(
                                    "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                ) as mock_count:
                                    mock_count.return_value = 5

                                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                                        mock_render.return_value = MagicMock()

                                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                                            with patch(
                                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                                            ) as mock_searches:
                                                mock_searches.return_value = []

                                                with patch.object(view, "get_server_info", return_value={}):
                                                    with patch(
                                                        "netbox_librenms_plugin.views.imports.list.messages"
                                                    ) as mock_messages:
                                                        with patch.object(
                                                            view, "_get_import_queryset", return_value=[]
                                                        ):
                                                            view.get(request)
                                                        # Should render page (synchronous fallback)
                                                        mock_render.assert_called_once()
                                                        mock_messages.warning.assert_called_once()

    def test_get_job_results_expired_shows_warning(self):
        """When job results are empty, shows warning message to user."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"job_id": "42"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch.object(view, "_load_job_results", return_value=[]):
                        with patch("netbox_librenms_plugin.views.imports.list.messages") as mock_messages:
                            with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                                mock_render.return_value = MagicMock()

                                with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                                    ) as mock_searches:
                                        mock_searches.return_value = []

                                        with patch.object(view, "get_server_info", return_value={}):
                                            view.get(request)
                                            mock_messages.warning.assert_called_once()

    def test_get_legacy_skip_vc_detection_flag(self):
        """Legacy skip_vc_detection=true sets vc_detection_enabled=False."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"skip_vc_detection": "true"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    view.get(request)
                                    assert view._vc_detection_enabled is False

    def test_get_enable_vc_detection_flag(self):
        """enable_vc_detection=1 sets vc_detection_enabled=True."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"enable_vc_detection": "1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    view.get(request)
                                    assert view._vc_detection_enabled is True

    def test_get_context_includes_can_use_background_jobs(self):
        """Rendered context includes can_use_background_jobs keyed on is_superuser."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(superuser=True)

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    view.get(request)
                                    ctx = mock_render.call_args[0][2]
                                    assert ctx["can_use_background_jobs"] is True

    def test_get_form_non_field_errors_set_warning(self):
        """Non-field form validation errors are stored as _filter_warning."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view_with_request(query_params={"apply_filters": "1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    expected_warning = "At least one filter is required."
                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = False
                    mock_form.non_field_errors.return_value = [expected_warning]
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    view.get(request)
                                    assert view._filter_warning == expected_warning


class TestGetViewFilterFields:
    """Tests that exercise individual filter field extraction paths in get()."""

    def _make_view(self, query_params=None):
        """Helper to create a view with a mock request containing specific params."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        request = MagicMock()
        request.user.is_superuser = False
        request.user.username = "testuser"
        params = query_params or {}
        request.GET.get = lambda key, default=None: params.get(key, default)
        request.GET.__contains__ = lambda self, key: key in params
        view.request = request
        return view, request

    def test_get_all_filter_fields_extracted(self):
        """All six filter fields are extracted into libre_filters for background jobs."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view(
            query_params={
                "apply_filters": "1",
                "librenms_location": "DC1",
                "librenms_type": "network",
                "librenms_os": "ios",
                "librenms_hostname": "router",
                "librenms_sysname": "sw1",
                "librenms_hardware": "Cisco C9300",
            }
        )

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta:
                                        mock_meta.return_value = "meta_key"
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.return_value = 5
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                with patch.object(view, "_get_import_queryset", return_value=[]):
                                                    view.get(request)
                                                # All filters were submitted — filter count passed to device count
                                                mock_count.assert_called_once()
                                                args, kwargs = mock_count.call_args
                                                filters = (
                                                    kwargs.get("filters")
                                                    if kwargs.get("filters") is not None
                                                    else (args[0] if args else None)
                                                )
                                                assert filters.get("location") == "DC1"
                                                assert filters.get("type") == "network"
                                                assert filters.get("os") == "ios"
                                                assert filters.get("hostname") == "router"
                                                assert filters.get("sysname") == "sw1"
                                                assert filters.get("hardware") == "Cisco C9300"

    def test_get_settings_exception_is_caught(self):
        """LibreNMSSettings exception at the top of get() is caught and settings set to None."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view(query_params={"apply_filters": "1", "librenms_location": "DC1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.side_effect = Exception("DB error")
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta:
                                        mock_meta.return_value = "meta_key"
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.return_value = 3
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                # Should not raise despite the settings exception
                                                with patch.object(view, "_get_import_queryset", return_value=[]):
                                                    view.get(request)
                                                mock_render.assert_called_once()

    def test_get_settings_exception_in_inline_load(self):
        """LibreNMSSettings exception inside filter block is caught (lines 263-264)."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view(query_params={"apply_filters": "1", "librenms_location": "DC1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                # First call (module-level read at top of get()) succeeds
                # Second call (inline, inside the filter block) raises
                first_call = [True]

                def first_then_raise(*a, **kw):
                    if first_call:
                        first_call.pop()
                        return None
                    raise Exception("DB error")

                mock_settings.objects.first.side_effect = first_then_raise
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta:
                                        mock_meta.return_value = "meta_key"
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.return_value = 3
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                # Should not raise despite the settings exception
                                                view.get(request)
                                                mock_render.assert_called_once()

    def test_get_device_count_exception_defaults_zero(self):
        """Device count exception falls back to 0 (lines 304-306)."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view(query_params={"apply_filters": "1", "librenms_location": "DC1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta:
                                        mock_meta.return_value = "meta_key"
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.side_effect = Exception("API error")
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                with patch(
                                                    "netbox_librenms_plugin.views.imports.list.logger"
                                                ) as mock_logger:
                                                    with patch.object(view, "_get_import_queryset", return_value=[]):
                                                        view.get(request)
                                                    mock_render.assert_called_once()
                                                    mock_logger.error.assert_called()
                                                    context = mock_render.call_args[0][2]
                                                    assert context["device_count"] == 0

    def test_get_cache_check_exception_continues(self):
        """Cache check exception is logged and processing continues (lines 293-294)."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view, request = self._make_view(query_params={"apply_filters": "1", "librenms_location": "DC1"})

        mock_api = MagicMock()
        mock_api.server_key = "default"

        with patch.object(LibreNMSImportView, "librenms_api", new_callable=lambda: property(lambda self: mock_api)):
            with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                mock_settings.objects.first.return_value = None
                mock_settings.objects.get_or_create.return_value = (None, False)

                with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                    mock_pref.return_value = None

                    mock_form_cls = MagicMock()
                    mock_form = MagicMock()
                    mock_form.is_valid.return_value = True
                    mock_form.cleaned_data = {
                        "enable_vc_detection": False,
                        "clear_cache": False,
                        "use_background_job": False,
                    }
                    mock_form_cls.return_value = mock_form
                    view.filterset_form = mock_form_cls

                    with patch("netbox_librenms_plugin.views.imports.list.render") as mock_render:
                        mock_render.return_value = MagicMock()

                        with patch("netbox_librenms_plugin.views.imports.list.DeviceImportTable"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.list.get_active_cached_searches"
                            ) as mock_searches:
                                mock_searches.return_value = []

                                with patch.object(view, "get_server_info", return_value={}):
                                    with patch(
                                        "netbox_librenms_plugin.import_utils.get_cache_metadata_key"
                                    ) as mock_meta:
                                        # Cache check raises exception
                                        mock_meta.side_effect = Exception("cache error")
                                        with patch(
                                            "netbox_librenms_plugin.import_utils.get_device_count_for_filters"
                                        ) as mock_count:
                                            mock_count.return_value = 5
                                            with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                                                mock_cache.get.return_value = None
                                                # Should not raise
                                                with patch.object(view, "_get_import_queryset", return_value=[]):
                                                    view.get(request)
                                                mock_render.assert_called_once()


class TestGetImportQuerysetFilterFields:
    """Tests for individual filter field branches in _get_import_queryset()."""

    def _make_view(self, filter_data=None):
        """Helper to create a configured view instance."""
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = object.__new__(LibreNMSImportView)
        view._job_results_loaded = False
        view._filters_submitted = True
        view._filter_warning = None
        view._filter_form_data = filter_data or {}
        view._vc_detection_enabled = False
        view._cache_cleared = False
        view._request = MagicMock()
        mock_api = MagicMock()
        mock_api.server_key = "default"
        view._librenms_api = mock_api
        view._use_sysname = True
        view._strip_domain = False
        return view

    def test_all_filter_fields_passed_to_process(self):
        """All 6 filter fields present in filter_data are forwarded to process_device_filters."""
        view = self._make_view(
            filter_data={
                "librenms_location": "DC1",
                "librenms_type": "network",
                "librenms_os": "ios",
                "librenms_hostname": "router01",
                "librenms_sysname": "sw1",
                "librenms_hardware": "Cisco",
                "enable_vc_detection": False,
                "clear_cache": False,
                "show_disabled": False,
                "exclude_existing": False,
            }
        )

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([], False)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.return_value = None

                    with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                        mock_cache.get.return_value = None

                        view._get_import_queryset()

                        call_kwargs = mock_process.call_args[1]
                        filters = call_kwargs["filters"]
                        assert filters["location"] == "DC1"
                        assert filters["type"] == "network"
                        assert filters["os"] == "ios"
                        assert filters["hostname"] == "router01"
                        assert filters["sysname"] == "sw1"
                        assert filters["hardware"] == "Cisco"

    def test_get_import_queryset_returns_empty_on_no_results(self):
        """_get_import_queryset returns [] when process_device_filters returns empty."""
        view = self._make_view(
            filter_data={
                "librenms_location": "DC1",
                "enable_vc_detection": False,
                "clear_cache": False,
            }
        )

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([], False)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    result = view._get_import_queryset()
                    assert result == []

    def test_settings_exception_in_get_import_queryset(self):
        """LibreNMSSettings exception in _get_import_queryset is caught (lines 475-477)."""
        view = self._make_view(
            filter_data={
                "librenms_location": "DC1",
                "enable_vc_detection": False,
                "clear_cache": False,
            }
        )

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([], False)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.side_effect = Exception("DB error")

                    with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                        mock_cache.get.return_value = None
                        # Should not raise
                        result = view._get_import_queryset()
                        assert result == []

    def test_cache_metadata_found_sets_timestamps(self):
        """When cache metadata is found, timestamps are set (lines 523-527)."""
        mock_device = {"device_id": 1, "_validation": {}}
        view = self._make_view(
            filter_data={
                "librenms_location": "DC1",
                "enable_vc_detection": True,
                "clear_cache": False,
            }
        )

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([mock_device], False)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.return_value = None

                    with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                        mock_cache.get.return_value = {
                            "cached_at": "2024-01-01T00:00:00",
                            "cache_timeout": 600,
                        }

                        with patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key") as mock_meta_key:
                            mock_meta_key.return_value = "meta_key"

                            result = view._get_import_queryset()
                            assert len(result) == 1
                            assert view._cache_timestamp == "2024-01-01T00:00:00"
                            assert view._cache_timeout == 600
                            # _vc_detection_enabled is propagated to device validation
                            assert result[0]["_validation"]["_vc_detection_enabled"] is True

    def test_cache_metadata_missing_sets_flag(self):
        """When cache metadata is absent, _cache_metadata_missing is set True."""
        mock_device = {"device_id": 1, "_validation": {}}
        view = self._make_view(
            filter_data={
                "librenms_location": "DC1",
                "enable_vc_detection": False,
                "clear_cache": False,
            }
        )

        with patch("netbox_librenms_plugin.views.imports.list.process_device_filters") as mock_process:
            mock_process.return_value = ([mock_device], True)

            with patch("netbox_librenms_plugin.views.imports.list.get_user_pref") as mock_pref:
                mock_pref.return_value = None

                with patch("netbox_librenms_plugin.views.imports.list.LibreNMSSettings") as mock_settings:
                    mock_settings.objects.first.return_value = None

                    with patch("netbox_librenms_plugin.views.imports.list.cache") as mock_cache:
                        # Cache metadata not found
                        mock_cache.get.return_value = None

                        with patch("netbox_librenms_plugin.import_utils.get_cache_metadata_key") as mock_meta_key:
                            mock_meta_key.return_value = "meta_key"

                            view._get_import_queryset()
                            assert view._cache_metadata_missing is True
