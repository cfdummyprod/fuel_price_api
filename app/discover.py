"""
Discovers all city-level fuel-price page URLs on goodreturns.in by crawling
the state index pages.

Flow:
  1. Fetch https://www.goodreturns.in/{fuel}-price.html
     -> parse the "State-Wise Petrol Price" table -> get ~35 state URLs
        (pattern: {fuel}-price-in-{state-slug}-s{N}.html)
  2. For each state URL, fetch it and parse the
     "List of Petrol Price in {State}" table -> get city URLs
        (pattern: {fuel}-price-in-{city-slug}.html)

Returns a de-duplicated list of dicts: {slug, city_name, state, url, fuel}
"""

import re
import time
import random
import logging
from typing import List, Dict

import requests
from lxml import html

logger = logging.getLogger("discover")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BASE = "https://www.goodreturns.in"

# XPath for the state-wise table links on the index page, and the
# city-wise table links on each state page. Both are plain <a> tags
# whose href matches the fuel's URL pattern, so one XPath covers both.
LINKS_XPATH = '//a[contains(@href, "-price-in-")]'

STATE_URL_RE = re.compile(r"-price-in-[a-z0-9-]+-s\d+\.html$")


def _get(session: requests.Session, url: str, timeout: int = 15) -> html.HtmlElement:
    resp = session.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return html.fromstring(resp.content)


def discover_state_urls(session: requests.Session, fuel: str) -> List[str]:
    index_url = f"{BASE}/{fuel}-price.html"
    tree = _get(session, index_url)

    urls = set()
    for a in tree.xpath(LINKS_XPATH):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = BASE + href
        if STATE_URL_RE.search(href):
            urls.add(href)
    return sorted(urls)


def discover_cities_for_state(session: requests.Session, state_url: str, fuel: str) -> List[Dict]:
    tree = _get(session, state_url)

    # State name from the h1 / breadcrumb, fall back to slug parsing
    state_name = None
    h1 = tree.xpath("//h1/text()")
    if h1:
        state_name = h1[0].replace(f"Petrol Price in ", "").strip()

    cities = []
    seen = set()
    for a in tree.xpath(LINKS_XPATH):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = BASE + href
        # Skip state-level links themselves
        if STATE_URL_RE.search(href):
            continue
        if f"/{fuel}-price-in-" not in href:
            continue
        slug_match = re.search(rf"{fuel}-price-in-([a-z0-9-]+)\.html", href)
        if not slug_match:
            continue
        slug = slug_match.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        city_name = (a.text_content() or slug.replace("-", " ").title()).strip()
        cities.append({
            "slug": slug,
            "fuel": fuel,
            "city_name": city_name,
            "state": state_name,
            "url": href,
        })
    return cities


def discover_all_cities(fuel: str = "petrol", delay_range=(0.8, 1.6)) -> List[Dict]:
    """Full crawl: state index -> all state pages -> all city URLs, deduped by slug."""
    session = requests.Session()
    all_cities: Dict[str, Dict] = {}

    state_urls = discover_state_urls(session, fuel)
    logger.info("Discovered %d state pages for %s", len(state_urls), fuel)

    for i, state_url in enumerate(state_urls, 1):
        try:
            cities = discover_cities_for_state(session, state_url, fuel)
            for c in cities:
                all_cities[c["slug"]] = c  # dedupe (e.g. Chandigarh is both a state and a city)
            logger.info("[%d/%d] %s -> %d cities", i, len(state_urls), state_url, len(cities))
        except Exception as e:
            logger.warning("Failed to discover cities for %s: %s", state_url, e)
        time.sleep(random.uniform(*delay_range))

    return list(all_cities.values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = discover_all_cities("petrol")
    print(f"Total unique city pages discovered: {len(result)}")
    for r in result[:10]:
        print(r)
