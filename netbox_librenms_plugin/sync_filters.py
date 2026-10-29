"""
Server-side filters for the sync tab tables.

A tab table describes its columns in a ``FilterSpec``. ``parse_sync_filters`` binds the tab's own
query parameters (``<prefix>q``, ``<prefix>sync_state`` and ``<prefix>filter_<column>``) to a plain
Django form, and ``filter_sync_rows`` keeps the rows that match. A filter only decides which rows one
table shows: no sync action reads it.
"""

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

from django import forms

from netbox_librenms_plugin.constants import MATCHING, MISMATCHED, NOT_IN_NETBOX
from netbox_librenms_plugin.utils import get_interface_name_field

# The text one filter field accepts, in characters.
MAX_FILTER_TEXT = 256

SYNC_STATE_CHOICES = (
    ("", "All sync states"),
    (MATCHING, "Matching"),
    (MISMATCHED, "Mismatched"),
    (NOT_IN_NETBOX, "Not in NetBox"),
)
# The empty text of a filtered table that has no row.
NO_MATCH_TEXT = "No rows match these filters"

QUERY_FIELD = "q"
SYNC_STATE_FIELD = "sync_state"
COLUMN_FIELD_PREFIX = "filter_"


@dataclass(frozen=True)
class FilterColumn:
    """One displayed textual column: its name, its label, and the function that returns its visible text."""

    name: str
    label: str
    text: Callable[[dict], str]
    # A column without its own field (for example an actions column) is searched by ``q`` only.
    has_field: bool = True


def is_filter_key(prefix, key):
    """Return whether query key *key* is a filter parameter of the tab that uses *prefix*."""
    if not key.startswith(prefix):
        return False
    name = key[len(prefix) :]
    return name in (QUERY_FIELD, SYNC_STATE_FIELD) or name.startswith(COLUMN_FIELD_PREFIX)


@dataclass(frozen=True)
class FilterSpec:
    """The filterable columns of one tab table variant, and how a row maps onto a sync state."""

    prefix: str
    columns: tuple[FilterColumn, ...]
    sync_state: Callable[[dict], str | None]

    @property
    def field_columns(self):
        """Return the columns that have their own filter field, in display order."""
        return tuple(column for column in self.columns if column.has_field)

    def param(self, field_name):
        """Return the query parameter name of one form field."""
        return f"{self.prefix}{field_name}"

    def owns(self, key):
        """Return whether a query parameter is one of this tab's filter parameters."""
        return is_filter_key(self.prefix, key)


def normalize_filter_text(text):
    """Return *text* as filters compare it: whitespace collapsed to single spaces, then case-folded."""
    return " ".join(str(text).split()).casefold()


class SyncFilterForm(forms.Form):
    """The filter form of one tab table. Field names carry the tab prefix with no separator."""

    def __init__(self, data, *, spec, duplicates, unknown):
        """Build the fields from *spec* and bind *data*, the request query."""
        super().__init__(data, prefix=spec.prefix)
        self.spec = spec
        self._duplicates = duplicates
        self._unknown = unknown
        self.fields[QUERY_FIELD] = forms.CharField(label="Search", required=False, max_length=MAX_FILTER_TEXT)
        self.fields[SYNC_STATE_FIELD] = forms.ChoiceField(
            label="Sync state", required=False, choices=SYNC_STATE_CHOICES
        )
        for column in spec.field_columns:
            self.fields[f"{COLUMN_FIELD_PREFIX}{column.name}"] = forms.CharField(
                label=column.label, required=False, max_length=MAX_FILTER_TEXT
            )

    def add_prefix(self, field_name):
        """Return the query parameter name: the tab prefix and the field name, as the tab links write it."""
        return f"{self.prefix}{field_name}"

    def clean(self):
        """Refuse a filter given twice and a column this table does not have."""
        cleaned = super().clean()
        for name in self._duplicates:
            if name in self.fields:
                self.add_error(name, "Give this filter only once.")
        for key in self._unknown:
            self.add_error(None, f"This table has no filter named {key}.")
        return cleaned

    @property
    def column_fields(self):
        """Return the bound fields of the per-column filters, in display order."""
        return [self[f"{COLUMN_FIELD_PREFIX}{column.name}"] for column in self.spec.field_columns]

    @property
    def columns_have_input(self):
        """Return whether a per-column filter has a value or an error, so the column panel opens."""
        return any(field.value() or field.errors for field in self.column_fields)


@dataclass(frozen=True)
class SyncFilters:
    """The bound filter form of one tab table, and the values it accepted."""

    form: SyncFilterForm
    spec: FilterSpec
    query: str = ""
    sync_state: str = ""
    columns: tuple[tuple[str, str], ...] = ()

    @property
    def is_valid(self):
        """Return whether every filter parameter of the tab is valid."""
        return self.form.is_valid()

    @property
    def active(self):
        """Return whether a valid filter narrows the rows."""
        return self.is_valid and bool(self.query or self.sync_state or self.columns)

    @property
    def active_count(self):
        """Return the number of valid filters that narrow the rows."""
        return int(bool(self.query)) + int(bool(self.sync_state)) + len(self.columns) if self.is_valid else 0

    @property
    def in_use(self):
        """Return whether the query holds a filter of the tab, valid or not, so the tab shows its filter form."""
        return self.active or not self.is_valid


