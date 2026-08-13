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
    gender: str | None = None
    age: int | None = None
    desired_gender: str = "any"
    desired_age_over: int = 0
    desired_age_under: int = 121
    matched_peer_uuids: set[str] = field(default_factory=set, repr=False)
    active_pair_uuid: str | None = None
    websocket: WebSocket | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Pair:
    first_user_uuid: str
    second_user_uuid: str
    created_at: float

    def other_user_uuid(self, user_uuid: str) -> str | None:
        if user_uuid == self.first_user_uuid:
            return self.second_user_uuid
        if user_uuid == self.second_user_uuid:
            return self.first_user_uuid
        return None


@dataclass
class CleanupActions:
    expired_sockets: list[WebSocket] = field(default_factory=list)
    timeout_notifications: list[tuple[WebSocket, str, str]] = field(
        default_factory=list
    )


users: dict[str, User] = {}
pairs: dict[str, Pair] = {}
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
            f"{API_KEY_ENV_NAME} не настроен. Добавьте его в {ENV_FILE}."
        )
    return api_key


def configured_user_ttl_seconds() -> int:
    raw_value = os.environ.get(
        USER_TTL_ENV_NAME, str(DEFAULT_USER_TTL_SECONDS)
    ).strip()
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{USER_TTL_ENV_NAME} должен быть положительным целым числом"
        ) from exc
    if timeout <= 0:
        raise RuntimeError(
            f"{USER_TTL_ENV_NAME} должен быть положительным целым числом"
        )
    return timeout


def api_key_is_valid(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, configured_api_key())


def authorize_http(request: Request) -> JSONResponse | None:
    if api_key_is_valid(request.headers.get("X-API-Key")):
        return None
    return JSONResponse(
        {
            "status": "disconnected",
            "error": "Неверный или отсутствующий API-ключ",
        },
        status_code=401,
    )


def parse_uuid(value: object) -> str | None:
    """Return a canonical UUID string or None for an invalid value."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def parse_profile(data: dict) -> tuple[dict[str, str | int] | None, str | None]:
    def integer_from_string(value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return int(value.strip())
        except ValueError:
            return value

    gender = data.get("gender")
    desired_gender = data.get("desired_gender")
    age = integer_from_string(data.get("age"))
    desired_age_over = integer_from_string(data.get("desired_age_over"))
    desired_age_under = integer_from_string(data.get("desired_age_under"))

    if isinstance(gender, str):
        gender = gender.strip().lower()
    if isinstance(desired_gender, str):
        desired_gender = desired_gender.strip().lower()

    if gender not in {"male", "female"}:
        return None, "Пол должен быть 'male' или 'female'"
    if desired_gender not in {"male", "female", "any"}:
        return None, "Пол собеседника должен быть 'male', 'female' или 'any'"
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 120:
        return None, "Возраст должен быть целым числом от 1 до 120"
    if (
        isinstance(desired_age_over, bool)
        or not isinstance(desired_age_over, int)
        or not 0 <= desired_age_over < 120
    ):
        return None, "Возраст собеседника должен быть целым числом от 0 до 119"
    if (
        isinstance(desired_age_under, bool)
        or not isinstance(desired_age_under, int)
        or not 2 <= desired_age_under <= 121
    ):
        return None, "Возраст собеседника должен быть целым числом от 2 до 121"
    if desired_age_over >= desired_age_under:
        return None, "Некорректные пределы возраста собеседника"

    return {
        "gender": gender,
        "age": age,
        "desired_gender": desired_gender,
        "desired_age_over": desired_age_over,
        "desired_age_under": desired_age_under,
    }, None


def profiles_are_compatible(first: User, second: User) -> bool:
    """Return True when both users satisfy each other's preferences."""
    if first.gender is None or first.age is None:
        return False
    if second.gender is None or second.age is None:
        return False
    first_accepts_second = (
        first.desired_gender in {"any", second.gender}
        and second.age > first.desired_age_over
        and second.age < first.desired_age_under
    )
    second_accepts_first = (
        second.desired_gender in {"any", first.gender}
        and first.age > second.desired_age_over
        and first.age < second.desired_age_under
    )
    return first_accepts_second and second_accepts_first


