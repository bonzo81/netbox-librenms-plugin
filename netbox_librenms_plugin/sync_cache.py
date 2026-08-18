"""Shared cache-consistency contract for the five device sync tabs."""

import json
import logging
import math
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from django.core.cache import cache
from django.contrib import messages
from django.db import transaction
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from netbox_librenms_plugin.librenms_api import configured_cache_timeout
from netbox_librenms_plugin.utils import (
    cache_remaining_ttl,
    coerce_librenms_id,
    get_librenms_device_id,
    get_librenms_sync_device,
    get_migrated_to_marker,
    resolve_server_mapping_display_id,
)

logger = logging.getLogger(__name__)

_ACKNOWLEDGED_REVISIONS_SESSION_KEY = "librenms_sync_cache_acknowledged_revisions"
# An entry is dropped when its tab becomes available again, which never happens for a deleted
# object. Cap the map so the session payload cannot grow without limit.
_MAX_ACKNOWLEDGED_REVISIONS = 200


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
    context_name: str
    content_id: str
    label: str
    shared_vc_owner: bool
    supports_virtual_machine: bool
    # The context key the tab's partial reads its table from; VLANs use their own.
    table_context_key: str = "table"
    has_last_fetched_key: bool = False
    has_vlan_overrides: bool = False
    has_cable_picks: bool = False


