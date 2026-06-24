"""
Server-rendered UI routes and HTMX partials for Gateway Console.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..auth_sessions import spotify_auth_sessions
from ..input_resolver import InputResolutionError, resolve_multi_input
from ..settings import (
    MEDIA_NODE_ENABLED,
    MUSIC_NODE_ENABLED,
    NAVIDROME_ENABLED,
    ROOT_DIR,
    TELEGRAM_NODE_ENABLED,
)
from ..tasks import (
    cancel_task,
    get_queue_summary,
    get_task,
    submit_async_job,
    submit_bound_job,
)
from . import media as media_api
from . import music as music_api
from . import telegram as telegram_api


router = APIRouter(tags=["UI"])
templates = Jinja2Templates(directory=str(ROOT_DIR / "app_node" / "templates"))

GITHUB_URL = "https://github.com/Sanathnavada/Code"
INSTAGRAM_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?(?:\?[^ \t\r\n<>'\"]*)?",
    re.IGNORECASE,
)


def _feature_flags() -> dict[str, bool]:
    return {
        "telegram": TELEGRAM_NODE_ENABLED,
        "media": MEDIA_NODE_ENABLED,
        "music": MUSIC_NODE_ENABLED,
        "navidrome": NAVIDROME_ENABLED,
    }


def _render(request: Request, template_name: str, **context):
    base_context = {
        "request": request,
        "active_page": context.pop("active_page", ""),
        "github_url": GITHUB_URL,
        "feature_flags": _feature_flags(),
    }
    base_context.update(context)
    return templates.TemplateResponse(template_name, base_context)


def _task_page_url(task_id: str) -> str:
    return f"/tasks/{task_id}"


def _task_card_url(task_id: str, container_id: str) -> str:
    return f"/ui/tasks/{task_id}/card?container_id={container_id}"


def _artifact_list_url(task_id: str) -> str:
    return f"/ui/tasks/{task_id}/artifacts"


def _request_client_id(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _format_task_time(value: Optional[str], *, include_zone: bool = True) -> str:
    if not value:
        return "Pending"
    try:
        parsed = datetime.fromisoformat(value)
        local_time = parsed.astimezone()
    except ValueError:
        return value

    time_label = local_time.strftime("%I:%M:%S %p").lstrip("0")
    zone_label = local_time.tzname() or "local time"
    return f"{time_label} {zone_label}" if include_zone else time_label


def _format_duration_seconds(value: Optional[int]) -> Optional[str]:
    if not value:
        return None
    minutes, seconds = divmod(max(int(value), 0), 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _task_timezone_label(task) -> str:
    for value in (task.submitted_at, task.started_at, task.finished_at):
        if value:
            try:
                return datetime.fromisoformat(value).astimezone().tzname() or "local time"
            except ValueError:
                continue
    return "local time"


def _format_task_events(task) -> list[dict]:
    events = task.meta.get("events", [])
    formatted = []
    for event in events[-6:]:
        event_time = event.get("time")
        formatted.append({
            "time": _format_task_time(event_time),
            "short_time": _format_task_time(event_time, include_zone=False) if event_time else "",
            "message": event.get("message", ""),
        })
    return formatted


def _poll_interval_seconds(task) -> int:
    if task.status == "queued":
        return 2
    if task.status != "running":
        return 0
    try:
        return int(task.meta.get("poll_interval_seconds") or 2)
    except (TypeError, ValueError):
        return 2


def _task_display_name(task) -> str:
    return task.meta.get("workflow_label") or task.service.replace(".", " ").title()


def _task_item_label(task) -> str:
    return task.meta.get("item_label") or "Task info"


def _task_item_detail(task) -> str:
    if task.meta.get("item_detail"):
        return task.meta["item_detail"]
    if task.result and isinstance(task.result, dict):
        inputs = task.result.get("input", {}).get("inputs")
        if inputs:
            return f"{inputs[0]} + {len(inputs) - 1} more" if len(inputs) > 1 else inputs[0]
    return "Waiting for job details"


def _task_result_items(task) -> list[dict]:
    if not task.result or not isinstance(task.result, dict):
        return []
    return task.result.get("items", [])[:8]


def _media_meta(workflow_label: str, item_label: str, item_detail: str, **extra) -> dict:
    return {
        "workflow_label": workflow_label,
        "item_label": item_label,
        "item_detail": item_detail,
        **extra,
    }


def _job_meta(workflow_label: str, item_label: str, item_detail: str, **extra) -> dict:
    return _media_meta(workflow_label, item_label, item_detail, **extra)


def _normalize_lines(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _resolve_ui_values(values: list[str], empty_message: str) -> list[str]:
    if not values:
        raise InputResolutionError(empty_message)
    return resolve_multi_input(direct_values=values, input_file=None)


def _task_view_model(task, *, container_id: str) -> dict:
    duration = None
    if task.started_at and task.finished_at:
        started = datetime.fromisoformat(task.started_at)
        finished = datetime.fromisoformat(task.finished_at)
        delta = max(int((finished - started).total_seconds()), 0)
        minutes, seconds = divmod(delta, 60)
        duration = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    playlists = []
    if (
        task.service == "music.user_playlists"
        and task.status == "completed"
        and isinstance(task.result, dict)
    ):
        playlists = task.result.get("output", {}).get("playlists", [])
        for playlist in playlists:
            for track in playlist.get("tracks", []):
                track["payload"] = json.dumps(
                    {
                        "title": track.get("title", ""),
                        "artist": track.get("artist", ""),
                        "album": track.get("album", ""),
                        "image_url": track.get("image_url", ""),
                    },
                    separators=(",", ":"),
                )

    return {
        "task": task,
        "task_card_url": _task_card_url(task.id, container_id),
        "task_page_url": _task_page_url(task.id),
        "artifact_list_url": _artifact_list_url(task.id),
        "is_detail_view": container_id == "task-detail-panel",
        "is_polling": task.status in {"queued", "running"},
        "poll_interval_seconds": _poll_interval_seconds(task),
        "duration_label": duration,
        "estimate_label": _format_duration_seconds(task.meta.get("estimated_transcription_seconds")),
        "display_name": _task_display_name(task),
        "item_label": _task_item_label(task),
        "item_detail": _task_item_detail(task),
        "submitted_label": _format_task_time(task.submitted_at),
        "started_label": _format_task_time(task.started_at),
        "finished_label": _format_task_time(task.finished_at) if task.finished_at else "Not finished",
        "timezone_label": _task_timezone_label(task),
        "task_events": _format_task_events(task),
        "result_items": _task_result_items(task),
        "playlist_options": playlists,
        "library_warnings": task.result.get("warnings", []) if isinstance(task.result, dict) else [],
        "playlist_target_id": f"#{container_id}",
    }


def _render_error_panel(request: Request, message: str, title: str = "Request Error",
                        status_code: int = status.HTTP_400_BAD_REQUEST):
    response = _render(
        request,
        "partials/flash_panel.html",
        message=message,
        title=title,
        kind="error",
    )
    response.status_code = status_code
    return response


def _render_task_card(request: Request, task_id: str, *, title: Optional[str] = None,
                      container_id: str = "task-panel"):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found.")
    view_model = _task_view_model(task, container_id=container_id)
    if (
        task.service == "music.user_playlists"
        and task.status == "completed"
        and view_model["playlist_options"]
    ):
        view_model["playlist_target_id"] = "#music-library-task-panel"
        return _render(
            request,
            "partials/spotify_library_panel.html",
            title=title,
            **view_model,
        )
    return _render(
        request,
        "partials/task_card.html",
        title=title,
        **view_model,
    )


def _build_music_song_inputs(form_data) -> list[str]:
    direct_values = []
    query = (form_data.get("query") or "").strip()
    if query:
        direct_values.append(query)
    direct_values.extend(_normalize_lines(form_data.get("queries_text")))
    return _resolve_ui_values(direct_values, "Provide at least one song, YouTube link, or Spotify link.")


def _build_music_yt_inputs(form_data) -> list[str]:
    direct_values = []
    single = (form_data.get("input") or "").strip()
    if single:
        direct_values.append(single)
    direct_values.extend(_normalize_lines(form_data.get("inputs_text")))
    return _resolve_ui_values(direct_values, "Provide at least one YouTube input.")


def _build_music_link_inputs(form_data) -> list[str]:
    direct_values = []
    single = (form_data.get("url") or "").strip()
    if single:
        direct_values.append(single)
    direct_values.extend(_normalize_lines(form_data.get("urls_text")))
    return _resolve_ui_values(direct_values, "Provide at least one Spotify URL.")


def _build_media_youtube_inputs(form_data) -> list[str]:
    direct_values = []
    single = (form_data.get("input") or "").strip()
    if single:
        direct_values.append(single)
    direct_values.extend(_normalize_lines(form_data.get("inputs_text")))
    return _resolve_ui_values(direct_values, "Provide at least one YouTube input.")


def _build_media_bulk_inputs(form_data) -> list[str]:
    text = form_data.get("urls_text") or ""
    direct_values = INSTAGRAM_POST_URL_RE.findall(text)
    if not direct_values:
        direct_values = _normalize_lines(text)
    return _resolve_ui_values(direct_values, "Provide at least one Instagram URL.")


def _task_summary_cards():
    summary = get_queue_summary()
    return [
        {
            "label": "Gateway",
            "value": "Healthy",
            "variant": "success",
        },
        {
            "label": "Queue",
            "value": "Idle" if summary["queued_jobs"] == 0 else f"{summary['queued_jobs']} queued",
            "variant": "success" if summary["queued_jobs"] == 0 else "warning",
        },
        {
            "label": "Worker",
            "value": "Available" if summary["running_jobs"] == 0 else "Busy",
            "variant": "success" if summary["running_jobs"] == 0 else "accent",
        },
    ]


@router.get("/", response_class=HTMLResponse, name="ui_home")
async def home_page(request: Request):
    return _render(
        request,
        "pages/index.html",
        active_page="home",
        status_cards=_task_summary_cards(),
    )


@router.get("/media", response_class=HTMLResponse, name="ui_media")
async def media_page(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    return _render(
        request,
        "pages/media.html",
        active_page="media",
    )


@router.get("/music", response_class=HTMLResponse, name="ui_music")
async def music_page(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    return _render(
        request,
        "pages/music.html",
        active_page="music",
    )


@router.get("/telegram", response_class=HTMLResponse, name="ui_telegram")
async def telegram_page(request: Request):
    if not TELEGRAM_NODE_ENABLED:
        raise HTTPException(404, "Telegram node is disabled.")
    return _render(
        request,
        "pages/telegram.html",
        active_page="telegram",
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse, name="ui_task_detail")
async def task_page(request: Request, task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found.")
    return _render(
        request,
        "pages/task.html",
        active_page="task",
        task_id=task_id,
        page_title=_task_display_name(task),
        service_name=_task_display_name(task),
    )


@router.get("/ui/system/status", response_class=HTMLResponse)
async def system_status_partial(request: Request):
    return _render(
        request,
        "partials/system_status.html",
        status_cards=_task_summary_cards(),
        queue_summary=get_queue_summary(),
    )


@router.get("/ui/media/forms/{workflow}", response_class=HTMLResponse)
async def media_form_partial(request: Request, workflow: str):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    template_map = {
        "youtube": "partials/media_youtube_form.html",
        "instagram": "partials/media_instagram_form.html",
        "post": "partials/media_post_form.html",
        "public-user": "partials/media_public_form.html",
        "private-user": "partials/media_private_form.html",
        "bulk": "partials/media_bulk_form.html",
    }
    template_name = template_map.get(workflow)
    if not template_name:
        raise HTTPException(404, "Unknown media workflow.")
    return _render(request, template_name)


@router.get("/ui/music/forms/{workflow}", response_class=HTMLResponse)
async def music_form_partial(request: Request, workflow: str):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    template_map = {
        "download": "partials/music_song_form.html",
        "song": "partials/music_song_form.html",
        "youtube": "partials/music_youtube_form.html",
        "spotify-link": "partials/music_link_form.html",
        "spotify-library": "partials/music_spotify_auth_form.html",
    }
    template_name = template_map.get(workflow)
    if not template_name:
        raise HTTPException(404, "Unknown music workflow.")
    return _render(request, template_name)


@router.get("/ui/telegram/runtime", response_class=HTMLResponse)
async def telegram_runtime_partial(request: Request):
    if not TELEGRAM_NODE_ENABLED:
        raise HTTPException(404, "Telegram node is disabled.")
    task = get_task(telegram_api._agent_task_id) if telegram_api._agent_task_id else None
    status_label = task.status if task else "stopped"
    return _render(
        request,
        "partials/telegram_runtime_card.html",
        task=task,
        status_label=status_label,
    )


@router.post("/ui/telegram/start", response_class=HTMLResponse)
async def telegram_start_submit(request: Request):
    if not TELEGRAM_NODE_ENABLED:
        raise HTTPException(404, "Telegram node is disabled.")
    existing = get_task(telegram_api._agent_task_id) if telegram_api._agent_task_id else None
    if existing and existing.status in {"queued", "running"}:
        return await telegram_runtime_partial(request)

    task = await submit_async_job(
        "telegram.agent",
        lambda: telegram_api.run_agent(),
        submitted_by=_request_client_id(request),
    )
    telegram_api._agent_task_id = task.id
    return await telegram_runtime_partial(request)


@router.post("/ui/telegram/stop", response_class=HTMLResponse)
async def telegram_stop_submit(request: Request):
    if not TELEGRAM_NODE_ENABLED:
        raise HTTPException(404, "Telegram node is disabled.")
    if telegram_api._agent_task_id:
        await cancel_task(telegram_api._agent_task_id)
        telegram_api._agent_task_id = None
    return await telegram_runtime_partial(request)


@router.post("/ui/media/youtube/submit", response_class=HTMLResponse)
async def media_youtube_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    try:
        inputs = _build_media_youtube_inputs(form)
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "media.youtube",
        lambda task: media_api._youtube_job(task.id, inputs, None),
        submitted_by=_request_client_id(request),
        meta=_media_meta(
            "YouTube transcription",
            "YouTube input",
            f"{inputs[0]} + {len(inputs) - 1} more" if len(inputs) > 1 else inputs[0],
            submitted_count=len(inputs),
        ),
    )
    return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")


@router.post("/ui/media/instagram/submit", response_class=HTMLResponse)
async def media_instagram_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    mode = (form.get("instagram_mode") or "posts").strip()

    if mode == "posts":
        try:
            urls = _build_media_bulk_inputs(form)
        except InputResolutionError as exc:
            return _render_error_panel(request, str(exc))

        task = await submit_bound_job(
            "media.ig_bulk",
            lambda task: media_api._ig_bulk_job(task.id, urls, None),
            submitted_by=_request_client_id(request),
            meta=_media_meta(
                "Instagram posts",
                "URLs",
                f"{urls[0]} + {len(urls) - 1} more" if len(urls) > 1 else urls[0],
                submitted_count=len(urls),
            ),
        )
        return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")

    if mode == "public_profile":
        username = (form.get("username") or "").strip()
        if not username:
            return _render_error_panel(request, "Instagram username is required.")
        first_n_raw = (form.get("first_n") or "3").strip()
        try:
            first_n = max(int(first_n_raw), 1)
        except ValueError as exc:
            return _render_error_panel(request, str(exc))

        task = await submit_bound_job(
            "media.public_user",
            lambda task: media_api._public_user_job(task.id, username, first_n, None),
            submitted_by=_request_client_id(request),
            meta=_media_meta(
                "Instagram public profile",
                "Profile",
                f"@{username} - first {first_n} posts",
                submitted_count=first_n,
            ),
        )
        return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")

    if mode == "private_collection":
        collection = (form.get("collection") or "").strip()
        if not collection:
            return _render_error_panel(request, "Collection name is required.")

        task = await submit_bound_job(
            "media.private_user",
            lambda task: media_api._private_user_job(task.id, collection, None, None, None),
            submitted_by=_request_client_id(request),
            meta=_media_meta("Instagram private collection", "Collection", collection),
        )
        return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")

    return _render_error_panel(request, "Unknown Instagram source.")


@router.post("/ui/media/post/submit", response_class=HTMLResponse)
async def media_post_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    url = (form.get("url") or "").strip()
    if not url:
        return _render_error_panel(request, "Instagram post URL is required.")
    task = await submit_bound_job(
        "media.post",
        lambda task: media_api._post_job(task.id, url, None),
        submitted_by=_request_client_id(request),
        meta=_media_meta("Instagram post", "Post URL", url),
    )
    return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")


@router.post("/ui/media/public-user/submit", response_class=HTMLResponse)
async def media_public_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        return _render_error_panel(request, "Instagram username is required.")

    first_n_raw = (form.get("first_n") or "3").strip()
    try:
        first_n = max(int(first_n_raw), 1)
    except ValueError as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "media.public_user",
        lambda task: media_api._public_user_job(task.id, username, first_n, None),
        submitted_by=_request_client_id(request),
        meta=_media_meta(
            "Public profile scrape",
            "Profile",
            f"@{username} - first {first_n} posts",
            submitted_count=first_n,
        ),
    )
    return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")


@router.post("/ui/media/private-user/submit", response_class=HTMLResponse)
async def media_private_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    collection = (form.get("collection") or "").strip()
    if not collection:
        return _render_error_panel(request, "Collection name is required.")

    task = await submit_bound_job(
        "media.private_user",
        lambda task: media_api._private_user_job(task.id, collection, None, None, None),
        submitted_by=_request_client_id(request),
        meta=_media_meta("Private collection scrape", "Collection", collection),
    )
    return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")


@router.post("/ui/media/bulk/submit", response_class=HTMLResponse)
async def media_bulk_submit(request: Request):
    if not MEDIA_NODE_ENABLED:
        raise HTTPException(404, "Media node is disabled.")
    form = await request.form()
    try:
        urls = _build_media_bulk_inputs(form)
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "media.ig_bulk",
        lambda task: media_api._ig_bulk_job(task.id, urls, None),
        submitted_by=_request_client_id(request),
        meta=_media_meta(
            "Bulk Instagram scrape",
            "URLs",
            f"{urls[0]} + {len(urls) - 1} more" if len(urls) > 1 else urls[0],
            submitted_count=len(urls),
        ),
    )
    return _render_task_card(request, task.id, title="Media Task", container_id="media-task-panel")


@router.post("/ui/music/song/submit", response_class=HTMLResponse)
async def music_song_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    form = await request.form()
    try:
        queries = _build_music_song_inputs(form)
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "music.song",
        lambda task: music_api._song_job(task.id, queries, None),
        submitted_by=_request_client_id(request),
        meta=_job_meta(
            "Music download",
            "Input",
            f"{queries[0]} + {len(queries) - 1} more" if len(queries) > 1 else queries[0],
            submitted_count=len(queries),
        ),
    )
    return _render_task_card(request, task.id, title="Music Task", container_id="music-task-panel")


@router.post("/ui/music/youtube/submit", response_class=HTMLResponse)
async def music_yt_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    form = await request.form()
    try:
        inputs = _build_music_yt_inputs(form)
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "music.yt",
        lambda task: music_api._yt_job(task.id, inputs, None),
        submitted_by=_request_client_id(request),
        meta=_job_meta(
            "YouTube audio download",
            "Input",
            f"{inputs[0]} + {len(inputs) - 1} more" if len(inputs) > 1 else inputs[0],
            submitted_count=len(inputs),
        ),
    )
    return _render_task_card(request, task.id, title="Music Task", container_id="music-task-panel")


@router.post("/ui/music/link/submit", response_class=HTMLResponse)
async def music_link_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    form = await request.form()
    try:
        urls = _build_music_link_inputs(form)
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "music.link",
        lambda task: music_api._link_job(task.id, urls, None),
        submitted_by=_request_client_id(request),
        meta=_job_meta(
            "Spotify link download",
            "Spotify URL",
            f"{urls[0]} + {len(urls) - 1} more" if len(urls) > 1 else urls[0],
            submitted_count=len(urls),
        ),
    )
    return _render_task_card(request, task.id, title="Music Task", container_id="music-task-panel")


@router.post("/ui/music/user/auth/start", response_class=HTMLResponse)
async def music_auth_start_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    session = spotify_auth_sessions.start_session()
    return _render(
        request,
        "partials/spotify_auth_card.html",
        session=session,
        poll_url=f"/ui/music/auth/session/{session.id}/card",
        redirect_uri=spotify_auth_sessions.redirect_uri,
    )


@router.get("/ui/music/auth/session/{auth_session_id}/card", response_class=HTMLResponse)
async def spotify_auth_card(request: Request, auth_session_id: str):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    session = spotify_auth_sessions.get_session(auth_session_id)
    if not session:
        return _render(request, "partials/stale_auth_card.html")
    return _render(
        request,
        "partials/spotify_auth_card.html",
        session=session,
        poll_url=f"/ui/music/auth/session/{session.id}/card",
        redirect_uri=spotify_auth_sessions.redirect_uri,
    )


@router.post("/ui/music/user/auth/complete", response_class=HTMLResponse)
async def music_auth_complete_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    form = await request.form()
    auth_session_id = (form.get("auth_session_id") or "").strip()
    redirected_url = (form.get("redirected_url") or "").strip()
    code = (form.get("code") or "").strip() or None
    state = None
    if redirected_url:
        parsed = urlparse(redirected_url)
        values = parse_qs(parsed.query)
        code = code or (values.get("code") or [None])[0]
        state = (values.get("state") or [None])[0]
        if not code and "://" not in redirected_url:
            code = redirected_url
    if not auth_session_id:
        return _render_error_panel(request, "Spotify auth session is missing.")
    if not code:
        return _render_error_panel(request, "Paste the Spotify redirect URL or authorization code.")

    try:
        session = spotify_auth_sessions.complete_from_code(auth_session_id, code, state=state)
    except Exception as exc:
        return _render_error_panel(request, str(exc), title="Spotify Authorization Failed")

    return _render(
        request,
        "partials/spotify_auth_card.html",
        session=session,
        poll_url=f"/ui/music/auth/session/{session.id}/card",
        redirect_uri=spotify_auth_sessions.redirect_uri,
    )


@router.post("/ui/music/user/playlists/submit", response_class=HTMLResponse)
async def music_user_playlists_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    if not spotify_auth_sessions.has_cached_user_token():
        return _render_error_panel(
            request,
            "Spotify user authorization is required before fetching playlists.",
            title="Spotify Authorization Required",
            status_code=status.HTTP_409_CONFLICT,
        )

    task = await submit_bound_job(
        "music.user_playlists",
        lambda task: music_api._user_library_job(),
        submitted_by=_request_client_id(request),
    )
    return _render_task_card(request, task.id, title="Spotify Library Task", container_id="music-library-panel")


@router.post("/ui/music/user/download/submit", response_class=HTMLResponse)
async def music_user_download_submit(request: Request):
    if not MUSIC_NODE_ENABLED:
        raise HTTPException(404, "Music node is disabled.")
    form = await request.form()
    playlist_names = form.getlist("playlists")
    track_refs = form.getlist("tracks")
    download_all = (form.get("download_all") or "").strip().lower() == "true"
    try:
        if download_all:
            selected = ["all"]
            track_refs = []
        elif playlist_names:
            selected = resolve_multi_input(direct_values=playlist_names, input_file=None)
        elif track_refs:
            selected = []
        else:
            raise InputResolutionError("Select at least one playlist or track.")
    except (InputResolutionError, ValueError) as exc:
        return _render_error_panel(request, str(exc))

    task = await submit_bound_job(
        "music.user_download",
        lambda task: music_api._user_download_job(task.id, selected, None, track_refs),
        submitted_by=_request_client_id(request),
    )
    return _render_task_card(request, task.id, title="Music Task", container_id="music-library-task-panel")


@router.get("/ui/tasks/{task_id}/card", response_class=HTMLResponse)
async def task_card_partial(request: Request, task_id: str, container_id: str = "task-panel"):
    if not get_task(task_id):
        return _render(request, "partials/stale_task_card.html")
    return _render_task_card(request, task_id, container_id=container_id)


@router.get("/ui/tasks/{task_id}/artifacts", response_class=HTMLResponse)
async def task_artifact_partial(request: Request, task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found.")
    return _render(
        request,
        "partials/artifact_list.html",
        task=task,
        artifacts=task.artifacts,
    )
