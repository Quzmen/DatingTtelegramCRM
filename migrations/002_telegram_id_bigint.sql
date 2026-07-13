-- Миграция для Supabase/PostgreSQL: contacts.telegram_id INTEGER -> BIGINT.
--
-- Причина: колонка была объявлена как INTEGER (лимит int4 —
-- 2147483647), а реальные Telegram ID пользователей могут быть
-- больше этого значения. При попытке импортировать/сохранить такой
-- контакт вставка падала с ошибкой:
--   sqlalchemy.exc.DataError: NumericValueOutOfRange
--
-- Как и с 001_telegram_settings.sql, выполнять это вручную не
-- обязательно — backend/database.py::run_migrations() делает это
-- сам при старте приложения (см. блок про telegram_id в run_migrations).
-- Этот файл — на случай, если вы предпочитаете применить миграцию
-- явно через SQL-редактор Supabase (например, до деплоя новой версии).
--
-- Изменение типа INTEGER -> BIGINT в PostgreSQL безопасно и не теряет
-- данные (диапазон BIGINT полностью включает диапазон INTEGER).
-- Блок ниже идемпотентен: повторный запуск ничего не сломает.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'contacts'
          AND column_name = 'telegram_id'
          AND data_type = 'integer'
    ) THEN
        ALTER TABLE contacts ALTER COLUMN telegram_id TYPE BIGINT;
    END IF;
END $$;
