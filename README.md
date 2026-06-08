# Cold-Email Research Assistant

A single-page internal tool for the Art & Storia marketing teams. Paste a company URL, optionally the contact's LinkedIn URL, pick a sender profile and intent, and the app returns recent company news, recent LinkedIn posts, and a draft cold email anchored on the freshest hook — in 30–90 seconds per company.

## Table of contents

- [Prerequisites](#prerequisites)
- [Quick start (Docker)](#quick-start-docker)
- [Local development (no Docker)](#local-development-no-docker)
- [Configuration — the `.env` file](#configuration--the-env-file)
- [Running the tests](#running-the-tests)
- [Diagnostics](#diagnostics)
- [API reference](#api-reference)
- [Deploying to a company server](#deploying-to-a-company-server)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

You need **one** of these, depending on how you want to run it:

| To run via | You need |
|---|---|
| Docker (recommended) | Docker Desktop or Docker Engine + `docker compose` |
| Local Python | Python 3.10 or newer + `pip` |

You will also need:

- An **OpenAI API key** (`sk-...`) — required, the app refuses to start without it.
- An **Apify token** — optional. Skip it if you don't want LinkedIn scraping; the rest still works.

---

## Quick start (Docker)

This is the simplest path. The Dockerfile uses the official Playwright image, so Chromium and all its system dependencies are pre-installed.

```bash
# 1. clone the repo
git clone <repo-url>
cd scrapegraph_usecase

# 2. create your .env from the template and fill in your keys
cp .env.example .env
# open .env in your editor; set OPENAI_API_KEY and APIFY_TOKEN

# 3. start the app (detached, with auto-restart on crash/reboot)
docker compose up -d --build

# 4. tail logs to confirm it started
docker compose logs -f
```

Open http://localhost:8000 in your browser. You should see the dark "Cold-Email Research Assistant" UI.

To stop the app: `docker compose down`. To rebuild after editing `requirements.txt` or the Dockerfile: `docker compose up -d --build --force-recreate`.

The `docker-compose.yml` mounts `./kb/`, `./logs/`, and `./.cache/` as volumes, so your SQLite database, audit logs, and scrape cache survive `docker compose down`.

---

## Local development (no Docker)

Use this if you want to edit the code and see changes immediately with auto-reload.

### One-time setup

```bash
# 1. create and activate a virtual environment
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. install Chromium for Playwright (one-time, ~150 MB)
playwright install chromium

# 4. create your .env
cp .env.example .env
# fill in OPENAI_API_KEY and APIFY_TOKEN
```

### Run with auto-reload (development)

```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Browse to http://127.0.0.1:8000 . Edit files under `src/` or `templates/` — the server restarts itself.

### Run without auto-reload (faster, closer to prod)

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
```

`--workers 2` runs two worker processes, matching the Docker config. SQLite WAL mode handles the concurrent writes safely.

### Production-style command (matches Dockerfile)

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers --forwarded-allow-ips='*'
```

Use `--proxy-headers` if you'll be putting Nginx or another reverse proxy in front of it.

---

## Configuration — the `.env` file

Copy [`.env.example`](.env.example) to `.env` and fill in your keys. The full set of variables:

### Required

| Variable | What it is |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (`sk-...`). The app refuses to start without it. |

### Required if you want LinkedIn fetching

| Variable | What it is |
|---|---|
| `APIFY_TOKEN` | Your Apify API token. Leave blank to disable LinkedIn entirely. |
| `APIFY_LINKEDIN_ACTOR` | Apify actor ID for the LinkedIn scraper. Default is fine. |
| `APIFY_LINKEDIN_POST_LIMIT` | How many posts to fetch per profile. Default `2`. |

### Optional behaviour knobs

| Variable | Default | What it controls |
|---|---|---|
| `LLM_MODEL` | `openai/gpt-4o-mini` | Which OpenAI model to use for extraction + drafting. |
| `RECENCY_DAYS` | `60` | Drop news / posts older than this. |
| `MAX_NEWS_ITEMS` | `8` | Cap on news items returned per company. |
| `SEARCH_MAX_RESULTS` | `5` | DuckDuckGo result count per query. |
| `DISABLE_WEB_SEARCH` | `true` | Default off — flip on per-request via the UI's Advanced section. |
| `DISABLE_LINKEDIN` | `false` | Force-skip LinkedIn even when a URL is provided. |
| `REQUIRE_CONTEXT_FOR_EMAIL` | `false` | Skip the email-drafting call if no news or LinkedIn posts were found (saves money on dead-end runs). |
| `LINKEDIN_CACHE_HOURS` | `24` | How long the Apify response stays cached on disk. |

These can also be edited live in the UI under "Advanced settings → Save as defaults" — settings persist in SQLite and are shared across all teammates hitting the same server.

### Playwright tuning (rarely needed)

`SCRAPEGRAPH_PROXY_SERVER`, `SCRAPEGRAPH_LOAD_STATE`, `SCRAPEGRAPH_RETRY_LIMIT`, `SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT` — defaults in `.env.example` work for most sites. See comments in that file if a particular site is failing.

---

## Running the tests

There are 234 unit tests covering extraction, ranking, LinkedIn filtering, settings persistence, etc.

```bash
pytest                  # all tests, quiet mode
pytest tests/test_extractor.py     # one file
pytest -k "linkedin"    # only tests with "linkedin" in the name
pytest --lf             # only re-run failures from last time
```

Pytest config lives in [`pyproject.toml`](pyproject.toml). Tests don't hit live APIs — they use stubbed responses, so they're fast and offline-safe.

---

## Diagnostics

### Health check

```bash
curl http://localhost:8000/api/health
# expected: {"ok": true, "openai_key_configured": true}
```

If `openai_key_configured` is `false`, your `.env` isn't being picked up.

### LinkedIn smoke test

[`check_apify.py`](check_apify.py) is a standalone script that calls the Apify actor in isolation — no FastAPI, no LLM, no Playwright. Use it to confirm your `APIFY_TOKEN` and actor are working:

```bash
python check_apify.py https://www.linkedin.com/in/satyanadella
python check_apify.py https://uk.linkedin.com/in/some-other-profile 5    # fetch 5 posts
```

It prints what it found, what got dropped (and why), and the total Apify cost.

---

## API reference

Useful if you want to integrate the tool with HubSpot, Bigin, n8n, etc.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | Serves the HTML UI |
| `GET` | `/api/health` | Liveness probe — returns `{ok, openai_key_configured}` |
| `POST` | `/api/research` | Run the full pipeline. Body: company URL + options. Returns news, LinkedIn posts, draft email. |
| `GET` | `/api/sender-profiles` | List all sender profiles |
| `POST` | `/api/sender-profiles` | Create a sender profile |
| `PUT` | `/api/sender-profiles/{id}` | Update a sender profile |
| `DELETE` | `/api/sender-profiles/{id}` | Delete a sender profile |
| `GET` | `/api/settings` | Get the current shared settings |
| `PUT` | `/api/settings` | Update one or more settings (partial) |
| `POST` | `/api/settings/reset` | Reset shared settings to `.env` defaults |

For the request/response schemas of each endpoint, see [`app.py`](app.py) — every Pydantic model is defined at the top of the file.

---

## Deploying to a company server

This is the same as the Docker quick start, just on a server.

### One-time on the server

```bash
# install Docker + docker-compose if not already there
# (Ubuntu / Debian example)
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin

# clone the repo
git clone <repo-url> /opt/cold-email-research
cd /opt/cold-email-research

# create .env with production keys
cp .env.example .env
nano .env

# start it
docker compose up -d --build
```

### What you get for free with `docker compose up -d`

- **Auto-restart** on crash or server reboot (`restart: unless-stopped`)
- **Persistent SQLite, logs, and scrape cache** via the three volume mounts in `docker-compose.yml`
- **Health checks** every 30 seconds — Docker marks the container unhealthy if `/api/health` stops responding
- **Two workers** sharing port 8000 — handles ~10 concurrent teammates with headroom

### Putting it behind Nginx (optional but recommended for company servers)

If you want TLS or a friendly URL like `tools.yourcompany.com/cold-email`, put Nginx in front:

```nginx
server {
    listen 443 ssl http2;
    server_name tools.yourcompany.com;

    # ... TLS config ...

    location /cold-email/ {
        proxy_pass http://localhost:8000/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

In `docker-compose.yml`, change the port mapping to `"127.0.0.1:8000:8000"` so the container only listens on localhost (Nginx is the only client).

### Network requirements

The server needs outbound access to:
- `api.openai.com` (LLM calls)
- `api.apify.com` (LinkedIn fetches)
- General internet (for `httpx` + `Playwright` to fetch prospect company sites and DuckDuckGo search)

If your company firewall blocks outbound traffic, IT needs to allowlist these.

### Backups

The only stateful thing worth backing up is `kb/app.db` — it holds sender profiles and shared app settings. Two options:

- **Filesystem snapshot**: include `/opt/cold-email-research/kb/` in your existing backup job.
- **SQLite dump**: `docker compose exec app sqlite3 /app/kb/app.db .dump > backup.sql` (run periodically via cron).

`logs/` and `.cache/` are not worth backing up — they're regenerated on the fly.

---

## Project layout

```
scrapegraph_usecase/
├── app.py                      # FastAPI entry point + API routes
├── check_apify.py              # standalone LinkedIn smoke test
├── docker-compose.yml          # production-ready orchestration
├── Dockerfile                  # Playwright base image + uvicorn
├── requirements.txt            # single source of truth for dependencies
├── pyproject.toml              # pytest config (nothing else)
├── .env.example                # template — copy to .env and fill in
├── src/                        # application code
│   ├── pipeline.py             # the orchestrator (start here)
│   ├── extractor.py            # httpx + Trafilatura + LLM news extraction
│   ├── search.py               # DuckDuckGo web-search path
│   ├── linkedin.py             # Apify LinkedIn fetch + cache
│   ├── linkedin_filter.py      # LLM-based scoring of LinkedIn posts
│   ├── relevance.py            # deterministic ranking heuristics
│   ├── recency.py              # date filtering
│   ├── email_writer.py         # LLM email drafting (with reasoning fields)
│   ├── sender_profiles.py      # profile CRUD
│   ├── db.py                   # SQLite engine, schema, auto-migration
│   ├── settings.py             # shared app-settings persistence
│   ├── cost_guard.py           # token counting + $0.20/req hard cap
│   ├── schemas.py              # Pydantic models used across the app
│   ├── config.py               # .env loading + defaults
│   ├── http_headers.py         # rotating user agents
│   ├── company.py              # company-site URL discovery
│   ├── rss.py                  # optional RSS path
│   └── query_log.py            # per-request JSON audit log
├── templates/
│   └── index.html              # the single-page UI (Jinja + vanilla JS)
├── tests/                      # 234 pytest unit tests
├── scripts/                    # utility scripts (dev-time, not deployed)
│   ├── build_combined_deck.py  # builds the demo PPTX
│   ├── capture_demo_screenshots.py
│   ├── cost_report.py          # roll-up cost reporting across runs
│   ├── render_pptx_to_png.ps1
│   └── scrape_batch.py         # batch eval over a list of company URLs
├── docs/                       # internal notes on ScrapeGraphAI etc.
└── (gitignored) .env, .venv/, kb/, logs/, .cache/, output/
```

---

## Troubleshooting

### The browser shows a blank page or 404

Something else is on port 8000. Check with:

```powershell
# Windows
netstat -ano | findstr :8000

# macOS / Linux
lsof -i :8000
```

Either stop the other process, or change the port — for Docker, edit `docker-compose.yml`'s `ports:` line; for local, pass `--port 8001` to uvicorn.

### The UI loads but `/api/research` errors out with "OPENAI_API_KEY is not set"

Your `.env` exists but the value isn't being read. Common causes:
- Quotes around the value (`OPENAI_API_KEY="sk-..."` — should be `OPENAI_API_KEY=sk-...` with no quotes)
- Empty value (`OPENAI_API_KEY=`)
- Wrong filename (`env.txt` instead of `.env`)
- For Docker: the container started before you edited `.env` — `docker compose restart` after editing

### LinkedIn always shows "skipped"

In order of how often each is the cause:
1. You didn't paste a LinkedIn URL into the form
2. You didn't tick "Enable LinkedIn fetch" in the UI's Advanced settings
3. `DISABLE_LINKEDIN=true` in `.env`
4. `APIFY_TOKEN` is blank or invalid — verify with `python check_apify.py <some-url>`

### A scrape takes >2 minutes and then fails

Heavily-protected sites (Cloudflare, Akamai) sometimes don't render in our headless browser. Things to try in `.env`:
- `SCRAPEGRAPH_LOAD_STATE=networkidle` — waits for JS to fully settle
- `SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT=120` — give it longer
- Configure an `SCRAPEGRAPH_PROXY_SERVER` if you have a paid proxy

Some sites just won't yield. Move on.

### "Apify quota exceeded" or LinkedIn fetches start returning empty

Apify is a pay-per-use service. Check your account balance at https://console.apify.com . The Apify run cost shows up in each JSON log under `linkedin_status_detail`.

### The container won't start — Docker says "permission denied" on the volume mounts

On Linux, the mounted directories need to be writable by the container user. If `kb/`, `logs/`, or `.cache/` don't exist yet:

```bash
mkdir -p kb logs .cache
chmod 777 kb logs .cache   # or chown to the docker user
```

### After a `git pull` something stopped working

Two likely causes:
- A new dependency was added — run `pip install -r requirements.txt` again (or `docker compose up -d --build`).
- The SQLite schema changed — restart the app once; [`src/db.py`](src/db.py) auto-runs the ALTER TABLE statements.

---

For deeper context on what each part actually does, see [`docs/`](docs/) — there's a learner's guide to the underlying scraping approach.
