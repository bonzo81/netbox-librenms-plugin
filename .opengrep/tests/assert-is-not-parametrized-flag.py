import pytest


@pytest.mark.parametrize("superuser", [False, True])
def test_flag_compared_by_identity(superuser):
    context = {"incomplete": None}
    # ruleid: assert-is-not-parametrized-flag
    assert context["incomplete"] is not superuser


@pytest.mark.parametrize(("mapping", "shown"), [(None, True), ({}, False)])
def test_flag_with_a_message(mapping, shown):
    value = mapping
    # ruleid: assert-is-not-parametrized-flag
    assert value is not shown, "the notice follows the mapping"


class TestInAClass:
    @pytest.mark.parametrize("primary", [False, True])
    def test_method(self, primary):
        changed = None
        # ruleid: assert-is-not-parametrized-flag
        assert changed is not primary


@pytest.mark.parametrize("superuser", [False, True])
def test_negated_flag(superuser):
    context = {"incomplete": not superuser}
    # ok: assert-is-not-parametrized-flag
    assert context["incomplete"] is (not superuser)


@pytest.mark.parametrize("superuser", [False, True])
def test_identity_against_a_local(superuser):
    first = object()
    second = object()
    # ok: assert-is-not-parametrized-flag
    assert second is not first


def test_identity_without_parametrize(flushed_events):
    value = object()
    # ok: assert-is-not-parametrized-flag
    assert value is not flushed_events


@pytest.mark.parametrize("superuser", [False, True])
def test_none_check(superuser):
    value = superuser
    # ok: assert-is-not-parametrized-flag
    assert value is not None


@pytest.mark.parametrize("superuser, mode", [(False, "a"), (True, "b")])
def test_flag_in_a_comma_string(mode, superuser):
    value = None
    # ruleid: assert-is-not-parametrized-flag
    assert value is not superuser


@pytest.mark.parametrize("mode", ["a", "b"])
def test_identity_against_a_fixture(mode, flushed_events):
    value = object()
    # ok: assert-is-not-parametrized-flag
    assert value is not flushed_events


@pytest.mark.parametrize("modes", [False, True])
def test_identity_against_a_similar_name(modes, mode):
    value = object()
    # ok: assert-is-not-parametrized-flag
    assert value is not mode


@pytest.mark.parametrize("original", [object(), None])
def test_identity_against_a_non_bool_parameter(original):
    value = object()
    # ok: assert-is-not-parametrized-flag
    assert value is not original


@pytest.mark.parametrize(["mode", "primary"], [("a", False), ("b", True)])
def test_flag_in_a_name_list(mode, primary):
    changed = None
    # ruleid: assert-is-not-parametrized-flag
    assert changed is not primary
