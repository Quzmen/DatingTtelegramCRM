"""
Database configuration.

Supports:
- SQLite locally (/data/crm.db)
- PostgreSQL in production via DATABASE_URL environment variable
"""

import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


# PostgreSQL from Render/Supabase
# If missing -> fallback to SQLite
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{DATA_DIR / 'crm.db'}"


# Render can provide postgres://
# SQLAlchemy 2.x requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )


if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={
            "check_same_thread": False
        }
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300
    )


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    """
    FastAPI dependency.
    Creates DB session and closes it after request.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """
    Lightweight migration for adding new Contact columns.

    Works with existing SQLite database.
    For PostgreSQL normally columns are created by
    Base.metadata.create_all().
    """

    inspector = sa.inspect(engine)

    if "contacts" not in inspector.get_table_names():
        return

    existing_columns = {
        c["name"]
        for c in inspector.get_columns("contacts")
    }

    new_columns = {
        "interest_score": "INTEGER DEFAULT 0",
        "interest_category": "VARCHAR(50)",
        "suggested_status": "VARCHAR(30)",
        "next_action": "VARCHAR(300)",
        "ai_summary": "TEXT",
        "suggested_reply": "TEXT",
        "ai_source": "VARCHAR(20)",
        "trend_direction": "VARCHAR(10)",
        "trend_label": "VARCHAR(64)",
        "trend_delta": "FLOAT",
        "analyzed_at": "DATETIME",
        "deep_report_json": "TEXT",
        "deep_report_at": "DATETIME",
    }

    with engine.begin() as conn:
        for column_name, ddl_type in new_columns.items():
            if column_name not in existing_columns:
                conn.execute(
                    sa.text(
                        f"ALTER TABLE contacts ADD COLUMN {column_name} {ddl_type}"
                    )
                )