"""
Coverage tests for import_utils/bulk_import.py.

Exercises error paths, cancellation flows, cache behaviour,
and edge cases in bulk_import_devices_shared and process_device_filters.
"""

from unittest.mock import MagicMock, patch

import pytest

# Shared real-DB builders (see tests/conftest.py).
from netbox_librenms_plugin.tests.conftest import delete_keeping_pk, make_device, make_vm

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_job(logger=True):
    """
    Return a minimal JobRunner-like mock.

    Args:
        logger: If True (default) attach a MagicMock logger; if False set it
                to None so the ``else: logger.warning(...)`` branches fire.
    """
    job = MagicMock()
    job.job = MagicMock()
    job.job.job_id = "test-uuid-1234"
    job.job.pk = 1
    job.logger = MagicMock() if logger else None
    return job


def _make_rq_running():
    """RQ job that is actively running (not stopped/failed)."""
    rq_job = MagicMock()
    rq_job.is_stopped = False
    rq_job.is_failed = False
    rq_job.get_status.return_value = "started"
    return rq_job


def _make_rq_stopped():
    """RQ job that has been stopped."""
    rq_job = MagicMock()
    rq_job.is_stopped = True
    rq_job.is_failed = False
    rq_job.get_status.return_value = "stopped"
    return rq_job


def _make_validation(existing_device=None, import_as_vm=False, issues=None):
    """Minimal valid validation dict used throughout the tests."""
    return {
        "resolved_name": "test-device",
        "is_ready": True,
        "can_import": True,
        "status": "active",
        "existing_device": existing_device,
        "import_as_vm": import_as_vm,
        "existing_match_type": None,
        "virtual_chassis": {"is_stack": False},
        "site": {"found": True, "site": MagicMock()},
        "device_type": {"found": True, "device_type": MagicMock()},
        "device_role": {"found": True, "role": MagicMock()},
        "platform": {"found": True, "platform": MagicMock()},
        "cluster": {"found": True},
        "issues": issues or [],
    }


def _make_import_result(success=True, device=None, message="Imported", error=None):
    """Return value for mocked ``import_single_device``."""
    return {
        "success": success,
        "device": device or (MagicMock() if success else None),
        "message": message,
        "error": error or ("" if success else "Import failed"),
    }


# ===========================================================================
# 1. TestBulkImportDevices# ===========================================================================


class TestBulkImportDevices:
    """Tests for the thin ``bulk_import_devices`` wrapper."""

    def test_delegates_to_shared_with_job_none(self):
        """bulk_import_devices must call bulk_import_devices_shared with job=None."""
        with patch("netbox_librenms_plugin.import_utils.bulk_import.bulk_import_devices_shared") as mock_shared:
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

            expected = {
                "total": 2,
                "success": [],
                "failed": [],
                "skipped": [],
                "virtual_chassis_created": 0,
            }
            mock_shared.return_value = expected
            user = MagicMock()

            result = bulk_import_devices(
                device_ids=[1, 2],
                server_key="default",
                sync_options={"use_sysname": True},
                manual_mappings_per_device={1: {"device_role_id": 5}},
                libre_devices_cache={1: {"hostname": "test"}},
                user=user,
            )

        mock_shared.assert_called_once_with(
            device_ids=[1, 2],
            server_key="default",
            sync_options={"use_sysname": True},
            manual_mappings_per_device={1: {"device_role_id": 5}},
            libre_devices_cache={1: {"hostname": "test"}},
            job=None,
            user=user,
        )
        assert result == expected


# ===========================================================================
# 2. TestBulkImportDevicesShared#    183, 203-254
# ===========================================================================


