# 07 · Gotchas, cost control, and debugging

The things that bite you in production.

## Cost: where the tokens actually go

Every graph has exactly **one node that calls the LLM** for output: `GenerateAnswerNode`. (RAGNode also calls embeddings, which is much cheaper per call.) Knowing this means cost = `(# pages) × (1 LLM call per page-or-chunk-batch) + (# pages) × (some embedding tokens)`.

For a typical page (~30k chars of cleaned text → ~7.5k tokens after parsing → fits in one chunk), one `SmartScraperGraph` run costs roughly:

| Model | Input | Output | Total per page |
|---|---|---|---|
| `gpt-4o-mini` | 7.5k × $0.00015/1k = $0.001 | 0.5k × $0.0006/1k = $0.0003 | ~**$0.0013** |
| `gpt-4o` | 7.5k × $0.0025/1k = $0.019 | 0.5k × $0.010/1k = $0.005 | ~**$0.024** |
| `gemini-1.5-flash` | 7.5k × $0.000075/1k = $0.0006 | 0.5k × $0.0003/1k = $0.0002 | ~**$0.0008** |
| `ollama/*` (local) | $0 | $0 | **$0** |

`SearchGraph` multiplies that by `max_results + 1` (one per fetched page + a merge call). `SmartScraperMultiGraph` multiplies by the number of URLs.

### Knobs that actually move the needle

In rough order of impact:

1. **Pass a Pydantic `schema`.** Cuts output tokens 30–60%. The LLM stops being chatty.
2. **Cheaper model.** `gpt-4o-mini` costs ~5% of `gpt-4o`; `gemini-1.5-flash` costs less than that. Quality is fine for extraction tasks.
3. **`cache_path`** for development. Re-runs cost zero network and zero LLM if the prompt didn't change *and* the chunk that the prompt happens to hit was cached. (Note: ScrapeGraphAI's default cache is at the *fetch* layer — the LLM still runs unless you cache its output yourself.)
4. **Bound your prompts.** "Up to 8 URLs," "summary 2-3 sentences," "skip if no date" — every cap is fewer output tokens.
5. **`max_results=3` for SearchGraph dev, raise to 5–8 only in prod.**
6. **Run a cheap model for discovery, an expensive one for extraction.** You can build two configs and pass them to different graphs.

### Hard cap pattern

What we do in `src/cost_guard.py`:

```python
class CostMeter:
    def __init__(self, cap_usd=0.20):
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
    def charge(self, usd, label=""):
        if self.spent_usd + usd > self.cap_usd:
            raise CostCapExceeded(...)
        self.spent_usd += usd
```

Call `meter.charge(estimate_call_usd(input_chars=...))` *before* every `graph.run()`. You won't get billed for a run you can't afford — the abort happens before the call goes out.

## Reliability: when scraping fails

### "I get an empty dict"

In order of likelihood:

1. **JS-only page**: Playwright didn't wait long enough. Set `headless=False` and watch the browser. Add a `wait_ms` if your version supports it, or use a smarter `loader_kwargs`.
2. **Login wall / cookie banner blocking content**: same diagnostic — open a non-headless run and look. Sites often hide the real content behind a modal that the LLM happily reads as "please accept cookies."
3. **Rate limited / IP blocked**: switch on the proxy broker (`loader_kwargs.proxy.server = "broker"`).
4. **Malformed HTML**: rare, but `ParseNode` can produce empty chunks for pages that are pure JS. Try a different page on the same site to confirm.

### "I get garbage / hallucinated content"

1. **No schema** → LLM made up its own format. Pass a Pydantic schema.
2. **Bad RAG retrieval** → the relevant chunks weren't picked. Either the chunks are too small (raise chunk size) or the prompt doesn't match the chunk vocabulary (rephrase the prompt to use words that actually appear in the page).
3. **Too-greedy prompt** → "extract all information about the company" gives the LLM permission to invent. Be specific.

### "Date fields are wrong / missing"

LLMs are bad at dates by default. Three fixes:

1. Make `published_date: date` *required* in your schema. Items without a parseable date will fail validation and be dropped — better than silently wrong dates.
2. Tell the LLM the format: `"return published_date in ISO-8601 (YYYY-MM-DD)"`.
3. Tell it what to do when it can't tell: `"if you cannot determine the date, omit the item entirely."`

## LinkedIn specifically

LinkedIn is the canonical hard case. What works in this repo (`src/linkedin.py`):

- **Don't expect Tier 1 to work most of the time.** It works sometimes for very public profiles, fails the rest. Treat it as opportunistic.
- **Have a Tier 2 for production.** Apify, Phantombuster, Proxycurl — pick one. Cost is real ($0.05–$0.30/profile) but reliability is night-and-day.
- **Stay public-only.** Don't try to log in via Playwright with a real LinkedIn account. Your account *will* get banned, and TOS-wise you're on shaky ground.

## Debugging tools

### Verbose mode

```python
"verbose": True
```

Prints every node's progress, the prompt sent to the LLM, the chunks retrieved, the raw answer before parsing. Always your first move when something's off.

### Burr (visual)

```python
"burr_kwargs": {"project_name": "scraper-debug", "app_instance_id": "run-1"}
```

Then `pip install scrapegraphai[burr]` and `burr` in another terminal. Live web UI of the graph executing. Fantastic for understanding what's happening, especially the first time you build a custom graph.

### Quick-and-dirty: run nodes manually

You can pull a node out of a graph and run it directly:

```python
from scrapegraphai.nodes import FetchNode
node = FetchNode(input="url", output=["doc"], node_config={"headless": False})
state = {"url": "https://example.com"}
state = node.execute(state)
print(state["doc"][0].page_content[:1000])
```

Best way to confirm "is the issue at fetch, parse, or generate?"

## Schema gotchas

- **Pydantic v2 is required** for the latest ScrapeGraphAI. Older v1-style models won't validate.
- **Use `HttpUrl` for URLs**, but be aware: a non-URL string in the LLM output will fail validation and the whole item will be dropped. If you'd rather coerce or default, use `str` and validate yourself.
- **Avoid `Optional` everywhere**. Required fields force the LLM to find or skip — that's the behavior you usually want for filtering.
- **Use `Literal[...]` for enums**. The LLM is much better at picking from a fixed set than free-form labeling. We use `Literal["news", "blog", "award", ...]` in `src/schemas.py`.

## Telemetry / privacy

By default the library sends an anonymous event with `prompt`, `model`, `total_tokens`, `execution_time` to ScrapeGraphAI's analytics. **If you don't want this**:

```python
import os
os.environ["SCRAPEGRAPHAI_TELEMETRY_ENABLED"] = "false"
```

Set it before you import `scrapegraphai`. We haven't disabled it in this repo; do so if you're processing sensitive prompts.

## Last resort: drop down to nodes

When even custom graphs get awkward, you can skip the graph layer and call nodes one at a time. You lose the orchestration sugar but gain full control — useful for one-off experiments or when you need to inject custom logic between every two steps.

The whole library, top to bottom, is designed to be poked at. Don't be afraid to read the source — most graph definitions are 50–100 lines and reading one is the fastest way to understand what it does.
