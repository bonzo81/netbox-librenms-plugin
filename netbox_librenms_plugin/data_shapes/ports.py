"""
Port-level primitives shared by the data-shape signature, compressor and anonymizer.

All three must agree on what a port is NAMED, whether it carries VLAN data, and whether it is a LAG
aggregate — a second copy of any of those rules drifts, and the compressor then drops a port whose
shape the signature reads (or the anonymizer hashes a name the signature matches). This module owns
them, and depends on nothing else in the package so every consumer can import it.
"""

import re


def port_has_vlan(port):
    """
    Return whether a port row carries real VLAN data (the signature's ``vlans`` axis predicate).

    Keyed on the VALUE, not mere key presence: ``ifVlan`` ``None``/``""``/``0``/``"0"`` (LibreNMS's
    no-/default-VLAN sentinels — ifVlan is string-valued JSON elsewhere in the client, so the
    string ``"0"`` is the same sentinel as the int ``0``) do NOT count, only a real id or a
    non-empty ``vlans`` list. The compressor's port fingerprint imports this so its VLAN axis stays
    in lockstep with the signature (otherwise two same-shape ports — one with ``ifVlan: 0``/``None``
    — could collapse to a representative whose value flips the signature's vlans axis).
    """
    return port.get("ifVlan") not in (None, "", 0, "0") or bool(port.get("vlans"))


# Upper bound on the interface name fed to an untrusted (recording-supplied) LAG regex. This trims the
# input a well-behaved pattern scans; it is NOT a ReDoS defense on its own — a nested unbounded
# quantifier backtracks exponentially in the input length, so no practical length cap tames it (that's
# what _is_redos_prone below is for). Real interface names are well under this; a longer one is
# anonymized garbage that never needs LAG name-pattern classification.
_MAX_LAG_NAME_LEN = 256

# lag_patterns in a community-submitted recording are UNTRUSTED regexes that --validate compiles and
# matches in CI. A length cap on the search input cannot bound catastrophic backtracking, so refuse to
# compile a pattern whose structure is the classic ReDoS shape — a group that itself contains an
# unbounded quantifier and is again unbounded-quantified (``^(a+)+$``, ``(a*)*``, ``(a+){2,}``) — and
# cap how many patterns are compiled at all. This is a heuristic, not a guarantee (it won't catch every
# pathological regex, e.g. overlapping alternation); the real gates remain human review of submissions
# and the CI job timeout. A skipped pattern is simply not used for LAG-name classification (a lossless
# nudge), exactly like the non-string/typo'd patterns already skipped below.
_MAX_LAG_PATTERNS = 100
# Bound the untrusted pattern length before running the detector on it: a real LAG pattern is short
# (``^Bundle-Ether\d+$`` is ~17 chars), and capping keeps the O(n^2) worst case of the detector's own
# scan on a pathological all-``(`` string trivially small — an over-long pattern is garbage/suspect and
# never needs LAG classification, so it's treated as ReDoS-prone (skipped) too.
_MAX_LAG_PATTERN_LEN = 200
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+]|\([^()]*[*+][^()]*\)\{\d*,\}")


def is_redos_prone(pattern):
    """
    Return whether *pattern* is unsafe to compile+match as an untrusted LAG regex.

    Rejects a non-string, an over-long pattern (garbage/suspect, and it bounds this check's own cost),
    and the classic catastrophic-backtracking shape — a group that itself contains an unbounded
    quantifier and is again unbounded-quantified (``^(a+)+$``, ``(a*)*``, ``(a+){2,}``).

    Args:
        pattern: A candidate regex string (untrusted, from a recording's ``lag_patterns``).

    Returns:
        True if the pattern must be skipped rather than compiled and applied.
    """
    if not isinstance(pattern, str) or len(pattern) > _MAX_LAG_PATTERN_LEN:
        return True
    return _NESTED_QUANTIFIER_RE.search(pattern) is not None


def compile_lag_patterns(recording):
    """
    Compile a recording's ``lag_patterns`` into usable regexes, skipping the unsafe ones.

    The single place a recording's (untrusted, community-submitted) LAG patterns are turned into
    regexes: the signature, the port compressor and the anonymizer all classify LAG names by these,
    and a second copy of the compile step would drift from the ReDoS guard above.

    Args:
        recording (dict): A recording; a missing or non-dict ``lag_patterns`` yields no patterns.

    Returns:
        list[re.Pattern]: The compiled patterns (ReDoS-prone, typo'd and non-string ones skipped).
    """
    compiled = []
    # A truthy non-dict lag_patterns (e.g. a list) has no .values() and must degrade to "no
    # patterns", not crash --validate.
    lag_patterns = recording.get("lag_patterns")
    lag_patterns = lag_patterns if isinstance(lag_patterns, dict) else {}
    for pattern_str in list(lag_patterns.values())[:_MAX_LAG_PATTERNS]:
        # Skip a ReDoS-prone pattern (a length cap can't bound its backtracking — see
        # _is_redos_prone) before compiling, and skip a typo'd regex (re.error) or a non-string
        # value (TypeError) rather than crash — mirroring resolve_port_relationships' hardening.
        if is_redos_prone(pattern_str):
            continue
        try:
            compiled.append(re.compile(pattern_str))
        except (re.error, TypeError):
            continue
    return compiled


def port_names(port):
    """
    Return the non-empty names a port is known by (``ifName`` + ``ifDescr``).

    Every name-based detector reads BOTH fields: on an ifDescr-mode device the structured name
    (a ``.N`` sub-unit, a LAG name) lives in ifDescr while ifName carries an arbitrary label, so an
    ifName-only scan misses the shape entirely.
    """
    return [n for n in (port.get("ifName"), port.get("ifDescr")) if isinstance(n, str) and n]


def name_matches_lag_pattern(name, compiled_lag_patterns):
    """Return whether *name* matches any compiled LAG pattern (length-bounded — see _MAX_LAG_NAME_LEN)."""
    if not isinstance(name, str) or len(name) > _MAX_LAG_NAME_LEN:
        return False
    return any(pat.search(name) for pat in compiled_lag_patterns)


def port_is_lag(port, compiled_lag_patterns):
    """
    Return whether *port* is a LAG aggregate, mirroring ``resolve_port_relationships._is_lag_aggregate``.

    An ``ieee8023adLag`` ifType OR a name matching a configured per-OS LAG pattern. Reading ifType
    alone would classify a pattern-based LAG (e.g. Cisco "Po1", carried as propVirtual) as not-a-LAG.
    """
    if port.get("ifType") == "ieee8023adLag":
        return True
    return any(name_matches_lag_pattern(name, compiled_lag_patterns) for name in port_names(port))
