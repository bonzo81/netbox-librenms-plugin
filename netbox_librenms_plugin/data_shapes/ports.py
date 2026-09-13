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

    Keyed on the VALUE, not mere key presence: ``ifVlan`` ``None``/``""``/``0``/``"0"`` are LibreNMS's
    no-/default-VLAN sentinels and do NOT count. The ifVlan value is string-valued JSON elsewhere in
    the client, so the string ``"0"`` is the same sentinel as the int ``0``. Only a real id or a
    non-empty ``vlans`` list counts. The compressor's port fingerprint imports this function, so its
    VLAN axis stays in lockstep with the signature. Otherwise, two same-shape ports (one with
    ``ifVlan: 0`` or ``None``) could collapse to a representative whose value flips the signature's
    vlans axis.

    Args:
        port (dict): A LibreNMS port row.

    Returns:
        bool: Whether the port carries a real VLAN id or a non-empty VLAN list.
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
# pathological regex); the real gates remain human review of submissions
# and the CI job timeout. A skipped pattern is simply not used for LAG-name classification (a lossless
# nudge), exactly like the non-string/typo'd patterns already skipped below.
_MAX_LAG_PATTERNS = 100
# Bound the untrusted pattern length before running the detector on it: a real LAG pattern is short
# (``^Bundle-Ether\d+$`` is ~17 chars), and capping keeps the O(n^2) worst case of the detector's own
# scan on a pathological all-``(`` string trivially small — an over-long pattern is garbage/suspect and
# never needs LAG classification, so it's treated as ReDoS-prone (skipped) too.
_MAX_LAG_PATTERN_LEN = 200
# ``{n,}`` is unbounded too, on either side of the nesting: ``(a{2,})+`` and ``(a+){2,}`` both
# backtrack the same way, so one alternative serves both positions.
_BOUNDED_RANGE_RE = re.compile(r"\{(\d*),(\d+)\}")
_FIXED_GROUP_REPEAT_RE = re.compile(r"\)\{(\d+)(?:,(\d+))?\}")
_UNBOUNDED_QUANTIFIER = r"(?:[*+]|\{\d*,\})"
# Matched at the position right after a group's ``)``: the group is repeated an unbounded number
# of times, which is what makes an ambiguous body catastrophic.
_UNBOUNDED_AFTER_RE = re.compile(_UNBOUNDED_QUANTIFIER)
_OPEN_RANGE_RE = re.compile(r"\{\d*,\}")


def _scan(pattern):
    """Yield ``(index, char, depth)`` for every character outside an escape or character class."""
    index = 0
    depth = 0
    in_class = False
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            continue
        if char == "(":
            depth += 1
            yield index, char, depth
        elif char == ")":
            yield index, char, depth
            depth -= 1
        else:
            yield index, char, depth
        index += 1


def _group_spans(pattern):
    """Return ``(open_index, close_index)`` for every balanced group, innermost last."""
    stack = []
    spans = []
    for index, char, _depth in _scan(pattern):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            spans.append((stack.pop(), index))
    return spans


def _content_is_ambiguous(pattern, start, end):
    """Whether the group body holds an unbounded quantifier or a branch at its own top level."""
    body = pattern[start + 1 : end]
    for index, char, depth in _scan(body):
        if char == "|" and depth == 0:
            # ``(a|aa)+`` backtracks without any nested quantifier: the branches overlap.
            return True
        if char in "*+":
            return True
        if char == "{" and _OPEN_RANGE_RE.match(body, index):
            return True
    return False


