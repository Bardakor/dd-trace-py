import json
import re

import pytest

from scripts import test_environments


def test_inventory_is_valid_and_all_locks_exist():
    result = test_environments.validate()

    assert result["instances"] >= result["unique_environments"]
    assert result["named_nodes"] > 0


def test_inventory_preserves_named_selections():
    contract = json.loads((test_environments.INVENTORY_PATH.parent / "riot-contract.json").read_text())
    resolved = test_environments.environments(environ={})

    named = {
        name: [environment.id for environment in test_environments.select(rf"^{re.escape(name)}$", resolved)]
        for name in contract["named_selections"]
    }
    assert named == contract["named_selections"]


def test_nightly_environment_is_applied_only_when_requested():
    normal = test_environments.environments(environ={})[0]
    nightly = test_environments.environments(environ={"NIGHTLY_BUILD": "true"})[0]

    assert "DD_CIVISIBILITY_CODE_COVERAGE_REPORT_UPLOAD_ENABLED" not in normal.variables
    assert nightly.variables["DD_CIVISIBILITY_CODE_COVERAGE_REPORT_UPLOAD_ENABLED"] == "1"


def test_unknown_core_override_is_rejected():
    with pytest.raises(ValueError, match="unknown-package"):
        test_environments._requirements(
            {"dependencies": [{"name": "pytest", "requirement": "pytest"}]},
            {"add": [], "override": {"unknown-package": "unknown-package<2"}},
        )
