# 05 · Our use case, end to end

This page connects the design to the actual code in this repo. After this you should be able to read any file and know exactly which part of the architecture it implements.

> **Note for readers coming from earlier versions of this doc.** This project originally used `SmartScraperGraph` / `SearchGraph` / `SmartScraperMultiGraph` from ScrapeGraphAI for nearly every fetch + extract step. After a series of latency, hallucination, and cost regressions, the pipeline today uses ScrapeGraphAI's `ChromiumLoader` only as a Playwright **fallback**. The happy path is plain `httpx + Trafilatura + direct OpenAI`. This doc has been rewritten to describe the current architecture.

## What we're building

A FastAPI-based internal tool for the marketing team. They paste a company URL (and optionally a LinkedIn URL of the target person), and we return:

1. **Recent insights** — news / blog posts / awards / funding rounds from the last N days, sourced from the company site itself **and** third-party coverage. Each item carries:
   - `category` — news / blog / award / funding / launch / press / other
   - `sentiment` — positive / neutral / negative
   - `event_significance` — major / notable / routine / administrative / peripheral / negative (the "is this even worth mentioning?" signal)
2. **A draft cold email** with the LLM's reasoning trail attached so you can audit why it picked the angle it did.

## Pipeline at a glance

```
              ┌────── company_url, linkedin_url, recipient, sender_kb, intent ──────┐
              │                                                                     │
              ▼                                                                     │
   ┌──────────────────────────┐                                                     │
   │ 1. discover_pages        │  httpx anchor scan → sitemap (+ lastmod) →          │
   │    "find news/blog URLs" │  common-path probe → LLM fallback                   │
   └────────────┬─────────────┘                                                     │
                │  urls[]                                                           │
                ▼                                                                   │
   ┌─────────────────────────────────────────────────┐  ⏩ ThreadPoolExecutor       │
   │ 2-5. PARALLEL FAN-OUT — all run concurrently    │                              │
   │ ┌─────────────────────────────────────────────┐ │                              │
   │ │ 2. extract_company_news                     │ │                              │
   │ │    httpx → JSON-LD scan → Trafilatura       │ │                              │
   │ │    → meta-date → direct OpenAI (lax schema) │ │                              │
   │ │    → per-item strict validate               │ │                              │
   │ ├─────────────────────────────────────────────┤ │                              │
   │ │ 3. fetch_rss_items                          │ │                              │
   │ │    httpx probe /feed, /rss, /atom + parse   │ │                              │
   │ ├─────────────────────────────────────────────┤ │                              │
   │ │ 4. web_search_company  ⚙️                   │ │                              │
   │ │    4 narrow DDG queries (weighted depth)    │ │                              │
   │ │    → same engine as ② → URL override        │ │                              │
   │ │    → brand-relevance filter                 │ │                              │
   │ ├─────────────────────────────────────────────┤ │                              │
   │ │ 5. fetch_linkedin  ⚙️                       │ │                              │
   │ │    Apify actor → date filter                │ │                              │
   │ └─────────────────────────────────────────────┘ │                              │
   └────────────┬─────────────────┬──────────────────┘                              │
                │ news items[]    │ raw posts[]                                     │
                ▼                 ▼                                                 │
   ┌──────────────────────┐  ┌───────────────────────────┐                          │
   │ 6. filter_dedupe_rank │  │ 7. filter_linkedin_posts │                          │
   │    Recency + dedupe + │  │    LLM scores 0-1,        │                          │
   │    5-signal scorer +  │  │    tags sentiment +       │                          │
   │    top-N cap          │  │    event_significance     │                          │
   └────────────┬──────────┘  └────────────┬──────────────┘                          │
                │  bundle.items[]          │  bundle.linkedin_posts[]                │
                └─────────────┬────────────┘                                         │
                              ▼                                                     │
   ┌─────────────────────────────────────────────────┐                              │
   │ 8. draft_email                                  │  ChatOpenAI direct           │
   │    Chain-of-thought structured output:          │  (DraftedEmail schema)       │
   │    anchor_chosen → business_implication →       │                              │
   │    sender_fit → intent_alignment →              │                              │
   │    pitch_angle → email body                     │                              │
   └────────────┬────────────────────────────────────┘                              │
                ▼                                                                   │
       PipelineResult(bundle, email)  ◄──────────────────────────────────────────  ┘
                                                              UI render
```

