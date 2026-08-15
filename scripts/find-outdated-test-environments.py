import argparse
from collections import defaultdict
import datetime as dt
from functools import lru_cache
from http.client import HTTPSConnection
from io import StringIO
import json
import pathlib
import sys
from typing import Any
from typing import Optional

from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.version import Version
from pip import _internal


# Add the project root and integration registry helpers to the import path.
sys.path.append(str(pathlib.Path(__file__).parent.parent.resolve()))
sys.path.append(str(pathlib.Path(__file__).parent.resolve() / "integration_registry"))

from mappings import DEPENDENCY_TO_INTEGRATION_MAPPING  # noqa: I001,E402
from mappings import INTEGRATION_TO_DEPENDENCY_MAPPING  # noqa: I001,E402

from scripts import test_environments  # noqa: I001,E402

CONTRIB_ROOT = pathlib.Path("ddtrace/contrib/internal")

# Supply-chain hardening (TEST-CD, APMLP-1362): when deciding whether the
# packages we test against are "outdated" with respect to PyPI, we ignore
# any release that was published less than COOLDOWN_DAYS ago. This prevents
# the daily lock update workflow from pulling in a freshly
# published (and potentially compromised) version before the broader
# community / security tooling has had a chance to flag it.
#
# 2 days = 48h matches the cross-language cooldown standard documented in
# the supply-chain hardening epic (APMLP-1343).
COOLDOWN_DAYS = 2

supported_versions = []
pinned_packages = set()


class Capturing(list):
    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = self._stringio = StringIO()
        sys.stderr = StringIO()
        return self

    def __exit__(self, *args):
        self.extend(self._stringio.getvalue().splitlines())
        del self._stringio  # free up some memory
        sys.stdout = self._stdout
        sys.stderr = self._stderr


