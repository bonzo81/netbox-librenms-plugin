import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier, Thread

from netbox_librenms_plugin.tests.conftest import run_in_threads


def test_executor_with_a_barrier():
    barrier = Barrier(2)
    # ruleid: barrier-needs-run-in-threads
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(barrier.wait, timeout=5) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)


def test_thread_with_a_qualified_barrier():
    barrier = threading.Barrier(2)
    # ruleid: barrier-needs-run-in-threads
    worker = threading.Thread(target=barrier.wait)
    worker.start()
    barrier.wait(timeout=5)
    worker.join()


def test_thread_with_a_barrier_in_a_wrapper():
    wrappers = [object() for _ in range(2)]
    barrier = Barrier(len(wrappers))
    # ruleid: barrier-needs-run-in-threads
    worker = Thread(target=barrier.wait)
    worker.start()
    worker.join()


def test_barrier_through_the_helper():
    barrier = Barrier(2)
    # ok: barrier-needs-run-in-threads
    run_in_threads(partial(barrier.wait, timeout=5), partial(barrier.wait, timeout=5), barriers=(barrier,), timeout=10)


def test_executor_without_a_barrier():
    # ok: barrier-needs-run-in-threads
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)


def test_executor_before_the_barrier():
    # ok: barrier-needs-run-in-threads
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(print).result(timeout=10)
    barrier = Barrier(1)
    barrier.wait(timeout=5)
