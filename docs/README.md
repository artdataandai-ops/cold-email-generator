# Docs index

This folder is split into two tracks:

- **The current system** (start here if you're presenting or onboarding to this repo):
  - **[00-architecture.md](00-architecture.md)** — Plain-English end-to-end flow with a mermaid diagram and a "what changed" table.
  - **[05-our-usecase.md](05-our-usecase.md)** — How this project's code is wired, file by file, with the design decisions called out.
- **ScrapeGraphAI library concepts** (kept for reference — most of these are still useful, but be aware that the codebase today uses ScrapeGraphAI's `ChromiumLoader` only as a Playwright fallback, not as the primary engine):
  - **[01-concepts.md](01-concepts.md)** — What ScrapeGraphAI is and the graph mental model.
  - **[02-anatomy.md](02-anatomy.md)** — How a graph actually runs: nodes, state dict, edges.
  - **[03-pipeline-types.md](03-pipeline-types.md)** — Built-in pipelines (SmartScraperGraph, SearchGraph, *Multi* variants, …).
  - **[04-config-cheatsheet.md](04-config-cheatsheet.md)** — Every key in `graph_config`.
  - **[06-recipes.md](06-recipes.md)** — Patterns for new use cases.
  - **[07-gotchas-and-cost.md](07-gotchas-and-cost.md)** — Token spend, anti-bot, debugging.

## Presentation-mode reading order

For a presentation about how the system works end-to-end, you only need:

1. **[00-architecture.md](00-architecture.md)** — the mermaid diagram and "big architectural shifts" table will carry most slides.
2. **[05-our-usecase.md](05-our-usecase.md)** — file-by-file map and the eight "design decisions behind the code" anchors give you the depth answers when someone asks "but why?"

## TL;DR

> Give it a URL and a natural-language prompt. It loads the page, chunks it, embeds the chunks, retrieves the relevant ones, asks an LLM to extract a JSON answer that matches your Pydantic schema, and returns a Python dict. The "graph" is just the *fixed sequence of those steps* — you can swap nodes in/out to build your own pipeline.

That's the whole library in one paragraph. Everything else is configuration and convenience.

## Reference

- Official repo: <https://github.com/ScrapeGraphAI/Scrapegraph-ai>
- Read-the-docs: <https://scrapegraph-ai.readthedocs.io/>
- Docusaurus: <https://docs-oss.scrapegraphai.com/>
- Managed cloud API (different product): <https://docs.scrapegraphai.com/>
