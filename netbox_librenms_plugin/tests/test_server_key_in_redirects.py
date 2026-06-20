"""Guard against the recurring "redirect URL drops server_key" review finding.

Module sync / interface / cable / VLAN / IP actions are server-scoped: after a POST (or an
HTMX refresh) on a non-default LibreNMS server, the follow-up URL must carry ``server_key`` so
the user returns to the same server's tab and cache namespace. This has been flagged across
multiple reviews, one ``?tab=`` builder at a time. Rather than re-checking each by hand, this
test asserts the whole class in one place: every ``?tab=`` URL built under the server-scoped
view packages must reference ``server_key`` within the same statement.

Scope is intentionally limited to ``views/sync``, ``views/base`` and ``views/object_sync`` —
the packages where the per-server redirect/tab convention applies.
"""

from pathlib import Path

import netbox_librenms_plugin.views as views_pkg

# Per the codebase convention, every redirect/tab URL built in these packages is server-scoped.
SCOPED_SUBPACKAGES = ("sync", "base", "object_sync")


def _scoped_python_files():
    root = Path(views_pkg.__file__).parent
    for sub in SCOPED_SUBPACKAGES:
        yield from sorted((root / sub).rglob("*.py"))


def test_every_tab_url_propagates_server_key():
    """Each ``?tab=`` URL builder must reference ``server_key`` in the same statement window."""
    offenders = []
    views_root = Path(views_pkg.__file__).parent
    for path in _scoped_python_files():
        lines = path.read_text().splitlines()
        for idx, line in enumerate(lines):
            if "?tab=" not in line:
                continue
            window = "\n".join(lines[idx : idx + 4])
            if "server_key" not in window:
                # Emit a path relative to the views package, not the bare basename, so the
                # offender is unambiguous when two scoped files share a name (e.g. several
                # subpackages each define interfaces.py).
                try:
                    where = path.relative_to(views_root)
                except ValueError:
                    where = path.name
                offenders.append(f"{where}:{idx + 1}: {line.strip()}")

    assert not offenders, (
        "These server-scoped '?tab=' URLs don't propagate server_key (the redirect would land "
        "on the default server's tab after a POST on another server):\n  " + "\n  ".join(offenders)
    )


def test_guard_actually_sees_tab_builders():
    """Sanity: the scan reaches real source (guards against a wrong path silently passing)."""
    total = 0
    for path in _scoped_python_files():
        total += path.read_text().count("?tab=")
    assert total > 0, f"Expected at least one '?tab=' builder under the scoped views, found {total}"
