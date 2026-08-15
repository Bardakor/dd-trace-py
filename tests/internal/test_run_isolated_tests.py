from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts import run_isolated_tests


def test_strip_xdist_options_preserves_other_pytest_arguments(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "4")

    args, workers = run_isolated_tests.strip_xdist_options(
        ["-v", "-n", "auto", "--dist=worksteal", "-k", "one or two", "tests/internal"]
    )

    assert args == ["-v", "-k", "one or two", "tests/internal"]
    assert workers == 4


def test_remove_collection_targets_keeps_options():
    args = ["-q", "tests/internal", "--ignore=tests/internal/test_slow.py"]

    assert run_isolated_tests.remove_collection_targets(args, ["tests/internal"]) == [
        "-q",
        "--ignore=tests/internal/test_slow.py",
    ]


def test_remove_collection_targets_rejects_unmatched_target():
    with pytest.raises(ValueError, match="missing.py"):
        run_isolated_tests.remove_collection_targets(["-q", "tests/internal"], ["missing.py"])


def test_merge_junit_combines_counts(tmp_path):
    first = tmp_path / "first.xml"
    second = tmp_path / "second.xml"
    output = tmp_path / "combined.xml"
    first.write_text(
        '<testsuites><testsuite name="first" tests="1" failures="0" errors="0" skipped="0" time="1.5"/></testsuites>'
    )
    second.write_text(
        '<testsuites><testsuite name="second" tests="2" failures="1" errors="0" skipped="1" time="2.0"/></testsuites>'
    )

    run_isolated_tests.merge_junit([first, Path("missing.xml"), second], output)

    root = ET.parse(output).getroot()
    assert root.attrib == {"tests": "3", "failures": "1", "errors": "0", "skipped": "1", "time": "3.5"}
    assert [suite.get("name") for suite in root] == ["first", "second"]
