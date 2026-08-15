import pathlib
from typing import Any

from mappings import EXCLUDED_FROM_TESTING


def test_integrations_have_test_environments(
    integration_dir_names: set[str],
    test_environment_names: set[str],
    project_root: pathlib.Path,
    internal_contrib_dir: pathlib.Path,
    untested_integrations: set[str],
):
    """
    Verify that every integration directory in ddtrace/contrib/internal has a
    corresponding named test environment.
    """
    missing_environments = integration_dir_names - test_environment_names - untested_integrations

    contrib_internal_rel_path = internal_contrib_dir.relative_to(project_root)

    assert not missing_environments, (
        f"\nThe following integration directories in '{contrib_internal_rel_path}' "
        f"are MISSING a corresponding named test environment:\n"
        f"  - " + "\n  - ".join(sorted(missing_environments)) + "\n"
        "\nPlease add an environment node with a matching name."
    )


def test_contrib_tests_have_valid_environment_name(test_environment_instances: Any, integration_dir_names: set[str]):
    """
    Verify that every environment containing contrib tests names an integration.
    """

    failed_environments = []
    for environment in test_environment_instances:
        if environment.command and "tests/contrib" in environment.command:
            integration_name = environment.name.split(":")[0].removesuffix("-pytest")
            if integration_name not in integration_dir_names and integration_name not in EXCLUDED_FROM_TESTING:
                failed_environments.append(environment)

    if failed_environments:
        failure_messages = [f"\n{'*' * 100}"]
        for environment in failed_environments:
            failure_messages.append(
                f"Environment '{environment.name}' has a test command containing 'tests/contrib': "
                f"{environment.command}, but is not an integration under 'ddtrace/contrib/internal'.\n"
            )
        failure_messages.append("*" * 100)
    assert failed_environments == [], "\n".join(failure_messages)
