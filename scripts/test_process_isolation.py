"""Enforce the one-test-per-process contract for fork-isolated pytest runs."""

import os


COLLECTION_PROCESS_ID = os.getpid()


def pytest_runtest_setup() -> None:
    # AIDEV-NOTE: pytest-forked imports this plugin before forking each test.
    # Matching PIDs mean isolation was requested but the test stayed in its worker.
    if os.getpid() == COLLECTION_PROCESS_ID:
        raise RuntimeError("Test isolation failed: test is running in the pytest collection process")
