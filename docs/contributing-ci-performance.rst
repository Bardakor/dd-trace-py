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
   * - Forked process
     - One test per forked child process, enforced by a runtime PID guard.
     - The default for pytest. It prevents two test cases from sharing a process while retaining parallel collection.
   * - Fresh process
     - One test per clean, exec-created Python process.
     - Tests whose correctness depends on collection-time imports, environment, or state that cannot safely be inherited.
   * - Agent isolated
     - One test process plus a unique test-agent session.
     - Tests that send spans to the agent but do not compare snapshots.
   * - Snapshot isolated
     - One test process plus test-agent start, flush, compare, and finalize.
     - Snapshot tests. Parallel lanes need independent session identifiers and a contamination test.

``xdist`` workers are reusable processes. Increasing ``-n`` is safe for the reusable class, but it does not provide
one-process-per-test isolation by itself. The uv runner adds ``pytest-forked`` so each test body runs in a child and
loads a guard that fails if it remains in the collection worker. Forked children inherit the worker's collection-time
state, so tests that create unsafe state during collection must use the ``fresh-process`` launcher instead.

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
     - Span and snapshot isolation coverage is incomplete
     - Critical
     - The uv runner now defaults every pytest suite to one forked child per test and fails if a test stays in the
       reusable collection worker. The full internal suite passed under this policy; its reusable-worker control failed
       a forked Symbol Database upload. Snapshot contamination stress coverage is still missing.
     - Validate the default across real CI, add a contamination sentinel, and promote collection-unsafe suites to
       clean ``exec`` isolation.
   * - 2
     - Duration-blind partitioning and ordering
     - High
     - Environment hashes are sorted and assigned round-robin. Tracer shards in one main run ranged from 126 to
       770 seconds, a 6.1x spread. With one environment per job, ordering alone cannot split a slow environment.
     - Use historical duration weights at environment and test-node level. Split slow environments and start the
       slowest safe units first.
   * - 3
     - Critical test jobs wait behind unrelated work
     - High
     - In the successful uv checkpoint, tracer jobs became eligible together but completed 7:56 to 11:22 later.
       GitHub status does not expose runner start, so queue and execution time cannot yet be separated.
     - Measure eligibility-to-start and execution inside CI, give changed-test jobs runner priority, and suppress
       unrelated parent and generated jobs during focused iteration.
   * - 4
     - Snapshot service cost is paid at suite granularity
     - High
     - 118 suite entries request the test agent. The whole job starts it even when only part of the selected test
       command performs snapshots.
     - Split reusable tests from agent-backed tests, then run the latter through isolated parallel lanes.
   * - 5
     - Shared build and cache setup remains on the critical path
     - High
     - Reusing one ddtrace base per Python version works. The validated uv checkpoint took 192 to 281 seconds per
       base. Removing the nested extension-cache setup changed all six producers from failure to success.
     - Time artifact transfer and uv materialization separately, then make each immutable Python artifact available
       to its consumers without waiting for unrelated versions.
   * - 6
     - Suite selectors schedule duplicate environments
     - High
     - The baseline ``tracer`` selector included all five ``tracer-uwsgi`` hashes, adding about 16 runner-minutes.
       Two integration-registry suite entries also selected the same hash. Focused CI now emits 14 exclusive tracer
       jobs instead of 19, with no uWSGI environments in the broad selector.
     - Fixed on the migration branch: selectors are exclusive and generation rejects environment overlap.
   * - 7
     - Subprocess execution has several overlapping paths
     - High
     - The tree has 821 ``subprocess`` marker references, 562 ``run_in_subprocess`` references, and 329 direct
       child-process calls. The first all-suite uv run also exposed a shared startup hook that broke test-owned
       ``sitecustomize`` modules across subprocess-heavy suites.
     - Instrument startup, import, body, and cleanup time; converge on one isolation launcher without batching span
       tests into a process.
   * - 8
     - Whole-shard retries amplify deterministic failures
     - Medium
     - 44 suite entries allow two retries. A deterministic failure can therefore consume three full shard runs.
     - Restrict CI retries to runner and service failures; retry an isolated test only when its failure class permits.
   * - 9
     - Broad, concurrently written caches
     - Medium
     - Generated jobs cache all of ``.cache`` under a suite key. A local gevent build restored c-ares configure state
       created with different compiler flags and failed until ``uv cache prune --ci`` removed 1.0 GiB and 37,000 files.
     - Record cache transfer bytes and time, separate immutable consumer caches from compiler outputs, and include
       native build inputs in every reusable cache key.
   * - 10
     - Test configuration has multiple sources and slow generated copies
     - Medium
     - Suite routing, uv environment nodes, launch metadata, YAML templates, and generated jobs still repeat related
       concepts. The baseline started one environment-tool subprocess per suite to calculate cache keys. Full local
       generation took 24.6 seconds; the observed CI configuration job took 88 to 97 seconds.
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

