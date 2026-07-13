-- Миграция для Supabase/PostgreSQL: сохранение реальных Telegram-данных
-- по каждой отправке медиафайла из встроенной медиатеки (раздел
-- СОХРАНЕНИЕ TELEGRAM ДАННЫХ ТЗ).
--
-- Таблица media_usages уже существует (см. 003_media_library.sql) —
-- этот файл только доливает три новых поля. Как и остальные миграции
-- в этом проекте, backend/database.py::run_migrations() делает то же
-- самое сам при старте приложения, поэтому применять этот файл
-- вручную нужно только если вы предпочитаете SQL-редактор Supabase.
-- Идемпотентен: повторный запуск ничего не сломает.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'media_usages' AND column_name = 'telegram_message_id'
    ) THEN
        ALTER TABLE media_usages ADD COLUMN telegram_message_id BIGINT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'media_usages' AND column_name = 'telegram_file_id'
    ) THEN
        ALTER TABLE media_usages ADD COLUMN telegram_file_id VARCHAR(500);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'media_usages' AND column_name = 'sent_kind'
    ) THEN
        ALTER TABLE media_usages ADD COLUMN sent_kind VARCHAR(20);
    END IF;
END $$;
