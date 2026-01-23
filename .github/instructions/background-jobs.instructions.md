---
applyTo: "**/jobs.py,**/views/imports/**"
description: Background job architecture and task management patterns
---

# Background Jobs & Task Management

## Job Architecture
- Background jobs use NetBox's `JobRunner` base class (`netbox.jobs.JobRunner`) for long-running operations like device filtering with VC detection.
- Jobs run via Redis Queue (RQ) in Redis, separate from the database Job model. Real-time status must be checked via RQ, not the database.

## Critical Job Architecture Points
- Job UUID (`job.job_id`) is used for RQ API endpoints: `/api/core/background-tasks/{uuid}/`
- Job PK (`job.pk`) is used for database endpoints and result loading
- RQ status values: `queued`, `started`, `finished`, `stopped`, `failed` (NOT `completed`)
- Database Job status values: `pending`, `scheduled`, `running`, `completed`, `failed`, `errored` (NO `cancelled` status exists)
- Check `rq_job.is_stopped` or `rq_job.is_failed` flags in Redis for cancellation detection, not database status

## Job Cancellation Flow
1. Call `/api/core/background-tasks/{uuid}/stop/` to stop RQ job
2. Call plugin's sync endpoint `/api/plugins/librenms_plugin/jobs/{pk}/sync-status/` to update database
3. Frontend polling detects status changes and redirects appropriately

## Polling Implementation
- Poll `/api/core/background-tasks/{uuid}/` for real-time RQ status
- Update modal messages based on status: "Job queued...", "Processing...", "Job completed!"
- Handle all RQ status values explicitly to avoid infinite polling
- Use `cancelInProgress` flag to prevent polling interference during cancellation

## Custom Sync Endpoint
`api/views.py::sync_job_status()` syncs database Job status with RQ job status, needed because NetBox worker doesn't always update DB when jobs stop before processing starts.