Implemented canary and remaining experiment:

#. The core environment model now declares ``forked-process`` as the default. Local and generated CI commands export
   that policy, add ``--forked``, and load a PID guard that fails if the test body remains in an xdist worker.
#. Keep ``fresh-process`` as an explicit suite override. Its controller collects once and starts each node with a clean
   pytest ``exec`` while reusing only the container, resolved dependencies, and test-agent service.
#. Add a contamination sentinel: run two known span tests concurrently and fail if either process observes the
   other's trace or active context.
#. Give every parallel agent lane an independent session namespace. If the test agent cannot guarantee this, use one
   agent sidecar per lane or serialize the snapshot handshake while keeping non-agent work parallel.
#. Reject configurations that disable process isolation for span-producing or snapshot tests.

Success gates:

* Zero mixed-context or cross-session traces in repeated stress runs.
* Zero ignored unmatched-trace responses before removing the existing expected-failure behavior.
* No regression in snapshot content, process cleanup, or agent connectivity.
* A maintained long-term isolation implementation. `pytest-forked 1.6.0
  <https://pypi.org/project/pytest-forked/1.6.0/>`_ is minimally maintained, depends on the legacy ``py`` package, and
  does not support Windows; dd-trace-py's container CI is Linux, but this remains a migration risk rather than a
  dependency to accept silently.

2. Balance by duration, not hash count
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``get-test-environment-ids.sh`` sorts IDs and ``ci-split-input.sh`` assigns them round-robin. The generator also scales
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
#. Replace the ``wait`` environment with a small runner-owned readiness probe. The current probe requires the
   Python 3.9 base and a large service-client dependency set even for jobs testing another Python version.

Do not share a single snapshot session between tests, and do not move snapshot tests into a reused worker to save
startup time.

4. Preserve useful build reuse and remove avoidable setup
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The base environment is built once per required Python version and downloaded by dependent jobs. This build-once
reuse remains in the uv design. The next gains are in cache hits, artifact size, and
allowing each Python-version lane to start as soon as its own artifact and pre-checks are ready.

Proposed experiment:

#. Add timers for image pull, base artifact download, uv cache restore, dependency prefix creation, editable ddtrace
   activation, test execution, and cache upload.
#. Key the base artifact by commit, Python version, build inputs, and native build inputs.
#. Key an immutable dependency prefix by Python version, lock digest, platform, and uv version. Let test jobs pull it
   without uploading the same data concurrently.
#. Keep compiler caches separate from pip and uv caches so their policies and retention can differ.

The migration branch first converted ``ext_cache.py`` to a standalone uv script, removing its per-Python management
venv. Real CI then showed that the extension-cache setup still failed before the base build on every Python version.
Removing that second native-artifact layer made all six base producers pass. ``ext_cache.py`` remains a local
warm-build diagnostic; CI has one artifact owner per Python version.

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

