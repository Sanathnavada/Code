from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def request(method: str, path: str, *, data=None, form=None, timeout=30):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
            return response.status, dict(response.headers), content
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def text(method: str, path: str, **kwargs):
    status, headers, content = request(method, path, **kwargs)
    return status, headers, content.decode("utf-8", errors="replace")


def json_body(method: str, path: str, **kwargs):
    status, headers, content = request(method, path, **kwargs)
    try:
        parsed = json.loads(content.decode("utf-8", errors="replace"))
    except Exception:
        parsed = None
    return status, headers, parsed


def extract_task_id(html: str) -> str | None:
    marker = "/ui/tasks/"
    if marker not in html:
        return None
    start = html.find(marker) + len(marker)
    end = html.find("/card", start)
    if end == -1:
        return None
    return html[start:end]


def first_artifact_download(task: dict) -> tuple[int, dict, bytes] | None:
    artifacts = task.get("artifacts") or []
    if not artifacts:
        return None
    url = artifacts[0]["download_url"]
    status, headers, content = request("GET", url)
    return status, headers, content


def header_value(headers: dict, name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def wait_task(task_id: str, timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, _, payload = json_body("GET", f"/api/tasks/{task_id}")
        if status not in (200, 202):
            return status, payload
        last = payload
        if payload and payload.get("status") not in {"queued", "running"}:
            return status, payload
        time.sleep(2)
    return 408, last


def main() -> int:
    checks: list[Check] = []

    status, _, payload = json_body("GET", "/health")
    checks.append(Check("health", status == 200 and payload == {"status": "ok"}, str(payload)))

    for page in ["/", "/media", "/music", "/telegram"]:
        status, _, html = text("GET", page)
        checks.append(Check(f"page {page}", status == 200 and "<html" in html.lower(), f"status={status}"))

    for path in [
        "/ui/media/forms/youtube",
        "/ui/media/forms/post",
        "/ui/media/forms/public-user",
        "/ui/media/forms/private-user",
        "/ui/media/forms/bulk",
        "/ui/music/forms/song",
        "/ui/music/forms/youtube",
        "/ui/music/forms/spotify-link",
        "/ui/music/forms/spotify-library",
        "/ui/telegram/runtime",
        "/ui/system/status",
    ]:
        status, _, html = text("GET", path)
        checks.append(Check(f"partial {path}", status == 200 and len(html) > 20, f"status={status}"))

    status, _, stale = text("GET", "/ui/tasks/not-a-task/card?container_id=media-task-panel")
    checks.append(Check("stale task card stops polling", status == 200 and "Job no longer active" in stale, f"status={status}"))

    status, _, html = text("POST", "/ui/media/post/submit", form={"url": ""})
    checks.append(Check("media post validation", status == 400 and "Instagram post URL is required" in html, f"status={status}"))

    status, _, html = text("POST", "/ui/media/private-user/submit", form={"collection": ""})
    checks.append(Check("media private validation", status == 400 and "Collection name is required" in html, f"status={status}"))

    status, _, html = text("POST", "/ui/music/link/submit", form={"url": "https://example.com/not-spotify"})
    task_id = extract_task_id(html)
    checks.append(Check("music invalid link accepted as queued task", status == 200 and task_id is not None, f"task={task_id} status={status}"))
    if task_id:
        _, task = wait_task(task_id, timeout=60)
        checks.append(Check("music invalid link fails visibly", bool(task and task.get("status") == "failed"), json.dumps(task)[:500] if task else "missing"))

    status, _, html = text("POST", "/ui/music/youtube/submit", form={})
    checks.append(Check("music youtube validation", status == 400 and "Provide at least one YouTube input" in html, f"status={status}"))

    status, _, html = text("POST", "/ui/music/song/submit", form={})
    checks.append(Check("music song validation", status == 400 and "Provide at least one song" in html, f"status={status}"))

    status, _, payload = json_body("GET", "/api/tasks")
    checks.append(Check("tasks list", status == 200 and isinstance(payload, list), f"status={status}"))

    status, _, payload = json_body("POST", "/api/media/youtube", data={})
    checks.append(Check("api media youtube validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/media/clean-bulk", data={"file_path": "missing-dev-e2e.txt"})
    checks.append(Check("api media clean missing file validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/media/post", data={"url": ""})
    checks.append(Check("api media post blank validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/media/public-user", data={"username": "", "first_n": 1})
    checks.append(Check("api media public blank validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/media/public-user", data={"username": "someone", "first_n": 0})
    checks.append(Check("api media public first_n validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/media/private-user", data={"collection": ""})
    checks.append(Check("api media private blank validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/music/link", data={"url": "https://example.com/not-spotify"})
    api_task_id = payload.get("task_id") if isinstance(payload, dict) else None
    checks.append(Check("api music invalid link accepted", status == 202 and api_task_id, f"status={status} payload={payload}"))
    if api_task_id:
        _, task = wait_task(api_task_id, timeout=60)
        checks.append(Check("api music invalid link fails task", bool(task and task.get("status") == "failed"), json.dumps(task)[:500] if task else "missing"))

    status, _, payload = json_body("POST", "/api/music/user/auth/start")
    auth_id = payload.get("auth_session_id") if isinstance(payload, dict) else None
    checks.append(Check("spotify auth start", status == 202 and auth_id and payload.get("authorization_url"), f"status={status} payload={payload}"))
    if auth_id:
        status, _, payload = json_body("GET", f"/api/music/user/auth/session/{auth_id}")
        checks.append(Check("spotify auth session waiting", status == 202 and payload.get("status") == "waiting_for_user", f"status={status} payload={payload}"))

    status, _, payload = json_body("POST", "/api/music/user/auth/complete", data={"auth_session_id": "missing"})
    checks.append(Check("spotify auth complete validation", status == 400, f"status={status} payload={payload}"))

    status, _, payload = json_body("GET", "/api/telegram/status")
    checks.append(Check("telegram status", status == 200 and payload.get("status") in {"stopped", "queued", "running", "failed", "completed"}, f"status={status} payload={payload}"))

    for check in checks:
        state = "PASS" if check.ok else "FAIL"
        print(f"{state} | {check.name} | {check.detail}")

    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
