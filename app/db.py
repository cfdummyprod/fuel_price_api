"""
Database engine + session setup.

DATABASE_URL must be a standard SQLAlchemy Postgres URL, e.g.:
  postgresql+psycopg2://user:password@host/dbname?sslmode=require

Works with Neon, Supabase, Render Postgres, or any managed Postgres.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Set it to your Postgres connection string (e.g. from Neon)."
    )

# Render/Neon connection strings sometimes start with postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