## File-by-file map

| File | Role |
|---|---|
| [`src/schemas.py`](../src/schemas.py) | Pydantic models: `NewsItem` (with `sentiment`, `event_significance`), `LinkedInPost`, `NewsCategory`, `NewsSentiment`, `EventSignificance`, `ResearchBundle`. The strict `NewsItem` URL validator rejects placeholder hosts (example.com), dotless hosts (`https://in/...`), and root-only paths. |
| [`src/config.py`](../src/config.py) | Builds `graph_config` dicts (still used by ScrapeGraphAI's `ChromiumLoader` Playwright fallback). Per-domain `cache_path`. |
| [`src/company.py`](../src/company.py) | `discover_pages` — 4-tier discovery (anchor scan → sitemap+lastmod → common-path probe → LLM fallback). `_homepage_anchor_scan`, `_try_sitemap_with_lastmod`, `_probe_common_paths`, `_validate_listing_candidates`. |
| [`src/extractor.py`](../src/extractor.py) | `extract_news_from_pages` — httpx-first fetch with Playwright fallback, JSON-LD short-circuit for listing pages, Trafilatura cleaning, `_extract_meta_date` hint, direct OpenAI `client.beta.chat.completions.parse` with `_LaxNewsItemList` response_format. Per-URL parallelism via `ThreadPoolExecutor(max_workers=4)`. |
| [`src/search.py`](../src/search.py) | `web_search_company` — 4 narrow DDG queries (`news`, `partnership`, `announces`, `launch`) with weighted depth (`_DDG_PER_INTENT_CAP`). Uses the SAME engine as `extractor.py` for per-URL scraping. URL-override hallucination guard. Brand-relevance post-filter with strict full-phrase match. |
| [`src/rss.py`](../src/rss.py) | `fetch_rss_items` — probes `/feed`, `/rss`, `/atom`, parses RSS 2.0 + Atom. Zero LLM. |
| [`src/linkedin.py`](../src/linkedin.py) | `fetch_linkedin` — Apify actor (`apify-client`). LinkedIn's anti-bot is hostile to direct scrapers; Apify owns that complexity. |
| [`src/linkedin_filter.py`](../src/linkedin_filter.py) | `filter_linkedin_posts` — LLM scores each post 0-1 for cold-email-anchor value AND classifies `sentiment` + `event_significance` using the same vocabulary as `NewsItem`. |
| [`src/relevance.py`](../src/relevance.py) | `rank_news` + `score_relevance` — 5-signal scorer (category, recency, signal-word regex, sender-KB overlap, recipient-role mention). Sentiment + event_significance are NOT yet weighted in (Phase 2 will). |
| [`src/recency.py`](../src/recency.py) | `filter_recent_news`, `dedupe_news`, `filter_recent_posts` — pure-Python boundary checks. |
| [`src/email_writer.py`](../src/email_writer.py) | `draft_email` returns a `DraftedEmail` with both the email body AND the LLM's reasoning trail (anchor_chosen / business_implication / sender_fit / intent_alignment / pitch_angle). Uses `langchain_openai.ChatOpenAI.with_structured_output(DraftedEmail)`. |
| [`src/pipeline.py`](../src/pipeline.py) | `research_pipeline` — orchestrates everything. Parallel fan-out via `ThreadPoolExecutor` after `discover_pages`. `_logged` wraps each step in a `QueryLogger.step` context. |
| [`src/cost_guard.py`](../src/cost_guard.py) | `CostMeter` with `threading.Lock` (required after parallel fan-out — multiple threads can land on `+= float` concurrently). |
| [`src/query_log.py`](../src/query_log.py) | `QueryLogger` — writes `logs/<ts>_<id>.json` per run. `list.append` is atomic so concurrent step writes are safe. |
| [`app.py`](../app.py) | FastAPI server: `GET /` HTML form, `POST /api/research` JSON endpoint. |
| [`templates/index.html`](../templates/index.html) | Single-page UI: vanilla JS, talks to `/api/research`. |
| [`scripts/scrape_batch.py`](../scripts/scrape_batch.py) | Test runner against `tests/fixtures/companies.json`. `--filter <substr>` to run just one prospect (cost-cheap iteration). `--recency-days N` to override default. |

## Behavior anchors — the design decisions behind the code

### 1. Why parallel fan-out after discover?

Sequential math: `discover (10s) + extract (15s) + rss (4s) + search (50s) + linkedin (12s) = ~91s`.
Parallel math: `discover (10s) + max(extract, rss, search, linkedin) (~50s) = ~60s`.

`web_search_company` is the longest single step (15 URLs to scrape), so parallelism reclaims everything that's not in its critical path. See `src/pipeline.py:research_pipeline` — `ThreadPoolExecutor(max_workers=4)` with each task using `_logged` so per-step JSON logs are unchanged. `CostMeter` is now lock-guarded against concurrent `charge()` and `record_llm_usage()` calls.

### 2. Why httpx-first instead of ScrapeGraphAI's `SmartScraperGraph`?

Empirical: `SmartScraperGraph` spins up a Playwright Chromium per URL (~6-8s cold-start) whether the page needs JS or not. Most news article pages (`pymnts.com`, `financialit.net`, `crowdfundinsider`, `finance.yahoo.com`) render fine on plain HTTP. The current `_fetch` in `src/extractor.py:_fetch` tries httpx first; only escalates to Playwright when:
- httpx returns <1KB (likely a challenge stub or empty page), OR
- the page is a recognized SPA shell (`<div id="root">` + low anchor density + thin text).

Net effect: third-party news scraping went from ~50s for 15 URLs to ~15s. Cloudflare-walled sites still work because they hit the Playwright fallback automatically.

### 3. Why JSON-LD short-circuit on listing pages?

Hedge Equities' `/blogs` listing emits a `<script type="application/ld+json">` containing all 20 blog posts with authoritative `datePublished`. Trafilatura strips that as non-prose, so the LLM was missing per-card dates and stamping everything with one default date (or skipping recent posts). `_extract_jsonld_items` walks every dict value recursively (handles `@graph`, `itemListElement`, Shopify's custom `"blogPosts"` wrapper) and pulls `BlogPosting` / `NewsArticle` objects directly. When ≥3 items come out of JSON-LD, we skip the LLM call entirely for that page — cheaper AND more accurate.

