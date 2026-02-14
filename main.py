"""
Text chat using python-socketio.
Clients register their UUID and can exchange messages with a peer by their UUID.
"""
import asyncio
import uuid
import socketio

sio = socketio.AsyncServer(async_mode="asyncio", cors_allowed_origins="*")

# sid -> user uuid; uuid -> sid (for message routing)
users_by_sid: dict[str, str] = {}
users_by_uuid: dict[str, str] = {}


@sio.event
async def connect(sid, environ, auth):
    print(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    u = users_by_sid.pop(sid, None)
    if u:
        users_by_uuid.pop(u, None)
        print(f"Client disconnected: {sid} (uuid={u})")
    else:
        print(f"Client disconnected: {sid}")


@sio.event
async def register(sid, data):
    """Register current client's UUID. data: {'uuid': '<uuid string>'}"""
    if not data or "uuid" not in data:
        await sio.emit("error", {"message": "uuid is required"}, to=sid)
        return
    u = data["uuid"]
    try:
        uuid.UUID(u)
    except (ValueError, TypeError):
        await sio.emit("error", {"message": "Invalid uuid"}, to=sid)
        return
    users_by_sid[sid] = u
    users_by_uuid[u] = sid
    await sio.emit("registered", {"uuid": u}, to=sid)
    print(f"Registered: sid={sid} -> uuid={u}")


@sio.event
async def message(sid, data):
    """
    Send a message to a peer.
    data: {'to_uuid': '<uuid>', 'text': '<text>'}
    """
    if not data or "to_uuid" not in data or "text" not in data:
        await sio.emit("error", {"message": "to_uuid and text are required"}, to=sid)
        return
    to_uuid = data["to_uuid"]
    text = data["text"]
    from_uuid = users_by_sid.get(sid)
    if not from_uuid:
        await sio.emit("error", {"message": "Call register with your uuid first"}, to=sid)
        return
    target_sid = users_by_uuid.get(to_uuid)
    if not target_sid:
        await sio.emit("error", {"message": f"Peer {to_uuid} is not online"}, to=sid)
        return
    payload = {"from_uuid": from_uuid, "text": text}
    await sio.emit("message", payload, to=target_sid)
    await sio.emit("message_sent", {"to_uuid": to_uuid}, to=sid)


async def run_client(my_uuid: str, peer_uuid: str, server_url: str = "http://localhost:8000"):
    """Console client: registers my_uuid, exchanges messages with peer_uuid."""
    client = socketio.AsyncClient()

    @client.event
    def message(data):
        print(f"\n<< From {data.get('from_uuid', '?')}: {data.get('text', '')}\n> ", end="")

    @client.event
    def error(data):
        print(f"\n!! {data.get('message', 'Error')}\n> ", end="")

    @client.event
    def registered(data):
        print(f"Registered as {data.get('uuid')}. Enter messages (empty line to exit):\n> ", end="")

    await client.connect(server_url)
    await client.emit("register", {"uuid": my_uuid})

    loop = asyncio.get_event_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def feed():
        while True:
            line = input("> ").strip()
            loop.call_soon_threadsafe(q.put_nowait, line if line else None)
            if not line:
                break

    async def send_input():
        while True:
            text = await q.get()
            if text is None:
                break
            await client.emit("message", {"to_uuid": peer_uuid, "text": text})

    t = asyncio.to_thread(feed)
    await asyncio.gather(send_input(), t)
    await client.disconnect()


if __name__ == "__main__":
    import sys
    from uvicorn import run
    from socketio import ASGIApp

    if len(sys.argv) >= 2 and sys.argv[1] == "--client":
        # python main.py --client <my_uuid> <peer_uuid> [url]
        my_uuid = sys.argv[2] if len(sys.argv) > 2 else str(uuid.uuid4())
        peer_uuid = sys.argv[3] if len(sys.argv) > 3 else ""
        url = sys.argv[4] if len(sys.argv) > 4 else "http://localhost:8000"
        if not peer_uuid:
            print("Usage: python main.py --client <my_uuid> <peer_uuid> [url]")
            sys.exit(1)
        asyncio.run(run_client(my_uuid, peer_uuid, url))
    else:
        app = ASGIApp(sio)
        run(app, host="0.0.0.0", port=8000)
