"""Build a 6-slide combined deck.

Layout strategy: open the Apollo→Bigin reference deck as the base (so it brings
its theme, master and three existing slides), then append three new slides for
the Cold-Email Research Assistant using the *same* visual language — navy
(#1F4E79) header strip, light-grey (#F4F6F9) cards, Calibri throughout, and the
matching footer/page-number band.

Page numbers on the Apollo slides are also updated from "N / 3" to "N / 6" so
the combined deck reads as one document.

Output: output/demo_deck/Marketing_Tools_Demo_Combined.pptx
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "output" / "demo_deck"
APOLLO_SRC = Path(r"C:\Users\artlptp265user\Downloads\multi-list-apollo-bigin\Apollo_Bigin_Integration_Demo.pptx")
OUT = SHOTS / "Marketing_Tools_Demo_Combined.pptx"

# Palette — copied verbatim from Apollo deck so the styling matches exactly.
NAVY = RGBColor(0x1F, 0x4E, 0x79)
NAVY_TXT = RGBColor(0x1F, 0x4E, 0x79)
SOFT = RGBColor(0xF4, 0xF6, 0xF9)
BODY = RGBColor(0x22, 0x2D, 0x3A)
MUTED = RGBColor(0x59, 0x59, 0x59)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT_BLUE = RGBColor(0x4A, 0x7E, 0xB8)
WARN = RGBColor(0xB7, 0x4A, 0x1F)  # warm rust for "hard parts" callouts

FONT = "Calibri"

TOTAL_SLIDES = 7  # 1 cover + 3 Apollo + 3 ours


# ---------------------------------------------------------------------------
# Drawing helpers — all mirror exact dimensions sniffed from Apollo slide 1.
# ---------------------------------------------------------------------------

def add_solid_rect(slide, left, top, width, height, color, *, no_line=True):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    if no_line:
        shape.line.fill.background()
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.shadow.inherit = False
    return shape


def add_text_run(
    slide,
    left,
    top,
    width,
    height,
    text: str,
    *,
    size: int,
    bold: bool = False,
    color: RGBColor = BODY,
    align=PP_ALIGN.LEFT,
    anchor=None,
    font: str = FONT,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.02)
    tf.margin_bottom = Inches(0.02)
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = font
    return tb


def add_paragraph_block(
    slide,
    left,
    top,
    width,
    height,
    text: str,
    *,
    size: int = 13,
    color: RGBColor = BODY,
    line_spacing: float = 1.3,
    font: str = FONT,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.02)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    p.line_spacing = line_spacing
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.name = font
    return tb


def add_feature_bullets(
    slide,
    left,
    top,
    width,
    height,
    items: list[tuple[str, str]],
    *,
    size: int = 12,
    lead_color: RGBColor = NAVY_TXT,
    body_color: RGBColor = BODY,
    bullet_color: RGBColor = NAVY_TXT,
    line_spacing: float = 1.25,
    space_after_pt: int = 6,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.02)
    for i, (lead, body) in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.line_spacing = line_spacing
        p.space_after = Pt(space_after_pt)
        bullet = p.add_run()
        bullet.text = "•  "
        bullet.font.size = Pt(size)
        bullet.font.bold = True
        bullet.font.color.rgb = bullet_color
        bullet.font.name = FONT
        head = p.add_run()
        head.text = f"{lead}  "
        head.font.size = Pt(size)
        head.font.bold = True
        head.font.color.rgb = lead_color
        head.font.name = FONT
        tail = p.add_run()
        tail.text = body
        tail.font.size = Pt(size)
        tail.font.color.rgb = body_color
        tail.font.name = FONT
    return tb


def add_image_fitted(slide, image_path: Path, left, top, max_w, max_h):
    with Image.open(image_path) as im:
        iw, ih = im.size
    scale = min(max_w / iw, max_h / ih)
    w = int(iw * scale)
    h = int(ih * scale)
    cx = left + (max_w - w) / 2
    cy = top + (max_h - h) / 2
    return slide.shapes.add_picture(str(image_path), int(cx), int(cy), width=int(w), height=int(h))


def add_chrome(slide, *, status_text: str, page_number: int, footer_text: str):
    """Add the navy top strip, navy bottom status bar, page number & footer.

    Dimensions copied verbatim from Apollo slide 1 to match exactly.
    """
    # Top accent strip
    add_solid_rect(slide, Inches(0), Inches(0), Inches(13.33), Inches(0.18), NAVY)
    # Bottom navy status strip
    add_solid_rect(slide, Inches(0.5), Inches(6.6), Inches(12.33), Inches(0.45), NAVY)
    # Status text (white on navy)
    add_text_run(
        slide, Inches(0.7), Inches(6.65), Inches(12.0), Inches(0.35),
        status_text, size=12, bold=True, color=WHITE,
    )
    # Footer text (muted, bottom-left)
    add_text_run(
        slide, Inches(0.4), Inches(7.15), Inches(8.0), Inches(0.3),
        footer_text, size=9, color=MUTED,
    )
    # Page number (muted, bottom-right)
    add_text_run(
        slide, Inches(11.4), Inches(7.15), Inches(1.5), Inches(0.3),
        f"{page_number} / {TOTAL_SLIDES}", size=9, color=MUTED, align=PP_ALIGN.RIGHT,
    )


def add_title_subtitle(slide, *, title: str, subtitle: str):
    add_text_run(
        slide, Inches(0.5), Inches(0.35), Inches(12.3), Inches(0.7),
        title, size=28, bold=True, color=NAVY_TXT,
    )
    add_text_run(
        slide, Inches(0.5), Inches(1.0), Inches(12.3), Inches(0.4),
        subtitle, size=14, color=MUTED,
    )


# ---------------------------------------------------------------------------
# Slide builders for our 3 use-case slides.
# ---------------------------------------------------------------------------

DEMO_FOOTER = "Cold-Email Research Assistant   ·   Marketing Tools Demo"
COVER_FOOTER = "Art Marketing Tools   ·   Demo overview"


def build_cover(prs: Presentation) -> None:
    """Front cover — book-style table of contents. Heading + two TOC lines."""
    s = prs.slides.add_slide(prs.slide_layouts[6])  # Blank

    # Top navy strip (matches every other slide)
    add_solid_rect(s, Inches(0), Inches(0), Inches(13.33), Inches(0.18), NAVY)

    # Heading
    add_text_run(
        s, Inches(0.5), Inches(1.8), Inches(12.3), Inches(1.1),
        "Art Marketing Tools", size=48, bold=True, color=NAVY_TXT,
    )

    # Section divider under the heading
    add_solid_rect(s, Inches(0.5), Inches(3.0), Inches(2.5), Inches(0.04), NAVY)

    def toc_line(top_in: float, title: str, page: str):
        # Tool title (left)
        add_text_run(
            s, Inches(0.5), Inches(top_in), Inches(9.5), Inches(0.6),
            title, size=22, bold=True, color=NAVY_TXT,
        )
        # Page number (right, monospace-aligned)
        add_text_run(
            s, Inches(10.0), Inches(top_in), Inches(2.83), Inches(0.6),
            page, size=22, bold=True, color=NAVY_TXT, align=PP_ALIGN.RIGHT,
        )

    toc_line(3.55, "Apollo → Zoho Bigin Sync",          "2")
    toc_line(4.55, "Cold-Email Research Assistant",     "5")

    add_chrome(
        s,
        status_text="Two production tools  ·  built for the Art & Storia marketing teams",
        page_number=1,
        footer_text=COVER_FOOTER,
    )


def move_slide_to_front(prs: Presentation, source_index: int) -> None:
    """Move the slide currently at `source_index` to position 0 in the deck.

    python-pptx only ever appends slides, so we manipulate the underlying
    <p:sldIdLst> XML directly to reorder. Safe — no slide contents are touched.
    """
    sld_id_lst = prs.slides._sldIdLst
    children = list(sld_id_lst)
    target = children[source_index]
    sld_id_lst.remove(target)
    sld_id_lst.insert(0, target)


def build_slide_4(prs: Presentation) -> None:
    """Cold-Email Research Assistant — Problem / Solution / What it does."""
    s = prs.slides.add_slide(prs.slide_layouts[6])  # Blank

    add_title_subtitle(
        s,
        title="Cold-Email Research Assistant",
        subtitle="Paste a company URL → personalized cold email in ~30–90 seconds — for Art & Storia marketing.",
    )

    # Left column: The problem
    add_text_run(
        s, Inches(0.5), Inches(1.6), Inches(6.0), Inches(0.4),
        "The problem", size=16, bold=True, color=NAVY_TXT,
    )
    add_paragraph_block(
        s, Inches(0.5), Inches(2.0), Inches(6.0), Inches(1.4),
        "Sales / marketing reps spent the best part of an hour per prospect doing the "
        "same research loop: searching recent news and funding, finding the contact's "
        "LinkedIn, scrolling for posts worth referencing, and synthesizing it all into a "
        "personalized email. At scale it collapses into generic templates — and reply "
        "rates suffer.",
        size=13, color=BODY,
    )

    # Left column: The solution
    add_text_run(
        s, Inches(0.5), Inches(3.5), Inches(6.0), Inches(0.4),
        "The solution", size=16, bold=True, color=NAVY_TXT,
    )
    add_paragraph_block(
        s, Inches(0.5), Inches(3.9), Inches(6.0), Inches(2.5),
        "A single-page internal tool. The rep pastes the company URL (and optionally the "
        "contact's LinkedIn URL), picks a sender profile + intent preset, and clicks "
        "Generate. Within 30–90 seconds the UI shows recent company news, third-party "
        "press, recent LinkedIn posts, and a draft cold email anchored on the freshest "
        "hook — ready to copy.",
        size=13, color=BODY,
    )

    # Right column: light-grey card with "What it does"
    add_solid_rect(s, Inches(7.0), Inches(1.6), Inches(5.85), Inches(5.2), SOFT)
    add_text_run(
        s, Inches(7.25), Inches(1.75), Inches(5.35), Inches(0.4),
        "What it does", size=16, bold=True, color=NAVY_TXT,
    )
    add_feature_bullets(
        s, Inches(7.25), Inches(2.25), Inches(5.35), Inches(4.3),
        [
            ("Company-site news scraping.",
             "httpx fetches the page, Trafilatura strips chrome (nav, footer, cookie banners), and an LLM pulls out title, summary, date and category from what's left. Pages with structured JSON-LD skip the model entirely; Playwright steps in only when httpx fails."),
            ("Third-party web coverage.",
             "DuckDuckGo finds press the prospect didn't publish themselves; the LLM extracts the same fields from each result page, and a brand-keyword check drops anything off-topic."),
            ("LinkedIn posts via Apify.",
             "Apify's actor pulls the posts (LinkedIn blocks free scraping). The LLM then scores each one for anchor quality, plus topics and sentiment. 24h disk cache to keep cost down."),
            ("60-day recency filter.",
             "Trafilatura reads each article's published date from page metadata, and a today − 60d window drops anything older. No model judgement on what counts as recent."),
            ("Relevance ranking + sender profiles.",
             "Five hand-tuned signals decide what rises to the top — category weight, recency decay, regex for \"raises $X\" / \"Series B\" / \"acquires\", overlap with the sender's KB, and recipient-role synonyms. Profiles live in SQLite and feed the email prompt."),
            ("Email reasoning + body.",
             "Before the 80–120 word draft, the LLM picks which insight to anchor on and reasons through business implication, sender fit, intent alignment and pitch angle — six structured fields, visible in every JSON log."),
        ],
        size=12, line_spacing=1.2,
    )

    add_chrome(
        s,
        status_text="Status: deployed (Docker)   |   Stack: FastAPI · httpx · Trafilatura · ddgs · Apify · OpenAI gpt-4o-mini",
        page_number=5,
        footer_text=DEMO_FOOTER,
    )


def build_slide_5(prs: Presentation) -> None:
    """How it works — the user's view. Two step cards with UI screenshots."""
    s = prs.slides.add_slide(prs.slide_layouts[6])

    add_title_subtitle(
        s,
        title="How it works — the user's view",
        subtitle="Prerequisite: rep has a company URL (and optionally the contact's LinkedIn URL). Then:",
    )

    # Two columns of step cards — geometry copied from Apollo slide 2.
    def step_card(left_in, number: int, title_text: str, image_path: Path, caption: str):
        # Outer card
        add_solid_rect(s, Inches(left_in), Inches(1.55), Inches(6.15), Inches(5.0), SOFT)
        # Numbered navy badge
        badge = add_solid_rect(s, Inches(left_in + 0.15), Inches(1.67), Inches(0.45), Inches(0.45), NAVY)
        # Centre the digit inside the badge
        tb = s.shapes.add_textbox(Inches(left_in + 0.15), Inches(1.67), Inches(0.45), Inches(0.45))
        tf = tb.text_frame
        tf.word_wrap = False
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = str(number)
        run.font.size = Pt(16)
        run.font.bold = True
        run.font.color.rgb = WHITE
        run.font.name = FONT
        # Step title
        add_text_run(
            s, Inches(left_in + 0.70), Inches(1.65), Inches(5.30), Inches(0.5),
            title_text, size=13, bold=True, color=NAVY_TXT,
        )
        # Image area (slightly darker grey ring like the Apollo slide)
        img_left = Inches(left_in + 0.45)
        img_top = Inches(2.19)
        img_w = Inches(5.30)
        img_h = Inches(3.32)
        add_solid_rect(s, img_left, img_top, img_w, img_h, SOFT)
        add_image_fitted(s, image_path, img_left + Inches(0.05), img_top + Inches(0.05), img_w - Inches(0.1), img_h - Inches(0.1))
        # Caption below the image
        add_paragraph_block(
            s, Inches(left_in + 0.15), Inches(5.55), Inches(5.85), Inches(1.0),
            caption, size=11, color=BODY, line_spacing=1.25,
        )

    step_card(
        left_in=0.39,
        number=1,
        title_text="Fill the form",
        image_path=SHOTS / "02_filled_form.png",
        caption=(
            "The rep enters the company URL, optionally the recipient's LinkedIn URL, picks a "
            "sender profile (their offering / ICP / tone notes) and an intent preset, then "
            "expands Advanced to set the recency window, web-search and LinkedIn toggles, and "
            "post limits if needed."
        ),
    )

    step_card(
        left_in=6.79,
        number=2,
        title_text="Get news + LinkedIn + draft email",
        image_path=SHOTS / "04_results.png",
        caption=(
            "Within 30–90 seconds the UI shows recent company-site and third-party news, recent "
            "LinkedIn posts, and a draft cold email anchored on the freshest hook. The rep clicks "
            "Copy and sends from their normal mail client."
        ),
    )

    # Right-arrow between the two cards (matches Apollo slide 2)
    arrow = s.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW,
        Inches(6.45), Inches(3.55), Inches(0.5), Inches(0.5),
    )
    arrow.line.fill.background()
    arrow.fill.solid()
    arrow.fill.fore_color.rgb = NAVY
    arrow.shadow.inherit = False

    add_chrome(
        s,
        status_text="V1 HubSpot UI  →  V2 HubSpot workflow  →  V3 Bigin standalone UI (this build)",
        page_number=6,
        footer_text=DEMO_FOOTER,
    )


