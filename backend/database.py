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

    # telegram_id раньше был INTEGER (лимит int4 — 2147483647), а
    # реальные Telegram ID могут быть больше. SQLite это не касается
    # (там нет строгого лимита на INTEGER), поэтому расширяем только
    # PostgreSQL, и только если колонка ещё не BIGINT — иначе ALTER
    # выполнялся бы при каждом старте приложения.
    if engine.dialect.name == "postgresql":
        telegram_id_column = next(
            (c for c in inspector.get_columns("contacts") if c["name"] == "telegram_id"),
            None,
        )
        if telegram_id_column is not None and str(telegram_id_column["type"]).upper() == "INTEGER":
            with engine.begin() as conn:
                conn.execute(sa.text("ALTER TABLE contacts ALTER COLUMN telegram_id TYPE BIGINT"))

    # Папки (сегменты) диалогов — folders создаётся автоматически через
    # create_all (новая таблица), а вот dialogs.folder_id нужно долить
    # существующим базам вручную, как и остальные колонки выше.
    if "dialogs" in inspector.get_table_names():
        dialog_columns = {c["name"] for c in inspector.get_columns("dialogs")}
        if "folder_id" not in dialog_columns:
            with engine.begin() as conn:
                conn.execute(sa.text("ALTER TABLE dialogs ADD COLUMN folder_id INTEGER"))

    # Медиатека (см. models.MediaFile/MediaUsage) — сами таблицы новые
    # и создаются через create_all, а вот campaigns.media_id нужно
    # долить существующим базам, как и остальные колонки выше.
    if "campaigns" in inspector.get_table_names():
        campaign_columns = {c["name"] for c in inspector.get_columns("campaigns")}
        if "media_id" not in campaign_columns:
            with engine.begin() as conn:
                conn.execute(sa.text("ALTER TABLE campaigns ADD COLUMN media_id INTEGER"))

    # Папки медиатеки (media_folders создаётся автоматически через
    # create_all, как новая таблица) — а media_files.folder_id нужно
    # долить существующим базам, как и dialogs.folder_id выше.
    # История использования медиатеки (media_usages создаётся
    # автоматически через create_all, как новая таблица) — а три поля
    # про реальные Telegram-данные отправки (message_id/file_id/kind,
    # см. models.MediaUsage) нужно долить существующим базам, как и
    # остальные колонки выше.
    if "media_usages" in inspector.get_table_names():
        usage_columns = {c["name"] for c in inspector.get_columns("media_usages")}
        usage_new_columns = {
            "telegram_message_id": "BIGINT",
            "telegram_file_id": "VARCHAR(500)",
            "sent_kind": "VARCHAR(20)",
        }
        with engine.begin() as conn:
            for column_name, ddl_type in usage_new_columns.items():
                if column_name not in usage_columns:
                    conn.execute(sa.text(f"ALTER TABLE media_usages ADD COLUMN {column_name} {ddl_type}"))

    if "media_files" in inspector.get_table_names():
        media_columns = {c["name"] for c in inspector.get_columns("media_files")}
        if "folder_id" not in media_columns:
            with engine.begin() as conn:
                conn.execute(sa.text("ALTER TABLE media_files ADD COLUMN folder_id INTEGER"))