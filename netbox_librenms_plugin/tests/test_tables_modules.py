"""Rendering tests for the real LibreNMS module table."""

import pytest
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser


pytestmark = pytest.mark.django_db


def _table(device=None, **overrides):
    from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

    permissions = {
        "has_write_permission": True,
        "can_add_module": True,
        "can_change_module": True,
        "can_change_interface": True,
        "can_delete_module": True,
        "can_add_module_bay_template": True,
        "can_add_module_type": True,
        "can_add_carrier_rule": True,
        "can_add_module_bay_mapping": True,
        "can_add_module_type_mapping": True,
    }
    permissions.update(overrides)
    table = LibreNMSModuleTable([], device=device, server_key="production", **permissions)
    table.csrf_token = "table-csrf-token"
    table.inventory_snapshot_digest = "table-snapshot-digest"
    table.return_url = "/plugins/librenms-sync/?tab=modules"
    return table


class TestRenderedColumns:
    @pytest.mark.parametrize(
        ("value", "record", "expected"),
        [
            ("Router", {"depth": 0}, "Router"),
            (None, {"depth": 0}, "-"),
            ("Card", {"depth": 2}, "padding-left:40px"),
            ("Card", {"depth": 2}, "└─"),
            ("Card", {"depth": 2, "_source": "oob"}, "OOB</span></span>"),
            (
                "Ethernet1",
                {
                    "matched_interface_url": "/dcim/interfaces/1/",
                    "matched_interface_name": "Ethernet1",
                    "matched_interface_source": "port_id",
                    "matched_interface_confidence": "exact",
                },
                "Matched by port id, confidence exact",
            ),
        ],
    )
    def test_name_rendering(self, value, record, expected):
        assert expected in str(_table().render_name(value, record))

    @pytest.mark.parametrize("value", [None, "", "-"])
    def test_empty_model_and_serial_render_as_placeholders(self, value):
        table = _table()

        assert table.render_model(value, {}) == "-"
        assert str(table.render_serial(value, {})) == "-"

    def test_model_and_bay_links_use_supplied_real_urls(self):
        table = _table()

        assert 'href="/dcim/module-types/1/"' in str(
            table.render_model("Module type", {"module_type_url": "/dcim/module-types/1/"})
        )
        assert 'href="/dcim/module-bays/1/"' in str(
            table.render_module_bay("Bay 1", {"module_bay_url": "/dcim/module-bays/1/"})
        )
        assert 'href="/dcim/module-types/1/"' in str(
            table.render_module_type("Module type", {"module_type_url": "/dcim/module-types/1/"})
        )

    def test_long_description_is_truncated_but_kept_in_the_title(self):
        description = "Description " * 10

        rendered = str(_table().render_description(description, {}))

        assert "&hellip;" in rendered
        assert description in rendered

    @pytest.mark.parametrize(
        ("item_class", "icon"),
        [
            ("module", "mdi-expansion-card"),
            ("ioModule", "mdi-expansion-card"),
            ("fan", "mdi-fan"),
            ("powerSupply", "mdi-power-plug"),
            ("port", "mdi-ethernet"),
            ("unknown", "mdi-card-outline"),
        ],
    )
    def test_item_class_icon(self, item_class, icon):
        assert icon in str(_table().render_item_class(item_class, {}))

    def test_missing_bay_and_type_are_distinct_warnings(self):
        table = _table()

        assert "text-danger" in str(table.render_module_bay(None, {}))
        assert "No matching bay" in str(table.render_module_bay(None, {}))
        assert "text-warning" in str(table.render_module_type(None, {}))
        assert "No matching type" in str(table.render_module_type(None, {}))
        assert table.render_module_bay(None, {"status": "Integrated"}) == "-"

    def test_child_bay_keeps_the_tree_indentation(self):
        rendered = str(_table().render_module_bay("Bay 1", {"depth": 2}))

        assert "padding-left:40px" in rendered
        assert "└─ Bay 1" in rendered


