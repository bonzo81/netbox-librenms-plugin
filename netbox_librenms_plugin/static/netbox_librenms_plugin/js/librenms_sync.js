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

    const toggleAll = table.querySelector('th input.toggle');
    const checkboxes = table.querySelectorAll('td input[name="select"]');
    let lastChecked = null;

    if (toggleAll) {
        toggleAll.addEventListener('change', function () {
            checkboxes.forEach(checkbox => {
                checkbox.checked = toggleAll.checked;
            });
        });
    }

    checkboxes.forEach(checkbox => {
        checkbox.addEventListener('click', function (e) {
            if (!lastChecked) {
                lastChecked = checkbox;
                return;
            }

            if (e.shiftKey) {
                const start = Array.from(checkboxes).indexOf(checkbox);
                const end = Array.from(checkboxes).indexOf(lastChecked);
                Array.from(checkboxes).slice(Math.min(start, end), Math.max(start, end) + 1).forEach(cb => {
                    cb.checked = lastChecked.checked;
                });
            }

            lastChecked = checkbox;
        });
    });
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

        if (interfaceTable) {
            // Only target VC member selects, exclude VLAN group selects
            const interfaceSelects = interfaceTable.querySelectorAll('.form-select.tomselected:not(.vlan-group-select)');
            interfaceSelects.forEach(select => {
                if (select.tomselect && !select.dataset.interfaceSelectInitialized) {
                    select.dataset.interfaceSelectInitialized = 'true';
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
    const safeName = btn.dataset.safeName;
    const deviceId = btn.dataset.deviceId;
    const vlans = JSON.parse(btn.dataset.vlans);
    const vlanGroups = JSON.parse(btn.dataset.vlanGroups);

    // Set modal title
    document.getElementById('vlanModalInterfaceName').textContent = interfaceName;

    // Store current interface context on modal for save handler
    const modal = document.getElementById('vlanDetailModal');
    modal.dataset.currentInterface = interfaceName;
    modal.dataset.currentSafeName = safeName;
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
            select.dataset.safeName = safeName;

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
                updateHiddenVlanGroupInput(safeName, vlan.vid, this.value);

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
 * @param {string} safeName - Safe interface name (slashes replaced)
 * @param {number} vid - VLAN ID
 * @param {string} groupId - Selected group ID
 */
function updateHiddenVlanGroupInput(safeName, vid, groupId) {
    const input = document.querySelector(
        `input.vlan-group-hidden[name="vlan_group_${safeName}_${vid}"]`
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

    // Capture safeName before the async fetch to avoid stale closure if the modal
    // is opened for a different interface while this request is in flight.
    const modal = document.getElementById('vlanDetailModal');
    const capturedSafeName = modal?.dataset.currentSafeName;

    fetch('/plugins/librenms_plugin/verify-vlan-group/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
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
                if (capturedSafeName) {
                    const btn = document.querySelector(`.vlan-edit-btn[data-safe-name="${capturedSafeName}"]`);
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
        const currentSafeName = modalEl.dataset.currentSafeName;

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
            : document.querySelectorAll(`.vlan-edit-btn[data-safe-name="${currentSafeName}"]`);

        // Apply DOM mutations (btn.dataset.vlans, hidden inputs, summary spans)
        // Called only after a successful server response when persisting, or immediately otherwise.
        function applyButtonUpdates() {
            buttonsToUpdate.forEach(btn => {
                try {
                    const btnVlans = JSON.parse(btn.dataset.vlans);
                    const groups = JSON.parse(btn.dataset.vlanGroups);
                    const btnSafeName = btn.dataset.safeName;
                    let changed = false;

                    btnVlans.forEach(v => {
                        if (vidGroupMap.hasOwnProperty(String(v.vid))) {
                            const newGroupId = vidGroupMap[String(v.vid)];
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
                                const matchedGroup = groups.find(g => String(g.id) === String(newGroupId));
                                v.group_name = matchedGroup ? matchedGroup.name : '-- No Group (Global) --';
                            }

                            changed = true;

                            // Update the hidden input for this VID on this interface
                            const input = document.querySelector(
                                `input.vlan-group-hidden[name="vlan_group_${btnSafeName}_${v.vid}"]`
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
            fetch('/plugins/librenms_plugin/save-vlan-group-overrides/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
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

    fetch('/plugins/librenms_plugin/verify-ipaddress/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
        },
        body: JSON.stringify({
            device_id: deviceId,
            ip_address: fullIpAddress,  // Use full IP address with prefix
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
            const row = document.querySelector(`tr[data-interface="${select.dataset.rowId}"]`);

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
    fetch('/plugins/librenms_plugin/verify-interface/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
        },
        body: JSON.stringify({
            device_id: value,
            interface_name: select.dataset.interface,
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
            const row = document.querySelector(`tr[data-interface="${select.dataset.rowId}"]`);
            if (data.status === 'success' && row) {
                const formattedRow = data.formatted_row;
                row.querySelector('td[data-col="name"]').innerHTML = formattedRow.name;
                row.querySelector('td[data-col="type"]').innerHTML = formattedRow.type;
                row.querySelector('td[data-col="speed"]').innerHTML = formattedRow.speed;
                row.querySelector('td[data-col="mac_address"]').innerHTML = formattedRow.mac_address;
                row.querySelector('td[data-col="mtu"]').innerHTML = formattedRow.mtu;
                row.querySelector('td[data-col="enabled"]').innerHTML = formattedRow.enabled;
                row.querySelector('td[data-col="description"]').innerHTML = formattedRow.description;
                initializeFilters();
            }
        })
        .catch(error => {
            console.error('Error verifying interface:', error.message);
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
    fetch('/plugins/librenms_plugin/verify-cable/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
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
            const selectedCheckboxes = interfaceTable.querySelectorAll('input[name="select"]:checked');

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
    const checkboxes = interfaceTable.querySelectorAll('input[name="select"]');
    checkboxes.forEach(checkbox => {
        checkbox.addEventListener('change', updateBulkActionButton);
    });

    const toggleAll = interfaceTable.querySelector('input.toggle');
    if (toggleAll) {
        toggleAll.addEventListener('change', function () {
            checkboxes.forEach(checkbox => {
                checkbox.checked = toggleAll.checked;
            });
            updateBulkActionButton();
        });
    }
}

/**
 * Update bulk action button enabled state based on checkbox selection.
 */
function updateBulkActionButton() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;
    const anyChecked = interfaceTable.querySelectorAll('input[name="select"]:checked').length > 0;
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
// TAB NAVIGATION
// ============================================

/**
 * Initialize tab navigation with URL parameter synchronization.
 * Activates correct tab based on URL and updates URL when tabs change.
 */
function initializeTabs() {
    const urlParams = new URLSearchParams(window.location.search);
    const activeTab = urlParams.get('tab') || 'interfaces'; // Set default tab
    const interfaceNameField = urlParams.get('interface_name_field');

    // Activate the tab based on the 'tab' parameter in the URL
    if (activeTab) {
        const tabElement = document.querySelector(`#${activeTab}-tab`);
        const tabContent = document.querySelector(`#${activeTab}`);

        if (tabElement && tabContent) {
            tabContent.classList.add('show', 'active');
            tabElement.classList.add('active');
        }
    }

    // Add event listeners to update URL when tabs are clicked
    const tabs = document.querySelectorAll('[data-bs-toggle="tab"]')
    tabs.forEach(tab => {
        tab.addEventListener('shown.bs.tab', function (e) {
            const tabId = this.getAttribute('aria-controls');
            const url = new URL(window.location);

            // Update the 'tab' parameter in the URL
            url.searchParams.set('tab', tabId);

            // Preserve 'interface_name_field' parameter if it exists
            if (interfaceNameField) {
                url.searchParams.set('interface_name_field', interfaceNameField);
            }

            // Update the browser history without reloading the page
            window.history.replaceState({}, '', url);
        });
    });
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
            const savePrefUrl = this.closest('[data-save-pref-url]')?.dataset.savePrefUrl;
            if (savePrefUrl) {
                const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || getCookie('csrftoken');
                if (csrfToken) {
                    fetch(savePrefUrl, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                        body: JSON.stringify({key: 'interface_name_field', value: this.value})
                    }).catch(err => console.debug('Failed to save interface_name_field pref:', err));
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
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                hideModal(document.getElementById('netboxOnlyInterfacesModal'));

                // Refresh the interface data by triggering the refresh button
                const refreshButton = document.querySelector('[hx-post*="interface-sync"]');
                if (refreshButton) {
                    refreshButton.click();
                } else {
                    // Fallback: reload the page
                    window.location.reload();
                }
            } else {
                alert('Error: ' + (data.error || 'Unknown error occurred'));
            }
        })
        .catch(error => {
            alert('Error deleting interfaces: ' + error.message);
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
            const originalText = button.textContent.trim();
            button.dataset.originalText = originalText;
            button.disabled = true;
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>' + originalText;
        });

        button.addEventListener('htmx:afterRequest', function () {
            button.disabled = false;
            button.innerHTML = button.dataset.originalText || button.textContent;
        });
    });
}


/**
 * Wire the "Install Selected" form to collect checked module-table rows before submit.
 * The form is separate from the table (to avoid nested forms), so we copy the
 * selected checkbox values into hidden inputs just before the form is submitted.
 * Guard against duplicate listeners on repeated HTMX swaps via a data attribute.
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
 * Tracks the in-flight AbortController for the module replace preview fetch.
 * Cancelled when a new Replace button is clicked before the previous fetch completes.
 */
let _activeReplaceController = null;

/**
 * Initialize Replace buttons on the module sync table.
 * Each button carries module/ent_index/server_key as data attributes and opens
 * the mismatch comparison modal by fetching the preview fragment from the server.
 */
function initializeModuleReplaceButtons() {
    document.querySelectorAll('.module-replace-btn').forEach(btn => {
        if (btn.dataset.replaceInitialized) return;
        btn.dataset.replaceInitialized = 'true';

        btn.addEventListener('click', function () {
            // Cancel any in-flight preview request before starting a new one
            if (_activeReplaceController) {
                _activeReplaceController.abort();
            }
            _activeReplaceController = new AbortController();
            const signal = _activeReplaceController.signal;

            const previewUrl = this.dataset.previewUrl;
            const moduleId = this.dataset.moduleId;
            const entIndex = this.dataset.entIndex;
            const serverKey = this.dataset.serverKey;

            const params = new URLSearchParams({
                module_id: moduleId,
                ent_index: entIndex,
                server_key: serverKey,
            });

            // Show shared HTMX modal with loading state
            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) {
                modalContent.innerHTML =
                    '<div class="modal-header">' +
                    '<h5 id="htmx-modal-label" class="modal-title"><i class="mdi mdi-swap-horizontal me-1"></i>Module Mismatch</h5>' +
                    '<button type="button" class="btn-close" onclick="closeHtmxModal()" aria-label="Close"></button>' +
                    '</div>' +
                    '<div class="modal-body text-center py-3" id="htmx-modal-body">' +
                    '<i class="mdi mdi-loading mdi-spin mdi-36px"></i>' +
                    '<p class="mt-2">Loading\u2026</p>' +
                    '</div>';
            }

            showModal(document.getElementById('htmx-modal'));

            // Fetch preview content and inject into modal body
            const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || getCookie('csrftoken');
            const fetchHeaders = {};
            if (csrfToken) {
                fetchHeaders['X-CSRFToken'] = csrfToken;
            }
            fetch(`${previewUrl}?${params.toString()}`, {
                signal,
                headers: fetchHeaders,
            })
                .then(response => {
                    if (!response.ok) return fetchErrorMessage(response).then(msg => { throw new Error(msg); });
                    return response.text();
                })
                .then(html => {
                    const modalBody = document.getElementById('htmx-modal-body');
                    if (modalBody) {
                        modalBody.innerHTML = html;
                        if (typeof htmx !== 'undefined') {
                            htmx.process(modalBody);
                        }
                        updateHtmxModalLabel();
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
                        alert.appendChild(document.createTextNode(err.message || 'Failed to load preview.'));
                        modalBody.textContent = '';
                        modalBody.appendChild(alert);
                    }
                });
        });
    });
}

function closeHtmxModal() {
    // Abort any in-flight module-replace preview request
    if (typeof _activeReplaceController !== 'undefined' && _activeReplaceController) {
        _activeReplaceController.abort();
        _activeReplaceController = null;
    }
    hideModal(document.getElementById('htmx-modal'));
}

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
    initializeTabs();
    initializeNetBoxOnlyInterfaces();
    initializeSyncFormSpinners();
    initializeVlanSyncGroupSelects();
    initializeInstallSelectedForm();
    initializeModuleReplaceButtons();
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
    });
});

// Initialize scripts after HTMX swaps content
document.body.addEventListener('htmx:afterSwap', function (event) {
    initializeScripts();
});

// Update HTMX modal accessible label after content loads so screen readers
// announce the actual dialog title rather than the static "Loading" placeholder.
function updateHtmxModalLabel() {
    const htmxModal = document.getElementById('htmx-modal');
    if (!htmxModal) return;
    const header = htmxModal.querySelector('.modal-title, .modal-header h5, .modal-header h4');
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
    }
});
