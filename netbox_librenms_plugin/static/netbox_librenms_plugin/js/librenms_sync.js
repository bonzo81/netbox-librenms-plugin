// Function to initialize Cache countdown timer
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
    countdownInterval = setInterval(updateCountdown, 1000);
    return countdownInterval;
}

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

    window.interfaceCountdownInterval = initializeCountdown("countdown-timer");
    window.cableCountdownInterval = initializeCountdown("cable-countdown-timer");
    window.ipCountdownInterval = initializeCountdown("ip-countdown-timer");
}


// Function to initialize checkbox handling for a specific table
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

// Initialize both tables
function initializeCheckboxes() {
    initializeTableCheckboxes('librenms-interface-table');
    initializeTableCheckboxes('librenms-interface-table-vm');
    initializeTableCheckboxes('librenms-cable-table');
    initializeTableCheckboxes('librenms-cable-table-vc');
    initializeTableCheckboxes('librenms-ipaddress-table');
}

// Initialize the 'Apply' button for the bulk VCMember select
function initializeVCMemberSelect() {
    setTimeout(() => {
        const interfaceTable = document.getElementById('librenms-interface-table');
        const cableTable = document.getElementById('librenms-cable-table-vc');

        if (interfaceTable) {
            const interfaceSelects = interfaceTable.querySelectorAll('.form-select.tomselected');
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
    }, 100);
}

// Function to initialize VRF selects
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
    }, 100);
}

// Function to handle VRF change event
function handleVRFChange(select, value) {
    const ipAddress = select.dataset.ip;
    const prefixLength = select.dataset.prefix || "";  // Get prefix length if present
    const fullIpAddress = prefixLength ? `${ipAddress}/${prefixLength}` : ipAddress;

    // Extract device ID from various URL patterns
    let deviceId = null;
    const deviceIdMatch = window.location.pathname.match(/\/devices\/(\d+)\//);
    const vmIdMatch = window.location.pathname.match(/\/virtual-machines\/(\d+)\//);
    const pluginDeviceMatch = window.location.pathname.match(/\/plugins\/librenms_plugin\/device\/(\d+)\//);
    const pluginVMMatch = window.location.pathname.match(/\/plugins\/librenms_plugin\/vm\/(\d+)\//);

    deviceId = deviceIdMatch ? deviceIdMatch[1] :
        vmIdMatch ? vmIdMatch[1] :
            pluginDeviceMatch ? pluginDeviceMatch[1] :
                pluginVMMatch ? pluginVMMatch[1] : null;

    if (!deviceId) {
        return;
    }

    fetch('/plugins/librenms_plugin/verify-ipaddress/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
        },
        body: JSON.stringify({
            device_id: deviceId,
            ip_address: fullIpAddress,  // Use full IP address with prefix
            vrf_id: value
        })
    })
        .then(response => response.json())
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
            console.error("Error during VRF change fetch:", error);
        });
}

// Function to handle VC member interface change event
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
            interface_name_field: document.querySelector('input[name="interface_name_field"]:checked').value
        })
    })
        .then(response => response.json())
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
        });
}
// Function to handle cable VC member change event
function handleCableChange(select, value) {
    fetch('/plugins/librenms_plugin/verify-cable/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
        },
        body: JSON.stringify({
            device_id: value,
            local_port: select.dataset.interface
        })
    })
        .then(response => response.json())
        .then(data => {
            const row = document.querySelector(`tr[data-interface="${select.dataset.rowId}"]`);

            if (data.status === 'success' && row) {
                const formattedRow = data.formatted_row;
                const actionsCell = row.querySelector('td[data-col="actions"]');
                row.querySelector('td[data-col="local_port"]').innerHTML = formattedRow.local_port;
                row.querySelector('td[data-col="remote_port"]').innerHTML = formattedRow.remote_port;
                row.querySelector('td[data-col="remote_device"]').innerHTML = formattedRow.remote_device;
                row.querySelector('td[data-col="cable_status"]').innerHTML = formattedRow.cable_status;
                row.querySelector('td[data-col="actions"]').innerHTML = formattedRow.actions;

            }
        });
}


// Function to initialize the 'Apply' button for bulk VC member assignment
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
            const bulkModal = document.getElementById('bulkVCMemberModal');
            if (bulkModal) {
                bulkModal.classList.remove('show');
                bulkModal.style.display = 'none';
                bulkModal.setAttribute('aria-hidden', 'true');
                bulkModal.removeAttribute('aria-modal');

                const backdrop = document.querySelector('.modal-backdrop');
                if (backdrop) {
                    backdrop.remove();
                }

                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('padding-right');
                document.body.style.removeProperty('overflow');
            }

        });
    }
}

// Update the 'initializeCheckboxListeners' function
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

// Update the 'updateBulkActionButton' function
function updateBulkActionButton() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;
    const anyChecked = interfaceTable.querySelectorAll('input[name="select"]:checked').length > 0;
    const bulkButton = document.getElementById('bulk-vc-member-button');
    if (bulkButton) {
        bulkButton.disabled = !anyChecked;
    }
}

// Generic function to initialize filters for a table
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

// Initialize a flag to prevent adding duplicate event listeners
let tabsInitialized = false;
// Function to initialize the 'active' tab based on the URL
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

    // Add event listeners only once
    if (!tabsInitialized) {
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
        tabsInitialized = true;
    }
}

