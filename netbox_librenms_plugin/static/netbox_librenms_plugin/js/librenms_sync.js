/**
 * librenms_sync.js
 *
 * Handles LibreNMS data synchronization for devices/VMs:
 * - Interface, cable, and IP address comparison tables
 * - Virtual chassis member selection and verification
 * - VRF assignment and verification
 * - Bulk operations and filtering
 * - Cache countdown timers
 *
 * Dependencies: Bootstrap 5, TomSelect, HTMX 2.x
 */

// ============================================
// CONSTANTS
// ============================================

const TOMSELECT_INIT_DELAY_MS = 100;
const COUNTDOWN_UPDATE_INTERVAL_MS = 1000;
const SYNC_CACHE_STATUS_TIMEOUT_MS = 15000;
const STALE_SYNC_CACHE_STATUS = Symbol('stale sync cache status');

/**
 * Return the CSRF token value, or null when the hidden input is missing/empty.
 * Callers MUST bail (running any needed UI cleanup) on null instead of reading
 * `.value` off a missing element, which throws a TypeError and breaks the handler.
 * @returns {string|null}
 */
function getCsrfToken() {
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    return input && input.value ? input.value : null;
}

/**
 * Show a Bootstrap modal, using native Bootstrap Modal when available,
 * falling back to manual DOM manipulation otherwise.
 * Matches the ModalManager pattern in librenms_import.js.
 * @param {HTMLElement} el - The modal element to show
 */
function showModal(el) {
    if (!el) return;

    // Register click-outside (backdrop) and dismiss-button handlers once per element.
    // These are needed regardless of whether Bootstrap is available — Tabler/NetBox
    // may not always wire up native Bootstrap backdrop-click behaviour for modals
    // opened programmatically.  Matches the safety-net pattern in librenms_import.js.
    if (!el._syncDismissHandlersBound) {
        // Click on the modal overlay (outside .modal-dialog) → close
        el.addEventListener('click', function (e) {
            if (e.target === el) {
                hideModal(el);
            }
        });
        // data-bs-dismiss="modal" buttons → close
        el.addEventListener('click', function (e) {
            if (e.target.closest('[data-bs-dismiss="modal"]')) {
                hideModal(el);
            }
        });
        el._syncDismissHandlersBound = true;
    }

    // Try Bootstrap 5 native (preferred — handles dismiss, backdrop, keyboard)
    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
        const instance = bootstrap.Modal.getInstance(el) || new bootstrap.Modal(el);
        instance.show();
        return;
    }

    // Fallback: manual DOM manipulation
    el.classList.add('show');
    el.style.display = 'block';
    el.setAttribute('aria-modal', 'true');
    el.removeAttribute('aria-hidden');
    let backdrop = document.querySelector('.modal-backdrop');
    if (!backdrop) {
        backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop fade show';
        document.body.appendChild(backdrop);
    }
    document.body.classList.add('modal-open');

    // Backdrop element click → close (only needed in manual fallback).
    // Bind once per backdrop so repeated showModal() calls do not stack handlers.
    if (!backdrop._syncBackdropClickBound) {
        backdrop.addEventListener('click', function () {
            const activeModal = document.querySelector('.modal.show');
            if (activeModal) {
                hideModal(activeModal);
            }
        });
        backdrop._syncBackdropClickBound = true;
    }
}

/**
 * Hide a Bootstrap modal, using native Bootstrap Modal when available,
 * falling back to manual DOM cleanup otherwise.
 * @param {HTMLElement} el - The modal element to hide
 */
function hideModal(el) {
    if (!el) return;

    // Try Bootstrap 5 native (preferred)
    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
        const instance = bootstrap.Modal.getInstance(el);
        if (instance) {
            instance.hide();
            return;
        }
    }

    // Fallback: manual DOM cleanup
    el.classList.remove('show');
    el.style.display = 'none';
    el.setAttribute('aria-hidden', 'true');
    el.removeAttribute('aria-modal');
    document.querySelectorAll('.modal-backdrop').forEach((backdrop) => backdrop.remove());
    document.body.classList.remove('modal-open');
    document.body.style.removeProperty('padding-right');
    document.body.style.removeProperty('overflow');
}

// Helper to read CSRF token from cookies
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

/**
 * Extract a human-readable error message from a non-2xx fetch Response.
 * Attempts JSON parse first, checking error/message/detail fields.
 * Falls back to raw response text. Truncates to 300 characters.
 * @param {Response} response
 * @returns {Promise<string>}
 */
function fetchErrorMessage(response) {
    return response.text().then(t => {
        const ct = (response.headers.get('Content-Type') || '').toLowerCase();
        let msg = t || `HTTP ${response.status}`;
        if (ct.includes('application/json')) {
            try { const d = JSON.parse(t); msg = d.error || d.message || d.detail || msg; } catch (_) {}
        }
        if (msg.length > 300) msg = msg.slice(0, 300) + '...';
        return msg;
    });
}

/**
 * Show an error in NetBox's standard Django-message toast area.
 *
 * @param {string} message
 */
function showErrorToast(message) {
    const container = document.getElementById('django-messages');
    const text = String(message || 'Server error').trim();

    if (!container) {
        console.error('LibreNMS plugin error:', text);
        return;
    }

    const toast = document.createElement('div');
    toast.className = 'toast toast-dark border-0 shadow-sm';
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');
    toast.setAttribute('data-bs-delay', '12000');

    const header = document.createElement('div');
    header.className = 'toast-header text-bg-danger';
    const icon = document.createElement('i');
    icon.className = 'mdi mdi-alert-circle me-1';
    header.appendChild(icon);
    header.appendChild(document.createTextNode(' Error'));
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'btn-close me-0 m-auto';
    closeButton.setAttribute('data-bs-dismiss', 'toast');
    closeButton.setAttribute('aria-label', 'Close');
    header.appendChild(closeButton);

    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = text;

    toast.appendChild(header);
    toast.appendChild(body);
    container.appendChild(toast);
    // NetBox bundles Bootstrap as an ES module, so `window.bootstrap` is not
    // available to plugin scripts. Display the standard toast markup directly.
    toast.classList.add('show');
    closeButton.addEventListener('click', () => toast.remove());
    window.setTimeout(() => toast.remove(), 12000);
}

/**
 * Extract device/VM ID and type from current URL pathname.
 * Supports multiple URL patterns:
 * - /dcim/devices/{id}/
 * - /virtualization/virtual-machines/{id}/
 * - /plugins/librenms_plugin/device/{id}/
 * - /plugins/librenms_plugin/vm/{id}/
 * - /plugins/librenms_plugin/virtualmachine/{id}/
 *
 * @returns {Object|null} Object with {id: string, type: 'device'|'virtualmachine'} or null if not found
 */
