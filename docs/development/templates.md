# Templates

Templates are located in `templates/netbox_librenms_plugin/` and follow NetBox's conventions, using Django's template language. The plugin uses a combination of base templates, partials, and includes to keep the UI modular and maintainable.

### Structure and Conventions
- **Base templates** (e.g., `librenms_sync_base.html`, `interfacetypemapping.html`) typically extend NetBox's generic templates (like `generic/object.html` or `generic/object_list.html`).
- **Partials and includes** (e.g., `_interface_sync.html`, `_interface_sync_content.html`, `_cable_sync.html`) are used for reusable UI components and AJAX/HTMX content updates.
- **The `inc/` directory** contains shared fragments, such as pagination controls (`paginator.html`).

### Customization and Inheritance
- Use the Django template tag `extends` to build on top of NetBox or plugin base templates, and the `block` tag to override or inject content.
- Use the Django template tag `include` for reusable sections (e.g., tables, forms, or modal dialogs).
- Static assets (JS/CSS) are loaded with the Django template tag `load static` and referenced using the `static` tag.
- Context variables and template tags (e.g., `helpers`, `plugins`, `render_table`) are used to render dynamic content and integrate with NetBox features.

### Examples
**Sync Views:**

  - `librenms_sync_base.html` provides the main layout for device/VM sync pages, extending NetBox's object template and including custom blocks for status, actions, and content.
  - `_interface_sync.html` and `_interface_sync_content.html` are used for the interface sync tab, supporting dynamic updates and user actions (like syncing selected interfaces).

**Mapping Views:**

  - `interfacetypemapping.html` and `interfacetypemapping_list.html` display and manage interface type mappings, using table layouts and info alerts.


For more on NetBox's template system, see the [NetBox documentation](https://netbox.readthedocs.io/en/stable/plugins/development/#templates).
