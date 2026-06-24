"""
Central task store and single-worker job orchestrator for app_node.

The app currently targets one-machine execution where only one heavy job
should run at a time, so an in-process FIFO queue is the simplest fit.
"""
import asyncio
import inspect
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Awaitable, Callable, Optional

from .runtime_capacity import MAX_CONCURRENT_JOBS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    service: str
    status: str = "queued"
    submitted_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Any = None
    submitted_by: Optional[str] = None
    queue_position: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _QueuedJob:
    task_id: str
    runner: Callable[[], Any]


def _exclusive_group_for_service(service: str) -> Optional[str]:
    if service in {"media.public_user", "media.private_user", "media.post", "media.ig_bulk"}:
        return "instagram"
    return None


class TaskManager:
    def __init__(self, max_concurrent_jobs: int = 1):
        self.max_concurrent_jobs = max_concurrent_jobs
        self._tasks: dict[str, Task] = {}
        self._queue: deque[_QueuedJob] = deque()
        self._queue_event = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None
        self._running_handles: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        for handle in list(self._running_handles.values()):
            handle.cancel()
        self._running_handles.clear()

    async def submit(self, service: str, runner: Callable[[], Any],
                     submitted_by: Optional[str] = None,
                     meta: Optional[dict[str, Any]] = None) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            service=service,
            submitted_by=submitted_by,
            meta=meta or {},
        )
        async with self._lock:
            self._tasks[task.id] = task
            self._queue.append(_QueuedJob(task_id=task.id, runner=runner))
            self._refresh_queue_positions_locked()
            self._queue_event.set()
        return task

    def get_task(self, task_id: Optional[str]) -> Optional[Task]:
        if not task_id:
            return None
        return self._tasks.get(task_id)

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    async def cancel_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            if task.status == "queued":
                self._queue = deque(job for job in self._queue if job.task_id != task_id)
                task.status = "cancelled"
                task.finished_at = _now()
                self._refresh_queue_positions_locked()
                return True

            if task.status != "running":
                return False

            handle = self._running_handles.get(task_id)
            if handle:
                handle.cancel()
            return True

    def attach_artifacts(self, task_id: str, artifacts: list[dict[str, Any]]) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.artifacts = artifacts

    async def _worker_loop(self) -> None:
        while True:
            await self._queue_event.wait()
            jobs = []
            async with self._lock:
                available_slots = max(self.max_concurrent_jobs - len(self._running_handles), 0)
                running_groups = {
                    group for group in (
                        _exclusive_group_for_service(self._tasks[task_id].service)
                        for task_id in self._running_handles
                        if task_id in self._tasks
                    )
                    if group
                }
                remaining = deque()
                while self._queue:
                    job = self._queue.popleft()
                    group = _exclusive_group_for_service(self._tasks[job.task_id].service)
                    if available_slots > 0 and (not group or group not in running_groups):
                        jobs.append(job)
                        available_slots -= 1
                        if group:
                            running_groups.add(group)
                    else:
                        remaining.append(job)
                self._queue = remaining
                if not self._queue:
                    self._queue_event.clear()
                self._refresh_queue_positions_locked()
            if not jobs:
                await asyncio.sleep(0.1)
                continue
            for job in jobs:
                handle = asyncio.create_task(self._run_job(job))
                self._running_handles[job.task_id] = handle

    async def _run_job(self, job: _QueuedJob) -> None:
        task = self._tasks[job.task_id]
        task.status = "running"
        task.started_at = _now()

        async def invoke_runner():
            if inspect.iscoroutinefunction(job.runner):
                return await job.runner()
            result = await asyncio.to_thread(job.runner)
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            task.result = await invoke_runner()
            if isinstance(task.result, dict) and isinstance(task.result.get("artifacts"), list):
                task.artifacts = task.result["artifacts"]
            task.status = "completed"
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "Task was cancelled."
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
        finally:
            self._running_handles.pop(task.id, None)
            task.finished_at = _now()

    def _refresh_queue_positions_locked(self) -> None:
        queued_ids = {job.task_id for job in self._queue}
        for task in self._tasks.values():
            if task.id in queued_ids:
                continue
            if task.status == "queued":
                task.queue_position = 0
        for index, job in enumerate(self._queue, start=1):
            task = self._tasks[job.task_id]
            task.queue_position = index


task_manager = TaskManager(max_concurrent_jobs=MAX_CONCURRENT_JOBS)


async def startup_tasks() -> None:
    await task_manager.startup()


async def shutdown_tasks() -> None:
    await task_manager.shutdown()


async def submit_sync_job(service: str, fn: Callable, *args,
                          submitted_by: Optional[str] = None,
                          meta: Optional[dict[str, Any]] = None) -> Task:
    return await task_manager.submit(
        service=service,
        runner=lambda: fn(*args),
        submitted_by=submitted_by,
        meta=meta,
    )


async def submit_bound_job(service: str, runner_factory: Callable[[Task], Any],
                           submitted_by: Optional[str] = None,
                           meta: Optional[dict[str, Any]] = None) -> Task:
    placeholder = Task(
        id=str(uuid.uuid4()),
        service=service,
        submitted_by=submitted_by,
        meta=meta or {},
    )

    async with task_manager._lock:
        task_manager._tasks[placeholder.id] = placeholder
        task_manager._queue.append(
            _QueuedJob(task_id=placeholder.id, runner=lambda: runner_factory(placeholder))
        )
        task_manager._refresh_queue_positions_locked()
        task_manager._queue_event.set()
    return placeholder


async def submit_async_job(service: str, coro_factory: Callable[[], Awaitable[Any]],
                           submitted_by: Optional[str] = None,
                           meta: Optional[dict[str, Any]] = None) -> Task:
    return await task_manager.submit(
        service=service,
        runner=coro_factory,
        submitted_by=submitted_by,
        meta=meta,
    )


def get_task(task_id: Optional[str]) -> Optional[Task]:
    return task_manager.get_task(task_id)


def all_tasks() -> list[Task]:
    return task_manager.all_tasks()


async def cancel_task(task_id: str) -> bool:
    return await task_manager.cancel_task(task_id)


def attach_artifacts(task_id: str, artifacts: list[dict[str, Any]]) -> None:
    task_manager.attach_artifacts(task_id, artifacts)


def append_task_event(task_id: str, message: str) -> None:
    task = task_manager.get_task(task_id)
    if not task:
        return
    events = task.meta.setdefault("events", [])
    events.append({"time": _event_time(), "message": message})
    del events[:-8]

    estimate_match = re.search(r"Estimated transcription time:\s*(?:(\d+)\s*min\s*)?(\d+)\s*sec", message)
    if estimate_match:
        minutes = int(estimate_match.group(1) or 0)
        seconds = int(estimate_match.group(2))
        estimated_seconds = minutes * 60 + seconds
        task.meta["estimated_transcription_seconds"] = estimated_seconds
        task.meta["poll_interval_seconds"] = max(5, min(20, estimated_seconds // 4 or 5))

    if "Transcribing audio" in message and task.meta.get("estimated_transcription_seconds"):
        task.meta["poll_interval_seconds"] = max(
            5,
            min(20, task.meta["estimated_transcription_seconds"] // 4 or 5),
        )


def get_queue_summary() -> dict[str, int | str]:
    tasks = task_manager.all_tasks()
    queued = sum(1 for task in tasks if task.status == "queued")
    running = sum(1 for task in tasks if task.status == "running")
    status = "idle" if queued == 0 and running == 0 else "busy"
    return {
        "queued_jobs": queued,
        "running_jobs": running,
        "worker_status": "available" if running == 0 else "occupied",
        "queue_status": status,
    }
