FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для сборки некоторых пакетов (например, часть
# telethon/криптографии может требовать компилятор при отсутствии wheel).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY run.py .

# Каталог для данных (БД, сессия Telegram, кэш медиа). На Render сюда
# монтируется постоянный диск (persistent disk), поэтому данные
# переживают редеплои и рестарты контейнера.
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Render передаёт порт через переменную окружения PORT — слушаем именно
# её, а не захардкоженный 8730 из run.py (тот вариант только для
# локального запуска с автооткрытием браузера).
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
