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
                select.tomselect.on('change', function(value) {
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

// Main function to initialize interface filters
function initializeInterfaceFilters() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;

    ['name', 'type', 'speed', 'mac', 'mtu', 'enabled', 'description'].forEach(filter => {
        const filterElement = document.getElementById(`filter-${filter}`);
        if (filterElement) {
            filterElement.addEventListener('input', filterInterfaceTable);
        }
    });
}
// Function to filter the interface table
function filterInterfaceTable() {
    const interfaceTable = document.getElementById('librenms-interface-table');
    if (!interfaceTable) return;

    const filters = {
        name: document.getElementById('filter-name')?.value.toLowerCase() || '',
        type: document.getElementById('filter-type')?.value.toLowerCase() || '',
        speed: document.getElementById('filter-speed')?.value.toLowerCase() || '',
        mac: document.getElementById('filter-mac')?.value.toLowerCase() || '',
        mtu: document.getElementById('filter-mtu')?.value.toLowerCase() || '',
        enabled: document.getElementById('filter-enabled')?.value.toLowerCase() || '',
        description: document.getElementById('filter-description')?.value.toLowerCase() || ''
    };

    const rows = interfaceTable.querySelectorAll('tr[data-interface]');
    rows.forEach(row => {
        const matches = {
            name: (row.querySelector('td[data-col="name"] span')?.textContent || row.querySelector('td[data-col="name"]').textContent).toLowerCase().includes(filters.name),
            type: (row.querySelector('td[data-col="type"] span')?.textContent || row.querySelector('td[data-col="type"]').textContent).toLowerCase().includes(filters.type),
            speed: (row.querySelector('td[data-col="speed"] span')?.textContent || row.querySelector('td[data-col="speed"]').textContent).toLowerCase().includes(filters.speed),
            mac: (row.querySelector('td[data-col="mac_address"] span')?.textContent || row.querySelector('td[data-col="mac_address"]').textContent).toLowerCase().includes(filters.mac),
            mtu: (row.querySelector('td[data-col="mtu"] span')?.textContent || row.querySelector('td[data-col="mtu"]').textContent).toLowerCase().includes(filters.mtu),
            enabled: (row.querySelector('td[data-col="enabled"] span')?.textContent || row.querySelector('td[data-col="enabled"]').textContent).toLowerCase().includes(filters.enabled),
            description: (row.querySelector('td[data-col="description"] span')?.textContent || row.querySelector('td[data-col="description"]').textContent).toLowerCase().includes(filters.description)
        };

        row.style.display = Object.values(matches).every(match => match) ? '' : 'none';
    });
}

// Function to initialize cable filters
function initializeCableFilters() {
    const cableTable = document.getElementById('librenms-cable-table');
    if (!cableTable) return;

    ['vc-member', 'local-port', 'remote-port', 'remote-device'].forEach(filter => {
        const filterElement = document.getElementById(`filter-${filter}`);
        if (filterElement) {
            filterElement.addEventListener('input', filterCableTable);
        }
    });
}
// Main function to filter cable table

function filterCableTable() {
    const cableTable = document.getElementById('librenms-cable-table');
    if (!cableTable) return;

    const filters = {
        vcMember: document.getElementById('filter-vc-member')?.value.toLowerCase() || '',
        localPort: document.getElementById('filter-local-port')?.value.toLowerCase() || '',
        remotePort: document.getElementById('filter-remote-port')?.value.toLowerCase() || '',
        remoteDevice: document.getElementById('filter-remote-device')?.value.toLowerCase() || ''
    };

    const rows = cableTable.querySelectorAll('tr[data-interface]');
    rows.forEach(row => {
        const matches = {
            vcMember: row.querySelector('.ts-control .item')?.textContent.toLowerCase().includes(filters.vcMember),
            localPort: row.querySelector('td[data-col="local_port"]').textContent.toLowerCase().includes(filters.localPort),
            remotePort: row.querySelector('td[data-col="remote_port"]').textContent.toLowerCase().includes(filters.remotePort),
            remoteDevice: row.querySelector('td[data-col="remote_device"]').textContent.toLowerCase().includes(filters.remoteDevice)
        };

        row.style.display = Object.values(matches).every(match => match) ? '' : 'none';
    });
}



// Check if URL contains tab parameter
const urlParams = new URLSearchParams(window.location.search);
const tab = urlParams.get('tab');
if (tab === 'cables') {
    // Trigger click on cables tab
    document.addEventListener('DOMContentLoaded', function() {
        document.getElementById('cables-tab').click();
    });
}


// Function to initialize all necessary scripts
function initializeScripts() {
    initializeCheckboxes();
    initializeVCMemberSelect();
    initializeInterfaceFilters();
    initializeCableFilters();
    initializeCountdowns();
}


// Initialize scripts on initial DOM load
document.addEventListener('DOMContentLoaded', function () {
    initializeScripts();

});

// Initialize scripts after HTMX swaps content
document.body.addEventListener('htmx:afterSwap', function (event) {
    initializeScripts();
});
