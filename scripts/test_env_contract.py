#!/usr/bin/env scripts/uv-run-script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "riot==0.22.0",
#     "ruamel.yaml==0.18.6",
# ]
# ///
"""Snapshot and validate the resolved test-environment contract during the Riot migration."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "tests" / "environments" / "riot-contract.json"
DEFAULT_CORE = ROOT / "tests" / "environments" / "core.json"
DEFAULT_INVENTORY = ROOT / "tests" / "environments" / "inventory.json"
RIOTFILE = ROOT / "riotfile.py"
SCHEMA_VERSION = 1


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _intern(table: dict[str, Any], value: Any) -> str:
    key = hashlib.sha256(_canonical_bytes(value)).hexdigest()[:16]
    previous = table.setdefault(key, value)
    if previous != value:
        raise ValueError(f"Definition digest collision for {key}")
    return key


def build_contract(instances: Iterable[Any], suites: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a compact, deterministic contract from resolved environment instances."""
    resolved = list(instances)
    commands: dict[str, Any] = {}
    dependencies: dict[str, Any] = {}
    environments: dict[str, Any] = {}
    records = []

    for position, instance in enumerate(resolved):
        records.append(
            {
                "command": _intern(commands, instance.command),
                "dependencies": _intern(dependencies, instance.full_pkg_str),
                "environment": _intern(environments, dict(sorted(instance.env.items()))),
                "identity": instance.ident or "",
                "long_hash": instance.long_hash,
                "name": instance.name or "",
                "position": position,
                "python": instance.py._hint,
                "short_hash": instance.short_hash,
            }
        )

    suite_selections = {}
    for suite_name, config in sorted(suites.items()):
        suite_selections[suite_name] = _selected_hashes(resolved, config.get("pattern", suite_name))

    named_selections = {
        name: _selected_hashes(resolved, rf"^{re.escape(name)}$")
        for name in sorted({instance.name for instance in resolved if instance.name})
    }
    contract = {
        "schema_version": SCHEMA_VERSION,
        "definitions": {
            "commands": dict(sorted(commands.items())),
            "dependencies": dict(sorted(dependencies.items())),
            "environments": dict(sorted(environments.items())),
        },
        "instances": records,
        "named_selections": named_selections,
        "suite_selections": suite_selections,
    }
    contract["contract_digest"] = hashlib.sha256(_canonical_bytes(contract)).hexdigest()
    return contract


def _selected_hashes(instances: list[Any], pattern: str) -> list[str]:
    compiled = re.compile(pattern)
    selected = []
    seen = set()
    for instance in instances:
        if instance.matches_pattern(compiled) and instance.short_hash not in seen:
            selected.append(instance.short_hash)
            seen.add(instance.short_hash)
    return selected


def current_contract() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from suitespec import get_suites

    import riotfile

    return build_contract(riotfile.venv.instances(), get_suites())


def source_audit(path: Path = RIOTFILE) -> dict[str, Any]:
    """Describe named and unnamed Venv nesting in the source file."""
    tree = ast.parse(path.read_text())
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    declarations = []
    for node in ast.walk(tree):
        if not _is_venv_call(node):
            continue
        fields = {keyword.arg for keyword in node.keywords if keyword.arg}
        name = _constant_keyword(node, "name")
        children = next((keyword.value for keyword in node.keywords if keyword.arg == "venvs"), None)
        child_count = len(children.elts) if isinstance(children, (ast.List, ast.Tuple)) else None
        depth, nearest_named = _ancestry(node, parents)
        declarations.append(
            {
                "child_count": child_count,
                "depth": depth,
                "fields": sorted(fields),
                "line": node.lineno,
                "name": name,
                "nearest_named_ancestor": nearest_named,
            }
        )

    depths = Counter(declaration["depth"] for declaration in declarations)
    unnamed = [declaration for declaration in declarations if declaration["name"] is None]
    return {
        "declarations": len(declarations),
        "deepest_unnamed_depth": max(declaration["depth"] for declaration in unnamed),
        "depth_counts": {str(depth): count for depth, count in sorted(depths.items())},
        "max_depth": max(depths),
        "named_declarations": len(declarations) - len(unnamed),
        "unnamed_candidates": unnamed,
        "unnamed_declarations": len(unnamed),
        "unnamed_with_children": sum("venvs" in declaration["fields"] for declaration in unnamed),
    }


