"""
Fetches and respects goodreturns.in's robots.txt at runtime, so the
scraper doesn't need to hardcode assumptions about crawl-delay or
disallowed paths.

Usage:
    from app.robots import RobotsGate
    gate = RobotsGate(base_url="https://www.goodreturns.in", user_agent="FuelPriceBot/1.0")
    if gate.can_fetch(url):
        gate.wait()   # sleeps for crawl-delay (or MIN_DELAY, whichever is larger)
        ... fetch url ...
"""

import time
import logging
import threading
from urllib import robotparser
from urllib.parse import urljoin

logger = logging.getLogger("robots")

# Even if robots.txt specifies no crawl-delay at all, never go faster than this.
# This is the "proper gap" floor — one request every 3-5 seconds, not a burst.
MIN_DELAY_SECONDS = 3.0
MAX_DELAY_SECONDS = 5.0


class RobotsGate:
    def __init__(self, base_url: str, user_agent: str = "FuelPriceBot/1.0 (+contact: set-your-email)"):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._parser = robotparser.RobotFileParser()
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._effective_delay = MIN_DELAY_SECONDS
        self._load()

    def _load(self):
        robots_url = urljoin(self.base_url + "/", "robots.txt")
        self._parser.set_url(robots_url)
        try:
            self._parser.read()
            site_delay = self._parser.crawl_delay(self.user_agent)
            if site_delay:
                # Respect the site's requested delay if it's stricter (larger)
                # than our own floor; never go below our own floor either.
                self._effective_delay = max(float(site_delay), MIN_DELAY_SECONDS)
                logger.info("robots.txt crawl-delay=%s -> using %.1fs between requests",
                            site_delay, self._effective_delay)
            else:
                self._effective_delay = MIN_DELAY_SECONDS
                logger.info("No crawl-delay in robots.txt -> defaulting to %.1fs between requests",
                            self._effective_delay)
        except Exception as e:
            # If robots.txt can't be fetched/parsed, fail safe: allow nothing
            # risky, just use our conservative default delay and permissive
            # matching (most sites without a readable robots.txt allow all).
            logger.warning("Could not read robots.txt (%s) — using conservative default delay only", e)
            self._effective_delay = MIN_DELAY_SECONDS

    def can_fetch(self, url: str) -> bool:
        try:
            return self._parser.can_fetch(self.user_agent, url)
        except Exception:
            # If robots.txt was unreadable, don't block on a broken parser
            return True

    def wait(self):
        """Blocks until it's polite to make the next request."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_at
            remaining = self._effective_delay - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_at = time.time()

    @property
    def delay_seconds(self) -> float:
        return self._effective_delay