def remove_inactive_users(now: float) -> CleanupActions:
    """Remove expired users. Caller must hold users_lock."""
    expired = [
        user_uuid
        for user_uuid, user in users.items()
        if now - user.last_seen > user_ttl_seconds
    ]
    actions = CleanupActions(
        expired_sockets=[
            users[user_uuid].websocket
            for user_uuid in expired
            if users[user_uuid].websocket is not None
        ]
    )
    expired_set = set(expired)
    for pair_uuid, pair in list(pairs.items()):
        pair_users = {pair.first_user_uuid, pair.second_user_uuid}
        if pair_users.isdisjoint(expired_set):
            continue
        del pairs[pair_uuid]
        for participant_uuid in pair_users:
            participant = users.get(participant_uuid)
            if participant and participant.active_pair_uuid == pair_uuid:
                participant.active_pair_uuid = None
            if participant and participant.websocket is not None:
                status = (
                    "disconnected"
                    if participant_uuid in expired_set
                    else "connected"
                )
                actions.timeout_notifications.append(
                    (participant.websocket, pair_uuid, status)
                )

    for user_uuid in expired:
        del users[user_uuid]
    if expired:
        for user in users.values():
            user.matched_peer_uuids.difference_update(expired_set)
    return actions


def end_pair_locked(pair_uuid: str, departing_user_uuid: str) -> WebSocket | None:
    """End a pair and return the other participant's socket for notification."""
    pair = pairs.pop(pair_uuid, None)
    if pair is None:
        return None
    other_user_uuid = pair.other_user_uuid(departing_user_uuid)
    for participant_uuid in (pair.first_user_uuid, pair.second_user_uuid):
        participant = users.get(participant_uuid)
        if participant and participant.active_pair_uuid == pair_uuid:
            participant.active_pair_uuid = None
    other_user = users.get(other_user_uuid) if other_user_uuid else None
    return other_user.websocket if other_user else None


def touch_user(user_uuid: str, now: float) -> bool:
    """Refresh a user or restore them if they are no longer in memory."""
    user = users.get(user_uuid)
    restored = user is None
    if restored:
        users[user_uuid] = User(last_seen=now)
    else:
        user.last_seen = now
    return restored


def current_user_status(user_uuid: str) -> str:
    """Return the user's current chat state."""
    user = users.get(user_uuid)
    if user is None:
        return "disconnected"
    if user.active_pair_uuid is not None and user.active_pair_uuid in pairs:
        return "paired"
    return "connected"


async def perform_cleanup_actions(actions: CleanupActions) -> None:
    for socket, pair_uuid, status in actions.timeout_notifications:
        with contextlib.suppress(Exception):
            await socket.send_json(
                {
                    "status": status,
                    "type": "system",
                    "event": "pair_timeout",
                    "pair_uuid": pair_uuid,
                    "message": "Пара закрыта из-за неактивности одного из собеседников",
                }
            )
    for socket in actions.expired_sockets:
        with contextlib.suppress(Exception):
            await socket.close(
                code=1000,
                reason=f"Нет активности более {user_ttl_seconds} секунд",
            )


async def purge_inactive_users(now: float) -> None:
    async with users_lock:
        actions = remove_inactive_users(now)
    await perform_cleanup_actions(actions)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await purge_inactive_users(time.monotonic())


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
        return None, JSONResponse(
            {"status": "disconnected", "error": "Некорректный JSON"},
            status_code=400,
        )
    if not isinstance(data, dict):
        return None, JSONResponse(
            {
                "status": "disconnected",
                "error": "Тело JSON должно быть объектом",
            },
            status_code=400,
        )
    return data, None


async def connect(request: Request) -> JSONResponse:
    """Connect a user. An occupied requested UUID is replaced with a new one."""
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error
    data, error = await read_json(request)
    if error:
        return error

    profile, profile_error = parse_profile(data)
    if profile_error:
        return JSONResponse(
            {"status": "disconnected", "error": profile_error},
            status_code=400,
        )
    assert profile is not None

    requested_value = data.get("uuid")
    requested_uuid = parse_uuid(requested_value) if requested_value is not None else None
    if requested_value is not None and requested_uuid is None:
        return JSONResponse(
            {"status": "disconnected", "error": "Некорректный UUID"},
            status_code=400,
        )

    now = time.monotonic()
    await purge_inactive_users(now)
    async with users_lock:
        uuid_was_occupied = requested_uuid in users if requested_uuid else False
        user_uuid = str(uuid.uuid4()) if not requested_uuid or uuid_was_occupied else requested_uuid
        while user_uuid in users:
            user_uuid = str(uuid.uuid4())
        users[user_uuid] = User(last_seen=now, **profile)

    return JSONResponse(
        {
            "status": "connected",
            "uuid": user_uuid,
            "requested_uuid_was_occupied": uuid_was_occupied,
            **profile,
        },
        status_code=201,
    )


