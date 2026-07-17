"""
Финальная миграция перехода на многопользовательский режим (п.13 плана).

Отдельный ручной одноразовый скрипт, а НЕ часть database.run_migrations():
run_migrations() выполняется на каждом старте приложения и обязана быть
безопасной даже при нуле пользователей в системе (например сразу после
установки) — SET NOT NULL там невозможен, пока строки с user_id IS NULL
не назначены конкретному владельцу. Этот же скрипт запускается один раз,
вручную, после того как единственный прежний владелец данных (тот, кто
раньше был единственным пользователем однопользовательской версии) вошёл
в CRM хотя бы один раз через Telegram (см. routers/auth.py) — то есть в
таблице users уже есть его запись.

Что делает:
1. Смотрит, сколько пользователей в users.
   - Если 0 — рано, некому назначать старые данные, скрипт останавливается.
   - Если больше 1 — НЕ угадывает, кому принадлежат "ничьи" данные
     (user_id IS NULL): назначить их не тому человеку значило бы раскрыть
     ему чужую переписку. Останавливается и просит указать владельца явно
     через --user-id.
   - Если ровно 1 — это единственно возможный однозначный ответ, использует
     его автоматически (или использует --user-id, если он передан явно).
2. Backfill: UPDATE ... SET user_id = <owner> WHERE user_id IS NULL — по
   всем таблицам из USER_SCOPED_TABLES.
3. Ужесточение схемы: ALTER TABLE ... ALTER COLUMN user_id SET NOT NULL.
   На PostgreSQL — выполняется. На SQLite (обычно только локальная
   разработка) ALTER COLUMN не поддерживается движком в принципе;
   пересборка таблицы ради этого не оправдана для локальной SQLite-базы
   одного разработчика — шаг пропускается с явным предупреждением в выводе.
4. Проверка внешних ключей: для строк, ссылающихся на родителя
   (interactions.contact_id -> contacts, dialogs.contact_id -> contacts,
   dialogs.folder_id -> folders, campaign_logs.campaign_id -> campaigns,
   media_usages.media_id -> media_files, media_files.folder_id ->
   media_folders, ai_memory_items.contact_id -> contacts,
   ai_overview_snapshots.contact_id -> contacts, campaigns.media_id ->
   media_files) сверяет, что user_id строки совпадает с user_id родителя.
   Несовпадения только печатаются в отчёте — автоматически ничего не
   правит, т.к. само по себе это означает повреждённые/чужие данные,
   требующие ручного разбора, а не механического исправления.

Использование:
    python -m backend.migrate_backfill_user_id [--user-id N] [--dry-run]

--dry-run печатает, что было бы сделано, ничего не меняя в БД.
"""
import argparse
import logging
import sys

import sqlalchemy as sa

from .database import engine

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate_backfill_user_id")

# Все таблицы, которыми теперь владеет пользователь (совпадает с
# _USER_SCOPED_TABLES в database.run_migrations). telegram_settings
# сюда намеренно НЕ входит — её user_id остаётся nullable по дизайну
# (см. docstring models.TelegramSettings), эта миграция её не трогает.
USER_SCOPED_TABLES = [
    "contacts", "tags", "interactions", "folders", "dialogs", "messages",
    "campaigns", "campaign_logs", "media_folders", "media_files", "media_usages",
    "ai_memory_items", "ai_patterns", "ai_decisions", "ai_overview_snapshots",
]

# (дочерняя таблица, колонка внешнего ключа, родительская таблица) —
# для сверки user_id дочерней строки с user_id родителя на шаге 4.
FK_CHECKS = [
    ("interactions", "contact_id", "contacts"),
    ("dialogs", "contact_id", "contacts"),
    ("dialogs", "folder_id", "folders"),
    ("campaign_logs", "campaign_id", "campaigns"),
    ("media_usages", "media_id", "media_files"),
    ("media_files", "folder_id", "media_folders"),
    ("ai_memory_items", "contact_id", "contacts"),
    ("ai_overview_snapshots", "contact_id", "contacts"),
    ("campaigns", "media_id", "media_files"),
]