### 4. Why the URL hallucination guard in web search?

The LLM, when given a third-party news page, occasionally fabricates a close-but-wrong URL — observed example: real article is at `https://thepaypers.com/payments/news/<slug>` but LLM emits `https://www.thepaypers.com/news/<slug>` (path prefix wrong, slug right). The guard in `web_search_company` is: `raw["url"] = source_url` — replace whatever the LLM said with the DDG-supplied URL we know is correct. The LLM's URL claim is never trusted because we KNOW which page we asked it to scrape.

### 5. Why the four narrow DDG queries instead of one wide one?

The earlier query `'"X" (news OR funding OR award OR launch OR partnership) after:Y -site:a.com -site:b.com ...×11'` collapsed DDG's relevance ranking entirely — empirically it returned generic top-news domains (TMZ, local-news affiliates) instead of relevant results. The current setup issues 4 narrow queries: `"X" news after:Y`, `"X" partnership after:Y`, `"X" announces after:Y`, `"X" launch after:Y` — each surfaces a different DDG relevance bucket. Per-intent depth caps in `_DDG_PER_INTENT_CAP` are weighted (`partnership=7, news=4, announces=2, launch=2`) because partnership-style stories (Currensea, Clique, Cross River) consistently surface deeper than position 4 of the partnership query.

### 6. Why chain-of-thought in the email writer?

