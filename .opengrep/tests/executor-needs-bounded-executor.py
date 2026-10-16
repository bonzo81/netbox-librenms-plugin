import concurrent.futures
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import ThreadPoolExecutor as Pool

import pytest

from netbox_librenms_plugin.tests.conftest import bounded_executor


def test_bare_executor():
    # ruleid: executor-needs-bounded-executor
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_aliased_executor():
    # ruleid: executor-needs-bounded-executor
    with Pool(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_qualified_executor():
    # ruleid: executor-needs-bounded-executor
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_module_alias_executor():
    # ruleid: executor-needs-bounded-executor
    with futures.ThreadPoolExecutor(max_workers=1):
        pass


def test_executor_after_another_manager():
    # ruleid: executor-needs-bounded-executor
    with pytest.raises(TimeoutError), ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_bounded_executor():
    # ok: executor-needs-bounded-executor
    with bounded_executor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_executor_with_explicit_shutdown():
    # ok: executor-needs-bounded-executor
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        executor.submit(print).result(timeout=10)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
