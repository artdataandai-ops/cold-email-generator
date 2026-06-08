# 04 · `graph_config` — every key, what it does, when to use it

`graph_config` is a dict you pass to every graph. Most keys are optional; a couple aren't. This is the one-page reference.

## Minimal viable config

```python
graph_config = {
    "llm": {"api_key": "sk-...", "model": "openai/gpt-4o-mini"},
}
```

That's enough for `SmartScraperGraph` on a small page. For big pages or `SearchGraph` you also need `embeddings`.

## Full reference

### `llm` — the chat model (required)

```python
"llm": {
    "api_key": "sk-...",            # provider key (omit for ollama/local)
    "model": "openai/gpt-4o-mini",  # provider/model-name
    "temperature": 0,               # default 0; raise for more creative output
    "model_tokens": 8192,           # override only for non-standard / local models
}
```

Model name format: `provider/model-name`. Supported providers: `openai`, `azure`, `gemini` (Google), `groq`, `anthropic`, `huggingface`, `ollama`, `mistralai`, `bedrock`, `nvidia`, `vertexai`, `deepseek`, `together`. Use the literal model id from that provider (e.g. `openai/gpt-4o-mini`, `gemini-1.5-flash`, `ollama/llama3.1`).

For **Ollama** and other local providers:

```python
"llm": {
    "model": "ollama/llama3.1",
    "format": "json",                      # ollama needs this for structured output
    "base_url": "http://localhost:11434",  # default ollama port
    "model_tokens": 8192,
}
```

### `embeddings` — for chunk retrieval

```python
"embeddings": {
    "model": "openai/text-embedding-3-small",
    "api_key": "sk-...",
}
```

Required when:
- The page is large enough to be chunked (most real pages).
- You're using `SearchGraph` (which always retrieves across N pages).

Optional when: pages always fit in one chunk (rare).

You can mix providers — e.g. Groq for the chat LLM (fast/cheap) and Ollama for embeddings (free):

```python
"llm":        {"model": "groq/llama-3.1-70b-versatile", "api_key": "..."},
"embeddings": {"model": "ollama/nomic-embed-text", "base_url": "http://localhost:11434"},
```

### `verbose` — log every node's progress

```python
"verbose": True,   # default False
```

Turn this on the first time you run a new pipeline. Prints which node is running, how many chunks were produced, how many tokens went out. Turn it off in production.

### `headless` — Playwright headless mode

```python
"headless": True,   # default True
```

