import logging
import random
import re
import time
import json
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import instaloader
except ImportError:
    instaloader = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


_SC_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
_VALID_SCRAPING_PATHS = {"instaloader", "ytdlp", "playwright"}
_SESSION_FILE = "web_session.json"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _make_loader() -> "instaloader.Instaloader":
    return instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
    )


class YtdlpInstaFetcher:
    """
    Fetch public Instagram content through a selectable backend.

    instaloader returns direct media URLs. yt-dlp returns pipeline-compatible
    media items that are downloaded by yt-dlp later. playwright enumerates
    profile post URLs in a real browser, then reuses the yt-dlp post path.
    """

    def __init__(self, scraping_path: str = "instaloader", session_dir: str | None = None):
        scraping_path = (scraping_path or "instaloader").strip().lower()
        if scraping_path not in _VALID_SCRAPING_PATHS:
            logger.warning("Unknown SCRAPING_PATH=%s; using instaloader.", scraping_path)
            scraping_path = "instaloader"
        self.scraping_path = scraping_path
        self.session_file = Path(session_dir) / _SESSION_FILE if session_dir else None

        if self.scraping_path == "instaloader" and instaloader is None:
            logger.error("instaloader not installed. Run: pip install instaloader")
            raise RuntimeError("instaloader not installed. Run: pip install instaloader")
        if self.scraping_path in {"ytdlp", "playwright"} and yt_dlp is None:
            logger.error("yt-dlp not installed. Run: pip install yt-dlp")
            raise RuntimeError("yt-dlp not installed. Run: pip install yt-dlp")
        if self.scraping_path == "playwright" and sync_playwright is None:
            logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
            raise RuntimeError("playwright not installed. Run: pip install playwright && playwright install chromium")

    def fetch_user_posts(self, username: str, first_n: int = 0, last_n: int = 0) -> list:
        if self.scraping_path == "playwright":
            return self._fetch_user_posts_playwright(username, first_n, last_n)
        if self.scraping_path == "ytdlp":
            return self._fetch_user_posts_ytdlp(username, first_n, last_n)
        return self._fetch_user_posts_instaloader(username, first_n, last_n)

    def fetch_single_post(self, url_or_shortcode: str) -> list:
        if self.scraping_path == "playwright":
            return self._fetch_single_post_playwright(url_or_shortcode)
        if self.scraping_path == "ytdlp":
            return self._fetch_single_post_ytdlp(url_or_shortcode)
        return self._fetch_single_post_instaloader(url_or_shortcode)

    def fetch_bulk(self, urls: list) -> list:
        if self.scraping_path == "playwright":
            return self._fetch_bulk_playwright(urls)
        if self.scraping_path == "ytdlp":
            return self._fetch_bulk_ytdlp(urls)
        return self._fetch_bulk_instaloader(urls)

    def _fetch_user_posts_instaloader(self, username: str, first_n: int = 0, last_n: int = 0) -> list:
        logger.info(f"[instaloader] Fetching profile: @{username}")
        L = _make_loader()

        try:
            profile = instaloader.Profile.from_username(L.context, username)
        except instaloader.exceptions.ProfileNotExistsException:
            logger.error(f"Profile @{username} not found.")
            raise RuntimeError(f"Instagram profile @{username} was not found.")

        if profile.is_private:
            logger.error(f"@{username} is a private account.")
            raise RuntimeError(f"Instagram profile @{username} is private.")

        fetch_limit = first_n if (last_n == 0 and first_n > 0) else None

        items = []
        for post in profile.get_posts():
            item = self._convert_instaloader(post)
            if item:
                items.append(item)
            if fetch_limit and len(items) >= fetch_limit:
                break

        logger.info(f"  Got {len(items)} posts from @{username}.")
        return self._slice(items, first_n, last_n)

    def _fetch_single_post_instaloader(self, url_or_shortcode: str) -> list:
        if instaloader is None:
            logger.warning("instaloader is not installed; cannot fetch %s", url_or_shortcode)
            return []
        shortcode = self._extract_shortcode(url_or_shortcode)
        logger.info(f"[instaloader] Fetching post: {shortcode}")
        L = _make_loader()
        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
        except Exception as e:
            logger.warning(f"Could not fetch {shortcode}: {e}")
            return []
        item = self._convert_instaloader(post)
        return [item] if item else []

    def _fetch_bulk_instaloader(self, urls: list) -> list:
        media_items = []
        L = _make_loader()
        for url in urls:
            time.sleep(random.uniform(1, 3))
            shortcode = self._extract_shortcode(url)
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                item = self._convert_instaloader(post)
                if item:
                    media_items.append(item)
            except Exception as e:
                logger.warning(f"Skipping {url}: {e}")
        return media_items

    def _convert_instaloader(self, post) -> dict | None:
        shortcode = post.shortcode
        caption = post.caption or ""
        username = post.owner_username
        typename = post.typename

        if typename == "GraphSidecar":
            children = []
            for idx, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video:
                    child = {
                        "code": f"{shortcode}_{idx}",
                        "media_type": 2,
                        "video_versions": [{"url": node.video_url}],
                        "image_versions2": {"candidates": [{"url": node.display_url}]},
                    }
                else:
                    child = {
                        "code": f"{shortcode}_{idx}",
                        "media_type": 1,
                        "image_versions2": {"candidates": [{"url": node.display_url}]},
                    }
                children.append(child)
            if not children:
                return None
            return {
                "code": shortcode,
                "media_type": 8,
                "carousel_media": children,
                "caption": {"text": caption},
                "user": {"username": username},
            }

        if typename == "GraphVideo":
            return {
                "code": shortcode,
                "media_type": 2,
                "video_versions": [{"url": post.video_url}],
                "image_versions2": {"candidates": [{"url": post.url}]},
                "caption": {"text": caption},
                "user": {"username": username},
            }

        return {
            "code": shortcode,
            "media_type": 1,
            "image_versions2": {"candidates": [{"url": post.url}]},
            "caption": {"text": caption},
            "user": {"username": username},
        }

    def _fetch_user_posts_ytdlp(self, username: str, first_n: int = 0, last_n: int = 0) -> list:
        if last_n > 0:
            logger.warning("yt-dlp profile mode fetches newest posts; last_n is treated as a newest-post limit.")
        limit = first_n or last_n or 0
        url = f"https://www.instagram.com/{username.strip().lstrip('@')}/"
        logger.info(f"[yt-dlp] Fetching profile: {url}")

        with yt_dlp.YoutubeDL(self._ytdlp_opts(playlistend=limit or None)) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = (info or {}).get("entries") or []
        items = [self._convert_ytdlp_info(entry) for entry in entries if entry]
        items = [item for item in items if item]
        logger.info(f"  Got {len(items)} posts from @{username} via yt-dlp.")
        return self._slice(items, first_n, last_n)

    def _fetch_single_post_ytdlp(self, url_or_shortcode: str) -> list:
        url = self._normalize_post_url(url_or_shortcode)
        logger.info(f"[yt-dlp] Fetching post metadata: {url}")
        try:
            with yt_dlp.YoutubeDL(self._ytdlp_opts()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.warning(f"Could not fetch {url}: {e}")
            return []
        item = self._convert_ytdlp_info(info)
        return [item] if item else []

    def _fetch_bulk_ytdlp(self, urls: list) -> list:
        media_items = []
        for url in urls:
            time.sleep(random.uniform(1, 3))
            media_items.extend(self._fetch_single_post_ytdlp(url))
        return media_items

    def _fetch_single_post_playwright(self, url_or_shortcode: str) -> list:
        url = self._normalize_post_url(url_or_shortcode)
        shortcode = self._extract_shortcode(url)
        logger.info(f"[playwright] Fetching post metadata: {url}")
        items = self._extract_post_media_with_playwright(url, shortcode)
        if items:
            return items

        logger.warning("Playwright did not expose post JSON for %s; falling back to instaloader.", url)
        items = self._fetch_single_post_instaloader(url)
        if items:
            return items

        if "/reel/" in url or "/tv/" in url:
            logger.warning("instaloader did not fetch %s; falling back to yt-dlp.", url)
            return self._fetch_single_post_ytdlp(url)

        logger.warning("Could not fetch post media for %s", url)
        return []

    def _fetch_bulk_playwright(self, urls: list) -> list:
        media_items = []
        for url in urls:
            time.sleep(random.uniform(1, 3))
            media_items.extend(self._fetch_single_post_playwright(url))
        return media_items

    def _fetch_user_posts_playwright(self, username: str, first_n: int = 0, last_n: int = 0) -> list:
        if last_n > 0:
            logger.warning("Playwright profile mode fetches newest posts; last_n is treated as a newest-post limit.")

        limit = first_n or last_n or 0
        profile_url = f"https://www.instagram.com/{username.strip().lstrip('@')}/"
        logger.info(f"[playwright] Enumerating profile posts: {profile_url}")
        items = self._enumerate_profile_media_with_playwright(profile_url, limit=limit)
        logger.info(f"  Discovered {len(items)} post(s) from @{username}.")
        return self._slice(items, first_n, last_n)

    def _enumerate_profile_media_with_playwright(self, profile_url: str, limit: int = 0) -> list[dict]:
        seen = set()
        items = []
        fallback_urls = []

        def add_item(item: dict) -> None:
            code = item.get("code")
            if code and code not in seen:
                seen.add(code)
                items.append(item)

        def add_url(url: str) -> None:
            shortcode = self._extract_shortcode(url)
            if shortcode and shortcode not in seen and url not in fallback_urls:
                fallback_urls.append(url)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_BROWSER_UA)
            self._restore_browser_cookies(context)
            page = context.new_page()

            def on_response(response) -> None:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    data = response.json()
                except Exception:
                    return
                for item in self._find_media_items(data):
                    add_item(item)

            page.on("response", on_response)
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)

            stall = 0
            previous_count = 0
            while stall < 4:
                for href in self._extract_post_hrefs(page):
                    add_url(href)

                if limit and len(items) >= limit:
                    browser.close()
                    return items[:limit]

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2_000)
                if len(items) == previous_count:
                    stall += 1
                else:
                    stall = 0
                previous_count = len(items)

            browser.close()

        if limit:
            fallback_urls = fallback_urls[:max(limit - len(items), 0)]
        for url in fallback_urls:
            for item in self._fetch_single_post_playwright(url):
                add_item(item)
        return items[:limit] if limit else items

    def _enumerate_profile_urls_with_playwright(self, profile_url: str, limit: int = 0) -> list[str]:
        seen = set()
        ordered = []
        stall = 0
        previous_count = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_BROWSER_UA)
            self._restore_browser_cookies(context)
            page = context.new_page()

            def add_url(url: str) -> None:
                shortcode = self._extract_shortcode(url)
                if shortcode and shortcode not in seen:
                    seen.add(shortcode)
                    ordered.append(url)

            def on_response(response) -> None:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    data = response.json()
                except Exception:
                    return
                for shortcode in self._find_shortcodes(data):
                    add_url(f"https://www.instagram.com/p/{shortcode}/")

            page.on("response", on_response)
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)

            while stall < 4:
                for href in self._extract_post_hrefs(page):
                    add_url(href)
                    if limit and len(ordered) >= limit:
                        browser.close()
                        return ordered[:limit]

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2_000)
                if limit and len(ordered) >= limit:
                    browser.close()
                    return ordered[:limit]
                if len(ordered) == previous_count:
                    stall += 1
                else:
                    stall = 0
                previous_count = len(ordered)

            browser.close()

        return ordered[:limit] if limit else ordered

    def _extract_post_media_with_playwright(self, url: str, shortcode: str) -> list[dict]:
        seen = set()
        items = []

        def add_item(item: dict) -> None:
            code = item.get("code")
            if code == shortcode and code not in seen:
                seen.add(code)
                items.append(item)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_BROWSER_UA)
            self._restore_browser_cookies(context)
            page = context.new_page()

            def on_response(response) -> None:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                try:
                    data = response.json()
                except Exception:
                    return
                for item in self._find_media_items(data):
                    add_item(item)

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)
            browser.close()

        return items

    def _restore_browser_cookies(self, context) -> None:
        if not self.session_file or not self.session_file.exists():
            return
        try:
            saved_cookies = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return
        cookies = [
            {"name": key, "value": value, "domain": ".instagram.com", "path": "/"}
            for key, value in saved_cookies.items()
            if key in {"sessionid", "csrftoken", "ds_user_id", "ig_did", "mid", "datr"}
        ]
        if cookies:
            context.add_cookies(cookies)

    @staticmethod
    def _extract_post_hrefs(page) -> list[str]:
        hrefs = page.eval_on_selector_all(
            'a[href^="/p/"], a[href^="/reel/"], a[href^="/tv/"]',
            "links => links.map(link => link.href)",
        )
        return [href for href in hrefs if _SC_RE.search(href)]

    @classmethod
    def _find_shortcodes(cls, value) -> list[str]:
        found = []

        def walk(node):
            if isinstance(node, dict):
                shortcode = node.get("shortcode") or node.get("code")
                if isinstance(shortcode, str) and _SHORTCODE_RE.match(shortcode):
                    found.append(shortcode)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return found

    def _find_media_items(self, value) -> list[dict]:
        found = []

        def walk(node):
            if isinstance(node, dict):
                item = self._normalize_web_media(node)
                if item:
                    found.append(item)
                    return
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return found

    def _normalize_web_media(self, media: dict) -> dict | None:
        code = media.get("code") or media.get("shortcode")
        if not isinstance(code, str) or not _SHORTCODE_RE.match(code):
            return None
        if not (
            media.get("image_versions2")
            or media.get("video_versions")
            or media.get("carousel_media")
        ):
            return None

        media_type = media.get("media_type")
        if media_type is None:
            media_type = 8 if media.get("carousel_media") else 2 if media.get("video_versions") else 1

        caption = ""
        raw_caption = media.get("caption")
        if isinstance(raw_caption, dict):
            caption = raw_caption.get("text") or ""
        elif raw_caption:
            caption = str(raw_caption)

        user = "unknown"
        raw_user = media.get("user")
        if isinstance(raw_user, dict):
            user = raw_user.get("username") or user

        item = {
            "pk": str(media.get("pk", media.get("id", code))),
            "id": str(media.get("id", "")),
            "code": code,
            "media_type": media_type,
            "product_type": media.get("product_type", "feed"),
            "caption": {"text": caption},
            "user": {"username": user},
        }

        if media.get("image_versions2"):
            item["image_versions2"] = media["image_versions2"]
        if media.get("video_versions"):
            item["video_versions"] = media["video_versions"]
        if media_type == 8 and media.get("carousel_media"):
            children = []
            for idx, child in enumerate(media.get("carousel_media", [])):
                child_media = child.copy() if isinstance(child, dict) else child
                if isinstance(child_media, dict) and not (child_media.get("code") or child_media.get("shortcode")):
                    child_media["code"] = f"{code}_{idx}"
                normalized = self._normalize_web_media(child_media)
                if normalized:
                    children.append(normalized)
            if children:
                item["carousel_media"] = children

        is_carousel_child = "_" in code
        if media_type == 2 and not is_carousel_child:
            source_url = self._post_url_for_item(item)
            item["_download_with_ytdlp"] = True
            item["_source_url"] = source_url
            item["video_versions"] = [{"url": source_url}]

        return item

    @staticmethod
    def _post_url_for_item(item: dict) -> str:
        code = item.get("code", "")
        if item.get("product_type") == "clips":
            return f"https://www.instagram.com/reel/{code}/"
        return f"https://www.instagram.com/p/{code}/"

    def _ytdlp_opts(self, *, playlistend: int | None = None) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": False,
            "ignoreerrors": True,
            "noplaylist": False,
            "extract_flat": False,
            "skip_download": True,
        }
        if playlistend:
            opts["playlistend"] = playlistend
        return opts

    def _convert_ytdlp_info(self, info: dict) -> dict | None:
        if not info:
            return None

        webpage_url = info.get("webpage_url") or info.get("original_url") or ""
        shortcode = self._extract_shortcode(webpage_url) if webpage_url else str(info.get("id", "")).strip()
        if not shortcode:
            return None

        thumbnails = [
            {"url": thumb.get("url")}
            for thumb in info.get("thumbnails", [])
            if thumb.get("url")
        ]
        source_url = webpage_url or self._normalize_post_url(shortcode)

        return {
            "code": shortcode,
            "media_type": 2,
            "product_type": "clips",
            "video_versions": [{"url": source_url}],
            "image_versions2": {"candidates": thumbnails},
            "caption": {"text": info.get("description") or info.get("title") or ""},
            "user": {"username": info.get("uploader") or info.get("channel") or "unknown"},
            "_download_with_ytdlp": True,
            "_source_url": source_url,
        }

    @staticmethod
    def download_with_ytdlp(source_url: str, folder: str, code: str) -> bool:
        if yt_dlp is None:
            logger.warning("yt-dlp is not installed; cannot download %s", source_url)
            return False

        Path(folder).mkdir(parents=True, exist_ok=True)
        outtmpl = str(Path(folder) / f"00_{code}.%(ext)s")
        opts = {
            "quiet": True,
            "no_warnings": False,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([source_url])
            return True
        except Exception as e:
            logger.warning(f"yt-dlp download failed for {source_url}: {e}")
            return False

    @staticmethod
    def _extract_shortcode(url_or_shortcode: str) -> str:
        m = _SC_RE.search(url_or_shortcode)
        if m:
            return m.group(1)
        return url_or_shortcode.strip("/")

    @classmethod
    def _normalize_post_url(cls, url_or_shortcode: str) -> str:
        value = url_or_shortcode.strip()
        if value.startswith("http"):
            return value
        shortcode = cls._extract_shortcode(value)
        return f"https://www.instagram.com/p/{shortcode}/"

    @staticmethod
    def _slice(items: list, first_n: int, last_n: int) -> list:
        if last_n > 0 and len(items) > (first_n + last_n):
            first_part = items[:first_n] if first_n > 0 else []
            last_part = items[-last_n:]
            seen, out = set(), []
            for item in first_part + last_part:
                code = item.get("code")
                if code and code not in seen:
                    seen.add(code)
                    out.append(item)
            return out
        if first_n > 0:
            return items[:first_n]
        return items
