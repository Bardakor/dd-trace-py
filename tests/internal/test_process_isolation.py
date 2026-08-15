import os

import pytest

from scripts import test_process_isolation


def test_default_isolation_runs_test_outside_collection_process():
    assert os.getpid() != test_process_isolation.COLLECTION_PROCESS_ID


def test_isolation_guard_rejects_collection_process(monkeypatch):
    monkeypatch.setattr(test_process_isolation.os, "getpid", lambda: test_process_isolation.COLLECTION_PROCESS_ID)

    with pytest.raises(RuntimeError, match="test is running in the pytest collection process"):
        test_process_isolation.pytest_runtest_setup()
