from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
import subprocess
from unittest import mock

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build-uv-base"


@pytest.fixture(scope="module")
def build_uv_base_mod():
    loader = SourceFileLoader("build_uv_base", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_creates_and_reuses_a_fingerprinted_base(build_uv_base_mod, monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    base_root = root / ".test-env" / "uv-bases"
    executable = tmp_path / "tool" / "python"
    executable.parent.mkdir()
    executable.touch()
    executable.with_name("uv").touch()
    monkeypatch.setattr(build_uv_base_mod, "ROOT", root)
    monkeypatch.setattr(build_uv_base_mod, "BASE_ROOT", base_root)
    monkeypatch.setattr(build_uv_base_mod.sys, "executable", str(executable))
    monkeypatch.setattr(build_uv_base_mod, "build_digest", lambda python, uv_version: "input-digest")
    monkeypatch.setattr(build_uv_base_mod, "version", lambda package: "0.12.5")

    destination = base_root / "py3.12"

    def run(command, **kwargs):
        if command[1] == "venv":
            (destination / "bin").mkdir(parents=True)
            (destination / "bin" / "python").touch()
        return subprocess.CompletedProcess(command, 0)

    mocked_run = mock.Mock(side_effect=run)
    monkeypatch.setattr(build_uv_base_mod.subprocess, "run", mocked_run)

    assert build_uv_base_mod.build("3.12") == destination
    assert mocked_run.call_count == 2
    assert (destination / ".dd-uv-base").read_text() == "input-digest\n"

    mocked_run.reset_mock()
    assert build_uv_base_mod.build("3.12") == destination
    mocked_run.assert_not_called()