// Function to toggle SNMP forms based on version
function toggleSNMPForms() {
    const snmpSelect = document.querySelector('#add-device-modal select.form-select');
    if (!snmpSelect) return;
    const version = snmpSelect.value;

    const v2Form = document.getElementById('snmpv2-form');
    const v3Form = document.getElementById('snmpv3-form');

    if (version === 'v2c') {
        v2Form.style.display = 'block';
        v3Form.style.display = 'none';
    } else {
        v2Form.style.display = 'none';
        v3Form.style.display = 'block';
    }
}

// Function to initialize modal-specific scripts
function initializeSNMPModalScripts() {
    const snmpSelect = document.querySelector('#add-device-modal select.form-select');
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
    const modal = document.getElementById('bulkVCMemberModal');
    if (modal) {
        modal.classList.add('show');
        modal.style.display = 'block';
        modal.setAttribute('aria-modal', 'true');
        modal.removeAttribute('aria-hidden');

        // Add backdrop
        const backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop fade show';
        document.body.appendChild(backdrop);

        document.body.classList.add('modal-open');
    }
}

// Function to update the interface_name_field radio button
function updateInterfaceNameField() {
    document.querySelectorAll('.interface-name-field').forEach(radio => {
        radio.addEventListener('change', function () {
            const url = new URL(window.location);
            url.searchParams.set('interface_name_field', this.value);
            window.history.pushState({}, '', url);

            // Set HTMX headers for subsequent requests
            htmx.config.defaultHeaders['X-Interface-Name-Field'] = this.value;

            // Refresh current tab content
            const activeTab = document.querySelector('.tab-pane.active');
            if (activeTab) {
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

            const interfaceNames = Array.from(selectedCheckboxes).map(cb => {
                const row = cb.closest('tr');
                return row.querySelector('td:nth-child(2) a').textContent;
            });

            deleteSelectedInterfaces(selectedCheckboxes);
        });
    }
}


function deleteSelectedInterfaces(selectedCheckboxes) {
    const interfaceIds = Array.from(selectedCheckboxes).map(cb => cb.value);

    const formData = new FormData();

    // Add CSRF token
    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value;

    if (!csrfToken) {
        console.error('CSRF token not found. Please refresh the page and try again.');
        return;
    }

    formData.append('csrfmiddlewaretoken', csrfToken);

    // Add interface IDs
    interfaceIds.forEach(id => {
        formData.append('interface_ids', id);
    });

    // Get the current URL to construct the delete URL
    const currentPath = window.location.pathname;
    const pathParts = currentPath.split('/');

    // Extract object type and ID from the path
    // Expected path formats: 
    // /dcim/devices/{id}/librenms-sync/
    // /dcim/virtual-machines/{id}/librenms-sync/
    // /plugins/librenms_plugin/device/{id}/librenms-sync/
    // /plugins/librenms_plugin/virtual-machines/{id}/librenms-sync/
    let objectType, objectId;

    const deviceIndex = pathParts.indexOf('devices');
    const vmIndex = pathParts.indexOf('virtual-machines');
    const pluginDeviceIndex = pathParts.indexOf('device');
    const pluginVMIndex = pathParts.indexOf('virtualmachine');

    if (deviceIndex !== -1 && deviceIndex + 1 < pathParts.length) {
        objectType = 'device';
        objectId = pathParts[deviceIndex + 1];
    } else if (vmIndex !== -1 && vmIndex + 1 < pathParts.length) {
        objectType = 'virtualmachine';
        objectId = pathParts[vmIndex + 1];
    } else if (pluginDeviceIndex !== -1 && pluginDeviceIndex + 1 < pathParts.length) {
        objectType = 'device';
        objectId = pathParts[pluginDeviceIndex + 1];
    } else if (pluginVMIndex !== -1 && pluginVMIndex + 1 < pathParts.length) {
        objectType = 'virtualmachine';
        objectId = pathParts[pluginVMIndex + 1];
    }

    if (!objectType || !objectId) {
        console.error('Could not determine object type and ID');
        return;
    }

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
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                // Close modal using native DOM methods
                const modalElement = document.getElementById('netboxOnlyInterfacesModal');
                if (modalElement) {
                    // Hide the modal
                    modalElement.classList.remove('show');
                    modalElement.style.display = 'none';
                    modalElement.setAttribute('aria-hidden', 'true');
                    modalElement.removeAttribute('aria-modal');

                    // Remove backdrop
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) {
                        backdrop.remove();
                    }

                    // Clean up body classes and styles
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('padding-right');
                    document.body.style.removeProperty('overflow');
                }

                // Refresh the interface data by triggering the refresh button
                const refreshButton = document.querySelector('[hx-post*="interface-sync"]');
                if (refreshButton) {
                    refreshButton.click();
                } else {
                    // Fallback: reload the page
                    window.location.reload();
                }
            } else {
                console.error('Error:', data.error || 'Unknown error occurred');
            }
        })
        .catch(error => {
            console.error('Error:', error);
        })
        .finally(() => {
            // Restore button state
            deleteButton.innerHTML = originalText;
            deleteButton.disabled = false;
        });
}


// Function to initialize all necessary scripts
function initializeScripts() {
    initializeCheckboxes();
    initializeVCMemberSelect();
    initializeVRFSelects();
    initializeFilters();
    initializeCountdowns();
    initializeCheckboxListeners();
    initializeBulkEditApply();
    updateInterfaceNameField();
    setInterfaceNameFieldFromURL();
    initializeTabs();
    initializeNetBoxOnlyInterfaces();
}


// Initialize scripts on initial DOM load
document.addEventListener('DOMContentLoaded', function () {
    initializeScripts();

});

// Initialize scripts after HTMX swaps content
document.body.addEventListener('htmx:afterSwap', function (event) {
    initializeScripts();
});