Real failure case: a Storia Films (AI video production) email pitching upGrad (AI-education company) generated: *"I noticed upGrad's AI Excellence Centres — at Storia we use AI in our films too."* Surface-level keyword stitching, no business reasoning. The fix: force the LLM to fill `anchor_chosen → business_implication → sender_fit → intent_alignment → pitch_angle` BEFORE writing the email, as fields in a `DraftedEmail` Pydantic schema. The LLM has to articulate *why* this news creates an opportunity for the sender's specific offering. Same cost (one LLM call), much better reasoning, and the trail is logged so you can audit "why did the LLM pick that angle?"

### 7. Why lax schema for the LLM's `response_format`?

OpenAI's strict structured-output parsing validates the WHOLE list atomically. If the LLM emits one item with a placeholder host like `example.com`, the entire response fails validation and we lose every item. `_LaxNewsItem` / `_LaxNewsItemList` in `extractor.py` accept any URL string at parse time; per-item strict validation happens downstream via `NewsItem.model_validate`, which drops just the bad row. Same end-state for the clean case, graceful degradation for the partial-fail case.

### 8. How "recent only" is enforced

Three layers, all of which must agree before an item reaches the email writer:

1. **DDG query level** — every web-search query embeds `after:YYYY-MM-DD` so DDG biases toward fresh results.
2. **Extraction prompt level** — both `_build_prompt` (on-site) and `_build_web_search_prompt` (web search) tell the LLM: "Use the article's ACTUAL publication date. Do NOT default to the cutoff date when unsure — SKIP items where you cannot determine the real publication date." A per-page `meta_date` hint from Trafilatura is included so the LLM has something to ground in.
3. **Schema level** — `NewsItem.published_date: date` is required; items missing a date fail `model_validate` and are dropped.
4. **Final code-level filter** — `recency.filter_recent_news` does `today - N_days <= published_date <= today` as belt-and-suspenders. JSON-LD short-circuit items get their dates from authoritative metadata, so they always pass.

## How to run

### Local (development)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"      # editable install + pytest, pytest-asyncio
python -m playwright install chromium
copy .env.example .env                  # edit OPENAI_API_KEY (+ APIFY_TOKEN if using LinkedIn)
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>, paste a company URL, hit Generate. JSON API at `POST /api/research`.

### Batch test runner

```powershell
.\.venv\Scripts\python.exe scripts/scrape_batch.py             # all fixtures
.\.venv\Scripts\python.exe scripts/scrape_batch.py --filter thredd  # just one
.\.venv\Scripts\python.exe scripts/scrape_batch.py --recency-days 30
```

Reports to `output/scrape_eval/<ts>.json`. Each `actual_items` entry carries `published_date`, `category`, `sentiment`, `event_significance` so you can manually inspect classification quality.

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

The full test suite (~230 tests) runs in ~5s. No network calls — everything is mocked.

### Docker (deployment)

```bash
cp .env.example .env       # fill in OPENAI_API_KEY, APIFY_TOKEN
docker compose up -d --build
```

The `Dockerfile` is based on `mcr.microsoft.com/playwright/python` so Chromium + system deps are baked in. `docker-compose.yml` mounts three persistent volumes:

- `./kb/` — SQLite DB with sender profiles (survives container rebuilds)
- `./logs/` — per-request JSON logs
- `./.cache/` — Playwright fetch cache + LinkedIn 24h cache

## What you should be able to do after reading this

- Read [`src/pipeline.py`](../src/pipeline.py) and identify the parallel fan-out block and how it differs from the sequential `_logged` calls.
- Read [`src/extractor.py`](../src/extractor.py) and recognise the JSON-LD short-circuit path vs the Trafilatura+LLM path, and trace the `_fetch` → `_clean_text` → `_extract_meta_date` → `_call_llm` chain.
- Open a `logs/<ts>_<id>.json` and explain what the `draft_email` step's reasoning fields tell you about why the LLM picked the angle it did.
- Predict what would happen if the recency window were widened to 180 days (more items survive the recency filter; sentiment + event_significance start mattering more for cap selection).

If you can do all four — you understand the system end to end.
