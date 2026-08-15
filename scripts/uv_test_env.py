from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PREFIX_ROOT = ROOT / ".cache" / "uv-test-prefixes"
BOOTSTRAP_PATH = ROOT / "scripts" / "uv_compat"
INSTALLER_SCHEMA = b"uv-test-env-v1"


def _dependency_prefix(metadata: dict[str, Any]) -> Path:
    return PREFIX_ROOT / f"py{metadata['python']}" / metadata["hash"]


def _site_packages(prefix: Path) -> Path:
    paths = {"base": str(prefix), "platbase": str(prefix)}
    return Path(sysconfig.get_path("purelib", vars=paths))


def _lock_digest(requirements: Path, uv_version: str, base_digest: str) -> str:
    digest = hashlib.sha256(INSTALLER_SCHEMA)
    digest.update(uv_version.encode())
    digest.update(base_digest.encode())
    digest.update(requirements.read_bytes())
    return digest.hexdigest()


def prepare_dependencies(metadata: dict[str, Any]) -> Path:
    requirements = ROOT / metadata["requirements"]
    if not requirements.is_file():
        raise RuntimeError(f"Missing requirements lock: {requirements}")

    prefix = _dependency_prefix(metadata)
    marker = prefix / ".dd-uv-lock"
    expected_digest = _lock_digest(requirements, metadata["uv_version"], metadata["base_digest"])
    if marker.is_file() and marker.read_text().strip() == expected_digest:
        return prefix

    if prefix.exists():
        shutil.rmtree(prefix)
    prefix.mkdir(parents=True)
    subprocess.run(
        [
            metadata["uv"],
            "pip",
            "install",
            "--python",
            sys.executable,
            "--prefix",
            str(prefix),
            "--no-deps",
            "--requirement",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
    )
    marker.write_text(expected_digest)
    return prefix


def command_environment(instance: dict[str, Any], prefix: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(instance["env"])
    env.update(
        {
            "RIOT": "1",
            "RIOT_PYTHON_HINT": instance["python"],
            "RIOT_PYTHON_VERSION": platform.python_version(),
            "RIOT_VENV_HASH": instance["hash"],
            "RIOT_VENV_IDENT": instance["ident"],
            "RIOT_VENV_NAME": instance["name"],
            "RIOT_VENV_PKGS": instance["packages"],
            "RIOT_VENV_FULL_PKGS": instance["full_packages"],
            "VIRTUAL_ENV": sys.prefix,
            "DD_TEST_SITE_PACKAGES": str(_site_packages(prefix)),
        }
    )
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        os.pathsep.join((str(BOOTSTRAP_PATH), current_pythonpath)) if current_pythonpath else str(BOOTSTRAP_PATH)
    )
    env["PATH"] = os.pathsep.join((str(prefix / "bin"), str(Path(sys.executable).parent), env.get("PATH", "")))
    return env


def format_command(command: str, command_args: list[str]) -> str:
    quoted_args = " ".join(shlex.quote(arg) for arg in command_args)
    return command.format(cmdargs=quoted_args).strip()


def run_environment(metadata_path: Path, command_args: list[str]) -> int:
    metadata = json.loads(metadata_path.read_text())
    running_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running_python != metadata["python"]:
        raise RuntimeError(f"Environment requires Python {metadata['python']}, got {running_python}")

    prefix = prepare_dependencies(metadata)
    for instance in metadata["instances"]:
        command = format_command(instance["command"], command_args)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=command_environment(instance, prefix),
            executable="/bin/bash",
            shell=True,
        )
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata", type=Path)
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command_args = args.command_args
    if command_args and command_args[0] == "--":
        command_args = command_args[1:]
    return run_environment(args.metadata, command_args)


if __name__ == "__main__":
    raise SystemExit(main())
