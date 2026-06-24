import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "http://localhost:8888/callback")

# --- Paths ---
from pathlib import Path

# config.py lives in: <project_root>/config
BASE_DIR = Path(__file__).parent            
PROJECT_ROOT = BASE_DIR.parent              

PREFERRED_OUTPUT = Path(r"D:\Music")
ROOT_OUTPUT_DIR = PREFERRED_OUTPUT if PREFERRED_OUTPUT.exists() else PROJECT_ROOT / "Music"

CACHE_FILE = BASE_DIR / "youtube_cache.json"
DB_FILE = PROJECT_ROOT / "download_history.db"

COOKIES_FILE = BASE_DIR / "cookies.txt"
SPOTIFY_CACHE_PATH = BASE_DIR / ".cache" 

# [FIX] Ensure this is consistent
SPOTIFY_BACKUP_FILE = BASE_DIR / "spotify_playlists_backup.txt"
# --- THE FIX IS HERE ---
# We removed 'CACHE_DIR' because we don't need a folder.
# We only ensure the Output Directory exists.
if not ROOT_OUTPUT_DIR.exists():
    ROOT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# --- Tuning ---
def _env_int_or_auto(name: str, auto_value: int) -> int:
    value = (os.getenv(name) or "auto").strip().lower()
    if value == "auto":
        return max(auto_value, 1)
    try:
        return max(int(value), 1)
    except ValueError:
        return max(auto_value, 1)


CPU_COUNT = max(os.cpu_count() or 1, 1)
YOUTUBE_RESOLVE_WORKERS = _env_int_or_auto("YOUTUBE_RESOLVE_WORKERS", min(8, max(2, CPU_COUNT)))
SPOTIFY_FETCH_WORKERS = _env_int_or_auto("SPOTIFY_FETCH_WORKERS", min(4, max(2, CPU_COUNT // 2)))
MUSIC_DOWNLOAD_WORKERS = _env_int_or_auto("MUSIC_DOWNLOAD_WORKERS", min(4, max(2, CPU_COUNT // 2)))
MUSIC_DOWNLOAD_WORKERS_EPHEMERAL = _env_int_or_auto(
    "MUSIC_DOWNLOAD_WORKERS_EPHEMERAL",
    min(2, MUSIC_DOWNLOAD_WORKERS),
)
MAX_WORKERS = YOUTUBE_RESOLVE_WORKERS
YT_SEARCH_LIMIT = 10
MIN_CONFIDENCE_SCORE = 40

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

def get_logger(name):
    return logging.getLogger(name)
