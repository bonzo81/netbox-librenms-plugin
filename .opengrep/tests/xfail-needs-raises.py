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


# ruleid: xfail-needs-raises
@pytest.mark.xfail(strict=True, raises=None, reason="recorded defect")
def test_raises_none():
    assert False


# ok: xfail-needs-raises
@pytest.mark.xfail.with_args(raises=ValueError)
def test_with_args_names_the_exception():
    raise ValueError


# ruleid: xfail-needs-raises
@pytest.mark.xfail.with_args(reason="recorded defect")
def test_with_args_without_raises():
    assert False


# ok: xfail-needs-raises
xfail = pytest.mark.xfail

# ruleid: xfail-needs-raises
pytestmark = pytest.mark.xfail

# ruleid: xfail-needs-raises
pytestmark = [pytest.mark.xfail, pytest.mark.django_db]


# ruleid: xfail-needs-raises
@pytest.mark.xfail
class TestBareClassMarker:
    def test_inside(self):
        # ok: xfail-needs-raises
        marker = pytest.mark.xfail
        assert marker


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
        # ruleid: xfail-needs-raises
        pytest.param(3, marks=pytest.mark.xfail),
    ],
)
def test_params(value):
    assert value
