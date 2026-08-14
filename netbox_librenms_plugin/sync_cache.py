"""Shared cache-consistency contract for the five device sync tabs."""

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from django.core.cache import cache
from django.contrib import messages
from django.db import transaction
from django.shortcuts import render
from django.utils import timezone

from netbox_librenms_plugin.utils import (
    coerce_librenms_id,
    get_librenms_device_id,
    get_librenms_sync_device,
    resolve_server_mapping_display_id,
)

logger = logging.getLogger(__name__)


class SyncTab(StrEnum):
    """The cache-backed tabs on a LibreNMS object sync page."""

    INTERFACES = "interfaces"
    CABLES = "cables"
    IP_ADDRESSES = "ipaddresses"
    MODULES = "modules"
    VLANS = "vlans"


class SyncTabState(StrEnum):
    """The observable state of one tab snapshot."""

    READY = "ready"
    INVALIDATED = "invalidated"
    REFRESH_FAILED = "refresh_failed"
    LOCALLY_CHANGED = "locally_changed"


@dataclass(frozen=True)
class SyncTabSpec:
    """Describe one tab's cache payload and object support."""

    data_type: str
    label: str
    shared_vc_owner: bool
    supports_virtual_machine: bool
    has_last_fetched_key: bool = False
    has_vlan_overrides: bool = False
    has_cable_picks: bool = False


TAB_SPECS = {
    SyncTab.INTERFACES: SyncTabSpec(
        data_type="ports",
        label="Interface",
        shared_vc_owner=True,
        supports_virtual_machine=True,
        has_last_fetched_key=True,
        has_vlan_overrides=True,
    ),
    SyncTab.CABLES: SyncTabSpec(
        data_type="links",
        label="Cable",
        shared_vc_owner=True,
        supports_virtual_machine=False,
        has_cable_picks=True,
    ),
    SyncTab.IP_ADDRESSES: SyncTabSpec(
        data_type="ip_addresses",
        label="IP address",
        shared_vc_owner=False,
        supports_virtual_machine=True,
    ),
    SyncTab.MODULES: SyncTabSpec(
        data_type="inventory",
        label="Module",
        shared_vc_owner=True,
        supports_virtual_machine=False,
    ),
    SyncTab.VLANS: SyncTabSpec(
        data_type="vlans",
        label="VLAN",
        shared_vc_owner=False,
        supports_virtual_machine=False,
        has_last_fetched_key=True,
    ),
}


@dataclass
class CacheMutationTransition:
    """Report the cache work that ran after a committed NetBox mutation."""

    transition_id: str = field(default_factory=lambda: uuid4().hex)
    removed_tabs: set[tuple[str, SyncTab]] = field(default_factory=set)
    affected_tabs: set[tuple[str, SyncTab]] = field(default_factory=set)
    revisions: dict[tuple[str, SyncTab], str] = field(default_factory=dict)
    cleanup_tabs: set[SyncTab] = field(default_factory=set)
    source_tab: SyncTab | None = None
    source_fragment_required: bool = False
    error: str | None = None
    completed: bool = False

    @property
    def removed_any(self):
        """Return whether at least one prior snapshot or temporary value was removed."""
        return bool(self.removed_tabs)

    def browser_payload(self):
        """Serialize only information the initiating browser needs."""
        return {
            "transition_id": self.transition_id,
            "removed": self.removed_any,
            "cleanup_failed": self.error is not None,
            "tabs": sorted({tab.value for _server_key, tab in self.removed_tabs}),
            "cleanup_tabs": sorted(tab.value for tab in self.cleanup_tabs),
            "source_tab": self.source_tab.value if self.source_tab is not None else None,
            "source_fragment_required": self.source_fragment_required,
            "revisions": {
                f"{server_key}:{tab.value}": revision
                for (server_key, tab), revision in sorted(
                    self.revisions.items(), key=lambda item: (item[0][0], item[0][1].value)
                )
            },
        }


def request_actor_id(request):
    """Return an optional integer user ID from a concrete or middleware request."""
    user = getattr(request, "user", None)
    actor_id = getattr(user, "pk", None)
    return actor_id if isinstance(actor_id, int) and not isinstance(actor_id, bool) else None