def build_slide_6(prs: Presentation) -> None:
    """Under the hood — architecture, stack, free-scraping tradeoffs."""
    s = prs.slides.add_slide(prs.slide_layouts[6])

    add_title_subtitle(
        s,
        title="Under the hood",
        subtitle="How free-tier scraping survives anti-bot defences, JS-heavy pages, and tight token budgets.",
    )

    # ---- Architecture row (5 boxes connected by arrows) ----
    arch_top = Inches(1.55)
    arch_h = Inches(1.0)

    def arch_box(left_in, w_in, text: str, *, fill=NAVY, color=WHITE):
        rect = add_solid_rect(s, Inches(left_in), arch_top, Inches(w_in), arch_h, fill)
        tb = s.shapes.add_textbox(Inches(left_in), arch_top, Inches(w_in), arch_h)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = Inches(0.05)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = text
        run.font.size = Pt(11)
        run.font.bold = True
        run.font.color.rgb = color
        run.font.name = FONT

    def arch_arrow(left_in):
        a = s.shapes.add_shape(
            MSO_SHAPE.RIGHT_ARROW, Inches(left_in), arch_top + Inches(0.32), Inches(0.35), Inches(0.36),
        )
        a.line.fill.background()
        a.fill.solid()
        a.fill.fore_color.rgb = NAVY
        a.shadow.inherit = False

    arch_box(0.5,  2.1, "Company URL\n(rep input)", fill=ACCENT_BLUE)
    arch_arrow(2.65)
    arch_box(3.05, 2.4, "Fetch + LLM extract\nhttpx · Trafilatura · ddgs · Apify")
    arch_arrow(5.50)
    arch_box(5.90, 2.0, "Rank + recency\n+ dedupe")
    arch_arrow(7.95)
    arch_box(8.35, 2.0, "LLM reasoning\n+ draft email")
    arch_arrow(10.40)
    arch_box(10.80, 2.05, "Bigin / Mail\n(rep copies)", fill=ACCENT_BLUE)

    add_paragraph_block(
        s, Inches(0.5), Inches(2.7), Inches(12.3), Inches(0.45),
        "Fetching goes via httpx / Playwright (company sites), DuckDuckGo (web search) and Apify (LinkedIn); the LLM extracts structured fields from each. "
        "Ranking, recency and dedupe are pure Python. The model returns at the end to reason about the angle and draft the email.",
        size=11, color=MUTED, line_spacing=1.2,
    )

    # ---- Two-column body: hard parts (left) + how we handle them (right) ----
    body_top = Inches(3.3)

    add_text_run(
        s, Inches(0.5), body_top, Inches(6.0), Inches(0.4),
        "The hard parts of free-tier scraping", size=14, bold=True, color=NAVY_TXT,
    )
    add_feature_bullets(
        s, Inches(0.5), body_top + Inches(0.45), Inches(6.0), Inches(3.0),
        [
            ("LinkedIn blocks scrapers.",
             "Login walls, fingerprinting, and per-IP rate limits stop naive HTTP requests."),
            ("Sites are JS-heavy.",
             "Plain HTTP returns shells; data only appears after a real browser executes scripts."),
            ("\"Is this recent?\".",
             "Dates are inconsistent or missing across press pages, RSS, and third-party news."),
            ("Token cost balloons.",
             "Long pages × LLM calls × multiple sources adds up quickly without bounds."),
        ],
        size=11, lead_color=NAVY_TXT, body_color=BODY, bullet_color=WARN, line_spacing=1.2,
    )

    # Right column — light-grey card
    right_left = Inches(7.0)
    right_w = Inches(5.85)
    add_solid_rect(s, right_left, body_top, right_w, Inches(3.45), SOFT)
    add_text_run(
        s, right_left + Inches(0.25), body_top + Inches(0.12), right_w - Inches(0.5), Inches(0.4),
        "How we handle them", size=14, bold=True, color=NAVY_TXT,
    )
    add_feature_bullets(
        s, right_left + Inches(0.25), body_top + Inches(0.55),
        right_w - Inches(0.5), Inches(2.85),
        [
            ("httpx fast path + Playwright fallback.",
             "~0.3-1s httpx fetch first; escalate to Playwright (stealth, randomized UA) only on failure or sub-1KB pages."),
            ("Trafilatura strips chrome.",
             "Removes nav / footer / cookie banners so the LLM sees only the article body — fewer tokens, fewer hallucinations."),
            ("URL constraints + Pydantic schemas.",
             "BeautifulSoup extracts the page's real <a href> set; LLM output is constrained to those URLs, dropping fabricated links at validation."),
            ("Apify for LinkedIn only.",
             "Apify pays for the anti-bot work; we call their actor with a 24h disk cache to keep spend down."),
            ("Recency, ranking + cost guard.",
             "A today − 60d window drops stale items, a 5-signal heuristic ranks what's left, per-domain disk cache keeps repeat runs cheap, and a $0.20-per-request hard cap stops any single LLM-heavy run from running away."),
        ],
        size=11, line_spacing=1.2,
    )

    add_chrome(
        s,
        status_text="Endpoints: POST /api/research · GET-PUT /api/settings · CRUD /api/sender-profiles · GET /api/health",
        page_number=7,
        footer_text=DEMO_FOOTER,
    )


