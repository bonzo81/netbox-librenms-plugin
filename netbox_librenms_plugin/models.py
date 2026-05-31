import functools
import logging
import re

import yaml
from dcim.choices import InterfaceTypeChoices
from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel

logger = logging.getLogger(__name__)


def _validate_replacement_template(compiled: re.Pattern, replacement: str) -> None:
    """Verify that *replacement* is a valid back-reference template for *compiled*.

    ``re.sub(pattern, replacement, test_string)`` only evaluates group references
    when the pattern actually matches the test string.  Using the pattern text
    itself as the test string may silently accept an invalid replacement when the
    pattern does not match its own source (e.g. ``^(\\d+)$`` never matches the
    string ``^(\\d+)$``).

    This function constructs a synthetic test pattern with one trivial capture
    group per group in *compiled* (named groups are preserved so ``\\g<name>``
    references are validated correctly), guaranteeing a match and ensuring all
    back-references are exercised.

    Raises ``re.error`` or ``IndexError`` if the replacement is invalid.
    """
    n = compiled.groups
    if n == 0:
        test_pat = re.compile("a")
        test_str = "a"
    else:
        name_by_pos = {v: k for k, v in compiled.groupindex.items()}
        parts = [f"(?P<{name_by_pos[i]}>a)" if i in name_by_pos else "(a)" for i in range(1, n + 1)]
        test_pat = re.compile("".join(parts))
        test_str = "a" * n
    test_pat.sub(replacement, test_str)


class FullCleanOnSaveMixin:
    """Mixin that calls full_clean() on every save() so custom clean() logic runs even on programmatic saves."""

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class LibreNMSSettings(models.Model):
    """
    Model to store LibreNMS plugin settings, specifically which server to use
    when multiple servers are configured.
    """

    selected_server = models.CharField(
        max_length=100,
        default="default",
        help_text="The key of the selected LibreNMS server from configuration",
    )

    vc_member_name_pattern = models.CharField(
        max_length=100,
        default="-M{position}",
        help_text="Pattern for naming virtual chassis member devices. "
        "Available placeholders: {position}, {serial}. "
        "Example: '-M{position}' results in 'switch01-M2'",
    )

    use_sysname_default = models.BooleanField(
        default=True,
        help_text="Use SNMP sysName instead of LibreNMS hostname when importing devices",
    )

    strip_domain_default = models.BooleanField(
        default=False,
        help_text="Remove domain suffix from device names during import",
    )

    auto_create_ipam_default = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, missing IP addresses reported by LibreNMS are auto-created "
            "as global /32 (IPv4) or /128 (IPv6) IPAM records during initial import, "
            "OOB-attach, and promote-to-host actions. Existing IPAM records are always reused."
        ),
    )

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    class Meta:
        """Meta options for LibreNMSSettings."""

        verbose_name = "LibreNMS Settings"
        verbose_name_plural = "LibreNMS Settings"

    def get_absolute_url(self):
        """Return the URL for the settings page."""
        return reverse("plugins:netbox_librenms_plugin:settings")

    def __str__(self):
        return f"LibreNMS Settings - Server: {self.selected_server}"


