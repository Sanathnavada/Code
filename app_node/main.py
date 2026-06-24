"""
Unified FastAPI gateway for node services.
"""
import asyncio
import logging
import socket
import subprocess
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from .artifacts import cleanup_expired_artifacts, list_artifacts, resolve_artifact_path
from .routers import media, music, telegram, ui
from .settings import NAVIDROME_DIR, NAVIDROME_ENABLED, NAVIDROME_EXE
from .tasks import all_tasks, cancel_task, get_task, shutdown_tasks, startup_tasks

logger = logging.getLogger(__name__)

_navidrome_proc = None
_rate_limit_window_seconds = 60
_rate_limit_max_requests = 10
_rate_limit_buckets: dict[str, deque[float]] = defaultdict(deque)
_job_submission_prefixes = (
    "/api/media/",
    "/api/music/",
    "/api/telegram/start",
)
_rate_limit_exempt_prefixes = (
    "/api/tasks",
    "/api/music/user/auth/callback",
    "/api/music/user/auth/",
    "/callback",
    "/health",
    "/docs",
    "/openapi.json",
)


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _navidrome_proc
    await startup_tasks()

    if not NAVIDROME_ENABLED:
        logger.info("Navidrome disabled by NAVIDROME_ENABLED=false.")
    elif _is_port_open("127.0.0.1", 4533):
        logger.info("Navidrome already available on http://localhost:4533 - reusing it.")
    elif NAVIDROME_EXE.exists():
        if sys.platform == "win32":
            _navidrome_proc = subprocess.Popen(
                [str(NAVIDROME_EXE)],
                cwd=str(NAVIDROME_DIR),
            )
        else:
            _navidrome_proc = await asyncio.create_subprocess_exec(
                str(NAVIDROME_EXE),
                cwd=str(NAVIDROME_DIR),
            )
        logger.info(f"Navidrome started (pid {_navidrome_proc.pid}) on http://localhost:4533")
    else:
        logger.warning(f"Navidrome not found at {NAVIDROME_EXE} - skipping.")

    yield

    await shutdown_tasks()

    if _navidrome_proc:
        running = _navidrome_proc.poll() is None if hasattr(_navidrome_proc, "poll") else _navidrome_proc.returncode is None
        if running:
            _navidrome_proc.terminate()
            if sys.platform == "win32":
                _navidrome_proc.wait()
            else:
                await _navidrome_proc.wait()
            logger.info("Navidrome stopped.")


app = FastAPI(title="Node Services API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app_node" / "static")), name="static")


@app.middleware("http")
async def rate_limit_submissions(request: Request, call_next):
    cleanup_expired_artifacts()

    path = request.url.path
    if path.startswith(_rate_limit_exempt_prefixes):
        return await call_next(request)

    is_submission = request.method in {"POST", "GET"} and path.startswith(_job_submission_prefixes)
    if is_submission:
        client_id = request.client.host if request.client else "unknown"
        now = time.time()
        bucket = _rate_limit_buckets[client_id]
        while bucket and now - bucket[0] > _rate_limit_window_seconds:
            bucket.popleft()
        if len(bucket) >= _rate_limit_max_requests:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded for job submission endpoints."},
            )
        bucket.append(now)

    return await call_next(request)


app.include_router(telegram.router, prefix="/api")
app.include_router(music.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(ui.router)


@app.get("/api/tasks", tags=["Tasks"])
async def list_tasks():
    return [
        {
            "id": task.id,
            "service": task.service,
            "status": task.status,
            "submitted_at": task.submitted_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "queue_position": task.queue_position,
            "error": task.error,
        }
        for task in all_tasks()
    ]


@app.get("/api/tasks/{task_id}", tags=["Tasks"])
async def get_task_detail(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found.")

    payload = {
        "id": task.id,
        "service": task.service,
        "status": task.status,
        "submitted_at": task.submitted_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "queue_position": task.queue_position,
        "error": task.error,
        "result": task.result,
        "artifacts": task.artifacts,
    }

    if task.status in {"queued", "waiting_for_user", "running"}:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)


@app.get("/api/tasks/{task_id}/artifacts", tags=["Tasks"])
async def get_task_artifacts(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found.")
    return {"task_id": task_id, "artifacts": list_artifacts(task_id)}


@app.get("/api/tasks/{task_id}/artifacts/{artifact_id:path}", tags=["Tasks"])
async def download_task_artifact(task_id: str, artifact_id: str):
    path = resolve_artifact_path(task_id, artifact_id)
    if not path:
        raise HTTPException(404, "Artifact not found.")
    return FileResponse(path, filename=path.name)


@app.get("/callback", tags=["Music"])
async def spotify_root_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    return await music.handle_spotify_auth_callback(
        state=state,
        code=code,
        error=error,
        error_description=error_description,
    )


@app.delete("/api/tasks/{task_id}", tags=["Tasks"])
async def kill_task(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, f"Task '{task_id}' not found.")
    if not await cancel_task(task_id):
        raise HTTPException(400, "Task is not queued or running.")
    return {"status": "cancelled", "task_id": task_id}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("app_node.main:app", host="0.0.0.0", port=8000, reload=True)