function getDeviceIdFromUrl() {
    const pathname = window.location.pathname;
    const pathParts = pathname.split('/');

    // Try device patterns
    const deviceIdMatch = pathname.match(/\/devices\/(\d+)\//);
    if (deviceIdMatch) {
        return { id: deviceIdMatch[1], type: 'device' };
    }

    // Try virtual machine patterns
    const vmIdMatch = pathname.match(/\/virtual-machines\/(\d+)\//);
    if (vmIdMatch) {
        return { id: vmIdMatch[1], type: 'virtualmachine' };
    }

    // Try plugin device pattern
    const pluginDeviceMatch = pathname.match(/\/plugins\/librenms_plugin\/device\/(\d+)\//);
    if (pluginDeviceMatch) {
        return { id: pluginDeviceMatch[1], type: 'device' };
    }

    // Try plugin VM patterns
    const pluginVMMatch = pathname.match(/\/plugins\/librenms_plugin\/vm\/(\d+)\//);
    if (pluginVMMatch) {
        return { id: pluginVMMatch[1], type: 'virtualmachine' };
    }

    // Try plugin virtualmachine pattern (alternate)
    const pluginVirtualMachineMatch = pathname.match(/\/plugins\/librenms_plugin\/virtualmachine\/(\d+)\//);
    if (pluginVirtualMachineMatch) {
        return { id: pluginVirtualMachineMatch[1], type: 'virtualmachine' };
    }

    // Also check path parts for edge cases
    const deviceIndex = pathParts.indexOf('devices');
    const vmIndex = pathParts.indexOf('virtual-machines');
    const pluginDeviceIndex = pathParts.indexOf('device');
    const pluginVMIndex = pathParts.indexOf('virtualmachine');

    if (deviceIndex !== -1 && deviceIndex + 1 < pathParts.length) {
        const id = pathParts[deviceIndex + 1];
        if (/^\d+$/.test(id)) return { id, type: 'device' };
    } else if (vmIndex !== -1 && vmIndex + 1 < pathParts.length) {
        const id = pathParts[vmIndex + 1];
        if (/^\d+$/.test(id)) return { id, type: 'virtualmachine' };
    } else if (pluginDeviceIndex !== -1 && pluginDeviceIndex + 1 < pathParts.length) {
        const id = pathParts[pluginDeviceIndex + 1];
        if (/^\d+$/.test(id)) return { id, type: 'device' };
    } else if (pluginVMIndex !== -1 && pluginVMIndex + 1 < pathParts.length) {
        const id = pathParts[pluginVMIndex + 1];
        if (/^\d+$/.test(id)) return { id, type: 'virtualmachine' };
    }

    return null;
}

// ============================================
// CACHE COUNTDOWN TIMERS
// ============================================

/**
 * Initialize a countdown timer for cache expiry display.
 * Updates every second to show remaining time in MM:SS format.
 *
 * @param {string} elementId - DOM element ID containing data-expiry attribute
 * @returns {number|undefined} Interval ID for cleanup, or undefined if element not found
 */
function initializeCountdown(elementId) {
    const countdownElement = document.getElementById(elementId);
    if (!countdownElement) return;

    let countdownInterval;

    function updateCountdown() {
        const expiry = new Date(countdownElement.dataset.expiry).getTime();
        const now = new Date().getTime();
        const distance = expiry - now;

        if (distance < 0) {
            clearInterval(countdownInterval);
            countdownElement.innerHTML = "EXPIRED";
            expireSyncTabFromElement(countdownElement);
            return;
        }

        const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((distance % (1000 * 60)) / 1000);
        countdownElement.innerHTML = minutes + "m " + seconds + "s ";
    }

    updateCountdown();
    countdownInterval = setInterval(updateCountdown, COUNTDOWN_UPDATE_INTERVAL_MS);
    return countdownInterval;
}

// ============================================
// CROSS-TAB CACHE CONSISTENCY
// ============================================

function syncCacheController() {
    const root = document.getElementById('librenms-sync-cache-state');
    if (!root) return null;
    if (root._controller) return root._controller;

    let initial = {};
    const initialElement = document.getElementById('librenms-sync-cache-initial');
    if (initialElement) {
        try {
            initial = JSON.parse(initialElement.textContent) || {};
        } catch (_) {
            initial = {};
        }
    }
    let serializedContract = {};
    const contractElement = document.getElementById('librenms-sync-cache-contract');
    if (contractElement) {
        try {
            serializedContract = JSON.parse(contractElement.textContent) || {};
        } catch (_) {
            serializedContract = {};
        }
    }
    const contract = serializedContract.tabs || {};
    const validStates = new Set(
        Array.isArray(serializedContract.states) ? serializedContract.states : []
    );
    root._controller = {
        root,
        status: initial,
        contract,
        validStates,
        invalidatedLocally: new Set(),
        failClosedTabs: new Set(),
        ownRevisions: new Set(),
        pendingRestores: new Set(),
        requiredSourceFragments: new Set(),
        notifiedRevisions: new Set(),
        checking: null,
        recheckPending: false,
        statusChecksDisabled: false,
        statusGeneration: 0,
        lastCheckFailed: false,
        lostFocus: false,
    };
    Object.entries(initial).forEach(([tab, state]) => {
        const content = syncCacheContent(tab);
        if (content && !state.snapshot_available && !isColdSyncCacheState(state)) {
            root._controller.invalidatedLocally.add(tab);
            clearSyncTabContent(tab, syncCacheUnavailableReason(tab, state));
        }
        updateSyncCacheTabState(tab, state);
    });
    return root._controller;
}

function syncCacheTabSpec(tab) {
    return syncCacheController()?.contract?.[tab] || null;
}

function syncCacheContent(tab) {
    const contentId = syncCacheTabSpec(tab)?.content_id;
    return contentId ? document.getElementById(contentId) : null;
}

function syncCacheLabel(tab) {
    return syncCacheTabSpec(tab)?.label || 'Sync';
}

function isColdSyncCacheState(state) {
    return Boolean(
        state &&
        state.state === 'missing' &&
        !state.snapshot_available &&
        !state.revision &&
        !state.timestamp &&
        !state.reason &&
        !state.refresh_error
    );
}

function updateSyncCacheTabState(tab, state) {
    const tabLink = document.getElementById(`${tab}-tab`);
    if (!tabLink) return;
    const cold = isColdSyncCacheState(state);
    const cacheUnavailable = !cold && (
        !state ||
        !state.snapshot_available ||
        ['invalidated', 'refresh_failed', 'missing', 'expired'].includes(state.state)
    );
    const ready = !cold && !cacheUnavailable && Boolean(state?.snapshot_available);
    const unavailable = (
        cacheUnavailable &&
        !tabLink.classList.contains('active') &&
        state?.attention_required !== false
    );
    const label = tabLink.dataset.tabLabel || syncCacheLabel(tab);
    tabLink.classList.toggle('sync-cache-ready', ready);
    tabLink.classList.toggle('sync-cache-unavailable', unavailable);
    if (unavailable) {
        tabLink.setAttribute('aria-label', `${label}. Cached data is unavailable.`);
        tabLink.setAttribute('title', 'Cached data is unavailable. Refresh this tab.');
    } else if (ready) {
        tabLink.setAttribute('aria-label', `${label}. Cached data is available.`);
        tabLink.setAttribute('title', 'Cached data is available.');
    } else {
        tabLink.setAttribute('aria-label', label);
        tabLink.removeAttribute('title');
    }
}

function showSyncCacheNotice(message, revision, level = 'warning') {
    const controller = syncCacheController();
    if (!controller || (revision && controller.notifiedRevisions.has(revision))) return;

    let container = document.getElementById('librenms-sync-cache-notices');
    if (!container) {
        container = document.createElement('div');
        container.id = 'librenms-sync-cache-notices';
        controller.root.insertAdjacentElement('afterend', container);
    }
    if (level !== 'danger' && container.querySelector('.alert-danger')) return;
    if (revision) controller.notifiedRevisions.add(revision);
    const alertElement = document.createElement('div');
    alertElement.className = `alert alert-${level} alert-dismissible`;
    alertElement.setAttribute('role', 'alert');
    alertElement.appendChild(document.createTextNode(message));
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'btn-close';
    dismiss.setAttribute('aria-label', 'Close');
    dismiss.addEventListener('click', () => alertElement.remove());
    alertElement.appendChild(dismiss);
    container.replaceChildren(alertElement);
}

function syncCacheReason(tab, state) {
    const source = syncCacheTabSpec(state.source_tab)?.label || 'another sync tab';
    const target = syncCacheLabel(tab);
    const relative = formatSyncCacheRelativeTime(state.timestamp);
    const suffix = relative ? ` ${relative}.` : '';
    if (state.same_user) {
        return `${target} data was cleared after you synchronized ${source} data from LibreNMS.${suffix}`;
    }
    return `${target} data was cleared because another user synchronized ${source} data from LibreNMS.${suffix}`;
}

function syncCacheUnavailableReason(tab, state) {
    if (state.state === 'refresh_failed') {
        const reason = state.reason || `${syncCacheLabel(tab)} refresh failed.`;
        return `${reason} Refresh this tab to try again.`;
    }
    if (state.state === 'missing' && !state.reason) {
        return `${syncCacheLabel(tab)} cache is unavailable. Refresh this tab to load current data.`;
    }
    return syncCacheReason(tab, state);
}

function syncCacheSummary(state) {
    if (state.same_user) {
        return 'Some sync data was cleared because you synchronized data from LibreNMS.';
    }
    return 'Some sync data was cleared because another user synchronized data from LibreNMS.';
}

function formatSyncCacheRelativeTime(timestamp) {
    const occurredAt = Date.parse(timestamp || '');
    if (!Number.isFinite(occurredAt)) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - occurredAt) / 1000));
    if (seconds < 60) return 'Less than a minute ago';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.floor(hours / 24);
    return `${days} day${days === 1 ? '' : 's'} ago`;
}

function clearSyncTabContent(tab, message) {
    const content = syncCacheContent(tab);
    if (!content) return;
    const card = document.createElement('div');
    card.className = 'card';
    const body = document.createElement('div');
    body.className = 'card-body text-center text-muted py-4';
    const icon = document.createElement('i');
    icon.className = 'mdi mdi-sync-off mdi-48px';
    const text = document.createElement('p');
    text.className = 'mt-2 mb-0';
    text.textContent = message;
    body.append(icon, text);
    card.appendChild(body);
    content.replaceChildren(card);
    content.dataset.cacheEmpty = 'true';
}

function failClosedSyncControls(message) {
    const controller = syncCacheController();
    if (!controller) return;
    Object.keys(controller.contract).forEach(tab => {
        controller.invalidatedLocally.add(tab);
        controller.failClosedTabs.add(tab);
        clearSyncTabContent(tab, message);
        updateSyncCacheTabState(tab, { state: 'invalidated', snapshot_available: false });
    });
    const sourceTab = activeSyncTab();
    document.querySelectorAll(
        '#librenms-sync-tabs button, #librenms-sync-tabs input, #librenms-sync-tabs select, #librenms-sync-tabs textarea, ' +
        '#htmx-modal-content button, #htmx-modal-content input, #htmx-modal-content select, #htmx-modal-content textarea'
    )
        .forEach(control => {
            if (control.disabled) return;
            control.disabled = true;
            control.dataset.cacheFailClosed = sourceTab;
        });
}

function restoreFailClosedSyncControls(status) {
    if (!status) return;
    document.querySelectorAll(
        '#librenms-sync-tabs [data-cache-fail-closed], #htmx-modal-content [data-cache-fail-closed]'
    )
        .forEach(control => {
            // A verified status restores pane chrome so users can create or reload a snapshot.
            // A modal acts on tab content, so it waits for that content to exist again.
            if (control.closest('#htmx-modal-content')) {
                const sourceTab = control.dataset.cacheFailClosed;
                const sourceState = status[sourceTab];
                const sourceContent = syncCacheContent(sourceTab);
                if (
                    !sourceState?.snapshot_available ||
                    !sourceContent ||
                    sourceContent.dataset.cacheEmpty === 'true'
                ) return;
            }
            delete control.dataset.cacheFailClosed;
            control.disabled = false;
        });
}

function expireSyncTabFromElement(element) {
    const pane = element.closest('.tab-pane[data-tab-id]');
    if (!pane) return;
    const tab = pane.dataset.tabId;
    const controller = syncCacheController();
    if (!controller) return;
    controller.invalidatedLocally.add(tab);
    clearSyncTabContent(tab, `${syncCacheLabel(tab)} cache expired. Refresh this tab to load current data.`);
    updateSyncCacheTabState(tab, { state: 'expired', snapshot_available: false });
}

function activeSyncTab() {
    const region = document.getElementById('librenms-sync-tabs');
    return region?.dataset.activeTab || new URLSearchParams(window.location.search).get('tab') || 'interfaces';
}

function renderedSyncCacheStatus() {
    const element = document.getElementById('librenms-sync-rendered-status');
    if (!element) return null;
    try {
        const status = JSON.parse(element.textContent) || null;
        return status && typeof status === 'object' && !Array.isArray(status) ? status : null;
    } catch (_) {
        return null;
    }
}

const SYNC_CACHE_GENERATION_HEADER = 'X-Sync-Cache-Generation';
const SYNC_CACHE_FRAGMENT_FAILED = 'Cache state could not be restored. Reload this tab before continuing.';

/**
 * Read the status generation a fragment load sent with its request.
 *
 * @param {CustomEvent} event - An htmx request lifecycle event.
 * @returns {?number} The generation the loader sent, or null for any other request.
 */
function syncCacheFragmentGeneration(event) {
    const header = event.detail?.requestConfig?.headers?.[SYNC_CACHE_GENERATION_HEADER];
    return header === undefined ? null : Number(header);
}

/**
 * Find the tab whose cached content an element holds.
 *
 * @param {?Element} element - Candidate cache content element.
 * @returns {string|undefined} The tab id, or undefined when the element holds no cached content.
 */
function syncCacheTabForContent(element) {
    const controller = syncCacheController();
    if (!element || !controller) return undefined;
    return Object.entries(controller.contract || {}).find(([, spec]) => spec.content_id === element.id)?.[0];
}

// NetBox imports htmx as a module, so there is no htmx global and HTML written with innerHTML keeps
// no hx- bindings. The pane's hidden [data-fragment-loader] lets htmx request and swap the fragment,
// which binds the restored forms like every other swap.
/**
 * Reload a tab's cached content through the pane's htmx loader, so the fragment gets a full swap lifecycle.
 *
 * @param {string} tab - Tab id to reload.
 * @param {?number} statusGeneration - Status generation this load belongs to.
 * @param {?AbortSignal} signal - Signal that cancels the load.
 * @returns {Promise<void>} Rejects only when the signal aborts the load.
 */
function loadSyncCacheFragment(tab, statusGeneration = null, signal = null) {
    const pane = document.getElementById(tab);
    const content = syncCacheContent(tab);
    const controller = syncCacheController();
    const loader = pane?.querySelector('[data-fragment-loader]');
    if (!pane || !content || !controller || !loader) return Promise.resolve();
    if (signal?.aborted) return Promise.reject(new Error('Cache fragment request aborted'));
    const requestGeneration = statusGeneration ?? controller.statusGeneration;
    loader.setAttribute('hx-vals', JSON.stringify({ server_key: controller.root.dataset.serverKey }));
    // The fragment carries return_url links built from HX-Current-URL (tables/modules.py), which htmx sends itself.
    // The generation header tells the body's htmx:afterSwap handler this swap answers a status check.
    const fragmentHeaders = {
        'X-Requested-With': 'XMLHttpRequest',
        [SYNC_CACHE_GENERATION_HEADER]: String(requestGeneration),
    };
    const fragmentCsrf = getCsrfToken();
    if (fragmentCsrf) fragmentHeaders['X-CSRFToken'] = fragmentCsrf;
    loader.setAttribute('hx-headers', JSON.stringify(fragmentHeaders));
    return new Promise((resolve, reject) => {
        let requestXhr = null;
        let confirmFired = false;
        const captureConfirm = () => { confirmFired = true; };
        const captureRequest = event => { requestXhr = event.detail.xhr; };
        const holdStaleResponse = event => {
            if (event.detail.xhr === requestXhr && controller.statusGeneration !== requestGeneration) {
                event.detail.shouldSwap = false;
            }
        };
        const abortRequest = () => requestXhr?.abort();
        const release = () => {
            loader.removeEventListener('htmx:confirm', captureConfirm);
            loader.removeEventListener('htmx:beforeRequest', captureRequest);
            loader.removeEventListener('htmx:afterRequest', settleRequest);
            loader.removeEventListener('htmx:sendAbort', settleAbort);
            loader.removeEventListener('htmx:sendError', settleFailure);
            loader.removeEventListener('htmx:timeout', settleFailure);
            content.removeEventListener('htmx:beforeSwap', holdStaleResponse);
            signal?.removeEventListener('abort', abortRequest);
        };
        const failClosed = message => {
            console.error(message);
            // Leaving the tab owed a restore lets the next status check re-issue the same failing
            // request forever, so retire every claim on a retry. failClosedTabs is a separate
            // disjunct in the recovery condition, so clearing invalidatedLocally alone still loops.
            controller.pendingRestores.delete(tab);
            controller.failClosedTabs.delete(tab);
            controller.invalidatedLocally.add(tab);
            clearSyncTabContent(tab, SYNC_CACHE_FRAGMENT_FAILED);
        };
        function settleRequest(event) {
            if (event.detail.xhr !== requestXhr) return;
            if (event.detail.xhr.status === 0) return;
            release();
            if (signal?.aborted) {
                reject(new Error('Cache fragment request aborted'));
                return;
            }
            if (controller.statusGeneration === requestGeneration) {
                if (event.detail.successful) {
                    delete content.dataset.cacheEmpty;
                    controller.invalidatedLocally.delete(tab);
                    controller.failClosedTabs.delete(tab);
                    controller.pendingRestores.delete(tab);
                } else {
                    failClosed(`HTTP ${event.detail.xhr.status}`);
                }
            }
            resolve();
        }
        function settleAbort(event) {
            if (event.detail.xhr !== requestXhr) return;
            release();
            if (signal?.aborted) {
                reject(new Error('Cache fragment request aborted'));
                return;
            }
            controller.pendingRestores.add(tab);
            resolve();
        }
        function settleFailure(event) {
            if (event.detail.xhr !== requestXhr) return;
            release();
            if (controller.statusGeneration === requestGeneration) failClosed('Cache fragment failed');
            resolve();
        }
        loader.addEventListener('htmx:confirm', captureConfirm);
        loader.addEventListener('htmx:beforeRequest', captureRequest);
        loader.addEventListener('htmx:afterRequest', settleRequest);
        loader.addEventListener('htmx:sendAbort', settleAbort);
        loader.addEventListener('htmx:sendError', settleFailure);
        loader.addEventListener('htmx:timeout', settleFailure);
        content.addEventListener('htmx:beforeSwap', holdStaleResponse);
        signal?.addEventListener('abort', abortRequest);
        loader.dispatchEvent(new CustomEvent('librenms:load-fragment'));
        if (requestXhr) return;
        release();
        // htmx fires htmx:confirm after the target check and before hx-sync, so a confirm with no request is a drop.
        if (confirmFired) {
            controller.pendingRestores.add(tab);
            resolve();
            return;
        }
        // htmx never processed the loader, so the tab cannot be restored.
        if (controller.statusGeneration === requestGeneration) failClosed('Cache fragment loader is not bound');
        resolve();
    });
}

function reconcileSyncCacheStatus(nextStatus, statusGeneration = null, signal = null) {
    const controller = syncCacheController();
    if (!controller) return Promise.resolve();
    const requestGeneration = statusGeneration ?? controller.statusGeneration;
    if (controller.statusGeneration !== requestGeneration) return Promise.resolve();
    const previous = controller.status || {};
    const activeTab = activeSyncTab();
    let notice = null;
    const fragmentLoads = [];

    Object.entries(nextStatus || {}).forEach(([tab, state]) => {
        const prior = previous[tab] || {};
        const restoreOwed = controller.pendingRestores.has(tab);
        const revisionChanged = restoreOwed || Boolean(state.revision && state.revision !== prior.revision);
        const refreshChanged = Boolean(
            state.refresh_error_timestamp &&
            state.refresh_error_timestamp !== prior.refresh_error_timestamp
        );
        const unavailable = !state.snapshot_available || ['invalidated', 'refresh_failed', 'missing'].includes(state.state);
        const locallyUnavailable = (
            controller.invalidatedLocally.has(tab) &&
            syncCacheContent(tab)?.dataset.cacheEmpty === 'true'
        );
        updateSyncCacheTabState(
            tab,
            locallyUnavailable && state.snapshot_available && !revisionChanged
                ? { state: 'invalidated', snapshot_available: false }
                : state
        );

        if (unavailable && (revisionChanged || prior.snapshot_available || refreshChanged)) {
            controller.invalidatedLocally.add(tab);
            let reason = syncCacheUnavailableReason(tab, state);
            if (state.refresh_error && state.state !== 'refresh_failed') {
                reason = `${reason} ${state.refresh_error} Refresh this tab to try again.`;
            }
            clearSyncTabContent(tab, reason);
            const marker = refreshChanged ? `refresh:${state.refresh_error_timestamp}` : state.revision;
            if (!notice && marker) {
                const message = state.state === 'invalidated' && !state.refresh_error
                    ? syncCacheSummary(state)
                    : reason;
                notice = { message, revision: marker };
            }
        } else if (
            state.state === 'locally_changed' &&
            state.snapshot_available &&
            revisionChanged &&
            (
                restoreOwed ||
                !controller.ownRevisions.has(state.revision) ||
                controller.requiredSourceFragments.has(tab)
            )
        ) {
            if (tab === activeTab) {
                fragmentLoads.push(loadSyncCacheFragment(tab, requestGeneration, signal));
                controller.requiredSourceFragments.delete(tab);
            }
        } else if (
            state.state === 'ready' &&
            state.snapshot_available &&
            revisionChanged
        ) {
            if (tab === activeTab) {
                controller.invalidatedLocally.delete(tab);
                fragmentLoads.push(loadSyncCacheFragment(tab, requestGeneration, signal));
            } else {
                controller.invalidatedLocally.add(tab);
                updateSyncCacheTabState(tab, { state: 'invalidated', snapshot_available: false });
            }
        } else if (
            state.snapshot_available &&
            tab === activeTab &&
            syncCacheContent(tab)?.dataset.cacheEmpty === 'true' &&
            (
                controller.failClosedTabs.has(tab) ||
                !controller.invalidatedLocally.has(tab)
            )
        ) {
            fragmentLoads.push(loadSyncCacheFragment(tab, requestGeneration, signal));
        }
    });
    if (controller.statusGeneration !== requestGeneration) return Promise.resolve();
    controller.status = nextStatus || {};
    if (notice) showSyncCacheNotice(notice.message, notice.revision);
    return Promise.all(fragmentLoads);
}

function isValidSyncCacheStatusPayload(payload, expectedTabs, validStates) {
    const tabs = payload?.tabs;
    if (!tabs || typeof tabs !== 'object' || Array.isArray(tabs)) return false;
    const tabNames = Object.keys(tabs);
    if (tabNames.length !== expectedTabs.length || !expectedTabs.every(tab => tabNames.includes(tab))) return false;
    return tabNames.every(tab => {
        const state = tabs[tab];
        return Boolean(
            state &&
            typeof state === 'object' &&
            !Array.isArray(state) &&
            typeof state.snapshot_available === 'boolean' &&
            validStates.has(state.state)
        );
    });
}

function isValidSyncCacheContract(contract, validStates) {
    if (!contract || typeof contract !== 'object' || Array.isArray(contract)) return false;
    if (
        !(validStates instanceof Set) ||
        !validStates.size ||
        !Array.from(validStates).every(state => typeof state === 'string' && state.trim())
    ) return false;
    const entries = Object.entries(contract);
    if (!entries.length) return false;
    const contentIds = new Set();
    return entries.every(([tab, spec]) => {
        if (
            !tab.trim() ||
            !spec ||
            typeof spec !== 'object' ||
            Array.isArray(spec) ||
            typeof spec.content_id !== 'string' ||
            !spec.content_id.trim() ||
            typeof spec.label !== 'string' ||
            !spec.label.trim() ||
            contentIds.has(spec.content_id)
        ) return false;
        const pane = document.getElementById(tab);
        const content = document.getElementById(spec.content_id);
        if (!pane || !content || !pane.contains(content)) return false;
        contentIds.add(spec.content_id);
        return true;
    });
}

function checkSyncCacheStatus() {
    const controller = syncCacheController();
    if (!controller) return Promise.resolve();
    if (controller.statusChecksDisabled) return Promise.resolve();
    const expectedTabs = Object.keys(controller.contract || {});
    if (!isValidSyncCacheContract(controller.contract, controller.validStates)) {
        controller.statusChecksDisabled = true;
        controller.lastCheckFailed = true;
        console.error('Invalid cache status contract');
        failClosedSyncControls('Cache status could not be verified. Reload this tab before continuing.');
        return Promise.resolve();
    }
    if (controller.checking) {
        controller.recheckPending = true;
        return controller.checking;
    }
    const requestGeneration = controller.statusGeneration;
    const url = new URL(controller.root.dataset.statusUrl, window.location.href);
    url.searchParams.set('server_key', controller.root.dataset.serverKey);
    const abortController = new AbortController();
    const timeoutId = setTimeout(() => abortController.abort(), SYNC_CACHE_STATUS_TIMEOUT_MS);
    let statusRequest;
    const statusHeaders = { 'Accept': 'application/json' };
    const statusCsrf = getCsrfToken();
    if (statusCsrf) statusHeaders['X-CSRFToken'] = statusCsrf;
    statusRequest = fetch(url, {
        credentials: 'same-origin',
        headers: statusHeaders,
        signal: abortController.signal,
    })
        .then(response => {
            if (controller.statusGeneration !== requestGeneration) return STALE_SYNC_CACHE_STATUS;
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(payload => {
            if (
                payload === STALE_SYNC_CACHE_STATUS ||
                controller.statusGeneration !== requestGeneration
            ) return;
            if (
                !isValidSyncCacheStatusPayload(payload, expectedTabs, controller.validStates)
            ) {
                throw new Error('Invalid cache status response');
            }
            return reconcileSyncCacheStatus(payload.tabs, requestGeneration, abortController.signal)
                .then(() => payload.tabs);
        })
        .then(status => {
            if (controller.statusGeneration !== requestGeneration) return;
            controller.lastCheckFailed = false;
            restoreFailClosedSyncControls(status);
        })
        .catch(error => {
            if (controller.statusGeneration !== requestGeneration) return;
            console.error(error.message);
            controller.lastCheckFailed = true;
            failClosedSyncControls('Cache status could not be verified. Reload this tab before continuing.');
        })
        .finally(() => {
            clearTimeout(timeoutId);
            if (controller.checking !== statusRequest) return;
            controller.checking = null;
            if (controller.recheckPending) {
                controller.recheckPending = false;
                checkSyncCacheStatus();
            }
        });
    controller.checking = statusRequest;
    return controller.checking;
}

function initializeSyncCacheConsistency() {
    const controller = syncCacheController();
    if (!controller || controller.root.dataset.listenersInitialized === 'true') return;
    controller.root.dataset.listenersInitialized = 'true';

    window.addEventListener('blur', () => { controller.lostFocus = true; });
    window.addEventListener('focus', () => {
        if (controller.lostFocus) {
            controller.lostFocus = false;
            checkSyncCacheStatus();
        }
    });
    document.addEventListener('librenmsCacheChanged', event => {
        const payload = event.detail?.value || event.detail || {};
        Object.values(payload.revisions || {}).forEach(revision => controller.ownRevisions.add(revision));
        if (payload.source_fragment_required) {
            const sourceTabs = payload.source_tabs || (payload.source_tab ? [payload.source_tab] : []);
            sourceTabs.forEach(tab => controller.requiredSourceFragments.add(tab));
        }
        if (payload.cleanup_failed) {
            (payload.cleanup_tabs || []).forEach(tab => {
                controller.invalidatedLocally.add(tab);
                clearSyncTabContent(
                    tab,
                    'Cache cleanup could not be verified. Refresh this tab before continuing.'
                );
                updateSyncCacheTabState(tab, { state: 'invalidated', snapshot_available: false });
            });
            showSyncCacheNotice(
                'Synchronization succeeded, but related cache cleanup failed. Reload this sync page before continuing.',
                payload.transition_id,
                'danger'
            );
        } else if (payload.removed) {
            showSyncCacheNotice(
                'Other sync tabs were cleared because you synchronized data from LibreNMS.',
                payload.transition_id,
                'info'
            );
        }
        checkSyncCacheStatus();
    });
}

/**
 * Initialize all cache countdown timers on the page.
 * Clears any existing intervals before starting new ones.
 */
function initializeCountdowns() {
    if (window.interfaceCountdownInterval) {
        clearInterval(window.interfaceCountdownInterval);
    }
    if (window.cableCountdownInterval) {
        clearInterval(window.cableCountdownInterval);
    }
    if (window.ipCountdownInterval) {
        clearInterval(window.ipCountdownInterval);
    }
    if (window.vlanCountdownInterval) {
        clearInterval(window.vlanCountdownInterval);
    }
    if (window.moduleCountdownInterval) {
        clearInterval(window.moduleCountdownInterval);
    }

    window.interfaceCountdownInterval = initializeCountdown("countdown-timer");
    window.cableCountdownInterval = initializeCountdown("cable-countdown-timer");
    window.ipCountdownInterval = initializeCountdown("ip-countdown-timer");
    window.vlanCountdownInterval = initializeCountdown("vlan-countdown-timer");
    window.moduleCountdownInterval = initializeCountdown("module-countdown-timer");
}

// ============================================
// TABLE CHECKBOX HANDLING
// ============================================

/**
 * Initialize checkbox selection for a table with shift-click support.
 * Enables "select all" toggle and shift-click range selection.
 *
 * @param {string} tableId - DOM element ID of the table
 */
function initializeTableCheckboxes(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;

    // Query the CURRENT checkboxes live inside every handler instead of closing over a snapshot.
    // The master initializer re-runs on each htmx:afterSwap, but the dataset guards below keep the
    // toggle/shift handlers from re-binding on a SURVIVING <thead> toggle; a NodeList captured once
    // would then go stale, so select-all / shift-range would iterate detached checkboxes and miss
    // the rows a later row-level swap injected.
    const liveCheckboxes = () => Array.from(table.querySelectorAll('td input[name="select"]:not(:disabled)'));
    const toggleAll = table.querySelector('th input.toggle');
    // Persist the shift-range anchor on the TABLE element, not in a per-call closure. This
    // initializer re-runs on every htmx:afterSwap: checkboxes bound in an earlier run keep their
    // handlers (the dataset guard skips re-binding), so a closure-scoped anchor would leave old
    // rows referencing a stale `lastChecked` while rows added by a later swap use a fresh one —
    // shift-clicking between the two then uses disconnected anchors and selects nothing. One
    // anchor on the shared table node keeps every row's handler in sync across swaps.
    const getAnchor = () => table._lnmsLastChecked || null;
    const setAnchor = (cb) => {
        table._lnmsLastChecked = cb;
    };

    // Guard against stacked handlers: register each listener at most once per element (a dataset
    // flag marks it done) since the master initializer re-runs on every htmx:afterSwap.
    if (toggleAll && toggleAll.dataset.tableToggleInitialized !== 'true') {
        toggleAll.dataset.tableToggleInitialized = 'true';
        toggleAll.addEventListener('change', function () {
            // Index the member rows once for the batch: a per-row selector query would be quadratic.
            const membersByLag = _membersByLag();
            liveCheckboxes().forEach(checkbox => {
                const changed = checkbox.checked !== toggleAll.checked;
                checkbox.checked = toggleAll.checked;
                applyRowSelection(checkbox, membersByLag, changed);
            });
            // Recompute once for the whole page rather than per row: select-all still has to pull
            // in an off-page parent or aggregate, but the closure is the same for every seed.
            refreshRequiredSelections();
            persistTableSelection(table);
            updateBulkActionButton();
        });
    }

    liveCheckboxes().forEach(checkbox => {
        if (checkbox.dataset.tableClickInitialized === 'true') return;
        checkbox.dataset.tableClickInitialized = 'true';
        checkbox.addEventListener('click', function (e) {
            const anchor = getAnchor();
            if (!anchor) {
                setAnchor(checkbox);
                return;
            }

            if (e.shiftKey) {
                const current = liveCheckboxes();
                const start = current.indexOf(checkbox);
                const end = current.indexOf(anchor);
                // Skip the range when the prior anchor was swapped out (indexOf -1) rather than
                // slicing a bogus range off the live list.
                if (start !== -1 && end !== -1) {
                    // Index the member rows once for the batch: a per-row selector query would be quadratic.
                    const membersByLag = _membersByLag();
                    current.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(cb => {
                        const changed = cb.checked !== anchor.checked;
                        cb.checked = anchor.checked;
                        applyRowSelection(cb, membersByLag, changed);
                    });
                    refreshRequiredSelections();
                    persistTableSelection(table);
                    updateBulkActionButton();
                }
            }

            setAnchor(checkbox);
        });
    });

    // Re-check whatever this table had selected on another page. Runs after the handlers are
    // bound so the requirement cascade sees the restored rows.
    restoreTableSelection(table);
}

/**
 * Initialize checkbox handling for all sync comparison tables.
 */
function initializeCheckboxes() {
    initializeTableCheckboxes('librenms-interface-table');
    initializeTableCheckboxes('librenms-interface-table-vm');
    initializeTableCheckboxes('librenms-cable-table');
    initializeTableCheckboxes('librenms-cable-table-vc');
    initializeTableCheckboxes('librenms-ipaddress-table');
    initializeTableCheckboxes('librenms-vlan-table');
    initializeTableCheckboxes('librenms-port-vlan-table');
    initializeTableCheckboxes('librenms-module-table');
}

/**
 * Selection dependency rules for the interface tables.
 *
 * A row cannot sync unless the rows it depends on are synced too: a sub-interface needs its
 * parent, and a LAG member needs its aggregate. Both are read off the row as LibreNMS port ids
 * (data-parent-port-id, data-member-of-lag) and both chain, so et-0/0/6.0 pulls in et-0/0/6,
 * which in turn pulls in ae2.
 *
 * Requirements are recomputed from the rows the user checked rather than propagated hop by hop.
 * A recompute is idempotent and needs no unwind bookkeeping, so an aggregate that is only
 * required transitively is dropped as soon as the last row needing it is cleared.
 *
 * Checking an aggregate additionally pulls its MEMBERS in. That is a convenience, not a
 * requirement (an aggregate syncs perfectly well on its own), so it is applied once when the
 * aggregate is toggled and the user stays free to clear an individual member afterwards.
 *
 * Everything here is gated on the #autoSelectLagMembers toggle.
 */

/** Marks a row this code checked to satisfy another row's requirement. */
const REQUIRED_MARKER = 'autoRequired';
/** Marks a member row an aggregate pulled in as a convenience. */
const MEMBER_MARKER = 'autoMember';
/** Marks a row the user cleared by hand, so the cascade stops offering to re-add it. */
const CLEARED_MARKER = 'selectionCleared';

/**
 * Return the table rows that carry a LibreNMS port id, indexed by that id.
 *
 * @returns {Map<string, HTMLTableRowElement>} Row per port id present on this page.
 */
function _rowsByPortId() {
    const rows = new Map();
    document.querySelectorAll('tr[data-port-id]').forEach(function (row) {
        const portId = row.dataset.portId;
        if (portId) rows.set(portId, row);
    });
    return rows;
}

/**
 * Return the port ids a row depends on: its parent interface and its aggregate.
 *
 * @param {HTMLTableRowElement} row - The row to read.
 * @returns {string[]} Port ids that must be selected alongside this row.
 */
function _requiredPortIds(row) {
    return [row.dataset.parentPortId, row.dataset.memberOfLag].filter(Boolean);
}

/**
 * Return a row's selection checkbox when it can be toggled.
 *
 * @param {HTMLTableRowElement|null} row - The row to read.
 * @returns {HTMLInputElement|null} The enabled checkbox, or null.
 */
function _selectableCheckbox(row) {
    if (!row) return null;
    const checkbox = row.querySelector('input[name="select"]');
    return checkbox && !checkbox.disabled ? checkbox : null;
}

/**
 * Return the member rows on this page, grouped by the port id of their aggregate.
 *
 * @returns {Map<string, HTMLTableRowElement[]>} Member rows per aggregate port id.
 */
function _membersByLag() {
    const members = new Map();
    document.querySelectorAll('tr[data-member-of-lag]').forEach(function (row) {
        const lagPortId = row.dataset.memberOfLag;
        if (!members.has(lagPortId)) members.set(lagPortId, []);
        members.get(lagPortId).push(row);
    });
    return members;
}

/**
 * Recompute which rows are held selected to satisfy another row's requirements.
 *
 * Walks the requirement chain from every row the user selected in their own right, checks each
 * required row still on this page, and releases any row this code had checked that nothing
 * requires any more. Rows required from another page raise an inline notice instead.
 *
 * @returns {boolean} True when the selection changed.
 */
function refreshRequiredSelections() {
    const toggle = document.getElementById('autoSelectLagMembers');
    const enabled = Boolean(toggle && toggle.checked);
    const rows = _rowsByPortId();
    const required = new Set();
    const offPage = new Map();

    if (enabled) {
        // Seeds are the rows selected on their own account: a user click, a select-all, or a
        // member an aggregate pulled in. A row held only by this function is never a seed, or
        // releasing the last real selection could not release the chain above it.
        const pending = [];
        rows.forEach(function (row) {
            const checkbox = _selectableCheckbox(row);
            if (checkbox && checkbox.checked && !checkbox.dataset[REQUIRED_MARKER]) {
                pending.push(row);
            }
        });

        while (pending.length) {
            const row = pending.pop();
            _requiredPortIds(row).forEach(function (portId) {
                if (required.has(portId)) return;
                required.add(portId);
                const requiredRow = rows.get(portId);
                if (requiredRow) {
                    pending.push(requiredRow);
                } else if (!offPage.has(portId)) {
                    // Name the relationship so the notice can say which row is missing and why.
                    const isParent = row.dataset.parentPortId === portId;
                    offPage.set(portId, {
                        kind: isParent ? 'Parent' : 'LAG',
                        name: (isParent ? row.dataset.parentName : row.dataset.lagName) || portId,
                    });
                }
            });
        }
    }

    let changed = false;
    rows.forEach(function (row, portId) {
        const checkbox = _selectableCheckbox(row);
        if (!checkbox) return;
        if (required.has(portId)) {
            // A row the user cleared by hand stays cleared. A missing parent only leaves the
            // relationship unset rather than failing the sync, so re-checking the box under the
            // user's cursor would be fighting them over a hint.
            if (!checkbox.checked && !checkbox.dataset[CLEARED_MARKER]) {
                checkbox.checked = true;
                checkbox.dataset[REQUIRED_MARKER] = 'true';
                changed = true;
            }
        } else {
            if (checkbox.dataset[REQUIRED_MARKER]) {
                delete checkbox.dataset[REQUIRED_MARKER];
                if (checkbox.checked) {
                    checkbox.checked = false;
                    changed = true;
                }
            }
            // Nothing needs this row any more, so a later requirement starts from a clean slate.
            delete checkbox.dataset[CLEARED_MARKER];
        }
    });

    offPage.forEach(function (related) {
        _showRequiredRowCrossPageNotice(related.name, related.kind);
    });

    return changed;
}

/**
 * Check or release the member rows of an aggregate the user just toggled.
 *
 * @param {HTMLTableRowElement} row - The aggregate row that changed.
 * @param {boolean} checked - Whether the aggregate is now selected.
 * @param {Map<string, HTMLTableRowElement[]>} membersByLag - Member rows per aggregate port id, from _membersByLag().
 * @returns {boolean} True when the selection changed.
 */
function _propagateToLagMembers(row, checked, membersByLag) {
    const portId = row.dataset.portId;
    if (!portId) return false;
    let changed = false;
    (membersByLag.get(portId) || []).forEach(function (memberRow) {
        const checkbox = _selectableCheckbox(memberRow);
        if (!checkbox) return;
        if (checked) {
            if (!checkbox.checked) {
                checkbox.checked = true;
                checkbox.dataset[MEMBER_MARKER] = 'true';
                changed = true;
            }
        } else if (checkbox.dataset[MEMBER_MARKER]) {
            delete checkbox.dataset[MEMBER_MARKER];
            if (checkbox.checked) {
                checkbox.checked = false;
                changed = true;
            }
        }
    });
    return changed;
}

/**
 * Apply the selection rules for a row the user set, and cascade to the aggregate's members.
 *
 * The change handler calls this for a click; the shift-range and select-all paths call it for
 * each row they assign, because a programmatic `checked` assignment raises no change event.
 * A range that crosses an already selected aggregate must not re-check a member the user cleared.
 *
 * @param {HTMLInputElement} checkbox - The row checkbox, already in its new state.
 * @param {Map<string, HTMLTableRowElement[]>} membersByLag - Member rows per aggregate port id, from _membersByLag().
 * @param {boolean} changed - Whether the checkbox state changed; only a transition cascades.
 * @returns {boolean} True when a member row changed.
 */
function applyRowSelection(checkbox, membersByLag, changed) {
    const row = checkbox.closest('tr');
    if (!row) return false;
    // The user acted on this row, so it is theirs now: drop the markers that said this code
    // had put it there, or a later recompute would release a row they chose to keep.
    delete checkbox.dataset[REQUIRED_MARKER];
    if (checkbox.checked) {
        delete checkbox.dataset[CLEARED_MARKER];
        delete checkbox.dataset[MEMBER_MARKER];
    } else {
        delete checkbox.dataset[MEMBER_MARKER];
        checkbox.dataset[CLEARED_MARKER] = 'true';
    }
    if (!changed) return false;
    const toggle = document.getElementById('autoSelectLagMembers');
    if (!toggle || !toggle.checked) return false;
    return _propagateToLagMembers(row, checkbox.checked, membersByLag);
}

document.addEventListener('change', function (e) {
    const checkbox = e.target;
    if (!checkbox.matches('input[name="select"]') || checkbox.disabled) return;

    const changed = applyRowSelection(checkbox, _membersByLag(), true);
    if (refreshRequiredSelections() || changed) {
        updateBulkActionButton();
    }
});

// Keep the cascade in step with #autoSelectLagMembers: turning it off releases every row this
// code is holding, turning it back on re-derives them from what the user has selected.
document.addEventListener('change', function (e) {
    const toggle = e.target;
    if (!toggle.matches('#autoSelectLagMembers')) return;

    let changed = false;
    if (!toggle.checked) {
        // One rule in both directions: turning the cascade off gives back exactly the rows it
        // added, so the selection left behind is the one the user made themselves.
        document.querySelectorAll('input[name="select"][data-auto-member]').forEach(function (checkbox) {
            delete checkbox.dataset[MEMBER_MARKER];
            if (checkbox.checked) {
                checkbox.checked = false;
                changed = true;
            }
        });
    }
    if (refreshRequiredSelections() || changed) {
        const table =
            document.getElementById('librenms-interface-table') ||
            document.getElementById('librenms-interface-table-vm');
        if (table) persistTableSelection(table);
        updateBulkActionButton();
    }

    if (!toggle.checked) {
        const noticeContainer = document.getElementById('parent-cross-page-notices');
        if (noticeContainer) {
            noticeContainer.remove();
        }
    }
});

// ============================================
// CROSS-PAGE SELECTION
// ============================================

/**
 * Selection persistence for the paginated sync tables.
 *
 * The comparison tables paginate with ordinary links, so moving to page 2 reloads the document
 * and every checked box is gone. The rows a user wants rarely sit on one page, so the selection
 * is kept per table in sessionStorage: written on every change, restored when the table renders,
 * and submitted as hidden inputs for the rows that are not on the visible page.
 *
 * All the sync POST handlers resolve a row from its own key against the whole cached snapshot
 * rather than the rendered page, so a row selected on another page needs no server-side change.
 * Per-row companion inputs (a VC member dropdown, a module's target device) are stored with the
 * row and re-submitted alongside it.
 */

/** Tables whose selection survives pagination, and the tab each belongs to. */
const SELECTABLE_TABLE_IDS = [
    'librenms-interface-table',
    'librenms-interface-table-vm',
    'librenms-cable-table',
    'librenms-cable-table-vc',
    'librenms-ipaddress-table',
    'librenms-vlan-table',
    'librenms-port-vlan-table',
    'librenms-module-table',
];

/**
 * Build the sessionStorage key for one table.
 *
 * Scoped to the object being synced (the path), the table, and the LibreNMS server, so two
 * devices, two tabs, or two servers never share a selection. The page number is deliberately
 * left out: carrying the selection across pages is the whole point.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {string} The storage key.
 */
function _selectionStorageKey(table) {
    const serverKeyInput = document.querySelector('[name=server_key]');
    const serverKey = serverKeyInput ? serverKeyInput.value : '';
    return 'lnmsSelection:' + window.location.pathname + ':' + table.id + ':' + serverKey;
}

/**
 * Read a table's stored selection.
 *
 * Each entry records the row's companion inputs and whether the cascade put the row there, so a
 * row restored after paging keeps the same standing it had before.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {Object<string, {inputs: Object<string, string>, auto: string}>} Selected rows by key.
 */
function readStoredSelection(table) {
    try {
        const raw = window.sessionStorage.getItem(_selectionStorageKey(table));
        const parsed = raw ? JSON.parse(raw) : null;
        // A hand-edited or half-written entry must not take the table down with it. The result
        // gets a null prototype so a row key like __proto__ becomes an ordinary entry instead of
        // hitting the prototype setter and disappearing.
        const store = Object.create(null);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            Object.keys(parsed).forEach(function (key) {
                store[key] = parsed[key];
            });
        }
        return store;
    } catch (_) {
        // Private browsing and blocked site data both throw on access, not just on write.
        return Object.create(null);
    }
}

/**
 * Replace a table's stored selection.
 *
 * @param {HTMLElement} table - The table element.
 * @param {Object} selection - Companion input values per selected row key.
 * @returns {void}
 */
function writeStoredSelection(table, selection) {
    try {
        const key = _selectionStorageKey(table);
        if (Object.keys(selection).length) {
            window.sessionStorage.setItem(key, JSON.stringify(selection));
        } else {
            window.sessionStorage.removeItem(key);
        }
    } catch (_) {
        // Storage is a convenience here; the visible page still submits its own checked rows.
    }
}

/**
 * Collect the companion inputs a row submits alongside its checkbox.
 *
 * A dropdown or hidden field inside the row (the VC member, a module's target device) is part of
 * that row's answer, so it has to travel with the row when the row is submitted from another page.
 *
 * @param {HTMLTableRowElement} row - The row to read.
 * @returns {Object<string, string>} Companion values by input name.
 */
function _rowCompanionInputs(row) {
    const values = {};
    row.querySelectorAll('select[name], input[type="hidden"][name]').forEach(function (input) {
        if (input.name && input.name !== 'select') {
            values[input.name] = input.value;
        }
    });
    return values;
}

/**
 * Store the visible page's selection, keeping rows selected on other pages untouched.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {void}
 */
function persistTableSelection(table) {
    if (!table || !table.id) return;
    const selection = readStoredSelection(table);
    table.querySelectorAll('td input[name="select"]').forEach(function (checkbox) {
        const row = checkbox.closest('tr');
        if (!row) return;
        if (checkbox.checked) {
            selection[checkbox.value] = {
                inputs: _rowCompanionInputs(row),
                auto: checkbox.dataset[REQUIRED_MARKER]
                    ? 'required'
                    : checkbox.dataset[MEMBER_MARKER]
                      ? 'member'
                      : '',
            };
        } else {
            // Only the visible page can retract a row: an absent checkbox means "another page",
            // not "cleared".
            delete selection[checkbox.value];
        }
    });
    writeStoredSelection(table, selection);
    updateOffPageSelectionNotice(table);
}

/**
 * Re-check the visible rows a previous page of this table left selected.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {void}
 */
function restoreTableSelection(table) {
    if (!table || !table.id) return;
    const selection = readStoredSelection(table);
    let restored = false;
    table.querySelectorAll('td input[name="select"]:not(:disabled)').forEach(function (checkbox) {
        const entry = selection[checkbox.value];
        if (!entry || checkbox.checked) return;
        checkbox.checked = true;
        // Restore how the row got there: a row the cascade added must still be released when
        // the row that needed it is cleared, rather than becoming a choice of the user's.
        if (entry.auto === 'required') {
            checkbox.dataset[REQUIRED_MARKER] = 'true';
        } else if (entry.auto === 'member') {
            checkbox.dataset[MEMBER_MARKER] = 'true';
        }
        restored = true;
    });
    if (restored) {
        refreshRequiredSelections();
        updateBulkActionButton();
    }
    updateOffPageSelectionNotice(table);
}

/**
 * Return the stored row keys that are not rendered on the visible page.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {string[]} Row keys selected on other pages.
 */
function offPageSelectionKeys(table) {
    const selection = readStoredSelection(table);
    const visible = new Set();
    table.querySelectorAll('td input[name="select"]').forEach(function (checkbox) {
        visible.add(checkbox.value);
    });
    return Object.keys(selection).filter(function (key) {
        return !visible.has(key);
    });
}

/**
 * Show how many rows are selected on other pages, with a control to clear them.
 *
 * Selections the user cannot see would otherwise be submitted silently, so the count is stated
 * on the page that will submit them.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {void}
 */
function updateOffPageSelectionNotice(table) {
    const count = offPageSelectionKeys(table).length;
    const noticeId = table.id + '-offpage-selection';
    let notice = document.getElementById(noticeId);

    if (!count) {
        if (notice) notice.remove();
        return;
    }

    if (!notice) {
        notice = document.createElement('div');
        notice.id = noticeId;
        notice.className = 'alert alert-info alert-sm py-1 px-2 small mb-1 d-flex align-items-center gap-2';
        const icon = document.createElement('i');
        icon.className = 'mdi mdi-checkbox-multiple-marked-outline';
        notice.appendChild(icon);
        notice.appendChild(document.createElement('span'));
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'btn btn-link btn-sm p-0';
        clear.textContent = 'Clear';
        clear.addEventListener('click', function () {
            // The notice counts only the rows on other pages, so Clear drops only those. Wiping
            // the store and the visible checkboxes would discard a selection the user can see and
            // did not ask to lose.
            const selection = readStoredSelection(table);
            offPageSelectionKeys(table).forEach(function (key) {
                delete selection[key];
            });
            writeStoredSelection(table, selection);
            updateOffPageSelectionNotice(table);
            updateBulkActionButton();
        });
        notice.appendChild(clear);
        table.parentNode.insertBefore(notice, table);
    }

    notice.querySelector('span').textContent =
        count === 1 ? '1 more row is selected on another page.' : count + ' more rows are selected on other pages.';
}

/**
 * Forget a table's stored selection.
 *
 * @param {HTMLElement} table - The table element.
 * @returns {void}
 */
function clearStoredSelection(table) {
    writeStoredSelection(table, {});
    updateOffPageSelectionNotice(table);
}

/**
 * Add the off-page selection to a form as hidden inputs, then forget it.
 *
 * The rows on the visible page serialize themselves; only the ones the user selected elsewhere
 * need injecting. The store is cleared because submitting consumes the selection.
 *
 * @param {HTMLFormElement} form - The form being submitted.
 * @returns {void}
 */
function injectOffPageSelections(form) {
    form.querySelectorAll('input[data-offpage-select]').forEach(function (input) {
        input.remove();
    });

    SELECTABLE_TABLE_IDS.forEach(function (tableId) {
        const table = document.getElementById(tableId);
        // The module table sits OUTSIDE its form and is rebuilt at htmx:configRequest instead,
        // which would drop anything injected here.
        if (!table || !form.contains(table)) return;

        const selection = readStoredSelection(table);
        offPageSelectionKeys(table).forEach(function (rowKey) {
            const hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = 'select';
            hidden.value = rowKey;
            hidden.dataset.offpageSelect = '1';
            form.appendChild(hidden);

            const companions = (selection[rowKey] && selection[rowKey].inputs) || {};
            Object.keys(companions).forEach(function (name) {
                const companion = document.createElement('input');
                companion.type = 'hidden';
                companion.name = name;
                companion.value = companions[name];
                companion.dataset.offpageSelect = '1';
                form.appendChild(companion);
            });
        });
        clearStoredSelection(table);
    });
}

// Persist on every selection change, whoever made it: a click, a select-all, or the requirement
// cascade pulling in a parent. The listener is registered once for the document, so a table
// swapped in by HTMX is covered without re-binding.
document.addEventListener('change', function (e) {
    if (!e.target.matches || !e.target.matches('input[name="select"]')) return;
    const table = e.target.closest('table');
    if (table && SELECTABLE_TABLE_IDS.includes(table.id)) {
        persistTableSelection(table);
    }
});

document.addEventListener('submit', function (e) {
    if (e.target instanceof HTMLFormElement) {
        injectOffPageSelections(e.target);
    }
});

/**
 * Show a brief inline notice when a row's parent or aggregate is on another page.
 *
 * The relationship is applied when that interface already exists in NetBox, but an interface
 * NetBox does not have yet is only created by syncing its own row, so the notice says where to
 * find it rather than promising it is handled. The selection survives the trip.
 *
 * The notice auto-dismisses after 5 seconds.
 *
 * @param {string} relatedName - Name of the required interface.
 * @param {string} kind - Relationship label, 'Parent' or 'LAG'.
 */
function _showRequiredRowCrossPageNotice(relatedName, kind) {
    const containerId = 'parent-cross-page-notices';
    let container = document.getElementById(containerId);
    if (!container) {
        // Insert before the table (find a stable anchor inside the form)
        const table = document.getElementById('librenms-interface-table') ||
                      document.getElementById('librenms-interface-table-vm');
        if (!table) return;
        container = document.createElement('div');
        container.id = containerId;
        table.parentNode.insertBefore(container, table);
    }

    // Avoid duplicate notices for the same interface
    if (container.querySelector('[data-required="' + CSS.escape(relatedName) + '"]')) return;

    const notice = document.createElement('div');
    notice.className = 'alert alert-info alert-dismissible py-1 px-2 small mb-1';
    notice.dataset.required = relatedName;

    // Build the notice via DOM nodes rather than innerHTML: the name is interface
    // data and must never be interpreted as HTML (DOM-XSS). textContent escapes it.
    const icon = document.createElement('i');
    icon.className = 'mdi mdi-information-outline me-1';
    notice.appendChild(icon);
    notice.appendChild(document.createTextNode(kind + ' interface '));
    const strong = document.createElement('strong');
    strong.textContent = relatedName;
    notice.appendChild(strong);
    notice.appendChild(
        document.createTextNode(
            ' is on another page. Select it there if it is not in NetBox yet; this page keeps your selection.'
        )
    );
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn-close btn-sm';
    closeBtn.setAttribute('data-bs-dismiss', 'alert');
    closeBtn.setAttribute('aria-label', 'Close');
    notice.appendChild(closeBtn);

    container.appendChild(notice);

    setTimeout(function () {
        if (notice.parentNode) notice.parentNode.removeChild(notice);
    }, 5000);
}

// ============================================
// VIRTUAL CHASSIS & VRF HANDLING
// ============================================

/**
 * Initialize TomSelect dropdowns for VC member selection.
 * Waits for TomSelect initialization before attaching change handlers.
 */
function initializeVCMemberSelect() {
    setTimeout(() => {
        const interfaceTable = document.getElementById('librenms-interface-table');
        const cableTable = document.getElementById('librenms-cable-table-vc');
        const moduleTable = document.getElementById('librenms-module-table');

        if (interfaceTable) {
            // Only target VC member selects, exclude VLAN group selects
            const interfaceSelects = interfaceTable.querySelectorAll('.form-select.tomselected:not(.vlan-group-select)');
            interfaceSelects.forEach(select => {
                if (select.tomselect && !select.dataset.interfaceSelectInitialized) {
                    select.dataset.interfaceSelectInitialized = 'true';
                    // Seed the rollback baseline HERE, before the change listener is attached:
                    // at init time select.value still reflects the originally rendered (verified)
                    // assignment. Seeding it lazily inside handleInterfaceChange instead would run
                    // after select.value already equals the newly-selected member, so a verify
                    // failure would "roll back" to the rejected member.
                    if (typeof select._lastVerifiedMember === 'undefined') {
                        const selectedOption = select.querySelector('option[selected]');
                        select._lastVerifiedMember = selectedOption ? selectedOption.value : select.value;
                    }
                    select.tomselect.on('change', function (value) {
                        handleInterfaceChange(select, value);
                    });
                }
            });
        }

        if (cableTable) {
            const cableSelects = cableTable.querySelectorAll('.form-select.tomselected');
            cableSelects.forEach(select => {
                if (select.tomselect && !select.dataset.cableSelectInitialized) {
                    select.dataset.cableSelectInitialized = 'true';
                    select.tomselect.on('change', function (value) {
                        handleCableChange(select, value);
                    });
                }
            });
        }

        if (moduleTable) {
            const moduleSelects = moduleTable.querySelectorAll('.vc-member-select');
            moduleSelects.forEach(select => {
                if (select.tomselect && !select.dataset.moduleSelectInitialized) {
                    select.dataset.moduleSelectInitialized = 'true';
                    select.tomselect.on('change', function (value) {
                        handleModuleChange(select, value);
                    });
                } else if (!select.tomselect && !select.dataset.moduleSelectInitialized) {
                    select.dataset.moduleSelectInitialized = 'true';
                    select.addEventListener('change', function () {
                        handleModuleChange(select, this.value);
                    });
                }
            });
        }
    }, TOMSELECT_INIT_DELAY_MS);
}

/**
 * Initialize VRF assignment dropdowns for IP addresses.
 * Handles both TomSelect-enhanced and standard select elements.
 */
function initializeVRFSelects() {
    setTimeout(() => {
        const ipAddressTable = document.getElementById('librenms-ipaddress-table');

        if (ipAddressTable) {
            // Find VRF dropdowns - look for both plain selects and TomSelect-enhanced ones
            const vrfSelects = ipAddressTable.querySelectorAll('.vrf-select');

            vrfSelects.forEach(select => {
                // Skip already initialized selects by checking the data attribute
                if (select.tomselect && !select.dataset.vrfSelectInitialized) {
                    select.dataset.vrfSelectInitialized = 'true';

                    // Add TomSelect listener
                    select.tomselect.on('change', function (value) {
                        handleVRFChange(select, value);
                    });
                }
                // For standard selects without TomSelect (fallback)
                else if (!select.tomselect && !select.dataset.vrfSelectInitialized) {
                    select.dataset.vrfSelectInitialized = 'true';

                    // Add direct event listener for regular selects
                    select.addEventListener('change', function (event) {
                        handleVRFChange(select, this.value);
                    });
                }
            });
        }
    }, TOMSELECT_INIT_DELAY_MS);
}

/**
 * Initialize VLAN edit buttons that open the VLAN detail modal.
 * Each button carries per-VLAN data and VLAN group options as data attributes.
 */
function initializeVlanGroupSelects() {
    document.querySelectorAll('.vlan-edit-btn').forEach(btn => {
        if (btn.dataset.vlanEditInitialized) return;
        btn.dataset.vlanEditInitialized = 'true';

        btn.addEventListener('click', function (e) {
            e.preventDefault();
            openVlanDetailModal(this);
        });
    });
}

/**
 * Open the VLAN detail modal for a specific interface.
 * Populates the modal table with per-VLAN rows and group dropdowns.
 *
 * @param {HTMLElement} btn - The edit button element with data attributes
 */
function openVlanDetailModal(btn) {
    const interfaceName = btn.dataset.interface;
    const rowKey = btn.dataset.rowKey;
    const deviceId = btn.dataset.deviceId;
    const vlans = JSON.parse(btn.dataset.vlans);
    const vlanGroups = JSON.parse(btn.dataset.vlanGroups);

    // Set modal title
    document.getElementById('vlanModalInterfaceName').textContent = interfaceName;

    // Store current interface context on modal for save handler
    const modal = document.getElementById('vlanDetailModal');
    modal.dataset.currentInterface = interfaceName;
    modal.dataset.currentRowKey = rowKey;
    modal.dataset.currentDeviceId = deviceId;

    // Clear any stale error from a previous save attempt
    const staleAlert = modal.querySelector('.vlan-override-error');
    if (staleAlert) { staleAlert.remove(); }

    // Build table rows
    const tbody = document.getElementById('vlanDetailTableBody');
    tbody.innerHTML = '';

    vlans.forEach(vlan => {
        const tr = document.createElement('tr');

        // VID cell
        const tdVid = document.createElement('td');
        const vidSpan = document.createElement('span');
        vidSpan.className = vlan.css;
        vidSpan.textContent = vlan.vid;
        if (vlan.missing) {
            vidSpan.innerHTML += ' <i class="mdi mdi-alert text-danger" title="VLAN not in NetBox"></i>';
        }
        tdVid.appendChild(vidSpan);
        tr.appendChild(tdVid);

        // Type cell
        const tdType = document.createElement('td');
        tdType.textContent = vlan.type === 'U' ? 'Untagged' : 'Tagged';
        tr.appendChild(tdType);

        // VLAN Group dropdown cell
        const tdGroup = document.createElement('td');

        {
            const select = document.createElement('select');
            select.className = 'form-select form-select-sm vlan-modal-group-select';
            select.dataset.vid = vlan.vid;
            select.dataset.interface = interfaceName;
            select.dataset.rowKey = rowKey;

            vlanGroups.forEach(group => {
                const option = document.createElement('option');
                option.value = group.id;
                option.textContent = group.scope ? `${group.name} (${group.scope})` : group.name;
                if (String(group.id) === String(vlan.group_id)) {
                    option.selected = true;
                }
                select.appendChild(option);
            });

            // On change, update the hidden input for this VLAN immediately
            select.addEventListener('change', function () {
                updateHiddenVlanGroupInput(rowKey, vlan.vid, this.value);

                // Re-verify VLAN colors after group change
                verifyVlanInGroup(this, deviceId, vlan.vid, vlan.type, this.value);
            });

            tdGroup.appendChild(select);
        }
        tr.appendChild(tdGroup);

        tbody.appendChild(tr);
    });

    // Reset "apply to all" checkbox
    const applyAllCheckbox = document.getElementById('applyVlanGroupToAll');
    if (applyAllCheckbox) {
        applyAllCheckbox.checked = false;
    }

    showModal(document.getElementById('vlanDetailModal'));
}

/**
 * Update the hidden input for a specific VLAN group assignment.
 *
 * @param {string} rowKey - Stable LibreNMS port ID
 * @param {number} vid - VLAN ID
 * @param {string} groupId - Selected group ID
 */
function updateHiddenVlanGroupInput(rowKey, vid, groupId) {
    const input = document.querySelector(
        `input.vlan-group-hidden[name="vlan_group_${rowKey}_${vid}"]`
    );
    if (input) {
        input.value = groupId;
    }
}

/**
 * Verify if a VLAN exists in the selected group and update the modal row status.
 * Also updates the css property in the edit button's data-vlans so that when
 * the modal is saved, the inline summary can be re-rendered with correct colors.
 *
 * @param {HTMLSelectElement} select - The group dropdown in the modal
 * @param {string} deviceId - Device ID for API call
 * @param {number} vid - VLAN ID to verify
 * @param {string} vlanType - "U" for untagged, "T" for tagged
 * @param {string} groupId - Selected group ID
 */
let pendingVlanVerifications = 0;

function _vlanVerifyStart(saveBtn) {
    pendingVlanVerifications++;
    if (saveBtn) saveBtn.disabled = true;
}

function _vlanVerifyEnd(saveBtn) {
    pendingVlanVerifications = Math.max(0, pendingVlanVerifications - 1);
    if (saveBtn && pendingVlanVerifications === 0) saveBtn.disabled = false;
}

function verifyVlanInGroup(select, deviceId, vid, vlanType, groupId) {

    const saveBtn = document.getElementById('saveVlanGroups');
    _vlanVerifyStart(saveBtn);

    // Capture rowKey before the async fetch to avoid stale closure if the modal
    // is opened for a different interface while this request is in flight.
    const modal = document.getElementById('vlanDetailModal');
    const capturedRowKey = modal?.dataset.currentRowKey;

    const csrfToken = getCsrfToken();
    if (!csrfToken) {
        _vlanVerifyEnd(saveBtn);  // don't leave the Save button stuck disabled
        return;
    }

    fetch('/plugins/librenms_plugin/verify-vlan-group/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({
            device_id: deviceId,
            interface_name: select.dataset.interface,
            vlan_group_id: groupId,
            vid: String(vid),
            vlan_type: vlanType
        })
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(msg); });
            }
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                const newCss = data.css_class || 'text-danger';
                const isMissing = data.is_missing;

                // Update the VID color and warning icon in the modal row
                const row = select.closest('tr');
                if (row) {
                    const vidSpan = row.querySelector('td:first-child span');
                    if (vidSpan) {
                        vidSpan.className = newCss;
                        // Update warning icon
                        const existingIcon = vidSpan.querySelector('.mdi-alert');
                        if (isMissing && !existingIcon) {
                            vidSpan.innerHTML = vid + ' <i class="mdi mdi-alert text-danger" title="VLAN not in selected group\u2014use VLAN Sync first to create it"></i>';
                        } else if (!isMissing && existingIcon) {
                            vidSpan.textContent = String(vid);
                        }
                    }
                }

                // Store the updated CSS on the modal row for the save handler to read
                if (row) {
                    row.dataset.resolvedCss = newCss;
                    row.dataset.resolvedMissing = isMissing ? 'true' : 'false';
                }

                // Update the css in the source edit button's data-vlans
                if (capturedRowKey) {
                    const btn = document.querySelector(`.vlan-edit-btn[data-row-key="${capturedRowKey}"]`);
                    if (btn) {
                        try {
                            const btnVlans = JSON.parse(btn.dataset.vlans);
                            const entry = btnVlans.find(v => String(v.vid) === String(vid));
                            if (entry) {
                                entry.css = newCss;
                                entry.missing = isMissing;
                            }
                            btn.dataset.vlans = JSON.stringify(btnVlans);
                        } catch (e) { /* skip */ }
                    }
                }
            }
        })
        .catch(err => {
            console.error('VLAN group verify failed:', err && err.message ? err.message : String(err));
        })
        .finally(() => {
            const saveBtn = document.getElementById('saveVlanGroups');
            _vlanVerifyEnd(saveBtn);
        });
}

