"""
Scrapes the current price from each discovered city page using the
verified XPath: //span[@id="fp-price"]

Hardened against anti-bot / rate-limit walls (Cloudflare etc.):
  - A shared rate limiter enforces a minimum gap between *any* two
    requests, regardless of how many worker threads are running. Plain
    per-thread time.sleep() doesn't do this — N threads sleeping
    independently still fire N requests close together.
  - Small pool of realistic User-Agents, rotated per request.
  - Detects challenge/block pages (Cloudflare "Just a moment", 403/429/503,
    captcha markers) and treats them as "blocked", not "not_found" — a
    missing price node and a block are different problems.
  - Circuit breaker: if too many requests in a row come back blocked, the
    run stops early instead of hammering a wall for the remaining pages.
    Better to get partial fresh data + old cached data for the rest than
    to get the IP banned for tomorrow's run too.
"""

import re
import time
import random
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

import requests
from lxml import html
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("scraper")

XPATH = '//span[@id="fp-price"]'
PRICE_RE = re.compile(r"[\d,]+\.?\d*")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BLOCK_MARKERS = (
    "just a moment", "attention required", "cf-browser-verification",
    "cf-chl", "captcha", "access denied", "request blocked",
    "unusual traffic", "are you a robot",
)


class RateLimiter:
    """
    Enforces a minimum gap between *any* two outgoing requests across all
    threads. This is what actually keeps aggregate request rate low —
    per-thread sleeps alone don't, since threads run concurrently.
    """

    def __init__(self, min_interval: float, jitter: float = 0.5):
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            target = self._last + self.min_interval + random.uniform(0, self.jitter)
            sleep_for = target - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


class BlockedError(Exception):
    """Raised when a response looks like a bot-block / challenge page."""


class CircuitOpen(Exception):
    """Raised to abort the whole run when too many blocks happen in a row."""


class CircuitBreaker:
    def __init__(self, max_consecutive_blocks: int = 5):
        self.max_consecutive_blocks = max_consecutive_blocks
        self._consecutive = 0
        self._lock = threading.Lock()
        self.tripped = False

    def record(self, blocked: bool):
        with self._lock:
            if blocked:
                self._consecutive += 1
                if self._consecutive >= self.max_consecutive_blocks:
                    self.tripped = True
            else:
                self._consecutive = 0

    def check(self):
        if self.tripped:
            raise CircuitOpen(
                f"{self.max_consecutive_blocks} consecutive blocked responses — "
                "stopping this run to avoid getting the IP banned."
            )


def _looks_blocked(status_code: int, text_sample: str) -> bool:
    if status_code in (403, 429, 503):
        return True
    lowered = text_sample[:2000].lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def _fetch(session: requests.Session, url: str, timeout: int = 15):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.goodreturns.in/",
    }
    resp = session.get(url, headers=headers, timeout=timeout)

    if _looks_blocked(resp.status_code, resp.text if resp.status_code != 200 else ""):
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30))
            except ValueError:
                pass
        raise BlockedError(f"Blocked response ({resp.status_code}) for {url}")

    resp.raise_for_status()
    return resp.content


def scrape_one(session: requests.Session, target: Dict, limiter: RateLimiter, breaker: CircuitBreaker) -> Dict:
    """target: {slug, fuel, city_name, state, url}. Returns a result dict."""
    breaker.check()
    result = {**target, "price": None, "raw_text": None, "status": "error"}

    limiter.wait()
    try:
        content = _fetch(session, target["url"])

        # Double-check the successful body too, in case a 200 was still a challenge page
        if _looks_blocked(200, content.decode("utf-8", errors="ignore")):
            raise BlockedError(f"Challenge page returned with 200 for {target['url']}")

        breaker.record(blocked=False)

        tree = html.fromstring(content)
        nodes = tree.xpath(XPATH)
        if not nodes:
            result["status"] = "not_found"
            return result

        raw_text = nodes[0].text_content().strip()
        result["raw_text"] = raw_text

        match = PRICE_RE.search(raw_text.replace(",", ""))
        if match:
            result["price"] = float(match.group())
            result["status"] = "ok"
        else:
            result["status"] = "not_found"

    except BlockedError as e:
        logger.warning(str(e))
        result["status"] = "blocked"
        breaker.record(blocked=True)
    except Exception as e:
        logger.warning("Failed scraping %s: %s", target["url"], e)
        result["status"] = "error"

    return result


def scrape_all(
    targets: List[Dict],
    max_workers: int = 3,
    min_interval: float = 2.0,
    jitter: float = 1.5,
    max_consecutive_blocks: int = 5,
    shuffle: bool = True,
) -> List[Dict]:
    """
    Scrapes all targets with:
      - max_workers concurrent threads (keep this low — 2-4)
      - a shared rate limiter enforcing >= min_interval seconds between
        ANY two requests (so total throughput stays ~1 req per
        min_interval seconds no matter how many workers you use)
      - a circuit breaker that aborts the run if the site starts
        returning block/challenge pages repeatedly

    At min_interval=2.0s, ~350 pages takes ~12 minutes — comfortably
    inside a daily cron window, and much less likely to trip Cloudflare
    or basic rate limiting than a fast concurrent burst.
    """
    results = []
    session = requests.Session()
    limiter = RateLimiter(min_interval=min_interval, jitter=jitter)
    breaker = CircuitBreaker(max_consecutive_blocks=max_consecutive_blocks)

    ordered = list(targets)
    if shuffle:
        random.shuffle(ordered)  # avoid a predictable alphabetical crawl pattern

    aborted = False
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scrape_one, session, t, limiter, breaker): t for t in ordered}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except CircuitOpen as e:
                if not aborted:
                    logger.error("Circuit breaker tripped: %s", e)
                    aborted = True
                results.append({**t, "price": None, "raw_text": None, "status": "blocked"})
            if i % 25 == 0 or i == len(ordered):
                logger.info("Scraped %d/%d (aborted=%s)", i, len(ordered), aborted)

    if aborted:
        logger.warning(
            "Run stopped early due to repeated blocks. %d/%d pages ended up marked "
            "blocked. Existing DB rows for those pages are left untouched (stale, "
            "not wiped) — next run will retry them.",
            sum(1 for r in results if r["status"] == "blocked"),
            len(ordered),
        )

    return results