def _table_exists(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def determine_owner(conn, explicit_user_id: int | None) -> int:
    users = conn.execute(sa.text("SELECT id, name, username, phone FROM users ORDER BY id")).fetchall()
    if not users:
        logger.error("В таблице users нет ни одного пользователя — сначала войдите в CRM через Telegram хотя бы раз.")
        sys.exit(1)

    if explicit_user_id is not None:
        if explicit_user_id not in {u.id for u in users}:
            logger.error("Пользователь с id=%s не найден в users.", explicit_user_id)
            sys.exit(1)
        return explicit_user_id

    if len(users) > 1:
        logger.error(
            "В users больше одного пользователя (%d) — невозможно автоматически и "
            "безопасно определить, кому принадлежат старые (user_id IS NULL) данные. "
            "Укажите владельца явно: --user-id N. Список пользователей:", len(users)
        )
        for u in users:
            logger.error("  id=%s  name=%r  username=%r  phone=%r", u.id, u.name, u.username, u.phone)
        sys.exit(1)

    owner = users[0]
    logger.info("Найден единственный пользователь: id=%s name=%r username=%r — старые данные будут его.",
                owner.id, owner.name, owner.username)
    return owner.id


def backfill(conn, inspector, owner_id: int, dry_run: bool) -> None:
    logger.info("--- Шаг 1/3: backfill user_id IS NULL -> %s ---", owner_id)
    for table in USER_SCOPED_TABLES:
        if not _table_exists(inspector, table):
            continue
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")).scalar()
        if not count:
            continue
        logger.info("  %s: %d строк(и) без user_id", table, count)
        if not dry_run:
            conn.execute(sa.text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"), {"uid": owner_id})


def enforce_not_null(conn, inspector, dry_run: bool) -> None:
    logger.info("--- Шаг 2/3: ALTER COLUMN user_id SET NOT NULL ---")
    if engine.dialect.name != "postgresql":
        logger.warning(
            "  Диалект БД — %s: ALTER COLUMN ... SET NOT NULL этим движком не "
            "поддерживается (типично для локальной SQLite-разработки). Данные уже "
            "backfill'ены (шаг 1), поэтому это безопасно пропустить — на "
            "PostgreSQL (боевая БД) ограничение будет применено полностью.",
            engine.dialect.name,
        )
        return
    for table in USER_SCOPED_TABLES:
        if not _table_exists(inspector, table):
            continue
        remaining = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")).scalar()
        if remaining:
            logger.warning("  %s: остались %d строк(и) с user_id IS NULL — NOT NULL не применяется.", table, remaining)
            continue
        logger.info("  %s: SET NOT NULL", table)
        if not dry_run:
            conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL"))


def check_foreign_keys(conn, inspector) -> None:
    logger.info("--- Шаг 3/3: проверка согласованности user_id между связанными таблицами ---")
    any_mismatch = False
    for child_table, fk_column, parent_table in FK_CHECKS:
        if not _table_exists(inspector, child_table) or not _table_exists(inspector, parent_table):
            continue
        rows = conn.execute(sa.text(
            f"""
            SELECT c.id AS child_id, c.user_id AS child_user_id, p.user_id AS parent_user_id
            FROM {child_table} c
            JOIN {parent_table} p ON c.{fk_column} = p.id
            WHERE c.user_id IS DISTINCT FROM p.user_id
            """
            if engine.dialect.name == "postgresql" else
            f"""
            SELECT c.id AS child_id, c.user_id AS child_user_id, p.user_id AS parent_user_id
            FROM {child_table} c
            JOIN {parent_table} p ON c.{fk_column} = p.id
            WHERE (c.user_id IS NULL) != (p.user_id IS NULL) OR c.user_id != p.user_id
            """
        )).fetchall()
        if rows:
            any_mismatch = True
            logger.warning(
                "  НЕСОВПАДЕНИЕ: %s.%s -> %s — %d строк(и), где user_id ребёнка != user_id родителя "
                "(требует ручного разбора, не исправлено автоматически):",
                child_table, fk_column, parent_table, len(rows),
            )
            for r in rows[:20]:
                logger.warning("    %s.id=%s user_id=%s, %s.user_id=%s", child_table, r.child_id, r.child_user_id, parent_table, r.parent_user_id)
            if len(rows) > 20:
                logger.warning("    ... и ещё %d", len(rows) - 20)
    if not any_mismatch:
        logger.info("  Несовпадений не найдено — все проверенные внешние ключи согласованы по user_id.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, default=None, help="Явно указать id владельца старых данных")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не менять в БД, только показать план")
    args = parser.parse_args()

    inspector = sa.inspect(engine)
    if "users" not in inspector.get_table_names():
        logger.error("Таблицы users нет вообще — сначала запустите приложение (create_all/run_migrations).")
        sys.exit(1)

    with engine.begin() as conn:
        owner_id = determine_owner(conn, args.user_id)
        backfill(conn, inspector, owner_id, args.dry_run)
        enforce_not_null(conn, inspector, args.dry_run)
        check_foreign_keys(conn, inspector)

    if args.dry_run:
        logger.info("--dry-run: изменения НЕ применялись.")
    else:
        logger.info("Готово.")


if __name__ == "__main__":
    main()
