-- Миграция для Supabase/PostgreSQL: AI Overview (backend/ai_overview.py,
-- backend/models.py: AIOverviewSnapshot).
--
-- Таблица новая, поэтому обычно создаётся автоматически через
-- Base.metadata.create_all() при старте приложения — выполнять этот
-- файл вручную не обязательно. На случай ручного применения через
-- SQL-редактор Supabase: идемпотентен, повторный запуск ничего не
-- сломает.
--
-- AI Overview — не число и не статистика активности CRM, а
-- структурированная картина "текущее состояние + дерево возможных
-- сценариев" для конкретного контакта, построенная по фактам/событиям,
-- истории чата и предыдущим анализам. История снимков хранится (не
-- перезаписывается), чтобы видеть, как менялась картина со временем.

CREATE TABLE IF NOT EXISTS ai_overview_snapshots (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    current_state TEXT NOT NULL,
    key_factors_json TEXT,
    scenarios_json TEXT NOT NULL,
    change_triggers_json TEXT,
    data_used_json TEXT,
    data_needed_json TEXT,
    confidence VARCHAR(10),
    risk_note TEXT,
    source VARCHAR(20) NOT NULL DEFAULT 'gemini',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ai_overview_snapshots_contact_id ON ai_overview_snapshots (contact_id);
CREATE INDEX IF NOT EXISTS ix_ai_overview_snapshots_created_at ON ai_overview_snapshots (created_at);
