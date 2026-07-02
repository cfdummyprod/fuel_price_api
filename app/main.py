"""
FastAPI service that serves cached fuel prices from Postgres.
This process never scrapes on-request — it only reads what the
scheduled cron job (scripts/run_scrape.py) already wrote to the DB.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db import get_db, engine, Base
from app.models import FuelPrice

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

app = FastAPI(
    title="India Fuel Price API",
    description="Daily-refreshed petrol prices for ~350 Indian cities, scraped from goodreturns.in",
    version="1.0.0",
)

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")


@app.on_event("startup")
def on_startup():
    # Creates tables if they don't exist yet (safe / idempotent)
    Base.metadata.create_all(bind=engine)


class PriceOut(BaseModel):
    slug: str
    fuel: str
    city_name: str
    state: Optional[str] = None
    price: Optional[float] = None
    currency: str
    raw_text: Optional[str] = None
    scraped_at: datetime
    status: str

    class Config:
        from_attributes = True


class MetaOut(BaseModel):
    total_cities: int
    last_scraped_at: Optional[datetime]
    stale: bool


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "India Fuel Price API",
        "endpoints": ["/health", "/meta", "/prices", "/prices/{slug}", "/states"],
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/meta", response_model=MetaOut, tags=["meta"])
def meta(db: Session = Depends(get_db)):
    total = db.query(func.count(FuelPrice.slug)).scalar() or 0
    last = db.query(func.max(FuelPrice.scraped_at)).scalar()
    stale = True
    if last:
        age_hours = (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        stale = age_hours > 30  # cron runs every 24h; flag if it's been > 30h
    return MetaOut(total_cities=total, last_scraped_at=last, stale=stale)


@app.get("/prices", response_model=List[PriceOut], tags=["prices"])
def list_prices(
    fuel: str = "petrol",
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(FuelPrice).filter(FuelPrice.fuel == fuel)
    if state:
        q = q.filter(func.lower(FuelPrice.state) == state.lower())
    rows = q.order_by(FuelPrice.city_name).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No data yet — has the scraper run?")
    return rows


@app.get("/prices/{slug}", response_model=PriceOut, tags=["prices"])
def get_price(slug: str, fuel: str = "petrol", db: Session = Depends(get_db)):
    row = db.query(FuelPrice).filter(FuelPrice.slug == slug, FuelPrice.fuel == fuel).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No data for '{slug}' ({fuel})")
    return row


@app.get("/states", tags=["prices"])
def list_states(fuel: str = "petrol", db: Session = Depends(get_db)):
    rows = (
        db.query(FuelPrice.state)
        .filter(FuelPrice.fuel == fuel, FuelPrice.state.isnot(None))
        .distinct()
        .order_by(FuelPrice.state)
        .all()
    )
    return [r[0] for r in rows]


@app.post("/admin/refresh", tags=["admin"])
def trigger_refresh(x_api_key: str = Header(default="")):
    """
    Optional manual trigger, protected by ADMIN_API_KEY env var.
    Runs the scrape synchronously — with the polite rate limiting in
    app/scraper.py this takes ~10-15 minutes for ~350 pages, so this
    request will hang open that whole time. Fine for occasional manual
    use; the scheduled cron job is still the primary path.
    """
    if not ADMIN_API_KEY or x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key")

    from scripts.run_scrape import main as run_scrape_main
    summary = run_scrape_main()
    return {"status": "completed", "summary": summary}