def sync_snapshot_key(obj, data_type, server_key=None):
    """Return the shared snapshot key for one object, data type, and server."""
    base = f"librenms_{data_type}_{obj._meta.model_name}_{obj.pk}"
    return f"{base}_{server_key}" if server_key else base


def sync_last_fetched_key(obj, data_type, server_key=None):
    """Return the shared last-fetched key for one object, data type, and server."""
    base = f"librenms_{data_type}_last_fetched_{obj._meta.model_name}_{obj.pk}"
    return f"{base}_{server_key}" if server_key else base


def sync_vlan_overrides_key(obj, server_key=None):
    """Return the VLAN override key for one object and server."""
    base = f"librenms_vlan_group_overrides_{obj._meta.model_name}_{obj.pk}"
    return f"{base}_{server_key}" if server_key else base


def _state_key(page_object, server_key, tab):
    return f"librenms_sync_tab_state_{page_object._meta.model_name}_{page_object.pk}_{server_key}_{tab.value}"


def _explicit_server_keys(obj):
    raw_mapping = getattr(obj, "custom_field_data", {}).get("librenms_id")
    if not isinstance(raw_mapping, dict):
        return set()
    return {
        str(server_key)
        for server_key, entry in raw_mapping.items()
        if isinstance(server_key, str) and server_key and resolve_server_mapping_display_id(entry)[0] is not None
    }


def mapped_server_keys(page_object, active_server_key=None):
    """Return only server namespaces explicitly linked to the page or its VC."""
    objects = [page_object]
    virtual_chassis = getattr(page_object, "virtual_chassis", None)
    if virtual_chassis is not None:
        objects = list(virtual_chassis.members.all())

    server_keys = set()
    for obj in objects:
        server_keys.update(_explicit_server_keys(obj))

    if active_server_key and active_server_key not in server_keys:
        sync_owner = get_librenms_sync_device(page_object, server_key=active_server_key) or page_object
        raw_mapping = getattr(sync_owner, "custom_field_data", {}).get("librenms_id")
        if not isinstance(raw_mapping, dict) and coerce_librenms_id(raw_mapping) is not None:
            server_keys.add(active_server_key)
        elif get_librenms_device_id(sync_owner, active_server_key, auto_save=False) is not None:
            server_keys.add(active_server_key)

    return tuple(sorted(server_keys))


