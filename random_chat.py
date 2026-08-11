"""HTTP chat with a random interlocutor and an in-memory user registry.

Run:
    python random_chat.py

All state is process-local and is lost when the server restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


DEFAULT_USER_TTL_SECONDS = 10 * 60
CLEANUP_INTERVAL_SECONDS = 30
API_KEY_ENV_NAME = "CHAT_API_KEY"
USER_TTL_ENV_NAME = "CHAT_USER_TTL_SECONDS"
ENV_FILE = Path(__file__).with_name(".env")


@dataclass
class User:
    last_seen: float
    websocket: WebSocket | None = field(default=None, repr=False)


users: dict[str, User] = {}
users_lock = asyncio.Lock()
user_ttl_seconds = DEFAULT_USER_TTL_SECONDS


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load simple KEY=VALUE pairs without an external dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def configured_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_NAME, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV_NAME} is not configured. Add it to {ENV_FILE}."
        )
    return api_key


def configured_user_ttl_seconds() -> int:
    raw_value = os.environ.get(
        USER_TTL_ENV_NAME, str(DEFAULT_USER_TTL_SECONDS)
    ).strip()
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{USER_TTL_ENV_NAME} must be a positive integer") from exc
    if timeout <= 0:
        raise RuntimeError(f"{USER_TTL_ENV_NAME} must be a positive integer")
    return timeout


def api_key_is_valid(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, configured_api_key())


def authorize_http(request: Request) -> JSONResponse | None:
    if api_key_is_valid(request.headers.get("X-API-Key")):
        return None
    return JSONResponse({"error": "Invalid or missing API key"}, status_code=401)


def parse_uuid(value: object) -> str | None:
    """Return a canonical UUID string or None for an invalid value."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def remove_inactive_users(now: float) -> list[WebSocket]:
    """Remove expired users. Caller must hold users_lock."""
    expired = [
        user_uuid
        for user_uuid, user in users.items()
        if now - user.last_seen > user_ttl_seconds
    ]
    sockets = [users[user_uuid].websocket for user_uuid in expired]
    for user_uuid in expired:
        del users[user_uuid]
    return [socket for socket in sockets if socket is not None]


def touch_user(user_uuid: str, now: float) -> bool:
    """Refresh a user or restore them if they are no longer in memory."""
    user = users.get(user_uuid)
    restored = user is None
    if restored:
        users[user_uuid] = User(last_seen=now)
    else:
        user.last_seen = now
    return restored


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        async with users_lock:
            expired_sockets = remove_inactive_users(time.monotonic())
        for socket in expired_sockets:
            with contextlib.suppress(Exception):
                await socket.close(
                    code=1000,
                    reason=f"Inactive for more than {user_ttl_seconds} seconds",
                )


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    global user_ttl_seconds

    load_dotenv()
    configured_api_key()
    user_ttl_seconds = configured_user_ttl_seconds()
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


async def read_json(request: Request) -> tuple[dict | None, JSONResponse | None]:
    try:
        data = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(data, dict):
        return None, JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    return data, None


async def connect(request: Request) -> JSONResponse:
    """Connect a user. An occupied requested UUID is replaced with a new one."""
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error
    data, error = await read_json(request)
    if error:
        return error

    requested_value = data.get("uuid")
    requested_uuid = parse_uuid(requested_value) if requested_value is not None else None
    if requested_value is not None and requested_uuid is None:
        return JSONResponse({"error": "Invalid uuid"}, status_code=400)

    now = time.monotonic()
    async with users_lock:
        remove_inactive_users(now)
        uuid_was_occupied = requested_uuid in users if requested_uuid else False
        user_uuid = str(uuid.uuid4()) if not requested_uuid or uuid_was_occupied else requested_uuid
        while user_uuid in users:
            user_uuid = str(uuid.uuid4())
        users[user_uuid] = User(last_seen=now)

    return JSONResponse(
        {"uuid": user_uuid, "requested_uuid_was_occupied": uuid_was_occupied},
        status_code=201,
    )


async def connected_count(request: Request) -> JSONResponse:
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error
    async with users_lock:
        remove_inactive_users(time.monotonic())
        count = len(users)
    return JSONResponse({"count": count})


