function postedServerKey() {
    // ruleid: sync-script-reads-page-server-key
    const input = document.querySelector('[name=server_key]');
    // ruleid: sync-script-reads-page-server-key
    const quoted = document.querySelector('input[name="server_key"]')?.value || null;
    // ruleid: sync-script-reads-page-server-key
    const all = document.querySelectorAll("[name='server_key']");
    // ruleid: sync-script-reads-page-server-key
    const named = document.getElementsByName("server_key");
    return [input, quoted, all, named];
}

function scopedServerKey(form) {
    // ok: sync-script-reads-page-server-key
    const own = form.querySelector('[name=server_key]');
    // ok: sync-script-reads-page-server-key
    const csrf = document.querySelector('[name=csrfmiddlewaretoken]');
    // ok: sync-script-reads-page-server-key
    const page = document.getElementById('librenms-sync-tabs')?.dataset.serverKey || '';
    return [own, csrf, page];
}