class TestStatusRendering:
    @pytest.mark.parametrize(
        ("status", "css_class"),
        [
            ("Installed", "bg-success"),
            ("Matched", "bg-info"),
            ("No Bay", "bg-warning"),
            ("Serial Mismatch", "bg-danger"),
            ("Name Conflict", "bg-warning"),
            ("Unknown", "bg-secondary"),
        ],
    )
    def test_status_badge_and_in_flight_indicator(self, status, css_class):
        rendered = str(_table().render_status(status, {}))

        assert css_class in rendered
        assert "lnms-status-live" in rendered
        assert "lnms-installing" in rendered
        assert "spinner-border" in rendered

    def test_update_and_install_rows_use_the_correct_progress_label(self):
        table = _table()
        update = str(
            table.render_status(
                "Serial Mismatch",
                {"installed_module_id": 42, "can_update_serial": True},
            )
        )
        install = str(table.render_status("Matched", {"can_install": True}))

        assert "Updating…" in update
        assert "Installing…" not in update
        assert "Installing…" in install

    @pytest.mark.parametrize(
        ("status", "record", "text"),
        [
            ("No Bay", {"model_warning": "Missing model details"}, "Missing model details"),
            ("Name Conflict", {"name_conflict_reason": "Duplicate interface names"}, "Duplicate interface names"),
            ("No Bay", {"holder_hint_present": True}, "Possible Carrier?"),
        ],
    )
    def test_status_explains_actionable_warnings(self, status, record, text):
        rendered = str(_table().render_status(status, record))

        assert "&lt;span" not in rendered
        assert text in rendered

    def test_integrated_status_names_the_parent_and_has_no_action(self):
        device = make_device("table-integrated")
        table = _table(device)
        record = {
            "status": "Integrated",
            "integrated_in_name": "Parent module",
            "type_suggestion": {"librenms_model": "ignored"},
        }

        rendered = str(table.render_status("Integrated", record))

        assert "Integrated in Parent module" in rendered
        assert "bg-body-secondary" in rendered
        assert "text-body" in rendered
        assert "bg-light" not in rendered
        assert "Duplicate SNMP entry" in rendered
        assert table.render_actions("", record) == ""


