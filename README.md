# realtime_chat
Live chat on Python

## Installation

Install dependencies:
~~~
pip install -r requirements.txt
~~~

## Run server

~~~
python chat_socketio.py
~~~

## Send message via console client

~~~
python chat_socketio.py --client aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
python chat_socketio.py --client bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
~~~

## Send message via HTTP API

You can send messages via HTTP POST request to `/api/send-message`:

~~~bash
curl -X POST http://localhost:8000/api/send-message \
  -H "Content-Type: application/json" \
  -d '{
    "recipientUuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "messageText": "Привет! Это сообщение отправлено через curl!",
    "senderName": "System"
  }'
~~~

**Note:** The recipient must be connected and registered with their UUID for the message to be delivered.

## Random WebSocket chat

`random_chat.py` implements a chat with a random interlocutor. The user registry
and active WebSocket connections are stored in the server process memory and are
lost after a restart.

Create a local `.env` file before starting the server:

```bash
cp .env.example .env
```

Replace the example value with a long random secret:

```dotenv
CHAT_API_KEY=your-long-random-secret
CHAT_USER_TTL_SECONDS=600
```

Generate a cryptographically secure API key with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the printed value into `.env` after `CHAT_API_KEY=`. For example:

```dotenv
CHAT_API_KEY=oYV5m8KpV7jU_c4h3JtF1NzqPLx9Q2s6dA0bWkErTyI
```

Each environment should use its own key. Do not publish it, commit it to Git, or
send it in application messages.

`CHAT_USER_TTL_SECONDS` sets the inactivity timeout in seconds. It must be a
positive integer; `600` is 10 minutes.

The real `.env` file is excluded by `.gitignore`. The server refuses to start if
`CHAT_API_KEY` is missing or empty, or if the timeout is invalid. System
environment variables with the same names take precedence over values from
`.env`.

Start the server:

```bash
python random_chat.py
```

By default, the server is available at `http://localhost:8000`.

For an Ubuntu production deployment with systemd, Nginx, HTTPS, and WSS, see
[DEPLOY.md](DEPLOY.md).

### Connect and get a UUID

Create a user with a generated UUID:

```bash
curl -X POST http://localhost:8000/api/connect \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-long-random-secret" \
  -d '{}'
```

Response:

```json
{
  "uuid": "f79aaf1d-6c89-47ea-96e9-8e45ea113740",
  "requested_uuid_was_occupied": false
}
```

The client can request a specific UUID:

```bash
curl -X POST http://localhost:8000/api/connect \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-long-random-secret" \
  -d '{"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}'
```

If that UUID is already present in memory, the server creates and returns a new
UUID. An invalid UUID produces a `400 Bad Request` response.

### Open a WebSocket connection

After receiving a UUID, keep a WebSocket connection open for incoming messages:

```text
ws://localhost:8000/ws?uuid=f79aaf1d-6c89-47ea-96e9-8e45ea113740&api_key=your-long-random-secret
```

Non-browser clients may send the key in the `X-API-Key` handshake header instead
of the `api_key` query parameter. The header is preferable because query strings
can appear in proxy and server logs. Browser WebSocket clients cannot set custom
handshake headers, so they should use the query parameter over an encrypted
`wss://` connection in production.

The server confirms the connection:

```json
{
  "type": "connected",
  "uuid": "f79aaf1d-6c89-47ea-96e9-8e45ea113740",
  "user_restored": false
}
```

If another socket is opened with the same UUID, it replaces the previous
connection.

### Get a random interlocutor

```bash
curl "http://localhost:8000/api/random-peer?uuid=f79aaf1d-6c89-47ea-96e9-8e45ea113740" \
  -H "X-API-Key: your-long-random-secret"
```

Only users with an active WebSocket connection participate in the selection.
The current user is excluded. If nobody is available, `peer_uuid` is `null`:

```json
{
  "peer_uuid": "29c1e07b-bfc1-4fe6-ae8b-c907309e8df4",
  "user_restored": false
}
```

### Send and receive messages

Send the following JSON through the open WebSocket connection:

```json
{
  "user_uuid": "f79aaf1d-6c89-47ea-96e9-8e45ea113740",
  "peer_uuid": "29c1e07b-bfc1-4fe6-ae8b-c907309e8df4",
  "text": "Hello!"
}
```

`user_uuid` must match the UUID from the WebSocket URL. The recipient immediately
receives the message through their open WebSocket connection:

```json
{
  "type": "message",
  "message_uuid": "b9344420-b7dd-4a67-8442-e6d5a2e430bf",
  "user_uuid": "f79aaf1d-6c89-47ea-96e9-8e45ea113740",
  "peer_uuid": "29c1e07b-bfc1-4fe6-ae8b-c907309e8df4",
  "text": "Hello!"
}
```

After successful delivery, the sender receives an acknowledgement:

```json
{
  "type": "message_sent",
  "message_uuid": "b9344420-b7dd-4a67-8442-e6d5a2e430bf",
  "peer_uuid": "29c1e07b-bfc1-4fe6-ae8b-c907309e8df4",
  "user_restored": false
}
```

If the interlocutor is not connected, the sender receives an event with
`"type": "error"`.

### Get the number of users

```bash
curl http://localhost:8000/api/users/count \
  -H "X-API-Key: your-long-random-secret"
```

Response:

```json
{
  "count": 2
}
```

### Inactivity timeout

A user who makes no requests or sends no WebSocket messages for longer than
`CHAT_USER_TTL_SECONDS` is removed from memory. The default is `600` seconds (10
minutes). Cleanup runs every 30 seconds. If a valid user UUID is no longer found
when that user makes a new request or opens a WebSocket connection, the UUID is
automatically added to the registry again. The `user_restored` response field
indicates when this happened.

### API key errors

All HTTP routes require the `X-API-Key` header. A missing or incorrect key
produces a `401 Unauthorized` response:

```json
{
  "error": "Invalid or missing API key"
}
```

The WebSocket endpoint rejects a handshake with close code `1008` when neither
the header nor the query parameter contains the configured key.
