def attributes(html):
    # ruleid: sync-tab-attributes-through-parser
    re.search(r'<div id="librenms-sync-tabs" data-active-tab="interfaces">', html)
    # ok: sync-tab-attributes-through-parser
    re.search(r'<div id="librenms-sync-tabs"[^>]*>', html)
    # ok: sync-tab-attributes-through-parser
    sync_tab_attributes(html)