/**
 * Initialize the VLAN modal save button.
 * Handles "Apply to all interfaces" when the checkbox is checked.
 */
function initializeVlanModalSave() {
    const saveBtn = document.getElementById('saveVlanGroups');
    if (!saveBtn || saveBtn.dataset.initialized) return;
    saveBtn.dataset.initialized = 'true';

    saveBtn.addEventListener('click', function () {
        const applyToAll = document.getElementById('applyVlanGroupToAll')?.checked;
        const modalEl = document.getElementById('vlanDetailModal');
        const currentRowKey = modalEl.dataset.currentRowKey;

        // Collect all group selections and resolved CSS from the modal
        const modalSelects = document.querySelectorAll('#vlanDetailTableBody .vlan-modal-group-select');
        const vidGroupMap = {};
        const vidCssMap = {};
        const vidMissingMap = {};
        modalSelects.forEach(select => {
            vidGroupMap[select.dataset.vid] = select.value;
            // Pick up resolved CSS from the verify endpoint (stored on the row)
            const row = select.closest('tr');
            if (row && row.dataset.resolvedCss) {
                vidCssMap[select.dataset.vid] = row.dataset.resolvedCss;
                vidMissingMap[select.dataset.vid] = row.dataset.resolvedMissing === 'true';
            }
        });

        // Determine which buttons to update
        const buttonsToUpdate = applyToAll
            ? document.querySelectorAll('.vlan-edit-btn')
            : document.querySelectorAll(`.vlan-edit-btn[data-row-key="${currentRowKey}"]`);

        // Apply DOM mutations (btn.dataset.vlans, hidden inputs, summary spans)
        // Called only after a successful server response when persisting, or immediately otherwise.
        function applyButtonUpdates() {
            buttonsToUpdate.forEach(btn => {
                try {
                    const btnVlans = JSON.parse(btn.dataset.vlans);
                    const groups = JSON.parse(btn.dataset.vlanGroups);
                    const btnRowKey = btn.dataset.rowKey;
                    let changed = false;

                    btnVlans.forEach(v => {
                        if (vidGroupMap.hasOwnProperty(String(v.vid))) {
                            const newGroupId = vidGroupMap[String(v.vid)];
                            const matchedGroup = groups.find(g => String(g.id) === String(newGroupId));
                            // A VC member can expose a different scoped group for the same VID.
                            // Do not copy a source row's scoped group into a row that cannot select it.
                            if (newGroupId && !matchedGroup) return;
                            v.group_id = newGroupId;

                            // Apply resolved missing/css state BEFORE computing group_name
                            // so group_name reflects the verified state from the server.
                            if (vidCssMap.hasOwnProperty(String(v.vid))) {
                                v.css = vidCssMap[String(v.vid)];
                                v.missing = vidMissingMap[String(v.vid)] || false;
                            }

                            if (v.missing) {
                                v.group_name = 'Not in NetBox';
                            } else {
                                v.group_name = matchedGroup ? matchedGroup.name : '-- No Group (Global) --';
                            }

                            changed = true;

                            // Update the hidden input for this VID on this interface
                            const input = document.querySelector(
                                `input.vlan-group-hidden[name="vlan_group_${btnRowKey}_${v.vid}"]`
                            );
                            if (input) {
                                input.value = newGroupId;
                            }
                        }
                    });

                    if (changed) {
                        btn.dataset.vlans = JSON.stringify(btnVlans);
                        // Update the tooltip and re-render inline summary colors
                        const summarySpan = btn.previousElementSibling;
                        if (summarySpan && summarySpan.tagName === 'SPAN') {
                            const tooltipLines = btnVlans.map(v =>
                                v.missing
                                    ? `VLAN ${v.vid}(${v.type}) \u2192 \u26A0 Not in NetBox`
                                    : `VLAN ${v.vid}(${v.type}) \u2192 ${v.group_name}`
                            );
                            summarySpan.title = tooltipLines.join('\n');

                            // Re-render inline VLAN summary with correct colors
                            const MAX_INLINE = 3;
                            const inlineParts = btnVlans.slice(0, MAX_INLINE).map(v => {
                                const warning = v.missing
                                    ? ' <i class="mdi mdi-alert text-danger" title="VLAN not in selected group\u2014use VLAN Sync first to create it"></i>'
                                    : '';
                                return `<span class="${v.css}">${v.vid}(${v.type})${warning}</span>`;
                            });
                            let html = inlineParts.join(', ');
                            if (btnVlans.length > MAX_INLINE) {
                                const extra = btnVlans.length - MAX_INLINE;
                                html += ` <span class="text-muted">+${extra} more</span>`;
                            }
                            summarySpan.innerHTML = html;
                        }
                    }
                } catch (e) {
                    // Skip buttons with invalid data
                }
            });
        }

        // Persist overrides in server cache so other table pages pick them up
        if (applyToAll && Object.keys(vidGroupMap).length > 0) {
            const deviceId = modalEl.dataset.currentDeviceId;
            const csrfToken = getCsrfToken();
            if (!csrfToken) {
                // Can't persist without a CSRF token; surface it via the same error UI the
                // fetch .catch uses rather than letting a `.value`-on-null TypeError abort silently.
                let alertEl = modalEl.querySelector('.vlan-override-error');
                if (!alertEl) {
                    alertEl = document.createElement('div');
                    alertEl.className = 'vlan-override-error alert alert-danger mt-2';
                    modalEl.querySelector('.modal-body')?.appendChild(alertEl);
                }
                alertEl.textContent = 'Failed to save VLAN group overrides: CSRF token not found.';
                return;
            }
            fetch('/plugins/librenms_plugin/save-vlan-group-overrides/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    device_id: deviceId,
                    vid_group_map: vidGroupMap,
                    server_key: document.querySelector('input[name="server_key"]')?.value || null
                })
            }).then(response => {
                if (!response.ok) {
                    return fetchErrorMessage(response).then(msg => { throw new Error(`HTTP ${response.status}: ${msg}`); });
                }
                // Apply DOM mutations only after the server has persisted the overrides
                applyButtonUpdates();
                // Close modal on success
                hideModal(modalEl);
            }).catch(error => {
                console.error('Failed to persist VLAN group overrides:', error.message);
                let alertEl = modalEl.querySelector('.vlan-override-error');
                if (!alertEl) {
                    alertEl = document.createElement('div');
                    alertEl.className = 'vlan-override-error alert alert-danger mt-2';
                    modalEl.querySelector('.modal-body')?.appendChild(alertEl);
                }
                alertEl.textContent = 'Failed to save VLAN group overrides: ' + error.message;
            });
        } else {
            // No server persist needed — apply DOM mutations and close immediately
            applyButtonUpdates();
            hideModal(modalEl);
        }
    });
}