def parse_args():
    """
    usage: python scripts/freshvenvs.py <output>
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["output"], help="mode: output")
    return parser.parse_args()


def _get_contrib_modules() -> set[str]:
    """Get all integrations by checking modules that have contribs implemented for them"""
    all_integration_names = set()
    for item in CONTRIB_ROOT.iterdir():
        if not item.is_dir():
            continue

        patch_filepath = item / "patch.py"

        if patch_filepath.is_file():
            all_integration_names.add(item.name)

    return all_integration_names


def _get_environments_including_any(contrib_modules: set[str]) -> set[str]:
    """Return environment IDs whose locks include at least one supplied module."""
    envs = set()
    for item in test_environments.LOCK_ROOT.iterdir():
        if item.suffix == ".txt":
            lockfile_content = item.read_text()
            for contrib_module in contrib_modules:
                if contrib_module in lockfile_content or (
                    _integration_to_dependency_mapping_contains(contrib_module, lockfile_content)
                ):
                    envs.add(item.stem)
                    break
    return envs


def _integration_to_dependency_mapping_contains(integration: str, lockfile_content: str) -> bool:
    if integration not in INTEGRATION_TO_DEPENDENCY_MAPPING:
        return False

    for dependency in INTEGRATION_TO_DEPENDENCY_MAPPING[integration]:
        if dependency in lockfile_content:
            return True

    return False


def _get_updatable_packages_implementing(contrib_modules: set[str]) -> set[str]:
    """Return all integrations that can be updated"""
    configured_packages = set()
    packages_setting_latest = set()
    for environment in test_environments.environments(environ={}):
        package = environment.name.split(":", 1)[0]
        if package not in contrib_modules:
            continue
        configured_packages.add(package)
        if _environment_sets_latest_for_package(environment, package):
            packages_setting_latest.add(package)

    pinned_packages.update(configured_packages - packages_setting_latest)

    packages = {m for m in contrib_modules if "." not in m and m not in pinned_packages}
    return packages


def _parse_pypi_upload_time(upload_timestamp: str) -> Optional[dt.datetime]:
    """Best-effort parse of a PyPI ``upload_time_iso_8601`` timestamp.

    PyPI usually returns ``YYYY-MM-DDTHH:MM:SS.fffZ`` but some old releases
    omit the microseconds component, so we try both formats and return
    ``None`` if neither matches.
    """
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(upload_timestamp, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


@lru_cache(maxsize=256)
def _get_version_extremes(contrib_module: str) -> tuple[Optional[str], Optional[str]]:
    """Return the (earliest, latest) supported versions of a given package.

    The returned ``latest`` is the most recent PyPI release that is at least
    ``COOLDOWN_DAYS`` old. Versions younger than that are ignored so the
    automated lockfile-refresh workflow does not pull in a release before
    the supply-chain "cooldown" period has elapsed (see TEST-CD).
    """
    with Capturing() as output:
        _internal.main(["index", "versions", contrib_module])
    if not output:
        return (None, None)

    version_list = [a for a in output if "available versions" in a.lower()]
    if not version_list:
        return (None, None)

    output_parts = version_list[0].split()
    versions = [p.strip(",") for p in output_parts[2:]]
    if not versions:
        return (None, None)

    earliest_within_window = versions[-1]

    conn = None
    try:
        conn = HTTPSConnection("pypi.org", 443, timeout=30)
        conn.request("GET", f"/pypi/{contrib_module}/json")
        response = conn.getresponse()

        if response.status != 200:
            raise ValueError(f"Failed to connect to PyPI: HTTP {response.status}")

        version_infos = json.loads(response.read().decode("utf-8"))["releases"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        raise ValueError(f"Failed to fetch version info for {contrib_module}: {e}")
    finally:
        if conn is not None:
            conn.close()

    current_time = dt.datetime.now(dt.timezone.utc)
    cooldown = dt.timedelta(days=COOLDOWN_DAYS)
    two_years = dt.timedelta(days=365 * 2)

    # The first version in ``versions`` that is at least COOLDOWN_DAYS old.
    # Falls back to the absolute latest if we can't determine the age (for
    # example, PyPI's JSON API didn't return release files for any of the
    # candidates), since that preserves the prior behaviour rather than
    # silently disabling the outdated-package detection.
    latest_after_cooldown: Optional[str] = None

    for version in versions:
        version_info = version_infos.get(version, [])
        if not version_info:
            continue

        upload_timestamp = version_info[0].get("upload_time_iso_8601")
        if not upload_timestamp:
            continue

        upload_time = _parse_pypi_upload_time(upload_timestamp)
        if upload_time is None:
            continue

        version_age = current_time - upload_time

        if latest_after_cooldown is None and version_age >= cooldown:
            latest_after_cooldown = version

        if version_age > two_years:
            earliest_within_window = version
            break

    if latest_after_cooldown is None:
        latest_after_cooldown = versions[0]
    return earliest_within_window, latest_after_cooldown


def _get_environment_id_to_name() -> dict[str, str]:
    """Return the stable environment ID to node-name mapping."""
    return {environment.id: environment.name.lower() for environment in test_environments.environments(environ={})}


def _get_package_versions_from(
    env: str, contrib_modules: set[str], environment_id_to_name: dict[str, str]
) -> list[tuple[str, str]]:
    """Return the list of package versions that are tested, related to the modules"""
    lockfile_content = (test_environments.LOCK_ROOT / f"{env}.txt").read_text().splitlines()
    lock_packages = []
    integration = None
    dependencies: set[str] = set()
    if environment_id_to_name.get(env):
        environment_name = environment_id_to_name[env].split(":")[0]

        def get_integration_and_dependencies(venv_name: str) -> tuple[Optional[str], set[str]]:
            if venv_name in contrib_modules:
                integration = venv_name
                dependencies = INTEGRATION_TO_DEPENDENCY_MAPPING.get(venv_name) or {integration}
                return integration, dependencies
            elif venv_name in DEPENDENCY_TO_INTEGRATION_MAPPING:
                integration = DEPENDENCY_TO_INTEGRATION_MAPPING[venv_name]
                dependencies = INTEGRATION_TO_DEPENDENCY_MAPPING[integration]
                return integration, dependencies
            else:
                return None, set()

        integration, dependencies = get_integration_and_dependencies(environment_name)

    for line in lockfile_content:
        package, _, versions = line.partition("==")
        package = package.split("[")[0]  # strip optional package installs like flask[async]
        if package in dependencies or package == integration:
            lock_packages.append((package, versions))
    return lock_packages


def _is_module_autoinstrumented(module: str) -> bool:
    import importlib

    _monkey = importlib.import_module("ddtrace._monkey")
    PATCH_MODULES = getattr(_monkey, "PATCH_MODULES")

    return module in PATCH_MODULES and PATCH_MODULES[module]


def _versions_fully_cover_bounds(bounds: tuple[str, str], versions: list[Version]) -> bool:
    """Return whether the tested versions cover the upper bound range of supported versions"""
    if not versions:
        return False
    _, upper_bound = bounds
    return versions[0] >= Version(upper_bound)


def _environment_sets_latest_for_package(environment: Any, suite_name: str) -> bool:
    """Return whether an environment leaves an integration dependency unpinned."""
    packages = INTEGRATION_TO_DEPENDENCY_MAPPING.get(suite_name, [suite_name])
    for raw_requirement in environment.requirements:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement:
            continue
        if requirement.name.lower() in packages and not requirement.specifier and requirement.url is None:
            return True
    return False


def _get_all_used_versions(envs, contrib_modules, environment_id_to_name) -> dict:
    """
    Return dependency versions used by the selected test environments.
    """
    all_used_versions = defaultdict(set)
    for env in envs:
        versions_used = _get_package_versions_from(env, contrib_modules, environment_id_to_name)
        for package, version in versions_used:
            all_used_versions[package].add(version)
    return all_used_versions


def _get_version_bounds(contrib_modules: set[str]) -> dict:
    """
    Return dict(module: (earliest, latest)) of the module from PyPI
    """
    bounds = dict()
    for contrib_module in contrib_modules:
        earliest, latest = _get_version_extremes(contrib_module)
        bounds[contrib_module] = (earliest, latest)
    return bounds


def output_outdated_packages(all_updatable_contribs, envs, bounds, environment_id_to_name):
    """
    Output a list of package names that can be updated.
    """
    outdated_packages = []

    for contrib_module in all_updatable_contribs:
        earliest, latest = _get_version_extremes(contrib_module)
        bounds[contrib_module] = (earliest, latest)

    all_used_versions = defaultdict(set)
    for env in envs:
        versions_used = _get_package_versions_from(env, all_updatable_contribs, environment_id_to_name)
        for pkg, version in versions_used:
            all_used_versions[pkg].add(version)

    for contrib_module in all_updatable_contribs:
        ordered = sorted([Version(v) for v in all_used_versions[contrib_module]], reverse=True)
        if not ordered:
            continue
        if contrib_module not in bounds or bounds[contrib_module] == (None, None):
            continue
        if not _versions_fully_cover_bounds(bounds[contrib_module], ordered):
            outdated_packages.append(contrib_module)

    print(" ".join(outdated_packages))


def main():
    parse_args()
    contribs = _get_contrib_modules()
    all_updatable_contribs = _get_updatable_packages_implementing(contribs)  # MODULE names
    environment_id_to_name = _get_environment_id_to_name()
    envs = _get_environments_including_any(contribs)

    bounds = _get_version_bounds(contribs)
    output_outdated_packages(all_updatable_contribs, envs, bounds, environment_id_to_name)


if __name__ == "__main__":
    main()