async def connected_count(request: Request) -> JSONResponse:
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error
    await purge_inactive_users(time.monotonic())
    async with users_lock:
        men = sum(user.gender == "male" for user in users.values())
        women = sum(user.gender == "female" for user in users.values())
        total = len(users)
    return JSONResponse(
        {"status": "connected", "men": men, "women": women, "total": total}
    )


async def random_peer(request: Request) -> JSONResponse:
    authorization_error = authorize_http(request)
    if authorization_error:
        return JSONResponse(
            {
                "status": "disconnected",
                "error": "Неверный или отсутствующий API-ключ",
            },
            status_code=401,
        )
    user_uuid = parse_uuid(request.query_params.get("uuid"))
    if user_uuid is None:
        return JSONResponse(
            {
                "status": "disconnected",
                "error": "Требуется корректный параметр uuid",
            },
            status_code=400,
        )

    now = time.monotonic()
    await purge_inactive_users(now)
    async with users_lock:
        restored = touch_user(user_uuid, now)
        current_user = users[user_uuid]
        if current_user.gender is None or current_user.age is None:
            return JSONResponse(
                {
                    "status": "connected",
                    "reason": "profile_missing",
                    "error": "Профиль пользователя отсутствует. Необходимо подключение.",
                },
                status_code=409,
            )
        if current_user.websocket is None:
            return JSONResponse(
                {
                    "status": "connected",
                    "reason": "websocket_not_connected",
                    "error": "Перед поиском откройте WebSocket пользователя",
                },
                status_code=409,
            )
        if current_user.active_pair_uuid is not None:
            return JSONResponse(
                {
                    "status": "paired",
                    "pair_uuid": current_user.active_pair_uuid,
                    "already_paired": True,
                    "user_restored": restored,
                }
            )
        candidates = [
            candidate
            for candidate, user in users.items()
            if candidate != user_uuid
            and user.websocket is not None
            and user.active_pair_uuid is None
            and candidate not in current_user.matched_peer_uuids
            and user_uuid not in user.matched_peer_uuids
            and profiles_are_compatible(current_user, user)
        ]
        peer_uuid = random.choice(candidates) if candidates else None
        if peer_uuid is None:
            return JSONResponse(
                {
                    "status": "connected",
                    "reason": "not_found",
                    "pair_uuid": None,
                    "user_restored": restored,
                }
            )

        pair_uuid = str(uuid.uuid4())
        peer = users[peer_uuid]
        pairs[pair_uuid] = Pair(
            first_user_uuid=user_uuid,
            second_user_uuid=peer_uuid,
            created_at=now,
        )
        current_user.active_pair_uuid = pair_uuid
        peer.active_pair_uuid = pair_uuid
        current_user.matched_peer_uuids.add(peer_uuid)
        peer.matched_peer_uuids.add(user_uuid)
        current_socket = current_user.websocket
        peer_socket = peer.websocket

    pair_event = {"status": "paired", "type": "paired", "pair_uuid": pair_uuid}
    try:
        await peer_socket.send_json(pair_event)
    except Exception:
        async with users_lock:
            peer = users.get(peer_uuid)
            if peer and peer.websocket is peer_socket:
                peer.websocket = None
            end_pair_locked(pair_uuid, peer_uuid)
        return JSONResponse(
            {
                "status": "connected",
                "reason": "peer_disconnected",
                "error": "Выбранный собеседник отключился",
            },
            status_code=409,
        )

    if current_socket is not None:
        with contextlib.suppress(Exception):
            await current_socket.send_json(pair_event)

    return JSONResponse(
        {
            "status": "paired",
            "pair_uuid": pair_uuid,
            "already_paired": False,
            "user_restored": restored,
        }
    )


