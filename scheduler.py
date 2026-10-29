"""Bounded single-event-loop admission and task ownership, without platform I/O."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any

MESSAGE_RETENTION_SECONDS = 300
HISTORY_LIMIT = 2048
UserKey = tuple[str, str]
MessageKey = tuple[str, str, str, str]


class JobPhase(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DELIVERING = "delivering"
    CLEANING = "cleaning"
    DONE = "done"


class JobResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    DELIVERY_UNKNOWN = "delivery_unknown"


class CleanupState(str, Enum):
    NOT_CREATED = "not_created"
    RETAINED = "retained"
    DELETED = "deleted"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_UNKNOWN = "cleanup_unknown"


class ManagerState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    PAUSED = "paused"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class SourceRoute:
    platform_id: str
    message_type: str
    target_id: str
    send_handle: Any = field(default=None, repr=False, compare=False)


@dataclass
class Job:
    task_id: str
    dedup_key: MessageKey
    user_key: UserKey
    prompt: str
    route: SourceRoute
    config: Mapping[str, Any] = field(default_factory=dict, repr=False)
    phase: JobPhase = JobPhase.QUEUED
    result: JobResult | None = None
    cleanup: CleanupState = CleanupState.NOT_CREATED
    admitted_at: float | None = None
    queue_deadline: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    reason: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class Admission:
    accepted: bool
    task_id: str | None = None
    queue_position: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class QueueStatus:
    state: ManagerState
    running: int
    queued: int
    pause_reason: str | None


@dataclass(frozen=True)
class JobOutcome:
    task_id: str
    result: JobResult
    cleanup: CleanupState
    admitted_at: float
    started_at: float | None
    finished_at: float
    reason: str | None = None
    error_type: str | None = None
    phase: JobPhase = JobPhase.DONE


def _freeze_config(value: Any) -> Any:
    """Copy JSON-like configuration into immutable containers.

    Args:
        value: Configuration value; runtime handles belong in SourceRoute.

    Returns:
        Detached mappings and tuples with immutable scalar leaves.

    Raises:
        ValueError: Unsupported objects, non-string keys or non-finite numbers.
    """
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Configuration keys must be strings")
        return MappingProxyType(
            {key: _freeze_config(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Configuration values must be finite JSON-like data")


class TaskManager:
    """Reserve capacity atomically and retain ownership through runner cleanup.

    Args:
        concurrency: Positive execution limit, shared by all sources.
        capacity: Nonnegative waiting capacity; zero disables waiting.
        queue_timeout_s: Finite positive monotonic waiting budget.
        runner: Coroutine covering execution, delivery and bounded cleanup.

    Raises:
        ValueError: Invalid constructor arguments.
    """

    def __init__(
        self,
        concurrency: int = 1,
        capacity: int = 5,
        queue_timeout_s: float = 600,
        runner: Callable[[Job], Awaitable[None]] | None = None,
        max_pending_per_user: int = 3,
    ) -> None:
        if type(concurrency) is not int or concurrency <= 0:
            raise ValueError("Concurrency must be a positive integer")
        if type(capacity) is not int or capacity < 0:
            raise ValueError("Capacity must be a nonnegative integer")
        if (
            type(queue_timeout_s) not in (int, float)
            or not math.isfinite(queue_timeout_s)
            or queue_timeout_s <= 0
        ):
            raise ValueError("Queue timeout must be finite and positive")
        if type(max_pending_per_user) is not int or max_pending_per_user < 1:
            raise ValueError("Per-user limit must be a positive integer")
        if not callable(runner):
            raise ValueError("An async runner is required")
        self._max_pending_per_user = max_pending_per_user
        self._concurrency = concurrency
        self._capacity = capacity
        self._queue_timeout_s = float(queue_timeout_s)
        self._runner = runner
        self._state = ManagerState.NEW
        self._pause_reason: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queued: OrderedDict[str, Job] = OrderedDict()
        self._active: dict[str, tuple[Job, asyncio.Task[None]]] = {}
        self._entered: set[str] = set()
        self._users: dict[UserKey, set[str]] = {}
        self._messages: dict[MessageKey, str] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._completion: dict[str, asyncio.Future[JobOutcome]] = {}
        self._seen: OrderedDict[MessageKey, tuple[float, str]] = OrderedDict()
        self._finished: OrderedDict[str, tuple[float, JobOutcome]] = OrderedDict()
        self._closing_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Open admission once; closed managers cannot be restarted."""
        if self._state == ManagerState.NEW:
            self._loop = asyncio.get_running_loop()
            self._state = ManagerState.RUNNING

    async def submit(self, job: Job) -> Admission:
        """Validate, snapshot and reserve a task without yielding mid-admission.

        Args:
            job: Request with unique task id and platform-scoped identity keys.

        Returns:
            Admission with initial FIFO position (0 means executing), or a
            stable rejection code. Rejections do not reserve user/message keys.

        Raises:
            ValueError: Malformed identity, route, prompt or configuration.
        """
        if self._state != ManagerState.RUNNING:
            reason = {
                ManagerState.NEW: "not_started",
                ManagerState.PAUSED: "paused",
            }.get(self._state, "closed")
            return Admission(False, reason=reason)
        now = self._loop.time()
        self._prune(now)
        for task_id in list(self._queued):
            self._expire(task_id)
        if (
            not isinstance(job, Job)
            or not isinstance(job.task_id, str)
            or not job.task_id.strip()
            or not isinstance(job.dedup_key, tuple)
            or len(job.dedup_key) != 4
            or not isinstance(job.user_key, tuple)
            or len(job.user_key) != 2
            or any(
                not isinstance(item, str) or not item.strip()
                for item in (*job.dedup_key, *job.user_key)
            )
            or job.user_key[0] != job.dedup_key[0]
            or not isinstance(job.route, SourceRoute)
            or (job.route.platform_id, job.route.message_type, job.route.target_id)
            != job.dedup_key[:3]
            or not isinstance(job.prompt, str)
            or not job.prompt.strip()
            or not isinstance(job.config, Mapping)
        ):
            raise ValueError("Invalid job identity, route, prompt or configuration")
        existing = self._messages.get(job.dedup_key)
        if existing is None and job.dedup_key in self._seen:
            existing = self._seen[job.dedup_key][1]
        if existing is not None:
            return Admission(False, existing, reason="duplicate_message")
        if job.task_id in self._completion or job.task_id in self._finished:
            return Admission(False, job.task_id, reason="duplicate_task_id")
        if len(self._users.get(job.user_key, ())) >= self._max_pending_per_user:
            return Admission(False, reason="user_busy")
        if (
            len(self._active) >= self._concurrency
            and len(self._queued) >= self._capacity
        ):
            return Admission(False, reason="queue_full")
        owned = replace(
            job,
            config=_freeze_config(job.config),
            phase=JobPhase.QUEUED,
            result=None,
            cleanup=CleanupState.NOT_CREATED,
            admitted_at=now,
            queue_deadline=now + self._queue_timeout_s,
            started_at=None,
            finished_at=None,
            cancel_requested=False,
            reason=None,
            error_type=None,
        )
        self._completion[owned.task_id] = self._loop.create_future()
        self._users.setdefault(owned.user_key, set()).add(owned.task_id)
        self._messages[owned.dedup_key] = owned.task_id
        self._queued[owned.task_id] = owned
        self._timers[owned.task_id] = self._loop.call_at(
            owned.queue_deadline, self._expire, owned.task_id
        )
        position = 0 if len(self._active) < self._concurrency else len(self._queued)
        self._dispatch()
        return Admission(True, owned.task_id, position)

    async def wait(self, task_id: str) -> JobOutcome:
        """Wait for the terminal outcome without cancelling the underlying job.

        Args:
            task_id: Accepted task id.

        Returns:
            Immutable metadata without prompt, configuration or source content.

        Raises:
            KeyError: Unknown id or evicted completed history (300s/2048 items).
        """
        if self._loop is not None:
            self._prune(self._loop.time())
        if task_id in self._finished:
            return self._finished[task_id][1]
        return await asyncio.shield(self._completion[task_id])

    def pending(self, user_key: UserKey) -> tuple[tuple[str, JobPhase], ...]:
        """Return this identity's unfinished task IDs and phases."""
        ids = self._users.get(user_key, set())
        jobs = list(self._queued.values()) + [job for job, _ in self._active.values()]
        return tuple((job.task_id, job.phase) for job in jobs if job.task_id in ids)

    async def cancel(self, user_key: UserKey, task_id: str | None = None) -> bool:
        """Cancel one user's task and await active cleanup outside the runner.

        Args:
            user_key: Platform and sender id; shared across group/private chats.

        Returns:
            Whether an unfinished task was found. Cancellation cannot retract
            delivery already confirmed or marked unknown by the runner.
        """
        ids = self._users.get(user_key, set())
        if task_id is None:
            if len(ids) != 1:
                return False
            task_id = next(iter(ids))
        elif task_id not in ids:
            return False
        if task_id in self._queued:
            current = self._queued.pop(task_id)
            current.result, current.reason = JobResult.CANCELLED, "cancelled"
            self._finish(current)
            self._dispatch()
        else:
            current, task = self._active[task_id]
            self._request_cancel(current, task)
            if task is not asyncio.current_task():
                await asyncio.shield(task)
        return True

    async def pause(self, reason: str) -> None:
        """Stop admission and request safe exit, including calls from a runner.

        Args:
            reason: Fault category, without user content or credentials.

        Raises:
            ValueError: Empty or non-string reason.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Pause reason must be a nonempty fault category")
        if self._state in (ManagerState.CLOSING, ManagerState.CLOSED):
            return
        self._state, self._pause_reason = ManagerState.PAUSED, reason
        for current in list(self._queued.values()):
            self._queued.pop(current.task_id)
            current.result, current.reason = (
                JobResult.FAILED,
                "manual_pause" if reason == "manual" else "paused",
            )
            self._finish(current)
        for current, task in self._active.values():
            self._request_cancel(current, task)

    async def wait_until_idle(self) -> None:
        """Wait for active jobs to finish their own cleanup after pausing."""
        if self._state != ManagerState.PAUSED:
            raise RuntimeError("Waiting for idle requires a paused manager")
        tasks = tuple(task for _, task in self._active.values())
        if tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            )

    async def resume(self) -> None:
        """Reopen admission after prior tasks exit; browser READY is caller-owned.

        Raises:
            RuntimeError: Not paused, or old runners are still cleaning.
        """
        if self._state == ManagerState.RUNNING:
            return
        if self._state != ManagerState.PAUSED or self._active or self._loop is None:
            raise RuntimeError("Resume requires a started, paused, drained manager")
        self._state, self._pause_reason = ManagerState.RUNNING, None

    def snapshot(self) -> QueueStatus:
        """Return only aggregate state; running includes delivery and cleanup.

        Returns:
            A frozen state/count snapshot without any task content.
        """
        return QueueStatus(
            self._state, len(self._active), len(self._queued), self._pause_reason
        )

    async def close(self) -> None:
        """Cancel and join owned runners; concurrent callers share shutdown.

        Raises:
            RuntimeError: Called inside a runner, which must use pause instead.
        """
        if any(task is asyncio.current_task() for _, task in self._active.values()):
            raise RuntimeError("A runner must pause, not close its own manager")
        if self._closing_task is None:
            self._state = ManagerState.CLOSING
            for current in list(self._queued.values()):
                self._queued.pop(current.task_id)
                current.result, current.reason = JobResult.CANCELLED, "closed"
                self._finish(current)
            for current, task in self._active.values():
                self._request_cancel(current, task)
            self._closing_task = asyncio.create_task(self._drain_close())
        await asyncio.shield(self._closing_task)

    async def _drain_close(self) -> None:
        await asyncio.gather(
            *(task for _, task in self._active.values()), return_exceptions=True
        )
        self._state = ManagerState.CLOSED

    def _request_cancel(self, job: Job, task: asyncio.Task[None]) -> None:
        if job.cancel_requested:
            return
        job.cancel_requested = True
        # Never inject cancellation twice into a runner's finally cleanup.
        # A not-yet-entered wrapper must run so it can release reservations.
        if (
            job.task_id in self._entered
            and job.phase != JobPhase.CLEANING
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _dispatch(self) -> None:
        while (
            self._state == ManagerState.RUNNING
            and self._queued
            and len(self._active) < self._concurrency
        ):
            _, current = self._queued.popitem(last=False)
            if current.queue_deadline <= self._loop.time():
                current.result, current.reason = JobResult.TIMED_OUT, "queue_timeout"
                self._finish(current)
                continue
            self._timers.pop(current.task_id).cancel()
            current.phase, current.started_at = JobPhase.RUNNING, self._loop.time()
            self._active[current.task_id] = (
                current,
                asyncio.create_task(self._run(current)),
            )

    async def _run(self, job: Job) -> None:
        self._entered.add(job.task_id)
        try:
            if job.cancel_requested:
                raise asyncio.CancelledError()
            await self._runner(job)
            if job.result is None:
                job.result = (
                    JobResult.CANCELLED if job.cancel_requested else JobResult.SUCCESS
                )
        except asyncio.CancelledError:
            if job.result is None:
                job.result, job.reason = JobResult.CANCELLED, "cancelled"
        except Exception as exc:
            if job.result is None:
                job.result = (
                    JobResult.TIMED_OUT
                    if isinstance(exc, asyncio.TimeoutError)
                    else JobResult.FAILED
                )
                job.reason = (
                    "runner_timeout"
                    if job.result == JobResult.TIMED_OUT
                    else "runner_error"
                )
            job.error_type = type(exc).__name__
        finally:
            self._entered.discard(job.task_id)
            self._active.pop(job.task_id)
            self._finish(job)
            self._dispatch()

    def _expire(self, task_id: str) -> None:
        current = self._queued.get(task_id)
        if current is None:
            return
        if current.queue_deadline > self._loop.time():
            self._timers.pop(task_id).cancel()
            self._timers[task_id] = self._loop.call_at(
                current.queue_deadline, self._expire, task_id
            )
            return
        self._queued.pop(task_id)
        current.result, current.reason = JobResult.TIMED_OUT, "queue_timeout"
        self._finish(current)
        self._dispatch()

    def _finish(self, job: Job) -> None:
        timer = self._timers.pop(job.task_id, None)
        if timer is not None:
            timer.cancel()
        job.phase, job.finished_at = JobPhase.DONE, self._loop.time()
        ids = self._users[job.user_key]
        ids.remove(job.task_id)
        if not ids:
            del self._users[job.user_key]
        self._messages.pop(job.dedup_key)
        outcome = JobOutcome(
            job.task_id,
            job.result,
            job.cleanup,
            job.admitted_at,
            job.started_at,
            job.finished_at,
            job.reason,
            job.error_type,
        )
        expires = job.finished_at + MESSAGE_RETENTION_SECONDS
        self._seen[job.dedup_key] = (expires, job.task_id)
        self._finished[job.task_id] = (expires, outcome)
        self._prune(job.finished_at)
        self._completion.pop(job.task_id).set_result(outcome)

    def _prune(self, now: float) -> None:
        for cache in (self._seen, self._finished):
            while cache and (
                next(iter(cache.values()))[0] <= now or len(cache) > HISTORY_LIMIT
            ):
                cache.popitem(last=False)