The direct-uv runner initially placed its dependency activation ``sitecustomize`` ahead of the repository on
``PYTHONPATH``. Python imports only the first module with that name, so tests that intentionally supplied their own
startup hook silently lost it. Dependency activation now runs from a ``.pth`` file installed in the shared base.
This still processes dependency-owned ``.pth`` files, but leaves ``sitecustomize`` available to the test. The base
fingerprint includes this change so cached bases cannot retain the old startup contract.

7. Narrow retries
^^^^^^^^^^^^^^^^^

Suite-level ``retry: 2`` reruns a complete GitLab job. This is appropriate for a failed runner or unavailable service,
but costly and misleading for an assertion or deterministic setup failure. Prefer CI retry conditions for runner and
service failures. If a known flaky test must be retried, rerun that isolated node in a fresh process and retain all
attempt results.

Generated uv jobs now apply the configured retry count only to API, runner-system, and stuck-or-timeout failures.
``script_failure`` is intentionally excluded, so a deterministic test or setup failure reports after one shard run.

8. Split cache ownership
^^^^^^^^^^^^^^^^^^^^^^^^

Each generated job currently caches the broad ``.cache`` directory with a suite-level key. Before changing policy,
capture restore and upload duration, transferred bytes, hit rate, extracted size, and concurrent writers. A likely
target model is:

* one read-only uv download cache shared by compatible locks;
* immutable per-lock dependency prefixes produced once and consumed by test jobs;
* a separately keyed native compiler cache;
* no test-job upload when the job cannot add a reusable artifact.

A local uv prefix rebuild exposed why the separation is a correctness requirement. gevent's bundled c-ares configure
state had been cached with different ``CFLAGS`` and caused a deterministic native build failure. Running
``uv cache prune --ci`` removed about 1.0 GiB across 37,000 files; the same 802-test environment then built and passed
in about 28 seconds. The next CI instrumentation should report native cache keys and pruneable bytes, not only a
binary hit or miss.

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
the environment tool separately to list each suite's IDs before hashing their lock files. It now reuses the IDs
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

2026-08-15, Riot structure flattening
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The migration first froze Riot's resolved behavior as a checked-in contract: 1,936 ordered instances, 1,886 unique
environment hashes, 193 resolved names, 203 suite selections, commands, dependency order, and environment variables.
Anonymous inheritance containers below the root then fell from 13 to zero while all 197 named declarations remained.
The contract digest and every environment hash stayed unchanged.

Before deletion, one anonymous leaf remained at depth three under the named ``tracer-python-optimize`` node. It was a
concrete Python variant rather than an inheritance container. The final JSON inventory keeps that resolved variant
inside the named node and has no anonymous grouping layers.

2026-08-15, uv inventory and base-build experiment
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Local routing, direct execution, and CI generation now read a flat JSON inventory. The inventory
factors eight shared dependencies and 12 base variables into ``core.json``, while retaining all 1,936 instances and
1,886 stable IDs. The final inventory is sharded by 193 preserved named nodes; every file remains below the CI size
limit, and validation covers selection order, definition references, IDs, and lock completeness.

The first Python 3.12 uv editable build spent 46.65 seconds preparing ddtrace and 0.16 seconds installing it. A warm
fingerprint check, including container startup, took 1.97 seconds. These local numbers are not directly comparable
to the earlier 243-to-340-second CI jobs because native outputs and compiler caches were warm. The next pushed CI run
must measure a clean producer before treating the difference as a pipeline win. A dependency-prefix cache bug found
during this experiment was fixed by including the base fingerprint; otherwise console-script shebangs kept selecting
an obsolete base interpreter.

2026-08-15, Hatch canary and direct-uv decision
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The first Hatch Python 3.12 tracer canary created and installed its own environment. It failed three attempts after
roughly 4, 20, and 14 minutes, so merely changing the environment manager did not preserve build reuse. A second
attempt downloaded the existing base artifact but still failed because Hatch expected ownership of the environment.

