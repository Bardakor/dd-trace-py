from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest import mock

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run-tests"


@pytest.fixture
def run_tests_mod(monkeypatch):
    suitespec_mod = ModuleType("tests.suitespec")
    suitespec_mod.get_patterns = lambda _: []
    suitespec_mod.get_suites = lambda: {}
    monkeypatch.setitem(sys.modules, "tests.suitespec", suitespec_mod)
    loader = SourceFileLoader("run_tests", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_noninteractive_selection_explains_how_to_select_an_environment(run_tests_mod, monkeypatch):
    runner = run_tests_mod.TestRunner()
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))

    with pytest.raises(SystemExit, match=r"--venv <id>"):
        runner._interactive_select(["first", "second"], "suites")


def test_direct_environment_without_suite_is_executed(run_tests_mod, monkeypatch):
    environment = run_tests_mod.EnvironmentChoice(1, "abc1234", "smoke_test", "3.12", "pytest")
    runner = mock.Mock()
    runner.get_venvs_by_hash_direct.return_value = [environment]
    runner.get_test_environments.return_value = []
    runner.run_tests.return_value = True
    monkeypatch.setattr(run_tests_mod, "TestRunner", lambda: runner)
    monkeypatch.setattr(run_tests_mod, "get_suites", lambda: {})
    monkeypatch.setattr(run_tests_mod.sys, "argv", ["run-tests", "--venv", environment.hash])

    assert run_tests_mod.main() == 0
    assert runner.run_tests.call_args.args[0] == [environment._replace(suite_name="direct")]


@pytest.mark.parametrize(
    "suite_config, expected",
    [
        ({"snapshot": True}, True),
        ({"services": ["testagent"]}, True),
        ({"services": ["redis"]}, False),
    ],
)
def test_uses_testagent_for_snapshots_and_explicit_services(run_tests_mod, suite_config, expected):
    assert run_tests_mod.TestRunner._uses_testagent(suite_config) is expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("redis", "localhost"),
        ("http://selenium-chrome:4444/wd/hub", "http://localhost:4444/wd/hub"),
        ("http://example.com:4444/wd/hub", "http://example.com:4444/wd/hub"),
    ],
)
def test_localize_service_value_for_host_network(run_tests_mod, value, expected):
    services = {"redis", "selenium-chrome"}
    assert run_tests_mod.TestRunner._localize_service_value(value, services) == expected
