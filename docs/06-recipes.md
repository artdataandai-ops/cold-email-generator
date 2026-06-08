# 06 · Recipes — patterns for new use cases

Copy-pasteable starting points for common jobs. Each recipe says **when** to use it, gives a working snippet, and notes the gotchas.

## Recipe 1: Single-page structured extraction

**When**: you have one URL and want one structured object out (a product page, a job posting, an article).

```python
from pydantic import BaseModel
from scrapegraphai.graphs import SmartScraperGraph

class JobPosting(BaseModel):
    title: str
    company: str
    location: str | None
    salary_range: str | None
    posted_date: str | None
    requirements: list[str]

graph = SmartScraperGraph(
    prompt="Extract this job posting's title, company, location, salary range, posted date, and a flat list of requirements.",
    source="https://example.com/jobs/12345",
    config={"llm": {"api_key": OPENAI_KEY, "model": "openai/gpt-4o-mini"}},
    schema=JobPosting,
)
result = graph.run()
```

**Gotcha**: keep schemas flat where possible. Deeply nested models cause more token churn during LLM JSON synthesis.

## Recipe 2: Discover URLs, then extract

**When**: you know the *root* of a site but not the specific pages (e.g., "give me every product on this Shopify store").

Two-step pattern, just like our `company.py`:

```python
# Step 1 — discovery
class Links(BaseModel):
    urls: list[str]

discovery = SmartScraperGraph(
    prompt="Return up to 30 absolute URLs to product detail pages on this site. JSON: {urls: [...]}",
    source="https://store.example.com/",
    config=cfg,
    schema=Links,
)
urls = discovery.run()["urls"]

# Step 2 — extract from each in parallel
from scrapegraphai.graphs import SmartScraperMultiGraph

extractor = SmartScraperMultiGraph(
    prompt="Extract the product: name, price, currency, description, image URL.",
    source=urls,
    config=cfg,
    schema=ProductList,   # has `items: list[Product]`
)
products = extractor.run()["items"]
```

**Gotcha**: cap the URLs you discover (`up to 30`). LLMs given an unbounded "find every link" prompt can return thousands; downstream multi-graph then explodes your bill.

## Recipe 3: Topical web search

**When**: you don't know which sites have what you need.

```python
from scrapegraphai.graphs import SearchGraph

class Mentions(BaseModel):
    items: list["Mention"]

class Mention(BaseModel):
    title: str
    summary: str
    url: str
    source_domain: str
    published_date: str | None

graph = SearchGraph(
    prompt='Find recent articles mentioning "Vision Pro 3" reviews from technology publications, after:2026-01-01',
    config={**cfg, "max_results": 8},
    schema=Mentions,
)
result = graph.run()
```

**Gotchas**:
- `SearchGraph` has no native time filter; use Google's `after:YYYY-MM-DD` operator inside the prompt as shown.
- Each `max_results` increment is one full SmartScraper run — keep it tight.
- Always pass a schema; `SearchGraph` outputs are messier than single-page extracts.

## Recipe 4: Periodic monitoring (price tracker, change detector)

**When**: you want to re-run the same extraction on a schedule and detect changes.

```python
import json
from pathlib import Path

def snapshot_product(url: str, cfg: dict) -> dict:
    graph = SmartScraperGraph(prompt="Extract name, price, in_stock.", source=url, config=cfg, schema=Product)
    return graph.run()

def diff_against_last(snapshot: dict, history_file: Path) -> dict | None:
    if not history_file.exists():
        history_file.write_text(json.dumps(snapshot))
        return None
    previous = json.loads(history_file.read_text())
    history_file.write_text(json.dumps(snapshot))
    if previous != snapshot:
        return {"before": previous, "after": snapshot}
    return None
```

Schedule with cron / Windows Task Scheduler / GitHub Actions. Keep `cache_path` **off** for monitoring (you want fresh fetches each run).

## Recipe 5: Lead enrichment from a CSV of company URLs

**When**: marketing or sales hands you a CSV of domains and you want to enrich each with structured info.

