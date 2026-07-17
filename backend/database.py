"""
Database configuration.

Supports:
- SQLite locally (/data/crm.db)
- PostgreSQL in production via DATABASE_URL environment variable
"""

import logging
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("telegram-crm")


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

    # Переход на многопользовательский режим (см. models.User/UserSession) —
    # в норме users/user_sessions создаются автоматически через create_all,
    # как новые таблицы. Но create_all() создаёт ТОЛЬКО отсутствующие
    # таблицы целиком и никогда не добавляет колонки в уже существующую
    # таблицу — а на некоторых базах таблица users уже существует (осталась
    # от более ранней версии/эксперимента), просто без нужных колонок.
    # Раньше это приводило к UndefinedColumn при первом же входе через
    # Telegram ("column users.telegram_id does not exist"). Долив тот же
    # защитный ALTER TABLE ADD COLUMN, что и для остальных таблиц выше.
    # unique=True из models.User здесь намеренно не навязываем: если в
    # таблице уже есть строки, наложить UNIQUE/NOT NULL можно только после
    # того как в них появятся корректные значения, а это не задача
    # автоматической миграции схемы при каждом старте (см. также
    # migrate_backfill_user_id.py — тот же принцип для user_id).
    if "users" in inspector.get_table_names():
        users_columns = {c["name"] for c in inspector.get_columns("users")}
        users_new_columns = {
            "telegram_id": "BIGINT",
            "name": "VARCHAR(150)",
            "username": "VARCHAR(100)",
            "phone": "VARCHAR(30)",
            "created_at": "TIMESTAMP",
            "last_login_at": "TIMESTAMP",
        }
        with engine.begin() as conn:
            for column_name, ddl_type in users_new_columns.items():
                if column_name not in users_columns:
                    conn.execute(sa.text(f"ALTER TABLE users ADD COLUMN {column_name} {ddl_type}"))

    if "user_sessions" in inspector.get_table_names():
        session_columns = {c["name"] for c in inspector.get_columns("user_sessions")}
        session_new_columns = {
            "token": "VARCHAR(64)",
            "user_id": "INTEGER",
            "created_at": "TIMESTAMP",
        }
        with engine.begin() as conn:
            for column_name, ddl_type in session_new_columns.items():
                if column_name not in session_columns:
                    conn.execute(sa.text(f"ALTER TABLE user_sessions ADD COLUMN {column_name} {ddl_type}"))

    # telegram_settings.user_id нужно долить
    # существующим базам, как и остальные колонки выше. Оставляем
    # nullable=True — старая "ничья" строка (user_id IS NULL) означает
    # сессию ещё не перенесённого единственного аккаунта из
    # однопользовательского режима, см. models.TelegramSettings.
    if "telegram_settings" in inspector.get_table_names():
        ts_columns = {c["name"] for c in inspector.get_columns("telegram_settings")}
        if "user_id" not in ts_columns:
            with engine.begin() as conn:
                conn.execute(sa.text("ALTER TABLE telegram_settings ADD COLUMN user_id INTEGER"))

    # Многопользовательский режим (продолжение) — user_id на всех
    # таблицах, которыми владеет пользователь. NULLABLE специально:
    # существующие строки на старых базах "ничьи" (принадлежат ещё не
    # перенесённому единственному аккаунту из однопользовательского
    # режима) — их постепенно проставит явный скрипт переноса данных,
    # а не эта миграция схемы (она только добавляет колонку).
    _USER_SCOPED_TABLES = [
        "contacts", "tags", "interactions", "folders", "dialogs", "messages",
        "campaigns", "campaign_logs", "media_folders", "media_files", "media_usages",
        "ai_memory_items", "ai_patterns", "ai_decisions", "ai_overview_snapshots",
    ]
    for table_name in _USER_SCOPED_TABLES:
        if table_name not in inspector.get_table_names():
            continue
        columns = {c["name"] for c in inspector.get_columns(table_name)}
        if "user_id" not in columns:
            with engine.begin() as conn:
                conn.execute(sa.text(f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER"))

    # Старые unique-ограничения на contacts.telegram_id / dialogs.telegram_id /
    # tags.name / messages(dialog_telegram_id, message_id) были рассчитаны на
    # одного пользователя на всё приложение — на многопользовательских
    # данных они не дают двум разным пользователям иметь контакт/диалог
    # с одним и тем же собеседником Telegram (обычный случай, а не
    # крайний). models.py уже объявляет новые составные ограничения
    # (uq_contact_user_telegram_id и т.д.) — они применятся сами через
    # create_all только на СОВСЕМ новой базе; на существующей базе
    # старое ограничение остаётся в БД и его нужно снять явно, иначе оно
    # продолжит бросать IntegrityError. Определяем старое имя constraint
    # через inspector, а не хардкодим — оно могло называться по-разному
    # в зависимости от того, кто и когда создавал таблицу.
    def _drop_old_unique(table_name: str, columns: tuple[str, ...]):
        if table_name not in inspector.get_table_names():
            return
        for uc in inspector.get_unique_constraints(table_name):
            if tuple(uc.get("column_names") or ()) == columns and uc.get("name"):
                with engine.begin() as conn:
                    conn.execute(sa.text(f'ALTER TABLE {table_name} DROP CONSTRAINT "{uc["name"]}"'))
        # Postgres иногда реализует column-level UNIQUE как индекс, а не
        # именованный constraint, попробовать через тот же inspector:
        for ix in inspector.get_indexes(table_name):
            if ix.get("unique") and tuple(ix.get("column_names") or ()) == columns and ix.get("name"):
                with engine.begin() as conn:
                    conn.execute(sa.text(f'DROP INDEX IF EXISTS "{ix["name"]}"'))

    try:
        _drop_old_unique("contacts", ("telegram_id",))
        _drop_old_unique("dialogs", ("telegram_id",))
        _drop_old_unique("tags", ("name",))
        _drop_old_unique("messages", ("dialog_telegram_id", "message_id"))
    except Exception:
        logger.exception(
            "Не удалось автоматически снять старые unique-ограничения "
            "(contacts.telegram_id / dialogs.telegram_id / tags.name / "
            "messages) — если на базе уже есть данные от нескольких "
            "пользователей, потребуется снять их вручную в БД."
        )