.. _ci-test-performance:

CI test performance and isolation
=================================

This is the living register for test CI bottlenecks. It ranks work by correctness risk, pipeline wall time,
and runner cost. Update the measurement log and the ranking when CI changes materially.

Last updated: 2026-08-15.

Goals
-----

* Reduce time to a trustworthy test result, not only aggregate runner time.
* Reuse the ddtrace build and immutable dependency data wherever correctness allows.
* Keep every span-producing test in its own Python process.
* Keep agent-backed snapshot tests isolated while retaining safe job-level parallelism.
* Measure queue, setup, dependency, test, and cleanup time separately.

Isolation is a correctness boundary
-----------------------------------

The following rules are not performance trade-offs:

* Two tests that create spans must never run in the same Python process. A reused process can retain tracer or
  context state and produce mixed traces.
* Every snapshot test must connect to the test agent, use an isolated test session, flush its writer, and complete
  the test-agent snapshot handshake before the process exits.
* A unique test-agent token does not make Python process reuse safe. It isolates agent storage, not tracer globals,
  context variables, imported modules, background workers, or inherited file descriptors.
* Base builds, dependency downloads, containers, and a proven-safe test-agent service may be reused. Mutable Python
  runtime state may not be reused between span-producing tests.

The target runner should make the isolation class explicit:

.. list-table::
   :header-rows: 1
   :widths: 18 30 52

   * - Class
     - Execution
     - Examples and constraints
   * - Reusable
     - Multiple tests may share a worker process.
     - Pure tests or tests with a proven reset contract. These can use normal pytest workers and work stealing.
   * - Fresh process
     - One test per clean, exec-created Python process.
     - Import-order, environment, fork, global tracer configuration, and any span-producing test.
   * - Agent isolated
     - Fresh process plus a unique test-agent session.
     - Tests that send spans to the agent but do not compare snapshots.
   * - Snapshot isolated
     - Fresh process plus test-agent start, flush, compare, and finalize.
     - Snapshot tests. Parallel lanes need independent session identifiers and a contamination test.

``xdist`` workers are reusable processes. Increasing ``-n`` is safe for the reusable class, but it does not provide
one-process-per-test isolation. A forked child also needs care because it can inherit active context and open
connections. The safe default for isolated tests is a clean controller that starts each test with ``exec``.

Ranked bottleneck register
--------------------------

Importance combines correctness, expected wall-clock impact, frequency, and confidence in the evidence. A high
rank does not imply that an optimization may relax the isolation rules above.

.. list-table::
   :header-rows: 1
   :widths: 7 24 12 37 20

   * - Rank
     - Bottleneck
     - Importance
     - Current evidence
     - First action
   * - 1
     - Span and snapshot isolation is implicit
     - Critical
     - Normal pytest sessions and ``xdist`` workers can run multiple tests in one process. Snapshot tokens isolate test
       agent sessions, but the snapshot helper records occasional unmatched traces between sessions.
     - Add explicit isolation metadata and enforce one clean process per span-producing or snapshot test.
   * - 2
     - Duration-blind partitioning and ordering
     - High
     - Environment hashes are sorted and assigned round-robin. Tracer shards in one main run ranged from 126 to
       770 seconds, a 6.1x spread. With one environment per job, ordering alone cannot split a slow environment.
     - Use historical duration weights at environment and test-node level. Split slow environments and start the
       slowest safe units first.
   * - 3
     - Snapshot service cost is paid at suite granularity
     - High
     - 118 suite entries request the test agent. The whole job starts it even when only part of the selected test
       command performs snapshots.
     - Split reusable tests from agent-backed tests, then run the latter through isolated parallel lanes.
   * - 4
     - Shared build and cache setup remains on the critical path
     - High
     - Reusing one ddtrace base per Python version works, but observed base builds took 243 to 340 seconds before
       dependent jobs could run.
     - Time artifact transfer and uv materialization separately, then make immutable artifacts available per Python
       version as soon as they are ready.
   * - 5
     - Suite selectors schedule duplicate environments
     - High
     - The baseline ``tracer`` selector included all five ``tracer-uwsgi`` hashes, adding about 16 runner-minutes.
       Two integration-registry suite entries also selected the same hash. Focused CI now emits 14 exclusive tracer
       jobs instead of 19, with no uWSGI environments in the broad selector.
     - Fixed on the migration branch: selectors are exclusive and generation rejects environment overlap.
   * - 6
     - Subprocess execution has several overlapping paths
     - Medium
     - The tree has 821 ``subprocess`` marker references, 562 ``run_in_subprocess`` references, and 329 direct
       child-process calls. The legacy unittest helper starts a full unittest or pytest command per test.
     - Instrument startup, import, body, and cleanup time; converge on one isolation launcher without batching span
       tests into a process.
   * - 7
     - Whole-shard retries amplify deterministic failures
     - Medium
     - 44 suite entries allow two retries. A deterministic failure can therefore consume three full shard runs.
     - Restrict CI retries to runner and service failures; retry an isolated test only when its failure class permits.
   * - 8
     - Broad, concurrently written caches
     - Medium, unmeasured
     - Generated jobs cache all of ``.cache`` under a suite key. Parallel shards can restore and update the same
       broad cache, mixing pip, uv, dependency prefixes, and compiler data.
     - Record cache transfer bytes and time, then separate immutable consumer caches from a single producer.
   * - 9
     - Test configuration has multiple sources and slow generated copies
     - Medium
     - Suite routing, Riot environments, uv launch metadata, YAML templates, and generated jobs repeat related
       concepts. The baseline started one Riot subprocess per suite to calculate cache keys. Full local generation
       took 24.6 seconds; the observed CI configuration job took 88 to 97 seconds.
     - Cache keys are now calculated in-process, cutting local generation to 0.69 seconds. The next two CI jobs took
       86 and 28 seconds, down from 97. Next, introduce one suite model with isolation and partitioning fields.

