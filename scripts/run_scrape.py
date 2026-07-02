"""
Entrypoint for the Render Cron Job. Runs once per invocation:

  1. Loads the known city_urls from the DB. If empty (first run) or if
     --rediscover is passed, re-crawls goodreturns.in to (re)build the list.
  2. Scrapes the current price for every city.
  3. Upserts results into fuel_prices.

Usage:
    python -m scripts.run_scrape                # normal daily run
    python -m scripts.run_scrape --rediscover    # also refresh the city list
"""

import sys
import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal, engine, Base
from app.models import CityUrl, FuelPrice
from app.discover import discover_all_cities
from app.scraper import scrape_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_scrape")

FUEL = "petrol"


def load_or_discover_cities(db, rediscover: bool = False):
    count = db.query(CityUrl).filter(CityUrl.fuel == FUEL).count()

    if rediscover or count == 0:
        logger.info("Discovering city URLs from goodreturns.in (fuel=%s) ...", FUEL)
        cities = discover_all_cities(FUEL)
        logger.info("Discovered %d cities. Upserting into city_urls ...", len(cities))

        for c in cities:
            stmt = pg_insert(CityUrl).values(
                slug=c["slug"], fuel=c["fuel"], city_name=c["city_name"],
                state=c["state"], url=c["url"], discovered_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["slug", "fuel"],
                set_={"city_name": c["city_name"], "state": c["state"], "url": c["url"]},
            )
            db.execute(stmt)
        db.commit()

    rows = db.query(CityUrl).filter(CityUrl.fuel == FUEL).all()
    return [
        {"slug": r.slug, "fuel": r.fuel, "city_name": r.city_name, "state": r.state, "url": r.url}
        for r in rows
    ]


def save_results(db, results):
    now = datetime.now(timezone.utc)
    for r in results:
        stmt = pg_insert(FuelPrice).values(
            slug=r["slug"], fuel=r["fuel"], city_name=r["city_name"], state=r["state"],
            url=r["url"], price=r["price"], currency="INR", raw_text=r["raw_text"],
            scraped_at=now, status=r["status"],
        ).on_conflict_do_update(
            index_elements=["slug", "fuel"],
            set_={
                "price": r["price"], "raw_text": r["raw_text"],
                "scraped_at": now, "status": r["status"],
                "city_name": r["city_name"], "state": r["state"], "url": r["url"],
            },
        )
        db.execute(stmt)
    db.commit()


def main():
    rediscover = "--rediscover" in sys.argv

    Base.metadata.create_all(bind=engine)  # idempotent
    db = SessionLocal()
    try:
        targets = load_or_discover_cities(db, rediscover=rediscover)
        logger.info("Scraping %d city pages for %s ...", len(targets), FUEL)

        # Conservative pacing: ~1 request every ~2-3.5s across ALL workers,
        # not per-worker. For 350 pages that's roughly 10-15 minutes total —
        # a small price for not getting the IP blocked. See app/scraper.py
        # for the rate limiter + circuit breaker implementation.
        results = scrape_all(
            targets,
            max_workers=3,
            min_interval=2.0,
            jitter=1.5,
            max_consecutive_blocks=5,
        )

        ok = sum(1 for r in results if r["status"] == "ok")
        not_found = sum(1 for r in results if r["status"] == "not_found")
        blocked = sum(1 for r in results if r["status"] == "blocked")
        errors = sum(1 for r in results if r["status"] == "error")

        # Don't let a heavily-blocked run overwrite good existing data with
        # nulls for every page — only upsert rows we actually got signal on
        # (ok / not_found / error are all real attempts; skip pure "blocked"
        # placeholders so the DB keeps yesterday's price for those cities).
        results_to_save = [r for r in results if r["status"] != "blocked" or r["price"] is not None]
        save_results(db, results_to_save)

        summary = {
            "total": len(results), "ok": ok, "not_found": not_found,
            "blocked": blocked, "errors": errors,
        }
        if blocked > 0:
            logger.warning(
                "%d/%d pages were blocked this run and were left as stale data "
                "in the DB rather than overwritten.", blocked, len(results),
            )
        logger.info("Done. %s", summary)
        return summary
    finally:
        db.close()


if __name__ == "__main__":
    main()
