# 03 · Pipeline types: which graph for which job

ScrapeGraphAI ships ~10 built-in graphs. Most jobs are solved by one of three: `SmartScraperGraph`, `SmartScraperMultiGraph`, or `SearchGraph`. Here's the full menu with picking guidance.

## The decision tree

```
Do you already have the URL(s)?
├── No → SearchGraph (it queries Google for you, then scrapes top N)
│
└── Yes
    ├── One URL → SmartScraperGraph
    ├── Multiple URLs, same prompt → SmartScraperMultiGraph
    └── Need to extract images too → OmniScraperGraph (or OmniSearchGraph)
```

For everything else (audio, code generation, PDFs, JSON files), see the "specialty graphs" section at the bottom.

## SmartScraperGraph — the workhorse

Single page, structured extraction.

```python
from scrapegraphai.graphs import SmartScraperGraph
from pydantic import BaseModel

class Article(BaseModel):
    title: str
    summary: str
    author: str | None = None

graph = SmartScraperGraph(
    prompt="Extract the article: title, 2-sentence summary, author if shown.",
    source="https://example.com/blog/post-123",
    config={"llm": {"api_key": "...", "model": "openai/gpt-4o-mini"}},
    schema=Article,
)
result = graph.run()    # -> {"title": "...", "summary": "...", "author": "..."}
```

Use when: you already have the exact URL and want one structured object out.

Internally: `FetchNode → ParseNode → RAGNode → GenerateAnswerNode`.

## SmartScraperMultiGraph — same prompt, many URLs

Same idea, but `source` is a `list[str]`, and the inner SmartScraperGraph runs **in parallel** across them. Returns a list of results (or a merged dict, depending on schema).

```python
from scrapegraphai.graphs import SmartScraperMultiGraph

graph = SmartScraperMultiGraph(
    prompt="Extract every news item: title, summary, published_date.",
    source=[
        "https://acme.com/news",
        "https://acme.com/blog",
        "https://acme.com/press",
    ],
    config=cfg,
    schema=NewsItemList,   # a Pydantic model with `items: list[NewsItem]`
)
```

Use when: you've already discovered a small set of pages and want to extract from all of them.

This is what we use in [`src/company.py`](../src/company.py) for `extract_company_news`.

## SearchGraph — let it find the URLs

You give it a *prompt* and no source. It builds a search query from your prompt, hits a search engine (Google by default), takes the top `max_results` URLs, and runs `SmartScraperGraph` against each.

```python
from scrapegraphai.graphs import SearchGraph

graph = SearchGraph(
    prompt='Find recent news about "Acme Corp" funding rounds in 2026',
    config={**cfg, "max_results": 5},
    schema=NewsItemList,
)
result = graph.run()
```

Use when: you don't have URLs in hand and want to *discover* sources. Great for "what's been said about X?" research.

Caveats:
- The OSS version has **no native date/time-range filter**. We work around it by adding `after:YYYY-MM-DD` to the prompt (Google honors that operator) — see [`src/search.py`](../src/search.py).
- Embeddings are required (chunks of N pages need to be retrieved against your prompt).
- This is the most expensive built-in graph because it makes N+1 LLM calls (one per page + a merge).

## OmniScraperGraph / OmniSearchGraph — with images

Same as the above two, but adds an image-understanding step using a multimodal model (GPT-4o, Gemini, etc.). Returns text **and** image descriptions.

```python
graph = OmniScraperGraph(
    prompt="List every project: title, description, image link, and image description.",
    source="https://perinim.github.io/projects",
    config={"llm": {"model": "openai/gpt-4o", "api_key": "..."}, "max_images": 5},
)
```

Use when: image content is part of the data you want (product catalogs, portfolios, real estate listings).

`max_images` caps the per-page image cost.

## ScriptCreatorGraph — generate a Python scraper

Instead of returning data, it returns *Python code* that scrapes the page using a traditional library (BeautifulSoup, Playwright). One LLM call up front, then you run the generated script for free forever.

```python
graph = ScriptCreatorGraph(
    prompt="Create a Python script to scrape the projects.",
    source="https://perinim.github.io/projects/",
    config={"llm": {...}, "library": "beautifulsoup4"},
)
script_text = graph.run()
```

Use when: you'll scrape the same site shape thousands of times and want zero per-call LLM cost. The trade-off: the script is brittle to layout changes (you're back to traditional scraping).

`ScriptCreatorMultiGraph` is the multi-source variant.

## SpeechGraph — extract + narrate

Wraps `SmartScraperGraph` and adds a `TextToSpeechNode` that produces an audio file from the extracted answer. Niche.

```python
graph = SpeechGraph(
    prompt="Make a detailed audio summary of the projects.",
    source="https://...",
    config={"llm": {...}, "tts_model": {"model": "tts-1", "voice": "alloy", "api_key": "..."}},
)
```

## Specialty file-format graphs

For local files instead of URLs:

| Graph | Source format |
|---|---|
| `CSVScraperGraph`, `CSVScraperMultiGraph` | `.csv` |
| `JSONScraperGraph`, `JSONScraperMultiGraph` | `.json` |
| `XMLScraperGraph`, `XMLScraperMultiGraph` | `.xml` |
| `PDFScraperGraph`, `PDFScraperMultiGraph` | `.pdf` |
| `MDScraperGraph` | `.md` |

Same API as `SmartScraperGraph` — pass a file path or content string as `source`. Useful for batch-processing internal documents with the same prompt-driven extraction approach.

## "But I want X they don't have"

If none of the built-ins fit, build your own by subclassing `BaseGraph` and wiring the existing nodes (or new ones) yourself. See `06-recipes.md` for an example.

## Picking guide for our use case

In this repo we use:

| Graph | Where | Why |
|---|---|---|
| `SmartScraperGraph` | `src/company.py::discover_pages` and `src/linkedin.py` | One URL in, structured list out. |
| `SmartScraperMultiGraph` | `src/company.py::extract_company_news` | Several blog/news URLs in parallel, same extraction prompt. |
| `SearchGraph` | `src/search.py` | We don't know in advance where third-party news will appear, so let Google find it. |

Three pipeline types covers a surprising amount of ground. Most production use cases mix-and-match these three.