The successful canary explicitly pointed Hatch at the already-built Python 3.12 environment. Its pending-to-success
interval was 555 seconds. The equivalent existing tracer shard in the same pipeline took 565 seconds. The 10-second,
1.8 percent difference is within single-run noise and does not establish a Hatch test-runtime win. It does show that
Hatch can preserve performance only after custom wiring makes it reuse the same base that uv can execute directly.

The direct-uv runner keeps that build-once behavior without a second environment model, Hatch bootstrap, template
matrix, or path-ownership workaround. Hatch therefore adds configuration and failure modes without a measured CI
benefit for this repository. uv remains the dependency resolver, lock compiler, base builder, layer installer, and
command launcher.

2026-08-15, first full uv checkpoint
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Commit ``0d13ea1a36`` was the first checkpoint that routed every generated test job through uv. The configuration
job succeeded. The repository file-size check rejected the original monolithic environment inventory, and all six
Python base-build jobs failed before test consumers could start. Internal job traces were unavailable during the
investigation because the GitLab endpoint was unreachable from the development network.

The size failure is fixed by sharding resolved environments across the 193 preserved named nodes; validation now
rejects JSON files above 100 KB. The builder also resolves the exact preinstalled CI interpreter instead of allowing
uv to select or download one. This base-build change remains unvalidated until the next real CI run, so it is a
corrective hypothesis rather than a measured win.

An exact local smoke test of the replacement extension-cache command found a separate regression before it reached
CI: setuptools tried to fetch ``patchelf`` through pip, but uv script environments do not seed pip. Declaring the
same conditional ``patchelf`` dependency as the project build configuration fixed the command. The clean restore
path now completes without recreating the nested venv.

Commit ``e6ac6c2b78`` confirmed that the sharded inventory passes the real file-size gate and configuration generation
passes. All six combined base-build and smoke jobs still failed, after roughly two to six minutes depending on the
Python version. A disposable cold checkout passed extension restore, base build, and Python 3.12 smoke locally with
the CI build variables. GitLab traces and the pinned internal testrunner image were unreachable without AppGate, so
the failure phase could not be read directly.

The next checkpoint separates smoke jobs from base artifact producers. This makes the failing phase visible in job
status, allows successful base artifacts to unblock their consumers, and keeps smoke coverage as an independent
required check instead of commenting it out.

That checkpoint confirmed the producer itself fails on all six versions; no smoke job starts. Riot 0.22 created its
base with ``virtualenv`` and ran ``pip install -e .``. The uv migration had changed both venv creation and the native
editable build frontend at once. The next iteration keeps uv venv creation and all test dependency management but
restores pip only for the editable ddtrace build, isolating the remaining behavior change while retaining direct uv
execution.

The pip-only iteration still failed in the producer before smoke could start. Exact job traces were unavailable because
the internal DDCI log wrapper could not reach GitLab without AppGate. The next diagnostic removes
``.cached_testrunner`` from the base producer. This keeps the producer independent from ``ext_cache.py`` and tests the
uv base build directly. It also removes a duplicated native-artifact layer: the base environment is itself the native
build artifact consumed by every test job.

Commit ``6ca85c94ad`` validated that diagnosis. All six base producers passed, with status-to-status elapsed times of
192 to 281 seconds, followed by six successful native smoke jobs. Five smoke jobs completed in 26 to 37 seconds;
Python 3.9 completed in 86 seconds, including any queue delay. All 14 tracer shards then started from the shared base
artifacts. The cached-runner generator is removed because no consumer remains and CI now has one native-artifact
owner per Python version.

Commit ``9864f0b487`` kept all six bases and smoke checks green. Base intervals were 199 to 309 seconds and the
pre-check gate took 151 seconds. The agent-readiness change was necessary for the reproduced agent-backed assertion,
but it was not the common CI failure: all 14 tracer shards failed once, 102 to 204 seconds after becoming eligible.
Restricting retries to infrastructure failures prevented two redundant reruns of every deterministic failure.