Detailed findings and experiments
---------------------------------

1. Make isolation explicit before increasing parallelism
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The snapshot fixture derives a token from the test request. ``snapshot_context`` flushes the tracer, adds the token
to writer headers and subprocess environment, starts a test-agent session, flushes again, and asks the agent to
compare the result. This is useful agent-side isolation. It does not reset all process state.

The same helper currently treats some ``received unmatched traces`` responses as expected failures because the test
agent can occasionally mix traces between sessions. That should be a measured reliability defect, not a permanent
reason to combine tests.

Proposed experiment:

#. Add an ``isolation`` field to the suite and test metadata. ``snapshot`` implies ``snapshot-isolated`` and any test
   that creates spans implies at least ``fresh-process``.
#. Add a contamination sentinel: run two known span tests concurrently and fail if either process observes the
   other's trace or active context.
#. Collect test node IDs once, then start isolated tests in clean child processes with bounded concurrency. Reuse the
   container, base Python, uv dependency prefix, and test-agent service, not the child interpreter.
#. Give every parallel agent lane an independent session namespace. If the test agent cannot guarantee this, use one
   agent sidecar per lane or serialize the snapshot handshake while keeping non-agent work parallel.
#. Reject configurations that send snapshot tests through normal reusable ``xdist`` workers.

Success gates:

* Zero mixed-context or cross-session traces in repeated stress runs.
* Zero ignored unmatched-trace responses before removing the existing expected-failure behavior.
* No regression in snapshot content, process cleanup, or agent connectivity.

2. Balance by duration, not hash count
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``get-riot-hashes.sh`` sorts hashes and ``ci-split-input.sh`` assigns them round-robin. The generator also scales
toward 200 jobs using environment counts. Neither decision uses historical duration or fixed job startup cost.
Tracer currently uses one environment per job, so hash ordering can improve queue priority but cannot shorten the
slowest environment. Reducing its critical path requires test-node partitioning inside long environments.

Proposed experiment:

#. Store p50 and p95 duration by suite, environment hash, Python version, isolation class, and test node ID.
#. Use a rolling median or exponentially weighted average so one bad run does not dominate future scheduling.
#. Split long environments into safe test-node work units. Pack short environments only when the saved fixed setup
   outweighs the loss of parallelism. Include image, service, artifact, and cache setup cost.
#. Choose parallelism by predicted completion time and runner-minute budget instead of a fixed 200-job floor.
#. Keep a fallback deterministic order for new or missing duration data.

Primary metrics are suite completion time, maximum-to-median shard ratio, total runner minutes, and queue time. After
excluding the duplicated uWSGI environments, the unique tracer environments ranged from 492 to 770 seconds, or 1.6x.
The first partitioning target is below 1.5x without increasing runner minutes.

3. Separate snapshot service scope from dependency scope
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``snapshot: true`` extends the entire generated job with the test-agent service and wait step. The current source
inventory contains 504 snapshot marker or decorator references across 78 Python files and 902 JSON snapshot files,
but 118 suite entries request the service. These lexical counts are directional; parametrization means they are not
test case counts.

