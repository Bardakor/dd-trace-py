import asyncio
import contextvars
import threading
import time

import pytest

from ddtrace._trace.provider import DefaultContextProvider
from ddtrace.contrib.internal.asyncio.patch import patch
from ddtrace.contrib.internal.asyncio.patch import unpatch
from ddtrace.internal import core
from ddtrace.internal.context_watcher import is_context_watcher_registered
from ddtrace.internal.wrapping import is_wrapped
from ddtrace.trace import Context
from ddtrace.trace import Span


_orig_create_task = asyncio.BaseEventLoop.create_task
_orig_handle_run = asyncio.Handle._run
_orig_to_thread = asyncio.to_thread
_CONTEXT_WATCHER_AVAILABLE = is_context_watcher_registered()


@pytest.fixture
def patched_asyncio():
    was_patched = getattr(asyncio, "_datadog_patch", False)
    patch()
    yield
    if not was_patched:
        unpatch()


def test_event_loop_unpatch(tracer):
    patch()
    # ensures that the event loop can be unpatched
    unpatch()
    assert isinstance(tracer.context_provider, DefaultContextProvider)
    assert asyncio.BaseEventLoop.create_task == _orig_create_task
    assert asyncio.Handle._run == _orig_handle_run
    assert asyncio.to_thread == _orig_to_thread


def test_context_switch_instrumentation(tracer):
    unpatch()
    patch()
    try:
        assert is_wrapped(asyncio.Handle._run) is not _CONTEXT_WATCHER_AVAILABLE
        assert is_wrapped(asyncio.to_thread) is not _CONTEXT_WATCHER_AVAILABLE
    finally:
        unpatch()


@pytest.mark.skipif(not hasattr(asyncio, "eager_task_factory"), reason="eager tasks require Python 3.12+")
@pytest.mark.skipif(_CONTEXT_WATCHER_AVAILABLE, reason="the native context watcher is active")
@pytest.mark.asyncio
async def test_eager_task_factory_publishes_inline_context(tracer, patched_asyncio):
    loop = asyncio.get_running_loop()
    span = Span("eager")
    tracer.context_provider.activate(span)
    context = contextvars.copy_context()
    tracer.context_provider.activate(None)
    switches = []

    def record_context_switch():
        switches.append(tracer.context_provider.active())

    async def eager():
        assert switches[-1] is span
        return "done"

    core.on("python.context.switch", record_context_switch)
    try:
        loop.set_task_factory(getattr(asyncio, "eager_task_factory"))
        assert loop.create_task(eager(), context=context).result() == "done"
    finally:
        loop.set_task_factory(None)
        core.reset_listeners("python.context.switch", record_context_switch)
        span.finish()
        tracer.context_provider.activate(None)

    assert switches[-1] is None


@pytest.mark.asyncio
async def test_event_loop_double_patch(tracer, test_spans):
    # ensures that double patching will not double instrument
    # the event loop
    patch()
    patch()
    await test_tasks_chaining(tracer, test_spans)


@pytest.mark.asyncio
async def test_tasks_chaining(tracer, test_spans):
    # ensures that the context is propagated between different tasks
    @tracer.wrap("spawn_task")
    async def coro_3():
        await asyncio.sleep(0.01)

    async def coro_2():
        # This will have a new context, first run will test that the
        # new context works correctly, second run will test if when we
        # pop off the last span on the context if it is still parented
        # correctly
        await coro_3()
        await coro_3()

    @tracer.wrap("main_task")
    async def coro_1():
        await asyncio.ensure_future(coro_2())

    await coro_1()

    traces = test_spans.pop_traces()
    assert len(traces) == 1
    spans = traces[0]
    assert len(spans) == 3
    main_task = spans[0]
    spawn_task1 = spans[1]
    spawn_task2 = spans[2]
    # check if the context has been correctly propagated
    assert spawn_task1.trace_id == main_task.trace_id
    assert spawn_task1.parent_id == main_task.span_id

    assert spawn_task2.trace_id == main_task.trace_id
    assert spawn_task2.parent_id == main_task.span_id


