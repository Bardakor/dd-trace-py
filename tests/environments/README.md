# Test environments

uv installs locked dependencies over one editable ddtrace build per Python version. The checked-in files here are the source of truth; there is no second Python environment tree to expand.

## Layout

- `core.json` holds supported Python requests, dependencies and variables shared by every environment, and the isolation contract.
- `nodes/` preserves every named selector used by local development and CI. Anonymous nesting has been flattened into resolved instances. Names containing `:` use `__` only in the filename so Windows can check out the tree; the `name` field remains unchanged.
- `definitions/` de-duplicates commands, dependency profiles, and environment-variable profiles. Instances refer to these values by content digest.
- `locks/<id>.txt` pins the complete dependency set for one stable seven-character environment ID.
- `inventory.json` is the small manifest that connects these files.

The order and IDs are stable interfaces. CI uses names to select groups and IDs to split and cache work.

## Changing an environment

1. Update shared dependencies or variables in `core.json`. Use a node-specific profile when the change does not apply to every test.
2. Update the relevant named node and its referenced definition. Reuse an existing definition when its content matches.
3. Run `python scripts/test_environments.py check` to validate references, ordering, IDs, and locks.
4. Recompile the affected locks in the test container:

   ```bash
   scripts/ddtest scripts/compile-test-environment-locks --force '^node-name$'
   ```

5. Run the affected suite through `scripts/run-tests` and commit the JSON and lock changes together.

Run `scripts/compile-test-environment-locks` without a selector to create missing locks, remove stale locks, and refresh integration version data. Set `DD_TEST_LOCK_EXCLUDE_NEWER` when a reproducible cutoff is required.

## Adding an environment

Add instances under a named node, even if the name is used only for local selection. Keep CI-facing names unchanged. A new instance needs a command profile, dependency profile, environment profile, Python request, stable ID, long ID, readable identity, and its next contiguous global position. Prefer extending an existing node over introducing another layer of grouping.

The validator rejects missing or stale locks, profile mismatches, unsupported Python requests, non-contiguous positions, invalid IDs, and files above the CI size limit.

## Isolation

The uv launcher starts every resolved environment command in a new subprocess. It never combines commands into one Python interpreter. This preserves the isolation boundary that existed between resolved environments.

That boundary is not yet one process per pytest test. Snapshot fixtures already create and clear a distinct test-agent session, but the pytest worker can still retain tracer and context state. Span-producing and snapshot tests require a clean process per test before they can be safely packed or reordered. The migration and remaining enforcement work are tracked in `docs/contributing-ci-performance.rst`.
