from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
import contextlib
from dataclasses import dataclass
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_TAG_SUFFIX = f"[py{sys.version_info.major}.{sys.version_info.minor}]"
XDIST_VALUE_OPTIONS = {"-n", "--numprocesses", "--dist", "--maxschedchunk", "--max-worker-restart"}
INFERRED_SERVICE_ENV = "_DD_PYTEST_XDIST_INFERRED_SERVICE"


class CollectionPlugin:
    def __init__(self) -> None:
        self.nodeids: list[str] = []
        self.targets: list[str] = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.nodeids = [remove_python_tag(item.nodeid) for item in session.items]
        self.targets = [str(target) for target in session.config.args]


@dataclass
class TestResult:
    index: int
    nodeid: str
    returncode: int
    stdout: bytes
    stderr: bytes
    junit: Path


def remove_python_tag(nodeid: str) -> str:
    if nodeid.endswith(PYTHON_TAG_SUFFIX):
        return nodeid[: -len(PYTHON_TAG_SUFFIX)]
    return nodeid


def strip_xdist_options(args: list[str]) -> tuple[list[str], int]:
    cleaned: list[str] = []
    workers: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        option, separator, value = arg.partition("=")
        if option in XDIST_VALUE_OPTIONS:
            if not separator:
                index += 1
                if index >= len(args):
                    raise ValueError(f"Missing value for {arg}")
                value = args[index]
            if option in {"-n", "--numprocesses"}:
                workers = value
        elif arg.startswith("-n") and arg != "-n":
            workers = arg[2:]
        else:
            cleaned.append(arg)
        index += 1

    override = os.getenv("DD_TEST_ISOLATION_WORKERS")
    if override:
        workers = override
    if workers in {"auto", "logical"}:
        workers = os.getenv("PYTEST_XDIST_AUTO_NUM_WORKERS") or str(os.cpu_count() or 1)
    worker_count = int(workers) if workers is not None else 1
    if worker_count < 1:
        raise ValueError("Isolation worker count must be positive")
    return cleaned, worker_count


def remove_collection_targets(args: list[str], targets: list[str]) -> list[str]:
    remaining = Counter(targets)
    cleaned = []
    for arg in args:
        if remaining[arg]:
            remaining[arg] -= 1
        else:
            cleaned.append(arg)
    missing = list(remaining.elements())
    if missing:
        raise ValueError(f"Could not remove pytest collection targets from command: {missing}")
    return cleaned


def collect_tests(args: list[str]) -> tuple[list[str], list[str], int]:
    pytest_args, _ = strip_xdist_options(args)
    plugin = CollectionPlugin()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        returncode = pytest.main([*pytest_args, "--collect-only", "-qq", "-o", "addopts="], plugins=[plugin])
    if returncode != pytest.ExitCode.OK:
        print(stdout.getvalue(), end="")
        print(stderr.getvalue(), end="", file=sys.stderr)
        return [], [], int(returncode)
    return plugin.nodeids, remove_collection_targets(pytest_args, plugin.targets), 0


def run_test(
    index: int,
    nodeid: str,
    pytest_args: list[str],
    executable: str,
    result_dir: Path,
    coverage_dir: Path,
    inferred_service: str | None,
) -> TestResult:
    junit = result_dir / f"junit-{index}.xml"
    env = os.environ.copy()
    env["DD_TEST_ISOLATED_CHILD"] = "1"
    env["COVERAGE_FILE"] = str(coverage_dir / f".coverage.{index}")
    if inferred_service:
        env[INFERRED_SERVICE_ENV] = inferred_service
    command = [executable, *pytest_args, nodeid, f"--junitxml={junit}"]
    completed = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return TestResult(index, nodeid, completed.returncode, completed.stdout, completed.stderr, junit)


def write_result(result: TestResult, total: int) -> None:
    status = "PASS" if result.returncode == 0 else "FAIL"
    print(f"[{result.index + 1}/{total}] {status} {result.nodeid}")
    if result.returncode:
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
    sys.stdout.flush()
    sys.stderr.flush()


def merge_junit(paths: list[Path], output: Path) -> None:
    root = ET.Element("testsuites")
    totals: dict[str, float] = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    for path in paths:
        if not path.is_file():
            continue
        source = ET.parse(path).getroot()
        suites = [source] if source.tag == "testsuite" else list(source.findall("testsuite"))
        for suite in suites:
            root.append(suite)
            for field in ("tests", "failures", "errors", "skipped"):
                totals[field] += int(suite.get(field, "0"))
            totals["time"] += float(suite.get("time", "0"))
    for field, value in totals.items():
        root.set(field, str(value if field == "time" else int(value)))
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def combine_coverage(coverage_dir: Path) -> None:
    if not any(coverage_dir.glob(".coverage.*")):
        return
    subprocess.run([sys.executable, "-m", "coverage", "combine", "--append", str(coverage_dir)], cwd=ROOT, check=False)


def run_isolated(pytest_args: list[str]) -> int:
    nodeids, child_args, collection_status = collect_tests(pytest_args)
    if collection_status:
        return collection_status
    if not nodeids:
        return int(pytest.ExitCode.NO_TESTS_COLLECTED)

    executable = shutil.which("pytest")
    if executable is None:
        raise RuntimeError("pytest is not available in the active test environment")

    from ddtrace.internal.settings._inferred_base_service import detect_service

    inference_args = strip_xdist_options(pytest_args)[0]
    inferred_service = detect_service([executable, *inference_args])

    workers = strip_xdist_options(pytest_args)[1]
    fail_fast = "-x" in child_args or "--exitfirst" in child_args
    environment_id = os.getenv("DD_TEST_ENV_ID", "unknown")
    output = ROOT / "test-results" / f"junit-isolated-{environment_id}.xml"
    results: list[TestResult] = []

    with tempfile.TemporaryDirectory(prefix="ddtrace-isolated-tests-") as temporary:
        temporary_path = Path(temporary)
        result_dir = temporary_path / "junit"
        coverage_dir = temporary_path / "coverage"
        result_dir.mkdir()
        coverage_dir.mkdir()

        next_index = 0
        failed = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            running: dict[Future[TestResult], int] = {}

            def submit() -> None:
                nonlocal next_index
                future = executor.submit(
                    run_test,
                    next_index,
                    nodeids[next_index],
                    child_args,
                    executable,
                    result_dir,
                    coverage_dir,
                    inferred_service,
                )
                running[future] = next_index
                next_index += 1

            while next_index < len(nodeids) and len(running) < workers:
                submit()

            while running:
                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(completed, key=lambda item: running[item]):
                    running.pop(future)
                    result = future.result()
                    results.append(result)
                    write_result(result, len(nodeids))
                    failed = failed or result.returncode != 0
                while next_index < len(nodeids) and len(running) < workers and not (failed and fail_fast):
                    submit()

        merge_junit([result.junit for result in sorted(results, key=lambda item: item.index)], output)
        combine_coverage(coverage_dir)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = args.pytest_args
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    return run_isolated(pytest_args)


if __name__ == "__main__":
    raise SystemExit(main())