class SyncCacheConsistency:
    """Own sync-tab cache keys, transitions, and status serialization."""

    def __init__(self, page_object, *, cache_timeout):
        self.page_object = page_object
        self.cache_timeout = cache_timeout

    def applicable_tabs(self):
        """Return the tabs supported by this page object's model."""
        is_virtual_machine = self.page_object._meta.model_name == "virtualmachine"
        return tuple(tab for tab, spec in TAB_SPECS.items() if not is_virtual_machine or spec.supports_virtual_machine)

    def _shared_owner(self, server_key):
        if self.page_object._meta.model_name != "device":
            return self.page_object
        return get_librenms_sync_device(self.page_object, server_key=server_key) or self.page_object

    def _primary_owner(self, tab, server_key):
        return self._shared_owner(server_key) if TAB_SPECS[tab].shared_vc_owner else self.page_object

    def _candidate_owners(self, tab, server_key):
        owners = {self.page_object.pk: self.page_object}
        if TAB_SPECS[tab].shared_vc_owner:
            shared_owner = self._shared_owner(server_key)
            owners[shared_owner.pk] = shared_owner
        return tuple(owners.values())

    def snapshot_key(self, tab, server_key):
        """Return the key read by the tab for this page and server."""
        spec = TAB_SPECS[tab]
        return sync_snapshot_key(self._primary_owner(tab, server_key), spec.data_type, server_key)

    def state_key(self, tab, server_key):
        """Return the page-scoped state key for one tab and server."""
        return _state_key(self.page_object, server_key, tab)

    def _state_record(self, state, tab, source_tab, actor_id, reason, revision=None):
        stored_actor_id = actor_id if isinstance(actor_id, int) and not isinstance(actor_id, bool) else None
        return {
            "revision": revision or uuid4().hex,
            "state": state.value,
            "source_tab": source_tab.value if source_tab is not None else tab.value,
            "actor_id": stored_actor_id,
            "timestamp": timezone.now().isoformat(),
            "reason": reason,
        }

    def _set_state(
        self,
        tab,
        server_key,
        state,
        *,
        source_tab=None,
        actor_id=None,
        reason=None,
        revision=None,
    ):
        record = self._state_record(state, tab, source_tab, actor_id, reason, revision)
        cache.set(self.state_key(tab, server_key), record, timeout=self.cache_timeout)
        return record

    def mark_refresh_success(self, tab, server_key, *, actor_id=None):
        """Publish a new ready revision after an explicit successful refresh."""
        return self._set_state(tab, server_key, SyncTabState.READY, actor_id=actor_id)

    def mark_refresh_failure(self, tab, server_key, *, actor_id=None):
        """Record a failed refresh unless an earlier invalidation reason must remain."""
        prior = cache.get(self.state_key(tab, server_key))
        if isinstance(prior, dict) and prior.get("state") == SyncTabState.INVALIDATED.value:
            prior = {
                **prior,
                "refresh_error": "The latest LibreNMS refresh failed.",
                "refresh_error_timestamp": timezone.now().isoformat(),
            }
            cache.set(self.state_key(tab, server_key), prior, timeout=self.cache_timeout)
            return prior
        reason = (
            f"Cached {TAB_SPECS[tab].label} data is unavailable because another user attempted "
            "to refresh it from LibreNMS and the refresh failed."
        )
        return self._set_state(
            tab,
            server_key,
            SyncTabState.REFRESH_FAILED,
            source_tab=tab,
            actor_id=actor_id,
            reason=reason,
        )

    def schedule_mutation(
        self,
        source_tab,
        active_server_key,
        *,
        actor_id=None,
        source_fragment_required=False,
    ):
        """Invalidate dependent snapshots only after the current transaction commits."""
        transition = CacheMutationTransition(
            cleanup_tabs=set(self.applicable_tabs()) - {source_tab},
            source_tab=source_tab,
            source_fragment_required=source_fragment_required,
        )

        def apply_transition():
            try:
                self._apply_mutation(source_tab, active_server_key, actor_id, transition)
            except Exception:
                logger.exception("Committed sync mutation could not clean related tab caches")
                transition.error = "Cache cleanup failed. Reload this sync page before continuing."
                try:
                    self._mark_cleanup_failure_states(
                        source_tab,
                        active_server_key,
                        actor_id,
                        transition,
                    )
                except Exception:
                    logger.exception("Committed sync mutation could not publish fail-closed cache states")
            finally:
                transition.completed = True

        connection = transaction.get_connection()
        if connection.in_atomic_block:
            transaction.on_commit(apply_transition)
        else:
            apply_transition()
        return transition

    def _delete_pattern(self, pattern):
        return int(cache.delete_pattern(pattern) or 0)

    @staticmethod
    def _pattern_has_values(pattern):
        """Return whether a snapshot-bound wildcard key currently exists."""
        iter_keys = getattr(cache, "iter_keys", None)
        if callable(iter_keys):
            return next(iter_keys(pattern), None) is not None
        keys = getattr(cache, "keys", None)
        if callable(keys):
            return bool(keys(pattern))
        # A backend without wildcard inspection must still receive the delete request.
        return True

    def _tab_has_values(self, tab, server_key):
        """Return whether a tab has a snapshot or snapshot-bound temporary state."""
        spec = TAB_SPECS[tab]
        for owner in self._candidate_owners(tab, server_key):
            snapshot_key = sync_snapshot_key(owner, spec.data_type, server_key)
            keys = {snapshot_key}
            if spec.has_last_fetched_key:
                keys.add(sync_last_fetched_key(owner, spec.data_type, server_key))
            if spec.has_vlan_overrides:
                keys.add(sync_vlan_overrides_key(owner, server_key))
            if any(cache.has_key(key) for key in keys):
                return True
            if spec.has_cable_picks and self._pattern_has_values(f"{snapshot_key}:manual-remote:*"):
                return True
        return False

    def _delete_tab_values(self, tab, server_key, *, preserve_key=None):
        spec = TAB_SPECS[tab]
        keys = set()
        patterns = set()
        for owner in self._candidate_owners(tab, server_key):
            snapshot_key = sync_snapshot_key(owner, spec.data_type, server_key)
            if snapshot_key != preserve_key:
                keys.add(snapshot_key)
                if spec.has_last_fetched_key:
                    keys.add(sync_last_fetched_key(owner, spec.data_type, server_key))
                if spec.has_vlan_overrides:
                    keys.add(sync_vlan_overrides_key(owner, server_key))
                if spec.has_cable_picks:
                    patterns.add(f"{snapshot_key}:manual-remote:*")

        existing = {key for key in keys if cache.has_key(key)}
        if existing:
            cache.delete_many(existing)
        removed_patterns = sum(
            self._delete_pattern(pattern) for pattern in patterns if self._pattern_has_values(pattern)
        )
        return bool(existing or removed_patterns)

    def _apply_mutation(self, source_tab, active_server_key, actor_id, transition):
        mapped_keys = mapped_server_keys(self.page_object, active_server_key)
        if active_server_key not in mapped_keys:
            raise ValueError(
                f"Server {active_server_key!r} is not mapped to "
                f"{self.page_object._meta.label_lower} {self.page_object.pk}."
            )

        transition.affected_tabs = {
            (server_key, tab)
            for server_key in mapped_keys
            for tab in self.applicable_tabs()
            if self._tab_has_values(tab, server_key)
        }

        mutation_revision = transition.transition_id
        for server_key in mapped_keys:
            for tab in self.applicable_tabs():
                is_source = server_key == active_server_key and tab == source_tab
                preserve_key = self.snapshot_key(tab, server_key) if is_source else None
                removed = self._delete_tab_values(tab, server_key, preserve_key=preserve_key)
                if is_source:
                    record = self._set_state(
                        tab,
                        server_key,
                        SyncTabState.LOCALLY_CHANGED,
                        source_tab=source_tab,
                        actor_id=actor_id,
                        reason=None,
                        revision=mutation_revision,
                    )
                    transition.revisions[(server_key, tab)] = record["revision"]
                elif removed:
                    reason = (
                        f"Cached {TAB_SPECS[tab].label} data was cleared because "
                        f"{TAB_SPECS[source_tab].label} data was synchronized from LibreNMS."
                    )
                    record = self._set_state(
                        tab,
                        server_key,
                        SyncTabState.INVALIDATED,
                        source_tab=source_tab,
                        actor_id=actor_id,
                        reason=reason,
                        revision=mutation_revision,
                    )
                    transition.removed_tabs.add((server_key, tab))
                    transition.revisions[(server_key, tab)] = record["revision"]

    def _mark_cleanup_failure_states(self, source_tab, active_server_key, actor_id, transition):
        """Publish one fail-closed revision after incomplete cache cleanup."""
        mapped_keys = mapped_server_keys(self.page_object, active_server_key)
        if active_server_key not in mapped_keys:
            return

        mutation_revision = transition.transition_id
        for server_key in mapped_keys:
            for tab in self.applicable_tabs():
                is_source = server_key == active_server_key and tab == source_tab
                if is_source:
                    state = SyncTabState.LOCALLY_CHANGED
                    reason = None
                else:
                    if (server_key, tab) not in transition.affected_tabs:
                        continue
                    state = SyncTabState.INVALIDATED
                    reason = (
                        f"Cached {TAB_SPECS[tab].label} data is unavailable because cache cleanup "
                        f"did not complete after {TAB_SPECS[source_tab].label} data was synchronized."
                    )
                    transition.removed_tabs.add((server_key, tab))
                record = self._set_state(
                    tab,
                    server_key,
                    state,
                    source_tab=source_tab,
                    actor_id=actor_id,
                    reason=reason,
                    revision=mutation_revision,
                )
                transition.revisions[(server_key, tab)] = record["revision"]

    def status(self, server_key, *, actor_id=None):
        """Inspect tab state and actual payload keys without contacting LibreNMS."""
        result = {}
        for tab in self.applicable_tabs():
            record = cache.get(self.state_key(tab, server_key))
            snapshot_exists = cache.has_key(self.snapshot_key(tab, server_key))
            if not isinstance(record, dict):
                record = {}
            timestamp = record.get("timestamp")
            state = record.get("state")
            same_user = bool(actor_id is not None and record.get("actor_id") == actor_id)
            reason = record.get("reason")
            if state == SyncTabState.REFRESH_FAILED.value and same_user:
                reason = (
                    f"Cached {TAB_SPECS[tab].label} data is unavailable because your latest LibreNMS refresh failed."
                )
            snapshot_available = snapshot_exists and state not in {
                SyncTabState.INVALIDATED.value,
                SyncTabState.REFRESH_FAILED.value,
            }
            if not snapshot_exists and state in {
                SyncTabState.READY.value,
                SyncTabState.LOCALLY_CHANGED.value,
            }:
                state = "missing"
            result[tab.value] = {
                "revision": record.get("revision"),
                "state": state or (SyncTabState.READY.value if snapshot_available else "missing"),
                "source_tab": record.get("source_tab"),
                "timestamp": timestamp,
                "reason": reason,
                "refresh_error": record.get("refresh_error"),
                "refresh_error_timestamp": record.get("refresh_error_timestamp"),
                "same_user": same_user,
                "snapshot_available": snapshot_available,
            }
        return result


