"""The preference endpoint must reject a non-string key instead of raising.

``key not in self.ALLOWED_PREFS`` is a dict lookup, so an unhashable value decoded from the
JSON body raises ``TypeError`` and returns a 500. The envelope check one line above already
rejects a non-object payload; this closes the same gap one level down.
"""

import json

import pytest

from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view


UNHASHABLE = ["use_sysname"]


@pytest.mark.django_db
class TestUnhashablePreferenceKey:
    """The preference endpoint rejects a non-string key instead of returning a 500."""

    def test_unhashable_key_is_a_400(self):
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        user = make_superuser("unhashable-pref-key")
        request = make_request(
            "post",
            json.dumps({"key": UNHASHABLE, "value": True}),
            user=user,
            path="/update-preference/",
            content_type="application/json",
        )
        view = make_view(SaveUserPrefView, request)

        response = view.post(request)

        assert response.status_code == 400, "an unhashable preference key must not reach a 500"

    def test_a_valid_key_still_works(self):
        """Positive control: the guard must not reject the keys the UI actually posts."""
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        user = make_superuser("valid-pref-key")
        request = make_request(
            "post",
            json.dumps({"key": "use_sysname", "value": True}),
            user=user,
            path="/update-preference/",
            content_type="application/json",
        )
        view = make_view(SaveUserPrefView, request)

        response = view.post(request)

        assert response.status_code == 200, response.content
