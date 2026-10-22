from urllib.parse import quote_plus, urlencode

from django.urls import reverse


def redirect_url(device, server_key, tab_url, request):
    # ruleid: sync-tab-url-through-sync-query
    url = f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device.pk])}?tab=cables"
    # ruleid: sync-tab-url-through-sync-query
    url = f"{tab_url}?tab=cables" + (f"&server_key={quote_plus(server_key)}" if server_key else "")
    url = (
        f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device.pk])}"
        # ruleid: sync-tab-url-through-sync-query
        "?tab=cables"
    )
    # ruleid: sync-tab-url-through-sync-query
    url = f"{tab_url}?server_key={quote_plus(server_key)}&tab=vlans"
    # ok: sync-tab-url-through-sync-query
    url = page_query_url(request, tab_url, {"tab": "cables", "server_key": server_key})
    # ok: sync-tab-url-through-sync-query
    url = f"{tab_url}?{urlencode({'tab': 'cables'})}"
    # ok: sync-tab-url-through-sync-query
    status = coordinator.status_for_request(request, server_key, active_tab="cables")
    # ok: sync-tab-url-through-sync-query
    tab_context = dict(source_tab="cables", data_tab="cables")
    return url, status, tab_context


def page_query_url(request, path, changes):
    return path


coordinator = None