@pytest.mark.asyncio
async def test_concurrent_chaining(tracer, test_spans):
    @tracer.wrap("f1")
    async def f1():
        await asyncio.sleep(0.01)

    @tracer.wrap("f2")
    async def f2():
        await asyncio.sleep(0.01)

    with tracer.trace("main_task"):
        await asyncio.gather(f1(), f2())
        # do additional synchronous work to confirm main context is
        # correctly handled
        with tracer.trace("main_task_child"):
            time.sleep(0.01)

    traces = test_spans.pop_traces()
    assert len(traces) == 1
    assert len(traces[0]) == 4
    main_task = traces[0][0]
    child_1 = traces[0][1]
    child_2 = traces[0][2]
    main_task_child = traces[0][3]
    # check if the context has been correctly propagated
    assert child_1.trace_id == main_task.trace_id
    assert child_1.parent_id == main_task.span_id
    assert child_2.trace_id == main_task.trace_id
    assert child_2.parent_id == main_task.span_id
    assert main_task_child.trace_id == main_task.trace_id
    assert main_task_child.parent_id == main_task.span_id


@pytest.mark.asyncio
async def test_propagation_with_new_context(tracer, test_spans):
    # ensures that if a new Context is activated, a trace
    # with the Context arguments is created
    ctx = Context(trace_id=100, span_id=101)
    tracer.context_provider.activate(ctx)

    with tracer.trace("async_task"):
        await asyncio.sleep(0.01)

    traces = test_spans.pop_traces()
    assert len(traces) == 1
    assert len(traces[0]) == 1
    span = traces[0][0]
    assert span.trace_id == 100
    assert span.parent_id == 101


@pytest.mark.asyncio
@pytest.mark.skipif(_CONTEXT_WATCHER_AVAILABLE, reason="the native context watcher is active")
async def test_context_switch_events_track_task_switches(tracer, patched_asyncio):
    first_started = asyncio.Event()
    resume_first = asyncio.Event()
    first_resumed = asyncio.Event()
    finish_first = asyncio.Event()
    switches = []

    def record_context_switch():
        switches.append(tracer.context_provider.active())

    async def first():
        with tracer.trace("first") as span:
            first_started.set()
            await resume_first.wait()
            try:
                assert switches[-1] is span
            finally:
                first_resumed.set()
            await finish_first.wait()

    async def second():
        await first_started.wait()
        with tracer.trace("second") as span:
            resume_first.set()
            await first_resumed.wait()
            try:
                assert switches[-1] is span
                assert switches[-2] is None
            finally:
                finish_first.set()

    core.on("python.context.switch", record_context_switch)
    try:
        await asyncio.gather(first(), second())
    finally:
        core.reset_listeners("python.context.switch", record_context_switch)


@pytest.mark.asyncio
@pytest.mark.skipif(_CONTEXT_WATCHER_AVAILABLE, reason="the native context watcher is active")
async def test_to_thread_context_switch_events(tracer, patched_asyncio):
    switches = []
    worker_id = None

    def record_context_switch():
        switches.append((threading.get_ident(), tracer.context_provider.active()))

    def worker():
        nonlocal worker_id
        worker_id = threading.get_ident()

    core.on("python.context.switch", record_context_switch)
    try:
        with tracer.trace("parent") as parent:
            await asyncio.to_thread(worker)
    finally:
        core.reset_listeners("python.context.switch", record_context_switch)

    assert worker_id is not None
    assert [context for ident, context in switches if ident == worker_id] == [parent, None]


@pytest.mark.asyncio
@pytest.mark.skipif(_CONTEXT_WATCHER_AVAILABLE, reason="the native context watcher is active")
async def test_context_switch_event_skips_finished_span(tracer, patched_asyncio):
    loop = asyncio.get_running_loop()
    callback_finished = loop.create_future()
    switches = []

    def record_context_switch():
        switches.append(tracer.context_provider.active())

    core.on("python.context.switch", record_context_switch)
    try:
        with tracer.trace("parent") as parent:
            with tracer.trace("child") as child:

                def callback():
                    callback_finished.set_result(switches[-1] if switches else None)

                loop.call_soon(callback)

            switches.clear()
            assert await callback_finished is parent
            assert parent in switches
            assert child not in switches
    finally:
        core.reset_listeners("python.context.switch", record_context_switch)
