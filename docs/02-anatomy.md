# 02 · Anatomy: nodes, state, and how a graph actually runs

This page opens the box and explains what happens between `graph = SmartScraperGraph(...)` and `result = graph.run()`.

## The state dict

Every graph maintains a **single Python dict** during execution. Each node *reads* keys from it and *writes* keys back to it. By the end, the final node returns the answer.

A typical state dict for `SmartScraperGraph` evolves like this:

```python
# initial
{"user_prompt": "...", "url": "https://..."}

# after FetchNode
{"user_prompt": "...", "url": "...", "doc": [<raw HTML document>]}

# after ParseNode
{... , "parsed_doc": [<list of cleaned text chunks>]}

# after RAGNode
{... , "relevant_chunks": [<top-k chunks most relevant to the prompt>]}

# after GenerateAnswerNode
{... , "answer": {"title": "...", "summary": "...", ...}}
```

`.run()` returns `state["answer"]`. That's the dict you get back.

You don't normally interact with this state directly — but knowing it exists makes everything else click.

## The standard nodes

These live in `scrapegraphai.nodes` and are the building blocks every built-in graph composes from. (The library has more, but these five cover ~90% of what you'll see.)

### 1. `FetchNode` — get the raw page

- Reads: `url` (or `local_dir` for local files).
- Writes: `doc` (the raw fetched content as a list of LangChain `Document`s).
- How: uses Playwright (Chromium) under the hood by default. JS-rendered pages work. `headless` and `loader_kwargs.proxy` control its browser behavior.
- This is the only node that touches the network.

### 2. `ParseNode` — clean the HTML and split it

- Reads: `doc`.
- Writes: `parsed_doc` (a list of text chunks, each small enough to fit in the LLM's context window).
- How: strips boilerplate (nav, scripts, styles), converts to Markdown-ish text, then splits into chunks based on the LLM's `model_tokens` limit. If the page fits in one chunk, the list has one item.

### 3. `RAGNode` — find the relevant parts

- Reads: `parsed_doc`, `user_prompt`.
- Writes: `relevant_chunks`.
- How: embeds every chunk using your `embeddings` model, embeds the user prompt, returns the top-k chunks by cosine similarity. **This is why `embeddings` is required for big pages and SearchGraph** — without embeddings, the library would have to send the whole page to the LLM (expensive, often impossible).
- For small pages that fit in one chunk, this node is effectively a passthrough — and you can sometimes skip configuring `embeddings` for graphs working only on tiny pages.

### 4. `GenerateAnswerNode` — call the LLM

- Reads: `relevant_chunks`, `user_prompt`, optional `schema`.
- Writes: `answer` (a dict).
- How: builds a prompt of the form *"Given these chunks and this user request, return JSON matching this schema,"* calls the chat model, parses the JSON, validates against your Pydantic schema if you passed one. If the page was big enough to produce multiple chunk-batches, this node runs once per batch and a follow-up merges the partial answers.

### 5. `GraphIteratorNode` — run a sub-graph many times

- Used by *Multi* and *Search* variants.
- Takes a list of URLs (or search results), runs an inner `SmartScraperGraph` against each in parallel, collects the answers into a list.
- This is how `SmartScraperMultiGraph` and `SearchGraph` get their parallelism.

There are a handful of others — `ConditionalNode`, `SearchInternetNode`, `TextToSpeechNode`, `MergeAnswersNode`, `GenerateScraperNode` — but you'll meet them only when you read the source of a specific built-in graph or build your own.

## How a graph executes

Every graph subclasses `BaseGraph` and defines two things:

1. **A list of nodes** in execution order.
2. **The edges** — usually a straight line `node1 → node2 → node3`, but `ConditionalNode` lets you branch ("if no answer, retry with a bigger context").

`graph.run()` then:

1. Initializes the state dict from the constructor arguments (`prompt`, `source`, `schema`).
2. Walks the node list, calling each node's `execute(state)` method.
3. Each node mutates the state dict.
4. Returns whatever the final node decided is the result key (typically `"answer"`).

Here's the actual execution sequence for `SmartScraperGraph`, simplified:

```python
class SmartScraperGraph(AbstractGraph):
    def _create_graph(self):
        fetch = FetchNode(...)
        parse = ParseNode(...)
        rag = RAGNode(...)            # only built for big pages
        generate = GenerateAnswerNode(...)
        return BaseGraph(
            nodes=[fetch, parse, rag, generate],
            edges=[(fetch, parse), (parse, rag), (rag, generate)],
            entry_point=fetch,
        )
```

That's the whole graph. Reading the source of any built-in graph is the fastest way to understand what it does — they're all 50–100 lines.

## Why this design is nice

- **Swap any node.** Want to fetch with `requests` instead of Playwright? Replace `FetchNode`. Want a different chunker? Replace `ParseNode`. The rest of the graph is unaffected.
- **Reusability.** `SearchGraph` is literally `SearchInternetNode → GraphIteratorNode(SmartScraperGraph) → MergeAnswersNode`. The library composes complex pipelines out of the same primitives.
- **Visualizable.** With the `burr_kwargs` config option you get a live web UI showing the graph and the state evolving — great for debugging.
- **Predictable cost.** You know exactly which node calls the LLM (only `GenerateAnswerNode`). You can put a timer or a token counter around it and know your spend.

## What this means in practice

When something goes wrong:

- **Empty result?** Probably `FetchNode` got a login wall or a JS-only SPA. Set `headless=False` and watch it run.
- **Garbled output?** Probably `ParseNode` dropped the relevant text into the wrong chunk and `RAGNode` retrieved the wrong one. Increase `top_k` or chunk overlap.
- **Wrong shape?** `GenerateAnswerNode` ignored your schema. Make the schema simpler, or add an explicit "Output JSON: {...}" instruction in the prompt.

Knowing which node owns which behavior turns "the library is broken" into "this specific node isn't doing what I want" — and that's something you can actually fix.
