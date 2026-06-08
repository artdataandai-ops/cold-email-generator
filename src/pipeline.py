from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from . import sender_profiles, settings
from .company import discover_pages, extract_company_news
from .config import APIFY_TOKEN
from .cost_guard import CostMeter
from .email_writer import draft_email
from .linkedin import fetch_linkedin
from .linkedin_filter import filter_linkedin_posts
from .query_log import QueryLogger
from .recency import dedupe_news, filter_recent_news, filter_recent_posts
from .relevance import rank_news
from .rss import fetch_rss_items
from .schemas import ResearchBundle
from .search import web_search_company

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    bundle: ResearchBundle
    email: str
    log_path: Optional[str] = None


def research_pipeline(
    company_url: str,
    linkedin_url: Optional[str] = None,
    *,
    company_name: Optional[str] = None,
    recipient_name: Optional[str] = None,
    recipient_role: Optional[str] = None,
    sender_profile_id: Optional[str] = None,
    recency_days: Optional[int] = None,
    max_discovered_urls: Optional[int] = None,
    max_news_items: Optional[int] = None,
    search_max_results: Optional[int] = None,
    disable_web_search: Optional[bool] = None,
    linkedin_post_limit: Optional[int] = None,
    intent: Optional[str] = None,
    disable_linkedin: Optional[bool] = None,
    require_context_for_email: Optional[bool] = None,
) -> PipelineResult:
    saved = settings.get_settings()
    meter = CostMeter()
    bundle = ResearchBundle(company_url=company_url, linkedin_url=linkedin_url)

    # Resolve precedence: per-request override → DB-saved default → (env, baked into DB on bootstrap).
    eff_recency = recency_days if recency_days is not None else saved.recency_days
    eff_news_cap = max_news_items if max_news_items is not None else saved.max_news_items
    eff_disable_search = disable_web_search if disable_web_search is not None else saved.disable_web_search
    eff_max_urls = max_discovered_urls if max_discovered_urls is not None else saved.max_discovered_urls
    eff_search_results = search_max_results if search_max_results is not None else saved.search_max_results
    eff_linkedin_limit = linkedin_post_limit if linkedin_post_limit is not None else saved.linkedin_post_limit
    eff_intent = (intent or "").strip() or saved.default_intent or ""
    eff_disable_linkedin = disable_linkedin if disable_linkedin is not None else bool(saved.disable_linkedin)
    eff_require_context = (
        require_context_for_email
        if require_context_for_email is not None
        else bool(saved.require_context_for_email)
    )

    sender_profile = sender_profiles.get_profile(sender_profile_id) if sender_profile_id else None
    sender_kb = sender_profile.kb_text if sender_profile else None

    qlog = QueryLogger.start(
        inputs={
            "company_url": company_url,
            "company_name": company_name,
            "linkedin_url": linkedin_url,
            "recipient_name": recipient_name,
            "recipient_role": recipient_role,
            "recency_days": eff_recency,
            "max_discovered_urls": eff_max_urls,
            "max_news_items": eff_news_cap,
            "search_max_results": eff_search_results,
            "disable_web_search": eff_disable_search,
            "linkedin_post_limit": eff_linkedin_limit,
            "disable_linkedin": eff_disable_linkedin,
            "require_context_for_email": eff_require_context,
            "intent": (eff_intent[:200] + "…") if len(eff_intent) > 200 else eff_intent,
            "sender_profile_id": sender_profile_id,
            "sender_profile_name": sender_profile.name if sender_profile else None,
        }
    )

    email = ""
    news_error: Optional[str] = None
    linkedin_error: Optional[str] = None
    # Mutable side-channel that discover_pages populates with sitemap-derived metadata
    # (e.g. {url: {"lastmod": "...", "source": "sitemap"}}). extract_company_news uses
    # the lastmod as a fallback published-date when LLM extraction returns null.
    url_metadata: dict = {}
    try:
        urls = _logged(qlog, "discover_pages", "SmartScraperGraph",
                       {"company_url": company_url, "max_urls": eff_max_urls}, meter,
                       lambda: discover_pages(company_url, meter, max_urls=eff_max_urls,
                                              url_metadata=url_metadata),
                       swallow=True) or [company_url]

        # Lever-1 speedup: extract_company_news, fetch_rss_items, web_search_company,
        # and fetch_linkedin are all independent of each other — they only read
        # the (already-resolved) ``urls`` list or the inputs we have at function
        # entry. Sequentially the four cost ~80s; in parallel the wall clock
        # collapses to max(individual) which is typically the web search step
        # (~40s). Each task uses ``_logged`` exactly as before so query_log
        # records every step independently. ``CostMeter`` is now lock-guarded
        # against concurrent ``+=`` updates from these threads.
        if eff_disable_search:
            search_task = None
        else:
            def _run_search():
                return _logged(qlog, "web_search_company", "SearchGraph",
                               {"company_url": company_url, "max_results": eff_search_results}, meter,
                               lambda: web_search_company(company_url, meter,
                                                          max_results=eff_search_results,
                                                          recency_days=eff_recency,
                                                          company_name=company_name),
                               swallow=True) or []
            search_task = _run_search

        def _run_extract():
            return _logged(qlog, "extract_company_news", "SmartScraperMultiGraph",
                           {"urls": urls, "sitemap_metadata_count": len(url_metadata)}, meter,
                           lambda: extract_company_news(urls, meter, url_metadata=url_metadata),
                           swallow=True) or []

        def _run_rss():
            return _logged(qlog, "fetch_rss_items", "RSS",
                           {"company_url": company_url}, meter,
                           lambda: fetch_rss_items(company_url),
                           swallow=True) or []

        if eff_disable_linkedin:
            linkedin_task = None
        else:
            def _run_linkedin():
                return _logged(qlog, "fetch_linkedin", "Apify",
                               {"linkedin_url": linkedin_url, "post_limit": eff_linkedin_limit}, meter,
                               lambda: fetch_linkedin(linkedin_url, bundle.warnings,
                                                      post_limit=eff_linkedin_limit),
                               swallow=True) or []
            linkedin_task = _run_linkedin

        # Submit the parallel tasks. We keep references in a dict so we can
        # collect by name (instead of by position) — clearer for the typical
        # case where some tasks are skipped.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                "extract": pool.submit(_run_extract),
                "rss": pool.submit(_run_rss),
            }
            if search_task is not None:
                futures["search"] = pool.submit(search_task)
            if linkedin_task is not None:
                futures["linkedin"] = pool.submit(linkedin_task)

            site_items = futures["extract"].result()
            rss_items = futures["rss"].result()
            search_items = futures["search"].result() if "search" in futures else []
            raw_posts = futures["linkedin"].result() if "linkedin" in futures else []

        # For the disabled paths the step log entry still needs to exist so the
        # JSON report shape stays consistent across runs. We write those AFTER
        # the parallel work so the disabled-step rows don't interleave with
        # real timings.
        if eff_disable_search:
            with qlog.step("web_search_company", graph="SearchGraph",
                           input={"disabled": True}) as ctx:
                ctx.set_output({"skipped": "disable_web_search=true"})
                ctx.set_cost(delta=0.0, cumulative=meter.spent_usd)
        if eff_disable_linkedin:
            with qlog.step("fetch_linkedin", graph="Apify",
                           input={"linkedin_url": linkedin_url, "disabled": True}) as ctx:
                ctx.set_output({"skipped": "disable_linkedin=true"})
                ctx.set_cost(delta=0.0, cumulative=meter.spent_usd)

        if not site_items and not rss_items and not search_items:
            # All upstream news sources returned nothing; check if there were any errors logged.
            recent_errors = [s for s in qlog.steps[-4:] if s.status == "error"]
            news_error = recent_errors[0].error if recent_errors else None

        all_items = site_items + rss_items + search_items
        with qlog.step("filter_dedupe_rank", input={
            "raw_count": len(all_items),
            "cap": eff_news_cap,
            "recency_days": eff_recency,
            "recipient_role": recipient_role,
            "sender_kb_chars": len(sender_kb) if sender_kb else 0,
        }) as ctx:
            filtered = filter_recent_news(all_items, days=eff_recency)
            deduped = dedupe_news(filtered)
            ranked = rank_news(deduped, sender_kb=sender_kb, recipient_role=recipient_role)
            capped = ranked[:eff_news_cap]
            bundle.items = capped
            ctx.set_output({
                "after_recency_filter": len(filtered),
                "after_dedupe": len(deduped),
                "after_rank": len(ranked),
                "kept": len(capped),
                "top_titles": [it.title[:80] for it in capped[:3]],
            })
            ctx.set_cost(delta=0.0, cumulative=meter.spent_usd)

        # raw_posts came from the parallel fan-out block earlier (or [] if
        # eff_disable_linkedin). Date filter still runs here so it sees the
        # current ``eff_recency`` value.
        date_filtered_posts = filter_recent_posts(raw_posts, days=eff_recency)

        ranked_posts = _logged(qlog, "filter_linkedin_posts", None,
                               {"raw_count": len(raw_posts), "after_date_filter": len(date_filtered_posts),
                                "recipient_role": recipient_role},
                               meter,
                               lambda: filter_linkedin_posts(
                                   date_filtered_posts,
                                   recipient_role=recipient_role,
                                   company_items=bundle.items,
                                   meter=meter,
                               ),
                               swallow=True) or []
        bundle.linkedin_posts = ranked_posts

        # Determine LinkedIn status for the UI
        if eff_disable_linkedin:
            bundle.linkedin_status = "skipped"
            bundle.linkedin_status_detail = "LinkedIn fetch disabled in settings."
        elif not linkedin_url:
            bundle.linkedin_status = "skipped"
            bundle.linkedin_status_detail = "No LinkedIn URL provided."
        elif not APIFY_TOKEN:
            bundle.linkedin_status = "skipped"
            bundle.linkedin_status_detail = "Apify token not configured."
        elif not raw_posts:
            bundle.linkedin_status = "empty"
            bundle.linkedin_status_detail = "Apify returned no posts (profile inactive, or fetch blocked)."
        elif not date_filtered_posts:
            bundle.linkedin_status = "empty"
            bundle.linkedin_status_detail = f"No posts within the last {eff_recency} days."
        elif not ranked_posts:
            bundle.linkedin_status = "empty"
            bundle.linkedin_status_detail = "All recent posts were pure reposts (filtered out)."
        else:
            bundle.linkedin_status = "ok"
            bundle.linkedin_status_detail = f"{len(ranked_posts)} relevant post(s)."

        # Determine news status for the UI
        if bundle.items:
            bundle.news_status = "ok"
            bundle.news_status_detail = f"{len(bundle.items)} recent item(s)."
        elif news_error:
            bundle.news_status = "error"
            bundle.news_status_detail = f"Fetch failed: {news_error}"
        else:
            bundle.news_status = "empty"
            bundle.news_status_detail = f"No news found in the last {eff_recency} days."

        has_context = bool(bundle.items) or bool(bundle.linkedin_posts)
        if eff_require_context and not has_context:
            with qlog.step("draft_email", input={
                "items_count": 0,
                "posts_count": 0,
                "require_context_for_email": True,
            }) as ctx:
                ctx.set_output({"skipped": "no context and require_context_for_email=true"})
                ctx.set_cost(delta=0.0, cumulative=meter.spent_usd)
            email = ""
            bundle.warnings.append(
                "Email drafting skipped: no news or LinkedIn context found "
                "(require_context_for_email is on)."
            )
        else:
            # draft_email now returns the full DraftedEmail (reasoning trail +
            # email body) so the per-run JSON log captures *why* the LLM chose
            # the angle it did, not just the final email. _summarize_output
            # serialises the Pydantic model in full.
            drafted = _logged(qlog, "draft_email", None,
                              {
                                  "items_count": len(bundle.items),
                                  "posts_count": len(bundle.linkedin_posts),
                                  "recipient_name": recipient_name,
                                  "recipient_role": recipient_role,
                                  "kb_chars": len(sender_kb) if sender_kb else 0,
                                  "intent_chars": len(eff_intent),
                                  "news_status": bundle.news_status,
                                  "linkedin_status": bundle.linkedin_status,
                              },
                              meter,
                              lambda: draft_email(
                                  bundle.items,
                                  bundle.linkedin_posts,
                                  recipient_name=recipient_name,
                                  recipient_role=recipient_role,
                                  company_url=company_url,
                                  meter=meter,
                                  sender_kb=sender_kb,
                                  intent=eff_intent,
                              ),
                              swallow=True)
            email = drafted.email if drafted is not None else ""

    except Exception as e:
        logger.exception("research_pipeline failed")
        bundle.warnings.append(f"Pipeline error: {e}")

    bundle.estimated_cost_usd = round(meter.spent_usd, 4)
    for w in bundle.warnings:
        qlog.warn(w)
    log_path = qlog.finish(
        result=PipelineResult(bundle=bundle, email=email),
        email_text=email,
        meter=meter,
    )
    return PipelineResult(bundle=bundle, email=email, log_path=str(log_path))


def _logged(qlog: QueryLogger, name: str, graph_name: Optional[str], step_input: dict,
            meter: CostMeter, fn, *, swallow: bool = False):
    spent_before = meter.spent_usd
    with qlog.step(name, graph=graph_name, input=step_input) as ctx:
        try:
            output = fn()
        except Exception as e:
            ctx.set_cost(delta=meter.spent_usd - spent_before, cumulative=meter.spent_usd)
            if swallow:
                ctx.set_output({"error_swallowed": f"{type(e).__name__}: {e}"})
                qlog.warn(f"{name} failed: {e}")
                return None
            raise
        ctx.set_output(_summarize_output(output))
        ctx.set_cost(delta=meter.spent_usd - spent_before, cumulative=meter.spent_usd)
        return output


def _summarize_output(value):
    if value is None:
        return None
    if isinstance(value, list):
        return {
            "count": len(value),
            "preview": [_summarize_output(v) for v in value[:5]],
        }
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"... [truncated, {len(value)} chars total]"
    return value