Proposed experiment:

#. During collection, emit reusable, fresh-process, agent-isolated, and snapshot-isolated manifests.
#. Run reusable tests without the test agent. Start agent-backed lanes only when their manifest is non-empty.
#. Keep one test agent per CI job initially. Measure readiness, per-session handshake, flush, comparison, and cleanup
   separately before deciding whether more sidecars are worthwhile.
#. Replace the Riot ``wait`` environment with a small runner-owned readiness probe. The current probe requires the
   Python 3.9 base and a large service-client dependency set even for jobs testing another Python version.

Do not share a single snapshot session between tests, and do not move snapshot tests into a reused worker to save
startup time.

4. Preserve useful build reuse and remove avoidable setup
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The base environment is built once per required Python version and downloaded by dependent jobs. This is the useful
part of the existing Riot design and should remain under uv. The next gains are in cache hits, artifact size, and
allowing each Python-version lane to start as soon as its own artifact and pre-checks are ready.

Proposed experiment:

#. Add timers for image pull, base artifact download, uv cache restore, dependency prefix creation, editable ddtrace
   activation, test execution, and cache upload.
#. Key the base artifact by commit, Python version, build inputs, and native build inputs.
#. Key an immutable dependency prefix by Python version, lock digest, platform, and uv version. Let test jobs pull it
   without uploading the same data concurrently.
#. Keep compiler caches separate from pip and uv caches so their policies and retention can differ.

5. Reject overlapping suite membership
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The default suite selector is its name as a regular expression. Because ``tracer`` is not anchored, it also matches
``tracer-uwsgi``. On 2026-08-15 the broad selector returned 19 hashes and the explicit uWSGI selector returned five;
all five uWSGI hashes were present in both lists. Their broad-tracer copies consumed 971 observed runner-seconds,
while the dedicated uWSGI copies consumed 952 seconds. Keeping the dedicated suite and removing the broad copies
would therefore save about 16.2 runner-minutes in that run, but not its 770-second critical path.

The immediate fix is an exclusive tracer selector. The durable fix is generated membership that does not depend on
overlapping regular expressions. Add a pre-check that reports every hash assigned to more than one suite and requires
an explicit allow-list entry for intentional overlap.

The migration branch now applies the exclusive selector, removes a second duplicate integration-registry suite
entry, and rejects any environment hash matched by multiple selected suites. Full local generation covered 203
suites with zero overlaps, producing 14 tracer jobs and five dedicated uWSGI jobs. The focused real CI run emitted
exactly 14 tracer jobs and no uWSGI jobs, confirming that the broad selector no longer pays for the duplicate work.

Track duplicate environment executions and duplicate test node IDs per pipeline. A zero-overlap declaration should
be enforced, not assumed.

6. Consolidate subprocess launchers without weakening isolation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

There are three common paths today: the pytest subprocess marker, ``SubprocessTestCase``, and direct calls to the
standard library. They do not report the same phase timings, inherit environment differently, and have different
collection overhead.

The goal is one launcher and one result protocol. It may reuse resolved dependencies and collection metadata, but a
span-producing test still gets a clean process. Optimize pure helper child processes separately; do not create a warm
span-test worker pool that runs multiple tests in one interpreter.

Measure at least:

* clean interpreter and pytest startup;
* test module import and collection;
* test body;
* trace flush and snapshot handshake;
* process cleanup, timeout, and captured output volume.

7. Narrow retries
^^^^^^^^^^^^^^^^^

Suite-level ``retry: 2`` reruns a complete GitLab job. This is appropriate for a failed runner or unavailable service,
but costly and misleading for an assertion or deterministic setup failure. Prefer CI retry conditions for runner and
service failures. If a known flaky test must be retried, rerun that isolated node in a fresh process and retain all
attempt results.

8. Split cache ownership
^^^^^^^^^^^^^^^^^^^^^^^^

Each generated job currently caches the broad ``.cache`` directory with a suite-level key. Before changing policy,
capture restore and upload duration, transferred bytes, hit rate, extracted size, and concurrent writers. A likely
target model is:

* one read-only uv download cache shared by compatible locks;
* immutable per-lock dependency prefixes produced once and consumed by test jobs;
* a separately keyed native compiler cache;
* no test-job upload when the job cannot add a reusable artifact.