class TestActionRendering:
    def test_requires_a_device_and_plugin_write_permission(self):
        assert _table().render_actions(None, {"can_install": True}) == ""
        device = make_device("table-no-write")
        assert _table(device, has_write_permission=False).render_actions(None, {"can_install": True}) == ""

    def test_install_and_branch_actions_use_real_reversed_urls(self):
        from netbox_librenms_plugin.utils import module_inventory_binding_matches

        device = make_device("table-install")
        table = _table(device)
        record = {
            "can_install": True,
            "module_bay_id": 5,
            "module_type_id": 10,
            "serial": "TEST-SERIAL",
            "has_installable_children": True,
            "ent_physical_index": 42,
            "inventory_digest": "install-row-digest",
        }

        rendered = str(table.render_actions(None, record))

        assert "Install" in rendered
        assert "Install Branch" in rendered
        # The install reads the serial from the cached row, so the form posts the row index only.
        assert 'name="ent_index" value="42"' in rendered
        assert "TEST-SERIAL" not in rendered
        assert "table-csrf-token" in rendered
        assert 'hx-target="#module-sync-content"' in rendered
        assert f"/{device.pk}/" in rendered
        bindings = [tag["value"] for tag in open_tags(rendered, "input") if tag.get("name") == "inventory_binding"]
        assert len(bindings) == 2
        assert any(
            module_inventory_binding_matches(
                binding,
                device.pk,
                "production",
                "install_module",
                {"module_bay_id": 5, "module_type_id": 10},
                42,
                "install-row-digest",
            )
            for binding in bindings
        )
        assert any(
            module_inventory_binding_matches(
                binding,
                device.pk,
                "production",
                "install_branch",
                {"parent_index": 42},
                42,
                "table-snapshot-digest",
            )
            for binding in bindings
        )

    def test_missing_branch_index_produces_no_branch_action(self):
        device = make_device("table-branch-missing")

        assert _table(device).render_actions(None, {"has_installable_children": True}) == ""

    def test_missing_snapshot_digest_produces_no_branch_action(self):
        device = make_device("table-branch-missing-snapshot")
        table = _table(device)
        table.inventory_snapshot_digest = ""

        rendered = str(
            table.render_actions(
                None,
                {
                    "has_installable_children": True,
                    "ent_physical_index": 42,
                },
            )
        )

        assert "Install Branch" not in rendered

    @pytest.mark.parametrize(
        ("action_label", "record"),
        [
            ("Install", {"can_install": True, "module_bay_id": 5, "module_type_id": 10}),
            ("Update Serial", {"can_update_serial": True, "installed_module_id": 99}),
            (
                "Update Interface",
                {
                    "can_update_interface_binding": True,
                    "installed_module_id": 99,
                    "librenms_port_id": 4501,
                },
            ),
        ],
    )
    def test_standard_actions_require_an_inventory_index(self, action_label, record):
        device = make_device(f"table-missing-{action_label.lower().replace(' ', '-')}-index")

        rendered = str(_table(device).render_actions(None, record))

        assert action_label not in rendered

    def test_update_serial_and_interface_actions_carry_real_identifiers(self):
        from netbox_librenms_plugin.utils import module_inventory_binding_matches

        device = make_device("table-update")
        table = _table(device)
        record = {
            "can_update_serial": True,
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": "77",
            "librenms_port_id": 56_284,
            "serial": "NEW-SERIAL",
            "name": "Ethernet1",
            "inventory_digest": "update-row-digest",
        }

        rendered = str(table.render_actions(None, record))

        assert "Update Serial" in rendered
        assert "Update Interface" in rendered
        assert 'name="module_id" value="42"' in rendered
        # The action reads the serial from the cached row, so the form posts the row index.
        assert 'name="ent_index" value="77"' in rendered
        bindings = [tag["value"] for tag in open_tags(rendered, "input") if tag.get("name") == "inventory_binding"]
        assert len(bindings) == 2
        assert any(
            module_inventory_binding_matches(
                binding,
                device.pk,
                "production",
                "update_module_serial",
                {"module_id": 42},
                77,
                "update-row-digest",
            )
            for binding in bindings
        )
        assert any(
            module_inventory_binding_matches(
                binding,
                device.pk,
                "production",
                "update_module_interface",
                {"module_id": 42},
                77,
                "update-row-digest",
            )
            for binding in bindings
        )
        assert "NEW-SERIAL" not in rendered
        assert 'hx-indicator="closest tr"' in rendered
        assert 'hx-disabled-elt="find button"' in rendered

    def test_render_actions_install_hidden_without_an_inventory_index(self):
        """The install resolves its row by index, so a row with no index has no standard action."""
        device = make_device("table-install-no-index")
        record = {
            "can_install": True,
            "module_bay_id": 5,
            "module_type_id": 10,
            "ent_physical_index": "",
        }

        rendered = str(_table(device).render_actions(None, record))

        assert "Install" not in rendered

    def test_render_actions_update_serial_hidden_without_an_inventory_index(self):
        """The view resolves the serial through the cached row, so a row with no index has no action."""
        device = make_device("table-update-no-index")
        record = {
            "can_update_serial": True,
            "installed_module_id": 99,
            "ent_physical_index": "",
        }

        rendered = str(_table(device).render_actions(None, record))

        assert "Update Serial" not in rendered

    def test_render_actions_update_interface_hidden_without_an_inventory_index(self):
        """The bind reads its metadata from the cached row, so a row with no index has no action."""
        device = make_device("table-bind-no-index")
        record = {
            "can_update_interface_binding": True,
            "installed_module_id": 99,
            "librenms_port_id": 4501,
            "ent_physical_index": "",
        }

        rendered = str(_table(device).render_actions(None, record))

        assert "Update Interface" not in rendered

    def test_interface_update_requires_interface_change_permission(self):
        device = make_device("table-interface-permission")
        record = {
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
        }

        rendered = str(_table(device, can_change_interface=False).render_actions(None, record))

        assert "Update Interface" not in rendered

    def test_carrier_option_posts_the_selected_real_ids(self):
        device = make_device("table-carrier")
        record = {
            "status": "No Bay",
            "carrier_install_options": [
                {"bay_id": 12, "module_type_id": 34, "module_type_name": "Carrier A", "bay_name": "Slot 0"}
            ],
        }

        rendered = str(_table(device).render_actions(None, record))

        assert "Install Carrier A into" in rendered
        assert 'name="module_bay_id" value="12"' in rendered
        assert 'name="module_type_id" value="34"' in rendered
        assert 'hx-target="#module-sync-content"' in rendered

    def test_replace_requires_add_change_and_delete_permissions(self):
        device = make_device("table-replace")
        record = {"can_replace": True, "installed_module_id": 55, "ent_physical_index": 200}

        for denied in ("can_add_module", "can_change_module", "can_delete_module"):
            assert "Replace" not in str(_table(device, **{denied: False}).render_actions(None, record))
        assert "Replace" in str(_table(device).render_actions(None, record))

    def test_replace_opens_the_preview_through_an_htmx_swap(self):
        device = make_device("table-replace-preview")
        record = {"can_replace": True, "installed_module_id": 55, "ent_physical_index": 200, "serial": "S1"}
        preview_url = reverse("plugins:netbox_librenms_plugin:module_mismatch_preview", kwargs={"pk": device.pk})

        rendered = str(_table(device).render_actions(None, record))

        assert "mdi-swap-horizontal" in rendered
        # The preview fragment carries hx- forms, so it must arrive through an HTMX swap: a
        # fetch()+innerHTML load leaves them unbound and they fall back to a full page reload.
        (button,) = [tag for tag in open_tags(rendered, "button") if tag.get("hx-get")]
        assert button["hx-get"] == (
            f"{preview_url}?module_id=55&ent_index=200&server_key=production&selected_device_id={device.pk}"
        )
        assert button["hx-target"] == "#htmx-modal-content"
        assert button["hx-swap"] == "innerHTML"
        # A newer click supersedes an older in-flight preview, as the AbortController did.
        assert button["hx-sync"] == "#htmx-modal-content:replace"
        assert button["hx-disabled-elt"] == "this"

    # One record per row action that posts through HTMX.
    _HTMX_ROW_ACTIONS = {
        "install": {"can_install": True, "module_bay_id": 1, "module_type_id": 2, "ent_physical_index": 32},
        "install_branch": {"has_installable_children": True, "ent_physical_index": 5},
        "update_serial": {
            "can_update_serial": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
            "serial": "S2",
        },
        "update_interface": {
            "can_update_interface_binding": True,
            "installed_module_id": 42,
            "ent_physical_index": 77,
            "librenms_port_id": 56284,
        },
        "carrier_install": {
            "status": "No Bay",
            "carrier_install_options": [
                {"bay_id": 12, "module_type_id": 34, "module_type_name": "CARRIER-A", "bay_name": "Slot 0"}
            ],
        },
    }

    @pytest.mark.parametrize("record", _HTMX_ROW_ACTIONS.values(), ids=list(_HTMX_ROW_ACTIONS))
    def test_row_form_drops_a_concurrent_module_action(self, record):
        """Two module actions in flight raced into one container: every row form takes the same lock."""
        device = make_device("table-row-action-lock")

        rendered = str(_table(device).render_actions(None, record))

        forms = [tag for tag in open_tags(rendered, "form") if tag.get("hx-post")]
        assert forms
        for form in forms:
            assert form["hx-sync"] == "#module-sync-content:drop"
            assert form["hx-target"] == "#module-sync-content"
            # The row spinner and the button lock stay: hx-sync only gates a SECOND action.
            assert form["hx-disabled-elt"] == "find button"

    def test_add_and_change_permissions_gate_their_own_actions(self):
        device = make_device("table-split-permissions")
        record = {
            "can_install": True,
            "module_bay_id": 1,
            "module_type_id": 2,
            "can_update_serial": True,
            "installed_module_id": 99,
            "ent_physical_index": 88,
            "serial": "SERIAL",
            "ent_physical_index": 99,
            "inventory_digest": "permission-row-digest",
        }

        add_only = str(_table(device, can_change_module=False).render_actions(None, record))
        change_only = str(_table(device, can_add_module=False).render_actions(None, record))

        assert "Install" in add_only and "Update Serial" not in add_only
        assert "Install" not in change_only and "Update Serial" in change_only

    def test_mapping_suggestions_use_real_mapping_routes(self):
        device = make_device("table-mapping")
        table = _table(device)
        bay_record = {
            "status": "No Bay",
            "model_suggestion": {
                "librenms_name": r"^0/(\d+)$",
                "librenms_class": "module",
                "netbox_bay_name": r"Slot \1",
                "is_regex": True,
                "description": "Suggested mapping",
            },
        }
        type_record = {
            "status": "No Type",
            "type_suggestion": {"librenms_model": "MODEL-A", "description": "Suggested type mapping"},
        }

        bay_action = str(table.render_actions("", bay_record))
        type_action = str(table.render_actions("", type_record))

        assert "Add Mapping" in bay_action
        assert "module-bay-mappings" in bay_action
        assert "is_regex=true" in bay_action
        assert "return_url=" in bay_action
        assert "Add Mapping" in type_action
        assert "module-type-mappings" in type_action

    def test_module_type_creation_drops_blank_prefill_values(self):
        device = make_device("table-type-create")
        record = {
            "status": "No Type",
            "module_type_create": {
                "model": "MODEL-A",
                "part_number": "MODEL-A",
                "description": "",
                "manufacturer": None,
            },
        }

        rendered = str(_table(device).render_actions("", record))

        assert "Add Module Type" in rendered
        assert "module-types/add" in rendered
        assert "model=MODEL-A" in rendered
        assert "description=" not in rendered
        assert "manufacturer=" not in rendered

    def test_creation_actions_are_hidden_without_the_specific_permission(self):
        device = make_device("table-create-permission")
        record = {
            "status": "No Type",
            "module_type_create": {"model": "MODEL-A", "part_number": "MODEL-A"},
        }

        rendered = str(_table(device, can_add_module_type=False).render_actions("", record))

        assert "Add Module Type" not in rendered


class TestTableLifecycle:
    def test_real_constructor_sets_table_state_and_selection_visibility(self):
        device = make_device("table-lifecycle")
        table = _table(device)
        read_only = _table(device, has_write_permission=False, can_add_module=False)

        assert table.device == device
        assert table.tab == "modules"
        assert table.prefix == "modules_"
        assert table.htmx_url is None
        assert table.columns["selection"].column.visible is True
        assert read_only.columns["selection"].column.visible is False

    def test_real_configure_uses_safe_browser_url_and_creates_csrf_token(self):
        device = make_device("table-configure")
        request = RequestFactory().get(
            "/plugins/librenms/table/",
            HTTP_HOST="testserver",
            HTTP_HX_CURRENT_URL="http://testserver/dcim/devices/1/?tab=modules",
        )
        request.user = make_superuser("table-configure")
        table = _table(device)

        table.configure(request)

        assert table.csrf_token
        assert table.return_url == "/dcim/devices/1/?tab=modules"
