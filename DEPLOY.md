# Cold-Email Research Assistant — Production Deployment

A single container, wired by `docker-compose.yml`, fronted by the shared edge nginx:

```
Browser ──https──► edge nginx :7777 ──/coldemail/──► 127.0.0.1:4766 ──► app (uvicorn :8000)
        (TLS terminated here)        (prefix stripped)        (host)        (container)
```

The FastAPI app serves both the UI (Jinja templates) and the JSON API (`/api/*`).
Behind the edge it runs under the `/coldemail/` prefix; the edge strips that prefix
before forwarding, and the app is told its mount point via `ROOT_PATH=/coldemail`
(set in `docker-compose.yml`) so the UI's `fetch()` calls target `/coldemail/api/*`.

- **Public entrypoint:** `https://ai.arttechgroup.com:7777/coldemail/`
- **Host port:** `4766` (container `8000`) — the edge's upstream target.

> Running standalone (no edge)? Clear `ROOT_PATH` in `docker-compose.yml` (set it to
> empty) and the app serves at `/` directly on `http://localhost:4766/`.

## Prerequisites
- Docker + Docker Compose (`docker compose` v2, or legacy `docker-compose`).
- A `.env` file in the project root, populated with real values (gitignored,
  never baked into the image — injected at runtime via `env_file`). Copy the
  template and fill it in:
  ```bash
  cp .env.example .env
  ```
  The only required secret is `OPENAI_API_KEY`. `APIFY_TOKEN` is required only
  if you use the LinkedIn-fetch feature. See `.env.example` for every knob.

## Run

### One-shot scripts (build → up → health-check)
```bash
./deploy.sh           # Linux/macOS: pull, build, (re)start, wait for health
./deploy.sh --no-pull # deploy the current checkout without git pull
```
```powershell
.\deploy.ps1            # Windows: build, (re)start, wait for health
.\deploy.ps1 -NoPull    # skip git pull
```

### Manual
```bash
docker compose build      # build the image
docker compose up -d      # start detached
docker compose ps         # check status
docker compose logs -f    # tail logs
docker compose down       # stop & remove
```

## Edge nginx wiring
Add these to the edge `server { listen 7777 ssl ... }` block on `ai.arttechgroup.com`
(mirrors the `/storia/` pattern — the prefix is stripped before forwarding):

```nginx
upstream coldemail_app { server 127.0.0.1:4766; }

# inside server { listen 7777 ssl http2; ... }
location = /coldemail { return 301 /coldemail/; }

location /coldemail/ {
    rewrite ^/coldemail/(.*) /$1 break;     # strip /coldemail/ — app serves at root
    proxy_pass http://coldemail_app;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 50M;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```
Reload after editing: `sudo nginx -t && sudo systemctl reload nginx`.

## Smoke test
```bash
# Direct to the container (prefix already stripped, so hit the root paths):
curl http://localhost:4766/api/health    # -> {"ok":true,"openai_key_configured":true}

# Through the edge (the real public path):
curl https://ai.arttechgroup.com:7777/coldemail/api/health   # -> 200
curl -I https://ai.arttechgroup.com:7777/coldemail/          # -> 200 (UI)
curl -I https://ai.arttechgroup.com:7777/coldemail           # -> 301 /coldemail/
```

## Persistent state
Three host directories are bind-mounted so data survives container rebuilds.
They are gitignored and excluded from the image (`.dockerignore`):

| Host path | Container path | Contents |
|-----------|----------------|----------|
| `./kb`    | `/app/kb`      | SQLite DB — sender profiles + app settings (WAL mode) |
| `./logs`  | `/app/logs`    | Per-request JSON logs |
| `./.cache`| `/app/.cache`  | ScrapeGraphAI fetch cache + LinkedIn cache |

To back up, copy these directories. To start fresh, stop the stack and delete them.

## Scaling & timeouts
- The container runs `uvicorn` with **2 workers** (set in the `Dockerfile` `CMD`).
  A research run makes several slow LLM + scrape calls; 2 workers handle a small
  team with headroom. Bump the worker count for more concurrency.
- SQLite is in WAL mode, so concurrent reads/writes across workers are safe.

## TLS / HTTPS
The container serves **plain HTTP** (host `4766`). TLS is terminated at the edge
nginx (`listen 7777 ssl`), which forwards plain HTTP to `127.0.0.1:4766`.

## Gotchas
- **Headless only.** Playwright runs headless in the container. The optional
  "stealth" scrape path (`build_stealth_config`, `headless=False`) needs a display
  and will fail in a plain container — leave it disabled in production, or add an
  `xvfb` wrapper to the `CMD` if you must use it.
- **Missing `.env`.** `docker compose up` fails fast if `.env` is absent because
  `env_file` requires it. The deploy scripts check for it first and tell you what's
  missing.
- **Prefix must match the edge.** `ROOT_PATH` (compose) and the nginx `location`
  prefix must be the same string. The UI bakes `ROOT_PATH` into its `fetch()` base,
  so hitting the container *directly* at `http://localhost:4766/` loads the page but
  its API calls 404 (they target `/coldemail/api/*`, which only the edge resolves).
  That's expected — test the full UI through the edge URL, or clear `ROOT_PATH` to
  run standalone.
