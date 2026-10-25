"""Template tags that build sync tab URLs from the page query (see ``sync_query``)."""

from django import template

from netbox_librenms_plugin.sync_query import SyncQuery

register = template.Library()


@register.simple_tag(takes_context=True)
def sync_tab_url(context, path="", prefix="", reset="", **changes):
    """
    Return *path* with the request's page query, after *reset* is dropped and *changes* are applied.

    Django's ``{% querystring %}`` cannot take a computed key such as ``<prefix>page``, so names here
    are relative to *prefix*, except the page keys ``tab``, ``server_key`` and ``interface_name_field``.

    Args:
        context (Context): The template context, which must hold the request.
        path (str): The target path; empty for the current page.
        prefix (str): The tab prefix, for example ``interfaces_``.
        reset (str): Space-separated names to drop; ``filters`` drops every filter of the tab.
        **changes: Values to set; an empty value drops the key.

    Returns:
        str: The URL.

    """
    return SyncQuery.from_request(context["request"]).url(path, changes, reset.split(), prefix)


@register.simple_tag(takes_context=True)
def sync_tab_query_items(context, prefix="", reset="", **changes):
    """Return the (key, value) pairs of the changed page query, for hidden form inputs; see ``sync_tab_url``."""
    return SyncQuery.from_request(context["request"]).items(changes, reset.split(), prefix)


@register.simple_tag
def sync_cache_timeout(server_key):
    """Return the configured cache lifetime in seconds for the tab's server."""
    from netbox_librenms_plugin.librenms_api import configured_cache_timeout

    return configured_cache_timeout(server_key)
