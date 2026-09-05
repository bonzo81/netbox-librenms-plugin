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

    def test_mis_keyed_cache_row_is_refetched_not_trusted(self):
        """A cached row whose own device_id contradicts the requested id (mis-keyed/stale) is NOT imported as this device — a live fetch runs instead.

        The multi-row collision pre-check verifies this, but its callers skip it for single-row
        imports, so the import path must re-check at the point of use.
        """
        # Requested id 1, but the cached payload describes device 99 (a mis-keyed/stale entry).
        libre_cache = {1: {"device_id": 99, "hostname": "someone-else"}}

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
            mock_api.get_device_info.return_value = (True, {"device_id": 1, "hostname": "real-device-1"})
            mock_api_cls.return_value = mock_api

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # The mis-keyed cache row is rejected → a live fetch for the REAL id 1 runs instead of
        # importing device 99's data under the id-1 selection.
        mock_api.get_device_info.assert_called_once_with(1, use_cache=False)

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"device_id": 77, "hostname": "wrong-device"},  # another device's row
            {},  # empty mapping — no identity to verify
            ["not-a-dict"],  # non-dict payload
        ],
    )
    def test_mismatched_fallback_fetch_is_failed_and_not_cached(self, bad_payload):
        """A live-fetch payload not carrying the requested device_id is failed, not used or cached."""
        # Same fail-closed identity rule as the multi-row collision pre-check (which single-row
        # imports skip). The rejected mis-keyed cached row forces the live-fetch fallback.
        stale_row = {"device_id": 99, "hostname": "someone-else"}
        libre_cache = {1: dict(stale_row)}

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI") as mock_api_cls,
            patch("netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import") as mock_validate,
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
        ):
            mock_api = MagicMock()
            mock_api.server_key = "default"
            mock_api.get_device_info.return_value = (True, bad_payload)
            mock_api_cls.return_value = mock_api

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=MagicMock(),
                libre_devices_cache=libre_cache,
            )

        # The row fails closed instead of importing another device's data under id 1...
        assert [row["device_id"] for row in result["failed"]] == [1]
        assert result["success"] == []
        mock_validate.assert_not_called()
        mock_import.assert_not_called()
        # ...and the bad payload must not overwrite the shared cache entry either.
        assert libre_cache[1] == stale_row

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
            # Real validate_device_for_import output carries cluster regardless of
            # import_as_vm; a cross-model match flips is_vm and recalculate then
            # bracket-reads cluster["found"], so the fixture must carry it too.
            "cluster": {"found": False, "cluster": None, "available_clusters": []},
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

    def test_cross_model_different_candidate_values_binds_preferred(self):
        """A Device matched by resolved_name and an unrelated VM matched by a different candidate (raw hostname) bind the preferred Device, not a phantom cross-model collision."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("sw-pref-resolved")  # carries the resolved (preferred) name
        make_vm("sw-pref-host")  # carries only the raw hostname — a DIFFERENT name

        validation = self._device_validation(resolved_name="sw-pref-resolved")
        # device_id 88888 matches no librenms_id CF, so the name fallback runs.
        libre_device = {"device_id": 88888, "hostname": "sw-pref-host", "sysName": "sw-pref-host"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "resolved_name"
        assert not any("Both a VM and Device" in w for w in validation.get("warnings", [])), (
            "a different-value VM match must not trigger the cross-model collision warning"
        )
        assert validation["can_import"] is False

    def test_cross_model_warning_cleared_when_serial_uniquely_binds_device(self):
        """A unique serial match binding the Device clears the earlier cross-model hostname warning."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        # Same name on BOTH models → the name fallback flags a cross-model hostname collision and
        # leaves the row unmatched. The Device additionally carries a unique serial.
        dev = make_device("crossclr-sw", serial="CRXCLR-SERIAL-1")
        make_vm("crossclr-sw")

        validation = self._device_validation(resolved_name="crossclr-sw")
        # device_id 77777 matches no librenms_id CF → the name fallback runs, warns, then the
        # serial fallback uniquely resolves the Device.
        libre_device = {
            "device_id": 77777,
            "hostname": "crossclr-sw",
            "sysName": "crossclr-sw",
            "serial": "CRXCLR-SERIAL-1",
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The stronger serial match wins and binds the Device...
        assert validation["existing_device"].pk == dev.pk
        assert validation["existing_match_type"] == "serial"
        # ...and the stale cross-model "cannot determine which to match" warning is gone.
        assert not any("Both a VM and Device exist with hostname" in w for w in validation.get("warnings", [])), (
            "a unique serial match must clear the earlier cross-model hostname warning"
        )
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_ambiguous_hostname_refresh_shows_only_duplicate_blocker(self):
        """An ambiguous multi-device hostname match is terminal: the row shows only the duplicate-resolution blocker, not a stale create-time role blocker."""
        from dcim.models import Device, Site
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev_a = make_device("amb-terminal-sw")
        site_b = Site.objects.create(name="crfix-amb-site-b", slug="crfix-amb-site-b")
        Device.objects.create(
            name="amb-terminal-sw",
            device_type=dev_a.device_type,
            role=dev_a.role,
            site=site_b,
            status="active",
        )

        # device_role not selected → the no-match path would (wrongly) re-add its create-time blocker.
        validation = self._device_validation(resolved_name="amb-terminal-sw")
        libre_device = {"device_id": 88889, "hostname": "amb-terminal-sw", "sysName": "amb-terminal-sw"}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert any("hostname/serial" in i for i in validation["issues"])
        assert not any("role must be manually selected" in i.lower() for i in validation["issues"]), (
            "the terminal ambiguity branch must not leak a stale new-import role blocker"
        )
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

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

    def test_serial_cached_row_promoted_to_librenms_id_when_now_linked(self):
        """A row cached as a serial conflict that has since gained the matching host librenms_id must promote to a librenms_id match, not keep the stale serial badge."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        dev = make_device("ref-serial-now-linked", librenms_cf={"default": {"id": 42}})
        validation = self._device_validation(existing_device=dev, existing_match_type="serial")

        _refresh_librenms_linkage(validation, dev, {"device_id": 42, "hostname": "h"}, "default")

        assert validation["existing_match_type"] == "librenms_id"

    def test_serial_cached_row_untouched_when_no_current_link_matches(self):
        """A serial-matched row whose device has no matching librenms link stays 'serial' — only prior id/OOB matches are cleared."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_librenms_linkage

        dev = make_device("ref-serial-no-link")  # no librenms_id CF
        validation = self._device_validation(existing_device=dev, existing_match_type="serial")

        _refresh_librenms_linkage(validation, dev, {"device_id": 42, "hostname": "h"}, "default")

        assert validation["existing_match_type"] == "serial"

    def test_serial_oob_candidate_promoted_to_host_clears_stale_action(self):
        """A serial->librenms_id promotion drops the now-stale serial_action/oob_candidate."""
        # Otherwise device_status.py renders a purple "Add as OOB controller" badge
        # (is_oob_candidate = serial_action == "oob_candidate", match-type-independent) on a
        # row that is actually host-linked.
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-serial-oob-now-linked", librenms_cf={"default": {"id": 6001}})
        validation = self._device_validation(
            existing_device=dev,
            existing_match_type="serial",
            serial_action="oob_candidate",
            oob_candidate={"device": dev, "type": "idrac"},
            device_role={"found": True, "role": dev.role},
        )

        _refresh_existing_device(validation, libre_device={"device_id": 6001, "hostname": "h"}, server_key="default")

        assert validation["existing_match_type"] == "librenms_id"
        assert validation.get("serial_action") is None
        assert validation.get("oob_candidate") is None

    def test_serial_merge_promoted_to_oob_clears_merge_candidates(self):
        """A serial->librenms_oob promotion drops the now-stale serial_action/merge_candidates."""
        # Otherwise device_validation_details.html (which checks serial_action ==
        # "merge_netbox_devices" BEFORE the existing_match_type chain) renders a destructive
        # "Merge two NetBox devices" form on a row that is already correctly OOB-linked.
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-serial-merge-now-linked", librenms_cf={"default": {"oob": {"id": 6002}}})
        validation = self._device_validation(
            existing_device=dev,
            existing_match_type="serial",
            serial_action="merge_netbox_devices",
            merge_candidates={
                "host_named": {"pk": 1, "name": "other"},
                "oob_named": {"pk": dev.pk, "name": dev.name},
            },
            device_role={"found": True, "role": dev.role},
        )

        _refresh_existing_device(validation, libre_device={"device_id": 6002, "hostname": "h"}, server_key="default")

        assert validation["existing_match_type"] == "librenms_oob"
        assert validation.get("serial_action") is None
        assert validation.get("merge_candidates") is None

    def test_already_linked_row_promotion_preserves_derived_fields(self):
        """A re-confirmed librenms_id row keeps its link-derived fields (no over-clear)."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        dev = make_device("ref-already-linked", librenms_cf={"default": {"id": 6003}})
        validation = self._device_validation(
            existing_device=dev,
            existing_match_type="librenms_id",
            serial_action="update_serial",  # a legitimate link-context action
            device_role={"found": True, "role": dev.role},
        )

        _refresh_existing_device(validation, libre_device={"device_id": 6003, "hostname": "h"}, server_key="default")

        assert validation["existing_match_type"] == "librenms_id"
        # prior match was already a link type → no spurious clear of the link-context action.
        assert validation.get("serial_action") == "update_serial"

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
        # merge_candidates follows the baseline's always-present contract
        # (validate_device_for_import and apply_oob_detection_result both keep the
        # key set, defaulting to None) — clearing must reset it, not remove it.
        assert validation["merge_candidates"] is None

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

    def test_exception_during_new_device_lookup_logs_error_and_fails_closed(self):
        """An exception in the newly-imported-device re-check is logged and blocks the import (forced)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        libre_device = {"device_id": 44, "hostname": "sw03", "sysName": "sw03"}
        # Cache-time state said "importable" — the failed recheck must revoke that.
        validation = {
            "existing_device": None,
            "import_as_vm": False,
            "resolved_name": None,
            "can_import": True,
            "is_ready": True,
        }

        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id",
                side_effect=Exception("lookup failed"),
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.logger") as mock_logger,
        ):
            _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        mock_logger.error.assert_called()
        assert validation["can_import"] is False
        assert validation["is_ready"] is False
        # Surfaced to the user as a blocking issue, not silently greyed out.
        assert any("re-check failed" in issue for issue in validation.get("issues", []))


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
        with patch.object(
            bulk_import,
            "_describe_existing_librenms_link",
            return_value={"host_id": host_id, "oob_id": oob_id, "oob_type": None},
        ) as mock_describe:
            bulk_import._refresh_librenms_linkage(validation, device, libre_device, "default")
        # _refresh_librenms_linkage ALWAYS reads the linkage (via _describe_existing_librenms_link)
        # up front, before it branches on match_type — so every case, including a non-librenms
        # match type, must issue that read scoped to the active server_key. A regression that
        # dropped server_key (or short-circuited before the read) would re-classify against the
        # wrong server's mapping, so assert the call unconditionally.
        mock_describe.assert_called_once_with(device, "default")
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
        """Serial/hostname/primary_ip matches are left alone (the linkage read still runs first)."""
        v = self._call(match_type="serial", scanned_id=42, host_id=None, oob_id=None)
        assert v["existing_match_type"] == "serial"


