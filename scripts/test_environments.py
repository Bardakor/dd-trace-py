"""Load the flat uv test-environment inventory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Iterable
from typing import Mapping
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "tests" / "environments" / "core.json"
INVENTORY_PATH = ROOT / "tests" / "environments" / "inventory.json"
LOCK_ROOT = ROOT / "tests" / "environments" / "locks"
CORE_SCHEMA_VERSION = 1
INVENTORY_SCHEMA_VERSION = 2
ISOLATION_POLICY = {
    "default": "forked-process",
    "fresh_process": "one-test-per-exec-created-process",
    "snapshot_test": "forked-process-and-test-agent-session-required",
    "span_test": "forked-process-required",
}


@dataclass(frozen=True)
class Environment:
    position: int
    id: str
    legacy_long_id: str
    name: str
    python: str
    command: str
    requirements: tuple[str, ...]
    variables: Mapping[str, str]
    identity: str

    def matches(self, pattern: re.Pattern[str]) -> bool:
        return pattern.match(self.name) is not None or pattern.match(self.id) is not None

    @property
    def lock_path(self) -> Path:
        return LOCK_ROOT / f"{self.id}.txt"


@lru_cache(maxsize=1)
def _load_data() -> tuple[dict, dict]:
    core = json.loads(CORE_PATH.read_text())
    manifest = json.loads(INVENTORY_PATH.read_text())
    if core.get("schema_version") != CORE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported core environment schema {core.get('schema_version')}")
    if manifest.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise ValueError(f"Unsupported environment inventory schema {manifest.get('schema_version')}")
    if manifest.get("layout") != "named-nodes-v1":
        raise ValueError(f"Unsupported environment inventory layout {manifest.get('layout')}")
    if core.get("isolation_policy") != ISOLATION_POLICY:
        raise ValueError("Test environment isolation policy is missing or unsupported")

    definitions = {
        name: json.loads((INVENTORY_PATH.parent / relative_path).read_text())
        for name, relative_path in manifest["definitions"].items()
    }
    instances = []
    for path in sorted(INVENTORY_PATH.parent.glob(manifest["nodes"])):
        node = json.loads(path.read_text())
        name = node["name"]
        if path.stem != name.replace(":", "__"):
            raise ValueError(f"Environment node {path} declares the name {name!r}")
        instances.extend({**instance, "name": name} for instance in node["instances"])
    instances.sort(key=lambda instance: instance["position"])
    inventory = {"definitions": definitions, "instances": instances}
    return core, inventory


def _requirements(core: dict, profile: dict) -> tuple[str, ...]:
    dependencies = core["dependencies"]
    core_names = [dependency["name"] for dependency in dependencies]
    overrides = profile["override"]
    unknown = sorted(set(overrides) - set(core_names))
    if unknown:
        raise ValueError(f"Unknown core dependency overrides: {', '.join(unknown)}")
    resolved = [overrides.get(dependency["name"], dependency["requirement"]) for dependency in dependencies]
    resolved.extend(profile["add"])
    return tuple(resolved)


def _variables(core: dict, profile: dict, environ: Mapping[str, str]) -> dict[str, str]:
    resolved = {**core["environment"], **profile}
    for condition in core["conditional_environment"]:
        if all(environ.get(key) == value for key, value in condition["when"].items()):
            resolved.update(condition["set"])
    return resolved


def environments(environ: Optional[Mapping[str, str]] = None) -> list[Environment]:
    """Return resolved environments in stable execution order."""
    core, inventory = _load_data()
    environ = os.environ if environ is None else environ
    definitions = inventory["definitions"]
    commands = definitions["commands"]
    dependency_profiles = definitions["dependency_profiles"]
    environment_profiles = definitions["environment_profiles"]
    resolved = []
    for position, instance in enumerate(inventory["instances"]):
        if instance["position"] != position:
            raise ValueError(f"Environment inventory position {instance['position']} is not contiguous at {position}")
        try:
            command = commands[instance["command"]]
            requirements = _requirements(core, dependency_profiles[instance["dependencies"]])
            variables = _variables(core, environment_profiles[instance["environment"]], environ)
        except KeyError as error:
            raise ValueError(f"Environment {position} references unknown definition {error.args[0]}") from error
        resolved.append(
            Environment(
                position=position,
                id=instance["id"],
                legacy_long_id=instance["legacy_long_id"],
                name=instance["name"],
                python=instance["python"],
                command=command,
                requirements=requirements,
                variables=variables,
                identity=instance["identity"],
            )
        )
    return resolved


def default_isolation() -> str:
    """Return the default process-isolation policy for test commands."""
    core, _ = _load_data()
    return str(core["isolation_policy"]["default"])


def _normalized_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_package_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        if match := re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line):
            names.add(_normalized_package_name(match.group(1)))
    return names


def select(pattern: str, candidates: Optional[Iterable[Environment]] = None) -> list[Environment]:
    """Select one ordered instance per stable ID for a name pattern."""
    compiled = re.compile(pattern)
    selected = []
    seen = set()
    pool = candidates if candidates is not None else environments()
    for environment in pool:
        if environment.matches(compiled) and environment.id not in seen:
            selected.append(environment)
            seen.add(environment.id)
    return selected


def find(identifier: str, candidates: Optional[Iterable[Environment]] = None) -> list[Environment]:
    """Find every command/environment variant represented by an ID or exact name."""
    pool = candidates if candidates is not None else environments()
    return [
        environment
        for environment in pool
        if environment.id == identifier
        or environment.legacy_long_id.startswith(identifier)
        or environment.name == identifier
    ]


def validate() -> dict[str, int]:
    """Validate references, stable IDs, and checked-in locks."""
    core, _ = _load_data()
    resolved = environments(environ={})
    supported_python = set(core["supported_python"])
    by_id: dict[str, list[Environment]] = {}
    for environment in resolved:
        if environment.python not in supported_python:
            raise ValueError(f"Environment {environment.id} uses unsupported Python {environment.python}")
        if not re.fullmatch(r"[0-9a-f]{7}", environment.id):
            raise ValueError(f"Environment ID {environment.id!r} is not a seven-character digest")
        if not environment.name:
            raise ValueError(f"Environment {environment.id} has no resolved name")
        if not environment.legacy_long_id.startswith(environment.id):
            raise ValueError(f"Environment {environment.id} has an inconsistent legacy ID")
        by_id.setdefault(environment.id, []).append(environment)

    missing_locks = sorted(
        environment_id for environment_id in by_id if not (LOCK_ROOT / f"{environment_id}.txt").is_file()
    )
    if missing_locks:
        raise ValueError(f"Missing environment locks: {', '.join(missing_locks)}")
    core_dependencies = {_normalized_package_name(dependency["name"]) for dependency in core["dependencies"]}
    incomplete_locks = {
        environment_id: sorted(core_dependencies - _locked_package_names(LOCK_ROOT / f"{environment_id}.txt"))
        for environment_id in by_id
    }
    incomplete_locks = {environment_id: missing for environment_id, missing in incomplete_locks.items() if missing}
    if incomplete_locks:
        environment_id, missing = next(iter(sorted(incomplete_locks.items())))
        raise ValueError(f"Environment lock {environment_id} is missing core dependencies: {', '.join(missing)}")
    for environment_id, variants in by_id.items():
        if len({(variant.name, variant.python, variant.requirements) for variant in variants}) != 1:
            raise ValueError(f"Environment variants for {environment_id} disagree on name, Python, or dependencies")
    stale_locks = sorted(path.stem for path in LOCK_ROOT.glob("*.txt") if path.stem not in by_id)
    if stale_locks:
        raise ValueError(f"Stale environment locks: {', '.join(stale_locks)}")
    return {
        "instances": len(resolved),
        "named_nodes": len({environment.name for environment in resolved}),
        "unique_environments": len(by_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("check")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("pattern")
    args = parser.parse_args()

    if args.action == "list":
        for environment in select(args.pattern):
            print(environment.id)
        return 0

    print(json.dumps(validate(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