async def leave_pair(request: Request) -> JSONResponse:
    """End the user's active dialog and notify the other participant."""
    authorization_error = authorize_http(request)
    if authorization_error:
        return authorization_error

    data, error = await read_json(request)
    if error:
        return error
    assert data is not None

    user_uuid = parse_uuid(data.get("uuid"))
    if user_uuid is None:
        return JSONResponse(
            {"status": "disconnected", "error": "Требуется корректный uuid"},
            status_code=400,
        )

    pair_uuid = parse_uuid(data.get("pair_uuid"))
    if pair_uuid is None:
        async with users_lock:
            status = current_user_status(user_uuid)
        return JSONResponse(
            {
                "status": status,
                "error": "Требуется корректный pair_uuid",
            },
            status_code=400,
        )

    now = time.monotonic()
    await purge_inactive_users(now)
    async with users_lock:
        user = users.get(user_uuid)
        pair_is_active = bool(
            user
            and user.active_pair_uuid == pair_uuid
            and pair_uuid in pairs
            and pairs[pair_uuid].other_user_uuid(user_uuid) is not None
        )
        if pair_is_active:
            user.last_seen = now
            partner_socket = end_pair_locked(pair_uuid, user_uuid)
        else:
            partner_socket = None
        status = current_user_status(user_uuid)

    if not pair_is_active:
        return JSONResponse(
            {
                "status": status,
                "error": "Пара неактивна",
                "pair_uuid": pair_uuid,
            },
            status_code=409,
        )

    if partner_socket is not None:
        with contextlib.suppress(Exception):
            await partner_socket.send_json(
                {
                    "status": "connected",
                    "type": "peer_disconnected",
                    "pair_uuid": pair_uuid,
                    "message": "Собеседник завершил диалог",
                    "text": "[Собеседник завершил диалог]",
                }
            )

    return JSONResponse(
        {
            "status": "connected",
            "pair_uuid": pair_uuid,
            "dialog_ended": True,
        }
    )


