import os
from pathlib import Path


def _load_dotenv() -> None:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env_int_or_auto(name: str, default: str = "auto") -> str:
    value = (os.getenv(name) or default).strip().lower()
    if value == "auto":
        return value
    try:
        return str(max(int(value), 1))
    except ValueError:
        return default


def _cpu_count() -> int:
    return max(os.cpu_count() or 1, 1)


def _total_ram_gb() -> float | None:
    try:
        import psutil

        return psutil.virtual_memory().total / 1024**3
    except Exception:
        return None


def _auto_jobs(cpu_count: int, ram_gb: float | None) -> int:
    if cpu_count < 4 or (ram_gb is not None and ram_gb < 8):
        return 1
    if cpu_count < 8 or (ram_gb is not None and ram_gb < 16):
        return 2
    return min(4, cpu_count // 4)


def _auto_music_download_workers(cpu_count: int) -> int:
    return min(4, max(2, cpu_count // 2))


def _auto_youtube_workers(cpu_count: int) -> int:
    return min(8, max(2, cpu_count))


def _auto_spotify_workers(cpu_count: int) -> int:
    return min(4, max(2, cpu_count // 2))


def resolve_worker_count(name: str, auto_value: int) -> int:
    value = _env_int_or_auto(name)
    if value == "auto":
        return max(auto_value, 1)
    return int(value)


CPU_COUNT = _cpu_count()
TOTAL_RAM_GB = _total_ram_gb()

MAX_CONCURRENT_JOBS = resolve_worker_count(
    "MAX_CONCURRENT_JOBS",
    _auto_jobs(CPU_COUNT, TOTAL_RAM_GB),
)
MUSIC_DOWNLOAD_WORKERS = resolve_worker_count(
    "MUSIC_DOWNLOAD_WORKERS",
    _auto_music_download_workers(CPU_COUNT),
)
MUSIC_DOWNLOAD_WORKERS_EPHEMERAL = resolve_worker_count(
    "MUSIC_DOWNLOAD_WORKERS_EPHEMERAL",
    min(2, MUSIC_DOWNLOAD_WORKERS),
)
YOUTUBE_RESOLVE_WORKERS = resolve_worker_count(
    "YOUTUBE_RESOLVE_WORKERS",
    _auto_youtube_workers(CPU_COUNT),
)
SPOTIFY_FETCH_WORKERS = resolve_worker_count(
    "SPOTIFY_FETCH_WORKERS",
    _auto_spotify_workers(CPU_COUNT),
)
