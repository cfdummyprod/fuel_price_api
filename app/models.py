from datetime import datetime, timezone

from sqlalchemy import Column, String, Float, DateTime, Text
from app.db import Base


class CityUrl(Base):
    """Discovered target pages to scrape (one row per city, per fuel type)."""
    __tablename__ = "city_urls"

    slug = Column(String, primary_key=True)       # e.g. "chandigarh"
    fuel = Column(String, primary_key=True)        # e.g. "petrol"
    city_name = Column(String, nullable=False)
    state = Column(String, nullable=True)
    url = Column(Text, nullable=False)
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class FuelPrice(Base):
    """Latest scraped price per city/fuel — this is what the API serves."""
    __tablename__ = "fuel_prices"

    slug = Column(String, primary_key=True)
    fuel = Column(String, primary_key=True)
    city_name = Column(String, nullable=False)
    state = Column(String, nullable=True)
    url = Column(Text, nullable=False)
    price = Column(Float, nullable=True)
    currency = Column(String, default="INR")
    raw_text = Column(String, nullable=True)
    scraped_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, default="ok")  # ok | not_found | blocked | error
