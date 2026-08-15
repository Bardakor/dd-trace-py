from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def command_environment(instance: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(instance["env"])
    env.update(
        {
            "RIOT": "1",
            "RIOT_PYTHON_HINT": instance["python"],
            "RIOT_PYTHON_VERSION": instance["python_version"],
            "RIOT_VENV_HASH": instance["hash"],
            "RIOT_VENV_IDENT": instance["ident"],
            "RIOT_VENV_NAME": instance["name"],
            "RIOT_VENV_PKGS": instance["packages"],
            "RIOT_VENV_FULL_PKGS": instance["full_packages"],
            "VIRTUAL_ENV": sys.prefix,
        }
    )
    return env


def format_command(command: str, command_args: list[str]) -> str:
    quoted_args = " ".join(shlex.quote(arg) for arg in command_args)
    return command.format(cmdargs=quoted_args).strip()


def run_environment(metadata_path: Path, command_args: list[str]) -> int:
    metadata = json.loads(metadata_path.read_text())
    running_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running_python != metadata["python"]:
        raise RuntimeError(f"Environment requires Python {metadata['python']}, got {running_python}")

    for instance in metadata["instances"]:
        command = format_command(instance["command"], command_args)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=command_environment(instance),
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