# ---------------------------------------------------------------------------
# Apollo slide tweaks: bump page numbers from "N / 3" to "N / 6".
# ---------------------------------------------------------------------------

def update_apollo_page_numbers(prs: Presentation) -> None:
    """Bump Apollo page numbers so they reflect their new position in the combined deck.

    Apollo's existing labels "1 / 3", "2 / 3", "3 / 3" become "2 / 7", "3 / 7",
    "4 / 7" — i.e. the cover slide is page 1, Apollo slides shift down by one.
    The regex matches "<digit> / 3" exactly so we never touch other text runs.
    """
    import re
    pat = re.compile(r"^\s*(\d+)\s*/\s*3\s*$")
    for slide in list(prs.slides)[:3]:  # only the original Apollo slides
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    m = pat.match(run.text or "")
                    if m:
                        run.text = f"{int(m.group(1)) + 1} / {TOTAL_SLIDES}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    prs = Presentation(APOLLO_SRC)
    update_apollo_page_numbers(prs)
    build_slide_4(prs)
    build_slide_5(prs)
    build_slide_6(prs)
    # Cover gets appended last and then moved to the front so it can be authored
    # against the loaded theme without disturbing the existing slide order until
    # the very end.
    build_cover(prs)
    move_slide_to_front(prs, source_index=len(prs.slides) - 1)

    target = OUT
    try:
        prs.save(target)
    except PermissionError:
        target = OUT.with_name(OUT.stem + "_new.pptx")
        prs.save(target)
        print(f"[warn] {OUT.name} is locked (open in PowerPoint?). Saved to {target.name} instead.")
    print(f"[ok] saved {target}  (slides: {len(prs.slides)})")


if __name__ == "__main__":
    main()