def runtime_audit(root: Any) -> dict[str, Any]:
    """Describe the effective Venv tree after helpers have expanded it."""
    nodes = []

    def visit(node: Any, depth: int, nearest_named: str | None) -> None:
        name = node.name or None
        children = list(node.venvs)
        nodes.append(
            {
                "child_count": len(children),
                "depth": depth,
                "name": name,
                "nearest_named_ancestor": nearest_named,
            }
        )
        for child in children:
            visit(child, depth + 1, name or nearest_named)

    visit(root, 0, None)
    depths = Counter(node["depth"] for node in nodes)
    unnamed = [node for node in nodes if node["name"] is None]
    anonymous_containers = [node for node in unnamed if node["depth"] and node["child_count"]]
    return {
        "anonymous_containers": anonymous_containers,
        "anonymous_container_count": len(anonymous_containers),
        "deepest_unnamed_depth": max(node["depth"] for node in unnamed),
        "depth_counts": {str(depth): count for depth, count in sorted(depths.items())},
        "max_depth": max(depths),
        "named_nodes": len(nodes) - len(unnamed),
        "nodes": len(nodes),
        "unnamed_nodes": len(unnamed),
    }


def current_runtime_audit() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    import riotfile

    return runtime_audit(riotfile.venv)


def _is_venv_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Venv"


