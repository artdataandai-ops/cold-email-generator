"""
Standalone Apify smoke test — runs the LinkedIn actor in isolation.

No cache, no LLM, no FastAPI, no Playwright. Just .env -> apify-client -> dataset.

USAGE
    python check_apify.py <linkedin-url> [post_limit]

EXAMPLES
    python check_apify.py https://www.linkedin.com/in/satyanadella
    python check_apify.py https://uk.linkedin.com/in/andrew-mouat 3

WHAT IT TELLS YOU
    • Token + actor ID configured?
    • Username correctly extracted from URL?
    • Does the actor.call() succeed?
    • How many items did the dataset return?
    • What field names do those items actually contain? (so we can adjust the
      coercer in src/linkedin.py if needed)
    • First 1-2 items pretty-printed for visual inspection.

INTERPRETING RESULTS
    Returns 0 items   -> Apify side. Profile inactive, LinkedIn blocked the
                         run, or actor input shape is wrong for this version.
    Returns N items   -> Apify is healthy. If the app still shows 0 LinkedIn
                         posts, the bug is in our _coerce_apify_post / cache
                         / date-filter logic.
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_LINKEDIN_ACTOR = os.getenv("APIFY_LINKEDIN_ACTOR", "LQQIXN9Othf8f7R5n")


def extract_username(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "in":
        return parts[1]
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    url = sys.argv[1].strip()
    limit = int(sys.argv[2]) if len(sys.argv) >= 3 else 2

    print("=" * 72)
    print("APIFY LINKEDIN SMOKE TEST")
    print("=" * 72)
    print(f"APIFY_TOKEN configured : {'yes' if APIFY_TOKEN else 'NO — fill APIFY_TOKEN in .env'}")
    print(f"APIFY_LINKEDIN_ACTOR   : {APIFY_LINKEDIN_ACTOR}")
    print(f"LinkedIn URL           : {url}")

    if not APIFY_TOKEN:
        return 1

    username = extract_username(url)
    print(f"Extracted username     : {username}")
    if not username:
        print("\n⚠️  Could not extract username from URL. Expected /in/<slug>.")
        return 1

    try:
        from apify_client import ApifyClient
    except ImportError:
        print("\n❌ apify-client not installed. Run: pip install apify-client")
        return 1

    client = ApifyClient(APIFY_TOKEN)
    actor = client.actor(APIFY_LINKEDIN_ACTOR)

    run_input = {
        "username": username,
        "profileUrls": [url],
        "page_number": 1,
        "limit": limit,
    }
    print("\nInput to actor.call():")
    print(json.dumps(run_input, indent=2))

    print("\nCalling actor (typically 30-120s)...")
    try:
        run = actor.call(run_input=run_input)
    except Exception as e:
        print(f"\n❌ actor.call() raised: {type(e).__name__}: {e}")
        return 1

    print(f"\nRun status   : {run.get('status')}")
    print(f"Dataset ID   : {run.get('defaultDatasetId')}")
    print(f"Build ID     : {run.get('buildId')}")
    msg = run.get("statusMessage")
    if msg:
        print(f"Status msg   : {msg}")

    if run.get("status") != "SUCCEEDED":
        print("\n⚠️  Run did not succeed. Check the Apify console for the run details.")
        print(f"   https://console.apify.com/actors/{APIFY_LINKEDIN_ACTOR}/runs/{run.get('id')}")
        return 1

    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"\nDataset items returned: {len(items)}")

    if not items:
        print("\n⚠️  Dataset is empty. Likely causes:")
        print("   • LinkedIn served an empty / blocked page to the actor this run")
        print("   • Profile has no public posts to scrape")
        print("   • Actor's input contract changed; our run_input shape may be off")
        print(f"\n   Look at the run in the Apify console for the actor's own logs:")
        print(f"   https://console.apify.com/actors/{APIFY_LINKEDIN_ACTOR}/runs/{run.get('id')}")
        return 0

    print("\nField names present in first item:")
    if isinstance(items[0], dict):
        for k in items[0].keys():
            v = items[0][k]
            sample = json.dumps(v, default=str)
            preview = sample[:100] + ("..." if len(sample) > 100 else "")
            print(f"   {k:28s} {preview}")

    print(f"\nFirst {min(2, len(items))} item(s):")
    for i, item in enumerate(items[:2]):
        print(f"\n--- Item {i + 1} ---")
        s = json.dumps(item, indent=2, default=str)
        print(s[:2000] + ("..." if len(s) > 2000 else ""))

    print("\n" + "=" * 72)
    print("NEXT STEP")
    print("=" * 72)
    print("Compare the field names above with what src/linkedin.py:_coerce_apify_post")
    print("looks for:  text/postText/content, postedAt/publishedAt/date, url/postUrl,")
    print("            type/postType, repostedPost, commentary, etc.")
    print("If the actor uses different names, that's why posts get dropped in our app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
