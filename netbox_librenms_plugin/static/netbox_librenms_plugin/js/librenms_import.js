/**
 * librenms_import.js
 *
 * Handles device import workflow functionality for LibreNMS Plugin:
 * - Filter form submission with background job support
 * - Job polling and cancellation
 * - Modal management for import actions
 * - Bulk device selection and import
 *
 * Dependencies: Bootstrap 5, HTMX 2.x
 */

// Wrap in IIFE to prevent const redeclaration errors during HTMX swaps
(function () {
    'use strict';

    // Guard against re-initialization during HTMX content swaps
    // The flag is reset before document.write() to allow proper initialization after page replacement
    if (window.LibreNMSImportInitialized) {
        return;
    }

    // Mark as initialized for this page session
    window.LibreNMSImportInitialized = true;

    // ============================================
    // CONSTANTS
    // ============================================

    // Job polling and cancellation timeouts
    const POLL_INTERVAL_MS = 2000;
    const JOB_CANCEL_WAIT_MS = 500;
    const JOB_CANCEL_REDIRECT_MS = 800;
    const JOB_CANCEL_ERROR_REDIRECT_MS = 1000;

    // Modal auto-close timeout
    const MODAL_AUTO_CLOSE_MS = 3000;

    // ============================================
    // MODAL MANAGER CLASS
    // ============================================

    /**
     * Modal Manager - Centralized Bootstrap modal handling with fallback support.
     * Tracks modal instances and provides consistent show/hide behavior across all modals.
     * Eliminates code duplication and provides state tracking.
     */
    class ModalManager {
        constructor(modalElement) {
            this.modal = modalElement;
            this.instance = null;
            this.backdropElement = null;
            this.isVisible = false;
        }

        /**
         * Show the modal using best available method.
         * @returns {boolean} Success status
         */
        show() {
            if (!this.modal) return false;

            // Try Bootstrap 5 (preferred)
            if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
                this.instance = bootstrap.Modal.getInstance(this.modal) || new bootstrap.Modal(this.modal);
                this.instance.show();
                this.isVisible = true;
                return true;
            }

            // Try window.bootstrap (alternate)
            if (typeof window.bootstrap !== 'undefined' && window.bootstrap.Modal) {
                this.instance = window.bootstrap.Modal.getInstance(this.modal) || new window.bootstrap.Modal(this.modal);
                this.instance.show();
                this.isVisible = true;
                return true;
            }

            // Fallback: Manual DOM manipulation
            this._showManual();
            this.isVisible = true;
            return true;
        }

        /**
         * Hide the modal using best available method.
         * @returns {boolean} Success status
         */
        hide() {
            if (!this.modal) return false;

            // Try Bootstrap instance
            if (this.instance && typeof this.instance.hide === 'function') {
                this.instance.hide();
                this.isVisible = false;
                return true;
            }

            // Fallback: Manual
            this._hideManual();
            this.isVisible = false;
            return true;
        }

        /**
         * Manual show implementation (no Bootstrap).
         * @private
         */
        _showManual() {
            if (!this.modal.classList.contains('show')) {
                this.modal.classList.add('show');
                this.modal.style.display = 'block';
                this.modal.removeAttribute('aria-hidden');
                this.modal.setAttribute('aria-modal', 'true');
            }

            if (!document.body.classList.contains('modal-open')) {
                document.body.classList.add('modal-open');
            }

            if (!this.backdropElement && !document.querySelector('.modal-backdrop')) {
                this.backdropElement = document.createElement('div');
                this.backdropElement.className = 'modal-backdrop fade show';
                document.body.appendChild(this.backdropElement);
            }
        }

        /**
         * Manual hide implementation (no Bootstrap).
         * @private
         */
        _hideManual() {
            this.modal.classList.remove('show');
            this.modal.style.display = '';
            this.modal.setAttribute('aria-hidden', 'true');
            this.modal.removeAttribute('aria-modal');
            document.body.classList.remove('modal-open');

            if (this.backdropElement) {
                this.backdropElement.remove();
                this.backdropElement = null;
            } else {
                const backdrop = document.querySelector('.modal-backdrop');
                if (backdrop) {
                    backdrop.remove();
                }
            }
        }

        /**
         * Update modal content and styling.
         * @param {Object} options - Content update options
         */
        updateContent(options = {}) {
            const {
                title = null,
                message = null,
                messageHTML = null,
                showSpinner = true,
                messageClasses = ['text-muted'],
                removeClasses = []
            } = options;

            const spinner = this.modal.querySelector('.spinner-border');
            const titleEl = this.modal.querySelector('h5');
            const messageEl = this.modal.querySelector('[id$="-message"]');

            if (spinner) {
                spinner.style.display = showSpinner ? 'block' : 'none';
            }

            if (titleEl && title !== null) {
                titleEl.textContent = title;
            }

            if (messageEl) {
                if (removeClasses.length > 0) {
                    removeClasses.forEach(cls => messageEl.classList.remove(cls));
                }
                if (messageClasses.length > 0) {
                    messageClasses.forEach(cls => messageEl.classList.add(cls));
                }
                if (messageHTML !== null) {
                    messageEl.innerHTML = messageHTML;
                } else if (message !== null) {
                    messageEl.textContent = message;
                }
            }
        }

        /**
         * Check if modal is currently visible.
         * @returns {boolean}
         */
        get visible() {
            return this.isVisible;
        }

        /**
         * Cleanup and remove modal instance.
         */
        dispose() {
            if (this.instance && typeof this.instance.dispose === 'function') {
                this.instance.dispose();
            }
            this._hideManual(); // Ensure manual cleanup
            this.instance = null;
            this.isVisible = false;
        }
    }

    // ============================================
    // UTILITY FUNCTIONS
    // ============================================

    /**
     * Get CSRF token from cookies.
     *
     * @param {string} name - Cookie name
     * @returns {string|null} Cookie value
     */
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    // ============================================
    // MODAL MANAGEMENT
    // ============================================

    /**
     * Show a Bootstrap modal with fallback support.
     * Legacy wrapper for HTMX handlers - prefer ModalManager for new code.
     *
     * @param {HTMLElement} modalElement - The modal element to show
     * @param {Object} fallbackBackdropRef - Reference object to store fallback backdrop (deprecated)
     */
    function showModal(modalElement, fallbackBackdropRef) {
        if (!modalElement) {
            return;
        }

        const manager = new ModalManager(modalElement);
        manager.show();
        
        // Store backdrop reference for legacy compatibility
        if (fallbackBackdropRef && manager.backdropElement) {
            fallbackBackdropRef.element = manager.backdropElement;
        }
    }

    /**
     * Hide a Bootstrap modal with fallback support.
     * Legacy wrapper for HTMX handlers - prefer ModalManager for new code.
     *
     * @param {HTMLElement} modalElement - The modal element to hide
     * @param {Object} fallbackBackdropRef - Reference object containing fallback backdrop (deprecated)
     */
    function hideModal(modalElement, fallbackBackdropRef) {
        if (!modalElement) {
            return;
        }

        const manager = new ModalManager(modalElement);
        manager.hide();
    }

    // ============================================
    // JOB POLLING & CANCELLATION
    // ============================================

    /**
     * Poll background job status and reload page when complete.
     * Handles NetBox RQ job status polling with cancellation support.
     *
     * @param {string} jobId - Job UUID for API polling
     * @param {number} jobPk - Job PK for result loading
     * @param {string} pollUrl - API endpoint to poll
     * @param {string} baseUrl - Base URL for reload
     * @param {string} originalFilters - Original filter params
     * @param {number} deviceCount - Number of devices being processed
     */
    function pollJobStatus(jobId, jobPk, pollUrl, baseUrl, originalFilters, deviceCount) {
        const messageEl = document.getElementById('filter-progress-message');
        const cancelBtn = document.getElementById('cancel-filter-btn');

        // Get CSRF token from cookie or form (needed for cancel and status sync)
        let csrfToken = getCookie('csrftoken');
        if (!csrfToken) {
            const csrfInput = document.querySelector('[name=csrfmiddlewaretoken]');
            if (csrfInput) {
                csrfToken = csrfInput.value;
            }
        }

        // Update modal message with device count
        if (messageEl) {
            if (deviceCount !== undefined && deviceCount !== null) {
                messageEl.textContent = `Found ${deviceCount} device${deviceCount !== 1 ? 's' : ''}, processing in background (job can be cancelled)...`;
                // Show device count in dedicated element
                const deviceCountEl = document.getElementById('filter-device-count');
                const deviceCountValueEl = document.getElementById('filter-device-count-value');
                if (deviceCountEl && deviceCountValueEl) {
                    deviceCountValueEl.textContent = deviceCount;
                    deviceCountEl.style.display = 'block';
                }
            } else {
                messageEl.textContent = 'Processing filters in background (job can be cancelled)...';
            }
        }

        // Track cancel state - must be declared before onclick handler uses it
        let cancelInProgress = false;

        // Wire cancel button to stop the job
        // jobId here is the UUID (job.job_id), not the integer pk
        if (cancelBtn) {
            cancelBtn.onclick = function () {
                // Mark that cancel is in progress to stop polling interference
                cancelInProgress = true;

                // Disable the cancel button to prevent double-clicks
                cancelBtn.disabled = true;
                const originalText = cancelBtn.textContent;

                // Update modal message
                if (messageEl) {
                    messageEl.textContent = 'Cancelling job...';
                }
                cancelBtn.textContent = 'Cancelling...';

                // Stop the job using NetBox's correct background task stop endpoint
                const stopUrl = `/api/core/background-tasks/${jobId}/stop/`;

                fetch(stopUrl, {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': csrfToken,
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    }
                })
                    .then(res => {
                        // Check if job already finished (404 = gone from queue)
                        if (res.status === 404 || res.status === 410) {
                            if (messageEl) {
                                messageEl.textContent = 'Job already completed, loading results...';
                            }
                            cancelBtn.textContent = 'Completed';

                            const modal = document.getElementById('filter-processing-modal');
                            if (modal) {
                                const manager = new ModalManager(modal);
                                manager.hide();
                            }

                            setTimeout(() => {
                                window.location.href = baseUrl + '?' + originalFilters + '&job_id=' + jobPk;
                            }, 100);
                            return Promise.resolve();
                        }

                        if (res.ok) {
                            // Job stopped or already gone
                            if (messageEl) {
                                messageEl.textContent = 'Verifying job stopped...';
                            }
                            cancelBtn.textContent = 'Verifying...';

                            // Wait for RQ to update
                            return new Promise(resolve => setTimeout(resolve, JOB_CANCEL_WAIT_MS))
                                .then(() => {
                                    // Sync database status via our plugin API
                                    if (messageEl) {
                                        messageEl.textContent = 'Updating job status...';
                                    }
                                    cancelBtn.textContent = 'Updating...';

                                    const syncUrl = `/api/plugins/librenms_plugin/jobs/${jobPk}/sync-status/`;

                                    return fetch(syncUrl, {
                                        method: 'POST',
                                        headers: {
                                            'X-CSRFToken': csrfToken,
                                            'Accept': 'application/json'
                                        }
                                    });
                                })
                                .then(syncRes => {
                                    return syncRes.json().then(data => {
                                        if (syncRes.ok) {
                                            if (messageEl) {
                                                messageEl.textContent = 'Job cancelled successfully.';
                                            }
                                            cancelBtn.textContent = 'Cancelled';

                                            const modal = document.getElementById('filter-processing-modal');
                                            if (modal) {
                                                const manager = new ModalManager(modal);
                                                manager.hide();
                                            }

                                            setTimeout(() => {
                                                window.location.href = baseUrl;
                                            }, JOB_CANCEL_REDIRECT_MS);
                                        } else {
                                            throw new Error(`Sync failed: ${syncRes.status}`);
                                        }
                                    });
                                })
                                .catch(syncErr => {
                                    // Still redirect even if sync fails - job is stopped in RQ
                                    if (messageEl) {
                                        messageEl.textContent = 'Job stopped (status sync failed).';
                                    }
                                    cancelBtn.textContent = 'Stopped';

                                    const modal = document.getElementById('filter-processing-modal');
                                    if (modal) {
                                        const manager = new ModalManager(modal);
                                        manager.hide();
                                    }

                                    setTimeout(() => {
                                        window.location.href = baseUrl;
                                    }, JOB_CANCEL_ERROR_REDIRECT_MS);
                                });
                        } else {
                            if (res.status === 404 || res.status === 410 || res.status === 400) {
                                if (messageEl) {
                                    messageEl.textContent = 'Job completed, loading results...';
                                }
                                cancelBtn.textContent = 'Completed';

                                const modal = document.getElementById('filter-processing-modal');
                                if (modal) {
                                    const manager = new ModalManager(modal);
                                    manager.hide();
                                }

                                setTimeout(() => {
                                    window.location.href = baseUrl + '?' + originalFilters + '&job_id=' + jobPk;
                                }, 100);
                                return;
                            }

                            // Genuine failure - close modal anyway, don't trap user
                            if (messageEl) {
                                messageEl.textContent = 'Failed to cancel job. Closing...';
                            }
                            cancelBtn.textContent = 'Close';
                            cancelBtn.disabled = false;

                            const modal = document.getElementById('filter-processing-modal');
                            if (modal) {
                                const manager = new ModalManager(modal);
                                manager.hide();
                            }

                            setTimeout(() => window.location.href = baseUrl, 1000);
                        }
                    })
                    .catch(err => {
                        if (messageEl) {
                            messageEl.textContent = 'Error cancelling job.';
                        }
                        cancelBtn.textContent = originalText;
                        cancelBtn.disabled = false;
                        alert('Error stopping job. Please try again.');
                    });
            };
        }

        // Poll function
        let pollingStopped = false; // Flag to stop polling
        const poll = () => {
            if (cancelInProgress) {
                // Cancel handler is taking over, don't interfere
                return;
            }

            if (pollingStopped) {
                return;
            }

            fetch(pollUrl, {
                headers: {
                    'Accept': 'application/json'
                }
            })
                .then(res => {
                    if (!res.ok) {
                        if (res.status === 404) {
                            // Job no longer in Redis queue, fall back to database endpoint
                            // Disable cancel button since we can't cancel jobs that left the queue
                            if (cancelBtn) {
                                cancelBtn.disabled = true;
                                cancelBtn.textContent = 'Job Finalizing...';
                            }
                            // Fall back to database endpoint (uses UUID job_id)
                            return fetch(`/api/core/jobs/${jobId}/`, {
                                headers: {
                                    'Accept': 'application/json'
                                }
                            }).then(dbRes => {
                                if (!dbRes.ok) {
                                    throw new Error(`Database job not found: ${dbRes.status}`);
                                }
                                return dbRes.json();
                            });
                        }
                        throw new Error(`HTTP ${res.status}`);
                    }
                    return res.json();
                })
                .then(data => {
                    // NetBox API returns status as an object with 'value' and 'label' fields
                    const statusValue = data.status?.value || data.status;

                    // Update modal message based on status
                    if (messageEl) {
                        const statusMessages = {
                            'queued': 'Job queued, waiting to start...',
                            'scheduled': 'Job scheduled...',
                            'started': 'Processing filters...',
                            'deferred': 'Job deferred...',
                            'finished': 'Job completed!',
                            'completed': 'Job completed!',
                            'failed': 'Job failed.',
                            'stopped': 'Job stopped.',
                            'errored': 'Job encountered an error.'
                        };
                        messageEl.textContent = statusMessages[statusValue] || `Job status: ${statusValue}`;
                    }

                    if (statusValue === 'completed' || statusValue === 'finished') {
                        pollingStopped = true; // Stop future polls

                        const modal = document.getElementById('filter-processing-modal');
                        if (modal) {
                            const manager = new ModalManager(modal);
                            manager.hide();
                        }

                        // Small delay to let modal close before redirect
                        setTimeout(() => {
                            window.location.href = baseUrl + '?' + originalFilters + '&job_id=' + jobPk;
                        }, 100);
                        return; // Stop polling
                    } else if (statusValue === 'stopped') {
                        pollingStopped = true;

                        const modal = document.getElementById('filter-processing-modal');
                        if (modal) {
                            const manager = new ModalManager(modal);
                            manager.hide();
                        }

                        setTimeout(() => window.location.href = baseUrl, 100);
                    } else if (statusValue === 'failed') {
                        pollingStopped = true;

                        const modal = document.getElementById('filter-processing-modal');
                        if (modal) {
                            const manager = new ModalManager(modal);
                            manager.hide();
                        }

                        const errorMsg = data.data?.error;
                        if (errorMsg) {
                            alert('Error: ' + errorMsg);
                        }
                        setTimeout(() => window.location.href = baseUrl, 100);
                    } else if (statusValue === 'errored') {
                        pollingStopped = true;

                        const modal = document.getElementById('filter-processing-modal');
                        if (modal) {
                            const manager = new ModalManager(modal);
                            manager.hide();
                        }

                        const errorMsg = data.data?.error || 'Job encountered an error. Please try again.';
                        alert('Error: ' + errorMsg);
                        setTimeout(() => window.location.href = baseUrl, 100);
                    } else if (statusValue === 'queued' || statusValue === 'started' || statusValue === 'deferred' || statusValue === 'scheduled') {
                        // Continue polling (job still in progress)
                        setTimeout(poll, POLL_INTERVAL_MS);
                    } else {
                        // Unknown status - continue polling
                        setTimeout(poll, POLL_INTERVAL_MS);
                    }
                })
                .catch(err => {
                    // Retry polling on network errors
                    setTimeout(poll, POLL_INTERVAL_MS);
                });
        };

        // Start polling
        poll();
    }

    // ============================================
    // FILTER FORM INITIALIZATION
    // ============================================

    /**
     * Initialize the LibreNMS import filter form.
     * Handles form submission, background job triggering, and cancellation.
     */
    function initializeFilterForm() {
        const filterForm = document.getElementById('librenms-import-filter-form');
        if (!filterForm) {
            return;
        }

        // Initialize modal manager
        const filterModal = document.getElementById('filter-processing-modal');
        const filterModalManager = filterModal ? new ModalManager(filterModal) : null;

        // AbortController for cancelling the filter request
        let currentAbortController = null;

        filterForm.addEventListener('submit', function (e) {
            e.preventDefault();

            if (!filterModalManager) {
                return;
            }

            // Validate that at least one filter is set before proceeding
            const filterFields = [
                'librenms_location',
                'librenms_type',
                'librenms_os',
                'librenms_hostname',
                'librenms_sysname'
            ];

            const hasFilter = filterFields.some(fieldName => {
                const field = filterForm.elements[fieldName];
                return field && field.value && field.value.trim() !== '';
            });

            if (!hasFilter) {
                // Show validation error in modal
                const deviceCount = document.getElementById('filter-device-count');
                const cancelBtn = document.getElementById('cancel-filter-btn');

                filterModalManager.updateContent({
                    title: 'Filter Required',
                    messageHTML: '<i class="mdi mdi-alert text-warning"></i> Please select at least one LibreNMS filter before applying the search.',
                    showSpinner: false,
                    messageClasses: ['text-warning'],
                    removeClasses: ['text-muted']
                });

                if (deviceCount) deviceCount.style.display = 'none';
                
                if (cancelBtn) {
                    cancelBtn.innerHTML = '<i class="mdi mdi-close"></i> Close';
                    cancelBtn.onclick = function () {
                        filterModalManager.hide();
                        // Reset modal content for next use
                        filterModalManager.updateContent({
                            title: 'Applying Filters',
                            message: 'Fetching LibreNMS data and processing filters...',
                            showSpinner: true,
                            messageClasses: ['text-muted'],
                            removeClasses: ['text-warning']
                        });
                    };
                }

                filterModalManager.show();
                return;
            }

            // Disable the apply filters button to prevent double submission
            const applyBtn = document.getElementById('apply-filters-btn');
            if (applyBtn) {
                applyBtn.disabled = true;
                applyBtn.innerHTML = '<i class="mdi mdi-loading mdi-spin"></i> Processing...';
            }

            // Cancel any existing request before starting a new one
            if (currentAbortController) {
                currentAbortController.abort();
                currentAbortController = null;
            }

            // Create new AbortController for this request
            currentAbortController = new AbortController();

            // Show filter processing modal
            filterModalManager.show();

            // Handle cancel button - actually abort the request
            const cancelBtn = document.getElementById('cancel-filter-btn');
            if (cancelBtn) {
                cancelBtn.onclick = function () {
                    // Abort the fetch request
                    if (currentAbortController) {
                        currentAbortController.abort();
                        currentAbortController = null;
                    }

                    // Re-enable the apply filters button
                    if (applyBtn) {
                        applyBtn.disabled = false;
                        applyBtn.innerHTML = '<i class="mdi mdi-filter"></i> Apply Filters';
                    }

                    // Hide modal
                    filterModalManager.hide();
                };
            }

            // Build completely clean URL parameters from current form state only
            const params = new URLSearchParams();
            // Add all text/select fields with non-empty values
            const inputs = this.querySelectorAll('input:not([type="checkbox"]), select');
            inputs.forEach(input => {
                // Skip job_id parameter - we want a fresh filter, not loading old results
                if (input.name && input.value && input.name !== 'job_id') {
                    params.append(input.name, input.value);
                }
            });

            // Explicitly add ONLY currently checked checkboxes
            const checkboxes = ['id_enable_vc_detection', 'id_clear_cache', 'id_show_disabled', 'id_exclude_existing', 'id_use_background_job'];
            checkboxes.forEach(id => {
                const checkbox = document.getElementById(id);
                if (checkbox?.checked && checkbox.name) {
                    params.set(checkbox.name, 'on');
                }
            });

            // Strip any existing query parameters from the action URL
            const baseUrl = this.action.split('?')[0];

            // Build final URL
            const finalUrl = baseUrl + '?' + params.toString();

            // Store original filters for reload after job completion
            const originalFilters = params.toString();

            // Use fetch with AbortController instead of window.location
            fetch(finalUrl, {
                method: 'GET',
                signal: currentAbortController.signal,
                headers: {
                    'Accept': 'application/json, text/html'
                }
            })
                .then(response => {
                    // Check if response is JSON (background job) or HTML (synchronous)
                    const contentType = response.headers.get('content-type');
                    if (contentType && contentType.includes('application/json')) {
                        return response.json().then(data => ({ type: 'json', data }));
                    }
                    return response.text().then(html => ({ type: 'html', html }));
                })
                .then(result => {
                    if (result.type === 'json') {
                        // Background job response
                        if (result.data.use_polling && result.data.job_id && result.data.poll_url) {
                            // Start polling for background job
                            // job_id is UUID for API, job_pk is integer for result loading
                            pollJobStatus(
                                result.data.job_id,
                                result.data.job_pk,
                                result.data.poll_url,
                                baseUrl,
                                originalFilters,
                                result.data.device_count
                            );
                        } else {
                            // Unexpected JSON response
                            alert('Unexpected response from server. Please try again.');
                            if (modalInstance) {
                                modalInstance.hide();
                            }
                        }
                    } else if (result.type === 'html') {
                        // Synchronous response - navigate to the URL to reload with results
                        if (modalInstance) {
                            modalInstance.hide();
                        }
                        // Navigate to the results URL, allowing proper browser history
                        window.location.href = finalUrl;
                    }
                })
                .catch(error => {
                    // Re-enable the apply filters button
                    if (applyBtn) {
                        applyBtn.disabled = false;
                        applyBtn.innerHTML = '<i class="mdi mdi-filter"></i> Apply Filters';
                    }

                    if (error.name === 'AbortError') {
                        // Request was cancelled by user - silent
                    } else {
                        console.error('Error fetching filtered results:', error);
                        alert('Error loading filtered results. Please try again.');
                    }

                    // Hide modal on error
                    filterModalManager.hide();
                    currentAbortController = null;
                });
        });
    }

    // ============================================
    // SELECTION STATE MANAGEMENT
    // ============================================

    /**
     * Get all device selection checkboxes.
     * @returns {NodeList} List of checkbox elements
     */
    function getCheckboxes() {
        return document.querySelectorAll('input[name="select"]');
    }

    /**
     * Capture current selection state for restoration.
     * @returns {Array} Array of checkbox states
     */
    function captureSelectionState() {
        return Array.from(getCheckboxes()).map(cb => ({
            value: cb.value,
            checked: cb.checked,
        }));
    }

    /**
     * Restore previous selection state.
     * @param {Array} state - Previously captured state
     */
    function restoreSelectionState(state) {
        if (!state) {
            return;
        }
        const checkboxes = getCheckboxes();
        state.forEach(saved => {
            const checkbox = Array.from(checkboxes).find(cb => cb.value === saved.value);
            if (checkbox) {
                checkbox.checked = saved.checked;
            }
        });
        updateSelectionDisplay();
    }

    /**
     * Update the selection count display and import button state.
     */
    function updateSelectionDisplay() {
        const bulkImportBtn = document.getElementById('bulk-import-btn');
        const selectionCount = document.getElementById('selection-count');
        const importCount = document.getElementById('import-count');

        if (!bulkImportBtn || !selectionCount || !importCount) {
            return;
        }

        const checkboxes = getCheckboxes();
        const selected = Array.from(checkboxes).filter(cb => cb.checked);
        const count = selected.length;

        selectionCount.innerHTML = `<i class="mdi mdi-information"></i> ${count} device${count !== 1 ? 's' : ''} selected`;
        importCount.textContent = count;
        bulkImportBtn.disabled = count === 0;
    }

    // ============================================
    // BULK IMPORT INITIALIZATION
    // ============================================

    /**
     * Initialize bulk device import functionality.
     * Handles device selection, bulk actions, and single-row imports.
     */
    function initializeBulkImport() {
        const bulkImportBtn = document.getElementById('bulk-import-btn');
        let pendingRowImport = null;

        // Initialize selection display
        updateSelectionDisplay();

        // Select all ready devices button
        const selectAllReadyBtn = document.getElementById('select-all-ready');
        if (selectAllReadyBtn) {
            selectAllReadyBtn.addEventListener('click', function () {
                const checkboxes = getCheckboxes();
                checkboxes.forEach(cb => {
                    // Select checkboxes for rows that have a ready-to-import button (device-ready class)
                    if (!cb.disabled && cb.closest('tr')?.querySelector('.device-ready')) {
                        cb.checked = true;
                    }
                });
                updateSelectionDisplay();
            });
        }

        // Deselect all button
        const selectNoneBtn = document.getElementById('select-none');
        if (selectNoneBtn) {
            selectNoneBtn.addEventListener('click', function () {
                const checkboxes = getCheckboxes();
                checkboxes.forEach(cb => {
                    cb.checked = false;
                });
                updateSelectionDisplay();
            });
        }

        // Use event delegation for checkbox changes since they can be dynamically added
        document.body.addEventListener('change', function (event) {
            if (event.target.matches('input[name="select"]')) {
                updateSelectionDisplay();
            }
        });

        // Handle single-row import buttons
        document.body.addEventListener('click', function (event) {
            const rowButton = event.target.closest('.device-import-btn.device-ready');
            if (!rowButton || rowButton.disabled) {
                return;
            }

            if (!bulkImportBtn || pendingRowImport) {
                return;
            }

            const deviceId = rowButton.dataset.deviceId;
            if (!deviceId) {
                return;
            }

            event.preventDefault();

            const previousSelections = captureSelectionState();
            pendingRowImport = { previousSelections };

            const checkboxes = getCheckboxes();
            checkboxes.forEach(cb => {
                cb.checked = false;
            });

            const targetCheckbox = Array.from(checkboxes).find(cb => cb.value === deviceId);
            if (!targetCheckbox) {
                restoreSelectionState(previousSelections);
                pendingRowImport = null;
                return;
            }

            targetCheckbox.checked = true;
            updateSelectionDisplay();

            bulkImportBtn.click();
        });

        // Restore selection after HTMX updates
        document.body.addEventListener('htmx:afterSwap', function (event) {
            if (event.detail.target.tagName === 'TR') {
                updateSelectionDisplay();
            }

            if (event.detail.target.id === 'import-results-modal-content') {
                const failedCount = event.detail.target.querySelector('[data-failed-count]');
                if (failedCount && failedCount.dataset.failedCount === '0') {
                    setTimeout(() => {
                        const resultsModal = document.getElementById('import-results-modal');
                        if (resultsModal && typeof bootstrap !== 'undefined' && bootstrap.Modal) {
                            const modalInstance = bootstrap.Modal.getInstance(resultsModal);
                            if (modalInstance) {
                                modalInstance.hide();
                            }
                        }
                        window.location.reload();
                    }, MODAL_AUTO_CLOSE_MS);
                }
            }
        });

        document.body.addEventListener('htmx:afterRequest', function (event) {
            if (event.target === bulkImportBtn && pendingRowImport) {
                restoreSelectionState(pendingRowImport.previousSelections);
                pendingRowImport = null;
            }
        });

        document.body.addEventListener('htmx:responseError', function (event) {
            if (event.target === bulkImportBtn && pendingRowImport) {
                restoreSelectionState(pendingRowImport.previousSelections);
                pendingRowImport = null;
            }
        });

        // SessionStorage management for device roles
        const currentUrl = window.location.href;
        const lastUrl = sessionStorage.getItem('last_import_url');

        if (currentUrl !== lastUrl) {
            Object.keys(sessionStorage).forEach(key => {
                if (key.startsWith('device_role_')) {
                    sessionStorage.removeItem(key);
                }
            });
        }

        sessionStorage.setItem('last_import_url', currentUrl);
    }

    // ============================================
    // HTMX HANDLERS INITIALIZATION
    // ============================================

    /**
     * Initialize HTMX-specific event handlers and modal management.
     */
    function initializeHTMXHandlers() {
        const modalElement = document.getElementById('htmx-modal');
        const modalContent = document.getElementById('htmx-modal-content');
        const fallbackBackdropRef = { element: null };

        // Configure HTMX to include CSRF token in all requests
        document.body.addEventListener('htmx:configRequest', function (event) {
            const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');
            if (csrfToken) {
                event.detail.headers['X-CSRFToken'] = csrfToken.value;
            }
        });



        // Initialize Bootstrap tooltips
        if (typeof bootstrap !== 'undefined' && bootstrap.Tooltip) {
            const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
            [...tooltipTriggerList].map(el => new bootstrap.Tooltip(el));
        }

        /**
         * Ensure modal is visible after HTMX swap.
         */
        function ensureModalVisible(event) {
            if (!modalElement || event.detail.target.id !== 'htmx-modal-content') {
                return;
            }

            if (modalContent && modalContent.innerHTML.trim().length === 0 && event.detail.xhr) {
                modalContent.innerHTML = event.detail.xhr.responseText;
            }

            showModal(modalElement, fallbackBackdropRef);
        }

        document.body.addEventListener('htmx:afterSwap', ensureModalVisible);

        // Handle modal dismiss buttons
        document.body.addEventListener('click', function (event) {
            if (!modalElement) {
                return;
            }

            const dismissTrigger = event.target.closest('[data-bs-dismiss="modal"]');
            if (dismissTrigger) {
                event.preventDefault();

                // Check if it's in the HTMX modal
                if (modalElement.contains(dismissTrigger)) {
                    hideModal(modalElement, fallbackBackdropRef);
                }
            }
        });

        // Handle backdrop clicks for HTMX modal
        modalElement?.addEventListener('click', function (event) {
            if (event.target === modalElement) {
                hideModal(modalElement, fallbackBackdropRef);
            }
        });

        // Handle Escape key to close modal
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                if (modalElement?.classList.contains('show')) {
                    hideModal(modalElement, fallbackBackdropRef);
                }
            }
        });

        // Listen for HTMX closeModal trigger to close the modal programmatically
        document.body.addEventListener('closeModal', function () {
            hideModal(modalElement, fallbackBackdropRef);
        });
    }

    // ============================================
    // CACHE EXPIRATION COUNTDOWN
    // ============================================

    /**
     * Create a countdown timer for a cache expiration.
     * Reusable function that handles the countdown logic.
     *
     * @param {Date} expiresAt - When the cache expires
     * @param {HTMLElement} countdownElement - Element to update with countdown text
     * @param {Function} onExpired - Optional callback when countdown reaches zero
     * @param {HTMLElement} warningElement - Optional element to add warning class to
     * @returns {Function} The update function (for immediate invocation)
     */
    function createCacheCountdown(expiresAt, countdownElement, onExpired, warningElement) {
        function updateCountdown() {
            const now = new Date();
            const remainingMs = expiresAt - now;

            if (remainingMs <= 0) {
                // Cache has expired
                countdownElement.textContent = 'expired';
                countdownElement.classList.add('text-danger');
                if (onExpired) {
                    onExpired();
                }
                return; // Stop updating
            }

            // Format remaining time
            const seconds = Math.floor(remainingMs / 1000);
            const minutes = Math.floor(seconds / 60);
            const secs = seconds % 60;

            countdownElement.textContent = minutes > 0 ? `${minutes}m ${secs}s` : `${secs}s`;

            // Visual warning when under 60 seconds
            if (seconds < 60) {
                if (!countdownElement.classList.contains('text-warning')) {
                    countdownElement.classList.add('text-warning');
                    if (warningElement) {
                        warningElement.classList.add('text-warning');
                    }
                }
            }

            // Continue updating
            setTimeout(updateCountdown, 1000);
        }

        return updateCountdown;
    }

    /**
     * Update the cached searches badge count.
     * Counts visible cached search buttons and updates the badge.
     */
    function updateCachedSearchesBadge() {
        const badge = document.getElementById('cached-searches-badge');
        if (!badge) {
            return;
        }

        // Count visible cached search buttons
        const buttons = document.querySelectorAll('.cached-search-countdown');
        let visibleCount = 0;
        buttons.forEach(function (element) {
            const button = element.closest('.btn');
            if (button && button.style.display !== 'none') {
                visibleCount++;
            }
        });

        // Update badge text
        const badgeElement = badge.querySelector('.badge');
        if (badgeElement) {
            badgeElement.textContent = visibleCount;
        }
    }

    /**
     * Initialize countdown timers for cached search items.
     * Each cached search button shows a live countdown until that cache expires.
     */
    function initializeCachedSearchCountdowns() {
        const countdownElements = document.querySelectorAll('.cached-search-countdown');
        if (countdownElements.length === 0) {
            return;
        }

        countdownElements.forEach(function (element) {
            const cacheTimestamp = element.dataset.cacheTimestamp;
            const cacheTimeout = parseInt(element.dataset.cacheTimeout, 10);

            if (!cacheTimestamp || !cacheTimeout) {
                return;
            }

            const cachedAt = new Date(cacheTimestamp);
            if (isNaN(cachedAt.getTime())) {
                console.warn('[Cached Search Countdown] Invalid timestamp:', cacheTimestamp);
                return;
            }

            const expiresAt = new Date(cachedAt.getTime() + cacheTimeout * 1000);

            // Create countdown with expiration handler to hide the button and update badge
            const updateCountdown = createCacheCountdown(
                expiresAt,
                element,
                function onExpired() {
                    const button = element.closest('.btn');
                    if (button) {
                        button.style.display = 'none';
                        // Update the badge count after hiding the button
                        updateCachedSearchesBadge();
                    }
                }
            );

            // Start countdown
            updateCountdown();
        });
    }

    /**
     * Display live countdown for cache expiration.
     * Shows time remaining until cached filter results expire.
     */
    function initializeCacheExpirationMonitor() {
        const cacheInfoDisplay = document.getElementById('cache-info-display');
        if (!cacheInfoDisplay) {
            return; // No cache data present
        }

        const cacheTimestamp = cacheInfoDisplay.dataset.cacheTimestamp;
        const cacheTimeout = parseInt(cacheInfoDisplay.dataset.cacheTimeout, 10);

        if (!cacheTimestamp || !cacheTimeout) {
            return;
        }

        const cachedAt = new Date(cacheTimestamp);
        if (isNaN(cachedAt.getTime())) {
            console.warn('[Cache Monitor] Invalid cache timestamp:', cacheTimestamp);
            return;
        }

        const expiresAt = new Date(cachedAt.getTime() + cacheTimeout * 1000);
        const countdownSpan = document.getElementById('cache-expiry-countdown');
        const iconElement = cacheInfoDisplay.querySelector('.mdi');

        if (!countdownSpan) {
            return;
        }

        // Create countdown with expiration handler to update icon and styling
        const updateCountdown = createCacheCountdown(
            expiresAt,
            countdownSpan,
            function onExpired() {
                if (iconElement) {
                    iconElement.classList.remove('mdi-clock-outline');
                    iconElement.classList.add('mdi-clock-alert', 'text-danger');
                }
                cacheInfoDisplay.classList.remove('text-muted');
                cacheInfoDisplay.classList.add('text-danger');
                cacheInfoDisplay.title = 'Cache expired - refresh page to reload data';
            },
            iconElement
        );

        // Start countdown
        updateCountdown();
    }

    // ============================================
    // INITIALIZATION
    // ============================================

    /**
     * Initialize all import page functionality.
     * Called when DOM is ready or immediately if already loaded.
     */
    function initializeImportPage() {
        initializeFilterForm();
        initializeBulkImport();
        initializeHTMXHandlers();
        initializeCachedSearchCountdowns();
        initializeCacheExpirationMonitor();
    }

    // Handle both cases: DOM already loaded or still loading
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeImportPage);
    } else {
        // DOM already loaded, initialize immediately
        initializeImportPage();
    }

})(); // End of IIFE