```python
import pandas as pd
from scrapegraphai.graphs import SmartScraperMultiGraph

class CompanyProfile(BaseModel):
    company_name: str
    description: str
    industry: str | None
    employees_estimate: str | None
    contact_email: str | None
    location: str | None

df = pd.read_csv("leads.csv")  # has a "url" column
urls = df["url"].dropna().tolist()

graph = SmartScraperMultiGraph(
    prompt="Extract the company profile.",
    source=urls,
    config=cfg,
    schema=CompanyProfileList,
)
profiles = graph.run()["items"]
df_enriched = df.merge(pd.DataFrame(profiles), left_on="url", right_on="source_url", how="left")
df_enriched.to_csv("leads_enriched.csv", index=False)
```

**Gotcha**: `SmartScraperMultiGraph` doesn't preserve input order — track URLs in your schema (e.g. add a `source_url` field the LLM extracts) so you can join back.

## Recipe 6: Custom graph (your own pipeline)

**When**: the built-ins don't fit. Maybe you want fetch → custom-classify-with-zero-shot → conditional-extract.

```python
from scrapegraphai.graphs import BaseGraph
from scrapegraphai.nodes import FetchNode, ParseNode, RAGNode, GenerateAnswerNode

class MyGraph:
    def __init__(self, prompt, source, config, schema=None):
        self.prompt = prompt
        self.source = source
        self.config = config
        self.schema = schema
        self.graph = self._build()

    def _build(self) -> BaseGraph:
        fetch = FetchNode(input="url | local_dir", output=["doc"], node_config={"loader_kwargs": self.config.get("loader_kwargs", {})})
        parse = ParseNode(input="doc", output=["parsed_doc"], node_config={"chunk_size": 4096})
        rag   = RAGNode(input="user_prompt & parsed_doc", output=["relevant_chunks"], node_config={"llm_model": self.config["llm"], "embedder_model": self.config["embeddings"]})
        gen   = GenerateAnswerNode(input="user_prompt & relevant_chunks", output=["answer"], node_config={"llm_model": self.config["llm"], "schema": self.schema})

        return BaseGraph(
            nodes=[fetch, parse, rag, gen],
            edges=[(fetch, parse), (parse, rag), (rag, gen)],
            entry_point=fetch,
            graph_name="MyGraph",
        )

    def run(self):
        state = {"user_prompt": self.prompt, "url": self.source}
        final, _ = self.graph.execute(state)
        return final.get("answer", {})
```

This is essentially what `SmartScraperGraph` does internally. Inheriting `AbstractGraph` (which most built-ins do) gives you the `.run()` plumbing for free; the pattern above shows the raw bones.

**Add your own node** by subclassing `BaseNode`:

```python
from scrapegraphai.nodes.base_node import BaseNode

class FilterNode(BaseNode):
    def __init__(self, predicate):
        super().__init__("FilterNode", "node", input="parsed_doc", output=["parsed_doc"])
        self.predicate = predicate
    def execute(self, state):
        state["parsed_doc"] = [c for c in state["parsed_doc"] if self.predicate(c)]
        return state
```

Insert it between `ParseNode` and `RAGNode` to drop chunks before retrieval (e.g. discard nav/footer noise that survived parsing).

## Recipe 7: PDF / local-file extraction

**When**: you have a folder of PDFs (annual reports, datasheets) and want the same extraction on each.

```python
from scrapegraphai.graphs import PDFScraperMultiGraph
from pathlib import Path

pdfs = [str(p) for p in Path("./reports").glob("*.pdf")]
graph = PDFScraperMultiGraph(
    prompt="Extract: company, fiscal_year, total_revenue_usd, net_income_usd, headcount.",
    source=pdfs,
    config=cfg,
    schema=ReportList,
)
```

Same pattern works for CSV / JSON / XML / Markdown via the `*ScraperGraph` family.

## Recipe 8: When ScrapeGraphAI is the **wrong** tool

Pick something else if:

| Symptom | Better tool |
|---|---|
| You'll scrape one site shape millions of times | Hand-written Playwright + selectors. ScrapeGraphAI's per-page LLM cost dominates. |
| You need login-walled or anti-bot–protected sites at scale | Apify, Bright Data, or a dedicated scraping API. ScrapeGraphAI's stealth is light. |
| You want to crawl an entire site recursively | Scrapy or `crawlee` for the crawl, ScrapeGraphAI for the per-page extraction step. |
| The page has structured data already (JSON-LD, OpenGraph, an RSS feed) | Just parse those directly. No LLM needed. |

ScrapeGraphAI shines for: small-to-medium volume, schema-driven extraction across **varied** sites where writing per-site selectors would be more work than running an LLM.
