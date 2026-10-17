"""Parse LibreNMS identifiers from stored values and API payloads."""

import re

# Bounded at 19 digits, the width of a PostgreSQL bigint. Without the bound an oversized string is
# rejected only by CPython's int_max_str_digits limit, which a host may raise or disable.
_ID_TEXT_SPACE = r"[ \t\r\n\f\v]*"
_ID_TEXT_SIGN = r"\+?"
_ID_TEXT_MAX_DIGITS = 19
_ASCII_POSITIVE_INTEGER_RE = re.compile(
    rf"^{_ID_TEXT_SPACE}{_ID_TEXT_SIGN}[0-9]{{1,{_ID_TEXT_MAX_DIGITS}}}{_ID_TEXT_SPACE}$"
)


def coerce_librenms_id(value) -> int | None:
    """
    Coerce a raw LibreNMS ID value (int or string-digit) to int, or None.

    Accepts only ``int`` and ``str`` — other types (None, dicts, MagicMocks, etc.)
    return None. Booleans are rejected because ``bool`` is a subclass of ``int`` in
    Python, so ``int(True)`` silently becomes ``1`` — a valid-looking device ID. Zero
    and negative values are also rejected since LibreNMS IDs are strictly positive
    integers. An int wider than 19 digits is rejected like its text form.

    Args:
        value: The raw LibreNMS id value to coerce.

    Returns:
        int | None: The positive integer id, or None if it can't be coerced.

    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 10**_ID_TEXT_MAX_DIGITS else None
    if isinstance(value, str):
        if not _ASCII_POSITIVE_INTEGER_RE.fullmatch(value):
            return None
        try:
            coerced = int(value)
        except ValueError:
            return None
        return coerced if coerced > 0 else None
    return None


def librenms_id_text_pattern(*values: int) -> str:
    """Return one SQL regex for the stored text forms of these coerce_librenms_id() results that it also reads."""
    forms = "|".join(f"0{{0,{_ID_TEXT_MAX_DIGITS - len(str(value))}}}{value}" for value in values)
    return rf"^{_ID_TEXT_SPACE}{_ID_TEXT_SIGN}(?:{forms}){_ID_TEXT_SPACE}$"


def normalize_librenms_port_id(value) -> int | None:
    """Normalize a LibreNMS port_id to a positive integer, or None."""
    return coerce_librenms_id(value)
