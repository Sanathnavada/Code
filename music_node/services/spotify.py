import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Dict, List

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from config.config import (
    SPOTIFY_FETCH_WORKERS,
    SPOTIFY_CACHE_PATH,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    get_logger,
)
from models import Track

logger = get_logger("SpotifyService")
SPOTIFY_REQUEST_TIMEOUT = 20
SPOTIFY_STATUS_FORCELIST = (429, 500, 502, 503, 504)


class SpotifyProvider:
    def __init__(self, use_user_auth: bool = True, allow_interactive: bool = True):
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            raise ValueError("Spotify credentials missing in .env")

        handler = CacheFileHandler(cache_path=str(SPOTIFY_CACHE_PATH))
        self.client_auth_manager = SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            cache_handler=handler,
        )

        if use_user_auth:
            logger.info("Initializing User Auth (Private Data)")
            self.user_auth_manager = SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri=SPOTIFY_REDIRECT_URI,
                scope="playlist-read-private playlist-read-collaborative",
                open_browser=False,
                cache_handler=handler,
            )

            if not allow_interactive:
                token_info = self.user_auth_manager.cache_handler.get_cached_token()
                if not token_info or not self.user_auth_manager.validate_token(token_info):
                    raise PermissionError("Spotify user authorization is required.")

            self.sp = self._spotify_client(self.user_auth_manager)
            try:
                self.user_id = self.sp.current_user()["id"]
            except Exception:
                if allow_interactive:
                    logger.warning("User auth failed, falling back to client auth.")
                    self.sp = self._spotify_client(self.client_auth_manager)
                else:
                    raise PermissionError("Spotify user authorization is required.")
        else:
            logger.info("Initializing Client Auth (Public Data only)")
            self.sp = self._spotify_client(self.client_auth_manager)

        self.last_library_errors: dict[str, str] = {}

    @staticmethod
    def _spotify_client(auth_manager):
        return spotipy.Spotify(
            auth_manager=auth_manager,
            requests_timeout=SPOTIFY_REQUEST_TIMEOUT,
            retries=4,
            status_retries=4,
            backoff_factor=0.6,
            status_forcelist=SPOTIFY_STATUS_FORCELIST,
        )

    @staticmethod
    def _image_url(images) -> str:
        if not images:
            return ""
        return images[-1].get("url") or images[0].get("url") or ""

    def _get_tracks_from_id(self, playlist_id: str) -> List[Track]:
        return self._get_tracks_from_id_with_client(self.sp, playlist_id)

    def _get_tracks_from_id_with_client(self, client, playlist_id: str) -> List[Track]:
        tracks = []
        offset = 0
        while True:
            res = client.playlist_items(
                playlist_id,
                offset=offset,
                limit=100,
                fields="items(track(name,artists(name),album(name,images))),next",
            )
            if not res.get("items"):
                break

            for item in res["items"]:
                track = item.get("track")
                if track:
                    artist = track["artists"][0]["name"] if track["artists"] else "Unknown"
                    album = track.get("album") or {}
                    tracks.append(
                        Track(
                            title=track["name"],
                            artist=artist,
                            album=album.get("name", ""),
                            image_url=self._image_url(album.get("images")),
                        )
                    )

            if not res.get("next"):
                break
            offset += len(res["items"])
        return tracks

    def _get_tracks_from_id_for_worker(self, playlist_id: str) -> List[Track]:
        client = self._spotify_client(self.user_auth_manager)
        return self._get_tracks_from_id_with_client(client, playlist_id)

    def _get_tracks_from_album(self, album_id: str, album_name: str) -> List[Track]:
        tracks = []
        offset = 0
        while True:
            res = self.sp.album_tracks(album_id, limit=50, offset=offset)
            if not res.get("items"):
                break

            for track in res["items"]:
                if track:
                    artist = track["artists"][0]["name"] if track["artists"] else "Unknown"
                    tracks.append(Track(title=track["name"], artist=artist, album=album_name))

            if not res.get("next"):
                break
            offset += len(res["items"])
        return tracks

    def _get_track_from_id(self, track_id: str) -> Track:
        track = self.sp.track(track_id)
        artist = track["artists"][0]["name"] if track.get("artists") else "Unknown"
        album = track.get("album") or {}
        return Track(
            title=track["name"],
            artist=artist,
            album=album.get("name", ""),
            image_url=self._image_url(album.get("images")),
        )

    def _fetch_playlist_batch(
        self,
        playlist_refs: list[tuple[str, str]],
        worker_count: int,
        *,
        retry_pass: bool = False,
    ) -> tuple[dict[str, list[Track]], dict[str, str]]:
        playlists = {}
        errors = {}
        if not playlist_refs:
            return playlists, errors

        if retry_pass:
            time.sleep(1.5)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_ref = {
                executor.submit(self._get_tracks_from_id_for_worker, playlist_id): (name, playlist_id)
                for name, playlist_id in playlist_refs
            }
            for future in as_completed(future_to_ref):
                name, _playlist_id = future_to_ref[future]
                try:
                    playlists[name] = future.result()
                except Exception as exc:
                    errors[name] = str(exc)
                    level = "retry" if retry_pass else "initial"
                    logger.warning(f"Spotify {level} fetch failed for playlist '{name}': {exc}")
        return playlists, errors

    def fetch_user_library(self) -> Dict[str, List[Track]]:
        if not hasattr(self, "user_auth_manager"):
            raise PermissionError("Cannot fetch user library in public mode.")

        logger.info(f"Fetching library for user: {self.user_id}")
        playlist_refs = []
        seen_names = set()
        offset = 0
        while True:
            res = self.sp.current_user_playlists(limit=50, offset=offset)
            if not res["items"]:
                break
            for playlist in res["items"]:
                name = playlist["name"]
                display_name = name
                duplicate_count = 2
                while display_name in seen_names:
                    display_name = f"{name} ({duplicate_count})"
                    duplicate_count += 1
                seen_names.add(display_name)
                playlist_refs.append((display_name, playlist["id"]))
            offset += len(res["items"])

        playlists = {}
        if not playlist_refs:
            return playlists

        worker_count = min(max(SPOTIFY_FETCH_WORKERS, 1), len(playlist_refs))
        logger.info(f"Fetching tracks for {len(playlist_refs)} playlists with {worker_count} workers")
        playlists, errors = self._fetch_playlist_batch(playlist_refs, worker_count)

        if errors:
            failed_refs = [(name, playlist_id) for name, playlist_id in playlist_refs if name in errors]
            retry_workers = min(2, len(failed_refs))
            logger.info(f"Retrying {len(failed_refs)} failed playlist(s) with {retry_workers} worker(s)")
            retried_playlists, retry_errors = self._fetch_playlist_batch(
                failed_refs,
                retry_workers,
                retry_pass=True,
            )
            playlists.update(retried_playlists)
            self.last_library_errors = retry_errors

        if not playlists and self.last_library_errors:
            raise RuntimeError(
                "Spotify timed out while fetching playlist tracks. "
                "Retry after a minute or reduce concurrent playlist fetching."
            )

        return {name: playlists[name] for name, _ in playlist_refs if name in playlists}

    def fetch_by_url(self, url: str) -> Dict[str, List[Track]]:
        track_match = re.search(r"track/([a-zA-Z0-9]+)", url)
        if track_match:
            track_id = track_match.group(1)
            try:
                track = self._get_track_from_id(track_id)
            except spotipy.exceptions.SpotifyException as exc:
                raise ValueError("Track not found.") from exc
            logger.info(f"Detected Track: {track.title} - {track.artist}")
            return {"Single Track": [track]}

        playlist_match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        if playlist_match:
            playlist_id = playlist_match.group(1)
            try:
                name = self.sp.playlist(playlist_id, fields="name")["name"]
            except spotipy.exceptions.SpotifyException as exc:
                raise PermissionError("Playlist not found.") from exc
            logger.info(f"Detected Playlist: {name}")
            return {name: self._get_tracks_from_id(playlist_id)}

        album_match = re.search(r"album/([a-zA-Z0-9]+)", url)
        if album_match:
            album_id = album_match.group(1)
            try:
                album_data = self.sp.album(album_id)
                name = album_data["name"]
            except spotipy.exceptions.SpotifyException as exc:
                raise ValueError("Album not found.") from exc
            logger.info(f"Detected Album: {name}")
            return {name: self._get_tracks_from_album(album_id, name)}

        raise ValueError("Invalid Spotify URL.")
