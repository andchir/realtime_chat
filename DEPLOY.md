# Deploying the random chat on Ubuntu

This guide deploys `random_chat.py` behind Nginx and runs it as a systemd
service. Replace `chat.example.com` and the repository URL in the examples with
your own values.

> Important: the application stores users and WebSocket connections in process
> memory. Run exactly **one Uvicorn worker**. Multiple workers would have separate
> user registries and would not be able to route all messages correctly.

## 1. Prepare the server and DNS

Use a non-root sudo user on Ubuntu 22.04 or 24.04. Create an `A`/`AAAA` DNS
record for `chat.example.com` pointing to the server before requesting a TLS
certificate.

Install the required system packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx ufw certbot python3-certbot-nginx
```

Allow SSH before enabling the firewall, then allow HTTP and HTTPS:

```bash
sudo ufw allow OpenSSH
sudo ufw allow "Nginx Full"
sudo ufw enable
sudo ufw status
```

Do not expose Uvicorn port `8000` through the firewall. Only Nginx should be
publicly reachable.

## 2. Create a service user and copy the project

Create a system user that cannot log in interactively:

```bash
sudo useradd --system --user-group --home-dir /opt/realtime_chat --shell /usr/sbin/nologin realtime-chat
sudo install -d -o realtime-chat -g realtime-chat /opt/realtime_chat
```

Clone the repository:

```bash
sudo -u realtime-chat git clone https://example.com/your/realtime_chat.git /opt/realtime_chat
```

For a private repository, upload an archive or use a deployment key instead.
After copying files manually, give the service user ownership:

```bash
sudo chown -R realtime-chat:realtime-chat /opt/realtime_chat
```

## 3. Create the Python environment

```bash
sudo -u realtime-chat python3 -m venv /opt/realtime_chat/venv
sudo -u realtime-chat /opt/realtime_chat/venv/bin/pip install --upgrade pip
sudo -u realtime-chat /opt/realtime_chat/venv/bin/pip install -r /opt/realtime_chat/requirements.txt
```

## 4. Configure `.env`

Generate a cryptographically secure API key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Create the configuration file:

```bash
sudo install -m 600 -o realtime-chat -g realtime-chat /dev/null /opt/realtime_chat/.env
sudoedit /opt/realtime_chat/.env
```

Add the generated key and the inactivity timeout in seconds:

```dotenv
CHAT_API_KEY=replace-this-with-the-generated-key
CHAT_USER_TTL_SECONDS=600
```

The timeout must be a positive integer. Keep `.env` secret; never commit it to
Git. To rotate the API key, change it here and restart the service. Existing
clients must then reconnect with the new key.

## 5. Create the systemd service

Open a new unit file:

```bash
sudoedit /etc/systemd/system/realtime-chat.service
```

Paste the following configuration:

```ini
[Unit]
Description=Random WebSocket chat
After=network.target

[Service]
Type=simple
User=realtime-chat
Group=realtime-chat
WorkingDirectory=/opt/realtime_chat
EnvironmentFile=/opt/realtime_chat/.env
ExecStart=/opt/realtime_chat/venv/bin/uvicorn random_chat:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`--no-access-log` prevents a WebSocket API key supplied in the URL query string
from appearing in Uvicorn access logs.

Load, enable, and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now realtime-chat
sudo systemctl status realtime-chat
```

Test the application locally on the server, substituting the real key:

```bash
curl http://127.0.0.1:8000/api/users/count \
  -H "X-API-Key: your-generated-key"
```

Expected response:

```json
{"count":0}
```

If startup fails, inspect the logs:

```bash
sudo journalctl -u realtime-chat -n 100 --no-pager
```

## 6. Configure Nginx

Create a virtual host:

```bash
sudoedit /etc/nginx/sites-available/realtime-chat
```

Paste this configuration and replace the domain:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name chat.example.com;

    client_max_body_size 1m;

    location /ws {
        # Browser clients put api_key in the query string. Do not log it.
        access_log off;

        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Keep this longer than CHAT_USER_TTL_SECONDS.
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the site and verify the configuration:

```bash
sudo ln -s /etc/nginx/sites-available/realtime-chat /etc/nginx/sites-enabled/realtime-chat
sudo nginx -t
sudo systemctl reload nginx
```

If the default Nginx page conflicts with this host, remove only its enabled
symlink and reload Nginx:

```bash
sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

## 7. Enable HTTPS and WSS

Request a Let's Encrypt certificate after DNS is working and port 80 is
reachable:

```bash
sudo certbot --nginx -d chat.example.com
```

Choose HTTP-to-HTTPS redirection when prompted. Verify automatic renewal:

```bash
sudo certbot renew --dry-run
```

The production endpoints are now:

```text
https://chat.example.com/api/connect
https://chat.example.com/api/users/count
https://chat.example.com/api/random-peer?uuid=USER_UUID
wss://chat.example.com/ws?uuid=USER_UUID&api_key=API_KEY
```

Non-browser WebSocket clients should send `X-API-Key` as a handshake header
instead of putting the key in the URL. Browser clients cannot add arbitrary
WebSocket handshake headers, so they use the `api_key` query parameter and must
connect over `wss://`.

## 8. Verify the public API

Create a user:

```bash
curl -X POST https://chat.example.com/api/connect \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-generated-key" \
  -d '{}'
```

Check the registry count:

```bash
curl https://chat.example.com/api/users/count \
  -H "X-API-Key: your-generated-key"
```

For a browser WebSocket smoke test, open the browser developer console and run:

```javascript
const userUuid = "UUID_RETURNED_BY_CONNECT";
const apiKey = "YOUR_API_KEY";
const socket = new WebSocket(
  `wss://chat.example.com/ws?uuid=${encodeURIComponent(userUuid)}&api_key=${encodeURIComponent(apiKey)}`
);
socket.onmessage = (event) => console.log(JSON.parse(event.data));
socket.onerror = (event) => console.error(event);
```

The first received event should have `"type": "connected"`.

## 9. Deploy updates

Pull the new code, refresh dependencies, and restart the service:

```bash
cd /opt/realtime_chat
sudo -u realtime-chat git pull --ff-only
sudo -u realtime-chat /opt/realtime_chat/venv/bin/pip install -r requirements.txt
sudo systemctl restart realtime-chat
sudo systemctl status realtime-chat
```

Check both application and proxy logs after an update:

```bash
sudo journalctl -u realtime-chat -n 100 --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

Restarting the application clears all users and WebSocket connections because
the registry is intentionally stored only in memory. Clients should implement
reconnection and call `/api/connect` again when necessary.

## Security notes

- Always use HTTPS/WSS outside a trusted local network.
- The configured API key is a shared server credential. It is suitable for
  trusted clients and service-to-service access.
- Do not embed the shared key in a public frontend bundle: every website visitor
  could extract it. A public application should use short-lived per-user tokens
  issued by a separate authentication service instead.
- Query strings may be recorded by CDNs, load balancers, or monitoring systems.
  Disable logging for `/ws` at every proxy layer or use the `X-API-Key` handshake
  header when the client supports it.
- Restrict `.env` to the service account (`chmod 600`) and rotate the key if it
  may have been exposed.