// ============================================
// VLAN SYNC TABLE GROUP VERIFICATION
// ============================================

/**
 * Initialize change listeners on the VLAN sync table's per-row group dropdowns.
 * When the user changes the VLAN group for a row, re-checks whether the VID
 * exists in the selected group and updates row colors accordingly.
 */
function initializeVlanSyncGroupSelects() {
    document.querySelectorAll('.vlan-sync-group-select').forEach(function (select) {
        if (select.dataset.vlanSyncInitialized) return;
        select.dataset.vlanSyncInitialized = 'true';

        select.addEventListener('change', function () {
            const vid = this.dataset.vlanId;
            const vlanName = this.dataset.vlanName;
            const groupId = this.value;

            verifyVlanSyncGroup(this, vid, vlanName, groupId);
        });
    });
}

/**
 * Verify if a VLAN exists in the selected group and update the row colors.
 *
 * @param {HTMLSelectElement} select - The group dropdown element
 * @param {string} vid - VLAN ID
 * @param {string} vlanName - VLAN name from LibreNMS
 * @param {string} groupId - Selected VLAN group ID (empty string = global)
 */
function verifyVlanSyncGroup(select, vid, vlanName, groupId) {
    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');
    if (!csrfToken) return;

    fetch('/plugins/librenms_plugin/verify-vlan-sync-group/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken.value
        },
        body: JSON.stringify({
            vid: String(vid),
            name: vlanName,
            vlan_group_id: groupId || null
        })
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(`HTTP ${response.status}: ${msg}`); });
            }
            return response.json();
        })
        .then(data => {
            if (data.status !== 'success') return;

            const row = select.closest('tr');
            if (!row) return;

            const cssClass = data.css_class || 'text-danger';

            // Update the VLAN ID cell color
            const vidCell = row.querySelector('td[data-col="vlan_id"] span');
            if (vidCell) {
                vidCell.className = cssClass;
            }

            // Update the Name cell color and tooltip
            const nameCell = row.querySelector('td[data-col="name"] span');
            if (nameCell) {
                nameCell.className = cssClass;

                // Add/remove name mismatch tooltip
                if (data.exists_in_netbox && !data.name_matches && data.netbox_vlan_name) {
                    nameCell.title = 'NetBox: ' + data.netbox_vlan_name + ' | LibreNMS: ' + vlanName;
                } else {
                    nameCell.title = '';
                }
            }
        })
        .catch(error => {
            console.error('VLAN sync group verification error:', error);
        });
}

