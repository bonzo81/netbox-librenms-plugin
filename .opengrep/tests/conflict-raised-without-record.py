from netbox_librenms_plugin import server_mappings, transactions
from netbox_librenms_plugin.server_mappings import IdentityBusy, LibreNMSPortBindingBusy, MappingChanged
from netbox_librenms_plugin.transactions import (
    ConcurrentRowChange,
    TransactionConflict,
    recorded_conflict,
    row_changed,
)


def raises(row, kind):
    if kind == 1:
        # ruleid: conflict-raised-without-record
        raise TransactionConflict("busy")
    if kind == 2:
        # ruleid: conflict-raised-without-record
        raise ConcurrentRowChange("changed") from None
    if kind == 3:
        # ruleid: conflict-raised-without-record
        raise IdentityBusy()
    if kind == 4:
        # ruleid: conflict-raised-without-record
        raise MappingChanged("changed")
    if kind == 5:
        # ruleid: conflict-raised-without-record
        raise LibreNMSPortBindingBusy("busy")
    if kind == 6:
        # ruleid: conflict-raised-without-record
        raise IdentityBusy
    if kind == 7:
        # ruleid: conflict-raised-without-record
        raise server_mappings.MappingChanged("changed")
    if kind == 8:
        # ruleid: conflict-raised-without-record
        raise transactions.TransactionConflict("busy")
    if kind == 9:
        # ok: conflict-raised-without-record
        raise recorded_conflict(MappingChanged("changed"))
    if kind == 10:
        # ok: conflict-raised-without-record
        raise recorded_conflict(IdentityBusy()) from None
    if kind == 11:
        # ok: conflict-raised-without-record
        raise row_changed(row.name)
    if kind == 12:
        # ok: conflict-raised-without-record
        raise ValueError("changed")
    # ok: conflict-raised-without-record
    raise
