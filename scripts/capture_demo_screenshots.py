"""Capture demo screenshots of the Cold-Email Research Assistant UI.

Usage:
    python scripts/capture_demo_screenshots.py

Requires the FastAPI app to already be running on http://127.0.0.1:8765
(uvicorn app:app --host 127.0.0.1 --port 8765). The script captures:

    01_empty_form.png       — Initial UI state
    02_filled_form.png      — Form filled with example inputs + advanced expanded
    03_profiles_modal.png   — Sender-profile manager modal
    04_results.png          — Results view (insights + LinkedIn + email) rendered
                              from a real prior run injected via the page's
                              render() function so we don't spend tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765/"
OUT = Path(__file__).resolve().parent.parent / "output" / "demo_deck"
OUT.mkdir(parents=True, exist_ok=True)

# Real result bundle reconstructed from logs/20260514T115120_f48af7be.json so
# the results screenshot looks like a genuine run without burning tokens.
SAMPLE_RESULT = {
    "bundle": {
        "items": [
            {
                "title": "Thredd Accelerates in 2026 with its Global Platform and Delivering Enterprise-Scale Outcomes",
                "category": "press",
                "published_date": "2026-05-14",
                "summary": "Thredd launches a fully cloud-native payments platform, expands geographic reach, and announces new enterprise customers — signalling its push into embedded finance at scale.",
                "url": "https://via.ritzau.dk/pressemeddelelse/14825826/thredd-accelerates-in-2026-with-its-global-platform-and-delivering-enterprise-scale-outcomes?lang=en",
            },
            {
                "title": "Thredd partners with PayMongo to expand card issuing in Southeast Asia",
                "category": "partnership",
                "published_date": "2026-05-08",
                "summary": "A new processing partnership lets PayMongo issue Visa cards across the Philippines using Thredd's platform — Thredd's third major APAC win this quarter.",
                "url": "https://www.pymnts.com/news/partnerships-acquisitions/2026/thredd-paymongo-card-issuing-philippines/",
            },
            {
                "title": "Q1 funding round: $30M Series C closes",
                "category": "funding",
                "published_date": "2026-04-22",
                "summary": "Thredd closes a $30M Series C led by existing investors to accelerate cloud-platform rollout and expand the engineering org.",
                "url": "https://finance.yahoo.com/news/thredd-series-c-30m-funding-2026/",
            },
        ],
        "linkedin_posts": [
            {
                "posted_date": "2026-05-12",
                "text": "Excited to share that Thredd's new cloud-native platform is now live for enterprise customers — a huge milestone for the team after 18 months of work. Embedded finance is finally getting the infrastructure it deserves.",
                "url": "https://www.linkedin.com/posts/thredd-ceo_cloud-native-launch-2026",
                "engagement": "342 reactions · 28 comments",
            },
            {
                "posted_date": "2026-05-03",
                "text": "Hiring across engineering and compliance in London, Singapore and Sydney. If you've shipped payments infrastructure at scale, let's talk.",
                "url": "https://www.linkedin.com/posts/thredd-ceo_hiring-2026",
                "engagement": "187 reactions · 12 comments",
            },
        ],
        "news_status": "ok",
        "news_status_detail": "3 items kept after recency filter (last 30 days)",
        "linkedin_status": "ok",
        "linkedin_status_detail": "2 posts from Apify · 24h cache hit",
        "warnings": [],
        "estimated_cost_usd": 0.0044,
    },
    "email": (
        "Subject: Supporting Thredd's Growth in Fintech Engineering\n\n"
        "Hi there,\n\n"
        "I noticed Thredd's exciting acceleration phase for 2026, particularly with the launch "
        "of your cloud-native platform. This expansion likely creates a need for strong "
        "engineering and compliance solutions to support your embedded finance capabilities. "
        "At Panasa, we specialize in AI-accelerated fintech engineering and governance, helping "
        "companies like yours scale effectively and ensure regulatory alignment.\n\n"
        "Could we schedule a 15-minute discovery call to discuss how we can support Thredd's "
        "growth?\n\n"
        "Best regards,\n"
        "[Your Name]\n"
        "Panasa"
    ),
}


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Wider viewport keeps form on one line; we crop after by element bbox.
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(URL, wait_until="networkidle")

        # 1) Empty form — capture the form card (the first .card after the
        # subtitle) tightly so no dead canvas ends up in the slide.
        form_card = page.locator("div.container > div.card").first
        form_card.screenshot(path=str(OUT / "01_empty_form.png"))
        print("[ok] 01_empty_form.png")

        # 2) Filled form with Advanced settings expanded — same bbox approach.
        page.fill("#company_url", "https://www.thredd.ai/")
        page.fill("#company_name", "Thredd")
        page.fill("#linkedin_url", "https://www.linkedin.com/in/example-ceo/")
        page.fill("#recipient_name", "Jane Doe")
        page.fill("#recipient_role", "VP of Engineering")
        page.evaluate("document.querySelector('details.advanced').open = true;")
        page.wait_for_timeout(250)
        form_card.screenshot(path=str(OUT / "02_filled_form.png"))
        print("[ok] 02_filled_form.png")

        # 3) Profiles modal — capture just the modal panel, not the backdrop.
        page.click("#manage-profiles-btn")
        page.wait_for_selector("#profiles-modal:not(.hidden)")
        page.wait_for_timeout(400)
        page.locator("#profiles-modal .modal").screenshot(path=str(OUT / "03_profiles_modal.png"))
        print("[ok] 03_profiles_modal.png")
        page.click("#close-modal")
        page.wait_for_timeout(150)

        # 4) Results view (inject a real-shaped payload through page.render)
        page.evaluate(
            "(data) => { lastSubmittedPayload = {disable_web_search:false, disable_linkedin:false, require_context_for_email:true}; render(data); }",
            SAMPLE_RESULT,
        )
        page.evaluate("document.querySelectorAll('details.insight').forEach(d => d.open = true)")
        page.wait_for_timeout(400)
        page.locator("#results").screenshot(path=str(OUT / "04_results.png"))
        print("[ok] 04_results.png")

        browser.close()
    print(f"saved to {OUT}")


if __name__ == "__main__":
    main()