/**
 * Handle VRF selection change and verify IP address assignment.
 * Sends verification request to backend and updates row status.
 *
 * @param {HTMLSelectElement} select - The VRF dropdown element
 * @param {string} value - Selected VRF ID
 */
function handleVRFChange(select, value) {
    const ipAddress = select.dataset.ip;
    const prefixLength = select.dataset.prefix || "";  // Get prefix length if present
    const fullIpAddress = prefixLength ? `${ipAddress}/${prefixLength}` : ipAddress;

    // Extract device ID from URL
    const deviceInfo = getDeviceIdFromUrl();
    if (!deviceInfo) {
        return;
    }
    const deviceId = deviceInfo.id;

    const csrfToken = getCsrfToken();
    if (!csrfToken) return;  // missing token → abort rather than throw on `.value`

    fetch('/plugins/librenms_plugin/verify-ipaddress/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({
            device_id: deviceId,
            ip_address: fullIpAddress,  // Use full IP address with prefix
            row_id: select.dataset.rowId,
            vrf_id: value,
            server_key: document.querySelector('input[name="server_key"]')?.value || null
        })
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(msg); });
            }
            return response.json();
        })
        .then(data => {
            const row = select.closest('tr');

            if (data.status === 'success' && row && data.formatted_row) {
                const statusCell = row.querySelector('td[data-col="status"]');
                if (statusCell) {
                    statusCell.innerHTML = data.formatted_row.status;
                }
            }
        })
        .catch(error => {
            console.error('VRF verification failed:', error.message);
        });
}