async def random_peer(request: Request) -> JSONResponse:
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error
    user_uuid = parse_uuid(request.query_params.get("uuid"))
    if user_uuid is None:
        return JSONResponse({"error": "Valid uuid query parameter is required"}, status_code=400)

    now = time.monotonic()
    async with users_lock:
        remove_inactive_users(now)
        restored = touch_user(user_uuid, now)
        candidates = [
            candidate
            for candidate, user in users.items()
            if candidate != user_uuid and user.websocket is not None
        ]
        peer_uuid = random.choice(candidates) if candidates else None

    return JSONResponse({"peer_uuid": peer_uuid, "user_restored": restored})


async def websocket_chat(websocket: WebSocket) -> None:
    """Register a socket and relay messages to another connected UUID."""
    supplied_api_key = websocket.headers.get("X-API-Key") or websocket.query_params.get(
        "api_key"
    )
    if not api_key_is_valid(supplied_api_key):
        await websocket.close(code=1008, reason="Invalid or missing API key")
        return

    user_uuid = parse_uuid(websocket.query_params.get("uuid"))
    if user_uuid is None:
        await websocket.close(code=1008, reason="Valid uuid query parameter is required")
        return

    await websocket.accept()
    now = time.monotonic()
    async with users_lock:
        remove_inactive_users(now)
        restored = touch_user(user_uuid, now)
        previous_socket = users[user_uuid].websocket
        users[user_uuid].websocket = websocket

    if previous_socket is not None and previous_socket is not websocket:
        with contextlib.suppress(Exception):
            await previous_socket.close(code=1000, reason="Replaced by a new connection")

    await websocket.send_json(
        {"type": "connected", "uuid": user_uuid, "user_restored": restored}
    )

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except (ValueError, TypeError):
                await websocket.send_json({"type": "error", "error": "Invalid JSON"})
                continue

            request_user_uuid = parse_uuid(data.get("user_uuid")) if isinstance(data, dict) else None
            peer_uuid = parse_uuid(data.get("peer_uuid")) if isinstance(data, dict) else None
            text = data.get("text") if isinstance(data, dict) else None

            if request_user_uuid != user_uuid:
                await websocket.send_json(
                    {"type": "error", "error": "user_uuid must match the socket uuid"}
                )
                continue
            if peer_uuid is None:
                await websocket.send_json({"type": "error", "error": "Valid peer_uuid is required"})
                continue
            if not isinstance(text, str) or not text.strip():
                await websocket.send_json({"type": "error", "error": "Non-empty text is required"})
                continue

            now = time.monotonic()
            async with users_lock:
                remove_inactive_users(now)
                message_restored = touch_user(user_uuid, now)
                users[user_uuid].websocket = websocket
                peer = users.get(peer_uuid)
                target_socket = peer.websocket if peer else None

            if target_socket is None:
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": "Peer is not connected",
                        "peer_uuid": peer_uuid,
                        "user_restored": message_restored,
                    }
                )
                continue

            message_uuid = str(uuid.uuid4())
            try:
                await target_socket.send_json(
                    {
                        "type": "message",
                        "message_uuid": message_uuid,
                        "user_uuid": user_uuid,
                        "peer_uuid": peer_uuid,
                        "text": text,
                    }
                )
            except Exception:
                async with users_lock:
                    peer = users.get(peer_uuid)
                    if peer and peer.websocket is target_socket:
                        peer.websocket = None
                await websocket.send_json(
                    {"type": "error", "error": "Peer connection is unavailable", "peer_uuid": peer_uuid}
                )
                continue

            await websocket.send_json(
                {
                    "type": "message_sent",
                    "message_uuid": message_uuid,
                    "peer_uuid": peer_uuid,
                    "user_restored": message_restored,
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        async with users_lock:
            user = users.get(user_uuid)
            if user and user.websocket is websocket:
                user.websocket = None


routes = [
    Route("/api/connect", connect, methods=["POST"]),
    Route("/api/users/count", connected_count, methods=["GET"]),
    Route("/api/random-peer", random_peer, methods=["GET"]),
    WebSocketRoute("/ws", websocket_chat),
]

app = Starlette(routes=routes, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
