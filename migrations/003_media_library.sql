-- Миграция для Supabase/PostgreSQL: модуль встроенной медиатеки.
--
-- Таблицы media_files и media_usages новые, поэтому обычно создаются
-- автоматически через Base.metadata.create_all() при старте
-- приложения — выполнять этот файл вручную не обязательно.
-- Единственное, что create_all не делает сам за вас на уже
-- существующей базе — это долить новую колонку в СУЩЕСТВУЮЩУЮ таблицу
-- campaigns, поэтому backend/database.py::run_migrations() делает это
-- сам при старте (см. блок про campaigns.media_id). Этот файл — на
-- случай, если вы предпочитаете применить миграцию явно через
-- SQL-редактор Supabase. Идемпотентен: повторный запуск ничего не сломает.

CREATE TABLE IF NOT EXISTS media_files (
    id SERIAL PRIMARY KEY,
    original_name VARCHAR(300) NOT NULL,
    stored_name VARCHAR(300) NOT NULL UNIQUE,
    kind VARCHAR(20) NOT NULL DEFAULT 'document',
    mime VARCHAR(120),
    size_bytes INTEGER NOT NULL DEFAULT 0,
    width INTEGER,
    height INTEGER,
    has_thumb BOOLEAN NOT NULL DEFAULT FALSE,
    send_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_media_files_kind ON media_files (kind);
CREATE INDEX IF NOT EXISTS ix_media_files_created_at ON media_files (created_at);

CREATE TABLE IF NOT EXISTS media_usages (
    id SERIAL PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    sent_at TIMESTAMP NOT NULL DEFAULT now(),
    context VARCHAR(20) NOT NULL DEFAULT 'chat'
);
CREATE INDEX IF NOT EXISTS ix_media_usages_media_id ON media_usages (media_id);
CREATE INDEX IF NOT EXISTS ix_media_usages_telegram_id ON media_usages (telegram_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'campaigns' AND column_name = 'media_id'
    ) THEN
        ALTER TABLE campaigns ADD COLUMN media_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL;
    END IF;
END $$;