/**
 * Handle VC member selection change and verify interface mapping.
 * Fetches interface data from selected device and updates table row.
 *
 * @param {HTMLSelectElement} select - The VC member dropdown element
 * @param {string} value - Selected device ID
 */
function handleInterfaceChange(select, value) {
    const csrfToken = getCsrfToken();
    if (!csrfToken) return;  // missing token → abort rather than throw on `.value`
    // Abort any still-in-flight verification for this select: on rapid VC-member changes an
    // older /verify-interface/ response can otherwise arrive after a newer one and repaint the
    // row with stale cells/relationship controls. Mirrors handleModuleChange's AbortController.
    if (select._interfaceVerifyController) {
        select._interfaceVerifyController.abort();
    }
    const controller = new AbortController();
    select._interfaceVerifyController = controller;

    // Resolve the row from the changed <select> itself, not by data-interface: the same
    // interface name can appear in both the main and OOB datasets, so a name lookup can
    // post the wrong row's port_id and repaint the wrong row. closest('tr') is unique.
    const row = select.closest('tr');

    // Defensive fallback: the baseline is normally seeded at init time (initializeVCMemberSelect),
    // before this change listener is attached, while select.value still holds the verified
    // assignment. If for some reason it wasn't, seed it ONLY from the rendered <option selected> —
    // NOT from select.value, which by the time this callback runs already equals the newly
    // selected (unverified) member, so a verify failure would otherwise "roll back" to it.
    if (typeof select._lastVerifiedMember === 'undefined') {
        const selectedOption = select.querySelector('option[selected]');
        select._lastVerifiedMember = selectedOption ? selectedOption.value : null;
    }

    // Disable this row's LAG/parent sync buttons while the verify is in flight. A click landing
    // before the response repaints the row would otherwise POST the freshly-selected member's
    // objectId together with the *previous* member's stale relationship metadata (lag/parent
    // port_id + name) carried by the old button markup, syncing the wrong relationship.
    // Tag the buttons *this* flow disables with data-verify-locked so re-enabling can target
    // exactly them by re-querying the live row, rather than replaying a captured list. Two
    // reasons: (1) on rapid changes a second handler captures an empty set (the buttons are
    // already disabled) and the first is aborted without re-enabling — re-querying the marker
    // lets whichever request settles last restore them; (2) the relationship sync click handler
    // also disables its own button mid-POST (and keeps it disabled on success), so we must not
    // re-enable a button it owns — only ones we marked. We only lock currently-enabled buttons,
    // so a button already disabled by an in-flight sync is left untouched.
    if (row) {
        row.querySelectorAll('.lag-sync-btn:not([disabled]), .parent-sync-btn:not([disabled])').forEach((b) => {
            b.disabled = true;
            b.dataset.verifyLocked = '1';
        });
    }
    const reenableRelationshipButtons = () => {
        if (!row) return;
        // Re-query so the request that settles last re-enables whatever is still verify-locked,
        // even buttons a superseded (aborted) request had locked. Buttons whose cell a successful
        // verify repainted are gone from the row (replaced by fresh enabled markup), so they're
        // simply not found here.
        row.querySelectorAll('.lag-sync-btn[data-verify-locked], .parent-sync-btn[data-verify-locked]').forEach((b) => {
            delete b.dataset.verifyLocked;
            b.disabled = false;
        });
    };

    // On a verify failure (non-success payload or a non-abort error), roll the dropdown back to
    // the last confirmed member so the visible selection matches the row's current HTML, then
    // re-enable the controls — the row is now consistent again and the user can retry. With no
    // confirmed baseline yet, keep the controls locked rather than re-enabling on an unverified
    // selection (which would let a retry post the previous member's stale lag/parent port_id).
    const rollbackToLastVerified = () => {
        if (select._lastVerifiedMember != null) {
            // The member control is TomSelect-enhanced, so assigning select.value alone
            // leaves the visible dropdown showing the rejected member while the backing
            // value rolls back — the next sync click would then post a different member
            // than the user sees. Sync through the widget (mirrors initializeVCMemberSelect
            // at the vcMemberSelect.tomselect.setValue call); pass silent=true so this
            // programmatic reset doesn't re-fire the change handler.
            if (select.tomselect && typeof select.tomselect.setValue === 'function') {
                select.tomselect.setValue(select._lastVerifiedMember, true);
            } else {
                select.value = select._lastVerifiedMember;
            }
            reenableRelationshipButtons();
        }
    };

    fetch('/plugins/librenms_plugin/verify-interface/', {
        method: 'POST',
        signal: controller.signal,
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({
            device_id: value,
            // Keep page-level migrated mode stable when the selected VC member changes.
            origin_device_id: row?.closest('[data-interface-origin-device-id]')?.dataset.interfaceOriginDeviceId || null,
            interface_name: select.dataset.interface,
            // Stable port_id of this row so the server picks the correct cached row even when
            // display names collide (host vs OOB controller); interface_name is the fallback.
            port_id: row?.dataset.portId || null,
            interface_name_field: document.querySelector('input[name="interface_name_field"]:checked')?.value || null,
            server_key: document.querySelector('input[name="server_key"]')?.value || null
        })
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(`Server error ${response.status}: ${msg}`); });
            }
            return response.json();
        })
        .then(data => {
            // Reuse the row resolved above so the response patches the row the user changed,
            // not the first same-named row on the page.
            if (data.status === 'success' && row) {
                const formattedRow = data.formatted_row;
                row.querySelector('td[data-col="name"]').innerHTML = formattedRow.name;
                row.querySelector('td[data-col="type"]').innerHTML = formattedRow.type;
                row.querySelector('td[data-col="speed"]').innerHTML = formattedRow.speed;
                row.querySelector('td[data-col="mac_address"]').innerHTML = formattedRow.mac_address;
                row.querySelector('td[data-col="mtu"]').innerHTML = formattedRow.mtu;
                row.querySelector('td[data-col="enabled"]').innerHTML = formattedRow.enabled;
                row.querySelector('td[data-col="description"]').innerHTML = formattedRow.description;
                const vlanCell = row.querySelector('td[data-col="vlans"]');
                if (vlanCell && typeof formattedRow.vlans !== 'undefined') {
                    vlanCell.innerHTML = formattedRow.vlans;
                }
                // The LibreNMS ID badge's colour is member-specific (it compares this port_id
                // against the resolved member's stored librenms_id), so refresh it too —
                // otherwise it keeps the previously-selected member's match/mismatch state.
                const librenmsIdCell = row.querySelector('td[data-col="librenms_id"]');
                if (librenmsIdCell && typeof formattedRow.librenms_id !== 'undefined') {
                    librenmsIdCell.innerHTML = formattedRow.librenms_id;
                }
                // Parent/LAG relationship is device-specific, so refresh it too — otherwise
                // it keeps the previously-selected member's status and sync button.
                const parentCell = row.querySelector('td[data-col="parent"]');
                if (parentCell && typeof formattedRow.parent !== 'undefined') {
                    parentCell.innerHTML = formattedRow.parent;
                }
                initializeVlanGroupSelects();
                initializeFilters();
                // This member is now server-confirmed: record it as the rollback target and
                // re-enable the relationship controls (the row HTML now matches this member).
                select._lastVerifiedMember = value;
                reenableRelationshipButtons();
            } else {
                // 2xx with data.status !== 'success' (application-level failure/conflict): the
                // row was NOT repainted, so the verify-locked LAG/parent buttons still carry the
                // previous member's lag/parent port_id. Roll the dropdown back to the last
                // confirmed member (restoring a consistent row) before re-enabling.
                console.error('Interface verification rejected:', data.error || data.message || 'Unknown error');
                rollbackToLastVerified();
            }
        })
        .catch(error => {
            // A superseded request was aborted on purpose — not an error to surface. Leave the
            // buttons disabled: the newer handleInterfaceChange call already re-disabled them and
            // owns re-enabling once its own verify settles.
            if (error.name === 'AbortError') return;
            // A genuine verify failure (HTTP error / network) means the row was NOT repainted for
            // the newly-selected member. Roll the dropdown back to the last confirmed member so
            // the visible selection matches the unchanged row, then re-enable — rather than
            // stranding the user on an unverified selection with locked controls.
            console.error('Error verifying interface:', error.message);
            rollbackToLastVerified();
        });
}

/**
 * Handle VC member selection change for cable verification.
 * Fetches cable connection data and updates table row.
 *
 * @param {HTMLSelectElement} select - The VC member dropdown element
 * @param {string} value - Selected device ID
 */
function handleCableChange(select, value) {
    const csrfToken = getCsrfToken();
    if (!csrfToken) return;  // missing token → abort rather than throw on `.value`

    fetch('/plugins/librenms_plugin/verify-cable/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({
            device_id: value,
            local_port_id: select.dataset.interface,
            server_key: document.querySelector('input[name="server_key"]')?.value || null
        })
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(`Server error ${response.status}: ${msg}`); });
            }
            return response.json();
        })
        .then(data => {
            const row = document.querySelector(`tr[data-interface="${select.dataset.rowId}"]`);

            if (data.status === 'success' && row) {
                const formattedRow = data.formatted_row;
                row.querySelector('td[data-col="local_port"]').innerHTML = formattedRow.local_port;
                row.querySelector('td[data-col="remote_port"]').innerHTML = formattedRow.remote_port;
                row.querySelector('td[data-col="remote_device"]').innerHTML = formattedRow.remote_device;
                row.querySelector('td[data-col="cable_status"]').innerHTML = formattedRow.cable_status;
                row.querySelector('td[data-col="actions"]').innerHTML = formattedRow.actions;
            }
        })
        .catch(error => {
            console.error('Error verifying cable:', error.message);
        });
}

/**
 * Handle VC member selection change for module verification.
 * Fetches recalculated matching status for one module row and updates cells inline.
 *
 * @param {HTMLSelectElement} select - VC member dropdown for a module row
 * @param {string} value - Selected NetBox device ID
 */
function handleModuleChange(select, value) {
    const row = document.querySelector(`tr[data-ent-index="${select.dataset.rowId}"]`);
    const rowDepth = row?.dataset?.depth || 0;

    // Abort any in-flight verify for this select so a slower earlier response
    // can't clobber a faster later one when the user changes the dropdown rapidly.
    if (select._moduleVerifyController) {
        select._moduleVerifyController.abort();
    }
    const controller = new AbortController();
    select._moduleVerifyController = controller;

    const csrfToken = getCsrfToken();
    if (!csrfToken) return;  // missing token → abort rather than throw on `.value`

    fetch('/plugins/librenms_plugin/verify-module/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
            'HX-Current-URL': window.location.href
        },
        body: JSON.stringify({
            device_id: value,
            ent_physical_index: select.dataset.module,
            depth: rowDepth,
            server_key: document.querySelector('input[name="server_key"]')?.value || null
        }),
        signal: controller.signal
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => { throw new Error(`Server error ${response.status}: ${msg}`); });
            }
            return response.json();
        })
        .then(data => {
            if (!row || data.status !== 'success' || !data.formatted_row) return;

            const formattedRow = data.formatted_row;
            const deviceSelCell = row.querySelector('td[data-col="device_selection"]');
            if (deviceSelCell) {
                deviceSelCell.innerHTML = formattedRow.device_selection || '';
            }
            row.querySelector('td[data-col="name"]').innerHTML = formattedRow.name;
            row.querySelector('td[data-col="model"]').innerHTML = formattedRow.model;
            row.querySelector('td[data-col="serial"]').innerHTML = formattedRow.serial;
            row.querySelector('td[data-col="description"]').innerHTML = formattedRow.description;
            row.querySelector('td[data-col="item_class"]').innerHTML = formattedRow.item_class;
            // Replace each cell content if present. Defensive null-checks keep this
            // resilient if the row markup ever drops one of these data-col cells.
            const cellMap = {
                module_bay: formattedRow.module_bay,
                module_type: formattedRow.module_type,
                status: formattedRow.status,
                actions: formattedRow.actions,
            };
            for (const [col, html] of Object.entries(cellMap)) {
                const cell = row.querySelector(`td[data-col="${col}"]`);
                if (cell) {
                    cell.innerHTML = html;
                } else {
                    console.warn(`Module row missing data-col="${col}" cell — skipping update`);
                }
            }

            // Re-bind listeners because row controls (select/buttons/forms) were replaced.
            initializeVCMemberSelect();
            initializeVCReportButtons();
        })
        .catch(error => {
            if (error.name === 'AbortError') return;
            console.error('Error verifying module:', error.message);
        });
}

/**
 * Initialize bulk VC member assignment functionality.
 * Applies selected VC member to all checked interfaces.
 */
function initializeBulkEditApply() {
    const applyButton = document.getElementById('apply-bulk-vc-member');
    if (applyButton) {
        applyButton.addEventListener('click', function () {
            const vcMemberSelectElement = document.getElementById('bulk-vc-member-select');
            if (!vcMemberSelectElement) return;
            const selectedVcMemberId = vcMemberSelectElement.value;

            // Get all selected checkboxes within the interface table
            const interfaceTable = document.getElementById('librenms-interface-table');
            if (!interfaceTable) return;
            const selectedCheckboxes = interfaceTable.querySelectorAll(
                'input[name="select"]:checked:not(:disabled)'
            );

            selectedCheckboxes.forEach(checkbox => {
                const row = checkbox.closest('tr');
                const vcMemberSelect = row.querySelector('.vc-member-select');
                if (vcMemberSelect && vcMemberSelect.tomselect) {
                    vcMemberSelect.tomselect.setValue(selectedVcMemberId);
                    // TomSelect handles the change event internally
                }
            });

            // Close the modal on 'Apply'
            hideModal(document.getElementById('bulkVCMemberModal'));

        });
    }
}

/**
 * Initialize checkbox change listeners for bulk actions.
 * Enables/disables bulk action button based on selection.
 */
