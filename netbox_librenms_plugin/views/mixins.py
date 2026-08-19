import json
from urllib.parse import quote_plus

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import get_script_prefix
from django.utils.http import url_has_allowed_host_and_scheme
from utilities.permissions import get_permission_for_model

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.utils import coerce_model_pk, is_list_of_dicts


def parse_request_json(request):
    """Parse JSON from request.body, returning (data, error_response).

    On success returns (dict, None). On malformed input returns (None, JsonResponse 400).
    json.loads happily returns a bare list/str/number for a valid-JSON non-object body;
    every caller immediately does ``data.get(...)``, which would 500 on those — so a
    non-dict payload is rejected here with a 400, keeping the (dict, None) contract true
    for every endpoint instead of only the ones that added their own guard.
    """
    try:
        data = json.loads(request.body)
    except (TypeError, ValueError):
        return None, JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({"status": "error", "message": "JSON payload must be an object"}, status=400)
    return data, None


def extract_cached_ports(cached, cache_key=None):
    """
    Return the validated dict payload of a cached "ports" entry, or None when malformed.

    Mirrors the cables view's ``_extract_cached_links``: a stale/corrupt but truthy cache
    value — a non-dict snapshot (e.g. a legacy bare list), a non-list ``ports``, or a
    non-dict port row — would AttributeError-500 the consumer's ``.get()``/row reads.
    Treat those as a cache miss; when ``cache_key`` is given, purge the bad entry so the
    next read doesn't keep serving garbage.

    Args:
        cached: The raw value read from the ports cache key.
        cache_key: Optional cache key to delete when the entry is malformed.

    Returns:
        dict | None: The cached payload with a list of dict port rows, or None.
    """
    # ``is_list_of_dicts`` is the same shape check the VLAN/interface read-paths use: ports must
    # be a list whose every row is a dict (an empty list is valid — a device with no ports).
    if not isinstance(cached, dict) or not is_list_of_dicts(cached.get("ports")):
        if cache_key is not None:
            cache.delete(cache_key)
        return None
    return cached


