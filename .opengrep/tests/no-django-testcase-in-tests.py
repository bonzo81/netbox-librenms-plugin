# ruleid: no-django-testcase-in-tests
from django.test import TestCase
# ruleid: no-django-testcase-in-tests
from django.test import TestCase as DjangoCase
# ruleid: no-django-testcase-in-tests
from django.test.testcases import TestCase as BaseCase
import django.test as testing


# ruleid: no-django-testcase-in-tests
class TestImport(TestCase):
    pass


# ruleid: no-django-testcase-in-tests
class TestAlias(DjangoCase):
    pass


# ruleid: no-django-testcase-in-tests
class TestQualified(testing.TestCase):
    pass


# ruleid: no-django-testcase-in-tests
class TestInternal(BaseCase):
    pass


# ok: no-django-testcase-in-tests
from django.test import Client, RequestFactory


# ok: no-django-testcase-in-tests
class TestPlain:
    def test_equal(self):
        assert 1 == 1