function initializeCheckboxListeners() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;
    // Query live so a later row-level swap receives its own bulk-button listener.
    const liveCheckboxes = () => interfaceTable.querySelectorAll('input[name="select"]:not(:disabled)');
    // Idempotent across htmx:afterSwap re-runs — register the change handler once per checkbox.
    liveCheckboxes().forEach(checkbox => {
        if (checkbox.dataset.bulkChangeInitialized === 'true') return;
        checkbox.dataset.bulkChangeInitialized = 'true';
        checkbox.addEventListener('change', updateBulkActionButton);
    });
}

/**
 * Update bulk action button enabled state based on checkbox selection.
 */
function updateBulkActionButton() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;
    const anyChecked = interfaceTable.querySelectorAll('input[name="select"]:checked:not(:disabled)').length > 0;
    const bulkButton = document.getElementById('bulk-vc-member-button');
    if (bulkButton) {
        bulkButton.disabled = !anyChecked;
    }
}

// ============================================
// TABLE FILTERING
// ============================================

/**
 * Initialize column-based filtering for a sync comparison table.
 * Creates filter inputs that hide rows not matching the filter text.
 *
 * @param {string} tableId - DOM element ID of the table
 * @param {string[]} filterKeys - Array of column identifiers to filter
 * @param {Object} dataCols - Configuration mapping column IDs to data attributes or selectors
 */
function initializeTableFilters(tableId, filterKeys, dataCols) {
    const table = document.getElementById(tableId);
    if (!table) return;

    filterKeys.forEach(filterKey => {
        const filterElement = document.getElementById(`filter-${filterKey}`);
        if (filterElement) {
            filterElement.addEventListener('input', () => filterTable(tableId, filterKeys, dataCols));
        }
    });
}

// Generic function to filter a table
function filterTable(tableId, filterKeys, dataCols) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const filters = {};
    filterKeys.forEach(key => {
        filters[key] = document.getElementById(`filter-${key}`)?.value.toLowerCase() || '';
    });

    const rows = table.querySelectorAll('tr[data-interface]');
    rows.forEach(row => {
        const matches = filterKeys.map(key => {
            let cellText = '';
            if (dataCols[key].selector) {
                cellText = row.querySelector(dataCols[key].selector)?.textContent.toLowerCase() || '';
            } else {
                const cell = row.querySelector(`td[data-col="${dataCols[key].name}"]`);
                cellText = (cell.querySelector('span')?.textContent || cell.textContent).toLowerCase();
            }
            return cellText.includes(filters[key]);
        });

        row.style.display = matches.every(Boolean) ? '' : 'none';
    });
}

// Initialize filters for different tables
function initializeFilters() {
    // Interface table
    initializeTableFilters(
        'librenms-interface-table',
        ['name', 'type', 'speed', 'mac', 'mtu', 'enabled', 'description'],
        {
            name: { name: 'name' },
            type: { name: 'type' },
            speed: { name: 'speed' },
            mac: { name: 'mac_address' },
            mtu: { name: 'mtu' },
            enabled: { name: 'enabled' },
            description: { name: 'description' }
        }
    );

    // VM Interface table
    initializeTableFilters(
        'librenms-interface-table-vm',
        ['name', 'mac', 'mtu', 'enabled', 'description'],
        {
            name: { name: 'name' },
            mac: { name: 'mac_address' },
            mtu: { name: 'mtu' },
            enabled: { name: 'enabled' },
            description: { name: 'description' }
        }
    );
    // Non Virtual Chassis Cable table (without 'vc-member' filter)
    initializeTableFilters(
        'librenms-cable-table',
        ['local-port', 'remote-port', 'remote-device'],
        {
            'local-port': { name: 'local_port' },
            'remote-port': { name: 'remote_port' },
            'remote-device': { name: 'remote_device' }
        }
    );
    // VC Cable table (with 'vc-member' filter)
    initializeTableFilters(
        'librenms-cable-table-vc',
        ['vc-member', 'local-port', 'remote-port', 'remote-device'],
        {
            'vc-member': { selector: '.ts-control .item' },
            'local-port': { name: 'local_port' },
            'remote-port': { name: 'remote_port' },
            'remote-device': { name: 'remote_device' }
        }
    );
    initializeTableFilters(
        'librenms-ipaddress-table',
        ['address', 'prefix', 'device', 'interface'],
        {
            address: { name: 'address' },
            prefix: { name: 'prefix' },
            device: { name: 'device' },
            interface: { name: 'interface' }
        }
    );
}

// ============================================
// SNMP CONFIGURATION MODAL
// ============================================

/**
 * Toggle SNMP form visibility based on selected version.
 * Shows either SNMPv1/v2c or SNMPv3 configuration form.
 */
function toggleSNMPForms() {
    const snmpSelect = document.getElementById('snmp-version-select');
    if (!snmpSelect) return;
    const version = snmpSelect.value;

    const v1v2Form = document.getElementById('snmpv1v2-form');
    const v3Form = document.getElementById('snmpv3-form');

    if (!v1v2Form || !v3Form) return;

    if (version === 'v1v2c') {
        v1v2Form.style.display = 'block';
        v3Form.style.display = 'none';
    } else if (version === 'v3') {
        v1v2Form.style.display = 'none';
        v3Form.style.display = 'block';
    }
}

/**
 * Initialize SNMP modal form behavior.
 * Sets up version toggle and displays correct form.
 */
function initializeSNMPModalScripts() {
    const snmpSelect = document.getElementById('snmp-version-select');
    if (snmpSelect) {
        snmpSelect.addEventListener('change', toggleSNMPForms);
        // Initial call to set the correct form visibility
        toggleSNMPForms();
    }
}

// Listen for the modal 'add-device-modal' 'shown.bs.modal' event to initialize scripts
document.addEventListener('DOMContentLoaded', function () {
    const addDeviceModal = document.getElementById('add-device-modal');
    if (addDeviceModal) {
        addDeviceModal.addEventListener('shown.bs.modal', function () {
            initializeSNMPModalScripts();
        });
    }
});

// Function to open the bulk VC modal
function openBulkVCModal() {
    showModal(document.getElementById('bulkVCMemberModal'));
}

// Function to update the interface_name_field radio button
function updateInterfaceNameField() {
    document.querySelectorAll('.interface-name-field').forEach(radio => {
        radio.addEventListener('change', function () {
            const url = new URL(window.location);
            url.searchParams.set('interface_name_field', this.value);
            window.history.pushState({}, '', url);

            // Set HTMX headers for subsequent requests
            if (typeof htmx !== 'undefined') {
                htmx.config.defaultHeaders['X-Interface-Name-Field'] = this.value;
            }

            // Persist to user preferences via API
            const preferenceSelector = this.closest('[data-save-pref-url]');
            const savePrefUrl = preferenceSelector?.dataset.savePrefUrl;
            if (savePrefUrl) {
                const csrfToken = getCsrfToken();
                if (!csrfToken) {
                    console.debug('Failed to save interface_name_field pref: missing CSRF token');
                } else {
                    const platformId = preferenceSelector.dataset.platformId || null;
                    fetch(savePrefUrl, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                        body: JSON.stringify({
                            key: 'interface_name_field',
                            value: this.value,
                            platform_id: platformId
                        })
                    }).then(response => {
                        if (!response.ok) {
                            throw new Error(`HTTP ${response.status}`);
                        }
                    }).catch(error => console.debug('Failed to save interface_name_field pref:', error.message));
                }
            }

            // Refresh current tab content
            const activeTab = document.querySelector('.tab-pane.active');
            if (activeTab && typeof htmx !== 'undefined') {
                htmx.trigger(activeTab, 'htmx:refresh');
            }
        });
    });
}
// Function to set the interface_name_field from the URL
function setInterfaceNameFieldFromURL() {
    const urlParams = new URLSearchParams(window.location.search);
    const interfaceNameField = urlParams.get('interface_name_field');
    if (interfaceNameField) {
        if (!['ifDescr', 'ifName'].includes(interfaceNameField)) return;
        const radio = document.querySelector(`input[name="interface_name_field"][value="${interfaceNameField}"]`);
        if (radio) {
            radio.checked = true;
        }
    }
}


// NetBox-only interfaces functionality
function initializeNetBoxOnlyInterfaces() {
    // Select all checkbox functionality
    const selectAllCheckbox = document.getElementById('select-all-netbox-interfaces');
    const interfaceCheckboxes = document.querySelectorAll('.netbox-interface-checkbox');

    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', function () {
            interfaceCheckboxes.forEach(checkbox => {
                checkbox.checked = this.checked;
            });
        });
    }

    // Update select all checkbox when individual checkboxes change
    interfaceCheckboxes.forEach(checkbox => {
        checkbox.addEventListener('change', function () {
            const checkedCount = document.querySelectorAll('.netbox-interface-checkbox:checked').length;
            const totalCount = interfaceCheckboxes.length;

            if (selectAllCheckbox) {
                selectAllCheckbox.checked = checkedCount === totalCount;
                selectAllCheckbox.indeterminate = checkedCount > 0 && checkedCount < totalCount;
            }
        });
    });

    // Delete interfaces functionality
    const deleteButton = document.getElementById('confirm-delete-interfaces');

    if (deleteButton) {
        deleteButton.addEventListener('click', function () {
            const selectedCheckboxes = document.querySelectorAll('.netbox-interface-checkbox:checked');

            if (selectedCheckboxes.length === 0) {
                return;
            }

            deleteSelectedInterfaces(selectedCheckboxes);
        });
    }
}

/**
 * Delete selected NetBox-only interfaces.
 * Sends bulk delete request and handles modal display.
 *
 * @param {NodeList} selectedCheckboxes - Checked interface checkboxes to delete
 */
function deleteSelectedInterfaces(selectedCheckboxes) {
    const interfaceIds = Array.from(selectedCheckboxes).map(cb => cb.value);

    const formData = new FormData();

    // Add CSRF token
    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value;

    if (!csrfToken) {
        alert('CSRF token not found. Please refresh the page and try again.');
        return;
    }

    formData.append('csrfmiddlewaretoken', csrfToken);
    const serverKey = document.querySelector('[name="server_key"]')?.value;
    if (serverKey) formData.append('server_key', serverKey);

    // Add interface IDs
    interfaceIds.forEach(id => {
        formData.append('interface_ids', id);
    });

    // Extract object type and ID from URL
    const deviceInfo = getDeviceIdFromUrl();
    if (!deviceInfo) {
        alert('Unable to determine object type. Please refresh and try again.');
        return;
    }
    const objectType = deviceInfo.type;
    const objectId = deviceInfo.id;

    const deleteUrl = `/plugins/librenms_plugin/${objectType}/${objectId}/delete-netbox-interfaces/`;

    // Show loading state
    const deleteButton = document.getElementById('confirm-delete-interfaces');
    const originalText = deleteButton.innerHTML;
    deleteButton.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Deleting...';
    deleteButton.disabled = true;

    fetch(deleteUrl, {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
        .then(response => {
            if (!response.ok) {
                return fetchErrorMessage(response).then(msg => {
                    throw new Error(`HTTP ${response.status} ${response.statusText}: ${msg}`);
                });
            }
            const transition = response.headers.get('X-LibreNMS-Cache-Transition');
            if (transition) {
                try {
                    document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: JSON.parse(transition) }));
                } catch (_) {
                    document.dispatchEvent(new CustomEvent('librenmsCacheChanged'));
                }
            }
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                hideModal(document.getElementById('netboxOnlyInterfacesModal'));
            } else {
                const message = data.error || 'Unknown error occurred';
                console.error('Error deleting interfaces:', message);
                showErrorToast(message);
            }
        })
        .catch(error => {
            console.error('Error deleting interfaces:', error);
            showErrorToast('Error deleting interfaces: ' + error.message);
        })
        .finally(() => {
            // Restore button state
            deleteButton.innerHTML = originalText;
            deleteButton.disabled = false;
        });
}

// ============================================
// SYNC BUTTON SPINNERS
// ============================================

/**
 * Initialize spinners on sync form submit buttons.
 * Shows the spinner and disables the button when a sync form is submitted.
 * Also adds loading indicators to HTMX refresh buttons.
 */
function initializeSyncFormSpinners() {
    // Handle regular form submit buttons with sync-spinner inside
    document.querySelectorAll('.spinner.spinner-border.d-none').forEach(function (spinner) {
        const form = spinner.closest('form');
        const button = spinner.closest('button');
        if (!form || !button || form.dataset.spinnerInitialized) return;

        form.dataset.spinnerInitialized = 'true';
        form.addEventListener('submit', function () {
            spinner.classList.remove('d-none');
            spinner.style.width = '1rem';
            spinner.style.height = '1rem';
            button.disabled = true;
        });
    });

    // Handle HTMX refresh buttons (btn-outline-primary with hx-post)
    document.querySelectorAll('button[hx-post].btn-outline-primary').forEach(function (button) {
        if (button.dataset.spinnerInitialized) return;
        button.dataset.spinnerInitialized = 'true';

        button.addEventListener('htmx:beforeRequest', function () {
            button.dataset.originalHtml = button.innerHTML;
            button.disabled = true;
            const label = button.textContent.trim();
            const spinner = document.createElement('span');
            spinner.className = 'spinner-border spinner-border-sm me-2';
            button.textContent = label;
            button.insertBefore(spinner, button.firstChild);
        });

        button.addEventListener('htmx:afterRequest', function () {
            button.disabled = false;
            button.innerHTML = button.dataset.originalHtml;
        });
    });
}


/**
 * Wire the "Install Selected" form to collect checked module-table rows before submit.
 * The form is separate from the table (to avoid nested forms), so we copy the
 * selected checkbox values into hidden inputs just before the form is submitted.
 * Guard against duplicate listeners on repeated HTMX swaps via a data attribute.
 *
 * NOTE: this submit-phase injection only reliably covers the NATIVE (no-htmx) submit
 * fallback. When htmx drives the POST, its own submit listener can be registered on the
 * form BEFORE this one (fresh page load: htmx's DOMContentLoaded processNode runs before
 * initializeScripts), so it serializes the form first and these hidden inputs arrive too
 * late. The htmx path is therefore injected at htmx:configRequest (see the
 * DOMContentLoaded handler), which fires after serialization and replaces any
 * select/device_selection values this handler managed to add.
 */
function handleInstallSelectedSubmit() {
    // Remove any previously-injected hidden inputs to avoid duplicates
    const form = document.getElementById('install-selected-form');
    if (!form) return;
    form.querySelectorAll('input[data-injected-select]').forEach(el => { el.remove(); });

    const table = document.getElementById('librenms-module-table');
    if (!table) return;

    table.querySelectorAll('input[name="select"]:checked').forEach(cb => {
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'select';
        hidden.value = cb.value;
        hidden.dataset.injectedSelect = '1';
        form.appendChild(hidden);

        const selectedDevice = table.querySelector(`#device_selection_${cb.value}`);
        if (selectedDevice) {
            const hiddenDevice = document.createElement('input');
            hiddenDevice.type = 'hidden';
            hiddenDevice.name = `device_selection_${cb.value}`;
            hiddenDevice.value = selectedDevice.value;
            hiddenDevice.dataset.injectedSelect = '1';
            form.appendChild(hiddenDevice);
        }
    });
}

function initializeInstallSelectedForm() {
    const form = document.getElementById('install-selected-form');
    if (!form) return;
    if (form.dataset.installInit) return;
    form.dataset.installInit = 'true';
    form.addEventListener('submit', handleInstallSelectedSubmit);
}

/**
 * Tracks the in-flight AbortController for the VC report fetch.
 * Cancelled when a new VC report button is clicked before the previous fetch completes,
 * so spam-clicks don't race.
 */
let _activeVCReportController = null;

/**
 * Wire the copy-to-clipboard button inside the VC report modal body.
 * Uses navigator.clipboard.writeText when available; falls back to the
 * textarea+execCommand path for older browsers / non-HTTPS contexts.
 * Called once after the fragment is injected into the modal.
 */
