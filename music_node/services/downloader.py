import yt_dlp
import logging
import sqlite3
import concurrent.futures
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from config.config import (
    ROOT_OUTPUT_DIR,
    COOKIES_FILE,
    DB_FILE,
    MUSIC_DOWNLOAD_WORKERS,
    MUSIC_DOWNLOAD_WORKERS_EPHEMERAL,
    get_logger,
)

logger = get_logger("Downloader")

class DownloadHistoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                video_id TEXT,
                playlist_name TEXT,
                title TEXT,
                PRIMARY KEY (video_id, playlist_name)
            )
        """)
        self.conn.commit()

    def exists(self, video_id: str, playlist_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM downloads WHERE video_id=? AND playlist_name=?", 
            (video_id, playlist_name)
        ).fetchone()
        return row is not None

    def add(self, video_id: str, playlist_name: str, title: str):
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO downloads (video_id, playlist_name, title) VALUES (?, ?, ?)", 
                (video_id, playlist_name, title)
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"DB Error: {e}")

    def close(self):
        self.conn.close()

class MusicDownloader:
    def __init__(self, ephemeral: bool = False, outdir: Optional[Path] = None):
        """
        :param ephemeral: If True, no DB writes and no m3u generation (temp/browser mode).
        :param outdir:    Custom output directory. When set, overrides ROOT_OUTPUT_DIR and
                          the ephemeral temp path. DB and m3u behaviour still follows ephemeral.
        """
        self.ephemeral = ephemeral
        self.outdir = outdir
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}

        # Only initialize DB if we are preserving history
        if not self.ephemeral:
            self.db = DownloadHistoryDB()
        else:
            self.db = None

    def _get_output_path(self, folder_name: str) -> Path:
        if self.outdir:
            # outdir is the exact destination — don't append folder_name as a subfolder
            path = self.outdir
        elif self.ephemeral:
            # Browser / temp mode
            path = Path(tempfile.gettempdir()) / "music_browser_mode" / folder_name
        else:
            # Persistent library mode
            path = ROOT_OUTPUT_DIR / folder_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _generate_m3u(self, folder_name: str):
        """ 
        [NEW] Automatically generates/updates the .m3u playlist file 
        for Navidrome compatibility.
        """
        # 1. Skip if in Browser Mode (We don't need playlists for temp files)
        if self.ephemeral:
            return
        
        folder_path = self._get_output_path(folder_name)
        
        # Safety check: ensure folder exists
        if not folder_path.exists():
            return
        
        # 2. Define the playlist file path (e.g. "My Playlist.m3u")
        m3u_path = folder_path / f"{folder_name}.m3u"
        
        # 3. Scan for audio files (Just like your script did)
        audio_files = [
            f.name for f in folder_path.glob("*") 
            if f.suffix.lower() in ['.m4a', '.webm', '.mp3', '.opus']
        ]
        
        if not audio_files:
            return

        try:
            # 4. Write the file
            with open(m3u_path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for file in audio_files:
                    f.write(file + "\n")
            
            logger.info(f"📝 Playlist Updated: {folder_name}.m3u ({len(audio_files)} tracks)")
            
        except Exception as e:
            logger.error(f"❌ Failed to generate m3u for {folder_name}: {e}")

    def _download_task(self, url: str, folder_name: str) -> Optional[Path]:
        output_dir = self._get_output_path(folder_name)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        output_tmpl = str(output_dir / "%(title)s [%(id)s].%(ext)s")

        # [FIX] Force yt-dlp cache to live in config folder (or disable it)
        # We assume config is 2 levels up from services: services -> root -> config
        # Or simpler: just let it be None if we want to disable persistence
        
        opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_tmpl,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'cookiefile': str(COOKIES_FILE),
            'writethumbnail': True,
            'addmetadata': True,
            'postprocessors': [{'key': 'FFmpegMetadata'}, {'key': 'EmbedThumbnail'}],
            'overwrites': self.ephemeral, 
            # Optional: Redirect yt-dlp cache to prevent root folder creation
            # 'cachedir': False, 
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info: return None

                vid_id = info['id']
                title = info.get('title', vid_id)

                if not self.ephemeral and self.db.exists(vid_id, folder_name):
                    self.stats["skipped"] += 1
                    logger.info(f"⏭️  Skipped (Exists): {title}")
                    return None

                ydl.download([url])
                
                downloaded_file = next(output_dir.glob(f"*{vid_id}*"), None)

                self.stats["downloaded"] += 1
                logger.info(f"✅ Downloaded: {title}")
                
                if not self.ephemeral:
                    self.db.add(vid_id, folder_name, title)
                
                return downloaded_file

        except Exception as e:
            logger.error(f"❌ Failed {url}: {e}")
            self.stats["failed"] += 1
            return None

    def download_all(self, url_map: Dict[str, List[str]]) -> Dict[str, any]:
        tasks = [(u, p) for p, urls in url_map.items() for u in urls]
        logger.info(f"💾 Processing {len(tasks)} items (Ephemeral: {self.ephemeral})...")
        
        results = []
        workers = MUSIC_DOWNLOAD_WORKERS_EPHEMERAL if self.ephemeral else MUSIC_DOWNLOAD_WORKERS
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            future_map = {ex.submit(self._download_task, u, p): u for u, p in tasks}
            for future in concurrent.futures.as_completed(future_map):
                file_path = future.result()
                if file_path:
                    results.append(str(file_path))
            
        if not self.ephemeral:
            self.db.close()
            # [NEW] Trigger Playlist Generation for every folder we touched
            for folder_name in url_map.keys():
                self._generate_m3u(folder_name)
            
        return {
            "stats": self.stats,
            "files": results
        }
