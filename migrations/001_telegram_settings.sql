-- Миграция для Supabase: таблица под StringSession Telethon.
--
-- Строго говоря, выполнять это вручную не обязательно — при старте
-- приложение само делает Base.metadata.create_all(bind=engine)
-- (backend/main.py) и создаёт отсутствующие таблицы, включая эту.
-- Но если вы предпочитаете явные миграции через SQL-редактор Supabase
-- (например, чтобы применить до первого деплоя) — выполните это:

CREATE TABLE IF NOT EXISTS telegram_settings (
    id SERIAL PRIMARY KEY,
    session_string TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- В таблице всегда 0 или 1 строка — CRM работает с одним Telegram-
-- аккаунтом. crud.save_telegram_session_string() сама решает,
-- обновить существующую строку или создать первую.
