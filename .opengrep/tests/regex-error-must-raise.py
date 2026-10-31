import logging
import re
import re as _re

from django.core.exceptions import ValidationError

from netbox_librenms_plugin.models import RuleConfigurationError
from netbox_librenms_plugin.utils import REGEX_COMPILE_ERRORS

logger = logging.getLogger(__name__)


def returns_none(rule):
    # ruleid: regex-error-must-raise
    try:
        return re.compile(rule.pattern)
    except REGEX_COMPILE_ERRORS:
        return None


def skips_in_a_loop(rules, value):
    for rule in rules:
        # ruleid: regex-error-must-raise
        try:
            value = re.sub(rule.match_pattern, rule.replacement, value)
        except (*REGEX_COMPILE_ERRORS, IndexError):
            logger.error("Invalid regex in rule %s, skipping", rule.pk)
    return value


def skips_a_replacement(match, mapping):
    # ruleid: regex-error-must-raise
    try:
        return match.expand(mapping.netbox_bay_name)
    except (re.error, IndexError):
        return None


def logs_through_an_alias(pattern):
    # ruleid: regex-error-must-raise
    try:
        return _re.compile(pattern)
    except _re.error as exc:
        logger.warning("Skipping invalid pattern %r: %s", pattern, exc)
        return None


def raises_the_typed_error(rule, field):
    # ok: regex-error-must-raise
    try:
        return re.compile(getattr(rule, field))
    except REGEX_COMPILE_ERRORS as exc:
        raise RuleConfigurationError(rule, field) from exc


def raises_a_validation_error(value):
    # ok: regex-error-must-raise
    try:
        return re.compile(value)
    except (re.error, IndexError) as exc:
        logger.debug("Rejected pattern")
        raise ValidationError({"pattern": "Invalid regex."}) from exc


def raises_from_a_bare_handler(rule, field):
    # ok: regex-error-must-raise
    try:
        return re.compile(getattr(rule, field))
    except REGEX_COMPILE_ERRORS:
        raise RuleConfigurationError(rule, field) from None


def re_raises_from_a_bare_alias_handler(pattern):
    # ok: regex-error-must-raise
    try:
        return _re.compile(pattern)
    except _re.error:
        logger.debug("Rejected pattern")
        raise


def re_raises_the_bound_error(pattern):
    # ok: regex-error-must-raise
    try:
        return re.compile(pattern)
    except re.error as exc:
        logger.debug("Rejected pattern: %s", exc)
        raise


def re_raises_from_a_tuple_handler(pattern):
    # ok: regex-error-must-raise
    try:
        return re.compile(pattern)
    except (re.error, IndexError) as exc:
        logger.debug("Rejected pattern: %s", exc)
        raise


def raises_without_a_cause_from_a_bare_handler(value):
    # ok: regex-error-must-raise
    try:
        return re.compile(value)
    except re.error:
        raise ValidationError({"pattern": "Invalid regex."})


def catches_another_error(value):
    # ok: regex-error-must-raise
    try:
        return int(value)
    except ValueError:
        return None