class TestBulkImportDevicesShared:
    """Tests for ``bulk_import_devices_shared``."""

    @pytest.fixture(autouse=True)
    def _stub_norm_preload(self):
        """bulk_import_devices_shared preloads device_type NormalizationRule once (issue #90); these are mock-based (no DB), so stub the preload to avoid real DB access."""
        with patch(
            "netbox_librenms_plugin.import_utils.bulk_import.preload_normalization_rules",
            return_value={},
        ):
            yield

    # ------------------------------------------------------------------
    # Lines 129 & 140 – "else: logger.warning(...)" when job.logger=None
    # ------------------------------------------------------------------

    def test_rq_stopped_logs_via_module_logger_when_job_logger_none(self):
        """job.logger=None: module logger.warning fires on RQ stop."""
        job = _make_job(logger=False)  # job.logger is None → else branch
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        mock_import.assert_not_called()
        mock_logger.warning.assert_called()
        assert result["success"] == []

    def test_rq_unavailable_does_not_cancel_import(self):
        """When RQ/Redis is unavailable, _is_job_cancelled returns False → import continues."""
        job = _make_job(logger=False)  # job.logger is None
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("django_rq.get_queue", side_effect=Exception("RQ unavailable")),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        mock_import.assert_called()

    # ------------------------------------------------------------------
    # Lines 146-147 – libre_devices_cache hit path
    # ------------------------------------------------------------------

    def test_libre_devices_cache_hit_skips_api_call(self):
        """Devices in libre_devices_cache skip the API call."""
        libre_cache = {
            1: {"device_id": 1, "hostname": "cached-host"},
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI") as mock_api_cls,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
        ):
            mock_api = MagicMock()
            mock_api.server_key = "default"
            mock_api_cls.return_value = mock_api

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # API.get_device_info should NOT have been called for this device
        mock_api.get_device_info.assert_not_called()
        assert len(result["success"]) == 1

    # ------------------------------------------------------------------
    # Line 183 – job.logger.info("Imported device X of Y")
    # ------------------------------------------------------------------

    def test_successful_import_emits_job_progress_log(self):
        """job.logger.info('Imported device X of Y') on success."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["success"]) == 1
        job.logger.info.assert_any_call("Imported device 1 of 1")

    # ------------------------------------------------------------------
    # Lines 203-254 – virtual chassis creation (is_stack=True)
    # ------------------------------------------------------------------

    def test_vc_creation_triggered_for_stack(self):
        """is_stack=True → create_virtual_chassis_with_members called."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_device = MagicMock()
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [
                {"serial": "SN001", "position": 1},
                {"serial": "SN002", "position": 2},
            ],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(device=mock_device),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1
        assert len(result["success"]) == 1

    def test_vc_creation_with_job_logger(self):
        """VC creation with job → job.logger.info logged."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert result["virtual_chassis_created"] == 1
        # Confirm job logger was called for the VC creation
        assert any("VC" in c.args[0] for c in job.logger.info.call_args_list if c.args)

    def test_vc_creation_deduplicates_by_member_serials(self):
        """Two devices with identical member serials → VC created only once."""
        libre_cache = {
            1: {"device_id": 1, "hostname": "stack-1"},
            2: {"device_id": 2, "hostname": "stack-2"},
        }
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"

        # Both devices share the same physical stack (same member serials)
        shared_vc_data = {
            "is_stack": True,
            "members": [
                {"serial": "SN001", "position": 1},
                {"serial": "SN002", "position": 2},
            ],
        }
        v1 = _make_validation()
        v1["virtual_chassis"] = shared_vc_data
        v2 = _make_validation()
        v2["virtual_chassis"] = shared_vc_data

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=[v1, v2],
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1, 2],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        assert mock_create_vc.call_count == 1
        assert result["virtual_chassis_created"] == 1

    def test_vc_creation_failure_continues_import(self):
        """VC creation exception → import device still succeeds."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                side_effect=Exception("VC creation failed"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # Import succeeded despite VC failure
        assert len(result["success"]) == 1
        assert result["virtual_chassis_created"] == 0
        mock_create_vc.assert_called_once()

    def test_vc_creation_skipped_without_vc_permission(self):
        """User lacks dcim.add_virtualchassis → stack device import fails fast (PR #257)."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}, {"serial": "SN002", "position": 2}],
        }

        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_virtualchassis"

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ) as mock_import,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_not_called()
        mock_import.assert_not_called()
        assert len(result["success"]) == 0
        assert len(result["failed"]) == 1
        assert "dcim.add_virtualchassis" in result["failed"][0]["error"]
        assert result["virtual_chassis_created"] == 0

    def test_vc_creation_skipped_without_permission_logs_job_warning(self):
        """Missing VC permission with job context logs error via job.logger (PR #257)."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_virtualchassis"

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
            ) as mock_create_vc,
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                job=job,
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_not_called()
        job.logger.error.assert_called()
        assert len(result["success"]) == 0
        assert len(result["failed"]) == 1
        assert "dcim.add_virtualchassis" in result["failed"][0]["error"]

    def test_vc_with_no_members_falls_back_to_device_id_domain(self):
        """No serials and no member fingerprint triggers device-id vc_domain fallback."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {"is_stack": True, "members": []}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=MagicMock(name="vc"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1

    def test_vc_creation_proceeds_with_vc_permission(self):
        """User has dcim.add_virtualchassis → VC creation proceeds normally."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}, {"serial": "SN002", "position": 2}],
        }

        user = MagicMock()
        user.has_perm.return_value = True  # all perms granted

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1

    def test_vc_no_member_serials_uses_device_id_domain(self):
        """Members with no valid serials → vc_domain falls back to device_id."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        mock_vc = MagicMock()
        mock_vc.name = "VC-1"

        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [
                {"serial": None, "position": 1},
                {"serial": "-", "position": 2},
            ],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        mock_create_vc.assert_called_once()
        assert result["virtual_chassis_created"] == 1

    def test_failed_import_with_job_logs_error(self):
        """result.success=False, device=None → job.logger.error called."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(success=False, device=None, error="Import failed"),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_queue = MagicMock()
            mock_get_queue.return_value = mock_queue
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["failed"]) == 1
        job.logger.error.assert_called()

    def test_manual_mappings_applied_to_device(self):
        """manual_mappings_per_device overrides are applied for the matching device."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        captured_mappings = {}

        def capture_import(device_id, server_key, validation, sync_options, manual_mappings, libre_device):
            captured_mappings.update(manual_mappings or {})
            return _make_import_result()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                side_effect=capture_import,
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
                manual_mappings_per_device={1: {"device_role_id": 42}},
            )

        assert result["success"]
        assert captured_mappings.get("device_role_id") == 42

    def test_device_skipped_when_already_exists(self):
        """result.success=False, result.device is truthy → device skipped."""
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        existing_device = MagicMock()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(success=False, device=existing_device, error="Device already exists"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "Device already exists"
        assert result["failed"] == []

    def test_vc_creation_failure_with_job_logs_warning(self):
        """VC failure with job.logger set → job.logger.warning fired."""
        job = _make_job()
        libre_cache = {1: {"device_id": 1, "hostname": "test"}}
        validation = _make_validation()
        validation["virtual_chassis"] = {
            "is_stack": True,
            "members": [{"serial": "SN001", "position": 1}],
        }

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value=_make_import_result(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                side_effect=Exception("VC error"),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                job=job,
                libre_devices_cache=libre_cache,
            )

        assert len(result["success"]) == 1
        assert result["virtual_chassis_created"] == 0
        job.logger.warning.assert_called()


# ===========================================================================
# 3. TestRefreshExistingDevice#    400-402, 420-421
# ===========================================================================


@pytest.mark.django_db
class TestRefreshExistingDevice:
    """Real-DB coverage for ``_refresh_existing_device``."""

    @staticmethod
    def _device_validation(**overrides):
        """Baseline device validation dict shaped like ``validate_device_for_import`` output."""
        base = {
            "existing_device": None,
            "import_as_vm": False,
            "issues": [],
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": False, "role": None, "available_roles": []},
        }
        base.update(overrides)
        return base

    @staticmethod
    def _vm_validation(**overrides):
        """Baseline VM validation dict (recalculate is_vm=True reads cluster + issues)."""
        base = {
            "existing_device": None,
            "import_as_vm": True,
            "issues": [],
            "site": {"found": True},
            "cluster": {"found": False, "cluster": None, "available_clusters": []},
        }
        base.update(overrides)
        return base

    # ------------------------------------------------------------------
    # Existing-device refresh (object still present)
    # ------------------------------------------------------------------

    def test_device_path_refreshes_role(self):
        """A non-VM existing device is refreshed from the DB and its current role applied."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-role-dev")
        validation = self._device_validation(existing_device=dev, device_role={})

        _refresh_existing_device(validation)

        assert validation["existing_device"].pk == dev.pk
        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] == dev.role
        # A matched existing device must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_device_path_refreshes_no_role(self):
        """Defensive branch: a refreshed device with no role → device_role={'found': False}."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock(pk=2)
        refreshed = MagicMock(role=None)
        validation = self._device_validation(existing_device=existing, device_role={"found": True, "role": MagicMock()})

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            _refresh_existing_device(validation)

        assert validation["existing_device"] is refreshed
        assert validation["device_role"] == {"found": False, "role": None, "available_roles": []}

    def test_duplicate_hostname_across_sites_fails_closed(self):
        """A hostname matching two NetBox devices in different sites fails closed (ambiguous), not bound to an arbitrary one."""
        from dcim.models import Device, Site
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        # Two devices share the name "core-sw1" but live in DIFFERENT sites — NetBox allows this
        # (name is unique only per-site), so a bare .first() would bind an arbitrary one.
        dev_a = make_device("core-sw1")  # shared infra site
        site_b = Site.objects.create(name="crfix-site-b", slug="crfix-site-b")
        Device.objects.create(
            name="core-sw1",
            device_type=dev_a.device_type,
            role=dev_a.role,
            site=site_b,
            status="active",
        )

        validation = self._device_validation(resolved_name="core-sw1")
        libre_device = {"device_id": 999, "hostname": "core-sw1", "sysName": "core-sw1"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Fail closed: no arbitrary existing_device bound; the row is blocked + marked ambiguous,
        # matching validate_device_for_import()'s duplicate-hostname behaviour.
        assert validation.get("existing_device") is None
        assert validation["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        assert any("hostname/serial" in i for i in validation["issues"])

    def test_neutralized_link_but_other_match_type_keeps_block(self):
        """A non-librenms cached match (hostname) must survive the linkage refresh: only a neutralized librenms/OOB link triggers re-evaluation, so the device stays matched and the row stays blocked."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-hostname-dev")
        validation = self._device_validation(
            existing_device=dev,
            existing_match_type="hostname",  # not librenms-based
            device_role={"found": True, "role": dev.role},
        )

        _refresh_existing_device(validation, libre_device={"device_id": 1}, server_key="default")

        # hostname match preserved → device kept (not cleared) and the row stays blocked.
        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "hostname"
        assert validation["can_import"] is False

    def test_neutralized_librenms_link_clears_match_and_reevaluates(self):
        """A cached ``librenms_id`` match whose link is gone in NetBox (the device carries no librenms_id custom field anymore) must be dropped and the row re-evaluated under current rules — not left wearing the stale "existing" badge until cache expiry."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        # Real device with NO librenms_id CF → linkage finds no host/OOB id → neutralizes.
        dev = make_device("ref-unlinked")
        validation = self._device_validation(
            existing_device=dev,
            existing_match_type="librenms_id",
            device_role={"found": True, "role": dev.role},
            resolved_name="router1",  # does not match dev.name → no fresh re-match
        )
        libre_device = {"device_id": 50, "hostname": "router1", "sysName": "router1"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Stale librenms match dropped — no "existing" badge — and the row is re-evaluated as a
        # fresh new import: now gated only on the role-selection blocker, not the vanished match.
        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert any("role" in issue.lower() for issue in validation["issues"])
        assert validation["can_import"] is False

    def test_missing_scanned_id_preserves_live_librenms_link(self):
        """A missing scanned device_id is NOT proof the link disappeared."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        dev = make_device("ref-live-link", librenms_cf={"default": {"id": 50}})
        validation = self._device_validation(existing_device=dev, existing_match_type="librenms_id")

        # libre_device with no usable device_id → scanned_id is None.
        _refresh_librenms_linkage(validation, dev, {"hostname": "h"}, "default")

        assert validation["existing_match_type"] == "librenms_id"

    def test_missing_scanned_id_with_gone_link_clears_match(self):
        """The companion case: when scanned_id is None AND the DB linkage is genuinely gone (no host_id, no oob_id), the stale librenms badge IS dropped."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        dev = make_device("ref-gone-link")  # no librenms_id CF → host_id/oob_id both None
        validation = self._device_validation(existing_device=dev, existing_match_type="librenms_id")

        _refresh_librenms_linkage(validation, dev, {"hostname": "h"}, "default")

        assert validation["existing_match_type"] is None

    # ------------------------------------------------------------------
    # Deleted match → readiness recompute
    # ------------------------------------------------------------------

    def test_deleted_device_clears_existing_and_recomputes_readiness(self):
        """Device deleted since caching → existing_device=None, role reset, readiness recomputed."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-deleted")
        validation = self._device_validation(existing_device=dev, device_role={"found": True})
        delete_keeping_pk(dev)

        _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["device_role"] == {"found": False, "role": None, "available_roles": []}
        # The dropped match is back in the "new import" path with no role selected, so the
        # create-time role blocker is re-asserted (matching the fresh-lookup path) — can_import
        # is False, not just is_ready. (Pre-fix this branch left it importable when libre_device
        # was None, inconsistent with the same scenario when libre_device is present.)
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        assert any("role" in issue.lower() for issue in validation["issues"])

    def test_deleted_device_clears_stale_match_derived_actions(self):
        """When the matched device is deleted, serial/OOB/merge/promote actions derived from it must be cleared so the UI can't offer actions on a now-gone device."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-deleted-actions")
        validation = self._device_validation(
            existing_device=dev,
            device_role={"found": True},
            # Stale actions produced from the now-deleted match:
            serial_action="merge_netbox_devices",
            oob_candidate={"device": dev, "type": "idrac"},
            promote_to_host={"existing_libre_id": 9},
            merge_candidates={"host_named": {"pk": 4}, "oob_named": {"pk": 5}},
            serial_role_choice_available=True,
        )
        delete_keeping_pk(dev)

        _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["serial_action"] is None
        assert validation["oob_candidate"] is None
        assert validation["serial_role_choice_available"] is False
        assert "promote_to_host" not in validation
        assert "merge_candidates" not in validation

    def test_deleted_vm_match_clears_stale_cluster_selection(self):
        """A dropped cached VM match must reset the stale cluster selection (preserving available_clusters), mirroring the device_role reset on the device path — otherwise the match-derived cluster.found=True survives and recalculate keeps the row "ready" even though a new VM import requires a fresh cluster."""
        from virtualization.models import Cluster, ClusterType

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = make_vm("ref-vm-deleted")
        stale_cluster = vm.cluster
        other_type, _ = ClusterType.objects.get_or_create(name="RefCType", slug="ref-ctype")
        other_cluster = Cluster.objects.create(name="RefCluster-B", type=other_type)
        available = [stale_cluster, other_cluster]
        validation = self._vm_validation(
            existing_device=vm,
            cluster={"found": True, "cluster": stale_cluster, "available_clusters": available},
        )
        delete_keeping_pk(vm)

        _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        # Cluster selection reset to unselected, available list preserved for the dropdown.
        assert validation["cluster"]["found"] is False
        assert validation["cluster"]["cluster"] is None
        assert validation["cluster"]["available_clusters"] == available
        # found=False feeds is_ready (is_vm=True), so the row can't slip through without a
        # fresh cluster choice.
        assert validation["is_ready"] is False

    def test_deleted_vm_recomputes_readiness_from_cluster(self):
        """VM deleted → the match-derived cluster is reset (found=False) and the row is back in the new-import path."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = make_vm("ref-vm-recompute")
        validation = self._vm_validation(existing_device=vm, cluster={"found": True})
        delete_keeping_pk(vm)

        _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["can_import"] is False  # cluster blocker re-asserted
        assert validation["is_ready"] is False  # not ready until a fresh cluster is selected
        assert any("cluster" in issue.lower() for issue in validation["issues"])

    def test_deleted_vm_not_ready_when_no_cluster(self):
        """Deleted VM with a blocking issue → can_import False and is_ready False."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = make_vm("ref-vm-nocluster")
        validation = self._vm_validation(existing_device=vm, issues=["some issue"], cluster={"found": False})
        delete_keeping_pk(vm)

        _refresh_existing_device(validation)

        assert validation["can_import"] is False  # has issues
        assert validation["is_ready"] is False

    # ------------------------------------------------------------------
    # Fresh lookup (existing_device was None at cache time)
    # ------------------------------------------------------------------

    def test_fresh_lookup_matches_by_serial_blocks_import(self):
        """The refresh re-check uses validate_device_for_import's breadth: a row with no librenms_id/name match must still be blocked when a NetBox device matches by hardware serial, otherwise it flips to importable and creates a duplicate."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-serial-dev", serial="ABC123")
        validation = self._device_validation()
        # device_id has no CF match; hostname/sysName don't match dev.name → serial is the hook.
        libre_device = {"device_id": 50, "hostname": "h", "sysName": "h", "serial": "ABC123"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "serial"
        assert validation["can_import"] is False

    def test_fresh_lookup_no_match_does_not_requery_librenms_id(self):
        """The no-match refresh path must not re-run find_by_librenms_id in the name fallback.

        The cross-model collision check already resolves the id against both models (2 queries);
        _lookup_in_model then does name-only fallbacks. Before the fix it re-queried
        find_by_librenms_id inside each _lookup_in_model call (4 total) for no result.
        """
        from unittest.mock import MagicMock, patch

        import netbox_librenms_plugin.import_utils.bulk_import as bi
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = self._device_validation()
        # device_id matches no CF; hostname/sysName match no device name; no serial/ip → no match.
        libre_device = {"device_id": 999142, "hostname": "no-such-host-xyzzy", "sysName": "no-such-host-xyzzy"}

        spy = MagicMock(side_effect=bi.find_by_librenms_id)  # real call, just counted
        with patch.object(bi, "find_by_librenms_id", spy):
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is None
        # Exactly the two cross-model collision-check queries (Device + VM); no redundant re-query.
        assert spy.call_count == 2, f"expected 2 find_by_librenms_id calls, got {spy.call_count}"

    def test_fresh_lookup_no_role_clears_stale_role_blocker(self):
        """When a fresh lookup resolves a previously-unmatched row to an existing device, the stale "Device role must be manually selected" blocker must be cleared so it doesn't linger in the UI (the row is force-blocked as an existing match regardless)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-serial-role", serial="ABC124")
        validation = self._device_validation(issues=["Device role must be manually selected before import"])
        libre_device = {"device_id": 51, "hostname": "h", "sysName": "h", "serial": "ABC124"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        # The stale role-blocker issue must have been removed.
        assert all("role" not in issue.lower() for issue in validation["issues"])

    def test_fresh_lookup_clears_stale_site_and_device_type_blockers(self):
        """A previously-unmatched row can carry create-time "No matching site…" / "No matching device type…" blockers (device_operations.py)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-serial-site-dt", serial="ABC125")
        validation = self._device_validation(
            issues=[
                "No matching site found for location: 'BasementX'",
                "No matching device type found for hardware: 'WidgetX'",
            ],
        )
        libre_device = {"device_id": 52, "hostname": "h", "sysName": "h", "serial": "ABC125"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        # Both stale create-time blockers cleared.
        assert all("site" not in issue.lower() for issue in validation["issues"])
        assert all("device type" not in issue.lower() for issue in validation["issues"])
        assert validation["can_import"] is False

    def test_fresh_lookup_vm_clears_stale_cluster_blocker(self):
        """A VM row resolves to an existing VM via a fresh name match (actual_is_vm=True)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = make_vm("ref-vm-cluster-clear")
        validation = self._vm_validation(
            issues=["Cluster must be manually selected before importing as VM"],
        )
        # No librenms_id CF match; the VM is found by resolved_name → Model=VirtualMachine,
        # found_as_cross_model=False → actual_is_vm=True (the branch that cleared nothing).
        validation["resolved_name"] = vm.name
        libre_device = {"device_id": 60, "hostname": "no-match", "sysName": "no-match"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == vm.pk
        assert validation["import_as_vm"] is True
        # The stale cluster-blocker issue must have been removed.
        assert all("cluster" not in issue.lower() for issue in validation["issues"])
        # An existing match is never import-ready.
        assert validation["can_import"] is False

    def test_cross_model_librenms_id_collision_blocks_import(self):
        """The same librenms_id assigned to BOTH a Device and a VirtualMachine is ambiguous: validate_device_for_import() blocks it as a duplicate, so the refresh re-check must agree."""
        from netbox_librenms_plugin.import_utils.bulk_import import (
            _AMBIGUOUS_LIBRENMS_ID_MARKER,
            _refresh_existing_device,
        )

        make_device("collide-dev", librenms_cf={"default": {"id": 77}})
        vm = make_vm("collide-vm")
        vm.custom_field_data["librenms_id"] = {"default": {"id": 77}}
        vm.save()

        # Device path (import_as_vm=False) → Model=Device, CrossModel=VirtualMachine. The id 77
        # resolves in BOTH, so the lookup must fail closed rather than bind to the Device.
        validation = self._device_validation(resolved_name="collide-dev")
        libre_device = {"device_id": 77, "hostname": "collide-dev", "sysName": "collide-dev"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["ambiguous_librenms_id"] is True
        assert validation["existing_match_type"] == "ambiguous_librenms_id"
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        assert any(_AMBIGUOUS_LIBRENMS_ID_MARKER in issue for issue in validation["issues"])

    def test_vanished_link_with_no_libre_device_stays_blocked(self):
        """Fail-closed: a cached librenms_id match whose link vanished in NetBox, refreshed with libre_device=None, drops the match and recomputes readiness — then the fresh lookup early-returns (no libre_device)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("vanished-link-dev")  # no librenms_id CF → linkage re-derive finds no link
        validation = self._device_validation(existing_device=dev, existing_match_type="librenms_id")

        _refresh_existing_device(validation, libre_device=None, server_key="default")

        # Match dropped (link gone) + no role selected ⇒ must stay blocked, with the role blocker
        # present (recomputed readiness alone, without the re-assert, would let it go importable).
        assert validation["can_import"] is False
        assert any("role" in issue.lower() for issue in validation["issues"])

    def test_fresh_lookup_matches_by_primary_ip_blocks_import(self):
        """As above, but the existing NetBox device is reachable only via its management IP — the refresh must catch it (interface-assigned IP → device) and block the import."""
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-ip-dev")
        iface = Interface.objects.create(device=dev, name="mgmt0", type="1000base-t")
        # A decoy row sharing the address, created FIRST and assigned to nothing — so .first()
        # returns it and the match must come from scanning the whole set, not just the first row.
        IPAddress.objects.create(address="10.0.0.9/24")
        IPAddress.objects.create(address="10.0.0.9/24", assigned_object=iface)
        validation = self._device_validation()
        libre_device = {"device_id": 52, "hostname": "h2", "sysName": "h2", "ip": "10.0.0.9"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "primary_ip"
        assert validation["can_import"] is False

    def test_fresh_lookup_matches_by_oob_ip_blocks_import(self):
        """The refresh must also catch a device reachable only via its oob_ip (no interface assignment), mirroring validate_device_for_import()."""
        from ipam.models import IPAddress

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-oob-ip-dev")
        # A decoy row sharing the same host address, created FIRST so .first() returns it — it is
        # NOT anyone's oob_ip, so the match must come from the FULL matching-IP set, not .first().
        IPAddress.objects.create(address="10.0.0.19/24")
        oob_ip = IPAddress.objects.create(address="10.0.0.19/24")  # assigned to no interface
        dev.oob_ip = oob_ip
        dev.save()
        validation = self._device_validation()
        libre_device = {"device_id": 53, "hostname": "h3", "sysName": "h3", "ip": "10.0.0.19"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "primary_ip"
        assert validation["can_import"] is False

    def test_no_existing_device_found_by_librenms_id(self):
        """existing=None: a device now carrying the scanned librenms_id host CF is re-matched."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("sw01", librenms_cf={"default": {"id": 42}})
        libre_device = {"device_id": 42, "hostname": "sw01", "sysName": "sw01"}
        validation = self._device_validation(resolved_name="sw01")

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "librenms_id"
        # Late-found existing match must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        # Device has a role → device_role should be set.
        assert validation["device_role"]["found"] is True

    def test_no_existing_device_found_as_oob_reclassifies_and_links(self):
        """existing=None at cache time, but the scanned LibreNMS id is now linked as a device's OOB controller: refresh must reclassify the match as 'librenms_oob' and populate existing_librenms_link (the stale-OOB-badge fix), DB-only."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device(
            "idrac-host",
            librenms_cf={"default": {"id": 99, "oob": {"id": 42, "type": "drac"}}},
        )
        libre_device = {"device_id": 42, "hostname": "10.0.0.5", "sysName": "idrac-x"}
        validation = self._device_validation(
            existing_match_type=None, existing_librenms_link=None, resolved_name="idrac-x"
        )

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Matched via the OOB sub-key → librenms_oob, not plain librenms_id.
        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "librenms_oob"
        assert validation["existing_librenms_link"] == {"host_id": 99, "oob_id": 42, "oob_type": "drac"}
        # A late-found existing match must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_no_existing_device_found_by_resolved_name(self):
        """existing=None: not matched by librenms_id, but matched by resolved_name."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("sw02-resolved")
        libre_device = {"device_id": 43, "hostname": "sw02", "sysName": "sw02"}
        validation = self._device_validation(resolved_name="sw02-resolved")

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "resolved_name"
        assert validation["can_import"] is False

    def test_no_existing_device_non_numeric_librenms_id_skips_id_lookup(self):
        """A non-numeric device_id is coerced to None (no id lookup), so the row still matches by resolved_name rather than crashing on int('not-an-int')."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("sw05")
        libre_device = {"device_id": "not-an-int", "hostname": "sw05", "sysName": "sw05"}
        validation = self._device_validation(resolved_name="sw05")

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Found via resolved_name; the non-numeric id neither crashed nor mis-matched.
        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "resolved_name"

    def test_no_existing_device_hostname_fallback(self):
        """existing=None, not matched by id or resolved_name → hostname fallback."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("sw04")
        libre_device = {"device_id": 45, "hostname": "sw04", "sysName": "sw04-sysname"}
        validation = self._device_validation(resolved_name=None)

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "hostname"

    def test_no_existing_device_no_libre_device_returns_early(self):
        """existing=None + libre_device=None → immediate return, validation untouched."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = {"existing_device": None}
        _refresh_existing_device(validation, libre_device=None)
        assert validation == {"existing_device": None}

    # ------------------------------------------------------------------
    # Error handling (exception simulation — justified mocks)
    # ------------------------------------------------------------------

    def test_exception_during_refresh_logs_error(self):
        """A DB error while refreshing an existing device is caught and logged, not raised."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock(pk=99)
        validation = {"existing_device": existing, "import_as_vm": False}

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            mock_Device.objects.filter.side_effect = Exception("DB down")
            _refresh_existing_device(validation)  # must not raise

        mock_logger.error.assert_called_once()
        # Don't pin the pk to the format string only: a switch to parameterized logging
        # (logger.error("... %s", pk)) would move "99" into a later positional arg while
        # still logging correctly. Check every positional arg instead.
        args = mock_logger.error.call_args.args
        assert any("99" in str(arg) for arg in args)

    def test_exception_during_new_device_lookup_logs_error(self):
        """An exception in the newly-imported-device check is caught and logged (forced)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        libre_device = {"device_id": 44, "hostname": "sw03", "sysName": "sw03"}
        validation = {"existing_device": None, "import_as_vm": False, "resolved_name": None}

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                side_effect=Exception("lookup failed"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        mock_logger.error.assert_called()


# ===========================================================================
# 4. TestProcessDeviceFilters#    548-566, 596-598, 604-641, 665, 687-694
# ===========================================================================


class TestProcessDeviceFilters:
    """Tests for ``process_device_filters``."""

    # Minimal set of patches required for every call
    _BASE_PATCHES = [
        "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
        "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
        "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
        "netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data",
        "netbox_librenms_plugin.import_utils.bulk_import.cache",
        "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
        "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
        "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
    ]

    def _make_api(self, server_key="default", cache_timeout=300):
        api = MagicMock()
        api.server_key = server_key
        api.cache_timeout = cache_timeout
        return api

    def _make_device(self, device_id=1, hostname="sw01"):
        return {"device_id": device_id, "hostname": hostname, "disabled": 0}

    # ------------------------------------------------------------------
    # Lines 469, 489: job logger on fetch and device-count messages
    # ------------------------------------------------------------------

    def test_job_logs_fetch_and_count_messages(self):
        """With job set, info logs for 'Fetching' and 'Found N devices' fire."""
        job = _make_job()
        device = self._make_device()
        api = self._make_api()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert len(result) == 1
        # Verify the two job-specific log calls were made
        log_calls = [c.args[0] for c in job.logger.info.call_args_list]
        assert any("Fetching" in s for s in log_calls)
        assert any("Found" in s for s in log_calls)

    # ------------------------------------------------------------------
    # Lines 495-506: VC prefetch with job logging
    # ------------------------------------------------------------------

    def test_vc_prefetch_with_job_logs_prefetch_messages(self):
        """With vc_detection_enabled+job, prefetch job-log messages fire."""
        job = _make_job()
        device = self._make_device()
        api = self._make_api()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices") as mock_prefetch,
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        mock_prefetch.assert_called_once()
        log_calls = [c.args[0] for c in job.logger.info.call_args_list]
        assert any("Pre-fetch" in s or "pre-fetch" in s or "virtual chassis" in s.lower() for s in log_calls)

    # ------------------------------------------------------------------
    # Lines 507-511: BrokenPipeError during VC prefetch + request set
    # ------------------------------------------------------------------

    def test_vc_prefetch_client_disconnect_with_request_returns_empty(self):
        """BrokenPipeError during prefetch + request set → _empty_return."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=False,
                show_disabled=True,
                request=request,
            )

        assert result == []

    def test_vc_prefetch_client_disconnect_with_return_cache_status(self):
        """BrokenPipeError + request + return_cache_status=True → ([], False)."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=True,
                clear_cache=False,
                show_disabled=True,
                request=request,
                return_cache_status=True,
            )

        assert result == ([], False)

    def test_vc_prefetch_client_disconnect_no_request_reraises(self):
        """BrokenPipeError during prefetch with request=None → exception re-raised."""

        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices",
                side_effect=BrokenPipeError("client gone"),
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            with pytest.raises(BrokenPipeError):
                process_device_filters(
                    api,
                    filters={},
                    vc_detection_enabled=True,
                    clear_cache=False,
                    show_disabled=True,
                    request=None,
                )

    # ------------------------------------------------------------------
    # Lines 520-531: Job pre-loop RQ check → job was already stopped
    # ------------------------------------------------------------------

    def test_job_rq_stopped_before_validation_loop_returns_empty(self):
        """RQ job stopped before loop → empty result returned."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []
        job.logger.warning.assert_called()

    def test_job_rq_stopped_before_loop_with_cache_status(self):
        """RQ job stopped + return_cache_status=True → ([], False)."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_stopped()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
                return_cache_status=True,
            )

        assert result == ([], False)

    # ------------------------------------------------------------------
    # Lines 532-537: Job pre-loop RQ raises → DB fallback → stopped
    # ------------------------------------------------------------------

    def test_job_cancelled_before_validation_loop_returns_empty(self):
        """Job cancelled at pre-loop check → returns empty list."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import._is_job_cancelled", return_value=True),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    def test_rq_unavailable_job_not_cancelled_in_preloop(self):
        """RQ unavailable before loop → _is_job_cancelled returns False → processing continues."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue", side_effect=Exception("RQ down")),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert len(result) == 1

    # ------------------------------------------------------------------
    # Lines 548-560: Per-device loop RQ stop detected
    # ------------------------------------------------------------------

    def test_job_validation_loop_rq_stop_returns_empty(self):
        """RQ stop detected during loop at idx=1 → return empty."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_get_queue.return_value = MagicMock()
            # _is_job_cancelled is called 4 times before the in-loop check:
            # pre-fetch, pre-VC, pre-validation-loop, then once per device.
            mock_rq_cls.fetch.side_effect = [
                _make_rq_running(),
                _make_rq_running(),
                _make_rq_running(),
                _make_rq_stopped(),
            ]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    # ------------------------------------------------------------------
    # Lines 561-566: Per-device loop DB fallback stop
    # ------------------------------------------------------------------

    def test_job_cancelled_in_validation_loop_returns_empty(self):
        """Job cancellation detected during loop → returns empty list."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import._is_job_cancelled",
                side_effect=[False, False, False, True],  # 3 pre-loop checks pass, in-loop check cancels
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        assert result == []

    # ------------------------------------------------------------------
    # Lines 584-601: Cache-hit path (clear_cache=False + cache miss=None)
    # NOTE: cache hit with exclude_existing=False is in existing tests.
    # ------------------------------------------------------------------

    def test_cache_hit_uses_cached_validation(self):
        """Cache hit → device validation taken from cache."""
        api = self._make_api()
        device = self._make_device()

        existing = MagicMock()
        cached_validation = _make_validation()
        cached_validation["existing_device"] = existing  # truthy → refresh skips new-device lookup
        cached_entry = dict(device)
        cached_entry["_validation"] = cached_validation

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import._refresh_existing_device"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import") as mock_validate,
        ):
            # First get → device cache hit; second get → metadata (truthy)
            mock_cache.get.side_effect = [cached_entry, MagicMock()]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,
                show_disabled=True,
            )

        # validate_device_for_import must NOT be called (cache hit)
        mock_validate.assert_not_called()
        assert len(result) == 1

    # ------------------------------------------------------------------
    # Lines 596-598: Cache-hit + exclude_existing → device skipped
    # ------------------------------------------------------------------

    def test_cache_hit_with_exclude_existing_skips_device(self):
        """Cache hit + exclude_existing + existing_device → device skipped."""
        api = self._make_api()
        device = self._make_device()

        existing = MagicMock()
        existing.pk = 1
        cached_validation = _make_validation(existing_device=existing)
        # Ensure _refresh_existing_device returns quickly without a real DB call
        cached_validation["existing_device"] = existing
        cached_entry = dict(device)
        cached_entry["_validation"] = cached_validation

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            # Patch _refresh_existing_device to be a no-op so we can control existing_device
            patch("netbox_librenms_plugin.import_utils.bulk_import._refresh_existing_device"),
        ):
            mock_cache.get.return_value = cached_entry

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,
                show_disabled=True,
                exclude_existing=True,
            )

        # Device should be excluded because existing_device is set
        assert result == []

    # ------------------------------------------------------------------
    # Lines 604-641: Validate-and-cache path (clear_cache=True)
    # ------------------------------------------------------------------

    def test_validate_and_cache_path_no_vc_detection(self):
        """clear_cache=True → validate + set empty VC data + cache stored."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data",
                return_value={"is_stack": False},
            ) as mock_empty_vc,
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
            )

        assert len(result) == 1
        # empty_virtual_chassis_data should have been called for vc=False
        mock_empty_vc.assert_called()
        # cache.set called for the validated device and simple key
        assert mock_cache.set.call_count >= 2

    def test_validate_path_exclude_existing_skips_device(self):
        """validate path + exclude_existing + existing_device → device skipped."""
        api = self._make_api()
        device = self._make_device()

        validation_with_existing = _make_validation(existing_device=MagicMock())

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation_with_existing,
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=True,
            )

        assert result == []

    def test_validate_path_client_disconnect_with_request_returns_empty(self):
        """validate raises BrokenPipeError + request set → _empty_return."""
        api = self._make_api()
        request = MagicMock()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=BrokenPipeError("client gone"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                request=request,
            )

        assert result == []

    def test_validate_path_client_disconnect_no_request_reraises(self):
        """validate raises BrokenPipeError, request=None → re-raised."""

        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=BrokenPipeError("client gone"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
        ):
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            with pytest.raises(BrokenPipeError):
                process_device_filters(
                    api,
                    filters={},
                    vc_detection_enabled=False,
                    clear_cache=True,
                    show_disabled=True,
                    request=None,
                )

    # ------------------------------------------------------------------
    # Line 665: pass – metadata already exists and should_update=False
    # ------------------------------------------------------------------

    def test_cache_metadata_not_updated_when_from_cache_and_existing(self):
        """from_cache=True + existing metadata → metadata preserved (pass branch)."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),  # from_cache=True
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            existing_metadata = {"cached_at": "2024-01-01T00:00:00+00:00", "cache_timeout": 300}
            # First cache.get → None (no device cache hit → forces validation)
            # Second cache.get → existing metadata (truthy)
            mock_cache.get.side_effect = [None, existing_metadata]

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=False,  # not clearing cache
                show_disabled=True,
            )

        assert len(result) == 1
        # cache.set for metadata should NOT have been called (existing preserved)
        set_calls = [c for c in mock_cache.set.call_args_list if c.args[0] == "mkey"]
        assert len(set_calls) == 0

    # ------------------------------------------------------------------
    # Lines 604-641: Cache metadata stored when clear_cache=False + from_cache=False
    # ------------------------------------------------------------------

    def test_cache_metadata_stored_when_fresh_data(self):
        """Fresh data (from_cache=False) → metadata and index stored."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),  # from_cache=False
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default  # no cache hit anywhere

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
            )

        assert len(result) == 1
        # cache.set must have been called at least for metadata
        assert mock_cache.set.call_count >= 1

    # ------------------------------------------------------------------
    # Lines 687-694: Final job logging
    # ------------------------------------------------------------------

    def test_job_final_log_without_exclude_existing(self):
        """With job + validated devices (no exclude_existing) → final log."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=False,
                job=job,
            )

        assert len(result) == 1
        final_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("Validation complete" in s for s in final_calls)

    def test_job_final_log_with_exclude_existing(self):
        """With job + exclude_existing + some devices filtered → extended final log."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        # Device will be excluded because it has an existing_device
        validation_with_existing = _make_validation(existing_device=MagicMock())

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation_with_existing,
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rq_cls,
        ):
            mock_cache.get.return_value = None
            mock_get_queue.return_value = MagicMock()
            mock_rq_cls.fetch.return_value = _make_rq_running()

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                exclude_existing=True,
                job=job,
            )

        # Device excluded → empty result; job should log the "filtered out" message
        assert result == []
        final_calls = [str(c) for c in job.logger.info.call_args_list]
        assert any("filtered out" in s for s in final_calls)

    # ------------------------------------------------------------------
    # Lines 698-699: return_cache_status=True → returns tuple
    # ------------------------------------------------------------------

    def test_return_cache_status_true_returns_tuple(self):
        """return_cache_status=True → (devices, from_cache) tuple."""
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], True),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: default

            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            result = process_device_filters(
                api,
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=True,
                return_cache_status=True,
            )

        assert isinstance(result, tuple)
        devices, from_cache = result
        assert len(devices) == 1
        assert from_cache is True

    def test_rq_fetch_exception_does_not_cancel_process_filters(self):
        """RQ Job.fetch raises → _is_job_cancelled returns False → processing continues."""
        job = _make_job()
        api = self._make_api()
        device = self._make_device()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=_make_validation(),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
            patch("django_rq.get_queue") as mock_get_queue,
        ):
            mock_cache.get.side_effect = lambda key, default=None: default
            mock_get_queue.return_value = MagicMock()
            with patch("rq.job.Job") as mock_rq_cls:
                mock_rq_cls.fetch.side_effect = Exception("RQ unavailable")
                from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

                result = process_device_filters(
                    api,
                    filters={},
                    vc_detection_enabled=False,
                    clear_cache=True,
                    show_disabled=True,
                    job=job,
                )

        assert len(result) == 1


# ===========================================================================
# Issue #26 — device_role reset must NOT clear VMs
# ===========================================================================


class TestDeviceRoleResetGuard:
    """#26: device_role should only be reset when the device was deleted and import_as_vm is False."""

    def _call_refresh(self, validation, libre_device=None):
        """Simulate a device that was found at cache time but has since been deleted."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        # Set a mock existing_device so the function takes the "deleted" path
        existing = MagicMock()
        existing.pk = 42
        validation["existing_device"] = existing

        with patch("dcim.models.Device") as mock_dev_cls:
            with patch("virtualization.models.VirtualMachine") as mock_vm_cls:
                mock_dev_cls.objects.filter.return_value.first.return_value = None  # deleted
                mock_vm_cls.objects.filter.return_value.first.return_value = None  # deleted
                _refresh_existing_device(validation, server_key="default")

    def test_device_role_reset_for_plain_device(self):
        """When import_as_vm=False and device deleted, device_role is reset to not-found but available_roles preserved."""
        mock_role = MagicMock()
        validation = _make_validation(import_as_vm=False)
        validation["device_role"] = {"found": True, "role": mock_role}
        self._call_refresh(validation)
        assert validation["device_role"] == {"found": False, "role": None, "available_roles": []}

    def test_device_role_preserved_for_vm(self):
        """When import_as_vm=True and device deleted, device_role must NOT be cleared."""
        mock_role = MagicMock()
        validation = _make_validation(import_as_vm=True)
        validation["device_role"] = {"found": True, "role": mock_role}
        self._call_refresh(validation)
        # device_role should remain untouched
        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] is mock_role


# ===========================================================================
# Issue #28 — cache index TTL always refreshed
# ===========================================================================


class TestCacheIndexTTLRefresh:
    """#28: cache index must be re-written even when the key is already present."""

    def _make_api(self, server_key="default", cache_timeout=300):
        api = MagicMock()
        api.server_key = server_key
        api.cache_timeout = cache_timeout
        return api

    def _run_process_with_cache(self, cache_index_before, api=None):
        """Run process_device_filters with a pre-seeded cache index and return the mock_cache."""
        if api is None:
            api = self._make_api()
        device = {"device_id": 1, "hostname": "sw01", "sysName": "sw01"}
        validation = _make_validation()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=([device], False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=validation,
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.empty_virtual_chassis_data", return_value={}),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key",
                return_value="vkey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_import_device_cache_key",
                return_value="ikey",
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key",
                return_value="mkey",
            ),
        ):
            mock_cache.get.side_effect = lambda key, default=None: (
                cache_index_before if "cache_index" in key else default
            )
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            process_device_filters(api, filters={}, vc_detection_enabled=False, clear_cache=False, show_disabled=False)
            return mock_cache

    def test_cache_index_refreshed_when_key_already_present(self):
        """cache.set is called for the index even when the metadata key is already present."""
        existing_key = "mkey"
        mock_cache = self._run_process_with_cache(cache_index_before=[existing_key])
        # Find the cache.set call that updates the index
        index_set_calls = [c for c in mock_cache.set.call_args_list if "cache_index" in (c.args[0] if c.args else "")]
        assert len(index_set_calls) >= 1, "cache.set for cache_index was never called"
        # The stored index must still contain the key (not duplicated)
        stored_index = index_set_calls[0].args[1]
        assert stored_index.count(existing_key) == 1

    def test_cache_index_refreshed_for_new_key(self):
        """cache.set is called when the key is new."""
        mock_cache = self._run_process_with_cache(cache_index_before=[])
        index_set_calls = [c for c in mock_cache.set.call_args_list if "cache_index" in (c.args[0] if c.args else "")]
        assert len(index_set_calls) >= 1


