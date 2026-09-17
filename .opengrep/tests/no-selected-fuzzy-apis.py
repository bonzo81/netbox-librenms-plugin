# ok: no-selected-fuzzy-apis
import difflib
# ok: no-selected-fuzzy-apis
import difflib as diff
# ok: no-selected-fuzzy-apis
from difflib import get_close_matches
# ok: no-selected-fuzzy-apis
from difflib import SequenceMatcher as Matcher
# ok: no-selected-fuzzy-apis
from difflib import unified_diff
# ok: no-selected-fuzzy-apis
from fuzzywuzzy import fuzz
# ok: no-selected-fuzzy-apis
import fuzzywuzzy.fuzz as fuzzy
# ok: no-selected-fuzzy-apis
from rapidfuzz import fuzz as rapid
# ok: no-selected-fuzzy-apis
from rapidfuzz.fuzz import ratio


def select(label, choices, left, right):
    # ruleid: no-selected-fuzzy-apis
    difflib.get_close_matches(label, choices)
    # ruleid: no-selected-fuzzy-apis
    get_close_matches(label, choices)
    # ruleid: no-selected-fuzzy-apis
    diff.SequenceMatcher(None, left, right).ratio()
    matcher = Matcher(None, left, right)
    # ruleid: no-selected-fuzzy-apis
    matcher.ratio()
    # ruleid: no-selected-fuzzy-apis
    Matcher(None, left, right).quick_ratio()
    # ruleid: no-selected-fuzzy-apis
    Matcher(None, left, right).real_quick_ratio()
    # ruleid: no-selected-fuzzy-apis
    fuzz.ratio(left, right)
    # ruleid: no-selected-fuzzy-apis
    fuzzy.partial_ratio(left, right)
    # ruleid: no-selected-fuzzy-apis
    rapid.token_sort_ratio(left, right)
    # ruleid: no-selected-fuzzy-apis
    ratio(left, right)
    # ok: no-selected-fuzzy-apis
    return left == right


def render_diff(left, right):
    # ok: no-selected-fuzzy-apis
    unified_diff(left, right)
    # ok: no-selected-fuzzy-apis
    difflib.unified_diff(left, right)
    # ok: no-selected-fuzzy-apis
    matcher = difflib.SequenceMatcher(None, left, right)
    # ok: no-selected-fuzzy-apis
    return matcher.get_opcodes()


# ok: no-selected-fuzzy-apis
from collections import defaultdict



def reused_matcher(left, right):
    matcher = Matcher(None, left, right)
    matcher.get_opcodes()
    # A method call invalidates the propagated constructor binding.
    # ok: no-selected-fuzzy-apis
    return matcher.ratio()