The common tracer failure reproduced locally in a subprocess test. A fixture changed the working directory before
launching plain ``python -m unittest``; the uv overlay exposed dependencies, but its ``PYTHONPATH`` did not retain the
repository root. The child interpreter therefore could not import ``tests``. The runtime now includes the repository
root in every child process environment. The exact three matching subprocess cases pass after the fix, as do the uv
runtime unit tests.

The same CI run exposed a documentation failure. The exact uv docs command reproduced it as four missing spelling
dictionary entries from this document, then passed after the dictionary update. This was a documentation gate rather
than an environment construction failure.

Commit ``10464b05a1`` kept the six producers and smoke jobs green and made the docs job pass. Producer intervals were
183 to 281 seconds, while the pre-check gate grew to 222 seconds. The tracer jobs waited for the complete six-version
matrix because each shard could contain any Python version. All 14 still failed, despite the exact Python 3.13
optimized environment passing 6,869 tests locally in 252.66 seconds.

The shared failure occurred before pytest. Every tracer job invoked the named ``wait`` environment for ``ddagent``,
but that legacy environment forced ``DD_TRACE_AGENT_URL`` to ``http://testagent:9126``. The suite started only
``ddagent`` at ``http://localhost:8126``, so every shard polled an absent service and failed. The wait environment now
inherits the suite-selected URL. This also identifies repeated installation of the Python 3.9 wait environment as a
high-priority dependency and subprocess bottleneck to remove after the correctness checkpoint.

Commit ``74c46041ed`` validated the fix with a temporary test-only parent pipeline. Generation took 48 seconds,
producers took 211 to 266 seconds, prechecks took 251 seconds, all six smoke checks passed, and docs passed. All 14
tracer environments passed. Their eligibility-to-terminal intervals ranged from 476 to 682 seconds. The status API
does not expose runner start, so the completion groups alone cannot distinguish queue delay from workload imbalance.

Commit ``a21854c40d`` tested two tracer environments per job. All seven jobs passed, but their
eligibility-to-terminal intervals were 824 to 968 seconds. The fastest packed job was slower than the entire 14-job
control, and suite completion regressed by 286 seconds, or 42 percent. The migration therefore keeps one environment
per tracer job. Reducing fixed setup remains useful, but serial environment packing is not a critical-path win.

The tracer-only generation filter is now removed. The next run validates every selected named suite through the uv
path while the temporary parent pipeline continues to suppress unrelated package and publishing work.

2026-08-15, first all-suite uv run
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Commit ``2141132edb`` expanded 83 selected suite groups into 855 sharded test contexts. All six base producers,
all six smoke checks, prechecks, and documentation passed. At the broad diagnostic checkpoint, 653 contexts had
passed, 194 had failed, and 31 remained pending. Failures clustered in subprocess-heavy suites, including AppSec,
CI Visibility, integration, and internal tests, which indicated a shared runtime contract rather than hundreds of
independent dependency errors.

The internal Python 3.10 shard reproduced two failures locally. A test that requires its neighboring
``sitecustomize`` produced no output, and a forked Symbol Database upload received an unexpected response. Both
exact tests pass after moving dependency-prefix activation from the runner-owned ``sitecustomize`` into the base
``.pth`` hook. The runtime unit tests also pass.

Commit ``e61af881bd`` selected internal, AppSec FastAPI, AppSec Flask test-agent, CI Visibility pytest snapshot, and
Selenium as a 41-job subprocess-heavy slice. All six bases, six smoke checks, and prechecks passed, while all 41
test jobs failed. The documentation failure was separate: the Sphinx dictionary contained ``Subprocess`` but not
its lowercase form, and the exact documentation command passes after adding it.