`True` = invisible browser, fast. `False` = visible Chromium window opens, you can watch what the page actually rendered. Set `False` when:
- A page returns garbage (probably JS that Playwright didn't wait for).
- You want stealth-like behavior (some sites detect headless and block).
- You want to debug what the scraper actually sees.

### `cache_path` — disk cache for fetched HTML

```python
"cache_path": "./.cache/example-com",
```

If set and the directory already contains cached HTML for the URL, the cached version is used instead of re-fetching. **Massive cost reducer** when you're iterating on prompts during development — the network fetch is free, only the LLM call repeats. Set this per-domain or per-job.

### `max_results` — for `SearchGraph`

```python
"max_results": 5,   # default 3
```

How many of Google's top hits SearchGraph fetches. Each one is a full SmartScraperGraph run, so this is your main cost dial for SearchGraph.

### `loader_kwargs` — proxy, user-agent, etc. (passed to FetchNode's loader)

In **this repo** these knobs are exposed as environment variables and wired into `build_graph_config` in [`src/config.py`](../src/config.py). Just set them in `.env`:

| env var | meaning | example |
|---|---|---|
| `SCRAPEGRAPH_PROXY_SERVER` | Proxy URL, or `broker` for the bundled free-proxy rotator, or empty to disable | `http://10.0.0.1:8080` |
| `SCRAPEGRAPH_PROXY_USERNAME` / `_PASSWORD` | Optional proxy auth | |
| `SCRAPEGRAPH_LOAD_STATE` | `domcontentloaded` (default, fast) or `networkidle` (wait for JS to settle) | `networkidle` |
| `SCRAPEGRAPH_RETRY_LIMIT` | Per-page retry attempts inside the loader (scrapegraph default is 1) | `2` |
| `SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT` | Per-page timeout in seconds | `60` |

Raw scrapegraph form, for reference:

```python
"loader_kwargs": {
    "proxy": {
        "server": "http://your-proxy:8080",
        "username": "...",
        "password": "...",
    },
    "load_state": "networkidle",
    "retry_limit": 2,
    "timeout": 60,
}
```

Or use the **built-in free-proxy broker** (no extra deps) — it picks an anonymous public proxy for every request:

```python
"loader_kwargs": {
    "proxy": {
        "server": "broker",
        "criteria": {
            "anonymous": True,
            "secure": True,
            "countryset": {"US", "GB"},
            "timeout": 10.0,
            "max_shape": 3,
        },
    },
}
```

Useful for: rate-limited sites, geo-restricted content, light anti-bot evasion. Free proxies are slow and unreliable; for production use a paid proxy provider's URL in the `server` field.

### `max_images` — for Omni* graphs

```python
"max_images": 5,
```

Caps how many images on a page get sent to the multimodal LLM. Each image costs extra tokens; default is small.

### `additional_info` — append to the default prompt

```python
"additional_info": "Always return ISO-8601 dates. Never guess.",
```

Prepends extra instructions to whatever default system prompt the graph uses. Useful when you want global "always do X" rules without rewriting your `prompt`.

### `output_path` — file path (SpeechGraph only)

Where the generated audio is saved.

### `tts_model` — TTS provider (SpeechGraph only)

```python
"tts_model": {"api_key": "...", "model": "tts-1", "voice": "alloy"}
```

### `library` — target library (ScriptCreatorGraph only)

```python
"library": "beautifulsoup4"   # or "lxml", "playwright"
```

Tells the script generator which scraping library to write the code against.

### `burr_kwargs` — visual debugger

```python
"burr_kwargs": {"project_name": "my-scraper", "app_instance_id": "run-1"}
```

Wires the run into [Burr](https://github.com/DAGWorks-Inc/burr)'s state-machine UI. Install with `pip install scrapegraphai[burr]`, run `burr` in a separate terminal, then watch your graph execute live in a browser. Don't use in production — for debugging only.

## Telemetry — anonymous by default, opt out

By default the library sends an anonymous event when a graph finishes (model name, execution time, total tokens, the prompt). Turn it off if you don't want that:

```python
import os
os.environ["SCRAPEGRAPHAI_TELEMETRY_ENABLED"] = "false"
```

Or programmatically:

```python
from scrapegraphai import telemetry
telemetry.disable_telemetry()
```

## Common config presets

**Cheap dev mode (Ollama, fully local):**
```python
{
    "llm": {"model": "ollama/llama3.1", "base_url": "http://localhost:11434", "format": "json"},
    "embeddings": {"model": "ollama/nomic-embed-text", "base_url": "http://localhost:11434"},
    "verbose": True,
    "headless": False,
}
```

**Production OpenAI:**
```python
{
    "llm": {"api_key": OPENAI_API_KEY, "model": "openai/gpt-4o-mini"},
    "embeddings": {"api_key": OPENAI_API_KEY, "model": "openai/text-embedding-3-small"},
    "verbose": False,
    "headless": True,
    "cache_path": f"./.cache/{domain}",
    "max_results": 5,
}
```

**Stealth-ish for tough sites (the LinkedIn variant in this repo):**
```python
{
    "llm": {...},
    "embeddings": {...},
    "headless": False,
    "cache_path": "...",
    "loader_kwargs": {
        "proxy": {"server": "broker", "criteria": {"anonymous": True, "secure": True, "timeout": 10.0}},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
    },
}
```

See [`src/config.py`](../src/config.py) in this repo for working `build_graph_config()` and `build_stealth_config()` helpers you can copy into other projects.