def _has_ambiguous_quantified_group(pattern):
    r"""
    Whether any unbounded-quantified group can partition its input more than one way.

    Depth-aware on purpose: a bounded ``\\([^()]*...\\)`` scan cannot see past a wrapper group, so
    ``^((a+))+$`` read as safe while a 26-character near-match already took seconds to fail.
    """
    for start, end in _group_spans(pattern):
        if not _UNBOUNDED_AFTER_RE.match(pattern, end + 1):
            continue
        if _content_is_ambiguous(pattern, start, end):
            return True
    return False


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
    # Variable finite ranges permit the same ambiguous partitions as unbounded repeats.
    # Reduce them for both structural checks, while preserving fixed-width ranges.
    structural_pattern = _BOUNDED_RANGE_RE.sub(
        lambda match: "+" if int(match[1] or "0") != int(match[2]) else match[0], pattern
    )
    # Fixed outer repetition can still partition an ambiguous inner group.
    structural_pattern = _FIXED_GROUP_REPEAT_RE.sub(
        lambda match: ")+" if int(match[2] or match[1]) > 1 else match[0], structural_pattern
    )
    return _has_ambiguous_quantified_group(structural_pattern)


def _compile_recording_patterns(recording, key):
    """
    Compile one of a recording's per-OS pattern maps into usable regexes.

    The single place a recording's (untrusted, community-submitted) patterns are turned into
    regexes: the signature, the port compressor, the anonymizer and the replay all read them, and
    a second copy of the compile step would drift from the ReDoS guard above.

    Args:
        recording (dict): A recording; a missing or non-dict map yields no patterns.
        key (str): The recording key holding the map.

    Returns:
        list[re.Pattern]: The compiled patterns (ReDoS-prone, typo'd and non-string ones skipped).
    """
    compiled = []
    # A truthy non-dict map (e.g. a list) has no .values() and must degrade to "no patterns",
    # not crash --validate.
    patterns = recording.get(key)
    patterns = patterns if isinstance(patterns, dict) else {}
    for pattern_str in list(patterns.values())[:_MAX_LAG_PATTERNS]:
        # Reject unsafe patterns before compilation. Skip invalid expressions, oversized
        # repetitions, and non-string values when compilation fails.
        if is_redos_prone(pattern_str):
            continue
        try:
            compiled.append(re.compile(pattern_str))
        except (re.error, TypeError, OverflowError):
            continue
    return compiled


def compile_lag_patterns(recording):
    """
    Compile a recording's ``lag_patterns`` into usable regexes, skipping the unsafe ones.

    Args:
        recording (dict): A recording; a missing or non-dict ``lag_patterns`` yields no patterns.

    Returns:
        list[re.Pattern]: The compiled LAG name patterns.
    """
    return _compile_recording_patterns(recording, "lag_patterns")


def compile_sap_patterns(recording):
    """
    Compile a recording's ``sap_patterns`` into usable regexes, skipping the unsafe ones.

    A replay that drops these resolves a service access point as a LAG member, so the captured
    rule has to travel with the recording exactly as the LAG rule does.

    Args:
        recording (dict): A recording; a missing or non-dict ``sap_patterns`` yields no patterns.

    Returns:
        list[re.Pattern]: The compiled SAP name patterns.
    """
    return _compile_recording_patterns(recording, "sap_patterns")


def port_names(port):
    """
    Return the non-empty names a port is known by (``ifName`` + ``ifDescr``).

    Every name-based detector reads BOTH fields: on an ifDescr-mode device the structured name
    (a ``.N`` sub-unit, a LAG name) lives in ifDescr while ifName carries an arbitrary label, so an
    ifName-only scan misses the shape entirely.

    Args:
        port (dict): A LibreNMS port row.

    Returns:
        list[str]: Non-empty string values from ``ifName`` and ``ifDescr``.
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

    Args:
        port (dict): A LibreNMS port row.
        compiled_lag_patterns (Iterable[re.Pattern]): Compiled per-OS LAG name patterns.

    Returns:
        bool: Whether the port's type or a known name identifies it as a LAG aggregate.
    """
    if port.get("ifType") == "ieee8023adLag":
        return True
    return any(name_matches_lag_pattern(name, compiled_lag_patterns) for name in port_names(port))