class InterfaceTypeMapping(FullCleanOnSaveMixin, NetBoxModel):
    """Map LibreNMS interface types and speeds to NetBox interface types."""

    librenms_type = models.CharField(max_length=100)
    netbox_type = models.CharField(
        max_length=50,
        choices=InterfaceTypeChoices,
        default=InterfaceTypeChoices.TYPE_OTHER,
    )
    librenms_speed = models.BigIntegerField(null=True, blank=True)
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this interface type mapping",
    )

    def clean(self):
        """Enforce uniqueness for NULL-speed rows (SQL UNIQUE does not cover NULL = NULL)."""
        from django.core.exceptions import ValidationError

        super().clean()
        normalized_type = (self.librenms_type or "").strip()
        if not normalized_type:
            raise ValidationError({"librenms_type": "LibreNMS type must not be blank or whitespace-only."})
        self.librenms_type = normalized_type
        if self.librenms_speed is None:
            qs = InterfaceTypeMapping.objects.filter(
                librenms_type=normalized_type,
                librenms_speed__isnull=True,
            )
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    {"librenms_type": ("A wildcard (speed = any) mapping for this interface type already exists.")}
                )

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:interfacetypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for InterfaceTypeMapping."""

        constraints = [
            models.UniqueConstraint(
                fields=["librenms_type", "librenms_speed"],
                condition=models.Q(librenms_speed__isnull=False),
                name="unique_interface_type_mapping",
            ),
            models.UniqueConstraint(
                fields=["librenms_type"],
                condition=models.Q(librenms_speed__isnull=True),
                name="unique_interface_type_mapping_wildcard",
            ),
        ]
        ordering = ["librenms_type", "librenms_speed"]

    def __str__(self):
        return f"{self.librenms_type} + {self.librenms_speed} -> {self.netbox_type}"

    def to_yaml(self):
        data = {
            "librenms_type": self.librenms_type,
            "librenms_speed": self.librenms_speed,
            "netbox_type": self.netbox_type,
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class DeviceTypeMapping(FullCleanOnSaveMixin, NetBoxModel):
    """Map LibreNMS hardware strings to NetBox DeviceType objects."""

    librenms_hardware = models.CharField(
        max_length=255,
        unique=True,
        help_text="Hardware string as reported by LibreNMS (e.g., 'Juniper MX480 Internet Backbone Router')",
    )
    netbox_device_type = models.ForeignKey(
        DeviceType,
        on_delete=models.CASCADE,
        related_name="librenms_device_type_mappings",
        help_text="The NetBox DeviceType this hardware string maps to",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def clean(self):
        """Normalize librenms_hardware to lowercase so case-variant duplicates are prevented at save time."""
        super().clean()
        self.librenms_hardware = (self.librenms_hardware or "").strip().lower()
        if not self.librenms_hardware:
            raise ValidationError({"librenms_hardware": "This field may not be blank after normalization."})

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:devicetypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for DeviceTypeMapping."""

        ordering = ["librenms_hardware"]

    def __str__(self):
        return f"{self.librenms_hardware} -> {self.netbox_device_type}"

    def to_yaml(self):
        data = {
            "librenms_hardware": self.librenms_hardware,
            "netbox_device_type": str(self.netbox_device_type),
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class ModuleTypeMapping(FullCleanOnSaveMixin, NetBoxModel):
    """Map LibreNMS inventory model names to NetBox ModuleType objects."""

    librenms_model = models.CharField(
        max_length=255,
        help_text="Model name from LibreNMS inventory (entPhysicalModelName)",
    )
    netbox_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        related_name="librenms_module_type_mappings",
        help_text="The NetBox ModuleType this model name maps to",
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="librenms_module_type_mappings",
        help_text=(
            "Optional: scope this mapping to one manufacturer (matches the device's "
            "device_type.manufacturer). Leave blank to apply across vendors. When both a "
            "manufacturer-scoped and a global mapping exist for the same librenms_model, "
            "the manufacturer-scoped row wins for devices of that vendor."
        ),
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def clean(self):
        """Normalize librenms_model so whitespace-padded values don't create duplicate entries."""
        super().clean()
        self.librenms_model = (self.librenms_model or "").strip()
        if not self.librenms_model:
            raise ValidationError({"librenms_model": "This field may not be blank after normalization."})

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:moduletypemapping_detail", args=[self.pk])

    class Meta:
        """Meta options for ModuleTypeMapping."""

        ordering = ["librenms_model"]
        # UniqueConstraint over (librenms_model, manufacturer):
        # PostgreSQL <13 treats NULL values as distinct in unique indexes, so
        # the nullable manufacturer FK lets two "global" rows (manufacturer
        # IS NULL) with the same librenms_model coexist. Splitting into a
        # conditional pair (NOT NULL + NULL) makes the constraint enforce
        # uniqueness in both cases on every supported PG version.
        constraints = [
            models.UniqueConstraint(
                fields=["librenms_model", "manufacturer"],
                condition=models.Q(manufacturer__isnull=False),
                name="unique_module_type_mapping",
            ),
            models.UniqueConstraint(
                fields=["librenms_model"],
                condition=models.Q(manufacturer__isnull=True),
                name="unique_module_type_mapping_global",
            ),
        ]

    def __str__(self):
        mfr = f" ({self.manufacturer.name})" if self.manufacturer_id else ""
        return f"{self.librenms_model}{mfr} -> {self.netbox_module_type}"

    def to_yaml(self):
        data = {
            "librenms_model": self.librenms_model,
            "manufacturer": self.manufacturer.name if self.manufacturer_id else "",
            "netbox_module_type": str(self.netbox_module_type),
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class ModuleBayMapping(FullCleanOnSaveMixin, NetBoxModel):
    """
    Map LibreNMS inventory names to NetBox module bay names.

    Used when LibreNMS inventory names don't match NetBox bay names exactly.
    For example: LibreNMS "Power Supply 1" → NetBox "PS1".
    When is_regex is True, librenms_name is treated as a regex pattern and
    netbox_bay_name can use backreferences (\\1, \\2, etc.).
    Mappings are global (not scoped to device type or manufacturer).
    """

    librenms_name = models.CharField(
        max_length=255,
        help_text="Name from LibreNMS inventory (entPhysicalName). "
        "When 'Use Regex' is enabled, this is a Python regex pattern.",
    )
    librenms_class = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional entPhysicalClass filter (e.g. 'powerSupply', 'fan', 'module')",
    )
    netbox_bay_name = models.CharField(
        max_length=255,
        help_text="NetBox module bay name to match. With regex, supports backreferences (\\1, \\2, etc.).",
    )
    is_regex = models.BooleanField(
        default=False,
        help_text="Treat LibreNMS Name as a regex pattern with backreferences in NetBox Bay Name",
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="librenms_module_bay_mappings",
        help_text="Optional: scope this mapping to one manufacturer (matches the device's "
        "device_type.manufacturer). Leave blank to apply across vendors. When both a "
        "vendor-scoped and a global mapping match, the vendor-scoped one wins.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    @functools.cached_property
    def _compiled_pattern(self):
        """Compiled regex for is_regex=True mappings; None for exact mappings or invalid patterns."""
        if not self.is_regex or not self.librenms_name:
            return None
        try:
            return re.compile(self.librenms_name)
        except re.error:
            return None

    def clean(self):
        """Validate that regex patterns compile when is_regex is True."""
        super().clean()
        # Invalidate cached compiled pattern so it's recomputed from the new value
        self.__dict__.pop("_compiled_pattern", None)
        librenms_name_stripped = self.librenms_name.strip() if self.librenms_name else ""
        if not librenms_name_stripped:
            raise ValidationError({"librenms_name": "LibreNMS name pattern must not be empty or whitespace-only."})
        self.librenms_name = librenms_name_stripped
        # Strip class too — whitespace-padded values form spurious distinct rows under unique_together.
        self.librenms_class = self.librenms_class.strip() if self.librenms_class else ""
        # Strip netbox_bay_name — whitespace-padded values would fail regex substitution.
        netbox_bay_name_stripped = self.netbox_bay_name.strip() if self.netbox_bay_name else ""
        if not netbox_bay_name_stripped:
            raise ValidationError({"netbox_bay_name": "NetBox bay name must not be empty or whitespace-only."})
        self.netbox_bay_name = netbox_bay_name_stripped
        if self.is_regex:
            try:
                pattern = re.compile(self.librenms_name)
            except re.error as e:
                raise ValidationError({"librenms_name": f"Invalid regex: {e}"})
            try:
                _validate_replacement_template(pattern, self.netbox_bay_name)
            except (re.error, IndexError) as e:
                raise ValidationError({"netbox_bay_name": f"Invalid replacement: {e}"})

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:modulebaymapping_detail", args=[self.pk])

    class Meta:
        """Meta options for ModuleBayMapping."""

        # Two conditional UniqueConstraints rather than a single
        # UniqueConstraint over (librenms_name, librenms_class, manufacturer):
        # PostgreSQL treats NULL ≠ NULL, so a single constraint that includes
        # the nullable manufacturer FK lets two "global" rows (manufacturer
        # IS NULL) with otherwise identical fields slip through. The
        # cleaner ``nulls_distinct=False`` option requires PostgreSQL 15+
        # (Django 5.2+), but NetBox 4.2 still supports PostgreSQL 12, so we
        # split into one constraint per branch instead. Issue #71.
        constraints = [
            models.UniqueConstraint(
                fields=["librenms_name", "librenms_class", "manufacturer"],
                condition=models.Q(manufacturer__isnull=False),
                name="unique_module_bay_mapping",
            ),
            models.UniqueConstraint(
                fields=["librenms_name", "librenms_class"],
                condition=models.Q(manufacturer__isnull=True),
                name="unique_module_bay_mapping_global",
            ),
        ]
        ordering = ["librenms_name"]

    def __str__(self):
        cls = f" [{self.librenms_class}]" if self.librenms_class else ""
        mfr = f" ({self.manufacturer.name})" if self.manufacturer_id else ""
        return f"{self.librenms_name}{cls}{mfr} -> {self.netbox_bay_name}"

    def to_yaml(self):
        data = {
            "librenms_name": self.librenms_name,
            "librenms_class": self.librenms_class,
            "netbox_bay_name": self.netbox_bay_name,
            "is_regex": self.is_regex,
            "manufacturer": self.manufacturer.name if self.manufacturer_id else "",
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class NormalizationRule(FullCleanOnSaveMixin, NetBoxModel):
    """
    Regex-based string normalization applied before matching lookups.

    Generic building block: a single rule engine handles normalization
    for module types, device types, module bays, and future scopes.
    Rules are applied in priority order; each transforms the string
    for the next rule in the chain.

    Example – strip Nokia revision suffixes:
        scope:       module_type
        match_pattern:  ^(3HE\\w{5}[A-Z]{2})[A-Z]{2}\\d{2}$
        replacement:    \\1
        Result: 3HE16474AARA01 → 3HE16474AA
    """

    SCOPE_MODULE_TYPE = "module_type"
    SCOPE_DEVICE_TYPE = "device_type"
    SCOPE_MODULE_BAY = "module_bay"

    SCOPE_CHOICES = [
        (SCOPE_MODULE_TYPE, "Module Type"),
        (SCOPE_DEVICE_TYPE, "Device Type"),
        (SCOPE_MODULE_BAY, "Module Bay"),
    ]

    scope = models.CharField(
        max_length=50,
        choices=SCOPE_CHOICES,
        db_index=True,
        help_text="Which matching lookup this rule applies to",
    )
    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="normalization_rules",
        help_text="Optional: only apply this rule to items from this manufacturer. "
        "Leave blank for vendor-agnostic rules.",
    )
    match_pattern = models.CharField(
        max_length=500,
        help_text="Regex pattern to match against input string (Python re syntax)",
    )
    replacement = models.CharField(
        max_length=500,
        help_text="Replacement string (supports regex back-references \\1, \\2, …)",
    )
    priority = models.PositiveIntegerField(
        default=100,
        help_text="Lower values run first. Rules chain: each transforms the output of the previous.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this rule",
    )

    def clean(self):
        """Validate that match_pattern compiles as a regex and replacement is a valid template."""
        super().clean()
        errors = {}
        if not self.match_pattern:
            errors["match_pattern"] = "This field is required."
        if self.replacement is None:
            errors["replacement"] = "This field is required."
        if errors:
            raise ValidationError(errors)
        try:
            compiled = re.compile(self.match_pattern)
        except re.error as e:
            raise ValidationError({"match_pattern": f"Invalid regex: {e}"})
        # Validate the replacement template by running a dummy substitution
        try:
            _validate_replacement_template(compiled, self.replacement)
        except (re.error, IndexError) as e:
            raise ValidationError({"replacement": f"Invalid replacement template: {e}"})

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:normalizationrule_detail", args=[self.pk])

    class Meta:
        """Meta options for NormalizationRule."""

        ordering = ["scope", "priority", "pk"]

    def __str__(self):
        return f"[{self.get_scope_display()}] {self.match_pattern} → {self.replacement}"

    def to_yaml(self):
        data = {
            "scope": self.scope,
            "manufacturer": str(self.manufacturer) if self.manufacturer else None,
            "match_pattern": self.match_pattern,
            "replacement": self.replacement,
            "priority": self.priority,
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class InventoryIgnoreRule(FullCleanOnSaveMixin, NetBoxModel):
    """
    Rule-based filter for ENTITY-MIB inventory items during module sync.

    Two use-cases are supported, controlled by the ``action`` field:

    **Skip** (``action='skip'``)
        The matched item is removed from the sync table entirely.  Used for
        phantom EEPROM/IDPROM child entities that Cisco IOS-XR reports with the
        same model and serial as the real parent module.

    **Transparent** (``action='transparent'``)
        The matched item's row is hidden, but its ENTITY-MIB children are
        *promoted* to device-level bay matching instead of being treated as
        sub-components.  Used for fixed-chassis devices (e.g. Cisco 8201-SYS)
        where the RP/system-board entity is the device itself — it carries the
        same serial number as the NetBox device, so its children (transceivers,
        fans, PSUs) should be matched directly against device-level bays.

    Match types:
        ``ends_with / starts_with / contains / regex``
            Compare ``entPhysicalName``.  Use ``require_serial_match_parent``
            as a safety net to avoid false positives.
        ``serial_matches_device``
            Match when the item's ``entPhysicalSerialNum`` equals the NetBox
            device's own serial number.  No ``pattern`` is required.
            Pair with ``action='transparent'`` for embedded-RP detection.
    """

    # --- action ---
    ACTION_SKIP = "skip"
    ACTION_TRANSPARENT = "transparent"
    ACTION_CHOICES = [
        (ACTION_SKIP, "Skip (remove from table)"),
        (ACTION_TRANSPARENT, "Transparent (hide row, promote children to device level)"),
    ]

    # --- match_type ---
    MATCH_ENDS_WITH = "ends_with"
    MATCH_STARTS_WITH = "starts_with"
    MATCH_CONTAINS = "contains"
    MATCH_REGEX = "regex"
    MATCH_SERIAL_DEVICE = "serial_matches_device"

    MATCH_TYPE_CHOICES = [
        (MATCH_ENDS_WITH, "Ends with (entPhysicalName)"),
        (MATCH_STARTS_WITH, "Starts with (entPhysicalName)"),
        (MATCH_CONTAINS, "Contains (entPhysicalName)"),
        (MATCH_REGEX, "Regex (entPhysicalName)"),
        (MATCH_SERIAL_DEVICE, "Serial matches device (entPhysicalSerialNum = Device.serial)"),
    ]

    name = models.CharField(
        max_length=100,
        help_text="Short descriptive label for this rule",
    )
    match_type = models.CharField(
        max_length=25,
        choices=MATCH_TYPE_CHOICES,
        default=MATCH_ENDS_WITH,
        help_text="How to match the inventory item",
    )
    pattern = models.CharField(
        max_length=200,
        blank=True,
        help_text="Pattern to match against entPhysicalName. "
        "Case-insensitive for ends_with / starts_with / contains; "
        "Python re syntax for regex. "
        "Not used for serial_matches_device.",
    )
    action = models.CharField(
        max_length=15,
        choices=ACTION_CHOICES,
        default=ACTION_SKIP,
        help_text="What to do when this rule matches: skip the item entirely, "
        "or hide its row and promote its children to device-level bay matching.",
    )
    require_serial_match_parent = models.BooleanField(
        default=True,
        help_text="(Name-based rules only) Only apply this rule if the item's serial "
        "number matches an ancestor entity's serial number.  Recommended to "
        "prevent false positives.  Ignored for serial_matches_device rules.",
    )
    enabled = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Uncheck to temporarily disable this rule without deleting it",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional notes about this rule (vendor, firmware version, etc.)",
    )

    def clean(self):
        """Validate pattern/match_type consistency."""
        super().clean()
        # Invalidate cached compiled pattern so it's recomputed from the new value
        self.__dict__.pop("_compiled_pattern", None)
        pattern_stripped = self.pattern.strip() if self.pattern else ""
        if self.match_type == self.MATCH_REGEX and pattern_stripped:
            try:
                re.compile(pattern_stripped)
            except re.error as e:
                raise ValidationError({"pattern": f"Invalid regex: {e}"})
        if self.match_type != self.MATCH_SERIAL_DEVICE and not pattern_stripped:
            raise ValidationError({"pattern": "Pattern is required for name-based match types."})
        # Normalize stored pattern to the stripped form so matches_name() and
        # clean() always operate on the same string.
        self.pattern = pattern_stripped

    @functools.cached_property
    def _compiled_pattern(self):
        """Compiled regex for MATCH_REGEX rules; None for other match types or invalid patterns."""
        if self.match_type != self.MATCH_REGEX or not self.pattern:
            return None
        try:
            return re.compile(self.pattern)
        except re.error:
            return None

    def matches_name(self, name: str) -> bool:
        """Return True if *name* matches this rule's pattern/match_type (name-based rules only)."""
        if not name or self.match_type == self.MATCH_SERIAL_DEVICE:
            return False
        if not self.pattern or not self.pattern.strip():
            return False
        if self.match_type == self.MATCH_REGEX:
            compiled = self._compiled_pattern
            if compiled is None:
                logger.error(
                    "Invalid regex in InventoryIgnoreRule pk=%s pattern=%r — skipping",
                    self.pk,
                    self.pattern,
                )
                return False
            try:
                return bool(compiled.search(name))
            except re.error as exc:
                logger.error(
                    "Regex error in InventoryIgnoreRule pk=%s pattern=%r name=%r: %s — skipping",
                    self.pk,
                    self.pattern,
                    name,
                    exc,
                )
                return False
        name_up = name.upper()
        pat = self.pattern.upper()
        if self.match_type == self.MATCH_ENDS_WITH:
            return name_up.endswith(pat)
        if self.match_type == self.MATCH_STARTS_WITH:
            return name_up.startswith(pat)
        if self.match_type == self.MATCH_CONTAINS:
            return pat in name_up
        return False

    def get_absolute_url(self):
        """Return the URL for this rule's detail page."""
        return reverse("plugins:netbox_librenms_plugin:inventoryignorerule_detail", args=[self.pk])

    class Meta:
        """Meta options for InventoryIgnoreRule."""

        ordering = ["name", "pk"]

    def __str__(self):
        if self.match_type == self.MATCH_SERIAL_DEVICE:
            return f"{self.name}: {self.get_match_type_display()}"
        serial_note = " [serial match]" if self.require_serial_match_parent else ""
        return f"{self.name}: {self.get_match_type_display()} '{self.pattern}'{serial_note}"

    def to_yaml(self):
        data = {
            "name": self.name,
            "match_type": self.match_type,
            "pattern": self.pattern,
            "action": self.action,
            "require_serial_match_parent": self.require_serial_match_parent,
            "enabled": self.enabled,
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class PlatformMapping(FullCleanOnSaveMixin, NetBoxModel):
    """Map LibreNMS OS strings to NetBox Platform objects."""

    librenms_os = models.CharField(
        max_length=255,
        unique=True,
        help_text="OS string as reported by LibreNMS (e.g., 'ios', 'eos', 'junos')",
    )
    netbox_platform = models.ForeignKey(
        Platform,
        on_delete=models.CASCADE,
        related_name="librenms_platform_mappings",
        help_text="The NetBox Platform this OS string maps to",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this mapping",
    )

    def clean(self):
        """Normalize librenms_os to lowercase so case-variant duplicates are prevented at save time."""
        super().clean()
        self.librenms_os = (self.librenms_os or "").strip().lower()
        if not self.librenms_os:
            raise ValidationError({"librenms_os": "This field may not be blank after normalization."})

    def get_absolute_url(self):
        """Return the URL for this mapping's detail page."""
        return reverse("plugins:netbox_librenms_plugin:platformmapping_detail", args=[self.pk])

    class Meta:
        """Meta options for PlatformMapping."""

        ordering = ["librenms_os"]

    def __str__(self):
        return f"{self.librenms_os} -> {self.netbox_platform}"

    def to_yaml(self):
        data = {
            "librenms_os": self.librenms_os,
            "netbox_platform": str(self.netbox_platform),
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)


class CarrierAutoInstallRule(FullCleanOnSaveMixin, NetBoxModel):
    """
    User-configurable suggestion rule for missing holder/carrier modules.

    Some chassis (e.g. Nokia 7750 SR-s with CMA controller carriers, mezzanine
    carriers, line-card cassettes) expose a holder bay at the chassis level
    whose nested child bays only become available once the carrier ModuleType
    is installed in NetBox. LibreNMS does not report the carrier itself, only
    its children (CPMs, MDAs, mezzanines), so they appear as orphan "No Bay"
    rows. A rule lets the user say: "for Nokia 7750 SR chassis, when LibreNMS
    reports a cpmModule named '^Slot [AB]$' and the chassis has an empty bay
    named '^CMA$', suggest installing carrier_module_type into that bay."

    Suggest-only — the user clicks an Install button to apply. No vendor data
    ships with the plugin; rules are loaded from the UI or contrib YAML.
    """

    manufacturer = models.ForeignKey(
        Manufacturer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="librenms_carrier_install_rules",
        help_text="Optional: scope this rule to one manufacturer (matches the device's "
        "device_type.manufacturer). Leave blank to apply across vendors.",
    )
    device_type_pattern = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional regex (Python re.fullmatch) on the device_type model name. "
        "Leave blank to apply to all device types of the selected manufacturer.",
    )
    librenms_child_class = models.CharField(
        max_length=50,
        help_text="Exact entPhysicalClass match for the orphan child reported by LibreNMS "
        "(e.g. cpmModule, mdaModule, fabricModule).",
    )
    librenms_child_name_pattern = models.CharField(
        max_length=255,
        help_text="Regex (Python re.fullmatch) on the orphan child's entPhysicalName (e.g. '^Slot [AB]$').",
    )
    netbox_bay_name_pattern = models.CharField(
        max_length=255,
        help_text="Regex (Python re.fullmatch) on the chassis-level empty module bay name "
        "where the carrier should be installed (e.g. '^CMA$' or '^Carrier \\d+$'). "
        "All matching empty bays will be offered as install targets.",
    )
    carrier_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.PROTECT,
        related_name="librenms_carrier_install_rules",
        help_text="The NetBox ModuleType to suggest installing into the matching empty bay.",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional notes about this rule.",
    )

    @functools.cached_property
    def _compiled_device_type_pattern(self):
        if not self.device_type_pattern:
            return None
        try:
            return re.compile(self.device_type_pattern)
        except re.error:
            return None

    @functools.cached_property
    def _compiled_child_name_pattern(self):
        if not self.librenms_child_name_pattern:
            return None
        try:
            return re.compile(self.librenms_child_name_pattern)
        except re.error:
            return None

    @functools.cached_property
    def _compiled_bay_name_pattern(self):
        if not self.netbox_bay_name_pattern:
            return None
        try:
            return re.compile(self.netbox_bay_name_pattern)
        except re.error:
            return None

    def clean(self):
        super().clean()
        # Invalidate cached compiled patterns so they recompute from new values.
        self.__dict__.pop("_compiled_device_type_pattern", None)
        self.__dict__.pop("_compiled_child_name_pattern", None)
        self.__dict__.pop("_compiled_bay_name_pattern", None)

        self.device_type_pattern = (self.device_type_pattern or "").strip()
        self.librenms_child_class = (self.librenms_child_class or "").strip()
        self.librenms_child_name_pattern = (self.librenms_child_name_pattern or "").strip()
        self.netbox_bay_name_pattern = (self.netbox_bay_name_pattern or "").strip()

        if not self.librenms_child_class:
            raise ValidationError({"librenms_child_class": "This field is required."})
        if not self.librenms_child_name_pattern:
            raise ValidationError({"librenms_child_name_pattern": "This field is required."})
        if not self.netbox_bay_name_pattern:
            raise ValidationError({"netbox_bay_name_pattern": "This field is required."})

        for field, value in (
            ("device_type_pattern", self.device_type_pattern),
            ("librenms_child_name_pattern", self.librenms_child_name_pattern),
            ("netbox_bay_name_pattern", self.netbox_bay_name_pattern),
        ):
            if not value:
                continue
            try:
                re.compile(value)
            except re.error as e:
                raise ValidationError({field: f"Invalid regex: {e}"})

    def get_absolute_url(self):
        return reverse(
            "plugins:netbox_librenms_plugin:carrierautoinstallrule_detail",
            args=[self.pk],
        )

    class Meta:
        """Meta options for CarrierAutoInstallRule."""

        ordering = ["manufacturer__name", "librenms_child_class", "librenms_child_name_pattern"]
        # See ModuleBayMapping.Meta.constraints for the full rationale: the
        # nullable manufacturer FK forces a pair of conditional
        # UniqueConstraints because PostgreSQL 12-14 (still supported by
        # NetBox 4.2) does not honour ``nulls_distinct=False``. Issue #71.
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "manufacturer",
                    "device_type_pattern",
                    "librenms_child_class",
                    "librenms_child_name_pattern",
                    "netbox_bay_name_pattern",
                ],
                condition=models.Q(manufacturer__isnull=False),
                name="unique_carrier_auto_install_rule",
            ),
            models.UniqueConstraint(
                fields=[
                    "device_type_pattern",
                    "librenms_child_class",
                    "librenms_child_name_pattern",
                    "netbox_bay_name_pattern",
                ],
                condition=models.Q(manufacturer__isnull=True),
                name="unique_carrier_auto_install_rule_global",
            ),
        ]

    def __str__(self):
        scope = self.manufacturer.name if self.manufacturer else "*"
        if self.device_type_pattern:
            scope = f"{scope}/{self.device_type_pattern}"
        return (
            f"{scope}: {self.librenms_child_class} '{self.librenms_child_name_pattern}'"
            f" -> install {self.carrier_module_type} into '{self.netbox_bay_name_pattern}'"
        )

    def to_yaml(self):
        data = {
            "manufacturer": self.manufacturer.name if self.manufacturer else "",
            "device_type_pattern": self.device_type_pattern,
            "librenms_child_class": self.librenms_child_class,
            "librenms_child_name_pattern": self.librenms_child_name_pattern,
            "netbox_bay_name_pattern": self.netbox_bay_name_pattern,
            "carrier_module_type": str(self.carrier_module_type),
            "description": self.description,
        }
        return yaml.dump(data, sort_keys=False)