9. De-duplicate test configuration
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The final model should express each fact once:

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Field
     - Meaning
   * - ``command``
     - Test command and selected paths.
   * - ``python`` and ``dependencies``
     - uv-managed Python and lock inputs.
   * - ``services``
     - Required service sidecars and readiness probes.
   * - ``isolation``
     - Reusable, fresh process, agent isolated, or snapshot isolated.
   * - ``sharding``
     - Historical weight, fixed overhead, memory, and concurrency limits.
   * - ``retry``
     - Explicit failure classes eligible for retry.
   * - ``change paths``
     - Source changes that select the suite.

Generate CI jobs, local ``run-tests`` routing, uv lock inputs, and validation from this model. Keep the generated file
small by including stable templates rather than copying and mutating a complete YAML file. Validate unknown fields,
missing locks, overlapping environment membership, unclassified snapshot tests, and invalid service combinations.

The migration branch also removed a subprocess per suite from configuration generation. The old generator invoked
Riot separately to list each suite's environment hashes before hashing their lock files. It now reuses the hashes
collected during the single environment pass and calculates the same cache key in-process. Full 203-suite generation
dropped from 24.6 to 0.69 seconds locally; the required-suite phase dropped from 24.6 to 0.31 seconds. The next real
CI configuration jobs took 86 and then 28 seconds, down from 97 seconds. The spread, and the 88-second main reference,
show that runner startup, cache warmth, and fixed setup now dominate this job. Add in-job phase timing before
optimizing it further.

Measurement log
---------------

2026-08-15, main commit ``e5c63be476``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Durations below are GitHub status pending-to-terminal intervals for the linked GitLab jobs. They are useful for
relative ranking but do not replace phase timers inside the jobs.

.. list-table::
   :header-rows: 1
   :widths: 38 22 40

   * - Work
     - Observed duration
     - Finding
   * - Generated test configuration
     - 88 seconds
     - Configuration generation is visible but not the dominant critical path.
   * - Pre-checks
     - 139 seconds
     - Every generated test job waits for this shared gate.
   * - Base ddtrace environments
     - 243 to 340 seconds
     - Build-once reuse works; Python 3.14 was the slowest setup lane.
   * - Tracer, 19 shards
     - 126 to 770 seconds
     - 6.1x overall. After excluding five duplicated uWSGI environments, the range was 492 to 770 seconds, or 1.6x.
   * - Tracer uWSGI, 5 shards
     - 96 to 280 seconds
     - 2.9x imbalance, and all five environments also matched the broad tracer selector.

Source inventory at commit ``5f6681657d``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* 19 hashes matched ``tracer``; five matched ``tracer-uwsgi``; the five were a complete overlap.
* One additional hash matched both ``integration_registry`` and ``contrib::integration_registry``.
* 118 suite entries set ``snapshot: true``.
* 504 snapshot marker or decorator references occurred across 78 Python files; 902 JSON snapshots existed.
* 821 pytest ``subprocess`` marker references, 562 ``run_in_subprocess`` references, and 329 direct child-process calls
  existed. These are lexical reference counts, not collected or parametrized test counts.
* 44 suite entries configured two whole-job retries.

2026-08-15, migration branch local generation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Before in-process cache keys: 24.6 seconds for all 203 suites.
* After in-process cache keys: 0.69 seconds for all 203 suites, with the same tracer cache key as the shell helper.
* Environment expansion and matching alone took about 0.1 seconds; repeated Riot subprocess startup dominated.

2026-08-15, migration branch CI validation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* Commit ``7f56b6fc7e`` emitted 14 focused tracer jobs instead of 19 and no ``tracer-uwsgi`` jobs. This validates the
  exclusive selector in real CI; the five uWSGI environments remain available through their dedicated suite.
* The configuration job took 97 seconds at ``7f56b6fc7e`` and 86 seconds after in-process cache keys at
  ``4bc8911a52``: an 11-second, or 11 percent, pending-to-success improvement.
* The following documentation-only commit, ``b7d31db910``, took 28 seconds with the same generator. Main commit
  ``e5c63be476`` took 88 seconds. These runs establish removal of the repeated local work but not a stable CI
  regression threshold; cache warmth and runner setup need internal phase timers before attributing the full change.

Next sequence
-------------

#. Add phase timing and isolation metadata without changing execution.
#. Replace count-based partitioning with duration-aware packing for tracer, then compare wall time and runner minutes.
#. Route snapshot and span-producing tests through clean-process lanes with a contamination stress test.
#. Split non-agent tests from snapshot jobs and replace the Python 3.9 Riot wait environment.
#. Consolidate subprocess launchers and narrow retry conditions.
#. Split cache ownership, remove Riot metadata, and make the suite model the single source for local and CI runs.