function initializeVCReportCopyButton() {
    const btn = document.getElementById('vc-report-copy-btn');
    if (!btn || btn.dataset.copyInitialized) return;
    btn.dataset.copyInitialized = 'true';

    const targetId = btn.dataset.target || 'vc-report-textarea';
    const idleHtml = '<i class="mdi mdi-content-copy me-1"></i>Copy to clipboard';
    const doneHtml = '<i class="mdi mdi-check me-1"></i>Copied';
    const errHtml = '<i class="mdi mdi-alert me-1"></i>Copy failed';

    const flash = (html, ms = 1500) => {
        btn.innerHTML = html;
        setTimeout(() => { btn.innerHTML = idleHtml; }, ms);
    };

    btn.addEventListener('click', () => {
        const ta = document.getElementById(targetId);
        if (!ta) return;
        const text = ta.value;
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            navigator.clipboard.writeText(text)
                .then(() => flash(doneHtml))
                .catch(() => flash(errHtml));
            return;
        }
        // Fallback for older browsers or insecure contexts.
        try {
            ta.select();
            const ok = document.execCommand('copy');
            flash(ok ? doneHtml : errHtml);
        } catch (e) {
            flash(errHtml);
        }
    });
}

/**
 * Initialize "Report VC issue" buttons on the module sync table.
 * Each button carries module/server/device data; click fetches a diagnostic
 * fragment that's swapped into the shared htmx-modal for copy-paste filing.
 */
function initializeVCReportButtons() {
    document.querySelectorAll('.vc-report-btn').forEach(btn => {
        if (btn.dataset.vcReportInitialized) return;
        btn.dataset.vcReportInitialized = 'true';

        btn.addEventListener('click', function () {
            // Cancel any in-flight VC report fetch before starting a new one
            if (_activeVCReportController) {
                _activeVCReportController.abort();
            }
            _activeVCReportController = new AbortController();
            const signal = _activeVCReportController.signal;

            const reportUrl = this.dataset.reportUrl;
            const moduleId = this.dataset.moduleId;
            const selectedDeviceId = this.dataset.selectedDeviceId;

            const params = new URLSearchParams({
                module_id: moduleId,
                selected_device_id: selectedDeviceId,
            });

            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) {
                modalContent.innerHTML =
                    '<div class="modal-header">' +
                    '<h5 id="htmx-modal-label" class="modal-title"><i class="mdi mdi-bug-outline me-1"></i>Report VC normalization issue</h5>' +
                    '<button type="button" class="btn-close" onclick="closeHtmxModal()" aria-label="Close"></button>' +
                    '</div>' +
                    '<div class="modal-body text-center py-3" id="htmx-modal-body">' +
                    '<i class="mdi mdi-loading mdi-spin mdi-36px"></i>' +
                    '<p class="mt-2">Loading…</p>' +
                    '</div>';
            }

            showModal(document.getElementById('htmx-modal'));

            const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value;
            const fetchHeaders = {};
            if (csrfToken) {
                fetchHeaders['X-CSRFToken'] = csrfToken;
            }
            fetch(`${reportUrl}?${params.toString()}`, { signal, headers: fetchHeaders })
                .then(response => {
                    if (!response.ok) return fetchErrorMessage(response).then(msg => { throw new Error(msg); });
                    return response.text();
                })
                .then(html => {
                    if (modalContent) {
                        modalContent.innerHTML = html;
                        updateHtmxModalLabel();
                        initializeVCReportCopyButton();
                    }
                })
                .catch(err => {
                    if (err.name === 'AbortError') return; // Superseded by a newer click — ignore
                    const modalBody = document.getElementById('htmx-modal-body');
                    if (modalBody) {
                        const alert = document.createElement('div');
                        alert.className = 'alert alert-danger';
                        const icon = document.createElement('i');
                        icon.className = 'mdi mdi-alert me-1';
                        alert.appendChild(icon);
                        alert.appendChild(document.createTextNode(err.message || 'Failed to load report.'));
                        modalBody.textContent = '';
                        modalBody.appendChild(alert);
                    }
                });
        });
    });
}

function closeHtmxModal() {
    // Abort any in-flight VC report fetch
    if (typeof _activeVCReportController !== 'undefined' && _activeVCReportController) {
        _activeVCReportController.abort();
        _activeVCReportController = null;
    }
    hideModal(document.getElementById('htmx-modal'));
}

// A module action posted from the mismatch modal answers HX-Trigger: closeModal, because the
// module tab fragment is swapped outside the modal and leaves it open.
document.body.addEventListener('closeModal', closeHtmxModal);

// ============================================
// INITIALIZATION
// ============================================

/**
 * Initialize all sync page functionality.
 * Called on DOMContentLoaded and after HTMX content swaps.
 */

function initializeScripts() {
    initializeCheckboxes();
    initializeVCMemberSelect();
    initializeVRFSelects();
    initializeVlanGroupSelects();
    initializeVlanModalSave();
    initializeFilters();
    initializeCountdowns();
    initializeCheckboxListeners();
    initializeBulkEditApply();
    updateInterfaceNameField();
    setInterfaceNameFieldFromURL();
    initializeNetBoxOnlyInterfaces();
    initializeSyncFormSpinners();
    initializeVlanSyncGroupSelects();
    initializeInstallSelectedForm();
    initializeVCReportButtons();
    initializeSyncCacheConsistency();
}


// Initialize scripts on initial DOM load
document.addEventListener('DOMContentLoaded', function () {
    initializeScripts();

    // Configure HTMX to include CSRF token in all requests
    document.body.addEventListener('htmx:configRequest', function (event) {
        const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]');
        if (csrfToken) {
            event.detail.headers['X-CSRFToken'] = csrfToken.value;
        }
        // Install Selected: the checked rows live in the table OUTSIDE the form, and htmx's
        // own submit listener (attached to the form at ITS DOMContentLoaded processNode,
        // which on a fresh page load runs before initializeScripts registers the
        // submit-phase injector on the same element — listener ORDER, not event phase,
        // decides) serializes the form BEFORE the hidden inputs are injected. The first
        // click after a full page load then POSTs no 'select' values and the view warns
        // "No modules selected." while wiping the selection. configRequest fires AFTER
        // htmx serialization, exactly to let listeners amend the outgoing parameters, so
        // injecting here is ordering-independent. Replace (not append to) any
        // select/device_selection values the submit-phase injector already serialized so
        // rows are never posted twice.
        if (event.detail.elt && event.detail.elt.id === 'install-selected-form') {
            const params = event.detail.parameters;
            Array.from(params.keys())
                .filter((k) => k === 'select' || k.startsWith('device_selection_'))
                .forEach((k) => params.delete(k));
            const table = document.getElementById('librenms-module-table');
            if (table) {
                table.querySelectorAll('input[name="select"]:checked').forEach((cb) => {
                    params.append('select', cb.value);
                    const selectedDevice = table.querySelector(`#device_selection_${cb.value}`);
                    if (selectedDevice) {
                        params.append(`device_selection_${cb.value}`, selectedDevice.value);
                    }
                });
                // Modules the user selected on another page of this table: the rows are gone
                // from the DOM, so their target device travels with them out of the store.
                const stored = readStoredSelection(table);
                offPageSelectionKeys(table).forEach((rowKey) => {
                    params.append('select', rowKey);
                    const companions = (stored[rowKey] && stored[rowKey].inputs) || {};
                    Object.keys(companions).forEach((name) => params.append(name, companions[name]));
                });
                clearStoredSelection(table);
            }
        }
    });
});

// Initialize scripts after HTMX swaps content
document.body.addEventListener('htmx:afterSwap', function (event) {
    const controller = syncCacheController();
    if (controller && event.target.id === 'librenms-sync-tabs') {
        controller.statusGeneration += 1;
        const renderedStatus = renderedSyncCacheStatus();
        if (renderedStatus) {
            controller.status = renderedStatus;
            controller.invalidatedLocally.clear();
            controller.requiredSourceFragments.clear();
            controller.pendingRestores.clear();
        }
    }
    const swappedTab = syncCacheTabForContent(event.target);
    if (swappedTab) {
        controller.invalidatedLocally.delete(swappedTab);
        controller.failClosedTabs.delete(swappedTab);
        controller.pendingRestores.delete(swappedTab);
        delete event.target.dataset.cacheEmpty;
        // A fragment load is the outcome of a status check, so its swap must not start another one.
        if (syncCacheFragmentGeneration(event) === null) checkSyncCacheStatus();
    }
    initializeScripts();
});

document.body.addEventListener('htmx:afterRequest', function (event) {
    const controller = syncCacheController();
    const requestedTab = syncCacheTabForContent(event.detail?.target);
    if (
        controller &&
        requestedTab &&
        syncCacheFragmentGeneration(event) === null &&
        controller.pendingRestores.has(requestedTab)
    ) checkSyncCacheStatus();
});

// Update HTMX modal accessible label after content loads so screen readers
// announce the actual dialog title rather than the static "Loading" placeholder.
function updateHtmxModalLabel() {
    const htmxModal = document.getElementById('htmx-modal');
    if (!htmxModal) return;
    const modalBody = htmxModal.querySelector('#htmx-modal-body') || htmxModal;
    const header = modalBody.querySelector('.modal-title, .modal-header h5, .modal-header h4');
    const labelId = htmxModal.getAttribute('aria-labelledby');
    const label = (labelId && document.getElementById(labelId)) || document.getElementById('htmx-modal-label');
    if (header && label && header !== label) {
        label.textContent = header.textContent.trim();
    }
}

// Listen at document level so the handler fires regardless of which element
// HTMX dispatches afterSettle on (swap target or ancestor).
document.addEventListener('htmx:afterSettle', function (event) {
    const htmxModal = document.getElementById('htmx-modal');
    if (htmxModal && (htmxModal === event.target || htmxModal.contains(event.target))) {
        updateHtmxModalLabel();
        // Auto-show the shared HTMX modal whenever new content is swapped into
        // it (e.g. the Add Bay Template flow). Buttons that target
        // #htmx-modal-content via hx-get no longer need to wire their own
        // bootstrap.Modal.show() call.
        if (!htmxModal.classList.contains('show')) {
            showModal(htmxModal);
        }
    }
});

/**
 * Extract a human-readable error message from a failed HTMX request, like fetchErrorMessage does.
 * @param {XMLHttpRequest} xhr
 * @returns {string}
 */
function xhrErrorMessage(xhr) {
    const ct = (xhr.getResponseHeader('Content-Type') || '').toLowerCase();
    let msg = xhr.responseText || `HTTP ${xhr.status}`;
    if (ct.includes('application/json')) {
        try { const d = JSON.parse(xhr.responseText); msg = d.error || d.message || d.detail || msg; } catch (_) {}
    }
    if (msg.length > 300) msg = msg.slice(0, 300) + '...';
    return msg;
}

// HTMX never swaps a 4xx/5xx answer, so a failed modal fetch would leave the modal closed and silent.
function showHtmxModalError(message) {
    const modalContent = document.getElementById('htmx-modal-content');

    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('h5');
    title.className = 'modal-title';
    title.textContent = 'Request failed';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', closeHtmxModal);
    header.appendChild(title);
    header.appendChild(close);

    const body = document.createElement('div');
    body.className = 'modal-body';
    const alert = document.createElement('div');
    alert.className = 'alert alert-danger';
    // Announce the failure: the modal opens without focus moving into it, so assistive
    // technology would otherwise report nothing at all.
    alert.setAttribute('role', 'alert');
    const icon = document.createElement('i');
    icon.className = 'mdi mdi-alert me-1';
    alert.appendChild(icon);
    alert.appendChild(document.createTextNode(message));
    body.appendChild(alert);

    modalContent.textContent = '';
    modalContent.appendChild(header);
    modalContent.appendChild(body);
    // #htmx-modal is labelled by #htmx-modal-label, which lives outside the replaced content.
    // Copy this header into it or the dialog loses its accessible name after an error swap.
    updateHtmxModalLabel();
    showModal(document.getElementById('htmx-modal'));
}

function htmxModalErrorTarget(event) {
    const modalContent = document.getElementById('htmx-modal-content');
    return modalContent && event.detail.target === modalContent ? modalContent : null;
}

document.body.addEventListener('htmx:responseError', function (event) {
    if (!htmxModalErrorTarget(event)) return;
    showHtmxModalError(xhrErrorMessage(event.detail.xhr));
});

// A request that never reaches the server (DNS, TLS, a redirect loop, a dropped connection) raises
// sendError or timeout instead of responseError. Without these the click leaves no modal and no
// message, so the button reads as dead.
document.body.addEventListener('htmx:sendError', function (event) {
    if (!htmxModalErrorTarget(event)) return;
    showHtmxModalError('Could not reach the server. The request failed before any response arrived.');
});

document.body.addEventListener('htmx:timeout', function (event) {
    if (!htmxModalErrorTarget(event)) return;
    showHtmxModalError('The request timed out before the server answered.');
});

// Event delegation for LAG and parent interface sync buttons.
// Buttons are rendered inline in the interface table's data-col="parent" cell
// (render_parent in tables/interfaces.py renders BOTH the LAG-sync and parent-sync
// buttons there; there is no separate data-col="lag" cell)
// and carry data attributes: port-id, lag-port-id / parent-port-id, object-type, object-id.
document.addEventListener('click', function (e) {
    const btn = e.target.closest('.lag-sync-btn, .parent-sync-btn');
    if (!btn) return;
    e.preventDefault();

    const isLag = btn.classList.contains('lag-sync-btn');
    const portId = btn.dataset.portId || '';
    const relatedPortId = isLag ? (btn.dataset.lagPortId || '') : (btn.dataset.parentPortId || '');
    const url = btn.dataset.syncUrl || '';
    // On a Virtual Chassis page the row carries a member-select dropdown; prefer the user's
    // live selection over the server-rendered data-object-id (a name-based heuristic), so the
    // sync POST lands on the member the user actually chose instead of the default member.
    const row = btn.closest('tr');
    const vcMemberSelect = row ? row.querySelector('.vc-member-select') : null;
    // When the row has a member-select, its value is authoritative even when blank: an empty
    // selection is a deliberate "no member chosen" state, so it must NOT fall back to the
    // server-rendered default member (data-object-id) — that would post the sync to the previous
    // member. Only fall back to data-object-id when there is no select at all (non-VC page).
    const objectId =
        vcMemberSelect ? vcMemberSelect.value : (btn.dataset.objectId || '');
    const relatedKey = isLag ? 'lag_port_id' : 'parent_port_id';

    if (!portId || !relatedPortId || !objectId || !url) {
        btn.innerHTML = '<i class="mdi mdi-alert text-danger"></i>';
        // A blank objectId on a VC page means no member is selected. Give that case a specific
        // instruction, and surface malformed relationship metadata with a general explanation.
        btn.title = vcMemberSelect && !objectId
            ? 'Select a VC member first.'
            : 'Required relationship data is unavailable.';
        return;
    }

    // Fail fast: a missing input OR an empty value both POST X-CSRFToken: "" → a guaranteed
    // 403. Surface the cause instead of firing a state-changing request that can't succeed.
    const csrf = getCsrfToken();
    if (!csrf) {
        btn.title = 'CSRF token not found. Please refresh the page and try again.';
        return;
    }

    const serverKeyInput = document.querySelector('[name=server_key]');
    const serverKey = serverKeyInput ? serverKeyInput.value : '';

    const body = new URLSearchParams({
        csrfmiddlewaretoken: csrf,
        port_id: portId,
        [relatedKey]: relatedPortId,
        interface_name_field: document.querySelector('input[name="interface_name_field"]:checked')?.value || '',
        server_key: serverKey,
    });

    btn.disabled = true;
    btn.innerHTML = '<i class="mdi mdi-loading mdi-spin"></i>';

    fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
    })
        .then(function (r) {
            if (!r.ok) {
                // Surface the backend error text (403/500/HTML page) instead of a
                // generic JSON parse failure.
                return fetchErrorMessage(r).then(function (msg) {
                    throw new Error(msg);
                });
            }
            const transition = r.headers.get('X-LibreNMS-Cache-Transition');
            if (transition) {
                try {
                    document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: JSON.parse(transition) }));
                } catch (_) {
                    document.dispatchEvent(new CustomEvent('librenmsCacheChanged'));
                }
            }
            return r.json();
        })
        .then(function (data) {
            if (data.status === 'success') {
                btn.innerHTML = '<i class="mdi mdi-check text-success"></i>';
                btn.title = data.message || 'Synced';
            } else {
                btn.disabled = false;
                btn.innerHTML = '<i class="mdi mdi-alert text-danger"></i>';
                btn.title = data.error || 'Sync failed';
            }
        })
        .catch(function (e) {
            btn.disabled = false;
            btn.innerHTML = '<i class="mdi mdi-alert text-danger"></i>';
            btn.title = e.message || 'Request failed';
        });
});