def validated_referer(request):
    """
    Return the request's ``Referer`` when it passes the open-redirect barrier, else None.

    The single home for the CWE-601 Referer check (``url_has_allowed_host_and_scheme`` against the
    current host/scheme) so every redirect helper that trusts the Referer — ``_get_safe_redirect_url``
    here and ``migrate._safe_referer`` — validates it identically and can't drift. Callers own their
    own fallback when this returns None.
    """
    referrer = request.META.get("HTTP_REFERER")
    if referrer and url_has_allowed_host_and_scheme(
        referrer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return referrer
    return None


def _get_safe_redirect_url(request):
    """
    Return a validated redirect URL from the HTTP Referer header.

    Validates the Referer against allowed hosts and schemes to prevent
    open-redirect attacks. Falls back to the current request path or "/".
    """
    if referrer := validated_referer(request):
        return referrer
    # No usable Referer. On a non-GET request, request.path is often a POST-only
    # action endpoint, so redirecting the browser there would 405 — fall back to a
    # GET-safe app root instead. Use the deployment script prefix (e.g. "/netbox/")
    # so a prefixed install doesn't bounce to the domain root. GET requests reload path.
    if getattr(request, "method", "GET") != "GET":
        return get_script_prefix()
    return getattr(request, "path", get_script_prefix())


def _safe_redirect_response(request):
    """
    Build a permission-denied redirect response to a validated URL.

    Resolves a candidate target via ``_get_safe_redirect_url`` and then
    re-applies the ``url_has_allowed_host_and_scheme`` guard inline as a
    positive guard, with the redirect sink inside the validated branch and a
    hard-coded ``"/"`` fallback otherwise. Keeping the open-redirect guard
    local to the sink (rather than in a helper) prevents open-redirect attacks
    and lets static analysers trace the sanitizer barrier.

    Returns an HTMX ``HX-Redirect`` response for HTMX requests, otherwise a
    standard redirect.
    """
    target = _get_safe_redirect_url(request)
    is_htmx = bool(request.headers.get("HX-Request"))

    if url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        if is_htmx:
            return HttpResponse("", headers={"HX-Redirect": target})
        return redirect(target)

    app_root = get_script_prefix()
    if is_htmx:
        return HttpResponse("", headers={"HX-Redirect": app_root})
    return redirect(app_root)


def resolve_configured_server_key(server_key):
    """
    Return *server_key* iff it matches a currently-configured LibreNMS server, else None.

    Centralises the "re-source the key from trusted config, never echo a raw/stale POST value"
    allowlist shared by the sync-tab redirect (:func:`device_fields._sync_redirect`) and the
    non-HTMX fallback URL builder (:func:`migrate._sync_tab_url`), so a stale or tampered
    ``server_key`` is dropped by the same rule in both places. A blank/None key resolves to None.

    Args:
        server_key (str | None): The candidate server key (typically a raw POST value).

    Returns:
        str | None: *server_key* when it names a configured server, otherwise None.
    """
    if not isinstance(server_key, str) or not server_key:
        return None
    return server_key if server_key in LibreNMSAPI.get_available_servers() else None


def redirect_with_server_key(request, url, server_key):
    """
    Redirect to *url*, appending a validated ``?server_key`` query param when one is given.

    Shared by the sync-tab redirect helpers (``device_fields._sync_redirect`` /
    ``interfaces_view._failure_redirect``) so the server_key-preserving redirect is written once.
    The candidate URL is gated by Django's ``url_has_allowed_host_and_scheme`` with the ``redirect``
    sink inside the validated branch — the open-redirect barrier for py/url-redirection (CWE-601).
    Callers own how *server_key* is sourced (a raw POST value, or one re-matched against the
    configured servers); a blank/None one redirects to the bare *url* (a trusted ``reverse()`` path).

    Args:
        request: The current HTTP request (host allowlist + scheme for the barrier).
        url (str): The already-reversed redirect target.
        server_key (str | None): The server key to carry on the redirect; blank/None → bare *url*.

    Returns:
        HttpResponseRedirect: Redirect to *url*, with the validated ``server_key`` query param when
            it passes the open-redirect barrier.
    """
    if server_key:
        sep = "&" if "?" in url else "?"
        candidate = f"{url}{sep}server_key={quote_plus(server_key)}"
        if url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(candidate)
    return redirect(url)


class LibreNMSPermissionMixin(PermissionRequiredMixin):
    """
    Mixin for views requiring LibreNMS plugin permissions.

    All plugin views require 'view_librenmssettings' to access the page.
    Write actions require 'change_librenmssettings' plus any relevant
    NetBox object permissions.
    """

    permission_required = PERM_VIEW_PLUGIN

    def has_write_permission(self):
        """Check if user can perform write actions."""
        user = getattr(getattr(self, "request", None), "user", None)
        return bool(user and user.has_perm(PERM_CHANGE_PLUGIN))

    def require_write_permission(self, error_message=None):
        """
        Check write permission and return error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with toast message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        if not self.has_write_permission():
            if getattr(self, "request", None) is None:
                return HttpResponse(status=403)
            msg = error_message or "You do not have permission to perform this action."
            messages.error(self.request, msg)

            return _safe_redirect_response(self.request)
        return None

    def require_write_permission_json(self, error_message=None):
        """
        Check write permission and return JSON error response if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        if not self.has_write_permission():
            msg = error_message or "You do not have permission to perform this action."
            return JsonResponse({"error": msg}, status=403)
        return None


class LibreNMSWritePermissionMixin(LibreNMSPermissionMixin):
    """
    Mixin for mutation views requiring LibreNMS plugin write permission.

    Sets permission_required to 'change_librenmssettings' so that only users
    with write access can access Create, Edit, Delete, BulkImport, and
    BulkDelete views.
    """

    permission_required = PERM_CHANGE_PLUGIN


def relock_scoped_row(model, **lookup):
    """
    Re-lock a row whose id came from an ALREADY-RESOLVED object: the single audited exception.

    Scoping happened where that object was resolved. Restricting the re-read instead would demand a
    permission the caller's gate never required: ``restrict()`` returns ``none()`` for a user
    without the model-level grant, so a change-only caller would silently lose rows out of a lock
    set and be told the object "no longer exists".

    Every call asserts that *lookup* was derived from an object the request already resolved through
    ``restricted_queryset``. Never pass a client-supplied id. One named chokepoint keeps these
    greppable and lets the raw-pk scan drop its spelling heuristic, which any request-derived
    ``*_id`` attribute satisfied by accident.

    Args:
        model: The model whose row to lock.
        **lookup: Lookup kwargs identifying the row (e.g. ``pk=donor.oob_ip_id``).

    Returns:
        The locked instance, or None when the row is gone.
    """
    # order_by() drops the model ordering, which can traverse a nullable FK (VLAN orders by
    # site/group): PostgreSQL refuses FOR UPDATE over the nullable side of the resulting outer
    # join. first() then orders by pk, which joins nothing.
    return model.objects.select_for_update().filter(**lookup).order_by().first()


class NetBoxObjectPermissionMixin:
    """
    Mixin for views requiring specific NetBox object permissions.

    Define required_object_permissions as a dict mapping HTTP methods
    to lists of (action, model) tuples.

    Example:
        required_object_permissions = {
            'POST': [
                ('add', Interface),
                ('change', Interface),
            ],
        }
    """

    required_object_permissions = {}

    def check_object_permissions(self, method):
        """
        Check all required object permissions for the given HTTP method.

        Args:
            method: HTTP method (GET, POST, etc.)

        Returns:
            tuple: (has_all: bool, missing: list[str])
        """
        requirements = self.required_object_permissions.get(method, [])
        missing = [get_permission_for_model(model, action) for action, model in requirements]

        # Fail closed if there's no usable request/user (e.g. the view was invoked outside
        # dispatch()): return the required perms as "missing" so callers get a deterministic
        # permission denial instead of an AttributeError on self.request.user.
        user = getattr(getattr(self, "request", None), "user", None)
        if user is None:
            return (not missing, missing)

        missing = [perm for perm in missing if not user.has_perm(perm)]
        return (len(missing) == 0, missing)

    def require_object_permissions(self, method):
        """
        Require all object permissions for the method, returning error response if denied.

        Handles both HTMX and regular requests appropriately:
        - HTMX: Returns HX-Redirect to referrer with flash message
        - Regular: Returns redirect to referrer with flash message

        Returns:
            None if permitted, or appropriate response if denied
        """
        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            if getattr(self, "request", None) is None:
                return HttpResponse(status=403)
            missing_str = ", ".join(missing)
            msg = f"Missing permissions: {missing_str}"
            messages.error(self.request, msg)

            return _safe_redirect_response(self.request)
        return None

    def require_object_permissions_json(self, method):
        """
        Require all object permissions for the method, returning JSON error if denied.

        Use this method for AJAX/HTMX endpoints that return JsonResponse.
        Does not set flash messages since JSON clients handle errors differently.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        from django.http import JsonResponse

        has_perms, missing = self.check_object_permissions(method)
        if not has_perms:
            missing_str = ", ".join(missing)
            return JsonResponse({"error": f"Missing permissions: {missing_str}"}, status=403)
        return None

    def restricted_queryset(self, model, action="view"):
        """
        Scope *model*'s queryset to the objects the request user may *action*.

        The permission gates (check_object_permissions / require_*_permissions) only ask
        ``user.has_perm(perm)`` with no instance, so a constrained grant (e.g. a site-scoped
        ``view_device``) passes the model-level check. Resolving an id against the plain manager
        would then expose any object by raw pk, so scope through ``model.objects.restrict`` — the
        same per-object constraint NetBox enforces everywhere else.

        Args:
            model: A NetBox model whose manager is a ``RestrictedQuerySet`` (exposes ``restrict``).
            action: The permission action to scope by (default ``"view"``).

        Returns:
            A queryset filtered to the objects the request user may perform *action* on.
        """
        return model.objects.restrict(self.request.user, action)

    def restrict_object_or_404(self, model, action="view", select_related=(), **kwargs):
        """
        Resolve one object through :meth:`restricted_queryset` (fail-closed lookup).

        An out-of-scope id 404s exactly like a nonexistent one, so a constrained grant can't read
        another object's data by raw pk. Mirrors ``get_object_or_404`` — pass the lookup as kwargs.

        Args:
            model: A NetBox model whose manager is a ``RestrictedQuerySet``.
            action: The permission action to scope by (default ``"view"``).
            select_related: Relations to join in the same query, for a caller that reads them right
                after (the plain ``Model.objects.select_related(...)`` form would skip the scoping).
            **kwargs: Lookup kwargs forwarded to ``get_object_or_404`` (e.g. ``pk=...``).

        Returns:
            The resolved object the user is permitted to access.
        """
        queryset = self.restricted_queryset(model, action)
        if select_related:
            queryset = queryset.select_related(*select_related)
        return get_object_or_404(queryset, **kwargs)

    def relock_scoped_row(self, model, **lookup):
        """Re-lock a row derived from an already-resolved object. See :func:`relock_scoped_row`."""
        return relock_scoped_row(model, **lookup)

    def require_all_permissions(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions.

        Combines require_write_permission() and require_object_permissions()
        into a single call. Handles HTMX and regular requests.

        Returns:
            None if permitted, or appropriate error response if denied
        """
        if error := self.require_write_permission():
            return error
        return self.require_object_permissions(method)

    def require_all_permissions_json(self, method="POST"):
        """
        Check both plugin write and NetBox object permissions, returning JSON errors.

        Combines require_write_permission_json() and require_object_permissions_json()
        into a single call for JSON/AJAX endpoints.

        Returns:
            None if permitted, or JsonResponse with 403 status if denied
        """
        if error := self.require_write_permission_json():
            return error
        return self.require_object_permissions_json(method)


class LibreNMSAPIMixin:
    """
    A mixin class that provides access to the LibreNMS API.

    This mixin initializes a LibreNMSAPI instance and provides a property
    to access it. It's designed to be used with other view classes that
    need to interact with the LibreNMS API.

    Attributes:
        _librenms_api (LibreNMSAPI): An instance of the LibreNMSAPI class.

    Properties:
        librenms_api (LibreNMSAPI): A property that returns the LibreNMSAPI instance,
                                    creating it if it doesn't exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._librenms_api = None

    @property
    def librenms_api(self):
        """
        Get or create an instance of LibreNMSAPI.

        This property ensures that only one instance of LibreNMSAPI is created
        and reused for subsequent calls. The API instance will use the currently
        selected server from settings.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            # The LibreNMSAPI will automatically use the selected server
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api

    def _render_server_key(self):
        """
        Resolve the LibreNMS ``server_key`` for cache/query scoping, degrading to ``None``.

        The lazy ``librenms_api`` property builds ``LibreNMSAPI()``, whose constructor raises
        KeyError/ValueError on a missing/misconfigured default server. A cached GET render (any sync
        tab) must not 500 on that: fall back to ``None`` (default scope) so cached rows still render
        and per-server lookups degrade to their bare-key clause instead of crashing during render.

        Shared by the cables and IP-address table views so the degradation lives in one place
        rather than being maintained per view.

        Returns:
            str | None: The active server key, or ``None`` when the client can't be constructed.
        """
        try:
            return self.librenms_api.server_key
        except (KeyError, ValueError):
            return None

    def resolve_requested_server_key(self, data):
        """
        Resolve a request payload's server key, degrading to the active-server resolve.

        Honour an explicitly-posted CONFIGURED key only.
        ``data["server_key"]`` is untrusted. A non-string (e.g. a JSON list) is unhashable and would
        TypeError-500 a later ``cf_dict.get(server_key)``; a stale/forged string must not address
        another server's cache namespace. Only a string that names a configured server is honoured —
        anything else falls back to :meth:`_render_server_key` (default scope). Shared by the device
        verify/render views so this "configured-string-key-or-fallback" rule lives in one place.

        Args:
            data: The parsed request payload (dict-like) carrying an optional ``server_key``.

        Returns:
            str | None: The validated posted key, or the degrading active-server key.
        """
        requested_server_key = data.get("server_key")
        if isinstance(requested_server_key, str) and requested_server_key in LibreNMSAPI.get_available_servers():
            return requested_server_key
        return self._render_server_key()

    def resolve_posted_server_key(self, data):
        """
        Resolve a requested server key for an ACTION path, degrading to the ACTIVE server.

        The module install/replace/bind flows scope cache reads AND write LibreNMS custom-field
        bindings (``custom_field_data["librenms_id"][server_key]``) under this key. A blank fallback
        alone is not enough: a forged *non-blank* key that names no configured server would otherwise be
        honoured and address an unconfigured namespace. Only a configured string key is honoured; blank,
        non-string, or unrecognised falls back to ``self.librenms_api.server_key`` — the active client
        server — so the port-bind still runs (unlike :meth:`resolve_requested_server_key`, which degrades
        to ``None`` for cached GET *renders*).

        Args:
            data: A dict-like request payload (``request.POST`` or ``request.GET``) carrying an optional
                ``server_key``.

        Returns:
            str: The validated posted key, or the active-server key.
        """
        requested_server_key = (data.get("server_key") or "").strip()
        if requested_server_key and requested_server_key in LibreNMSAPI.get_available_servers():
            return requested_server_key
        return self.librenms_api.server_key

    def get_live_device_info(self, librenms_id):
        """
        Fetch LIVE LibreNMS device info for a write path (``use_cache=False``).

        Device-mutating actions (name/serial/type/platform update, legacy-id convert, OOB IP
        resolution) must persist current LibreNMS values, not the possibly-stale sync-tab render
        snapshot cached for up to ``DEVICE_INFO_CACHE_TIMEOUT``. Centralizing this here keeps every
        write path opting out of the cache the same way instead of repeating the rationale inline.

        Args:
            librenms_id: The device's LibreNMS id.

        Returns:
            tuple[bool, dict | None]: ``(success, device_info)`` from ``get_device_info``.
        """
        return self.librenms_api.get_device_info(librenms_id, use_cache=False)

    @property
    def active_server_key(self):
        """
        The server key of the currently-bound API client, or ``"default"``.

        Reads ``self._librenms_api.server_key`` WITHOUT going through the lazy
        :attr:`librenms_api` property — used on rebind-failure render paths where the
        rebind already returned None and constructing a fresh default client (which the
        property would do, and which can raise on a misconfigured default) is exactly
        what must be avoided. Falls back to ``"default"`` when no client is bound yet.

        Returns:
            str: The bound client's resolved server key, or ``"default"``.
        """
        return getattr(getattr(self, "_librenms_api", None), "server_key", None) or "default"

    def render_sync_partial(self, request, obj, server_key, context):
        """
        Render the view's ``partial_template_name`` with migrated-context flags always merged in.

        Every partial-render exit of the sync tab views needs the ``_migrated_to`` marker
        context (``migrated_to_marker`` / ``migrated_to_winner``) so a migrated donor keeps its
        migration controls and doesn't re-expose ordinary sync buttons. Routing all exits through
        this one chokepoint makes the spread impossible to forget on a new error/success branch.

        ``build_migrated_context`` returns ``migrated_to_winner`` as a lazy proxy, so the
        cable/module/VLAN partials (which render only the marker banner, never the winner) don't
        pay the winner ``Device`` lookup on every HTMX refresh.

        Args:
            request: The current HTTP request.
            obj: The device/VM whose migration marker is resolved.
            server_key (str): The server key the marker is namespaced under (use
                :attr:`active_server_key` on the rebind-failure path).
            context (dict): The view-specific partial context (e.g. ``{"vlan_sync": ...}``).

        Returns:
            HttpResponse: The rendered partial.
        """
        from netbox_librenms_plugin.utils import build_migrated_context

        # has_write_permission gates the migrated-donor "Move to winner" controls in the shared
        # inc/_migrate_move_button.html include. Inject it at this chokepoint so EVERY partial
        # render exit (interface/IP/cable/module/VLAN success + error branches) carries it — a
        # caller that omitted it silently collapsed every move button to the disabled read-only
        # branch on an HTMX re-render, even for a user with change permission. Callers therefore
        # don't need to pass it themselves; the `**context` spread comes last only so that if one
        # ever does set it explicitly, that value still wins (defensive — no caller relies on it).
        merged = {"has_write_permission": self.has_write_permission(), **context}
        return render(request, self.partial_template_name, {**merged, **build_migrated_context(obj, server_key)})

    def rebind_api_for_server(self, server_key):
        """
        Rebind ``self.librenms_api`` to the POST-scoped *server_key*.

        Base refresh views run live LibreNMS lookups through ``self.librenms_api``; in a multi-server
        setup the active session server can differ from the tab the user is acting on, so the client
        must be re-scoped to the POSTed key — otherwise data fetched from the session/default server
        is cached under the posted key (wrong cable/VLAN/inventory set). Mirrors
        :meth:`SyncIPAddressesView.post`.

        Args:
            server_key (str | None): The server key from the POST; blank/None falls back to the
                session/default server.

        Returns:
            str | None: The resolved server key, or None when the posted key is
                unknown/misconfigured (stale page or tampered request) so the caller can surface a
                fragment error instead of an unhandled 500.
        """
        from netbox_librenms_plugin.librenms_api import build_librenms_api

        server_key = (server_key or "").strip()
        if not server_key:
            # No posted key: fall back to the session/default server. Reuse an already-built
            # client if present; otherwise build the default via build_librenms_api(None),
            # which returns None on a misconfigured/missing default rather than raising. Going
            # through self.librenms_api here would construct LibreNMSAPI() directly and could
            # raise KeyError/ValueError, defeating this helper's fail-closed None contract.
            cached_api = getattr(self, "_librenms_api", None)
            if cached_api is not None:
                return cached_api.server_key
            api = build_librenms_api(None)
            if api is None:
                return None
            self._librenms_api = api
            return api.server_key

        api = build_librenms_api(server_key)
        if api is None:
            return None
        self._librenms_api = api
        # Return the *resolved* key (build_librenms_api may normalize e.g. "default"
        # to a configured name); downstream cache/OOB scoping must use api.server_key
        # so live fetches and cache writes target the same server.
        return api.server_key

    def resolve_get_render_server_key(self, request):
        """
        Resolve and rebind ``self.librenms_api`` for a GET-render cache read.

        On a full page render the orchestrator delegates to each tab's ``get_context_data``
        without a ``server_key`` and never rebinds the client, so every sync tab must rebind
        itself to the request's ``?server_key`` — otherwise it reads the *default* server's
        cache and renders an empty table right after a successful refresh on another server. A
        blank/absent query falls back to the session/default server, so single-server and
        default-server renders are unchanged.

        Args:
            request: The current request; ``?server_key`` is read from its GET params.

        Returns:
            tuple[str | None, bool]: ``(scoped_key, unresolved)``. ``scoped_key`` is the key
                to scope the cache read to. ``unresolved`` is True when ``?server_key`` named a
                non-blank server that no longer resolves (deleted/misconfigured); a caller that
                wants to short-circuit can render an empty table scoped to ``scoped_key`` rather
                than fall back to the default server's cached data.
        """
        requested = (request.GET.get("server_key") or "").strip()
        resolved = self.rebind_api_for_server(requested)
        if requested and resolved is None:
            return requested, True
        # Blank/missing key with a None resolve means the rebind declined the default
        # (no cached client + misconfigured default). Read the bound client's key directly
        # instead of going through the lazy ``librenms_api`` property, which would reconstruct
        # ``LibreNMSAPI()`` and can re-raise the very misconfiguration the rebind just avoided.
        # ``requested`` is necessarily blank on this branch (a non-blank ``requested`` with
        # ``resolved is None`` already returned above), so the bound client's key is the only
        # reachable fallback.
        scoped = resolved if resolved is not None else getattr(getattr(self, "_librenms_api", None), "server_key", None)
        return scoped, False

    def get_server_info(self):
        """
        Get information about the currently active LibreNMS server.

        Returns:
            dict: Server information including display name and URL
        """
        try:
            # Get the current server key
            server_key = self.librenms_api.server_key

            # Try to get multi-server configuration
            from netbox.plugins import get_plugin_config

            servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

            if servers_config and isinstance(servers_config, dict) and server_key in servers_config:
                # Multi-server configuration
                config = servers_config[server_key]
                return {
                    "display_name": config.get("display_name", server_key),
                    "url": config["librenms_url"],
                    "is_legacy": False,
                    "server_key": server_key,
                }
            else:
                # Legacy configuration
                legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
                return {
                    "display_name": "Default Server",
                    "url": legacy_url or "Not configured",
                    "is_legacy": True,
                    "server_key": "default",
                }
        except (KeyError, AttributeError, ImportError, ValueError):
            # ValueError: reading self.librenms_api with no client bound reconstructs
            # LibreNMSAPI(), which raises on a misconfigured default. That happens on the
            # degraded render paths (stale ?server_key + broken default), where the header
            # must show the configuration error rather than 500 the page.
            return {
                "display_name": "Unknown Server",
                "url": "Configuration error",
                "is_legacy": True,
                "server_key": "unknown",
            }

    def get_context_data(self, **kwargs):
        """Add server info to context for all views using this mixin."""
        try:
            context = super().get_context_data(**kwargs)
        except AttributeError:
            context = kwargs
        context["librenms_server_info"] = self.get_server_info()
        return context


class CacheMixin:
    """
    A mixin class that provides caching functionality.
    """

    def get_cache_key(self, obj, data_type="ports", server_key=None):
        """
        Get the cache key for the object.

        Args:
            obj: The object to cache data for
            data_type: Type of data being cached ('ports', 'links', 'inventory', etc.)
            server_key: Optional LibreNMS server key for namespacing per-server data
        """
        model_name = obj._meta.model_name
        base = f"librenms_{data_type}_{model_name}_{obj.pk}"
        if server_key:
            return f"{base}_{server_key}"
        return base

    def get_last_fetched_key(self, obj, data_type="ports", server_key=None):
        """
        Get the cache key for the last fetched time of the object.
        """
        model_name = obj._meta.model_name
        base = f"librenms_{data_type}_last_fetched_{model_name}_{obj.pk}"
        if server_key:
            return f"{base}_{server_key}"
        return base

    def get_vlan_overrides_key(self, obj, server_key=None):
        """
        Get the cache key for user VLAN group override selections.

        Stores a {vid_str: group_id_str} map so that "apply to all" VLAN
        group choices persist across table pages. Including server_key scopes
        overrides per-server to avoid leakage when multiple servers are configured.
        """
        model_name = obj._meta.model_name
        if server_key:
            return f"librenms_vlan_group_overrides_{model_name}_{obj.pk}_{server_key}"
        return f"librenms_vlan_group_overrides_{model_name}_{obj.pk}"


class VlanAssignmentMixin:
    """
    Mixin providing VLAN assignment utilities for views.

    Provides methods for:
    - Getting relevant VLAN groups for a device based on scope hierarchy
    - Building lookup maps for VLAN matching
    - Selecting the most specific VLAN group based on device context
    - Finding VLANs by VID within a specific group
    - Updating interface VLAN assignments
    """

    def get_vlan_groups_for_device(self, device):
        """Get all VLAN groups relevant to one device."""
        return self.get_vlan_groups_for_devices([device])

    def get_vlan_groups_for_devices(self, devices):
        """
        Get all VLAN groups relevant to a set of devices.

        Searches for VLAN groups scoped to:
        - Site: Each device's assigned site
        - Location: Each device's location and all parent locations
        - Region: Each device site's region and all parent regions
        - Site Group: Each device site's group and all parent site groups
        - Rack: Each device's rack
        - Global: VLAN groups with no scope

        Returns:
            List of VLANGroup objects, deduplicated and sorted by name
        """
        from dcim.models import Location, Rack, Region, Site, SiteGroup
        from ipam.models import VLANGroup

        sites = set()
        locations = set()
        regions = set()
        site_groups = set()
        racks = set()
        for device in devices:
            site = getattr(device, "site", None)
            if site is not None:
                sites.add(site)
                if site.region:
                    regions.update(self._get_ancestors(site.region))
                if site.group:
                    site_groups.update(self._get_ancestors(site.group))
            location = getattr(device, "location", None)
            if location is not None:
                locations.update(self._get_ancestors(location))
            rack = getattr(device, "rack", None)
            if rack is not None:
                racks.add(rack)

        groups = set()
        groups.update(self._get_vlan_groups_for_scope(Site, sites))
        groups.update(self._get_vlan_groups_for_scope(Location, locations))
        groups.update(self._get_vlan_groups_for_scope(Region, regions))
        groups.update(self._get_vlan_groups_for_scope(SiteGroup, site_groups))
        groups.update(self._get_vlan_groups_for_scope(Rack, racks))

        # Global VLAN groups (no scope)
        global_groups = VLANGroup.objects.filter(scope_type__isnull=True)
        groups.update(global_groups)

        # Return sorted by name for consistent display
        return sorted(groups, key=lambda g: g.name.lower())

    def filter_vlan_groups_for_device(self, vlan_groups, device):
        """Restrict a preloaded VLAN group union to the scopes relevant to one device."""
        from dcim.models import Location, Rack, Region, Site, SiteGroup
        from django.contrib.contenttypes.models import ContentType

        scope_keys = set()

        def add_scope(model, objects):
            content_type_id = ContentType.objects.get_for_model(model).pk
            scope_keys.update((content_type_id, obj.pk) for obj in objects if obj is not None)

        site = getattr(device, "site", None)
        add_scope(Site, [site])
        add_scope(Region, self._get_ancestors(site.region) if site and site.region else [])
        add_scope(SiteGroup, self._get_ancestors(site.group) if site and site.group else [])
        location = getattr(device, "location", None)
        add_scope(Location, self._get_ancestors(location) if location else [])
        add_scope(Rack, [getattr(device, "rack", None)])

        return [
            group
            for group in vlan_groups
            if group.scope_type_id is None or (group.scope_type_id, group.scope_id) in scope_keys
        ]

    def _build_vlan_lookup_maps(self, vlan_groups):
        """
        Build lookup dictionaries for VLAN matching.

        Returns a dict with:
        - vid_to_groups: {vid: [vlan_group, ...]} - VID to groups containing that VID
        - vid_group_to_vlan: {(vid, group_id): vlan} - unique per group lookup
        - vid_to_vlans: {vid: [vlan, ...]} - all VLANs with that VID
        - vid_name_to_vlan: {(vid, name): vlan} - VID + name lookup
        """
        from ipam.models import VLAN

        # Get all VLANs from relevant groups and global VLANs
        group_pks = [g.pk for g in vlan_groups]
        vlans = VLAN.objects.filter(group__pk__in=group_pks).select_related("group")
        # Also get global VLANs (no group)
        global_vlans = VLAN.objects.filter(group__isnull=True)
        return self._index_vlans([*vlans, *global_vlans])

    @staticmethod
    def _index_vlans(vlans):
        """Build VLAN lookup dictionaries from an already loaded VLAN iterable."""
        vid_to_groups = {}
        vid_group_to_vlan = {}
        vid_to_vlans = {}
        vid_name_to_vlan = {}

        for vlan in vlans:
            vid = vlan.vid
            group = vlan.group
            group_id = group.pk if group else None
            name = vlan.name

            # Build VID to groups lookup for ambiguity detection (group VLANs only)
            if group:
                if vid not in vid_to_groups:
                    vid_to_groups[vid] = []
                if group not in vid_to_groups[vid]:
                    vid_to_groups[vid].append(group)

            # Build (vid, group_id) to vlan lookup
            vid_group_to_vlan[(vid, group_id)] = vlan

            # Build VID to all VLANs list (for dropdown options)
            if vid not in vid_to_vlans:
                vid_to_vlans[vid] = []
            vid_to_vlans[vid].append(vlan)

            # Build (vid, name) to vlan lookup
            vid_name_to_vlan[(vid, name)] = vlan

        return {
            "vid_to_groups": vid_to_groups,
            "vid_group_to_vlan": vid_group_to_vlan,
            "vid_to_vlans": vid_to_vlans,
            "vid_name_to_vlan": vid_name_to_vlan,
        }

    def restrict_vlan_lookup_maps(self, lookup_maps, vlan_groups):
        """Restrict union lookup maps to the supplied device-relevant groups and globals."""
        group_ids = {group.pk for group in vlan_groups}
        vlans = {
            vlan.pk: vlan
            for candidates in lookup_maps.get("vid_to_vlans", {}).values()
            for vlan in candidates
            if vlan.group_id is None or vlan.group_id in group_ids
        }
        return self._index_vlans(vlans.values())

    def _add_vlan_group_selection(self, port, lookup_maps, device, vlan_group_overrides=None):
        """
        Add per-VLAN group auto-selection data to port record.

        Sets:
        - vlan_group_map: {vid: {"group_id": str, "group_name": str, "is_ambiguous": bool}}
          Maps each VID to its auto-selected VLAN group based on scope hierarchy.
          If vlan_group_overrides contains a user selection for a VID, that takes
          precedence over auto-selection.
        """
        vid_to_groups = lookup_maps.get("vid_to_groups", {})
        untagged_vid = port.get("untagged_vlan")
        tagged_vids = port.get("tagged_vlans", [])

        all_vids = []
        if untagged_vid:
            all_vids.append(untagged_vid)
        all_vids.extend(tagged_vids)

        vlan_group_map = {}
        for vid in all_vids:
            groups = vid_to_groups.get(vid, [])
            if len(groups) == 1:
                vlan_group_map[vid] = {
                    "group_id": str(groups[0].pk),
                    "group_name": groups[0].name,
                    "is_ambiguous": False,
                }
            elif len(groups) > 1:
                most_specific = self._select_most_specific_group(groups, device)
                if most_specific:
                    vlan_group_map[vid] = {
                        "group_id": str(most_specific.pk),
                        "group_name": most_specific.name,
                        "is_ambiguous": False,
                    }
                else:
                    vlan_group_map[vid] = {
                        "group_id": "",
                        "group_name": "Ambiguous",
                        "is_ambiguous": True,
                    }
            else:
                vlan_group_map[vid] = {
                    "group_id": "",
                    "group_name": "Global",
                    "is_ambiguous": False,
                }

        # Apply user overrides from "apply to all" selections (persisted in cache)
        if vlan_group_overrides:
            from ipam.models import VLANGroup

            # Batch-fetch all referenced override group IDs to avoid N+1 queries
            override_group_ids = {
                group_id
                for vid in all_vids
                if str(vid) in vlan_group_overrides
                and (group_id := coerce_model_pk(vlan_group_overrides[str(vid)])) is not None
            }
            override_groups_by_id = {}
            if override_group_ids:
                override_groups_by_id = VLANGroup.objects.in_bulk(list(override_group_ids))

            for vid in all_vids:
                vid_str = str(vid)
                if vid_str in vlan_group_overrides:
                    raw_override_group_id = vlan_group_overrides[vid_str]
                    override_group_id = coerce_model_pk(raw_override_group_id)
                    if override_group_id is not None:
                        group = override_groups_by_id.get(override_group_id)
                        # The row's in-scope groups, not only groups that already carry the VID:
                        # "apply to all" exists to put the VLAN into a group that lacks it.
                        allowed_group_ids = {candidate.pk for candidate in port.get("vlan_groups", [])}
                        if group and group.pk in allowed_group_ids:
                            vlan_group_map[vid] = {
                                "group_id": str(group.pk),
                                "group_name": group.name,
                                "is_ambiguous": False,
                            }
                        # Keep auto-selection when the group was deleted or is out of the row's scope.
                    elif raw_override_group_id == "" and (vid, None) in lookup_maps.get("vid_group_to_vlan", {}):
                        # User explicitly chose "No Group (Global)"
                        vlan_group_map[vid] = {
                            "group_id": "",
                            "group_name": "Global",
                            "is_ambiguous": False,
                        }

        port["vlan_group_map"] = vlan_group_map

    def _add_missing_vlans_info(self, port, lookup_maps):
        """
        Add missing VLANs info to port record for warning display.

        Sets:
        - missing_vlans: List of VIDs not found in any NetBox VLAN group
        """
        vid_to_vlans = lookup_maps.get("vid_to_vlans", {})
        missing_vlans = []

        untagged_vid = port.get("untagged_vlan")
        tagged_vids = port.get("tagged_vlans", [])

        if untagged_vid and untagged_vid not in vid_to_vlans:
            missing_vlans.append(untagged_vid)

        for vid in tagged_vids:
            if vid not in vid_to_vlans:
                missing_vlans.append(vid)

        port["missing_vlans"] = missing_vlans

    def _select_most_specific_group(self, groups, device):
        """
        Select the most specific VLAN group based on device context.

        Priority order (most specific to least specific):
        1. Rack-scoped (device's rack)
        2. Location-scoped (device's location, closer ancestors win)
        3. Site-scoped (device's site)
        4. Site Group-scoped (device's site's group, closer ancestors win)
        5. Region-scoped (device's site's region, closer ancestors win)
        6. Global (no scope)

        Args:
            groups: List of VLANGroup objects that all contain the same VID
            device: NetBox Device object

        Returns:
            VLANGroup or None if no clear winner (e.g., multiple groups at same priority level)
        """
        from dcim.models import Location, Rack, Region, Site, SiteGroup
        from django.contrib.contenttypes.models import ContentType

        if not device or not groups:
            return None

        # Build scope priority lookup for this device
        # Lower number = higher priority (more specific)
        scope_priority = {}
        priority = 0

        # Priority 1: Rack (most specific)
        if hasattr(device, "rack") and device.rack:
            rack_ct = ContentType.objects.get_for_model(Rack)
            scope_priority[(rack_ct.pk, device.rack.pk)] = priority
            priority += 1

        # Priority 2: Location hierarchy (device's location first, then ancestors)
        if hasattr(device, "location") and device.location:
            location_ct = ContentType.objects.get_for_model(Location)
            for loc in self._get_ancestors(device.location):
                scope_priority[(location_ct.pk, loc.pk)] = priority
                priority += 1

        # Priority 3: Site
        if hasattr(device, "site") and device.site:
            site_ct = ContentType.objects.get_for_model(Site)
            scope_priority[(site_ct.pk, device.site.pk)] = priority
            priority += 1

            # Priority 4: Site Group hierarchy
            if device.site.group:
                site_group_ct = ContentType.objects.get_for_model(SiteGroup)
                for sg in self._get_ancestors(device.site.group):
                    scope_priority[(site_group_ct.pk, sg.pk)] = priority
                    priority += 1

            # Priority 5: Region hierarchy
            if device.site.region:
                region_ct = ContentType.objects.get_for_model(Region)
                for reg in self._get_ancestors(device.site.region):
                    scope_priority[(region_ct.pk, reg.pk)] = priority
                    priority += 1

        # Priority 6: Global (no scope) - lowest priority
        global_priority = priority

        # Find the group with the highest priority (lowest number)
        best_group = None
        best_priority = float("inf")
        same_priority_count = 0

        for group in groups:
            if group.scope_type is None:
                # Global scope
                group_priority = global_priority
            else:
                scope_key = (group.scope_type.pk, group.scope_id)
                group_priority = scope_priority.get(scope_key, float("inf"))

            if group_priority < best_priority:
                best_priority = group_priority
                best_group = group
                same_priority_count = 1
            elif group_priority == best_priority:
                same_priority_count += 1

        # Only return a group if there's a single winner at the best priority level
        if same_priority_count == 1 and best_group is not None:
            return best_group

        return None

    def _get_ancestors(self, obj):
        """
        Get all ancestors of a hierarchical object (location, region, site group).
        Returns list including the object itself and all parents up to root.
        """
        ancestors = []
        current = obj
        while current is not None:
            ancestors.append(current)
            current = getattr(current, "parent", None)
        return ancestors

    def _get_vlan_groups_for_scope(self, model_class, objects):
        """
        Get VLAN groups scoped to any of the given objects.

        Args:
            model_class: The Django model class (Site, Location, Region, etc.)
            objects: List of model instances to check

        Returns:
            QuerySet of VLANGroup objects
        """
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        if not objects:
            return VLANGroup.objects.none()

        content_type = ContentType.objects.get_for_model(model_class)
        object_ids = [obj.pk for obj in objects if obj is not None and obj.pk is not None]

        if not object_ids:
            return VLANGroup.objects.none()

        return VLANGroup.objects.filter(scope_type=content_type, scope_id__in=object_ids)

    def _find_vlan_in_group(self, vid, vlan_group_id, lookup_maps):
        """
        Find a VLAN by VID, preferring the specified group.

        Args:
            vid: VLAN ID (integer)
            vlan_group_id: Optional VLAN group ID to prefer
            lookup_maps: Dict from _build_vlan_lookup_maps()

        Returns:
            VLAN object or None
        """
        vid_group_to_vlan = lookup_maps.get("vid_group_to_vlan", {})
        vid_to_vlans = lookup_maps.get("vid_to_vlans", {})

        # Try specific group first
        if vlan_group_id:
            try:
                vlan = vid_group_to_vlan.get((vid, int(vlan_group_id)))
                if vlan:
                    return vlan
            except (ValueError, TypeError):
                pass

        # Try global (no group)
        vlan = vid_group_to_vlan.get((vid, None))
        if vlan:
            return vlan

        # Fallback: first matching VLAN
        vlans = vid_to_vlans.get(vid, [])
        return vlans[0] if vlans else None

    def _update_interface_vlan_assignment(self, interface, vlan_data, vlan_group_map, lookup_maps):
        """
        Update interface VLAN assignments in NetBox (mode, untagged_vlan, tagged_vlans).

        Args:
            interface: NetBox Interface or VMInterface object
            vlan_data: Dict with 'untagged_vlan' (int or None) and 'tagged_vlans' (list of ints)
            vlan_group_map: Dict mapping VID (str) to VLAN group ID for per-VLAN group lookups.
                           Can also be a single group ID string for backward compat.
            lookup_maps: Dict from _build_vlan_lookup_maps()

        Returns:
            Dict with sync results:
                - mode_set: str or None
                - untagged_set: VLAN object or None
                - tagged_set: list of VLAN objects
                - missing_vlans: list of VIDs not found in NetBox
        """
        # Support both dict (per-VLAN) and string/int/None (single group) for backward compat
        if not isinstance(vlan_group_map, dict):
            single_group_id = vlan_group_map
            vlan_group_map = None
        else:
            single_group_id = None

        untagged_vid = vlan_data.get("untagged_vlan")
        tagged_vids = vlan_data.get("tagged_vlans", [])
        missing_vlans = []

        def _get_group_id_for_vid(vid):
            """Resolve the VLAN group ID for a specific VID."""
            if vlan_group_map is not None:
                return vlan_group_map.get(str(vid), "")
            return single_group_id or ""

        # Determine mode
        if tagged_vids:
            interface.mode = "tagged"
        elif untagged_vid:
            interface.mode = "access"
        else:
            # No VLANs - clear mode
            interface.mode = ""

        # Set untagged VLAN
        untagged_set = None
        if untagged_vid:
            vlan = self._find_vlan_in_group(untagged_vid, _get_group_id_for_vid(untagged_vid), lookup_maps)
            if vlan:
                interface.untagged_vlan = vlan
                untagged_set = vlan
            else:
                missing_vlans.append(untagged_vid)
                interface.untagged_vlan = None
        else:
            interface.untagged_vlan = None

        # Save mode + untagged_vlan before M2M operations.
        # tagged_vlans.set() triggers a DB refresh that wipes unsaved
        # in-memory attributes, so we must persist first.
        interface.save()

        # Set tagged VLANs (M2M - requires the instance to be saved first)
        tagged_set = []
        if tagged_vids:
            for vid in tagged_vids:
                vlan = self._find_vlan_in_group(vid, _get_group_id_for_vid(vid), lookup_maps)
                if vlan:
                    tagged_set.append(vlan)
                else:
                    missing_vlans.append(vid)
            interface.tagged_vlans.set(tagged_set)
        else:
            interface.tagged_vlans.clear()

        return {
            "mode_set": interface.mode,
            "untagged_set": untagged_set,
            "tagged_set": tagged_set,
            "missing_vlans": missing_vlans,
        }