# ===========================================================================
# TestRefreshExistingDeviceVMMatch — real-DB display state for VM matches
# ===========================================================================


@pytest.mark.django_db
class TestRefreshExistingDeviceVMMatch:
    """Real-DB checks that a late-found VM match populates cluster display state."""

    @staticmethod
    def _cross_model_validation():
        """Cached device row with no match at cache time, shaped like validate_device_for_import output."""
        return {
            "existing_device": None,
            "import_as_vm": False,
            "issues": [],
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": False, "role": None, "available_roles": []},
            "cluster": {"found": False, "cluster": None, "available_clusters": []},
        }

    def test_vm_match_shows_the_matched_vms_cluster(self):
        """A cross-model VM match reflects the VM's actual cluster, mirroring the device-role display."""
        from virtualization.models import Cluster, ClusterType, VirtualMachine

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        ctype = ClusterType.objects.create(name="ct-refresh", slug="ct-refresh")
        cluster = Cluster.objects.create(name="cl-refresh", type=ctype)
        VirtualMachine.objects.create(name="vm-clustered", cluster=cluster)

        validation = self._cross_model_validation()
        _refresh_existing_device(validation, {"hostname": "vm-clustered"}, server_key="default")

        assert validation["import_as_vm"] is True
        assert validation["cluster"]["found"] is True
        assert validation["cluster"]["cluster"] == cluster
        # Existing-match gating must stay force-blocked either way.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_vm_match_without_cluster_resets_stale_cluster_display(self):
        """A matched VM with no cluster clears a stale cached cluster selection."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        VirtualMachine.objects.create(name="vm-clusterless")

        validation = self._cross_model_validation()
        validation["cluster"] = {"found": True, "cluster": object(), "available_clusters": ["keep-me"]}

        _refresh_existing_device(validation, {"hostname": "vm-clusterless"}, server_key="default")

        assert validation["cluster"]["found"] is False
        assert validation["cluster"]["cluster"] is None
        assert validation["cluster"]["available_clusters"] == ["keep-me"]


@pytest.mark.django_db
class TestDetectCollisionsForDeviceIds:
    """Real-DB coverage for ``detect_collisions_for_device_ids`` (the import-path gate helper)."""

    @staticmethod
    def _api():
        from types import SimpleNamespace

        # server_key only — get_device_info is never reached because the cache is pre-populated.
        return SimpleNamespace(server_key="default")

    def test_two_rows_resolving_to_one_device_collide(self):
        """Two LibreNMS rows whose real validation resolves to one NetBox device → a collision."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        nb = make_device("collide-batch-host")
        cache = {
            8001: {"device_id": 8001, "sysName": "collide-batch-host", "hostname": "collide-batch-host"},
            8002: {"device_id": 8002, "sysName": "collide-batch-host", "hostname": "collide-batch-host"},
        }
        collisions, unresolved = detect_collisions_for_device_ids(
            [8001, 8002], self._api(), libre_devices_cache=cache, sync_options={"use_sysname": True}
        )
        assert unresolved == []
        assert len(collisions) == 1
        assert collisions[0]["nb_device_pk"] == nb.pk
        assert collisions[0]["nb_model_name"] == "device"
        assert {r["device_id"] for r in collisions[0]["librenms_rows"]} == {8001, 8002}

    def test_empty_cached_row_is_unresolved_not_a_clean_scan(self):
        """A negatively-cached empty payload can't be collision-checked → unresolved, never importable."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        # A prior failed lookup left an empty dict in the shared cache. Without the fix this reaches
        # validation as a brand-new device and the gate reports a clean scan for an unverified id.
        collisions, unresolved = detect_collisions_for_device_ids(
            [8010], self._api(), libre_devices_cache={8010: {}}, sync_options={"use_sysname": True}
        )
        assert collisions == []
        assert unresolved == [8010]

    def test_mismatched_cached_device_id_is_unresolved(self):
        """A cached row whose device_id doesn't match the requested id is unresolved, not scanned."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        # Stale/mis-keyed cache entry: id 8011 requested but the payload describes device 9999.
        cache = {8011: {"device_id": 9999, "sysName": "wrong-row", "hostname": "wrong-row"}}
        collisions, unresolved = detect_collisions_for_device_ids(
            [8011], self._api(), libre_devices_cache=cache, sync_options={"use_sysname": True}
        )
        assert collisions == []
        assert unresolved == [8011]

    def test_get_device_info_exception_is_unresolved_not_crash(self, caplog):
        """A raised fetch failure must be logged and fail closed to unresolved, not crash the gate."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        def _boom(device_id, **_kwargs):
            raise RuntimeError("cache backend unavailable")

        # Id not in the cache → the gate must fetch it; get_device_info raises instead of returning
        # (False, None). The row can't be collision-checked, so it's recorded unresolved (the caller
        # fails closed on it) rather than propagating and aborting the whole batch.
        api = SimpleNamespace(server_key="default", get_device_info=_boom)
        collisions, unresolved = detect_collisions_for_device_ids([8099], api, libre_devices_cache={})
        assert collisions == []
        assert unresolved == [8099]
        assert "Collision pre-check couldn't fetch device 8099" in caplog.text
        assert "cache backend unavailable" in caplog.text

    def test_two_rows_resolving_to_one_vm_collide(self):
        """Two LibreNMS rows whose real validation resolves to one existing VirtualMachine → a collision keyed to the VM model, proving the pre-check gate covers the VM half of a batch (validation flips import_as_vm on the existing-VM match)."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("collide-batch-vm")
        cache = {
            8005: {"device_id": 8005, "sysName": "collide-batch-vm", "hostname": "collide-batch-vm"},
            8006: {"device_id": 8006, "sysName": "collide-batch-vm", "hostname": "collide-batch-vm"},
        }
        collisions, unresolved = detect_collisions_for_device_ids(
            [8005, 8006],
            self._api(),
            libre_devices_cache=cache,
            sync_options={"use_sysname": True},
            # Mirror the real callers: VM-selected ids are passed so each row validates in
            # its actual import mode (the hostname→existing-VM match works either way, but
            # the wiring under test is the per-row mode the view/job now supply).
            vm_device_ids={8005, 8006},
        )
        assert unresolved == []
        assert len(collisions) == 1
        assert collisions[0]["nb_device_pk"] == vm.pk
        assert collisions[0]["nb_model_name"] == "virtualmachine"
        assert {r["device_id"] for r in collisions[0]["librenms_rows"]} == {8005, 8006}

    def test_vm_row_serial_match_does_not_fabricate_a_collision(self):
        """A VM-selected row must be validated in VM mode: Device-only serial matching (which bulk_import_vms intentionally skips) would otherwise resolve the VM row onto a Device another row targets and block a perfectly valid batch."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        nb = make_device("phantom-host", serial="ZZSER-PHANTOM")
        cache = {
            # Device row: hostname-matches the existing device — legitimate single-row target.
            8401: {"device_id": 8401, "sysName": "phantom-host", "hostname": "phantom-host", "serial": ""},
            # VM row: hostname matches nothing, but its serial equals the device's. Device-mode
            # validation would serial-match it onto nb; the real VM import never would.
            8402: {
                "device_id": 8402,
                "sysName": "vm-unrelated",
                "hostname": "vm-unrelated",
                "serial": "ZZSER-PHANTOM",
            },
        }
        collisions, unresolved = detect_collisions_for_device_ids(
            [8401, 8402],
            self._api(),
            libre_devices_cache=cache,
            sync_options={"use_sysname": True},
            vm_device_ids={8402},
        )
        assert unresolved == []
        assert collisions == [], f"VM-mode row must not serial-match onto Device pk={nb.pk}"

    def test_distinct_devices_do_not_collide(self):
        """Rows resolving to different NetBox devices → no collision."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        make_device("clean-a-host")
        make_device("clean-b-host")
        cache = {
            8003: {"device_id": 8003, "sysName": "clean-a-host", "hostname": "clean-a-host"},
            8004: {"device_id": 8004, "sysName": "clean-b-host", "hostname": "clean-b-host"},
        }
        collisions, unresolved = detect_collisions_for_device_ids(
            [8003, 8004], self._api(), libre_devices_cache=cache, sync_options={"use_sysname": True}
        )
        assert collisions == []
        assert unresolved == []

    def test_unfetchable_id_is_reported_unresolved(self):
        """An id not in the cache whose get_device_info fails is returned as unresolved (not silently skipped), so the caller can fail closed instead of importing it unchecked."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        make_device("resolvable-host")
        cache = {9001: {"device_id": 9001, "sysName": "resolvable-host", "hostname": "resolvable-host"}}
        # 9002 isn't cached and its info fetch fails → it can't be collision-checked.
        api = SimpleNamespace(server_key="default", get_device_info=lambda _did, **_kwargs: (False, None))
        collisions, unresolved = detect_collisions_for_device_ids(
            [9001, 9002], api, libre_devices_cache=cache, sync_options={"use_sysname": True}
        )
        assert unresolved == [9002]
        assert collisions == []  # only one resolvable row → no collision

    def test_validation_error_id_is_reported_unresolved(self):
        """A row whose validator returns a partial-result issue is reported unresolved, not collision-checked on the partial result."""
        from netbox_librenms_plugin.import_utils import bulk_import as bulk_import_module
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
        from netbox_librenms_plugin.import_utils.device_operations import VALIDATION_ERROR_ISSUE_PREFIX

        make_device("validatable-host")
        cache = {
            9101: {"device_id": 9101, "sysName": "validatable-host", "hostname": "validatable-host"},
            9102: {"device_id": 9102, "sysName": "unvalidatable-host", "hostname": "unvalidatable-host"},
        }

        # Stub the consumer's validator so 9102 returns the except-branch shape directly, instead
        # of relying on _determine_device_name crashing on a non-string sysName — which would
        # silently stop exercising the fail-closed path if name coercion is ever hardened. Build the
        # issue from the SHARED prefix constant so producer and consumer can't drift apart unnoticed
        # (a rename of the marker moves both this input and the guard together). 9101 delegates to
        # the REAL validator so the clean-row collision logic this test asserts on stays genuine.
        real_validate = bulk_import_module.validate_device_for_import

        def fake_validate(libre_device, *args, **kwargs):
            if libre_device.get("device_id") == 9102:
                return {
                    "issues": [f"{VALIDATION_ERROR_ISSUE_PREFIX} simulated validator exception"],
                    "resolved_name": None,
                }
            return real_validate(libre_device, *args, **kwargs)

        with patch.object(bulk_import_module, "validate_device_for_import", side_effect=fake_validate):
            collisions, unresolved = detect_collisions_for_device_ids(
                [9101, 9102],
                self._api(),
                libre_devices_cache=cache,
                sync_options={"use_sysname": True, "strip_domain": True},
            )
        assert unresolved == [9102]
        assert collisions == []  # only one validatable row → no collision

    def test_cancelled_job_stops_scan_before_any_fetch(self):
        """A job already cancelled when the scan starts issues ZERO LibreNMS calls; every id is returned unresolved so the caller's existing gate fails the batch closed instead of importing it unchecked."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils import bulk_import as bulk_import_module
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        calls = []

        def _get_device_info(did, **_kwargs):
            calls.append(did)
            return True, {"device_id": did, "sysName": f"host-{did}", "hostname": f"host-{did}"}

        api = SimpleNamespace(server_key="default", get_device_info=_get_device_info)
        # _is_job_cancelled reads RQ/Redis job state — the one true external boundary here.
        with patch.object(bulk_import_module, "_is_job_cancelled", return_value=True):
            collisions, unresolved = detect_collisions_for_device_ids(
                [8201, 8202, 8203],
                api,
                libre_devices_cache={},
                sync_options={"use_sysname": True},
                job=SimpleNamespace(logger=None),
            )
        assert calls == [], "cancelled scan must not keep calling LibreNMS"
        assert unresolved == [8201, 8202, 8203]
        assert collisions == []

    def test_mid_scan_cancellation_marks_remainder_unresolved(self):
        """Cancellation arriving mid-scan stops at the next poll (every 5th id); already-scanned ids stay checked and every unscanned id is returned unresolved, so a partial scan can never pass for a clean full one."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils import bulk_import as bulk_import_module
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        ids = [8301, 8302, 8303, 8304, 8305, 8306, 8307]
        calls = []

        def _get_device_info(did, **_kwargs):
            calls.append(did)
            return True, {"device_id": did, "sysName": f"host-{did}", "hostname": f"host-{did}"}

        api = SimpleNamespace(server_key="default", get_device_info=_get_device_info)
        # Not cancelled at the idx==1 poll, cancelled by the idx==5 poll → ids 5..7 unscanned.
        with patch.object(bulk_import_module, "_is_job_cancelled", side_effect=[False, True]):
            collisions, unresolved = detect_collisions_for_device_ids(
                ids,
                api,
                libre_devices_cache={},
                sync_options={"use_sysname": True},
                job=SimpleNamespace(logger=None),
            )
        assert calls == [8301, 8302, 8303, 8304], "scan must stop at the cancellation poll"
        assert unresolved == [8305, 8306, 8307]
        assert collisions == []

    def test_fetched_row_is_written_back_into_shared_cache(self):
        """A cache-miss fetch is persisted into the caller's cache dict so the downstream import reuses it instead of re-fetching the same LibreNMS device."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        make_device("writeback-a-host")
        make_device("writeback-b-host")
        calls = []

        def _get_device_info(did, **kwargs):
            calls.append((did, kwargs))
            name = {8101: "writeback-a-host", 8102: "writeback-b-host"}[did]
            return True, {"device_id": did, "sysName": name, "hostname": name}

        api = SimpleNamespace(server_key="default", get_device_info=_get_device_info)
        shared_cache = {}  # empty → both ids miss, must be fetched AND written back

        detect_collisions_for_device_ids(
            [8101, 8102], api, libre_devices_cache=shared_cache, sync_options={"use_sysname": True}
        )
        # Each id fetched once and persisted into the SAME dict object the caller passed.
        assert calls == [(8101, {"use_cache": False}), (8102, {"use_cache": False})]
        assert shared_cache[8101]["hostname"] == "writeback-a-host"
        assert shared_cache[8102]["hostname"] == "writeback-b-host"

        # A downstream consumer reusing that warmed cache adds ZERO further LibreNMS calls.
        calls.clear()
        detect_collisions_for_device_ids(
            [8101, 8102], api, libre_devices_cache=shared_cache, sync_options={"use_sysname": True}
        )
        assert calls == [], "second pass must hit the warmed cache, not re-fetch"

    def test_fresh_fetch_mismatched_device_id_is_not_written_back(self):
        """A freshly fetched payload whose device_id doesn't match the requested id is unresolved and never written back into the shared cache (fresh-fetch analog of the cached-mismatch guard)."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        # 8020 isn't cached; the fetch SUCCEEDS but returns a payload describing device 9999
        # (a mis-keyed / stale LibreNMS response). It must fail closed WITHOUT persisting.
        shared_cache = {}
        api = SimpleNamespace(
            server_key="default",
            get_device_info=lambda _did, **_kwargs: (
                True,
                {"device_id": 9999, "sysName": "wrong-row", "hostname": "wrong-row"},
            ),
        )
        collisions, unresolved = detect_collisions_for_device_ids(
            [8020], api, libre_devices_cache=shared_cache, sync_options={"use_sysname": True}
        )
        assert unresolved == [8020]
        assert collisions == []
        # The mismatched payload must NOT leak into the shared cache the caller passed in.
        assert 8020 not in shared_cache, "mismatched fresh fetch poisoned the shared cache"
        assert shared_cache == {}, "no mis-keyed payload may survive the collision gate"