def apply_transition_to_response(request, response, transition):
    """Attach one mutation transition to the endpoint's existing response style."""
    if transition is None:
        return response

    payload = transition.browser_payload()
    response["X-LibreNMS-Cache-Transition"] = json.dumps(payload, separators=(",", ":"))
    browser_navigation = bool(response.get("Location") or response.get("HX-Redirect"))
    if request.headers.get("HX-Request") == "true" and not browser_navigation:
        existing = response.get("HX-Trigger")
        try:
            trigger_payload = json.loads(existing) if existing else {}
        except (TypeError, ValueError):
            trigger_payload = {existing: None} if existing else {}
        if not isinstance(trigger_payload, dict):
            trigger_payload = {}
        trigger_payload["librenmsCacheChanged"] = payload
        response["HX-Trigger"] = json.dumps(trigger_payload, separators=(",", ":"))

    if transition.error and browser_navigation:
        messages.warning(
            request,
            "Synchronization succeeded, but related cache cleanup failed. Reload this sync page before continuing.",
        )
    elif transition.removed_any and browser_navigation:
        messages.info(
            request,
            "Other sync tabs were cleared because you synchronized data from LibreNMS.",
        )
    return response


def render_sync_cache_miss(request, refresh_label, *, retarget=None):
    """Return an empty HTMX tab fragment for a writer whose snapshot is unavailable."""
    if request.headers.get("HX-Request") != "true":
        return None
    response = render(
        request,
        "netbox_librenms_plugin/htmx/sync_cache_missing.html",
        {"refresh_label": refresh_label},
    )
    if retarget:
        response["HX-Retarget"] = retarget
    return response


def configured_cache_timeout():
    """Return the sync cache lifetime without constructing a LibreNMS client."""
    from netbox.plugins import get_plugin_config

    return get_plugin_config("netbox_librenms_plugin", "cache_timeout", 300)


def schedule_request_cache_mutation(
    request,
    page_object,
    source_tab,
    server_key,
    *,
    cache_timeout=None,
    source_fragment_required=False,
):
    """Schedule one mutation transition and retain it for the endpoint response."""
    transition = SyncCacheConsistency(
        page_object,
        cache_timeout=cache_timeout or configured_cache_timeout(),
    ).schedule_mutation(
        source_tab,
        server_key,
        actor_id=request_actor_id(request),
        source_fragment_required=source_fragment_required,
    )
    request._librenms_cache_transition = transition
    return transition


def apply_request_cache_transition(request, response):
    """Attach the transition scheduled by the current request, when present."""
    return apply_transition_to_response(
        request,
        response,
        request.__dict__.get("_librenms_cache_transition"),
    )
