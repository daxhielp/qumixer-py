"""The synchronous facade (PRD 11.2).

One implementation only: this class contains no protocol logic. It runs an
event loop in a daemon thread and proxies every coroutine method onto it,
blocking the caller until the coroutine completes. The public surface is
generated from ``AsyncQuMixer`` at import time (see the bottom of this
module), not hand-maintained, so the two APIs cannot drift --
``test_the_two_apis_expose_the_same_method_names`` enforces this by diffing
``dir()`` of the two classes *and* of two live instances, and
``test_nothing_reachable_hands_back_an_unawaited_coroutine`` walks a live
mixer to prove nothing reachable is left unwrapped.

The object facade (``qumidi/model/``) is shared, unmodified protocol logic --
built once, from ``async def`` methods, for both APIs (PRD 3, 11.1). Every
value handed back by a proxied call is passed through ``_wrap_value``, which
recursively makes coroutine methods block and re-wraps anything that is
itself one of those facade objects, so a whole chain built from separate
synchronous expressions -- e.g. ``mixer.input(6).send(Mix.LR).pan.set_raw(x)``
-- behaves synchronously at every step, without duplicating any of that logic
here.

Property *reads* (cache lookups) never cross the thread boundary -- they are
plain attribute/dict reads, safe under the GIL, executed directly on the
caller's own thread. Property *setters* (``proxy.db = ...``,
``proxy.raw = ...``) are handled entirely by ``ParamProxy._blocking``
(``qumidi/model/base.py``): they block until the write is flushed, the same
guarantee as ``await`` in the async API, and raise the same exceptions.

The same ``_wrap_value`` treatment covers the two *instance*-level sub-APIs
``mixer.meters`` (``qumidi.meters``, Slice 10) and ``mixer.strips``
(``qumidi.protocol.strips``, Slice 11). Neither is visible to the generation
loop below -- ``dir()`` on a *class* never sees an attribute set in
``__init__`` -- so both arrive through ``__getattr__``, and both are listed
in ``_FACADE_MODULES`` so their coroutine methods block like everything else.
Their two async-only shapes get real synchronous equivalents rather than an
error: ``mixer.meters.enabled()`` (an ``@asynccontextmanager``) becomes a
plain ``with`` block via ``_SyncContextManager``, and
``mixer.meters.stream()`` (an async generator) becomes a plain ``for`` loop
via ``_SyncIterator``, which pulls one frame per iteration through the very
same ``_run`` used by every other blocking call. Both are pure thread
marshalling over the unmodified async implementation -- no second copy of any
protocol logic, and no capability the sync API lacks::

    with QuMixer("192.168.1.60") as mixer:
        mixer.wait_synced()
        with mixer.meters.enabled(), mixer.meters.stream() as frames:
            for frame in frames:
                ...
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, TypeVar

from qumidi.client_async import AsyncQuMixer
from qumidi.events import ChangeEvent
from qumidi.model.base import _in_loop_thread
from qumidi.protocol.channels import ChannelId
from qumidi.transport.base import ConnectionState

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Bounded, and drops the oldest queued callback (with a warning) once full --
#: no unbounded queue anywhere in this library (PRD 18).
_CALLBACK_QUEUE_MAX = 1024

#: How often _run's blocking wait re-checks liveness (self._closed, and
#: whether the in-flight future ever completed) while polling, rather than
#: making one unbounded .result() call. This is not a ceiling on how long a
#: legitimate call may take -- a multi-second ramp or a scene recall's resync
#: waits as long as it needs to, since nothing times out while the mixer
#: stays open -- it only bounds how long a call can be stuck *after* a
#: concurrent close() has made it clear no result will ever arrive.
_POLL_INTERVAL = 0.2

#: Modules whose objects are part of the async surface a sync caller reaches
#: *through* the mixer -- the generated object facade (Task 9.1), plus the two
#: instance-level sub-APIs the facade also has to cover: MeterController
#: (Slice 10) and StripApi (Slice 11). A value whose type comes from one of
#: these gets wrapped recursively by _wrap_value so its own methods keep
#: blocking correctly; anything else (an int, a CachedValue, a plain str) is
#: returned untouched.
#:
#: Adding a new async sub-API to AsyncQuMixer means adding its module here.
#: test_nothing_reachable_hands_back_an_unawaited_coroutine
#: (tests/test_sync_facade.py) is the structural guard that catches a
#: forgotten entry -- it walks a live QuMixer, instance attributes included,
#: rather than diffing dir() of the two *classes*, which by construction can
#: never see an attribute set in AsyncQuMixer.__init__.
_FACADE_MODULES = frozenset(
    {
        "qumidi.model.base",
        "qumidi.model.channel",
        "qumidi.meters",
        "qumidi.protocol.strips",
    }
)


class QuMixer:
    """Blocking control of one Qu console (PRD 11.2).

    Same constructor signature as ``AsyncQuMixer``, plus ``thread_safe``. Use
    as a context manager, or call connect()/close() directly::

        with QuMixer("192.168.1.60") as mixer:
            mixer.wait_synced()
            mixer.input(6).fader.db = -6.0

    Every ``AsyncQuMixer`` coroutine method is available here without
    ``await`` -- see the generation loop at the bottom of this module.
    Callbacks registered through ``on_change`` and friends run on this
    mixer's own background loop thread by default; pass ``thread_safe=True``
    to instead queue them for ``drain_callbacks()`` to collect on the
    caller's thread.
    """

    def __init__(self, *args: Any, thread_safe: bool = False, **kwargs: Any) -> None:
        self._thread_safe = thread_safe
        self._closed = False
        #: Guards the check-and-set of _closed in close() -- without it, two
        #: threads calling close() concurrently could both observe
        #: _closed=False before either sets it, and both run the teardown
        #: sequence (double-stopping the loop, double-joining the thread).
        self._close_lock = threading.Lock()
        self._callback_queue: deque[Callable[[], None]] = deque()
        self._queue_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="qumidi-loop", daemon=True)
        self._thread.start()
        try:
            self._async: AsyncQuMixer = self._run(self._make(args, kwargs))
        except BaseException:
            # Never leak the loop thread just because construction failed --
            # e.g. a bad keyword argument raised inside AsyncQuMixer.__init__.
            self._stop_loop()
            raise

    # -- loop plumbing --------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _make(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> AsyncQuMixer:
        return AsyncQuMixer(*args, **kwargs)

    def _run(self, coro: Coroutine[Any, Any, T], *, _internal: bool = False) -> T:
        """Schedule ``coro`` onto this mixer's loop and block for the result.

        Safe to call from any thread -- including several at once, which is
        exactly how ``run_coroutine_threadsafe`` is meant to be used -- except
        the loop's own thread. A callback running inline on the loop thread
        (e.g. an ``on_change`` callback registered without
        ``thread_safe=True``) that calls back into any proxied method would
        otherwise block that very thread waiting on a coroutine that can only
        ever run *on* it -- a reentrant deadlock. Detected and raised instead,
        reusing the exact same ``_in_loop_thread`` check
        ``ParamProxy._blocking`` (qumidi/model/base.py) already uses for its
        own property-setter sugar path, rather than a second copy of it.

        Every generated proxy method (coroutine and property alike) and
        ``_wrap_value``'s blocking wrapper ultimately call this one method
        (``run`` in ``_wrap_value`` is always this bound method), so the guard
        and the polling below cover them all from this single place.

        Waits in short polled slices rather than one unbounded ``.result()``
        call, so a call racing a concurrent ``close()`` -- the submission
        loses the race against ``loop.stop()`` actually taking effect, and
        the coroutine is queued but never runs -- fails with a clear error
        within one poll interval instead of hanging forever. This is not a
        ceiling on how long a legitimate call may itself take: nothing times
        out as long as the mixer stays open.
        """
        if _in_loop_thread(self._loop):
            coro.close()
            raise RuntimeError(
                "reentrant call into the sync facade: this was invoked from inside a "
                "callback already running on this mixer's own loop thread (e.g. an "
                "on_change callback registered without thread_safe=True calling an "
                "explicit proxied method) -- blocking here would deadlock the loop "
                "thread that call itself needs in order to run. Register callbacks "
                "with thread_safe=True and use drain_callbacks() to run "
                "callback-triggered writes from a different thread instead."
            )
        if not _internal and self._closed:
            coro.close()
            raise RuntimeError(f"{type(self).__name__} is closed; cannot submit further calls")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        while True:
            try:
                return future.result(timeout=_POLL_INTERVAL)
            except concurrent.futures.TimeoutError:
                if not _internal and self._closed and not future.done():
                    future.cancel()
                    raise RuntimeError(
                        f"{type(self).__name__} was closed while this call was still "
                        f"in flight; it may or may not have reached the console"
                    ) from None

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()

    # -- lifecycle --------------------------------------------------------------

    def connect(self) -> None:
        self._run(self._async.connect())

    def close(self) -> None:
        """Idempotent -- concurrent close() calls are safe: _close_lock
        serialises the check-and-set of _closed, so only one caller ever
        proceeds past it and runs the actual close sequence; every other
        concurrent caller sees _closed already True under the same lock and
        returns immediately. Always joins the loop thread and never leaves a
        non-daemon thread behind -- even if the underlying close() raises.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            # _internal=True: this is the one call allowed to run *while*
            # self._closed is already True (every other caller of _run is
            # rejected once it is) -- it's the close sequence itself.
            self._run(self._async.close(), _internal=True)
        finally:
            self._stop_loop()

    def __enter__(self) -> QuMixer:  # noqa: PYI034 -- typing.Self needs py311+
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- thread-safe callbacks (PRD 11.2) ----------------------------------------

    def _enqueue_callback(self, call: Callable[[], None]) -> None:
        with self._queue_lock:
            if len(self._callback_queue) >= _CALLBACK_QUEUE_MAX:
                self._callback_queue.popleft()
                log.warning(
                    "sync facade callback queue is full (%d queued); dropping the "
                    "oldest queued callback. Call drain_callbacks() more often, or "
                    "reduce callback traffic.",
                    _CALLBACK_QUEUE_MAX,
                )
            self._callback_queue.append(call)

    def drain_callbacks(self) -> list[Callable[[], None]]:
        """With thread_safe=True, collect callbacks queued for the caller's own thread.

        Each item is the original callback, already bound to the arguments it
        fired with -- call it from whichever thread should run it. Without
        thread_safe=True this is always empty: callbacks run inline on the
        loop thread instead (PRD 11.2).
        """
        with self._queue_lock:
            items = list(self._callback_queue)
            self._callback_queue.clear()
        return items

    def _wrap_callback(self, cb: Callable[..., None]) -> Callable[..., None]:
        """PRD 11.2: with thread_safe=True, marshal delivery onto the bounded
        queue instead of invoking ``cb`` inline on the loop thread.
        """
        if not self._thread_safe:
            return cb

        def marshal(*args: Any) -> None:
            self._enqueue_callback(lambda: cb(*args))

        return marshal

    # -- callback registration: explicit, so thread_safe=True can intercept -----

    def on_change(
        self,
        cb: Callable[[ChangeEvent], None],
        channel: ChannelId | None = None,
        param: str | None = None,
    ) -> Callable[[], None]:
        return self._async.on_change(self._wrap_callback(cb), channel=channel, param=param)

    def on_connection_state(self, cb: Callable[[ConnectionState], None]) -> None:
        self._async.on_connection_state(self._wrap_callback(cb))

    def on_sync_complete(self, cb: Callable[[], None]) -> None:
        self._async.on_sync_complete(self._wrap_callback(cb))

    def on_write_unconfirmed(self, cb: Callable[[ChannelId, str, int | None], None]) -> None:
        self._async.on_write_unconfirmed(self._wrap_callback(cb))

    def on_scene_recalled(self, cb: Callable[[int], None]) -> None:
        self._async.on_scene_recalled(self._wrap_callback(cb))

    def on_meter_layout_error(self, cb: Callable[[str], None]) -> None:
        self._async.on_meter_layout_error(self._wrap_callback(cb))

    # -- fallback for plain instance attributes (.host, .cache, .events, ...) ---

    def __getattr__(self, name: str) -> Any:
        """Anything AsyncQuMixer sets outside its class body (``.host``,
        ``.cache``, ``.events``, ``.names``, ``.meters``, ``.strips``, ...)
        isn't visible to the generation loop below -- ``dir(AsyncQuMixer)``
        doesn't see it either -- but is still forwarded here, through
        ``_wrap_value``, so it is both reachable *and* blocking.
        """
        if name == "_async":
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute '_async' -- "
                f"construction must have failed before it was set"
            )
        return _wrap_value(getattr(self._async, name), self._run)

    def __dir__(self) -> list[str]:
        """This class's own surface *plus* everything __getattr__ forwards.

        Without this, ``dir(mixer)`` reports only the generated class-level
        proxies and silently omits every instance attribute the async mixer
        sets in ``__init__`` (``.meters``, ``.strips``, ``.cache``,
        ``.events``, ...) -- which is precisely the blind spot that let the
        unwrapped ``meters``/``strips`` sub-APIs ship. An instance-level
        ``dir()`` now sees them, so the drift guard can too.
        """
        return sorted(set(super().__dir__()) | set(dir(self._async)))


