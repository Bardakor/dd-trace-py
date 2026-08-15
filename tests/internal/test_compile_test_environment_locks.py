from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "compile-test-environment-locks"


@pytest.fixture(scope="module")
def compiler_mod():
    loader = SourceFileLoader("compile_test_environment_locks", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compile_lock_uses_pinned_uv_and_removes_temporary_input(compiler_mod, monkeypatch, tmp_path):
    executable = tmp_path / "tool" / "python"
    executable.parent.mkdir()
    executable.touch()
    executable.with_name("uv").touch()
    lock = tmp_path / "locks" / "abc1234.txt"
    lock.parent.mkdir()
    environment = SimpleNamespace(lock_path=lock, requirements=("pytest", "attrs<24"), python="3.12")
    run = mock.Mock()

    monkeypatch.setattr(compiler_mod, "ROOT", tmp_path)
    monkeypatch.setattr(compiler_mod.sys, "executable", str(executable))
    monkeypatch.setattr(compiler_mod, "_python_executable", lambda hint: "/python/3.12")
    monkeypatch.setattr(compiler_mod.subprocess, "run", run)

    assert compiler_mod.compile_lock(environment, "2026-08-13T00:00:00Z", force=True)

    command = run.call_args.args[0]
    assert command[:3] == [str(executable.with_name("uv")), "pip", "compile"]
    assert command[command.index("--python") + 1] == "/python/3.12"
    assert command[command.index("--exclude-newer") + 1] == "2026-08-13T00:00:00Z"
    assert not lock.with_suffix(".in").exists()


def test_compile_lock_reuses_an_existing_lock(compiler_mod, tmp_path):
    lock = tmp_path / "abc1234.txt"
    lock.touch()
    environment = SimpleNamespace(lock_path=lock, requirements=("pytest",), python="3.12")

    assert not compiler_mod.compile_lock(environment, None, force=False)