# ===========================================================================
# Issue #36 — cross-model conflict detection in stale cache refresh
# ===========================================================================


class TestCrossModelConflictDetection:
    """#36: stale-cache refresh must detect device imported as VM (or vice versa)."""

    @pytest.mark.django_db
    def test_vm_found_when_device_imported_as_vm(self):
        """import_as_vm=False but a REAL VM exists under the name; the refresh detects it cross-model and blocks the import."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=False)
        # existing_device=None means "check if imported since caching" branch
        validation["existing_device"] = None
        validation["resolved_name"] = "sw-cross"
        libre_device = {"device_id": 99, "hostname": "sw-cross", "sysName": "sw-cross"}

        # A real VM owns the name; no Device does → the preferred (Device) model finds nothing and
        # the cross-model (VM) lookup matches. find_by_librenms_id runs for real (no CF → None).
        vm = make_vm("sw-cross")

        _refresh_existing_device(validation, libre_device, server_key="default")

        assert validation["existing_device"] == vm
        # import_as_vm must be flipped to True so future refreshes query VirtualMachine
        assert validation["import_as_vm"] is True
        # A late-found cross-model match must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_id_match_in_cross_model_wins_over_name_in_preferred_model(self):
        """When the scanned LibreNMS id is linked to the cross model (VM) while the preferred model (Device) merely shares the name, the id-linked object must win — binding the name-colliding Device would disagree with validation and render actions for the wrong object."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=False)  # Model=Device, CrossModel=VM
        validation["existing_device"] = None
        libre_device = {"device_id": 99, "hostname": "shared-name", "sysName": "shared-name"}

        mock_vm = MagicMock()
        mock_vm.role = None  # the id-linked VM
        mock_vm.cf = {"librenms_id": {"default": {"id": 99}}}  # real host link _refresh_librenms_linkage re-derives
        mock_device = MagicMock()
        mock_device.role = MagicMock()  # a Device that merely shares the name

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id") as fbi,
            patch("dcim.models.Device") as mock_dev_cls,
            patch("virtualization.models.VirtualMachine") as mock_vm_cls,
        ):
            # id 99 is linked to the VM (cross model), not the Device (preferred model).
            fbi.side_effect = lambda m, lid, sk: mock_vm if m is mock_vm_cls else None
            # The Device shares the name → a name fallback would (wrongly) match it.
            mock_dev_cls.objects.filter.return_value.first.return_value = mock_device
            mock_vm_cls.objects.filter.return_value.first.return_value = None

            _refresh_existing_device(validation, libre_device, server_key="default")

        # The id-linked VM wins across models, not the name-colliding Device.
        assert validation["existing_device"] is mock_vm
        assert validation["existing_match_type"] == "librenms_id"
        assert validation["import_as_vm"] is True  # cross-model → flipped

    @pytest.mark.django_db
    def test_device_found_when_vm_imported_as_device(self):
        """import_as_vm=True but a REAL Device exists under the name; the cross-model lookup detects it and blocks the import."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=True)
        validation["existing_device"] = None
        validation["resolved_name"] = "vm-but-device"
        libre_device = {"device_id": 77, "hostname": "vm-but-device", "sysName": "vm-but-device"}

        # A real Device owns the name; no VM does → preferred (VM) model finds nothing, cross-model
        # (Device) matches.
        device = make_device("vm-but-device")

        _refresh_existing_device(validation, libre_device, server_key="default")

        assert validation["existing_device"] == device
        # import_as_vm must be flipped to False so future refreshes query Device
        assert validation["import_as_vm"] is False
        # A late-found cross-model match must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    @pytest.mark.django_db
    def test_both_models_match_surfaces_ambiguity_warning(self):
        """Same hostname resolves in BOTH models: the refresh must surface the ambiguity warning even when the validation dict omits the 'warnings' key."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=False)
        validation["existing_device"] = None
        validation["resolved_name"] = "sw-both"
        # _make_validation intentionally omits "warnings" — a minimal caller (or the
        # _device_validation/_vm_validation test baselines) can build the dict without it, so the
        # cross-model branch must create the key rather than silently drop the warning.
        assert "warnings" not in validation
        libre_device = {"device_id": 55, "hostname": "sw-both", "sysName": "sw-both"}

        # BOTH a Device and a VM own the name → the elif model_match and cross_match branch fires.
        make_device("sw-both")
        make_vm("sw-both")

        _refresh_existing_device(validation, libre_device, server_key="default")

        # Cross-model ambiguity: bind NEITHER (leave unmatched) but SURFACE the warning so the user
        # knows to set librenms_id on the correct object.
        assert validation["existing_device"] is None
        assert any("Both a VM and Device exist with hostname 'sw-both'" in w for w in validation.get("warnings", [])), (
            "cross-model ambiguity warning was silently dropped"
        )
        # Unlike the single-cross-match siblings above (which bind existing_device and force
        # can_import=False), the both-models-match branch deliberately does NOT block: it warns and
        # leaves the row importable as a NEW device (the user then sets librenms_id on the correct
        # object). Pin that so a regression that instead blocks — or one that binds an arbitrary
        # existing match — is caught.
        assert validation["can_import"] is True
        assert validation["is_ready"] is True


