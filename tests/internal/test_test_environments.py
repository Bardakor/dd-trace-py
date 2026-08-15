import json
import re

import pytest

from scripts import test_environments


def test_inventory_is_valid_and_all_locks_exist():
    result = test_environments.validate()

    assert result["instances"] >= result["unique_environments"]
    assert result["named_nodes"] > 0


def test_isolation_policy_keeps_span_and_snapshot_tests_in_fresh_processes():
    core, _ = test_environments._load_data()

    assert core["isolation_policy"] == test_environments.ISOLATION_POLICY


def test_inventory_preserves_named_selections():
    resolved = test_environments.environments(environ={})

    for path in sorted((test_environments.INVENTORY_PATH.parent / "nodes").glob("*.json")):
        node = json.loads(path.read_text())
        expected = list(dict.fromkeys(instance["id"] for instance in node["instances"]))
        selected = test_environments.select(rf"^{re.escape(node['name'])}$", resolved)
        assert [environment.id for environment in selected] == expected


def test_inventory_files_stay_below_ci_added_file_limit():
    inventory_files = list(test_environments.INVENTORY_PATH.parent.rglob("*.json"))
    oversized = [path for path in inventory_files if path.stat().st_size > 100_000]
    windows_unsafe = [path for path in inventory_files if set(path.name) & set('<>:"/\\|?*')]

    assert oversized == []
    assert windows_unsafe == []


def test_nightly_environment_is_applied_only_when_requested():
    normal = test_environments.environments(environ={})[0]
    nightly = test_environments.environments(environ={"NIGHTLY_BUILD": "true"})[0]

    assert "DD_CIVISIBILITY_CODE_COVERAGE_REPORT_UPLOAD_ENABLED" not in normal.variables
    assert nightly.variables["DD_CIVISIBILITY_CODE_COVERAGE_REPORT_UPLOAD_ENABLED"] == "1"


def test_wait_environment_uses_the_requested_agent_url():
    wait = test_environments.find("wait")[0]

    assert "DD_TRACE_AGENT_URL" not in wait.variables


def test_unknown_core_override_is_rejected():
    with pytest.raises(ValueError, match="unknown-package"):
        test_environments._requirements(
            {"dependencies": [{"name": "pytest", "requirement": "pytest"}]},
            {"add": [], "override": {"unknown-package": "unknown-package<2"}},
        )
