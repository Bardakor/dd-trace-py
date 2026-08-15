#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [package]"
    exit 1
fi

if [[ $# -eq 1 ]]; then
    pkgs="$1"
else
    pkgs=$(python scripts/find-outdated-test-environments.py output)
fi

echo "Outdated packages: $pkgs"

if [[ -z "$pkgs" ]]; then
    echo "No outdated packages found."
    exit 0
fi

for pkg in $pkgs; do
    echo "Checking if new latest version exists for $pkg"
    export VENV_NAME="$pkg"

    if ! ENVIRONMENT_IDS_OUTPUT=$(python scripts/test_environments.py list "^${VENV_NAME}$" 2>&1); then
        echo "Error listing test environments for $pkg: $ENVIRONMENT_IDS_OUTPUT"
        continue
    fi
    mapfile -t ENVIRONMENT_IDS <<< "$ENVIRONMENT_IDS_OUTPUT"
    ENVIRONMENT_IDS=("${ENVIRONMENT_IDS[@]//[[:space:]]/}")
    ENVIRONMENT_IDS=(${ENVIRONMENT_IDS[@]})

    echo "Found ${#ENVIRONMENT_IDS[@]} environment IDs: ${ENVIRONMENT_IDS[*]}"

    if [[ ${#ENVIRONMENT_IDS[@]} -eq 0 ]]; then
        echo "No test environments found for pattern: $VENV_NAME"
        continue
    fi

    if [[ -n "${GITHUB_ENV:-}" ]]; then
        echo "VENV_NAME=$VENV_NAME" >> "$GITHUB_ENV"
    fi

    for environment_id in "${ENVIRONMENT_IDS[@]}"; do
        echo "Removing test environment lock: tests/environments/locks/${environment_id}.txt"
        rm -f "tests/environments/locks/${environment_id}.txt"
    done

    scripts/compile-test-environment-locks "^${VENV_NAME}$"

    # Supply-chain hardening (TEST-CD, APMLP-1362): verify that none of the
    # newly resolved pins (including transitive dependencies that uv
    # picks up) are younger than the cooldown. This is defense-in-depth on
    # top of the cutoff passed directly to uv by the lock compiler.
    REGENERATED_LOCKFILES=()
    for environment_id in "${ENVIRONMENT_IDS[@]}"; do
        if [[ -f "tests/environments/locks/${environment_id}.txt" ]]; then
            REGENERATED_LOCKFILES+=("tests/environments/locks/${environment_id}.txt")
        fi
    done

    if [[ ${#REGENERATED_LOCKFILES[@]} -gt 0 ]]; then
        echo "Validating cooldown on ${#REGENERATED_LOCKFILES[@]} regenerated lockfile(s)"
        python scripts/check_lockfile_cooldown.py "${REGENERATED_LOCKFILES[@]}"
    fi

    # Only process one package per run
    break
done
