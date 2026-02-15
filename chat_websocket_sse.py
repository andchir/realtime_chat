"""
Text chat using native WebSocket and Server-Sent Events (SSE).
- WebSocket for bidirectional communication (preferred)
- SSE for receiving messages (alternative to WebSocket)
- HTTP POST for sending messages when using SSE
"""
import uuid
import json
import asyncio
from typing import Dict, Set
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from uvicorn import run


# User registry: uuid -> WebSocket connections and SSE queues
websocket_connections: Dict[str, WebSocket] = {}
sse_queues: Dict[str, asyncio.Queue] = {}


async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for bidirectional communication."""
    await websocket.accept()
    user_uuid = None

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            if event_type == "register":
                # Register user
                user_uuid = data.get("uuid")
                if not user_uuid:
                    await websocket.send_json({
                        "type": "error",
                        "message": "UUID is required"
                    })
                    continue

                try:
                    uuid.UUID(user_uuid)
                except (ValueError, TypeError):
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid UUID"
                    })
                    continue

                # Store connection
                websocket_connections[user_uuid] = websocket
                await websocket.send_json({
                    "type": "registered",
                    "uuid": user_uuid
                })
                print(f"WebSocket: Registered {user_uuid}")

            elif event_type == "message":
                # Send message to peer
                to_uuid = data.get("to_uuid")
                text = data.get("text")

                if not user_uuid:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Register first"
                    })
                    continue

                if not to_uuid or not text:
                    await websocket.send_json({
                        "type": "error",
                        "message": "to_uuid and text are required"
                    })
                    continue

                # Try to deliver via WebSocket
                target_ws = websocket_connections.get(to_uuid)
                if target_ws:
                    try:
                        await target_ws.send_json({
                            "type": "message",
                            "from_uuid": user_uuid,
                            "text": text
                        })
                    except Exception:
                        pass

                # Also deliver via SSE if connected
                target_queue = sse_queues.get(to_uuid)
                if target_queue:
                    await target_queue.put({
                        "type": "message",
                        "from_uuid": user_uuid,
                        "text": text
                    })

                # Check if peer is online
                if not target_ws and not target_queue:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Peer {to_uuid} is not online"
                    })
                else:
                    await websocket.send_json({
                        "type": "message_sent",
                        "to_uuid": to_uuid
                    })

    except WebSocketDisconnect:
        if user_uuid:
            websocket_connections.pop(user_uuid, None)
            print(f"WebSocket: Disconnected {user_uuid}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        if user_uuid:
            websocket_connections.pop(user_uuid, None)


async def sse_endpoint(request: Request):
    """SSE endpoint for receiving messages."""
    user_uuid = request.query_params.get("uuid")

    if not user_uuid:
        return JSONResponse(
            {"error": "UUID parameter is required"},
            status_code=400
        )

    try:
        uuid.UUID(user_uuid)
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "Invalid UUID"},
            status_code=400
        )

    # Create queue for this user
    queue = asyncio.Queue()
    sse_queues[user_uuid] = queue

    async def event_generator():
        try:
            # Send initial connection event
            yield f"data: {json.dumps({'type': 'connected', 'uuid': user_uuid})}\n\n"
            print(f"SSE: Connected {user_uuid}")

            while True:
                # Wait for messages
                message = await queue.get()
                yield f"data: {json.dumps(message)}\n\n"

        except asyncio.CancelledError:
            # Client disconnected
            sse_queues.pop(user_uuid, None)
            print(f"SSE: Disconnected {user_uuid}")
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )


async def send_message_http(request: Request):
    """HTTP POST endpoint to send a message (for SSE clients)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON"},
            status_code=400
        )

    from_uuid = data.get("from_uuid")
    to_uuid = data.get("to_uuid")
    text = data.get("text")

    if not from_uuid or not to_uuid or not text:
        return JSONResponse(
            {"success": False, "error": "from_uuid, to_uuid and text are required"},
            status_code=400
        )

    # Validate UUIDs
    try:
        uuid.UUID(from_uuid)
        uuid.UUID(to_uuid)
    except (ValueError, TypeError):
        return JSONResponse(
            {"success": False, "error": "Invalid UUID"},
            status_code=400
        )

    # Try to deliver via WebSocket
    target_ws = websocket_connections.get(to_uuid)
    delivered = False

    if target_ws:
        try:
            await target_ws.send_json({
                "type": "message",
                "from_uuid": from_uuid,
                "text": text
            })
            delivered = True
        except Exception:
            pass

    # Also deliver via SSE if connected
    target_queue = sse_queues.get(to_uuid)
    if target_queue:
        await target_queue.put({
            "type": "message",
            "from_uuid": from_uuid,
            "text": text
        })
        delivered = True

    if not delivered:
        return JSONResponse(
            {"success": False, "error": f"Peer {to_uuid} is not online"},
            status_code=404
        )

    return JSONResponse({
        "success": True,
        "message": "Message sent successfully"
    })


async def chat_page(request: Request):
    """Serve the chat HTML page."""
    with open("templates/chat_websocket_sse.html", "r") as f:
        content = f.read()
    from starlette.responses import HTMLResponse
    return HTMLResponse(content)


# Routes
routes = [
    Route("/chat", chat_page),
    WebSocketRoute("/ws", websocket_endpoint),
    Route("/sse", sse_endpoint),
    Route("/api/send", send_message_http, methods=["POST"]),
]

app = Starlette(routes=routes)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    print("Starting WebSocket/SSE chat server on http://0.0.0.0:8001")
    print("Open http://localhost:8001/chat in your browser")
    run(app, host="0.0.0.0", port=8001)
