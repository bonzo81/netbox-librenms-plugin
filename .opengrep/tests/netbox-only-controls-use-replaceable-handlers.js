function initializeNetBoxOnlyInterfaces() {
    // ruleid: netbox-only-controls-use-replaceable-handlers
    deleteButton.addEventListener('click', handler);
    // ruleid: netbox-only-controls-use-replaceable-handlers
    selectAllCheckbox.addEventListener('change', handler);
    // ruleid: netbox-only-controls-use-replaceable-handlers
    checkbox.addEventListener('change', handler);
    // ok: netbox-only-controls-use-replaceable-handlers
    deleteButton.onclick = handler;
    // ok: netbox-only-controls-use-replaceable-handlers
    checkbox.onchange = handler;
}
function anotherInitializer() {
    // ok: netbox-only-controls-use-replaceable-handlers
    button.addEventListener('click', handler);
}