def _constant_keyword(node: ast.Call, name: str) -> str | None:
    value = next((keyword.value for keyword in node.keywords if keyword.arg == name), None)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _ancestry(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> tuple[int, str | None]:
    depth = 0
    nearest_named = None
    parent = parents.get(node)
    while parent is not None:
        if _is_venv_call(parent):
            depth += 1
            if nearest_named is None:
                nearest_named = _constant_keyword(parent, "name")
        parent = parents.get(parent)
    return depth, nearest_named


def compare_contract(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return compact diagnostics for contract differences."""
    if expected == actual:
        return []

    differences = []
    expected_instances = expected.get("instances", [])
    actual_instances = actual.get("instances", [])
    if len(expected_instances) != len(actual_instances):
        differences.append(f"instance count: expected {len(expected_instances)}, got {len(actual_instances)}")
    for position, (before, after) in enumerate(zip(expected_instances, actual_instances)):
        if before != after:
            changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
            differences.append(f"instance {position}: changed {', '.join(changed)}")
            if len(differences) == 20:
                break

    for selection_type in ("named_selections", "suite_selections"):
        before = expected.get(selection_type, {})
        after = actual.get(selection_type, {})
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                differences.append(f"{selection_type}.{name}: selection changed")
                if len(differences) == 20:
                    break

    if not differences:
        differences.append("definition tables changed")
    return differences


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if match is None:
        raise ValueError(f"Cannot determine package name from {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group()).lower()


def build_inventory(contract: dict[str, Any], core: dict[str, Any]) -> dict[str, Any]:
    """Convert the resolved contract into a flat uv-owned inventory."""
    core_dependencies = core["dependencies"]
    core_names = [dependency["name"] for dependency in core_dependencies]
    core_requirements = [dependency["requirement"] for dependency in core_dependencies]
    if core_names != [_requirement_name(requirement) for requirement in core_requirements]:
        raise ValueError("Core dependency names must match their requirements")

    dependency_profiles = {}
    for profile_id, package_string in contract["definitions"]["dependencies"].items():
        requirements = shlex.split(package_string)
        if [_requirement_name(requirement) for requirement in requirements[: len(core_names)]] != core_names:
            raise ValueError(f"Dependency profile {profile_id} does not start with the core dependencies")
        overrides = {
            name: requirement
            for name, base, requirement in zip(core_names, core_requirements, requirements)
            if requirement != base
        }
        dependency_profiles[profile_id] = {
            "add": requirements[len(core_names) :],
            "override": overrides,
        }

    base_environment = core["environment"]
    environment_profiles = {}
    for profile_id, environment in contract["definitions"]["environments"].items():
        missing = sorted(set(base_environment) - set(environment))
        if missing:
            raise ValueError(f"Environment profile {profile_id} is missing core keys: {', '.join(missing)}")
        environment_profiles[profile_id] = {
            key: value for key, value in environment.items() if base_environment.get(key) != value
        }

    instances = [
        {
            "command": instance["command"],
            "dependencies": instance["dependencies"],
            "environment": instance["environment"],
            "id": instance["short_hash"],
            "identity": instance["identity"],
            "legacy_long_id": instance["long_hash"],
            "name": instance["name"],
            "python": instance["python"],
        }
        for instance in contract["instances"]
    ]
    inventory = {
        "definitions": {
            "commands": contract["definitions"]["commands"],
            "dependency_profiles": dependency_profiles,
            "environment_profiles": environment_profiles,
        },
        "instances": instances,
        "schema_version": SCHEMA_VERSION,
        "source_contract_digest": contract["contract_digest"],
    }
    inventory["inventory_digest"] = hashlib.sha256(_canonical_bytes(inventory)).hexdigest()
    return inventory


def _write_contract(path: Path, contract: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "snapshot"):
        command = subparsers.add_parser(action)
        command.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    export = subparsers.add_parser("export")
    export.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    export.add_argument("--core", type=Path, default=DEFAULT_CORE)
    export.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    check_inventory = subparsers.add_parser("check-inventory")
    check_inventory.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    check_inventory.add_argument("--core", type=Path, default=DEFAULT_CORE)
    check_inventory.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--details", action="store_true")
    audit.add_argument("--max-anonymous-containers", type=int)
    audit.add_argument("--max-unnamed-depth", type=int)
    args = parser.parse_args()

    if args.action == "audit":
        source = source_audit()
        runtime = current_runtime_audit()
        if not args.details:
            source.pop("unnamed_candidates")
            runtime.pop("anonymous_containers")
        result = {"runtime": runtime, "source": source}
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.max_unnamed_depth is not None and runtime["deepest_unnamed_depth"] > args.max_unnamed_depth:
            return 1
        if (
            args.max_anonymous_containers is not None
            and runtime["anonymous_container_count"] > args.max_anonymous_containers
        ):
            return 1
        return 0

    if args.action in ("export", "check-inventory"):
        contract = json.loads(args.contract.read_text())
        core = json.loads(args.core.read_text())
        expected = build_inventory(contract, core)
        if args.action == "export":
            _write_contract(args.inventory, expected)
            print(f"Wrote {len(expected['instances'])} uv environments to {args.inventory}")
            return 0
        actual = json.loads(args.inventory.read_text())
        if actual != expected:
            print("uv environment inventory is out of date; run scripts/test_env_contract.py export", file=sys.stderr)
            return 1
        print(f"uv environment inventory matches {args.contract}")
        return 0

    actual = current_contract()
    if args.action == "snapshot":
        _write_contract(args.contract, actual)
        print(f"Wrote {len(actual['instances'])} resolved environments to {args.contract}")
        return 0

    expected = json.loads(args.contract.read_text())
    differences = compare_contract(expected, actual)
    if differences:
        print("Resolved test-environment contract changed:", file=sys.stderr)
        for difference in differences:
            print(f"- {difference}", file=sys.stderr)
        print("Review the change, then run scripts/test_env_contract.py snapshot", file=sys.stderr)
        return 1
    print(f"Resolved test-environment contract matches {args.contract}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