def selection_scope(**context):
    """
    Return a stable text that names the rows a tab table can hold, for the browser's selection store.

    The filters are left out: the selection belongs to the tab, so a filter change keeps the rows the
    user selected, and the notice above the table counts the ones the filter hides.

    Args:
        **context: Page state that decides which rows the table holds, such as the name field.

    Returns:
        str: The context, sorted and URL-encoded.

    """
    return urlencode(sorted((key, str(value)) for key, value in context.items() if value))


def parse_sync_filters(query, spec):
    """
    Bind the filter parameters of one tab table from *query*.

    A parameter given more than once, a ``<prefix>filter_*`` key for a column the table does not
    show, text longer than ``MAX_FILTER_TEXT``, or an unknown sync state makes the form invalid.
    Parameters of other tabs are not read.

    Args:
        query (QueryDict): The request query.
        spec (FilterSpec): The table variant's filter columns.

    Returns:
        SyncFilters: The bound form and, when it is valid, the accepted values.

    """
    known = {QUERY_FIELD, SYNC_STATE_FIELD, *(f"{COLUMN_FIELD_PREFIX}{column.name}" for column in spec.field_columns)}
    owned = [key for key in query if spec.owns(key)]
    duplicates = [key[len(spec.prefix) :] for key in owned if len(query.getlist(key)) > 1]
    unknown = [key for key in owned if key[len(spec.prefix) :] not in known]
    form = SyncFilterForm(query, spec=spec, duplicates=duplicates, unknown=unknown)
    if not form.is_valid():
        return SyncFilters(form=form, spec=spec)
    data = form.cleaned_data
    columns = tuple(
        (column.name, data[f"{COLUMN_FIELD_PREFIX}{column.name}"])
        for column in spec.field_columns
        if data[f"{COLUMN_FIELD_PREFIX}{column.name}"]
    )
    return SyncFilters(
        form=form,
        spec=spec,
        query=data[QUERY_FIELD],
        sync_state=data[SYNC_STATE_FIELD],
        columns=columns,
    )


def parse_table_filters(query, build_table):
    """
    Bind the filters of one tab table from *query*, also when the tab has no snapshot to show.

    Args:
        query (QueryDict): The request query.
        build_table (Callable[[list[dict]], Table]): Returns a table of the given rows; it gets no row.

    Returns:
        SyncFilters: The bound form and, when it is valid, the accepted values.

    """
    return parse_sync_filters(query, build_table([]).filter_spec())


def filter_sync_rows(rows, filters, spec):
    """
    Return the rows that match every filter, in their input order.

    Text matches when it contains the filter text after both are normalized by
    ``normalize_filter_text``. ``q`` matches when any column contains it. The column filters,
    ``q`` and the sync state must all match.

    Args:
        rows (list[dict]): The table rows.
        filters (SyncFilters): Valid filters.
        spec (FilterSpec): The spec the filters were parsed with.

    Returns:
        list[dict]: The matching rows.

    """
    query = normalize_filter_text(filters.query)
    texts = {column.name: column.text for column in spec.columns}
    columns = [(texts[name], normalize_filter_text(text)) for name, text in filters.columns]

    def matches(row):
        if filters.sync_state and spec.sync_state(row) != filters.sync_state:
            return False
        if any(needle not in normalize_filter_text(text(row)) for text, needle in columns):
            return False
        return not query or any(query in normalize_filter_text(column.text(row)) for column in spec.columns)

    return [row for row in rows if matches(row)]


def filter_sync_table(request, owner, rows, build_table, *, server_key, **scope_context):
    """
    Return the table of the rows that match the tab's filters in the request query, its filters, and the row count before them.

    The table owns the cell text functions, so a table of every row gives the filter spec, and a
    second table shows the matches. The table attributes tell the browser whether a filter is active
    and which selection store the table uses. The selection store always names the interface name
    field: the port names of every tab can follow it, so a new name field starts an empty selection.

    Args:
        request (HttpRequest): The request; its query holds the filters.
        owner (Device | VirtualMachine): The object of the sync page, which scopes the name field preference.
        rows (list[dict]): Every row of the whole snapshot, prepared for display.
        build_table (Callable[[list[dict]], Table]): Returns a table of the given rows. The table has
            ``filter_spec()``.
        server_key (str | None): The LibreNMS server of the tab. Each server has its own selection.
        **scope_context: Other page state of one tab that decides which rows the table holds (see
            ``selection_scope``).

    Returns:
        tuple[Table, SyncFilters, int | None]: The table to show, the bound filters, and the row count
            before the filter (None when no filter narrows the table).

    """
    table = build_table(rows)
    spec = table.filter_spec()
    filters = parse_sync_filters(request.GET, spec)
    filtered_from = None
    if filters.active:
        filtered_from = len(rows)
        table = build_table(filter_sync_rows(rows, filters, spec))
        table.empty_text = NO_MATCH_TEXT
    table.attrs["data-filter-active"] = "true" if filters.active else "false"
    # A filter keeps the browser selection; another name field or ignored toggle starts an empty one.
    # The invalid filter placeholder reads the attribute, because a template cannot look up a hyphenated key.
    table.selection_scope = selection_scope(
        interface_name_field=get_interface_name_field(request, owner), server_key=server_key, **scope_context
    )
    table.attrs["data-selection-scope"] = table.selection_scope
    return table, filters, filtered_from