# -- generating the public surface from AsyncQuMixer (PRD 11.2) -----------------
#
# dir() on a *class* never triggers instance-level __getattr__ -- only real
# entries in the class's __dict__ (and its bases') show up -- so
# test_the_two_apis_expose_the_same_method_names, which diffs dir() of the two
# classes, would fail if the proxy surface only existed dynamically via
# __getattr__ above. Every public AsyncQuMixer method/property therefore gets
# a real attribute on QuMixer, generated once here at import time, so the
# names are genuinely present on the class.
#
# The converse is the trap this loop cannot escape and QuMixer.__dir__ exists
# to cover: an attribute AsyncQuMixer sets in __init__ (.meters, .strips, ...)
# is invisible here, so it gets no generated proxy and reaches the caller
# through __getattr__ -- correctly wrapped only because its module is listed
# in _FACADE_MODULES above.


def _wrap_value(value: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> Any:
    """Recursively make one object-facade value safe to use from the sync API.

    A coroutine-bound method becomes a call that runs on the mixer's own loop
    and blocks for the result (itself re-wrapped, in case it is also a facade
    object). A plain method or property that might return another facade
    object (``.send(Mix.LR)``, ``.fader``, ``.peq.hf``, ...) has its result
    wrapped too, recursively -- so a whole chain built purely from separate,
    synchronous caller-side expressions keeps behaving synchronously at every
    step.

    Two async-only *result* shapes are converted rather than handed back
    broken, because a synchronous caller cannot write ``async with`` or
    ``async for`` at all: an async generator object (``meters.stream()``)
    becomes a blocking ``_SyncIterator``, and an async context manager
    (``meters.enabled()``) becomes a blocking ``_SyncContextManager``. Both
    are detected on the returned object, not on the callable -- an
    ``@asynccontextmanager``-decorated method is an ordinary function as far
    as ``inspect`` is concerned, so ``call_and_wrap`` above calls it first and
    this recursion then sees what it actually produced.

    Anything else (an int, a ``CachedValue``, a plain str) passes through
    untouched.
    """
    if inspect.iscoroutinefunction(value):

        def run_blocking(*args: Any, **kwargs: Any) -> Any:
            return _wrap_value(run(value(*args, **kwargs)), run)

        run_blocking.__name__ = getattr(value, "__name__", "run_blocking")
        run_blocking.__doc__ = value.__doc__
        return run_blocking
    if inspect.ismethod(value) or inspect.isfunction(value):

        def call_and_wrap(*args: Any, **kwargs: Any) -> Any:
            return _wrap_value(value(*args, **kwargs), run)

        call_and_wrap.__name__ = getattr(value, "__name__", "call_and_wrap")
        call_and_wrap.__doc__ = value.__doc__
        return call_and_wrap
    if inspect.isasyncgen(value):
        return _SyncIterator(value, run)
    if _is_async_only_context_manager(value):
        return _SyncContextManager(value, run)
    if type(value).__module__ in _FACADE_MODULES:
        return _FacadeProxy(value, run)
    return value


def _is_async_only_context_manager(value: Any) -> bool:
    """True for something usable *only* as ``async with``.

    ``AsyncQuMixer`` is excluded explicitly: it is an async context manager
    too, but ``QuMixer`` already owns the connect/close lifecycle and
    re-wrapping it here would hand out a second, competing one. Anything that
    already offers ``__enter__`` is left alone for the same reason -- it has a
    synchronous protocol of its own and does not need one synthesised.
    """
    cls = type(value)
    if isinstance(value, AsyncQuMixer) or hasattr(cls, "__enter__"):
        return False
    return hasattr(cls, "__aenter__") and hasattr(cls, "__aexit__")


async def _anext(agen: Any) -> Any:
    """``await agen.__anext__()`` as a real coroutine.

    ``agen.__anext__()`` returns an ``async_generator_asend``, which is
    awaitable but is not a coroutine on every supported Python (3.10 is the
    floor here), and ``asyncio.run_coroutine_threadsafe`` insists on a genuine
    coroutine. Awaiting it inside one costs nothing and works everywhere.
    """
    return await agen.__anext__()


async def _aclose(agen: Any) -> None:
    """``await agen.aclose()`` as a real coroutine -- see ``_anext``."""
    await agen.aclose()


class _SyncIterator:
    """A blocking iterator over one async generator (``meters.stream()``).

    Each ``next()`` drives exactly one ``__anext__`` on the mixer's own loop
    thread through the same ``_run`` every other blocking call uses, so a
    caller waiting for the next meter frame is blocked in ``_run``'s polled
    wait -- responsive to a concurrent ``close()``, and with no ceiling on how
    long a legitimately quiet console may take to produce the next frame.

    Also a context manager, so ``with mixer.meters.stream() as frames:``
    unwinds the underlying generator deterministically instead of leaving it
    to the garbage collector.
    """

    def __init__(self, agen: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
        self._agen: Any | None = agen
        self._run = run

    def __iter__(self) -> _SyncIterator:
        return self

    def __next__(self) -> Any:
        if self._agen is None:
            raise StopIteration
        try:
            return _wrap_value(self._run(_anext(self._agen)), self._run)
        except StopAsyncIteration:
            self._agen = None
            raise StopIteration from None

    def __enter__(self) -> _SyncIterator:  # noqa: PYI034 -- typing.Self needs py311+
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Unwind the underlying async generator. Idempotent."""
        agen, self._agen = self._agen, None
        if agen is None:
            return
        try:
            self._run(_aclose(agen))
        except RuntimeError:
            # The mixer was closed (or this is a reentrant call from its own
            # loop thread) -- either way the generator cannot be unwound from
            # here, and AsyncQuMixer.close() has already torn down the meter
            # pump that was feeding it. Log rather than raise out of a
            # cleanup path.
            log.debug("could not unwind an async generator; the mixer is gone", exc_info=True)


class _SyncContextManager:
    """A blocking ``with`` wrapper around one async context manager.

    ``mixer.meters.enabled()`` is the case that matters: enter and exit are
    just ``enable()``/``disable()``, both already safe to run on the mixer's
    loop thread, so this is pure marshalling -- the nesting count, the
    exception path and the reconnect behaviour are all the async
    implementation's, unchanged.
    """

    def __init__(self, cm: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
        self._cm = cm
        self._run = run

    def __enter__(self) -> Any:
        return _wrap_value(self._run(self._cm.__aenter__()), self._run)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return bool(self._run(self._cm.__aexit__(exc_type, exc, tb)))


class _FacadeProxy:
    """Wraps one object-facade instance (ParamProxy, BaseStrip, Send, ...) or
    sub-API instance (MeterController, StripApi) so every method on it blocks,
    and every value it hands back is wrapped the same way, recursively. See
    ``_wrap_value``'s docstring for the full rule.
    """

    def __init__(self, target: Any, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_run", run)

    def __getattr__(self, name: str) -> Any:
        return _wrap_value(getattr(self._target, name), self._run)

    def __setattr__(self, name: str, value: Any) -> None:
        # e.g. `mixer.input(6).fader.db = -6.0` -- ParamProxy._blocking
        # already does the actual thread-marshalling for the setter; this
        # only has to forward the assignment to the real object instead of
        # swallowing it into this wrapper's own __dict__.
        setattr(self._target, name, value)

    def __dir__(self) -> list[str]:
        """Report the wrapped object's surface, not this wrapper's.

        Makes the proxy honest to ``dir()``/tab-completion, and is what lets
        the structural drift guard in tests/test_sync_facade.py walk *into* a
        proxied sub-API rather than stopping at an opaque wrapper.
        """
        return dir(self._target)

    def __repr__(self) -> str:
        return repr(self._target)


def _proxy_property(name: str) -> property:
    def getter(self: QuMixer) -> Any:
        return _wrap_value(getattr(self._async, name), self._run)

    getter.__doc__ = getattr(AsyncQuMixer, name).__doc__
    return property(getter)


def _proxy_async_method(name: str) -> Callable[..., Any]:
    def method(self: QuMixer, *args: Any, **kwargs: Any) -> Any:
        return _wrap_value(self._run(getattr(self._async, name)(*args, **kwargs)), self._run)

    method.__name__ = name
    method.__doc__ = getattr(AsyncQuMixer, name).__doc__
    return method


def _proxy_sync_method(name: str) -> Callable[..., Any]:
    def method(self: QuMixer, *args: Any, **kwargs: Any) -> Any:
        return _wrap_value(getattr(self._async, name)(*args, **kwargs), self._run)

    method.__name__ = name
    method.__doc__ = getattr(AsyncQuMixer, name).__doc__
    return method


def _install_generated_proxies() -> None:
    for name in dir(AsyncQuMixer):
        if name.startswith("_") or name in vars(QuMixer):
            continue
        member = getattr(AsyncQuMixer, name)
        if isinstance(member, property):
            setattr(QuMixer, name, _proxy_property(name))
        elif inspect.iscoroutinefunction(member):
            setattr(QuMixer, name, _proxy_async_method(name))
        elif callable(member):
            setattr(QuMixer, name, _proxy_sync_method(name))


_install_generated_proxies()
