"""
Coverage tests for import_utils/bulk_import.py.

Exercises error paths, cancellation flows, cache behaviour,
and edge cases in bulk_import_devices_shared and process_device_filters.
"""

from unittest.mock import MagicMock, patch

import pytest

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


class TestRefreshExistingDevice:
    """Tests for ``_refresh_existing_device``."""

    # ------------------------------------------------------------------
    # Lines 336-341: Device path → refreshed device with role
    # ------------------------------------------------------------------

    def test_device_path_refreshes_role(self):
        """Non-VM existing device refreshed; role updated on result."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 1
        refreshed = MagicMock()
        refreshed.role = MagicMock(name="switch")

        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "device_role": {},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            _refresh_existing_device(validation)

        assert validation["existing_device"] is refreshed
        assert validation["device_role"]["found"] is True
        assert validation["device_role"]["role"] is refreshed.role

    # ------------------------------------------------------------------
    # Lines 342-345: Device path → refreshed device without role
    # ------------------------------------------------------------------

    def test_device_path_refreshes_no_role(self):
        """Refreshed device has no role → device_role = {'found': False}."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 2
        refreshed = MagicMock()
        refreshed.role = None  # role removed since caching

        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "device_role": {"found": True, "role": MagicMock()},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            _refresh_existing_device(validation)

        assert validation["existing_device"] is refreshed
        assert validation["device_role"] == {"found": False, "role": None, "available_roles": []}

    # ------------------------------------------------------------------
    # Lines 346-365: Existing device was deleted (Device.objects returns None)
    # ------------------------------------------------------------------

    def test_deleted_device_clears_existing_and_recomputes_readiness(self):
        """Device deleted → existing_device=None, readiness recomputed."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 3
        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "issues": [],
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": True},
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = None  # deleted
            _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["device_role"] == {"found": False, "role": None, "available_roles": []}
        assert validation["can_import"] is True  # no issues
        assert validation["is_ready"] is False  # device_role.found is now missing

    def test_deleted_device_clears_stale_match_derived_actions(self):
        """When the matched device is deleted, serial/OOB/merge/promote actions derived from
        it must be cleared so the UI can't offer actions on a now-gone device."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 4
        validation = {
            "existing_device": existing,
            "import_as_vm": False,
            "issues": [],
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": True},
            # Stale actions produced from the now-deleted match:
            "serial_action": "merge_netbox_devices",
            "oob_candidate": {"device": existing, "type": "idrac"},
            "promote_to_host": {"existing_libre_id": 9},
            "merge_candidates": {"host_named": {"pk": 4}, "oob_named": {"pk": 5}},
            "serial_role_choice_available": True,
        }

        with patch("dcim.models.Device") as mock_Device:
            mock_Device.objects.filter.return_value.first.return_value = None  # deleted
            _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["serial_action"] is None
        assert validation["oob_candidate"] is None
        assert validation["serial_role_choice_available"] is False
        assert "promote_to_host" not in validation
        assert "merge_candidates" not in validation

    def test_neutralized_librenms_link_clears_match_and_reevaluates(self):
        """A cached librenms_id match whose link was removed in NetBox since caching must be
        dropped, not left blocking the row: _refresh_librenms_linkage neutralizes the badge,
        and the refresh then clears existing_device + recomputes readiness (and re-looks up)
        so the row becomes importable again instead of staying blocked until cache expiry."""
        from netbox_librenms_plugin.import_utils import bulk_import

        existing = MagicMock(pk=2)
        refreshed = MagicMock(pk=2)
        refreshed.role = None
        validation = {
            "existing_device": existing,
            "existing_match_type": "librenms_id",
            "import_as_vm": False,
            "device_role": {"found": True, "role": MagicMock()},
            "resolved_name": "router1",
            "issues": [],
        }
        libre_device = {"device_id": 50, "hostname": "router1", "sysName": "router1"}

        def _neutralize(val, dev, libre, sk):
            val["existing_match_type"] = None  # link gone since caching

        def _filter(**kwargs):
            # pk-refresh returns the refreshed row; name/hostname re-lookups find nothing.
            m = MagicMock()
            m.first.return_value = refreshed if "pk" in kwargs else None
            return m

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("virtualization.models.VirtualMachine") as mock_VM,
            patch.object(bulk_import, "_refresh_librenms_linkage", side_effect=_neutralize),
            patch.object(bulk_import, "find_by_librenms_id", return_value=None),
        ):
            mock_Device.objects.filter.side_effect = _filter
            mock_VM.objects.filter.side_effect = _filter
            bulk_import._refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Stale librenms match dropped; nothing re-matches → importable, no "existing" badge.
        assert validation["existing_device"] is None
        assert validation["existing_match_type"] is None
        assert validation["can_import"] is True

    def test_fresh_lookup_matches_by_serial_blocks_import(self):
        """The refresh re-check must use validate_device_for_import's breadth: a row with no
        librenms_id/name match must still be blocked when a NetBox device matches by hardware
        serial, otherwise it flips to importable and creates a duplicate."""
        from netbox_librenms_plugin.import_utils import bulk_import

        serial_dev = MagicMock(pk=7)
        serial_dev.role = None
        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "issues": [],
            "device_role": {"available_roles": []},
        }
        libre_device = {"device_id": 50, "hostname": "h", "sysName": "h", "serial": "ABC123"}

        def _filter(**kwargs):
            m = MagicMock()
            m.first.return_value = serial_dev if kwargs.get("serial") == "ABC123" else None
            return m

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("virtualization.models.VirtualMachine") as mock_VM,
            patch.object(bulk_import, "find_by_librenms_id", return_value=None),
            patch.object(bulk_import, "_refresh_librenms_linkage"),
            patch.object(bulk_import, "recalculate_validation_status"),
        ):
            mock_Device.objects.filter.side_effect = _filter
            mock_VM.objects.filter.return_value.first.return_value = None
            bulk_import._refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is serial_dev
        assert validation["existing_match_type"] == "serial"
        assert validation["can_import"] is False

    def test_fresh_lookup_matches_by_primary_ip_blocks_import(self):
        """As above, but the existing NetBox device is reachable only via its management IP —
        the refresh must catch it (interface-assigned IP -> device) and block the import."""
        from netbox_librenms_plugin.import_utils import bulk_import

        ip_dev = MagicMock(pk=8)
        ip_dev.role = None
        iface = MagicMock()
        iface.device = ip_dev
        existing_ip = MagicMock()
        existing_ip.assigned_object = iface
        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "issues": [],
            "device_role": {"available_roles": []},
        }
        libre_device = {"device_id": 51, "hostname": "h2", "sysName": "h2", "ip": "10.0.0.9"}

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("virtualization.models.VirtualMachine") as mock_VM,
            patch("ipam.models.IPAddress") as mock_IP,
            patch.object(bulk_import, "find_by_librenms_id", return_value=None),
            patch.object(bulk_import, "_refresh_librenms_linkage"),
            patch.object(bulk_import, "recalculate_validation_status"),
        ):
            # No serial/name match; the IP lookup resolves to the device.
            mock_Device.objects.filter.return_value.first.return_value = None
            mock_VM.objects.filter.return_value.first.return_value = None
            mock_IP.objects.filter.return_value.first.return_value = existing_ip
            bulk_import._refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is ip_dev
        assert validation["existing_match_type"] == "primary_ip"
        assert validation["can_import"] is False

    def test_neutralized_link_but_other_match_type_keeps_block(self):
        """If the cached match was NOT librenms-id-based (e.g. hostname), a None badge from
        the linkage refresh must NOT clear the existing device — only a neutralized
        librenms/OOB link triggers the re-evaluation."""
        from netbox_librenms_plugin.import_utils import bulk_import

        existing = MagicMock(pk=3)
        refreshed = MagicMock(pk=3)
        refreshed.role = None
        validation = {
            "existing_device": existing,
            "existing_match_type": "hostname",  # not librenms-based
            "import_as_vm": False,
            "device_role": {"found": True, "role": MagicMock()},
            "issues": [],
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch.object(bulk_import, "_refresh_librenms_linkage"),  # no-op: leaves match_type "hostname"
            patch.object(bulk_import, "recalculate_validation_status"),  # branch-decision test, not readiness
        ):
            mock_Device.objects.filter.return_value.first.return_value = refreshed
            bulk_import._refresh_existing_device(validation, libre_device={"device_id": 1}, server_key="default")

        # hostname match preserved → device kept (not cleared) and the row stays blocked.
        assert validation["existing_device"] is refreshed
        assert validation["existing_match_type"] == "hostname"
        assert validation["can_import"] is False

    def test_deleted_vm_recomputes_readiness_from_cluster(self):
        """VM deleted → is_ready reflects cluster.found."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 4
        validation = {
            "existing_device": existing,
            "import_as_vm": True,
            "issues": [],
            "cluster": {"found": True},
        }

        with patch("virtualization.models.VirtualMachine") as mock_VM:
            mock_VM.objects.filter.return_value.first.return_value = None  # deleted
            _refresh_existing_device(validation)

        assert validation["existing_device"] is None
        assert validation["can_import"] is True
        assert validation["is_ready"] is True  # cluster.found=True

    def test_deleted_vm_not_ready_when_no_cluster(self):
        """Deleted VM with no cluster → is_ready=False."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 5
        validation = {
            "existing_device": existing,
            "import_as_vm": True,
            "issues": ["some issue"],
            "cluster": {"found": False},
        }

        with patch("virtualization.models.VirtualMachine") as mock_VM:
            mock_VM.objects.filter.return_value.first.return_value = None
            _refresh_existing_device(validation)

        assert validation["can_import"] is False  # has issues
        assert validation["is_ready"] is False

    # ------------------------------------------------------------------
    # Lines 366-368: Exception caught → logger.error called
    # ------------------------------------------------------------------

    def test_exception_during_refresh_logs_error(self):
        """DB error during refresh is caught and logged."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        existing = MagicMock()
        existing.pk = 99
        validation = {"existing_device": existing, "import_as_vm": False}

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            mock_Device.objects.filter.side_effect = Exception("DB down")
            _refresh_existing_device(validation)  # must not raise

        mock_logger.error.assert_called_once()
        assert "99" in mock_logger.error.call_args[0][0]

    # ------------------------------------------------------------------
    # Line 373: no existing_device, no libre_device → early return
    # ------------------------------------------------------------------

    def test_no_existing_device_no_libre_device_returns_early(self):
        """existing=None + libre_device=None → immediate return."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = {"existing_device": None}
        # No exception and validation unchanged
        _refresh_existing_device(validation, libre_device=None)
        assert validation == {"existing_device": None}

    # ------------------------------------------------------------------
    # Lines 393-395: existing=None, found via librenms_id
    # ------------------------------------------------------------------

    def test_no_existing_device_found_by_librenms_id(self):
        """existing=None: device found by librenms_id custom field."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = MagicMock(name="switch")
        # find_by_librenms_id matched this device on its host-side librenms_id CF; give it a
        # realistic CF so _refresh_librenms_linkage re-verifies host_id == scanned id (42) and
        # keeps the 'librenms_id' classification (rather than neutralizing on a bare mock).
        new_device.cf = {"librenms_id": {"default": {"id": 42}}}
        libre_device = {"device_id": 42, "hostname": "sw01", "sysName": "sw01"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw01",
            "device_role": {"found": False, "role": None},
            "site": {"found": True, "site": MagicMock()},
            "device_type": {"found": True, "device_type": MagicMock()},
            "issues": [],
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=new_device,
            ),
        ):
            mock_Device.objects.filter.return_value.first.return_value = None
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "librenms_id"
        # Late-found existing match must never be import-ready, even if recalculate
        # would otherwise set can_import=True (no issues + all fields found).
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        # Device has a role → device_role should be set
        assert validation["device_role"]["found"] is True

    def test_no_existing_device_found_as_oob_reclassifies_and_links(self):
        """existing=None at cache time, but the scanned LibreNMS id is now linked
        as a device's OOB controller: refresh must reclassify as 'librenms_oob'
        and populate existing_librenms_link (the stale-OOB-badge fix), DB-only."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        # Host device whose OOB sub-key points at the scanned LibreNMS id (42).
        host = MagicMock()
        host.role = None
        host.cf = {"librenms_id": {"default": {"id": 99, "oob": {"id": 42, "type": "drac"}}}}
        libre_device = {"device_id": 42, "hostname": "10.0.0.5", "sysName": "idrac-x"}

        validation = {
            "existing_device": None,
            "existing_match_type": None,
            "existing_librenms_link": None,
            "import_as_vm": False,
            "resolved_name": "idrac-x",
            "device_role": {"found": False, "role": None},
            "site": {"found": True, "site": MagicMock()},
            "device_type": {"found": True, "device_type": MagicMock()},
            "issues": [],
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=host,
            ) as mock_find,
        ):
            mock_Device.objects.filter.return_value.first.return_value = None
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The lookup must use the scanned LibreNMS id (42) and the server key
        # ("default") — otherwise it could match the wrong device or drop scoping.
        args, kwargs = mock_find.call_args
        assert 42 in args or kwargs.get("librenms_id") == 42
        assert "default" in args or kwargs.get("server_key") == "default"

        # Matched via the OOB sub-key → librenms_oob, not plain librenms_id.
        assert validation["existing_match_type"] == "librenms_oob"
        assert validation["existing_librenms_link"] == {"host_id": 99, "oob_id": 42, "oob_type": "drac"}
        # The matched host must be stored back into existing_device — a regression that only
        # updates the badge/link fields would lose the object that downstream row logic needs.
        assert validation["existing_device"] is host
        # A late-found existing match must never be import-ready.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    # ------------------------------------------------------------------
    # Lines 400-402: existing=None, found by resolved_name
    # ------------------------------------------------------------------

    def test_no_existing_device_found_by_resolved_name(self):
        """existing=None: not found by librenms_id, but found by resolved_name."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None  # no role
        libre_device = {"device_id": 43, "hostname": "sw02", "sysName": "sw02"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw02-resolved",
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=None,
            ),
        ):
            # resolved_name match
            mock_Device.objects.filter.return_value.first.return_value = new_device
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "resolved_name"
        assert validation["can_import"] is False

    # ------------------------------------------------------------------
    # Lines 420-421: exception in the "no existing device" lookup path
    # ------------------------------------------------------------------

    def test_exception_during_new_device_lookup_logs_error(self):
        """Exception in the newly-imported-device check is caught and logged."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        libre_device = {"device_id": 44, "hostname": "sw03", "sysName": "sw03"}
        validation = {"existing_device": None, "import_as_vm": False, "resolved_name": None}

        with (
            patch("dcim.models.Device"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                side_effect=Exception("lookup failed"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        mock_logger.error.assert_called()

    def test_no_existing_device_non_numeric_librenms_id_skips_id_lookup(self):
        """Non-numeric device_id raises ValueError → except pass, falls back to name."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None
        # Non-numeric device_id triggers ValueError inside int() → except (ValueError, TypeError): pass
        libre_device = {"device_id": "not-an-int", "hostname": "sw05", "sysName": "sw05"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": "sw05",
        }

        with (
            patch("dcim.models.Device") as mock_Device,
            patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id") as mock_find,
        ):
            mock_Device.objects.filter.return_value.first.return_value = new_device
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Should find via resolved_name (librenms_id lookup was skipped due to ValueError)
        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "resolved_name"
        # Crucially: find_by_librenms_id must never have been called — int("not-an-int")
        # raises ValueError before the call site is reached.
        mock_find.assert_not_called()

    def test_no_existing_device_hostname_fallback(self):
        """existing=None, not found by id or resolved_name → hostname fallback."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        new_device = MagicMock()
        new_device.role = None
        libre_device = {"device_id": 45, "hostname": "sw04", "sysName": "sw04-sysname"}

        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": None,  # no resolved_name → fall through to hostname
        }

        # filter returns new_device only for the hostname kwargs; any other filter
        # call (e.g. resolved_name or sysname) returns None — so the test fails if
        # the wrong lookup path is exercised.
        def filter_first(*args, **kwargs):
            m = MagicMock()
            m.first.return_value = new_device if kwargs.get("name__iexact") == "sw04" else None
            return m

        with (
            patch("dcim.models.Device") as mock_Device,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                return_value=None,
            ),
        ):
            mock_Device.objects.filter.side_effect = filter_first
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is new_device
        assert validation["existing_match_type"] == "hostname"
        # Verify the hostname-keyed filter call was actually made
        mock_Device.objects.filter.assert_any_call(name__iexact="sw04")


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

    def test_vm_found_when_device_imported_as_vm(self):
        """
        import_as_vm=False (cached as device) but the object was actually imported as VM.
        The refresh should detect the VM and mark the import as conflicting.
        """
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=False)
        # existing_device=None means "check if imported since caching" branch
        validation["existing_device"] = None
        libre_device = {"device_id": 99, "hostname": "sw-cross", "sysName": "sw-cross"}

        mock_vm = MagicMock()
        mock_vm.role = None

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id", return_value=None),
            patch("dcim.models.Device") as mock_dev_cls,
            patch("virtualization.models.VirtualMachine") as mock_vm_cls,
        ):
            # Primary model (Device) finds nothing; cross model (VirtualMachine) finds it
            mock_dev_cls.objects.filter.return_value.first.return_value = None
            mock_vm_cls.objects.filter.return_value.first.return_value = mock_vm

            _refresh_existing_device(validation, libre_device, server_key="default")

        assert validation["existing_device"] is mock_vm
        # import_as_vm must be flipped to True so future refreshes query VirtualMachine
        assert validation["import_as_vm"] is True
        # A late-found cross-model match must never be import-ready:
        # _refresh_existing_device re-asserts can_import=False/is_ready=False after
        # recalculate_validation_status regardless of issues/fields state.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_device_found_when_vm_imported_as_device(self):
        """
        import_as_vm=True but the object was imported as a Device.
        The refresh should detect the Device through cross-model lookup.
        """
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        validation = _make_validation(import_as_vm=True)
        validation["existing_device"] = None
        libre_device = {"device_id": 77, "hostname": "vm-but-device", "sysName": "vm-but-device"}

        mock_device = MagicMock()
        mock_device.role = MagicMock()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id", return_value=None),
            patch("dcim.models.Device") as mock_dev_cls,
            patch("virtualization.models.VirtualMachine") as mock_vm_cls,
        ):
            # Primary model (VirtualMachine) finds nothing; cross model (Device) finds it
            mock_vm_cls.objects.filter.return_value.first.return_value = None
            mock_dev_cls.objects.filter.return_value.first.return_value = mock_device

            _refresh_existing_device(validation, libre_device, server_key="default")

        assert validation["existing_device"] is mock_device
        # import_as_vm must be flipped to False so future refreshes query Device
        assert validation["import_as_vm"] is False
        # A late-found cross-model match must never be import-ready:
        # _refresh_existing_device re-asserts can_import=False/is_ready=False after
        # recalculate_validation_status regardless of issues/fields state.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False


class TestRefreshLibreNMSLinkage:
    """_refresh_librenms_linkage: re-classify a librenms-id match on cache-hit refresh.

    The host/OOB link can change in NetBox after a row is cached, so the match-type is
    re-derived against the *scanned* device id rather than trusting the stale value.
    """

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
        """Linkage changed since caching: neither host nor OOB id matches the scanned
        device, so the stale 'librenms_id' badge must be cleared (not kept)."""
        v = self._call(match_type="librenms_id", scanned_id=42, host_id=99, oob_id=None)
        assert v["existing_match_type"] is None

    def test_non_librenms_match_type_untouched(self):
        """Serial/hostname/primary_ip matches are left alone."""
        v = self._call(match_type="serial", scanned_id=42, host_id=None, oob_id=None)
        assert v["existing_match_type"] == "serial"
