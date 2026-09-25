#!/usr/bin/env python3
"""Bridge the shared TenPlus dashboard queue to one local OpenWA session.

No customer numbers, message bodies, QR payloads, or credentials are written to
logs. The OpenWA API is kept on localhost; only the dashboard worker token is
used for outbound HTTPS calls to the shared dashboard.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


OPENWA_ROOT = Path(os.environ.get("OPENWA_ROOT", "/home/ubuntu/tenplus-openwa"))
TOKEN_FILE = Path(os.environ.get("DASHBOARD_WORKER_TOKEN_FILE", "/home/ubuntu/amber-live/worker-token"))
DASHBOARD = os.environ.get("DASHBOARD_BASE", "https://tenplus-dual-agents.onrender.com").rstrip("/")
OPENWA = os.environ.get("OPENWA_API_BASE", "http://127.0.0.1:2785").rstrip("/")
SESSION_NAME = "tenplus-shared"
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 8

stopping = False


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


ENV = load_dotenv(OPENWA_ROOT / ".env")
OPENWA_KEY = ENV.get("API_MASTER_KEY", "")


class ApiFailure(RuntimeError):
    def __init__(self, service: str, code: int | None = None):
        self.service = service
        self.code = code
        super().__init__(f"{service} request failed" + (f" (HTTP {code})" if code else ""))


def http_json(base: str, path: str, *, headers: dict[str, str], payload: dict[str, Any] | None = None,
              timeout: float = 8) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json", "User-Agent": "TenPlus-OpenWA-Bridge/1.0", **headers}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=req_headers,
                                     method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(150_000)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raise ApiFailure("API", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ApiFailure("API") from None


def openwa(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "X-API-Key": OPENWA_KEY,
               "User-Agent": "TenPlus-OpenWA-Bridge/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(OPENWA + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(150_000)
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raise ApiFailure("OpenWA", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ApiFailure("OpenWA") from None


def worker_headers() -> dict[str, str]:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        raise RuntimeError("Dashboard worker token is missing")
    return {"X-Heena-Worker-Token": token}


def dashboard(path: str, payload: dict[str, Any] | None = None) -> Any:
    return http_json(DASHBOARD, path, headers=worker_headers(), payload=payload, timeout=10)


def sessions() -> list[dict[str, Any]]:
    result = openwa("GET", "/api/sessions")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    rows = result.get("sessions") or result.get("data") or [] if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def get_session(*, create: bool = False) -> dict[str, Any] | None:
    for row in sessions():
        if row.get("name") == SESSION_NAME:
            return row
    if not create:
        return None
    row = openwa("POST", "/api/sessions", {"name": SESSION_NAME})
    if not isinstance(row, dict) or not row.get("id"):
        raise ApiFailure("OpenWA")
    return row


def refresh_session(row: dict[str, Any], *, start_if_stopped: bool = True) -> dict[str, Any]:
    session_id = urllib.parse.quote(str(row.get("id") or ""), safe="")
    if not session_id:
        raise ApiFailure("OpenWA")
    current = openwa("GET", f"/api/sessions/{session_id}")
    if not isinstance(current, dict):
        raise ApiFailure("OpenWA")
    if start_if_stopped and not current.get("engineLoaded") and current.get("status") in {
        "created", "disconnected", "failed"
    }:
        try:
            openwa("POST", f"/api/sessions/{session_id}/start", {})
        except ApiFailure as exc:
            # A concurrent auto-start can win the race. Re-read the actual lifecycle state.
            current = openwa("GET", f"/api/sessions/{session_id}")
            if not isinstance(current, dict) or not current.get("engineLoaded"):
                raise exc
        current = openwa("GET", f"/api/sessions/{session_id}")
    return current


def report_state(status: str, row: dict[str, Any] | None = None, error: str = "") -> None:
    row = row or {}
    api_status = str(row.get("status") or "")
    mapped = {
        "ready": "connected",
        "qr_ready": "qr",
        "initializing": "scanning",
        "authenticating": "authenticating",
    }.get(api_status, status)
    account = str(row.get("phone") or "")[:32]
    pushname = str(row.get("pushName") or "")[:80]
    qr_data_url = ""
    if api_status == "qr_ready" and row.get("id"):
        session_id = urllib.parse.quote(str(row["id"]), safe="")
        try:
            qr = openwa("GET", f"/api/sessions/{session_id}/qr")
            qr_data_url = str(qr.get("qrCode") or "")[:120_000] if isinstance(qr, dict) else ""
        except ApiFailure:
            pass
    body = {"status": mapped, "account": account, "pushname": pushname,
            "error": error[:300], "qr_data_url": qr_data_url}
    dashboard("/api/whatsapp/worker/status", body)


def handle_control(command: dict[str, Any], current: dict[str, Any] | None) -> None:
    command_id = str(command.get("id") or "")
    action = str(command.get("action") or "")
    if not command_id:
        return
    try:
        if action == "login":
            row = get_session(create=True)
            if row is None:
                raise ApiFailure("OpenWA")
            row = refresh_session(row)
            detail = "OpenWA login is ready; scan the QR in the dashboard if shown."
        elif action == "logout":
            row = get_session()
            if row is not None:
                session_id = urllib.parse.quote(str(row.get("id") or ""), safe="")
                openwa("POST", f"/api/sessions/{session_id}/logout", {})
            detail = "WhatsApp session logged out."
        else:
            dashboard("/api/whatsapp/worker/control/report", {
                "command_id": command_id, "status": "failed", "detail": "Unsupported command"
            })
            return
        dashboard("/api/whatsapp/worker/control/report", {
            "command_id": command_id, "status": "done", "detail": detail
        })
    except (ApiFailure, RuntimeError) as exc:
        dashboard("/api/whatsapp/worker/control/report", {
            "command_id": command_id, "status": "failed", "detail": str(exc)[:300]
        })


def take_and_send_job() -> None:
    result = dashboard("/api/whatsapp/worker/next")
    job = result.get("job") if isinstance(result, dict) else None
    if not isinstance(job, dict):
        return
    job_id = str(job.get("id") or "")
    try:
        number = "".join(ch for ch in str(job.get("to") or "") if ch.isdigit())
        message = str(job.get("message") or "").strip()
        if not job_id or not 7 <= len(number) <= 15 or not message:
            raise ValueError("Invalid WhatsApp job")
        row = get_session()
        if row is None or row.get("status") != "ready":
            raise ApiFailure("OpenWA", 409)
        session_id = urllib.parse.quote(str(row.get("id") or ""), safe="")
        openwa("POST", f"/api/sessions/{session_id}/messages/send-text", {
            "chatId": number + "@c.us", "text": message[:4096]
        })
        dashboard("/api/whatsapp/worker/report", {
            "job_id": job_id, "status": "sent", "detail": "Delivered to OpenWA"
        })
        print("WhatsApp job sent", flush=True)
    except (ApiFailure, RuntimeError, ValueError) as exc:
        dashboard("/api/whatsapp/worker/report", {
            "job_id": job_id, "status": "failed", "detail": str(exc)[:300]
        })
        print("WhatsApp job failed:", str(exc)[:120], flush=True)


def preflight() -> None:
    if not OPENWA_KEY:
        raise RuntimeError("API_MASTER_KEY is missing in OpenWA .env")
    worker_headers()
    health = http_json(OPENWA, "/api/health", headers={}, timeout=5)
    if not isinstance(health, dict) or health.get("ok") is False:
        raise RuntimeError("OpenWA health check failed")
    # This endpoint is GET-only and does not mutate WhatsApp state.
    status = dashboard("/api/whatsapp/worker/control")
    if not isinstance(status, dict) or status.get("ok") is not True:
        raise RuntimeError("Dashboard worker authentication failed")
    print("OpenWA API: OK")
    print("Shared dashboard worker authentication: OK")
    print("WhatsApp session: not changed; no message sent")


def stop_handler(_signum: int, _frame: Any) -> None:
    global stopping
    stopping = True


def main() -> None:
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    if "--check" in sys.argv:
        preflight()
        return
    preflight()
    print("TenPlus shared OpenWA worker online", flush=True)
    last_heartbeat = 0.0
    last_state_key = ""
    while not stopping:
        try:
            row = get_session()
            if row is not None:
                row = refresh_session(row)
                status = str(row.get("status") or "offline")
                state_key = ":".join((status, str(row.get("phone") or ""), str(row.get("pushName") or "")))
                state_changed = state_key != last_state_key
                if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS or state_changed:
                    report_state("offline", row)
                    last_heartbeat = time.monotonic()
                    last_state_key = state_key
                    if state_changed:
                        print("OpenWA session state changed", status, flush=True)
            elif time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                report_state("offline")
                last_heartbeat = time.monotonic()

            control = dashboard("/api/whatsapp/worker/control")
            command = control.get("command") if isinstance(control, dict) else None
            if isinstance(command, dict):
                handle_control(command, row if row is not None else None)

            # Do not claim queue work until the linked WhatsApp session is ready.
            if row is not None and row.get("status") == "ready":
                take_and_send_job()
        except (ApiFailure, RuntimeError) as exc:
            print("Worker loop error:", str(exc)[:160], flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