class TestRefreshLibreNMSLinkage:
    """_refresh_librenms_linkage: re-classify a librenms-id match on cache-hit refresh."""

    def _call(self, *, match_type, scanned_id, host_id, oob_id):
        from netbox_librenms_plugin.import_utils import bulk_import

        validation = {"existing_match_type": match_type}
        device = MagicMock()
        libre_device = {"device_id": scanned_id}
        oob = {"id": oob_id} if oob_id is not None else None
        with (
            patch.object(
                bulk_import,
                "_describe_existing_librenms_link",
                return_value={"host_id": host_id, "oob_id": oob_id, "oob_type": None},
            ) as mock_describe,
            patch.object(bulk_import, "get_librenms_oob", return_value=oob) as mock_oob,
        ):
            bulk_import._refresh_librenms_linkage(validation, device, libre_device, "default")
        # Both linkage reads must be scoped to the active server_key — a regression that
        # dropped server_key would re-classify against the wrong server's mapping.
        mock_describe.assert_called_once_with(device, "default")
        if mock_oob.called:
            mock_oob.assert_called_with(device, server_key="default")
        return validation

    def test_host_id_match_classifies_librenms_id(self):
        v = self._call(match_type="librenms_id", scanned_id=42, host_id=42, oob_id=None)
        assert v["existing_match_type"] == "librenms_id"

    def test_oob_id_match_classifies_librenms_oob(self):
        v = self._call(match_type="librenms_id", scanned_id=77, host_id=None, oob_id=77)
        assert v["existing_match_type"] == "librenms_oob"

    def test_neither_match_neutralizes_stale_badge(self):
        """Linkage changed since caching: neither host nor OOB id matches the scanned device, so the stale 'librenms_id' badge must be cleared (not kept)."""
        v = self._call(match_type="librenms_id", scanned_id=42, host_id=99, oob_id=None)
        assert v["existing_match_type"] is None

    def test_non_librenms_match_type_untouched(self):
        """Serial/hostname/primary_ip matches are left alone."""
        v = self._call(match_type="serial", scanned_id=42, host_id=None, oob_id=None)
        assert v["existing_match_type"] == "serial"