TAB_SPECS = {
    SyncTab.INTERFACES: SyncTabSpec(
        data_type="ports",
        context_name="interface_sync",
        content_id="interface-sync-content",
        label="Interface",
        shared_vc_owner=True,
        supports_virtual_machine=True,
        has_last_fetched_key=True,
        has_vlan_overrides=True,
    ),
    SyncTab.CABLES: SyncTabSpec(
        data_type="links",
        context_name="cable_sync",
        content_id="cable-sync-content",
        label="Cable",
        shared_vc_owner=True,
        supports_virtual_machine=False,
        has_cable_picks=True,
    ),
    SyncTab.IP_ADDRESSES: SyncTabSpec(
        data_type="ip_addresses",
        context_name="ip_sync",
        content_id="ipaddress-sync-content",
        label="IP address",
        shared_vc_owner=False,
        supports_virtual_machine=True,
    ),
    SyncTab.MODULES: SyncTabSpec(
        data_type="inventory",
        context_name="module_sync",
        content_id="module-sync-content",
        label="Module",
        shared_vc_owner=True,
        supports_virtual_machine=False,
    ),
    SyncTab.VLANS: SyncTabSpec(
        data_type="vlans",
        context_name="vlan_sync",
        content_id="vlan-sync-content",
        label="VLAN",
        shared_vc_owner=False,
        supports_virtual_machine=False,
        table_context_key="vlan_table",
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
    source_tabs: tuple[SyncTab, ...] | None = None
    transition_ids: tuple[str, ...] | None = None
    revision_entries: tuple[tuple[str, SyncTab, str], ...] | None = None

    @property
    def removed_any(self):
        """Return whether at least one prior snapshot or temporary value was removed."""
        return bool(self.removed_tabs)

    def browser_payload(self):
        """Serialize only information the initiating browser needs."""
        if self.revision_entries is None:
            revision_entries = tuple(
                (server_key, tab, revision) for (server_key, tab), revision in self.revisions.items()
            )
        else:
            revision_entries = self.revision_entries
        revisions = {}
        duplicate_indexes = {}
        for server_key, tab, revision in sorted(
            revision_entries,
            key=lambda item: (item[0], item[1].value, item[2]),
        ):
            base_key = f"{server_key}:{tab.value}"
            key = base_key
            if key in revisions:
                duplicate_indexes[base_key] = duplicate_indexes.get(base_key, 1) + 1
                key = f"{base_key}#{duplicate_indexes[base_key]}"
            revisions[key] = revision

        payload = {
            "transition_id": self.transition_id,
            "removed": self.removed_any,
            "cleanup_failed": self.error is not None,
            "tabs": sorted({tab.value for _server_key, tab in self.removed_tabs}),
            "cleanup_tabs": sorted(tab.value for tab in self.cleanup_tabs),
            "source_tab": self.source_tab.value if self.source_tab is not None else None,
            "source_fragment_required": self.source_fragment_required,
            "revisions": revisions,
        }
        if self.source_tabs is not None:
            payload["source_tabs"] = sorted(tab.value for tab in self.source_tabs)
        if self.transition_ids is not None:
            payload["transition_ids"] = list(self.transition_ids)
        return payload


def merge_cache_transitions(transitions):
    """Combine cache mutations scheduled by one response into one browser event."""
    transitions = tuple(transition for transition in transitions if transition is not None)
    if not transitions:
        return None
    if len(transitions) == 1:
        return transitions[0]

    source_tabs = tuple(
        sorted(
            {transition.source_tab for transition in transitions if transition.source_tab is not None},
            key=lambda tab: tab.value,
        )
    )
    revision_entries = tuple(
        (server_key, tab, revision)
        for transition in transitions
        for (server_key, tab), revision in transition.revisions.items()
    )
    errors = tuple(dict.fromkeys(transition.error for transition in transitions if transition.error))
    return CacheMutationTransition(
        transition_id="+".join(transition.transition_id for transition in transitions),
        removed_tabs=set().union(*(transition.removed_tabs for transition in transitions)),
        affected_tabs=set().union(*(transition.affected_tabs for transition in transitions)),
        cleanup_tabs=set().union(*(transition.cleanup_tabs for transition in transitions)),
        source_tab=source_tabs[0] if source_tabs else None,
        source_fragment_required=any(transition.source_fragment_required for transition in transitions),
        error="; ".join(errors) if errors else None,
        completed=all(transition.completed for transition in transitions),
        source_tabs=source_tabs,
        transition_ids=tuple(transition.transition_id for transition in transitions),
        revision_entries=revision_entries,
    )


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
        if isinstance(server_key, str)
        and server_key
        and (
            resolve_server_mapping_display_id(entry)[0] is not None
            or get_migrated_to_marker(obj, server_key) is not None
        )
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

    def __init__(self, page_object):
        self.page_object = page_object
        # One instance serves a single request or post-commit callback, so the owner a server
        # key resolves to cannot change during its lifetime.
        self._shared_owners = {}

    def applicable_tabs(self):
        """Return the tabs supported by this page object's model."""
        is_virtual_machine = self.page_object._meta.model_name == "virtualmachine"
        return tuple(tab for tab, spec in TAB_SPECS.items() if not is_virtual_machine or spec.supports_virtual_machine)

    def _shared_owner(self, server_key):
        if self.page_object._meta.model_name != "device":
            return self.page_object
        if server_key not in self._shared_owners:
            self._shared_owners[server_key] = (
                get_librenms_sync_device(self.page_object, server_key=server_key) or self.page_object
            )
        return self._shared_owners[server_key]

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
        """Return the state key for one tab and server, scoped like its snapshot."""
        # A shared snapshot needs a shared state, else a sibling stays blocked after a refresh.
        return _state_key(self._primary_owner(tab, server_key), server_key, tab)

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
        cache.set(
            self.state_key(tab, server_key),
            record,
            timeout=configured_cache_timeout(server_key),
        )
        return record

    def mark_refresh_success(self, tab, server_key, *, actor_id=None):
        """Publish a new ready revision after an explicit successful refresh."""
        return self._set_state(tab, server_key, SyncTabState.READY, actor_id=actor_id)

    def mark_refresh_failure(self, tab, server_key, *, actor_id=None):
        """Record a failed refresh unless an earlier invalidation reason must remain."""
        state_key = self.state_key(tab, server_key)
        prior = cache.get(state_key)
        if isinstance(prior, dict) and prior.get("state") == SyncTabState.INVALIDATED.value:
            remaining_timeout = self._remaining_state_timeout(state_key, prior, server_key)
            if remaining_timeout > 0:
                prior = {
                    **prior,
                    "refresh_error": "The latest LibreNMS refresh failed.",
                    "refresh_error_timestamp": timezone.now().isoformat(),
                }
                cache.set(state_key, prior, timeout=remaining_timeout)
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

    def _remaining_state_timeout(self, state_key, record, server_key):
        """Return a state record's remaining lifetime without extending it."""
        remaining_timeout = cache_remaining_ttl(cache, state_key)
        if isinstance(remaining_timeout, int):
            return remaining_timeout
        created_at = parse_datetime(record.get("timestamp") or "")
        if created_at is None or not timezone.is_aware(created_at):
            return 0
        elapsed = max(0, (timezone.now() - created_at).total_seconds())
        return max(0, math.ceil(configured_cache_timeout(server_key) - elapsed))

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
        delete_pattern = getattr(cache, "delete_pattern", None)
        if not callable(delete_pattern):
            return 0
        try:
            return int(delete_pattern(pattern) or 0)
        except (AttributeError, NotImplementedError):
            return 0

    @staticmethod
    def _pattern_has_values(pattern):
        """Return whether a snapshot-bound wildcard key currently exists."""
        iter_keys = getattr(cache, "iter_keys", None)
        if callable(iter_keys):
            try:
                return next(iter_keys(pattern), None) is not None
            except (AttributeError, NotImplementedError):
                return False
        keys = getattr(cache, "keys", None)
        if callable(keys):
            try:
                return bool(keys(pattern))
            except (AttributeError, NotImplementedError):
                return False
        return False

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

    def status_for_request(self, request, server_key, *, active_tab=None):
        """Return status with revision-specific attention state for this browser session."""
        result = self.status(server_key, actor_id=request_actor_id(request))
        stored = request.session.get(_ACKNOWLEDGED_REVISIONS_SESSION_KEY)
        acknowledgements = dict(stored) if isinstance(stored, dict) else {}
        changed = False

        def acknowledgement_key(tab):
            return ":".join(
                (
                    self.page_object._meta.model_name,
                    str(self.page_object.pk),
                    server_key,
                    tab.value,
                )
            )

        for tab in self.applicable_tabs():
            key = acknowledgement_key(tab)
            if result[tab.value]["snapshot_available"] and key in acknowledgements:
                acknowledgements.pop(key)
                changed = True

        if active_tab is not None:
            active_state = result[active_tab.value]
            active_revision = active_state.get("revision")
            if not active_state["snapshot_available"] and active_revision:
                key = acknowledgement_key(active_tab)
                if acknowledgements.get(key) != active_revision:
                    # Re-insert so the dictionary order stays newest-last for the cap below.
                    acknowledgements.pop(key, None)
                    acknowledgements[key] = active_revision
                    changed = True

        for tab in self.applicable_tabs():
            state = result[tab.value]
            revision = state.get("revision")
            state["attention_required"] = bool(
                not state["snapshot_available"]
                and revision
                and acknowledgements.get(acknowledgement_key(tab)) != revision
            )

        if changed:
            excess = len(acknowledgements) - _MAX_ACKNOWLEDGED_REVISIONS
            if excess > 0:
                # Oldest first: the dictionary keeps insertion order and updates re-insert.
                for stale_key in list(acknowledgements)[:excess]:
                    acknowledgements.pop(stale_key)
            request.session[_ACKNOWLEDGED_REVISIONS_SESSION_KEY] = acknowledgements
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


def schedule_request_cache_mutation(
    request,
    page_object,
    source_tab,
    server_key,
    *,
    source_fragment_required=False,
):
    """Schedule one mutation transition and retain it for the endpoint response."""
    transition = SyncCacheConsistency(page_object).schedule_mutation(
        source_tab,
        server_key,
        actor_id=request_actor_id(request),
        source_fragment_required=source_fragment_required,
    )
    transitions = getattr(request, "_librenms_cache_transitions", None)
    if not isinstance(transitions, list):
        transitions = []
    transitions.append(transition)
    request._librenms_cache_transitions = transitions
    return transition


def apply_request_cache_transition(request, response):
    """Attach the transition scheduled by the current request, when present."""
    transitions = getattr(request, "_librenms_cache_transitions", None)
    if not isinstance(transitions, list):
        transitions = []
    transition = merge_cache_transitions(transitions)
    return apply_transition_to_response(
        request,
        response,
        transition,
    )
