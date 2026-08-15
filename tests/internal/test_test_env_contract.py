import ast
import importlib.util
from pathlib import Path
import re
import types

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "test_env_contract.py"


@pytest.fixture(scope="module")
def contract_mod():
    spec = importlib.util.spec_from_file_location("test_env_contract", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeInstance:
    def __init__(self, name, short_hash, command="pytest tests/unit"):
        self.command = command
        self.env = {"SHARED": "1"}
        self.full_pkg_str = "'pytest'"
        self.ident = "pytest"
        self.long_hash = f"{short_hash}abcdefghi"
        self.name = name
        self.py = types.SimpleNamespace(_hint="3.12")
        self.short_hash = short_hash

    def matches_pattern(self, pattern: re.Pattern):
        return pattern.search(self.name) is not None


def test_build_contract_preserves_order_and_named_selections(contract_mod):
    contract = contract_mod.build_contract(
        [FakeInstance("tracer", "abc1234"), FakeInstance("tracer", "def5678")],
        {"tracer": {"pattern": "^tracer$"}},
    )

    assert [instance["short_hash"] for instance in contract["instances"]] == ["abc1234", "def5678"]
    assert contract["named_selections"] == {"tracer": ["abc1234", "def5678"]}
    assert contract["suite_selections"] == {"tracer": ["abc1234", "def5678"]}
    assert len(contract["definitions"]["commands"]) == 1
    assert len(contract["definitions"]["dependencies"]) == 1
    assert len(contract["definitions"]["environments"]) == 1


def test_source_audit_identifies_unnamed_nesting_without_flattening_named_nodes(contract_mod, tmp_path):
    source = tmp_path / "riotfile.py"
    source.write_text(
        """\
venv = Venv(
    venvs=[
        Venv(
            name="suite",
            venvs=[
                Venv(
                    pys=["3.12"],
                    venvs=[Venv(name="named-child")],
                ),
            ],
        ),
    ],
)
"""
    )

    audit = contract_mod.source_audit(source)

    assert audit["max_depth"] == 3
    assert audit["deepest_unnamed_depth"] == 2
    assert audit["named_declarations"] == 2
    assert audit["unnamed_declarations"] == 2
    assert audit["unnamed_candidates"][1]["nearest_named_ancestor"] == "suite"


def test_runtime_audit_separates_anonymous_containers_from_leaf_variants(contract_mod):
    leaf = types.SimpleNamespace(name=None, venvs=[])
    anonymous_container = types.SimpleNamespace(name=None, venvs=[leaf])
    named = types.SimpleNamespace(name="suite", venvs=[anonymous_container])
    root = types.SimpleNamespace(name=None, venvs=[named])

    audit = contract_mod.runtime_audit(root)

    assert audit["nodes"] == 4
    assert audit["named_nodes"] == 1
    assert audit["deepest_unnamed_depth"] == 3
    assert audit["anonymous_container_count"] == 1
    assert audit["anonymous_containers"] == [
        {
            "child_count": 1,
            "depth": 2,
            "name": None,
            "nearest_named_ancestor": "suite",
        }
    ]


def test_compare_contract_reports_changed_instance_fields(contract_mod):
    expected = contract_mod.build_contract([FakeInstance("tracer", "abc1234")], {})
    actual = contract_mod.build_contract([FakeInstance("tracer", "def5678")], {})

    assert contract_mod.compare_contract(expected, actual) == [
        "instance 0: changed long_hash, short_hash",
        "named_selections.tracer: selection changed",
    ]


def test_is_venv_call_ignores_other_calls(contract_mod):
    call = ast.parse("Other()").body[0].value

    assert not contract_mod._is_venv_call(call)


def test_build_inventory_factors_core_dependencies_and_environment(contract_mod):
    contract = contract_mod.build_contract([FakeInstance("tracer", "abc1234")], {})
    dependency_id = contract["instances"][0]["dependencies"]
    environment_id = contract["instances"][0]["environment"]
    core = {
        "dependencies": [
            {"name": "pytest", "requirement": "pytest"},
        ],
        "environment": {"SHARED": "1"},
    }

    inventory = contract_mod.build_inventory(contract, core)

    assert inventory["definitions"]["dependency_profiles"][dependency_id] == {
        "add": [],
        "override": {},
    }
    assert inventory["definitions"]["environment_profiles"][environment_id] == {}
    assert inventory["instances"][0]["id"] == "abc1234"
