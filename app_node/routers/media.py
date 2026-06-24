"""
Media scraper API routes.
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..artifacts import create_job_output_dir, get_artifact_expiry, register_directory_artifacts
from ..input_resolver import InputResolutionError, resolve_multi_input
from ..settings import MEDIA_NODE_DIR, ROOT_DIR, SCRAPING_PATH
from ..tasks import append_task_event, submit_bound_job

sys.path.insert(0, str(MEDIA_NODE_DIR))

_spec = importlib.util.spec_from_file_location("media_services", str(MEDIA_NODE_DIR / "services.py"))
_media_services = importlib.util.module_from_spec(_spec)
sys.modules["media_services"] = _media_services
_spec.loader.exec_module(_media_services)

ScraperService = _media_services.ScraperService
YoutubeService = _media_services.YoutubeService

from media_node.insta.web_fetcher import WebCollectionFetcher  # noqa: E402
from media_node.insta.ytdlp_fetcher import YtdlpInstaFetcher  # noqa: E402
from media_node.llm_service import LLMCleaner  # noqa: E402
from media_node.utils import get_content_type  # noqa: E402

router = APIRouter(prefix="/media", tags=["Media"])

SESSION_ROOT = ROOT_DIR / "data" / "app_node_sessions" / "instagram"
USER_ARTIFACT_DIR = "_user_downloads"
MEDIA_DOWNLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}


def _accepted_response(task) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "task_id": task.id,
            "status": task.status,
            "queue_position": task.queue_position,
            "poll_url": f"/api/tasks/{task.id}",
        },
    )


def _build_result(*, task_id: str, message: str, input_payload: dict,
                  output_payload: dict, stats: dict,
                  artifacts: list[dict], items: Optional[list[dict]] = None) -> dict:
    result = {
        "message": message,
        "input": input_payload,
        "output": output_payload,
        "artifacts": artifacts,
        "stats": stats,
    }
    if items is not None:
        result["items"] = items
    expires_at = get_artifact_expiry(task_id)
    if expires_at:
        result["expires_at"] = expires_at
    return result


def _post_id_for(path: Path, base_dir: Path) -> str:
    try:
        relative = path.relative_to(base_dir)
    except ValueError:
        return path.stem
    for part in relative.parts:
        if part.startswith("post_") and len(part) > 5:
            return part[5:]
    return path.stem


def _copy_user_media_files(source_dir: Path, target_dir: Path) -> None:
    counters: dict[tuple[str, str], int] = {}
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        if USER_ARTIFACT_DIR in source.parts:
            continue
        if source.suffix.lower() not in MEDIA_DOWNLOAD_SUFFIXES:
            continue

        post_id = _post_id_for(source, source_dir)
        kind = "video" if source.suffix.lower() in {".mp4", ".mov", ".m4v"} else "photo"
        key = (post_id, kind)
        counters[key] = counters.get(key, 0) + 1
        index = counters[key]
        label = kind if kind == "video" and index == 1 else f"{kind}_{index}"
        shutil.copy2(source, target_dir / f"{post_id}_{label}{source.suffix.lower()}")


def _write_user_transcription(source_dir: Path, target_dir: Path) -> None:
    transcription_path = source_dir / "transcription.txt"
    if transcription_path.exists():
        shutil.copy2(transcription_path, target_dir / "transcription.txt")
        return

    text_files = [
        path for path in sorted(source_dir.glob("*.txt"))
        if path.name != "all_combined_cleaned.txt"
    ]
    if not text_files:
        return

    blocks = []
    for path in text_files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            blocks.append(text)
    if blocks:
        (target_dir / "transcription.txt").write_text("\n\n".join(blocks), encoding="utf-8")


def _register_user_media_artifacts(task_id: str, job_outdir: Path) -> list[dict]:
    target_dir = job_outdir / USER_ARTIFACT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    _write_user_transcription(job_outdir, target_dir)
    _copy_user_media_files(job_outdir, target_dir)
    return register_directory_artifacts(task_id, target_dir)


def _summarize_media_items(items: list[dict]) -> dict[str, int]:
    counts = {"visual": 0, "audio": 0, "other": 0}
    for item in items:
        ctype = get_content_type(item)
        if ctype in {"image", "carousel"}:
            counts["visual"] += 1
        elif ctype in {"video", "reel"}:
            counts["audio"] += 1
        else:
            counts["other"] += 1
    return counts


def _append_media_item_summary(task_id: str, items: list[dict]) -> dict[str, int]:
    counts = _summarize_media_items(items)
    append_task_event(
        task_id,
        (
            f"Fetched {len(items)} post(s): "
            f"{counts['visual']} visual, {counts['audio']} reel/video, {counts['other']} other"
        ),
    )
    return counts


def _make_public_insta_fetcher(task_id: str) -> YtdlpInstaFetcher:
    append_task_event(task_id, f"Instagram scraping path: {SCRAPING_PATH}")
    return YtdlpInstaFetcher(scraping_path=SCRAPING_PATH, session_dir=str(SESSION_ROOT))


class YoutubeRequest(BaseModel):
    input: Optional[str] = None
    inputs: Optional[list[str]] = None
    input_file: Optional[str] = None
    outdir: Optional[str] = None


class PublicUserRequest(BaseModel):
    username: str
    first_n: int = 10
    outdir: Optional[str] = None


class PostRequest(BaseModel):
    url: str
    outdir: Optional[str] = None


class IgBulkRequest(BaseModel):
    urls: Optional[list[str]] = None
    input_file: Optional[str] = None
    outdir: Optional[str] = None


class PrivateUserRequest(BaseModel):
    collection: str
    username: Optional[str] = None
    password: Optional[str] = None
    outdir: Optional[str] = None


class CleanBulkRequest(BaseModel):
    file_path: str


def _normalize_yt_inputs(req: YoutubeRequest) -> list[str]:
    direct_values = []
    if req.input:
        direct_values.append(req.input)
    if req.inputs:
        direct_values.extend(req.inputs)
    return resolve_multi_input(direct_values=direct_values or None, input_file=req.input_file)


def _normalize_bulk_urls(req: IgBulkRequest) -> list[str]:
    return resolve_multi_input(direct_values=req.urls, input_file=req.input_file)


def _youtube_job(task_id: str, inputs: list[str], outdir: Optional[str]) -> dict:
    job_outdir, output_mode = create_job_output_dir(task_id, "media.youtube", outdir is None, outdir)
    items = []

    for input_value in inputs:
        append_task_event(task_id, f"Queued YouTube input: {input_value}")
        YoutubeService.process_video(
            input_value,
            str(job_outdir),
            progress=lambda message: append_task_event(task_id, message),
        )
        items.append({
            "input": input_value,
            "status": "completed",
        })

    artifacts = _register_user_media_artifacts(task_id, job_outdir)
    return _build_result(
        task_id=task_id,
        message="Completed YouTube media request",
        input_payload={"inputs": inputs},
        output_payload={"output_mode": output_mode},
        stats={"submitted_count": len(inputs), "completed_count": len(items)},
        artifacts=artifacts,
        items=items,
    )


def _public_user_job(task_id: str, username: str, first_n: int, outdir: Optional[str]) -> dict:
    job_outdir, output_mode = create_job_output_dir(task_id, "media.public_user", outdir is None, outdir)
    append_task_event(task_id, "Instagram scraping path: authenticated browser")
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    web = WebCollectionFetcher(
        outdir=str(job_outdir),
        session_dir=str(SESSION_ROOT),
        username=None,
        password=None,
    )
    items = web.fetch_public_profile(username, first_n=first_n)
    if not items:
        raise RuntimeError(
            f"No media items were fetched from @{username}. Login may be required or Instagram returned an empty page."
        )
    media_counts = _append_media_item_summary(task_id, items)
    checkpoint = os.path.join(job_outdir, "checkpoint.json")
    ScraperService.process_posts(
        items,
        str(job_outdir),
        checkpoint_path=checkpoint,
        combined_file_path=str(job_outdir / "transcription.txt"),
        clean_output=False,
        progress=lambda message: append_task_event(task_id, message),
    )
    artifacts = _register_user_media_artifacts(task_id, job_outdir)
    return _build_result(
        task_id=task_id,
        message="Completed public user scrape",
        input_payload={"username": username, "first_n": first_n},
        output_payload={"output_mode": output_mode},
        stats={"fetched_item_count": len(items), **media_counts},
        artifacts=artifacts,
    )


def _post_job(task_id: str, url: str, outdir: Optional[str]) -> dict:
    job_outdir, output_mode = create_job_output_dir(task_id, "media.post", outdir is None, outdir)
    fetcher = _make_public_insta_fetcher(task_id)
    items = fetcher.fetch_single_post(url)
    if not items:
        raise RuntimeError(f"Could not fetch post: {url}")
    media_counts = _append_media_item_summary(task_id, items)
    checkpoint = os.path.join(job_outdir, "checkpoint.json")
    ScraperService.process_posts(
        items,
        str(job_outdir),
        checkpoint_path=checkpoint,
        combined_file_path=str(job_outdir / "transcription.txt"),
        clean_output=False,
        progress=lambda message: append_task_event(task_id, message),
    )
    artifacts = _register_user_media_artifacts(task_id, job_outdir)
    return _build_result(
        task_id=task_id,
        message="Completed single post scrape",
        input_payload={"url": url},
        output_payload={"output_mode": output_mode},
        stats={"fetched_item_count": len(items), **media_counts},
        artifacts=artifacts,
    )


def _ig_bulk_job(task_id: str, urls: list[str], outdir: Optional[str]) -> dict:
    job_outdir, output_mode = create_job_output_dir(task_id, "media.ig_bulk", outdir is None, outdir)
    fetcher = _make_public_insta_fetcher(task_id)
    items = fetcher.fetch_bulk(urls)
    if not items:
        raise RuntimeError("No media items fetched from the provided URLs.")
    media_counts = _append_media_item_summary(task_id, items)
    checkpoint = os.path.join(job_outdir, "checkpoint.json")
    ScraperService.process_posts(
        items,
        str(job_outdir),
        checkpoint_path=checkpoint,
        combined_file_path=str(job_outdir / "transcription.txt"),
        clean_output=False,
        progress=lambda message: append_task_event(task_id, message),
    )
    artifacts = _register_user_media_artifacts(task_id, job_outdir)
    return _build_result(
        task_id=task_id,
        message="Completed bulk Instagram scrape",
        input_payload={"urls": urls},
        output_payload={"output_mode": output_mode},
        stats={
            "submitted_url_count": len(urls),
            "fetched_item_count": len(items),
            **media_counts,
        },
        artifacts=artifacts,
    )


def _private_user_job(task_id: str, collection: str, username: Optional[str],
                      password: Optional[str], outdir: Optional[str]) -> dict:
    job_outdir, output_mode = create_job_output_dir(task_id, "media.private_user", outdir is None, outdir)
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    if not (SESSION_ROOT / "web_session.json").exists():
        append_task_event(task_id, "Instagram login window opening; check the browser window or taskbar.")
    web = WebCollectionFetcher(
        outdir=str(job_outdir),
        session_dir=str(SESSION_ROOT),
        username=username,
        password=password,
    )
    items = web.fetch_collection(collection)
    if not items:
        raise RuntimeError(
            f"No media items were fetched from collection '{collection}'. Check the collection name and session."
        )
    media_counts = _append_media_item_summary(task_id, items)
    checkpoint = os.path.join(job_outdir, "checkpoint.json")
    ScraperService.process_posts(
        items,
        str(job_outdir),
        checkpoint_path=checkpoint,
        combined_file_path=str(job_outdir / "transcription.txt"),
        clean_output=False,
        progress=lambda message: append_task_event(task_id, message),
    )
    artifacts = _register_user_media_artifacts(task_id, job_outdir)
    return _build_result(
        task_id=task_id,
        message="Completed private collection scrape",
        input_payload={"collection": collection},
        output_payload={"output_mode": output_mode},
        stats={"fetched_item_count": len(items), **media_counts},
        artifacts=artifacts,
    )


def _clean_bulk_job(task_id: str, file_path: str) -> dict:
    base, ext = os.path.splitext(file_path)
    output = f"{base}_cleaned{ext}"
    LLMCleaner().clean_bulk_file(file_path, output)
    artifacts = register_directory_artifacts(task_id, Path(output).parent, include_suffixes=[Path(output).suffix])
    return _build_result(
        task_id=task_id,
        message="Completed bulk file cleaning",
        input_payload={"file_path": file_path},
        output_payload={"cleaned_file_path": output, "output_mode": "persistent"},
        stats={"submitted_file_count": 1},
        artifacts=[artifact for artifact in artifacts if artifact["name"] == Path(output).name],
    )


@router.post("/youtube")
async def process_youtube(req: YoutubeRequest, request: Request):
    try:
        inputs = _normalize_yt_inputs(req)
    except InputResolutionError as exc:
        raise HTTPException(400, str(exc)) from exc

    task = await submit_bound_job(
        "media.youtube",
        lambda task: _youtube_job(task.id, inputs, req.outdir),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)


@router.post("/public-user")
async def scrape_public_user(req: PublicUserRequest, request: Request):
    username = req.username.strip()
    if not username:
        raise HTTPException(400, "Instagram username is required.")
    if req.first_n < 1:
        raise HTTPException(400, "first_n must be at least 1.")

    task = await submit_bound_job(
        "media.public_user",
        lambda task: _public_user_job(task.id, username, req.first_n, req.outdir),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)


@router.post("/post")
async def scrape_post(req: PostRequest, request: Request):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Instagram post URL is required.")

    task = await submit_bound_job(
        "media.post",
        lambda task: _post_job(task.id, url, req.outdir),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)


@router.post("/ig-bulk")
async def scrape_ig_bulk(req: IgBulkRequest, request: Request):
    try:
        urls = _normalize_bulk_urls(req)
    except InputResolutionError as exc:
        raise HTTPException(400, str(exc)) from exc

    task = await submit_bound_job(
        "media.ig_bulk",
        lambda task: _ig_bulk_job(task.id, urls, req.outdir),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)


@router.post("/private-user")
async def scrape_private_user(req: PrivateUserRequest, request: Request):
    collection = req.collection.strip()
    if not collection:
        raise HTTPException(400, "Collection name is required.")

    task = await submit_bound_job(
        "media.private_user",
        lambda task: _private_user_job(task.id, collection, req.username, req.password, req.outdir),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)


@router.post("/clean-bulk")
async def clean_bulk(req: CleanBulkRequest, request: Request):
    if not os.path.isfile(req.file_path):
        raise HTTPException(400, f"File not found: {req.file_path}")

    task = await submit_bound_job(
        "media.clean_bulk",
        lambda task: _clean_bulk_job(task.id, req.file_path),
        submitted_by=request.client.host if request.client else None,
    )
    return _accepted_response(task)