The exact failed Selenium Python 3.12 environment then reproduced locally. A nested pytest command retained
``PYTHONPATH`` but intentionally filtered most other environment variables, removing ``DD_TEST_SITE_PACKAGES``.
Its generated pytest launcher selected the shared base interpreter, which could no longer find pytest in the uv
dependency prefix. The runtime now also appends the resolved dependency directory to ``PYTHONPATH`` as a fallback;
the base ``.pth`` hook remains responsible for processing dependency-owned ``.pth`` files. The exact Selenium shard
passes all four tests in 13.31 seconds, and the startup-hook regression remains green. A local-runner fix also maps
suite service aliases and the test-agent URL to host-network endpoints, allowing CI failures to be reproduced with
the same command. Repeat the representative slice before returning to the 855-context run.

Commit ``f66d678149`` validated the shared subprocess fix. All 14 CI Visibility pytest snapshot jobs and both
Selenium jobs passed, as did three of six internal shards. All six base producers, smoke checks, prechecks, and the
documentation job also passed. The remaining failures were 17 AppSec FastAPI jobs, two AppSec Flask test-agent jobs,
and internal shards one, three, and five.

The 19 AppSec jobs did not reach pytest. They explicitly started ``testagent`` but inherited the default localhost
URL for their readiness probe; only snapshot jobs exported the service alias before waiting. Generated jobs now
export ``http://testagent:9126`` before probing an explicitly requested test agent. The local container runner also
forwards its host-network URL override into the test container. With that override, the exact Python 3.12 FastAPI
environment passes 51 tests and skips five. The three internal CI shard failures remain a separate investigation;
the complete first failing Python 3.11 environment passes 823 tests locally, with six skips and two expected failures.
This is not evidence that those CI failures are fixed, so they remain in the next focused checkpoint.

Commit ``f3b3c83a63`` passed all 41 focused test jobs: 17 AppSec FastAPI, two AppSec Flask test-agent, 14 CI Visibility
pytest snapshot, two Selenium, and six internal jobs. All six base producers, smoke checks, and prechecks also passed.
The only failure was documentation spelling for the literal ``localhost`` endpoint; the source dictionary now contains
that word. This run validates the direct-uv runtime and service routing for the representative subprocess-heavy slice,
but predates per-test process isolation.

2026-08-15, process-isolation experiment
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The clean ``exec`` controller passed 835 internal tests with four parallel lanes, but took 816.11 seconds. A comparable
reusable-worker run took 27.52 seconds of pytest time and 45.67 seconds end to end, while failing the Symbol Database
fork-upload test with a test-agent 404. Clean ``exec`` was about 23 times slower, so it remains the strict fallback
rather than the default.

The fork-per-test canary ran the same internal suite with ten xdist workers. It passed 825 tests, skipped ten, and had
three expected failures in 22.96 seconds of pytest time and 41.08 seconds end to end. That was 16.6 percent faster in
pytest time and about 10 percent faster end to end than the reusable-worker control, while also avoiding its failure.
After adding the dependency to every checked-in uv lock and enabling the runtime PID guard, the current 837-test
internal suite passed 827 tests, skipped ten, and had two expected failures in 29.99 seconds of pytest time and 48.82
seconds end to end. The difference between the two forked runs is normal suite and host variation, not evidence of a
regression. Real focused CI is the next performance gate.

Next sequence
-------------

#. Validate fork-per-test isolation in the 41-job representative CI slice, then expand to every generated suite.
#. Add a cross-process contamination stress test and classify any collection-unsafe suite as ``fresh-process``.
#. Replace count-based partitioning with duration-aware packing for tracer, then compare wall time and runner minutes.
#. Split non-agent tests from snapshot jobs and replace the Python 3.9 wait environment.
#. Add phase timing, split cache ownership, and consolidate subprocess launchers.
#. Make the suite model the single source for isolation, services, dependencies, sharding, and change routing.
