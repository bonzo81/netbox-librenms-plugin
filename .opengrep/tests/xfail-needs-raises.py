import pytest
from pytest import mark


# ruleid: xfail-needs-raises
@pytest.mark.xfail(strict=True, reason="recorded defect")
def test_strict_without_raises():
    assert False


# ruleid: xfail-needs-raises
@pytest.mark.xfail
def test_bare_marker():
    assert False


# ruleid: xfail-needs-raises
@mark.xfail(reason="recorded defect")
def test_imported_mark():
    assert False


# ok: xfail-needs-raises
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="recorded defect")
def test_names_the_exception():
    assert False


# ok: xfail-needs-raises
@pytest.mark.skip(reason="not an xfail")
def test_skip():
    assert False


@pytest.mark.parametrize(
    "value",
    [
        # ruleid: xfail-needs-raises
        pytest.param(1, marks=pytest.mark.xfail(reason="recorded defect")),
        # ok: xfail-needs-raises
        pytest.param(2, marks=pytest.mark.xfail(raises=ValueError, reason="recorded defect")),
    ],
)
def test_params(value):
    assert value
