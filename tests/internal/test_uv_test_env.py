import importlib.util
import json
import pathlib
import subprocess
import sys
from unittest import mock

import pytest


_SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "uv_test_env.py"


@pytest.fixture(scope="module")
def uv_test_env_mod():
    spec = importlib.util.spec_from_file_location("uv_test_env", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance(command="pytest {cmdargs} tests/tracer"):
    return {
        "command": command,
        "env": {"DD_TRACE_ENABLED": "false"},
        "full_packages": "pytest==8.4.2",
        "hash": "abc1234",
        "ident": "tracer",
        "name": "tracer",
        "packages": "pytest",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
    }


def test_prepare_dependencies_uses_uv_and_reuses_matching_prefix(uv_test_env_mod, monkeypatch, tmp_path):
    lock = tmp_path / "requirements.txt"
    lock.write_text("pytest==8.4.2\n")
    monkeypatch.setattr(uv_test_env_mod, "ROOT", tmp_path)
    monkeypatch.setattr(uv_test_env_mod, "PREFIX_ROOT", tmp_path / "prefixes")
    run = mock.Mock()
    monkeypatch.setattr(uv_test_env_mod.subprocess, "run", run)
    metadata = {
        "hash": "abc1234",
        "python": "3.12",
        "requirements": "requirements.txt",
    }

    prefix = uv_test_env_mod.prepare_dependencies(metadata)
    assert prefix == tmp_path / "prefixes" / "py3.12" / "abc1234"
    run.assert_called_once_with(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--prefix",
            str(prefix),
            "--no-deps",
            "--requirement",
            str(lock),
        ],
        cwd=tmp_path,
        check=True,
    )

    uv_test_env_mod.prepare_dependencies(metadata)
    run.assert_called_once()


def test_command_environment_preserves_riot_contract(uv_test_env_mod, monkeypatch, tmp_path):
    prefix = tmp_path / "prefix"
    monkeypatch.setattr(uv_test_env_mod.os, "environ", {"PATH": "/bin", "PYTHONPATH": "existing"})

    env = uv_test_env_mod.command_environment(_instance(), prefix)

    assert env["DD_TRACE_ENABLED"] == "false"
    assert env["RIOT"] == "1"
    assert env["RIOT_VENV_HASH"] == "abc1234"
    assert env["VIRTUAL_ENV"] == sys.prefix
    assert env["PYTHONPATH"] == f"{uv_test_env_mod._site_packages(prefix)}{uv_test_env_mod.os.pathsep}existing"
    assert env["PATH"].startswith(f"{prefix / 'bin'}{uv_test_env_mod.os.pathsep}")


def test_format_command_shell_quotes_forwarded_arguments(uv_test_env_mod):
    command = uv_test_env_mod.format_command("pytest {cmdargs} tests/tracer", ["-k", "one or two", "it's"])
    assert command == "pytest -k 'one or two' 'it'\"'\"'s' tests/tracer"


def test_run_environment_runs_all_matching_instances(uv_test_env_mod, monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "hash": "abc1234",
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "instances": [_instance(), _instance("python tests/smoke_test.py {cmdargs}")],
            }
        )
    )
    prefix = tmp_path / "prefix"
    monkeypatch.setattr(uv_test_env_mod, "ROOT", tmp_path)
    monkeypatch.setattr(uv_test_env_mod, "prepare_dependencies", lambda metadata: prefix)
    monkeypatch.setattr(
        uv_test_env_mod, "command_environment", lambda instance, path: {"INSTANCE": instance["command"]}
    )
    run = mock.Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(uv_test_env_mod.subprocess, "run", run)

    result = uv_test_env_mod.run_environment(metadata_path, ["--ddtrace"])

    assert result == 0
    assert [call.args[0] for call in run.call_args_list] == [
        "pytest --ddtrace tests/tracer",
        "python tests/smoke_test.py --ddtrace",
    ]
    assert all(call.kwargs["cwd"] == tmp_path for call in run.call_args_list)
    assert all(call.kwargs["executable"] == "/bin/bash" for call in run.call_args_list)


def test_run_environment_stops_after_failure(uv_test_env_mod, monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "hash": "abc1234",
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                "instances": [_instance(), _instance("unreachable {cmdargs}")],
            }
        )
    )
    monkeypatch.setattr(uv_test_env_mod, "prepare_dependencies", lambda metadata: tmp_path / "prefix")
    monkeypatch.setattr(uv_test_env_mod, "command_environment", lambda instance, path: {})
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 17))
    monkeypatch.setattr(uv_test_env_mod.subprocess, "run", run)

    assert uv_test_env_mod.run_environment(metadata_path, []) == 17
    run.assert_called_once()
