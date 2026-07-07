"""The consolidated sidebar navigation: Mappings and Rules & Patterns.

Nine per-model menu items collapse into two sidebar entries. The object lists
cross-link through a switcher rendered above the native Results/Filters tabs, so
each list keeps the full generic feature set (filters, bulk ops, import/export,
changelog) while the sidebar stays small.
"""

import pytest
from django.urls import reverse

# Tab orders mirror the old menu order; each group's single nav entry lands on the
# first tab. Branches that add a new rule/pattern model extend these lists (and the
# switcher include) in the same commit that adds the model.
MAPPING_LIST_ROUTES = [
    "interfacetypemapping_list",
    "devicetypemapping_list",
    "moduletypemapping_list",
    "modulebaymapping_list",
    "platformmapping_list",
]
RULE_LIST_ROUTES = [
    "inventoryignorerule_list",
    "normalizationrule_list",
    "carrierautoinstallrule_list",
]


def _menu_groups():
    from netbox_librenms_plugin.navigation import menu

    return {group.label: [item.link for item in group.items] for group in menu.groups}


class TestConsolidatedMenu:
    """One sidebar entry per group; no per-model items remain."""

    def test_mappings_group_is_a_single_entry(self):
        groups = _menu_groups()
        assert groups["Mappings"] == [
            f"plugins:netbox_librenms_plugin:{MAPPING_LIST_ROUTES[0]}",
        ]

    def test_rules_and_patterns_group_is_a_single_entry(self):
        groups = _menu_groups()
        assert groups.get("Rules & Patterns") == [
            f"plugins:netbox_librenms_plugin:{RULE_LIST_ROUTES[0]}",
        ]


@pytest.mark.django_db
class TestListPageSwitchers:
    """Each list page renders its group's switcher, current page marked active."""

    def _rendered(self, client, route):
        from netbox_librenms_plugin.tests.conftest import make_superuser

        client.force_login(make_superuser())
        url = reverse(f"plugins:netbox_librenms_plugin:{route}")
        response = client.get(url)
        assert response.status_code == 200
        return url, response.content.decode()

    @pytest.mark.parametrize("route", MAPPING_LIST_ROUTES)
    def test_mapping_pages_cross_link_all_mapping_lists(self, route, client):
        url, html = self._rendered(client, route)
        for other in MAPPING_LIST_ROUTES:
            assert reverse(f"plugins:netbox_librenms_plugin:{other}") in html, (
                f"{route} page is missing the switcher link to {other}"
            )
        assert f'class="nav-link active" href="{url}"' in html

    @pytest.mark.parametrize("route", RULE_LIST_ROUTES)
    def test_rule_pages_cross_link_all_rule_lists(self, route, client):
        url, html = self._rendered(client, route)
        for other in RULE_LIST_ROUTES:
            assert reverse(f"plugins:netbox_librenms_plugin:{other}") in html, (
                f"{route} page is missing the switcher link to {other}"
            )
        assert f'class="nav-link active" href="{url}"' in html
