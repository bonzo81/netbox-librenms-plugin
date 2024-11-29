// Function to initialize Cache countdown timer
function initializeCountdown(elementId) {
    const countdownElement = document.getElementById(elementId);
    if (!countdownElement) return;

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
    return setInterval(updateCountdown, 1000);
}

function initializeCountdowns() {
    if (window.interfaceCountdownInterval) {
        clearInterval(window.interfaceCountdownInterval);
    }
    if (window.cableCountdownInterval) {
        clearInterval(window.cableCountdownInterval);
    }

    window.interfaceCountdownInterval = initializeCountdown("countdown-timer");
    window.cableCountdownInterval = initializeCountdown("cable-countdown-timer");
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
}


// Function to initialize TomSelect elements for interface selection
function initializeVCMemberSelect() {
    // Small delay to ensure TomSelect is fully initialized in the DOM before attaching event listeners
    setTimeout(() => {
        const interfaceTable = document.getElementById('librenms-interface-table');
        if (!interfaceTable) return;

        const selects = interfaceTable.querySelectorAll('.form-select.tomselected');

        selects.forEach(select => {

            if (select.tomselect && !select.dataset.interfaceSelectInitialized) {
                select.dataset.interfaceSelectInitialized = 'true';
                select.tomselect.on('change', function (value) {
                    const deviceId = value;
                    const interfaceName = select.dataset.interface;
                    const rowId = select.dataset.rowId;

                    fetch('/plugins/librenms_plugin/verify-interface/', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value
                        },
                        body: JSON.stringify({
                            device_id: deviceId,
                            interface_name: interfaceName
                        })
                    })
                        .then(response => {
                            return response.json();
                        })
                        .then(data => {
                            const row = document.querySelector(`tr[data-interface="${rowId}"]`);


                            if (data.status === 'success' && row) {
                                const formattedRow = data.formatted_row;
                                row.querySelector('td[data-col="name"]').innerHTML = formattedRow.name;
                                row.querySelector('td[data-col="type"]').innerHTML = formattedRow.type;
                                row.querySelector('td[data-col="speed"]').innerHTML = formattedRow.speed;
                                row.querySelector('td[data-col="mac_address"]').innerHTML = formattedRow.mac_address;
                                row.querySelector('td[data-col="mtu"]').innerHTML = formattedRow.mtu;
                                row.querySelector('td[data-col="enabled"]').innerHTML = formattedRow.enabled;
                                row.querySelector('td[data-col="description"]').innerHTML = formattedRow.description;

                                filterCableTable();
                            }
                        });
                });
            }
        });
    }, 100);
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
            const bulkModal = bootstrap.Modal.getInstance(document.getElementById('bulkVCMemberModal'));
            bulkModal.hide();

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
}

// Check if URL contains tab parameter
const urlParams = new URLSearchParams(window.location.search);
const tab = urlParams.get('tab');
if (tab === 'cables') {
    // Trigger click on cables tab
    document.addEventListener('DOMContentLoaded', function () {
        document.getElementById('cables-tab').click();
    });
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
function initializeModalScripts() {
    const snmpSelect = document.querySelector('#add-device-modal select.form-select');
    if (snmpSelect) {
        snmpSelect.addEventListener('change', toggleSNMPForms);
        // Initial call to set the correct form visibility
        toggleSNMPForms();
    }
}

// Listen for the modal 'shown.bs.modal' event to initialize scripts
document.addEventListener('DOMContentLoaded', function () {
    const addDeviceModal = document.getElementById('add-device-modal');
    if (addDeviceModal) {
        addDeviceModal.addEventListener('shown.bs.modal', function () {
            initializeModalScripts();
        });
    }
});

function openBulkVCModal() {
    const modal = new bootstrap.Modal(document.getElementById('bulkVCMemberModal'));
    modal.show();
}

// Function to initialize all necessary scripts
function initializeScripts() {
    initializeCheckboxes();
    initializeVCMemberSelect();
    initializeFilters();
    initializeCountdowns();
    initializeCheckboxListeners();
    initializeBulkEditApply();
}


// Initialize scripts on initial DOM load
document.addEventListener('DOMContentLoaded', function () {
    initializeScripts();

});

// Initialize scripts after HTMX swaps content
document.body.addEventListener('htmx:afterSwap', function (event) {
    initializeScripts();
});