async def websocket_chat(websocket: WebSocket) -> None:
    """Register a socket and relay messages only within an active pair."""
    supplied_api_key = websocket.headers.get("X-API-Key") or websocket.query_params.get(
        "api_key"
    )
    if not api_key_is_valid(supplied_api_key):
        await websocket.close(code=1008, reason="Неверный или отсутствующий API-ключ")
        return

    user_uuid = parse_uuid(websocket.query_params.get("uuid"))
    if user_uuid is None:
        await websocket.close(code=1008, reason="Требуется корректный UUID пользователя")
        return

    await websocket.accept()
    now = time.monotonic()
    await purge_inactive_users(now)
    async with users_lock:
        restored = touch_user(user_uuid, now)
        previous_socket = users[user_uuid].websocket
        users[user_uuid].websocket = websocket
        status = current_user_status(user_uuid)

    if previous_socket is not None and previous_socket is not websocket:
        with contextlib.suppress(Exception):
            await previous_socket.close(code=1000, reason="Соединение заменено новым")

    await websocket.send_json(
        {
            "status": status,
            "type": "connected",
            "uuid": user_uuid,
            "user_restored": restored,
        }
    )

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except (ValueError, TypeError):
                await websocket.send_json(
                    {
                        "status": current_user_status(user_uuid),
                        "type": "error",
                        "error": "Некорректный JSON",
                    }
                )
                continue

            event_type = data.get("type", "message") if isinstance(data, dict) else None
            pair_uuid = parse_uuid(data.get("pair_uuid")) if isinstance(data, dict) else None

            if pair_uuid is None:
                await websocket.send_json(
                    {
                        "status": current_user_status(user_uuid),
                        "type": "error",
                        "error": "Требуется корректный pair_uuid",
                    }
                )
                continue

            now = time.monotonic()
            await purge_inactive_users(now)

            if event_type == "leave_pair":
                async with users_lock:
                    message_restored = touch_user(user_uuid, now)
                    users[user_uuid].websocket = websocket
                    user = users[user_uuid]
                    if user.active_pair_uuid != pair_uuid or pair_uuid not in pairs:
                        partner_socket = None
                        pair_was_active = False
                    else:
                        partner_socket = end_pair_locked(pair_uuid, user_uuid)
                        pair_was_active = True

                if not pair_was_active:
                    await websocket.send_json(
                        {
                            "status": current_user_status(user_uuid),
                            "type": "error",
                            "error": "Пара неактивна",
                            "pair_uuid": pair_uuid,
                        }
                    )
                    continue
                if partner_socket is not None:
                    with contextlib.suppress(Exception):
                        await partner_socket.send_json(
                            {
                                "status": "connected",
                                "type": "peer_disconnected",
                                "pair_uuid": pair_uuid,
                                "message": "Собеседник завершил диалог",
                                "text": "[Собеседник завершил диалог]",
                            }
                        )
                await websocket.send_json(
                    {
                        "status": "connected",
                        "type": "pair_left",
                        "pair_uuid": pair_uuid,
                        "user_restored": message_restored,
                    }
                )
                continue

            if event_type != "message":
                await websocket.send_json(
                    {
                        "status": current_user_status(user_uuid),
                        "type": "error",
                        "error": "Неподдерживаемый тип события",
                    }
                )
                continue

            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(text, str) or not text.strip():
                await websocket.send_json(
                    {
                        "status": current_user_status(user_uuid),
                        "type": "error",
                        "error": "Требуется непустой текст сообщения",
                    }
                )
                continue

            async with users_lock:
                message_restored = touch_user(user_uuid, now)
                users[user_uuid].websocket = websocket
                user = users[user_uuid]
                pair = pairs.get(pair_uuid)
                other_user_uuid = pair.other_user_uuid(user_uuid) if pair else None
                other_user = users.get(other_user_uuid) if other_user_uuid else None
                pair_is_active = bool(
                    pair
                    and user.active_pair_uuid == pair_uuid
                    and other_user
                    and other_user.active_pair_uuid == pair_uuid
                )
                target_socket = other_user.websocket if pair_is_active and other_user else None
                if pair_is_active and target_socket is None:
                    end_pair_locked(pair_uuid, user_uuid)

            if target_socket is None:
                await websocket.send_json(
                    {
                        "status": current_user_status(user_uuid),
                        "type": "error",
                        "error": "Пара неактивна",
                        "pair_uuid": pair_uuid,
                        "user_restored": message_restored,
                    }
                )
                continue

            message_uuid = str(uuid.uuid4())
            try:
                await target_socket.send_json(
                    {
                        "status": "paired",
                        "type": "message",
                        "message_uuid": message_uuid,
                        "pair_uuid": pair_uuid,
                        "text": text,
                    }
                )
            except Exception:
                async with users_lock:
                    pair = pairs.get(pair_uuid)
                    other_user_uuid = pair.other_user_uuid(user_uuid) if pair else None
                    other_user = users.get(other_user_uuid) if other_user_uuid else None
                    if other_user and other_user.websocket is target_socket:
                        other_user.websocket = None
                    end_pair_locked(pair_uuid, user_uuid)
                await websocket.send_json(
                    {
                        "status": "connected",
                        "type": "error",
                        "error": "Соединение с парой недоступно",
                        "text": "[Соединение с парой недоступно]",
                        "pair_uuid": pair_uuid,
                    }
                )
                continue

            # A successfully delivered message is activity for the dialog as a
            # whole. Refresh both participants so a user who is reading and
            # replying less often is not expired while messages are still being
            # exchanged in the pair.
            delivered_at = time.monotonic()
            async with users_lock:
                pair = pairs.get(pair_uuid)
                if pair is not None:
                    for participant_uuid in (
                        pair.first_user_uuid,
                        pair.second_user_uuid,
                    ):
                        participant = users.get(participant_uuid)
                        if participant is not None:
                            participant.last_seen = delivered_at

            await websocket.send_json(
                {
                    "status": "paired",
                    "type": "message_sent",
                    "message_uuid": message_uuid,
                    "pair_uuid": pair_uuid,
                    "user_restored": message_restored,
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        partner_socket = None
        disconnected_pair_uuid = None
        async with users_lock:
            user = users.get(user_uuid)
            if user and user.websocket is websocket:
                user.websocket = None
                disconnected_pair_uuid = user.active_pair_uuid
                if disconnected_pair_uuid is not None:
                    partner_socket = end_pair_locked(
                        disconnected_pair_uuid, user_uuid
                    )
        if partner_socket is not None and disconnected_pair_uuid is not None:
            with contextlib.suppress(Exception):
                await partner_socket.send_json(
                    {
                        "status": "connected",
                        "type": "peer_disconnected",
                        "pair_uuid": disconnected_pair_uuid,
                        "message": "Соединение с собеседником разорвано",
                        "text": "[Соединение с собеседником разорвано]",
                    }
                )


routes = [
    Route("/api/connect", connect, methods=["POST"]),
    Route("/api/users/count", connected_count, methods=["GET"]),
    Route("/api/random-peer", random_peer, methods=["GET"]),
    Route("/api/leave-pair", leave_pair, methods=["POST"]),
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
