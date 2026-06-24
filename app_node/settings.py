"""
Central configuration — resolves absolute paths to every node service.
All paths are derived from this file's location so the server works
regardless of where it is invoked from.
"""
import os
import sys
from pathlib import Path

# Root of the whole monorepo  (…/code/)
ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = (os.getenv(name) or default).strip().lower()
    return value if value in choices else default


_load_dotenv(ROOT_DIR / ".env")

# ── Node service working directories ────────────────────────────────────────
INODE_DIR      = ROOT_DIR / "i_node"
MUSIC_NODE_DIR = ROOT_DIR / "music_node"
MEDIA_NODE_DIR = ROOT_DIR / "media_node"
NAVIDROME_DIR  = ROOT_DIR / "music_node" / "server"
NAVIDROME_EXE  = NAVIDROME_DIR / "Navidrome.exe"

# ── Default output directories ───────────────────────────────────────────────
DEFAULT_MUSIC_OUTDIR = str(ROOT_DIR / "downloads" / "music")
DEFAULT_MEDIA_OUTDIR = str(ROOT_DIR / "downloads" / "media")

# ── Python interpreter (same one that's running this server) ─────────────────
PYTHON = sys.executable

# ── API settings ─────────────────────────────────────────────────────────────
API_HOST = "0.0.0.0"
API_PORT = 8000

TELEGRAM_NODE_ENABLED = _env_bool("TELEGRAM_NODE_ENABLED", True)
MEDIA_NODE_ENABLED = _env_bool("MEDIA_NODE_ENABLED", True)
MUSIC_NODE_ENABLED = _env_bool("MUSIC_NODE_ENABLED", True)
NAVIDROME_ENABLED = _env_bool("NAVIDROME_ENABLED", True)
SCRAPING_PATH = _env_choice("SCRAPING_PATH", "playwright", {"instaloader", "ytdlp", "playwright"})
