"""
The page query that every hop of the sync page carries.

The URL query is the one carrier of a sync tab's state: the active tab, the server, the name field,
and each tab's page, page size, sort, show-ignored toggle and filters. Every link, form and
follow-up request starts from the query of the page it was rendered for and states only what it
changes. ``SyncQuery`` owns the reset rules: which keys a transition drops.
"""

from django.utils.http import url_has_allowed_host_and_scheme

from netbox_librenms_plugin.sync_filters import COLUMN_FIELD_PREFIX, QUERY_FIELD, SYNC_STATE_FIELD

# Keys the whole page shares. Any other name a caller gives is relative to the tab prefix.
PAGE_KEYS = frozenset({"tab", "server_key", "interface_name_field"})

# The reset name that drops every filter parameter of a tab.
FILTERS = "filters"


class SyncQuery:
    """A read-only page query, and the transitions the sync tabs apply to it."""

    def __init__(self, query):
        """Keep a private copy of *query*, a ``QueryDict``."""
        self._query = query.copy()

    @classmethod
    def from_request(cls, request):
        """Return the page query a request carries."""
        return cls(request.GET)

    @staticmethod
    def tab_key(prefix, name):
        """Return the query key of *name*: a page key as it is, any other name with the tab *prefix*."""
        return name if name in PAGE_KEYS else f"{prefix}{name}"

    def _filter_keys(self, prefix):
        names = (QUERY_FIELD, SYNC_STATE_FIELD)
        return [
            key
            for key in self._query
            if key.startswith(prefix)
            and (key[len(prefix) :] in names or key[len(prefix) :].startswith(COLUMN_FIELD_PREFIX))
        ]

    def _changed(self, changes, reset, prefix):
        query = self._query.copy()
        for name in reset:
            keys = self._filter_keys(prefix) if name == FILTERS else [self.tab_key(prefix, name)]
            for key in keys:
                query.pop(key, None)
        for name, value in (changes or {}).items():
            key = self.tab_key(prefix, name)
            if value is None or value == "":
                query.pop(key, None)
            else:
                query[key] = str(value)
        return query

    def url(self, path="", changes=None, reset=(), prefix=""):
        """
        Return *path* with this query, after *reset* is dropped and *changes* are applied.

        Args:
            path (str): The target path. An empty path gives a bare ``?query`` for the current page.
            changes (dict | None): Values to set. An empty value or None drops the key.
            reset (Iterable[str]): Names to drop. ``FILTERS`` drops every filter parameter of the tab.
            prefix (str): The tab prefix that names in *changes* and *reset* take, except ``PAGE_KEYS``.

        Returns:
            str: The URL.

        """
        encoded = self._changed(changes, reset, prefix).urlencode()
        return f"{path}?{encoded}" if encoded else (path or "?")

    def items(self, changes=None, reset=(), prefix=""):
        """Return the (key, value) pairs of the changed query, a repeated key once per value; see ``url``."""
        query = self._changed(changes, reset, prefix)
        return [(key, value) for key in query for value in query.getlist(key)]


def page_query_url(request, path, changes=None):
    """
    Return *path* with the request's page query and *changes*, for a redirect back to the sync page.

    The query comes from the request, so the URL must pass the open-redirect barrier (CWE-601).
    A URL that does not pass gives the bare *path*, a trusted ``reverse()`` result.

    Args:
        request (HttpRequest): The request whose query is the page query.
        path (str): The sync page path.
        changes (dict | None): Page keys to set, such as ``tab`` and ``server_key``; see ``SyncQuery.url``.

    Returns:
        str: The local URL.

    """
    target = SyncQuery.from_request(request).url(path, changes)
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return path
