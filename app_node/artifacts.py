"""
Helpers for ephemeral job output directories and artifact registration.
"""
from __future__ import annotations

import mimetypes
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from .settings import ROOT_DIR


ARTIFACT_ROOT = ROOT_DIR / "data" / "app_node_artifacts"
EPHEMERAL_TTL = timedelta(hours=2)

_artifact_index: dict[str, dict[str, dict[str, Any]]] = {}
_artifact_expirations: dict[str, datetime] = {}


def ensure_artifact_root() -> None:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


def _public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    public = artifact.copy()
    public.pop("absolute_path", None)
    return public


def create_job_output_dir(task_id: str, service: str, ephemeral: bool,
                          persistent_outdir: Optional[str] = None) -> tuple[Path, str]:
    if persistent_outdir:
        outdir = Path(persistent_outdir).expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        return outdir, "persistent"

    if ephemeral:
        ensure_artifact_root()
        service_slug = service.replace(".", "_")
        outdir = ARTIFACT_ROOT / task_id / service_slug
        outdir.mkdir(parents=True, exist_ok=True)
        _artifact_expirations[task_id] = datetime.now(timezone.utc) + EPHEMERAL_TTL
        return outdir, "ephemeral"

    raise ValueError("Persistent outputs require an explicit outdir.")


def register_directory_artifacts(task_id: str, base_dir: Path,
                                 include_suffixes: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    if not base_dir.exists():
        return []

    suffix_filter = {s.lower() for s in include_suffixes} if include_suffixes else None
    artifacts = []
    for path in sorted(p for p in base_dir.rglob("*") if p.is_file()):
        if suffix_filter and path.suffix.lower() not in suffix_filter:
            continue
        artifact_id = path.relative_to(base_dir).as_posix()
        encoded_artifact_id = quote(artifact_id, safe="/")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifacts.append({
            "artifact_id": artifact_id,
            "name": path.name,
            "relative_path": artifact_id,
            "absolute_path": str(path),
            "content_type": content_type,
            "size_bytes": path.stat().st_size,
            "download_url": f"/api/tasks/{task_id}/artifacts/{encoded_artifact_id}",
        })

    _artifact_index[task_id] = {artifact["artifact_id"]: artifact for artifact in artifacts}
    return [_public_artifact(artifact) for artifact in artifacts]


def list_artifacts(task_id: str) -> list[dict[str, Any]]:
    artifacts = []
    for artifact in _artifact_index.get(task_id, {}).values():
        artifact = _public_artifact(artifact)
        artifact["download_url"] = (
            f"/api/tasks/{task_id}/artifacts/"
            f"{quote(artifact['artifact_id'], safe='/')}"
        )
        artifacts.append(artifact)
    return artifacts


def resolve_artifact_path(task_id: str, artifact_id: str) -> Optional[Path]:
    metadata = _artifact_index.get(task_id, {}).get(artifact_id)
    if metadata:
        path = Path(metadata["absolute_path"])
        return path if path.exists() else None

    task_root = (ARTIFACT_ROOT / task_id).resolve()
    if not task_root.exists():
        return None

    for service_dir in task_root.iterdir():
        if not service_dir.is_dir():
            continue
        candidate = (service_dir / artifact_id).resolve()
        if candidate.is_file() and task_root in candidate.parents:
            return candidate
    return None


def get_artifact_expiry(task_id: str) -> Optional[str]:
    expiry = _artifact_expirations.get(task_id)
    return expiry.isoformat() if expiry else None


def cleanup_expired_artifacts(now: Optional[datetime] = None) -> None:
    current = now or datetime.now(timezone.utc)
    expired = [task_id for task_id, expiry in _artifact_expirations.items() if expiry <= current]
    for task_id in expired:
        shutil.rmtree(ARTIFACT_ROOT / task_id, ignore_errors=True)
        _artifact_expirations.pop(task_id, None)
        _artifact_index.pop(task_id, None)

    if not ARTIFACT_ROOT.exists():
        return

    cutoff = current - EPHEMERAL_TTL
    for task_dir in ARTIFACT_ROOT.iterdir():
        if not task_dir.is_dir() or task_dir.name in _artifact_expirations:
            continue
        try:
            modified_at = datetime.fromtimestamp(task_dir.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        if modified_at <= cutoff:
            shutil.rmtree(task_dir, ignore_errors=True)
            _artifact_index.pop(task_dir.name, None)
