-- Миграция для Supabase/PostgreSQL: папки внутри встроенной медиатеки
-- (раздел СТРУКТУРА МЕДИАТЕКИ ТЗ).
--
-- Таблица media_folders новая, поэтому обычно создаётся автоматически
-- через Base.metadata.create_all() при старте приложения — выполнять
-- этот файл вручную не обязательно. Единственное, что create_all не
-- делает сам за вас на уже существующей базе — это долить новую
-- колонку в СУЩЕСТВУЮЩУЮ таблицу media_files, поэтому
-- backend/database.py::run_migrations() делает это сам при старте
-- (см. блок про media_files.folder_id). Этот файл — на случай, если
-- вы предпочитаете применить миграцию явно через SQL-редактор
-- Supabase. Идемпотентен: повторный запуск ничего не сломает.

CREATE TABLE IF NOT EXISTS media_folders (
    id SERIAL PRIMARY KEY,
    name VARCHAR(60) NOT NULL,
    color VARCHAR(20) NOT NULL DEFAULT '#6C8EF5',
    icon VARCHAR(16),
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'media_files' AND column_name = 'folder_id'
    ) THEN
        ALTER TABLE media_files ADD COLUMN folder_id INTEGER REFERENCES media_folders(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_media_files_folder_id ON media_files (folder_id);
CREATE INDEX IF NOT EXISTS ix_media_folders_position ON media_folders (position);
