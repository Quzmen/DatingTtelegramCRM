-- Миграция для Supabase/PostgreSQL: Personal AI Operating System
-- (AIMemoryItem / AIPattern / AIDecision, см. backend/models.py).
--
-- Все три таблицы новые, поэтому обычно создаются автоматически через
-- Base.metadata.create_all() при старте приложения — выполнять этот
-- файл вручную не обязательно. На случай ручного применения через
-- SQL-редактор Supabase: идемпотентен, повторный запуск ничего не
-- сломает.
--
-- Важно: этот слой хранит факты только о САМОМ пользователе (его
-- планы, договорённости, привычки) — не оценки поведения контактов.

CREATE TABLE IF NOT EXISTS ai_memory_items (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(20) NOT NULL DEFAULT 'fact',
    title VARCHAR(300) NOT NULL,
    details TEXT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    related_at TIMESTAMP,
    importance FLOAT NOT NULL DEFAULT 0.5,
    source VARCHAR(30) NOT NULL DEFAULT 'manual',
    source_text TEXT,
    is_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ai_memory_items_kind ON ai_memory_items (kind);
CREATE INDEX IF NOT EXISTS ix_ai_memory_items_contact_id ON ai_memory_items (contact_id);
CREATE INDEX IF NOT EXISTS ix_ai_memory_items_related_at ON ai_memory_items (related_at);

CREATE TABLE IF NOT EXISTS ai_patterns (
    id SERIAL PRIMARY KEY,
    title VARCHAR(300) NOT NULL,
    description TEXT,
    confidence FLOAT NOT NULL DEFAULT 0.5,
    evidence_json TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id SERIAL PRIMARY KEY,
    situation TEXT NOT NULL,
    options_json TEXT NOT NULL,
    chosen_option VARCHAR(300),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
