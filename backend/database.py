"""
Database configuration.

SQLite database file lives in /data/crm.db, next to the project root,
so it's easy to find, back up, or delete without touching any code.
"""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'crm.db'}"

# check_same_thread=False is required because FastAPI can use the
# connection from different threads within the same request lifecycle.
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """Лёгкая авто-миграция для SQLite.

    ``Base.metadata.create_all`` создаёт только отсутствующие таблицы
    целиком и не умеет добавлять новые колонки в уже существующую
    таблицу. Если пользователь обновляет CRM с более ранней версии
    (когда AI-полей у Contact ещё не было), файл data/crm.db уже
    существует, и без этой миграции новые колонки просто не появятся.

    SQLite поддерживает ``ALTER TABLE ... ADD COLUMN`` — этого
    достаточно, чтобы аккуратно докатить новые поля без пересоздания БД.
    """
    import sqlalchemy as sa

    inspector = sa.inspect(engine)
    if "contacts" not in inspector.get_table_names():
        return

    existing_columns = {c["name"] for c in inspector.get_columns("contacts")}
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
                conn.execute(sa.text(f"ALTER TABLE contacts ADD COLUMN {column_name} {ddl_type}"))
