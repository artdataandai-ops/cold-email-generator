# 01 · Concepts: what ScrapeGraphAI actually is

## The problem it solves

Traditional web scraping is brittle: you write CSS/XPath selectors against a page, the site changes its markup next week, your scraper breaks. You also have to write per-site code — every new target needs new selectors.

ScrapeGraphAI replaces "selectors against HTML" with "**a prompt against an LLM that has read the HTML.**" You say *"give me title, summary, published_date for every article on this page"* and the library handles the rest. When the site changes its layout, your prompt still works because the LLM re-reads the new DOM each time.

That's the value proposition. The graph stuff is just how it organizes the work internally.

## Why "graph"?

Extracting clean data from a web page is not a single LLM call. It's a *sequence* of steps:

```
download HTML  →  clean / parse it  →  if too big, chunk + embed + retrieve  →  ask LLM to extract  →  validate / merge
```

Each step is a **node**. The fixed wiring of "node A feeds into node B feeds into node C" is the **graph**. The graph runs top-to-bottom, each node reading from and writing to a shared **state dict**.

So when the docs say *"SmartScraperGraph is a graph,"* what they mean is: it's a pre-wired pipeline of nodes that solves the "single page, structured extraction" task. You don't have to wire the nodes yourself — you just instantiate the class and call `.run()`.

If you've used **LangGraph**, this is the same idea, simpler. If you've used **scikit-learn pipelines**, same idea — except the steps know about LLMs and HTML, not numpy arrays.

## The four things you control

When you call a graph, you control four inputs and everything else is sensible defaults:

| Input | What it's for |
|---|---|
| `prompt` | The natural-language instruction. *"Extract title, author, date, summary for every article."* |
| `source` | A URL, a list of URLs, or already-downloaded HTML/text. |
| `config` (a dict) | Which LLM, which embedding model, headless browser yes/no, cache, proxy, etc. |
| `schema` (a Pydantic class) | The shape of the JSON you want back. **This is the single biggest quality lever** — pass one. |

Output is always a Python dict (or a list of dicts for multi-graphs). If you passed a `schema`, the dict matches it.

## The mental model in one diagram

```
                   ┌────────────────────────────────────────────┐
   prompt ────────►│           Graph (e.g. SmartScraperGraph)   │
   source ────────►│                                            │
   config ────────►│  Node1 → Node2 → Node3 → Node4 → ...       │
   schema ────────►│   ▲                                        │
                   │   └─ each node reads/writes a shared state │
                   └────────────────────────────────────────────┘
                                     │
                                     ▼
                              dict (matching schema)
```

That's it. The next page (`02-anatomy.md`) opens the box and shows what the nodes actually do.

## Two products, one name (don't get confused)

The name "ScrapeGraphAI" refers to **two related but separate products**:

1. **`scrapegraphai` (the open-source Python library)** — what this repo uses. You run it on your machine. You bring your own LLM key (OpenAI / Gemini / Ollama / etc.). You pay tokens to the LLM provider. **This is what these docs cover.**
2. **The managed cloud API** at `dashboard.scrapegraphai.com` — a hosted service with endpoints like SmartScraper, SearchScraper, AgenticScraper. You pay them per credit. They run the LLM, the browser, the proxies. Different SDKs (`scrapegraph-py`, `scrapegraph-js`).

When you read the official docs, double-check which one you're looking at. Class names like `SmartScraperGraph` belong to the OSS library. Names like `smartscraper.request()` or "30 credits per query" belong to the cloud API.

This project chose **the OSS library**, for cost control and because we want our own LLM keys. That's why every code example in these docs imports from `scrapegraphai.graphs`, not from `scrapegraph_py`.
