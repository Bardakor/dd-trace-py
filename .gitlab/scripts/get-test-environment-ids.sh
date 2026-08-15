#!/usr/bin/env bash
set -e -u -o pipefail

SUITE_NAME="${1:-}"
python scripts/test_environments.py list "${SUITE_NAME}" | sort | ./.gitlab/ci-split-input.sh
