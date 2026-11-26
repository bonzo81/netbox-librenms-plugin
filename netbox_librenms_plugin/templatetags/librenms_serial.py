"""Template filters for the serial text that LibreNMS reports."""

from django import template

from netbox_librenms_plugin.serial_identity import page_text

register = template.Library()
register.filter("page_text", page_text)
